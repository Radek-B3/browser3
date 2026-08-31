# Local Browser3 MCP adapter

This directory contains a thin transport adapter between a standard MCP client
and the existing `browser3-agent` Session API v1. The adapter does not own
Chromium, profiles, proxy configuration, fingerprint configuration, or locks.
Those responsibilities remain exclusively with `browser3-agent` and the
launcher.

## Pinned protocol and installation

The implementation uses the official `@modelcontextprotocol/sdk@1.30.0` and is
validated against MCP specification `2025-11-25`. The version is pinned in both
`package.json` and `package-lock.json`; do not run `npm update` as part of normal
operation.

After unpacking Browser3, run this command in this directory:

```powershell
npm ci
```

Node.js 18 or newer is required. In another terminal, start the single owner of
the browser lifecycle:

```powershell
python browser3_agent.py --listen 127.0.0.1 --port 17890
```

The MCP client starts the server over `stdio`; the adapter does not listen on a
TCP port. The client configuration example uses an absolute path to
`mcp/browser3_mcp.mjs`, `node` as the command, and `mcp` as the working
directory. `BROWSER3_AGENT_URL` is accepted only in the form
`http://127.0.0.1:<port>` without a path, credentials, or another hostname.

The minimal example MCP client performs tool discovery and a health check:

```powershell
node mcp/browser3_mcp_client.mjs
```

You can optionally exercise the complete lifecycle for profile 1 through 5.
The example requires `confirm=true`, returns a loopback `cdp_url`, reconnects,
and always attempts to stop the session:

```powershell
node mcp/browser3_mcp_client.mjs 1
```

## Tools and permissions

| Tool | Purpose | Gate |
|---|---|---|
| `browser3_health` | agent availability and active session count | none, read-only |
| `browser3_sessions_list` | session status without a CDP URL | none, read-only |
| `browser3_session_get` | one session's status without a CDP URL | none, read-only |
| `browser3_session_start` | start a profile in `Release`, `control=cdp` | `confirm=true`; proxy also requires `allow_proxy=true` |
| `browser3_session_connect` | reconnect to a `ready` session | matching `profile` and `confirm=true` |
| `browser3_session_stop` | graceful `Browser.close` and cleanup | `confirm=true` |

The MCP layer accepts only profiles 1–5. Start always sends exactly
`build=Release` and `control=cdp` to the existing Session API; it does not
create an alternative lifecycle. Invalid types, unknown fields, invalid
profiles, profile mismatches, lock collisions, timeouts, cancellation, and an
unavailable agent produce deterministic errors with a code and safe details.
Status responses intentionally do not expose the CDP capability; it is available
only after an explicit `session_connect` call.

When a mutating request is canceled, its result is not treated as successful. If
the Session API completes the request later, the adapter makes a best-effort
DELETE for a canceled start. When the `stdio` client disconnects, the adapter
cleans up sessions that it started itself. Hard process termination, such as
`SIGKILL` or a power failure, cannot be observed; in that case, inspect the state
with `browser3_sessions_list` and let the Session API perform cleanup.

## Security boundary

- the only MCP transport is local `stdio`, with no HTTP/SSE/Streamable HTTP listener;
- the only downstream endpoint is the exact IPv4 loopback address `127.0.0.1`;
- proxy use is disabled by default and requires two explicit confirmations;
- `browser3-agent` remains the sole owner of the profile, OS lock, forwarder,
  Chromium process, and persistent data;
- the adapter contains no JavaScript fingerprint hooks, CDP injection, webdriver
  flags, or copy of the masking logic; masking remains in the native C++
  Browser3 runtime;
- MCP messages and responses do not carry credentials, profile contents, or
  local paths.

This adapter is not WebMCP. WebMCP is a different contract between a web page
and an agent. This adapter only transports the Browser3 session lifecycle and its
loopback CDP capability; page actions remain the responsibility of an optional
Playwright, Puppeteer, or agent-browser client connected to the returned
endpoint.

## Verification

```powershell
npm test
node --check browser3_mcp.mjs
node --check browser3_mcp_client.mjs
```

Release integration verification uses the same adapter against `browser3-agent`
with `build=Release` and profiles 1–5. The MCP test is not a new fingerprint
gate: it does not modify the CDP-free launcher, native masking, or existing
baselines.

Official sources: [MCP specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25),
[TypeScript SDK](https://github.com/modelcontextprotocol/typescript-sdk/tree/v1.x),
and [pinned release 1.30.0](https://www.npmjs.com/package/@modelcontextprotocol/sdk/v/1.30.0).
