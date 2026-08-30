# Browser3 with Puppeteer

This example connects Puppeteer to a Browser3 session that is owned by the local
Browser3 Session API. Puppeteer does not launch Chromium, choose a profile, choose a
proxy, or inject fingerprinting code.

## Clean setup from the Browser3 release ZIP

1. Download the official Windows x64 Browser3 release and verify its signed checksum
   manifest.
2. Extract the complete archive and keep the `runtime\` directory intact.
3. In the extracted Browser3 directory, start the local Session API in one terminal:

   ```powershell
   python browser3_agent.py --listen 127.0.0.1 --port 17890
   ```

4. In a second terminal, enter this directory and install the exact client dependency:

   ```powershell
   cd examples\puppeteer
   npm ci
   ```

5. Run the example with an existing or newly generated profile:

   ```powershell
   node puppeteer.mjs --profile 1
   ```

The example uses `https://example.com` by default. Pass a URL explicitly when needed:

```powershell
node puppeteer.mjs --profile 1 https://example.com
```

The `browser3_agent.py` process owns the Browser3 process, persistent profile lock and
cleanup. Stop the session with the example's normal exit path or with the Session API
`DELETE /v1/sessions/<session_id>` endpoint. Do not run `npm exec puppeteer` or
`puppeteer.launch()` here: those commands would start a different browser lifecycle.

## CDP and fingerprinting boundary

The integration is opt-in and uses a loopback CDP endpoint. Browser3 fingerprint
masking remains in the packaged native Chromium layer. This example contains no
`Proxy` object, page hook, `addScriptToEvaluateOnNewDocument`, or other fingerprint
injection.

An active Puppeteer connection can enable the CDP Runtime domain. Detector tooling may
therefore report a Developer Tools signal. An open CDP endpoint without a client and a
Puppeteer attach are measured separately; neither result is a universal anti-bot
verdict.

The package is pinned to Puppeteer Core `25.3.0` and requires Node.js `22.12.0` or
newer. The package downloads Puppeteer libraries only; Browser3 remains the source of
the browser runtime.
