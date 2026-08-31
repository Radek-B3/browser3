#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

/**
 * Local Browser3 MCP transport.
 *
 * This file owns only the MCP/HTTP adapter. The Browser3 Session API remains
 * the sole owner of the profile, lock, process, proxy, and cleanup.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { pathToFileURL } from "node:url";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

export const MCP_SPEC_REVISION = "2025-11-25";
export const MCP_SDK_VERSION = "1.30.0";
export const ADAPTER_VERSION = "0.1.0";
export const MAX_PROFILE = 5;
export const DEFAULT_AGENT_URL = "http://127.0.0.1:17890";
export const DEFAULT_TIMEOUT_MS = 30_000;

const SESSION_ID_RE = /^ses_[0-9a-f]{32}$/;
const SAFE_API_DETAILS = new Set([
  "profile",
  "session_id",
  "build",
  "control",
  "desktop",
  "allowed_builds",
]);

export class AdapterError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "AdapterError";
    this.code = code;
    this.details = details;
  }
}

class OperationCanceled extends Error {
  constructor() {
    super("The operation was canceled.");
    this.name = "OperationCanceled";
  }
}

class SessionApiError extends Error {
  constructor(status, code, details = {}) {
    super("Session API rejected the request.");
    this.name = "SessionApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function safeDetails(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => SAFE_API_DETAILS.has(key)),
  );
}

function apiErrorMessage(code) {
  const messages = {
    invalid_json: "The Session API rejected invalid JSON.",
    invalid_request: "The Session API rejected an invalid request.",
    profile_not_found: "The requested profile does not exist.",
    profile_in_use: "The requested profile is already used by another session.",
    session_not_found: "The requested session does not exist.",
    unsupported_build: "The requested build is not supported.",
    unsupported_control: "The requested control mode is not supported.",
    unsupported_desktop: "The requested desktop mode is not supported.",
    browser_not_found: "The Browser3 runtime was not found.",
    browser_start_failed: "The Browser3 session could not be started.",
    browser_exited_early: "The Browser3 process exited before startup completed.",
    cdp_timeout: "The Browser3 session was not ready before the timeout.",
  };
  return messages[code] || "The Session API rejected the operation.";
}

function mapSessionApiError(error) {
  if (!(error instanceof SessionApiError)) return error;
  return new AdapterError(
    error.code || "agent_error",
    apiErrorMessage(error.code),
    safeDetails(error.details),
  );
}

function assertObject(value, code = "invalid_input") {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new AdapterError(code, "Tool input must be a JSON object.");
  }
}

function rejectUnknownKeys(value, allowed) {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new AdapterError(
        "invalid_input",
        "Input contains a disallowed field.",
        { field: key },
      );
    }
  }
}

function requireConfirmation(value) {
  if (value !== true) {
    throw new AdapterError(
      "confirmation_required",
      "A state-changing operation requires confirm=true.",
    );
  }
}

function validateProfile(value) {
  if (!Number.isSafeInteger(value) || value < 1 || value > MAX_PROFILE) {
    throw new AdapterError(
      "invalid_profile",
      "The MCP adapter supports only profiles 1 through 5.",
      { allowed_profiles: [1, 2, 3, 4, 5] },
    );
  }
}

function validateSessionId(value) {
  if (typeof value !== "string" || !SESSION_ID_RE.test(value)) {
    throw new AdapterError("invalid_session_id", "session_id has an invalid format.");
  }
}

function validateLoopbackCdpUrl(value) {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return url.protocol === "ws:" && url.hostname === "127.0.0.1" &&
      Number.isInteger(Number(url.port)) && Number(url.port) > 0 &&
      !url.username && !url.password;
  } catch {
    return false;
  }
}

function publicSession(session, exposeCdp = false) {
  if (!session || typeof session !== "object") {
    throw new AdapterError("agent_invalid_response", "The Session API returned invalid data.");
  }
  const result = { ...session };
  if (!exposeCdp) delete result.cdp_url;
  if (result.error && typeof result.error === "object") {
    result.error = {
      code: typeof result.error.code === "string" ? result.error.code : "session_failed",
      message: "The session ended with an error.",
      details: safeDetails(result.error.details),
    };
  }
  return result;
}

function jsonText(value) {
  return JSON.stringify(value);
}

/**
 * Minimal HTTP client for the existing Session API. The URL is deliberately
 * restricted to the exact IPv4 loopback address so the environment cannot
 * redirect MCP to the network.
 */
export class SessionApiClient {
  constructor(rawUrl = process.env.BROWSER3_AGENT_URL || DEFAULT_AGENT_URL, options = {}) {
    let url;
    try {
      url = new URL(rawUrl);
    } catch {
      throw new AdapterError("invalid_agent_url", "BROWSER3_AGENT_URL is not a valid URL.");
    }
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" ||
        url.username || url.password || (url.pathname !== "/" && url.pathname !== "") ||
        url.search || url.hash) {
      throw new AdapterError(
        "invalid_agent_url",
        "The Session API must be an HTTP endpoint on 127.0.0.1 without credentials or a path.",
      );
    }
    const port = url.port ? Number(url.port) : 80;
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new AdapterError("invalid_agent_url", "The Session API port is invalid.");
    }
    url.pathname = "/";
    url.port = String(port);
    this.baseUrl = url.toString().replace(/\/$/, "");
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    if (!Number.isInteger(this.timeoutMs) || this.timeoutMs < 1 || this.timeoutMs > 120_000) {
      throw new AdapterError("invalid_timeout", "The Session API timeout must be between 1 and 120000 ms.");
    }
  }

  async request(path, { method = "GET", body, signal } = {}) {
    const controller = new AbortController();
    let timedOut = false;
    let canceled = Boolean(signal?.aborted);
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.timeoutMs);
    const onAbort = () => {
      canceled = true;
      controller.abort();
    };
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        method,
        body,
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          ...(body ? { "content-type": "application/json" } : {}),
          Connection: "close",
        },
      });
      const raw = await response.text();
      let payload;
      try {
        payload = raw ? JSON.parse(raw) : null;
      } catch {
        throw new AdapterError("agent_invalid_response", "The Session API returned invalid JSON.");
      }
      if (!response.ok) {
        const error = payload?.error;
        throw new SessionApiError(
          response.status,
          typeof error?.code === "string" ? error.code : "agent_rejected",
          safeDetails(error?.details),
        );
      }
      if (!payload || typeof payload !== "object") {
        throw new AdapterError("agent_invalid_response", "The Session API returned invalid data.");
      }
      return payload;
    } catch (error) {
      if (error instanceof AdapterError || error instanceof SessionApiError) throw error;
      if (canceled) throw new AdapterError("operation_canceled", "The request was canceled.");
      if (timedOut) throw new AdapterError("agent_timeout", "The Session API did not respond before the timeout.");
      throw new AdapterError("agent_unavailable", "The local browser3-agent is unavailable.");
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
    }
  }

  health(options) {
    return this.request("/v1/health", options);
  }

  listSessions(options) {
    return this.request("/v1/sessions", options);
  }

  getSession(sessionId, options) {
    return this.request(`/v1/sessions/${encodeURIComponent(sessionId)}`, options);
  }

  createSession(payload, options) {
    return this.request("/v1/sessions", {
      ...options,
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  deleteSession(sessionId, options) {
    return this.request(`/v1/sessions/${encodeURIComponent(sessionId)}`, {
      ...options,
      method: "DELETE",
    });
  }
}

async function awaitCancellable(promise, signal, onSuccessAfterCancel = async () => {}) {
  if (!signal) return promise;
  if (signal.aborted) {
    Promise.resolve(promise).then(onSuccessAfterCancel).catch(() => {});
    throw new OperationCanceled();
  }
  let onAbort;
  const canceled = new Promise((_, reject) => {
    onAbort = () => reject(new OperationCanceled());
    signal.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([promise, canceled]);
  } catch (error) {
    if (error instanceof OperationCanceled) {
      Promise.resolve(promise).then(onSuccessAfterCancel).catch(() => {});
    }
    throw error;
  } finally {
    signal.removeEventListener("abort", onAbort);
  }
}

export class Browser3McpAdapter {
  constructor(api = new SessionApiClient()) {
    this.api = api;
    this.ownedSessions = new Set();
  }

  async health(signal) {
    try {
      return await this.api.health({ signal });
    } catch (error) {
      throw mapSessionApiError(error);
    }
  }

  async listSessions(signal) {
    try {
      const result = await this.api.listSessions({ signal });
      if (!Array.isArray(result.sessions)) {
        throw new AdapterError("agent_invalid_response", "The Session API returned an invalid session list.");
      }
      return { sessions: result.sessions.map((session) => publicSession(session)) };
    } catch (error) {
      throw mapSessionApiError(error);
    }
  }

  async getSession(args, signal) {
    assertObject(args);
    rejectUnknownKeys(args, new Set(["session_id"]));
    validateSessionId(args.session_id);
    try {
      return publicSession(await this.api.getSession(args.session_id, { signal }));
    } catch (error) {
      throw mapSessionApiError(error);
    }
  }

  validateStart(args) {
    assertObject(args);
    rejectUnknownKeys(args, new Set(["profile", "proxy", "allow_proxy", "desktop", "confirm"]));
    validateProfile(args.profile);
    requireConfirmation(args.confirm);
    const proxy = args.proxy ?? false;
    if (typeof proxy !== "boolean") {
      throw new AdapterError("invalid_input", "proxy must be a boolean.", { field: "proxy" });
    }
    const allowProxy = args.allow_proxy ?? false;
    if (typeof allowProxy !== "boolean") {
      throw new AdapterError("invalid_input", "allow_proxy must be a boolean.", { field: "allow_proxy" });
    }
    if (proxy !== allowProxy) {
      throw new AdapterError(
        "proxy_permission_required",
        "Proxy requires both proxy=true and allow_proxy=true.",
      );
    }
    const desktop = args.desktop ?? "current";
    if (desktop !== "current" && desktop !== "isolated") {
      throw new AdapterError("invalid_input", "desktop must be current or isolated.", { field: "desktop" });
    }
    return { profile: args.profile, proxy, build: "Release", control: "cdp", desktop };
  }

  async startSession(args, signal) {
    const payload = this.validateStart(args);
    const request = this.api.createSession(payload);
    let result;
    try {
      result = await awaitCancellable(request, signal, async (started) => {
        if (started?.session_id) {
          try {
            await this.api.deleteSession(started.session_id);
          } catch {
            // After cancellation, the state cannot be reported safely; the agent remains the source of truth.
          }
        }
      });
    } catch (error) {
      if (error instanceof OperationCanceled) {
        throw new AdapterError("operation_canceled", "Session start was canceled; its result must not be treated as complete.");
      }
      throw mapSessionApiError(error);
    }
    if (result.status !== "ready" || !validateLoopbackCdpUrl(result.cdp_url)) {
      throw new AdapterError(
        "agent_invalid_response",
        "The Session API did not return a ready loopback CDP session.",
      );
    }
    validateSessionId(result.session_id);
    this.ownedSessions.add(result.session_id);
    return publicSession(result, true);
  }

  async connectSession(args, signal) {
    assertObject(args);
    rejectUnknownKeys(args, new Set(["session_id", "profile", "confirm"]));
    validateSessionId(args.session_id);
    validateProfile(args.profile);
    requireConfirmation(args.confirm);
    let result;
    try {
      result = await this.api.getSession(args.session_id, { signal });
    } catch (error) {
      throw mapSessionApiError(error);
    }
    if (result.profile !== args.profile) {
      throw new AdapterError("profile_mismatch", "The session does not belong to the requested profile.", { profile: args.profile });
    }
    if (result.status !== "ready") {
      throw new AdapterError("session_not_ready", "The session is not in the ready state.", { status: result.status });
    }
    if (!validateLoopbackCdpUrl(result.cdp_url)) {
      throw new AdapterError("agent_invalid_response", "The session does not have a valid loopback CDP endpoint.");
    }
    return publicSession(result, true);
  }

  async stopSession(args, signal) {
    assertObject(args);
    rejectUnknownKeys(args, new Set(["session_id", "confirm"]));
    validateSessionId(args.session_id);
    requireConfirmation(args.confirm);
    const request = this.api.deleteSession(args.session_id);
    let result;
    try {
      result = await awaitCancellable(request, signal);
    } catch (error) {
      if (error instanceof OperationCanceled) {
        throw new AdapterError("operation_canceled", "Session stop was canceled; check the state with session_get.");
      }
      throw mapSessionApiError(error);
    }
    this.ownedSessions.delete(args.session_id);
    return publicSession(result);
  }

  async cleanupOwned() {
    const sessions = [...this.ownedSessions];
    this.ownedSessions.clear();
    for (const sessionId of sessions) {
      try {
        await this.api.deleteSession(sessionId);
      } catch {
        // Cleanup is best-effort; the Session API remains the sole source of truth.
      }
    }
  }
}

const TOOL_OUTPUT_SCHEMA = {
  type: "object",
  properties: {
    ok: { type: "boolean" },
    operation: { type: "string" },
    data: {},
  },
  required: ["ok", "operation", "data"],
  additionalProperties: false,
};

export const TOOL_DEFINITIONS = [
  {
    name: "browser3_health",
    description: "Checks local browser3-agent availability and the number of active sessions.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    outputSchema: TOOL_OUTPUT_SCHEMA,
  },
  {
    name: "browser3_sessions_list",
    description: "Lists local Browser3 session status without CDP endpoints.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    outputSchema: TOOL_OUTPUT_SCHEMA,
  },
  {
    name: "browser3_session_get",
    description: "Gets one local Browser3 session's status; the CDP endpoint is exposed only through session_connect.",
    inputSchema: {
      type: "object",
      properties: { session_id: { type: "string", pattern: "^ses_[0-9a-f]{32}$" } },
      required: ["session_id"],
      additionalProperties: false,
    },
    outputSchema: TOOL_OUTPUT_SCHEMA,
  },
  {
    name: "browser3_session_start",
    description: "Starts a Release Browser3 session for profile 1–5; requires confirm=true and returns a loopback cdp_url.",
    inputSchema: {
      type: "object",
      properties: {
        profile: { type: "integer", minimum: 1, maximum: 5 },
        proxy: { type: "boolean", default: false },
        allow_proxy: { type: "boolean", default: false },
        desktop: { type: "string", enum: ["current", "isolated"], default: "current" },
        confirm: { const: true, description: "Explicit confirmation for a state-changing operation." },
      },
      required: ["profile", "confirm"],
      additionalProperties: false,
    },
    outputSchema: TOOL_OUTPUT_SCHEMA,
  },
  {
    name: "browser3_session_connect",
    description: "Reconnects to a ready session for a specific profile and returns its loopback cdp_url; requires confirm=true.",
    inputSchema: {
      type: "object",
      properties: {
        session_id: { type: "string", pattern: "^ses_[0-9a-f]{32}$" },
        profile: { type: "integer", minimum: 1, maximum: 5 },
        confirm: { const: true, description: "Explicit confirmation to expose the CDP capability." },
      },
      required: ["session_id", "profile", "confirm"],
      additionalProperties: false,
    },
    outputSchema: TOOL_OUTPUT_SCHEMA,
  },
  {
    name: "browser3_session_stop",
    description: "Stops a Browser3 session through the sole Session API lifecycle and cleans up the process and lock; requires confirm=true.",
    inputSchema: {
      type: "object",
      properties: {
        session_id: { type: "string", pattern: "^ses_[0-9a-f]{32}$" },
        confirm: { const: true, description: "Explicit confirmation for a state-changing operation." },
      },
      required: ["session_id", "confirm"],
      additionalProperties: false,
    },
    outputSchema: TOOL_OUTPUT_SCHEMA,
  },
];

function success(operation, data) {
  const payload = { ok: true, operation, data };
  return {
    content: [{ type: "text", text: jsonText(payload) }],
    structuredContent: payload,
  };
}

function failure(error) {
  const adapterError = error instanceof AdapterError
    ? error
    : new AdapterError("internal_error", "The local MCP adapter encountered an internal error.");
  const payload = {
    error: {
      code: adapterError.code,
      message: adapterError.message,
      details: adapterError.details || {},
    },
  };
  return { content: [{ type: "text", text: jsonText(payload) }], isError: true };
}

export function createServer(adapter = new Browser3McpAdapter()) {
  const server = new Server(
    { name: "browser3-mcp", version: ADAPTER_VERSION },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: TOOL_DEFINITIONS,
  }));

  server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
    const name = request.params.name;
    const args = request.params.arguments || {};
    const signal = extra?.signal;
    try {
      switch (name) {
        case "browser3_health":
          assertObject(args);
          rejectUnknownKeys(args, new Set());
          return success(name, await adapter.health(signal));
        case "browser3_sessions_list":
          assertObject(args);
          rejectUnknownKeys(args, new Set());
          return success(name, await adapter.listSessions(signal));
        case "browser3_session_get":
          return success(name, await adapter.getSession(args, signal));
        case "browser3_session_start":
          return success(name, await adapter.startSession(args, signal));
        case "browser3_session_connect":
          return success(name, await adapter.connectSession(args, signal));
        case "browser3_session_stop":
          return success(name, await adapter.stopSession(args, signal));
        default:
          return failure(new AdapterError("unknown_tool", "The Browser3 MCP tool does not exist."));
      }
    } catch (error) {
      if (!(error instanceof AdapterError)) {
        console.error("browser3-mcp: unexpected tool error", error?.name || "Error");
      }
      return failure(error);
    }
  });

  return { server, adapter };
}

export async function main() {
  const { server, adapter } = createServer();
  let shuttingDown = false;
  const cleanup = async (exitCode = 0) => {
    if (shuttingDown) return;
    shuttingDown = true;
    await adapter.cleanupOwned();
    process.exitCode = exitCode;
  };
  server.onclose = () => { void cleanup(0); };
  const stdinClosed = () => {
    if (shuttingDown) return;
    void cleanup(0).finally(() => process.exit(0));
  };
  process.stdin.once("end", stdinClosed);
  process.stdin.once("close", stdinClosed);
  process.stdin.once("error", stdinClosed);
  process.stdin.resume();
  const signalHandler = (signal, exitCode) => {
    void cleanup(exitCode).finally(() => process.exit());
  };
  process.once("SIGINT", () => signalHandler("SIGINT", 130));
  process.once("SIGTERM", () => signalHandler("SIGTERM", 143));
  const transport = new StdioServerTransport();
  try {
    await server.connect(transport);
  } finally {
    await cleanup(process.exitCode || 0);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error("browser3-mcp: failed to start", error?.name || "Error");
    process.exitCode = 1;
  });
}
