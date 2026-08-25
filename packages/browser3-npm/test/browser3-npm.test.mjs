// SPDX-License-Identifier: MPL-2.0

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { discoverPython } from "../lib/python.js";
import { main } from "../lib/cli.js";

function output() {
  let text = "";
  return {
    write(value) { text += value; },
    get text() { return text; },
  };
}

test("package uses the owner-scoped public npm identity", () => {
  const packageJson = JSON.parse(
    readFileSync(new URL("../package.json", import.meta.url), "utf8"),
  );
  assert.equal(packageJson.name, "@radek-b3/browser3");
  assert.equal(packageJson.bin.browser3, "bin/browser3.js");
  assert.equal(packageJson.publishConfig.access, "public");
});

test("discovery prefers py 3.13 and requires the Python client", () => {
  const calls = [];
  const python = discoverPython({
    platform: "win32",
    arch: "x64",
    spawnSyncImpl(command, args) {
      calls.push([command, args]);
      return { status: 0, stdout: "3.13;1", stderr: "" };
    },
  });
  assert.deepEqual(python, { command: "py", prefixArgs: ["-3.13"], version: "3.13" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "py");
  assert.equal(calls[0][1][0], "-3.13");
});

test("discovery falls through old or missing clients to python", () => {
  const calls = [];
  const python = discoverPython({
    platform: "win32",
    arch: "x64",
    spawnSyncImpl(command, args) {
      calls.push([command, args]);
      if (command === "python") return { status: 0, stdout: "3.11;1" };
      return { status: 1, stdout: "", stderr: "not installed" };
    },
  });
  assert.equal(python.command, "python");
  assert.equal(python.version, "3.11");
  assert.equal(calls.length, 5);
});

test("discovery fails closed when Python client is absent", () => {
  assert.throws(
    () => discoverPython({
      platform: "win32",
      arch: "x64",
      spawnSyncImpl: () => ({ status: 0, stdout: "3.13;0" }),
    }),
    /does not install Python or fetch a package from PyPI automatically/,
  );
});

test("unsupported platform is rejected before probing", () => {
  assert.throws(
    () => discoverPython({ platform: "linux", arch: "x64", spawnSyncImpl: () => {
      throw new Error("must not probe");
    } }),
    /Windows x64 only/,
  );
});

test("main forwards the command, arguments, stdio, and exit code", async () => {
  const stdout = output();
  const stderr = output();
  const processObject = {
    stdout,
    stderr,
    once() {},
    removeListener() {},
  };
  let spawnCall;
  const code = await main(["launch", "--profile", "1"], {
    discover: () => ({ command: "py", prefixArgs: ["-3.13"] }),
    processObject,
    spawnImpl(command, args, options) {
      spawnCall = { command, args, options };
      return {
        once(event, callback) {
          if (event === "exit") callback(7, null);
        },
        kill() {},
      };
    },
  });
  assert.equal(code, 7);
  assert.deepEqual(spawnCall, {
    command: "py",
    args: ["-3.13", "-m", "browser3", "launch", "--profile", "1"],
    options: { stdio: "inherit", windowsHide: true },
  });
});

test("missing Python client produces a clear nonzero diagnostic", async () => {
  const stderr = output();
  const code = await main(["doctor"], {
    discover: () => { throw new Error("requires an installed Python browser3 client"); },
    processObject: { stdout: output(), stderr, once() {}, removeListener() {} },
  });
  assert.equal(code, 78);
  assert.match(stderr.text, /requires an installed Python browser3 client/);
  assert.doesNotMatch(stderr.text, /C:\\Users\\|token|password/i);
});

test("unsupported commands fail without invoking Python", async () => {
  const stderr = output();
  let discovered = false;
  const code = await main(["upgrade"], {
    discover: () => { discovered = true; },
    processObject: { stdout: output(), stderr, once() {}, removeListener() {} },
  });
  assert.equal(code, 64);
  assert.equal(discovered, false);
  assert.match(stderr.text, /unsupported command/);
});
