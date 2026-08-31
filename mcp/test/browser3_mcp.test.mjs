import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import {
  Browser3McpAdapter,
  SessionApiClient,
  TOOL_DEFINITIONS,
} from "../browser3_mcp.mjs";

const SESSION_ID = `ses_${"a".repeat(32)}`;
const CDP_URL = "ws://127.0.0.1:49152/devtools/browser/test";

function session(overrides = {}) {
  return {
    session_id: SESSION_ID,
    status: "ready",
    profile: 1,
    proxy: false,
    build: "Release",
    control: "cdp",
    desktop: "current",
    desktop_name: null,
    cdp_url: CDP_URL,
    created_at: "2026-08-30T00:00:00Z",
    updated_at: "2026-08-30T00:00:00Z",
    error: null,
    ...overrides,
  };
}

class FakeApi {
  constructor() {
    this.createdPayload = null;
    this.deleted = [];
    this.current = session();
  }

  health() { return Promise.resolve({ status: "ok", api_version: "v1", active_sessions: 1 }); }
  listSessions() { return Promise.resolve({ sessions: [this.current] }); }
  getSession() { return Promise.resolve(this.current); }
  createSession(payload) {
    this.createdPayload = payload;
    this.current = session({ profile: payload.profile, proxy: payload.proxy, desktop: payload.desktop });
    return Promise.resolve(this.current);
  }
  deleteSession(sessionId) {
    this.deleted.push(sessionId);
    this.current = session({ session_id: sessionId, status: "stopped", cdp_url: null });
    return Promise.resolve(this.current);
  }
}

function validStart(overrides = {}) {
  return { profile: 1, confirm: true, ...overrides };
}

test("pinned definitions expose only the expected tools", () => {
  assert.deepEqual(
    TOOL_DEFINITIONS.map((tool) => tool.name),
    [
      "browser3_health",
      "browser3_sessions_list",
      "browser3_session_get",
      "browser3_session_start",
      "browser3_session_connect",
      "browser3_session_stop",
    ],
  );
  const start = TOOL_DEFINITIONS.find((tool) => tool.name === "browser3_session_start");
  assert.deepEqual(start.inputSchema.properties.profile.minimum, 1);
  assert.deepEqual(start.inputSchema.properties.profile.maximum, 5);
  assert.deepEqual(start.inputSchema.properties.confirm.const, true);
});

test("the SDK pin matches in the manifest and lockfile", () => {
  const packageJson = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  const lock = JSON.parse(readFileSync(new URL("../package-lock.json", import.meta.url), "utf8"));
  assert.equal(packageJson.dependencies["@modelcontextprotocol/sdk"], "1.30.0");
  assert.equal(lock.packages[""].dependencies["@modelcontextprotocol/sdk"], "1.30.0");
  assert.equal(lock.packages["node_modules/@modelcontextprotocol/sdk"].version, "1.30.0");
});

test("SessionApiClient rejects endpoints outside the exact IPv4 loopback", () => {
  assert.throws(() => new SessionApiClient("http://localhost:17890"), /127\.0\.0\.1/);
  assert.throws(() => new SessionApiClient("https://127.0.0.1:17890"), /HTTP endpoint/);
  assert.throws(() => new SessionApiClient("http://127.0.0.1:17890/path"), /HTTP endpoint/);
  assert.doesNotThrow(() => new SessionApiClient("http://127.0.0.1:17890"));
});

test("start requires explicit permission, a whitelist, and one Session API payload", async () => {
  const api = new FakeApi();
  const adapter = new Browser3McpAdapter(api);
  await assert.rejects(adapter.startSession({ profile: 1 }, undefined), { code: "confirmation_required" });
  await assert.rejects(adapter.startSession(validStart({ profile: 6 }), undefined), { code: "invalid_profile" });
  await assert.rejects(adapter.startSession(validStart({ proxy: true }), undefined), { code: "proxy_permission_required" });
  await assert.rejects(adapter.startSession(validStart({ extra: 1 }), undefined), { code: "invalid_input" });

  const created = await adapter.startSession(validStart({ proxy: true, allow_proxy: true, desktop: "isolated" }));
  assert.equal(created.cdp_url, CDP_URL);
  assert.deepEqual(api.createdPayload, {
    profile: 1,
    proxy: true,
    build: "Release",
    control: "cdp",
    desktop: "isolated",
  });
  assert.deepEqual([...adapter.ownedSessions], [SESSION_ID]);
});

test("list/get redact the CDP capability, while connect exposes it only for a matching profile", async () => {
  const api = new FakeApi();
  const adapter = new Browser3McpAdapter(api);
  const list = await adapter.listSessions();
  assert.equal(Object.hasOwn(list.sessions[0], "cdp_url"), false);
  const get = await adapter.getSession({ session_id: SESSION_ID });
  assert.equal(Object.hasOwn(get, "cdp_url"), false);
  await assert.rejects(
    adapter.connectSession({ session_id: SESSION_ID, profile: 2, confirm: true }),
    { code: "profile_mismatch" },
  );
  await assert.rejects(
    adapter.connectSession({ session_id: SESSION_ID, profile: 1 }),
    { code: "confirmation_required" },
  );
  const connected = await adapter.connectSession({ session_id: SESSION_ID, profile: 1, confirm: true });
  assert.equal(connected.cdp_url, CDP_URL);
});

test("stop requires confirmation and delegates cleanup to the Session API", async () => {
  const api = new FakeApi();
  const adapter = new Browser3McpAdapter(api);
  await adapter.startSession(validStart());
  await assert.rejects(adapter.stopSession({ session_id: SESSION_ID }), { code: "confirmation_required" });
  const stopped = await adapter.stopSession({ session_id: SESSION_ID, confirm: true });
  assert.equal(stopped.status, "stopped");
  assert.deepEqual(api.deleted, [SESSION_ID]);
  assert.equal(adapter.ownedSessions.size, 0);
});

test("cleanupOwned delegates cleanup for all owned sessions", async () => {
  const api = new FakeApi();
  const adapter = new Browser3McpAdapter(api);
  await adapter.startSession(validStart());
  adapter.ownedSessions.add(`ses_${"b".repeat(32)}`);
  await adapter.cleanupOwned();
  assert.deepEqual(api.deleted, [SESSION_ID, `ses_${"b".repeat(32)}`]);
  assert.equal(adapter.ownedSessions.size, 0);
});

test("a canceled start does not report success and cleans up a session created after the request completes", async () => {
  const api = new FakeApi();
  let resolveCreate;
  api.createSession = () => new Promise((resolve) => { resolveCreate = resolve; });
  const adapter = new Browser3McpAdapter(api);
  const controller = new AbortController();
  const pending = adapter.startSession(validStart(), controller.signal);
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort();
  await assert.rejects(pending, { code: "operation_canceled" });
  resolveCreate(session());
  const deadline = Date.now() + 1000;
  while (api.deleted.length === 0 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.deepEqual(api.deleted, [SESSION_ID]);
});

test("a standard MCP client discovers tools over stdio", async (t) => {
  const serverPath = fileURLToPath(new URL("../browser3_mcp.mjs", import.meta.url));
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverPath],
    cwd: path.dirname(serverPath),
    stderr: "pipe",
  });
  const client = new Client(
    { name: "browser3-mcp-test-client", version: "0.1.0" },
    { capabilities: {} },
  );
  t.after(async () => { await client.close().catch(() => {}); });
  await client.connect(transport);
  const listing = await client.listTools();
  assert.equal(listing.tools.length, 6);
  assert.ok(listing.tools.some((tool) => tool.name === "browser3_session_start"));
  const invalid = await client.callTool({
    name: "browser3_session_start",
    arguments: { profile: 1 },
  });
  assert.equal(invalid.isError, true);
  assert.equal(JSON.parse(invalid.content[0].text).error.code, "confirmation_required");
});
