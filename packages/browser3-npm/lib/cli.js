// SPDX-License-Identifier: MPL-2.0

import { spawn } from "node:child_process";
import { discoverPython } from "./python.js";

const COMMANDS = new Set(["install", "launch", "update", "list", "doctor"]);
const SIGNAL_CODES = { SIGINT: 130, SIGTERM: 143, SIGHUP: 129 };

export const USAGE = [
  "Usage: browser3 <install|launch|update|list|doctor> [options]",
  "",
  "This npm command delegates to the installed Python browser3 client.",
  "It does not install Python, browser3 from PyPI, or Chromium automatically.",
].join("\n");

function print(stream, message) {
  stream.write(`${message}\n`);
}

function forward(python, args, {
  spawnImpl = spawn,
  processObject = process,
} = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const child = spawnImpl(
      python.command,
      [...python.prefixArgs, "-m", "browser3", ...args],
      { stdio: "inherit", windowsHide: true },
    );

    const cleanSignals = [];
    for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
      const handler = () => {
        // stdin/stdout/stderr are inherited, so only the control signal needs
        // forwarding. The child remains the authority for its exit status.
        if (typeof child.kill === "function") child.kill(signal);
      };
      if (typeof processObject.once === "function") {
        processObject.once(signal, handler);
        cleanSignals.push(() => processObject.removeListener?.(signal, handler));
      }
    }

    const finish = (code) => {
      if (settled) return;
      settled = true;
      for (const clean of cleanSignals) clean();
      resolve(code);
    };

    child.once("error", (error) => {
      print(
        processObject.stderr ?? process.stderr,
        `browser3: could not start Python (${error.message}). ` +
          "Install the Python browser3 client and retry.",
      );
      finish(127);
    });
    child.once("exit", (code, signal) => {
      if (signal) finish(SIGNAL_CODES[signal] ?? 1);
      else finish(Number.isInteger(code) ? code : 1);
    });
  });
}

/**
 * Run the npm wrapper. Dependencies are injectable for deterministic tests;
 * production uses the real child-process APIs and never touches the network.
 */
export async function main(
  argv = process.argv.slice(2),
  {
    discover = discoverPython,
    spawnImpl,
    processObject = process,
  } = {},
) {
  if (argv.length === 0 || argv[0] === "--help" || argv[0] === "-h") {
    print(processObject.stdout ?? process.stdout, USAGE);
    return argv.length === 0 ? 64 : 0;
  }
  if (!COMMANDS.has(argv[0])) {
    print(processObject.stderr ?? process.stderr, `browser3: unsupported command: ${argv[0]}`);
    print(processObject.stderr ?? process.stderr, USAGE);
    return 64;
  }

  let python;
  try {
    python = discover();
  } catch (error) {
    print(processObject.stderr ?? process.stderr, `browser3: ${error.message}`);
    return 78;
  }
  return forward(python, argv, { spawnImpl, processObject });
}
