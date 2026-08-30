// SPDX-License-Identifier: MPL-2.0
// Browser3 owns Chromium lifecycle; Puppeteer is only a CDP client.
import puppeteer from "puppeteer-core";

const agentUrl = process.env.BROWSER3_AGENT_URL || "http://127.0.0.1:17890";
const args = process.argv.slice(2);
let profile = 1;
let target = "https://example.com";

if (args[0] === "--profile") {
  profile = Number(args[1]);
  args.splice(0, 2);
}
if (args[0]) target = args[0];
if (!Number.isInteger(profile) || profile < 1) {
  throw new Error("--profile must be a positive integer");
}

async function request(path, options = {}) {
  const response = await fetch(`${agentUrl}${path}`, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(`${response.status}: ${JSON.stringify(payload)}`);
  }
  return payload;
}

const session = await request("/v1/sessions", {
  method: "POST",
  body: JSON.stringify({ profile, proxy: false, build: "Release", control: "cdp" }),
});
let browser;
try {
  browser = await puppeteer.connect({
    browserWSEndpoint: session.cdp_url,
    defaultViewport: null,
  });
  const page = (await browser.pages())[0] || await browser.newPage();
  await page.goto(target, { waitUntil: "load" });
  console.log(JSON.stringify({
    client: "puppeteer-core",
    client_version: "25.3.0",
    browser3_session: session.session_id,
    profile,
    title: await page.title(),
    url: page.url(),
  }));
} finally {
  if (browser) await browser.disconnect().catch(() => {});
  await request(`/v1/sessions/${encodeURIComponent(session.session_id)}`, {
    method: "DELETE",
  }).catch(() => {});
}
