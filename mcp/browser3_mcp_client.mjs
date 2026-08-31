#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

/** Minimal example MCP client: discovery, health, and optional profile start/stop. */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SERVER = path.join(HERE, "browser3_mcp.mjs");

function profileArgument(argv) {
  const value = argv[0];
  if (value === undefined) return null;
  if (!/^\d+$/.test(value)) throw new Error("Usage: node browser3_mcp_client.mjs [profile 1-5]");
  const profile = Number(value);
  if (!Number.isInteger(profile) || profile < 1 || profile > 5) {
    throw new Error("Profile must be an integer from 1 through 5.");
  }
  return profile;
}

function toolData(result) {
  if (result?.isError) throw new Error(result.content?.[0]?.text || "The MCP tool failed.");
  return result.structuredContent?.data;
}

export async function main(argv = process.argv.slice(2)) {
  const profile = profileArgument(argv);
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [SERVER],
    cwd: HERE,
    stderr: "inherit",
  });
  const client = new Client(
    { name: "browser3-mcp-example-client", version: "0.1.0" },
    { capabilities: {} },
  );
  let sessionId = null;
  try {
    await client.connect(transport);
    const listing = await client.listTools();
    console.log(JSON.stringify({ tools: listing.tools.map((tool) => tool.name) }));
    if (profile === null) return;

    const started = await client.callTool({
      name: "browser3_session_start",
      arguments: { profile, confirm: true },
    });
    const session = toolData(started);
    sessionId = session.session_id;
    console.log(JSON.stringify({ session_id: sessionId, cdp_url: session.cdp_url }));

    const connected = await client.callTool({
      name: "browser3_session_connect",
      arguments: { session_id: sessionId, profile, confirm: true },
    });
    console.log(JSON.stringify({ connected: Boolean(toolData(connected)?.cdp_url) }));
  } finally {
    if (sessionId) {
      await client.callTool({
        name: "browser3_session_stop",
        arguments: { session_id: sessionId, confirm: true },
      }).catch(() => {});
    }
    await client.close().catch(() => {});
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(`browser3-mcp-client: ${error.message}`);
    process.exitCode = 1;
  });
}
