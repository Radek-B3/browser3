// SPDX-License-Identifier: MPL-2.0

/**
 * Locate a supported Python interpreter which already has the Browser3
 * client installed.  The npm package deliberately does not install Python,
 * pip packages, or a browser runtime: the Python client remains the single
 * implementation of release verification and runtime caching.
 */

import { spawnSync } from "node:child_process";

export const MIN_PYTHON = Object.freeze([3, 10]);

const PROBE = [
  "-c",
  "import importlib.util,sys; " +
  "print(f'{sys.version_info[0]}.{sys.version_info[1]};' + " +
  "('1' if importlib.util.find_spec('browser3') else '0'))",
];

function text(value) {
  if (value === undefined || value === null) return "";
  return Buffer.isBuffer(value) ? value.toString("utf8") : String(value);
}

function displayCandidate(candidate) {
  return [candidate.command, ...candidate.prefixArgs].join(" ");
}

function parseProbe(result) {
  if (!result || result.error || result.status !== 0) return null;
  const match = text(result.stdout).trim().match(/^(\d+)\.(\d+);([01])$/);
  if (!match) return null;
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    hasClient: match[3] === "1",
  };
}

/**
 * Return an interpreter descriptor or throw a diagnostic that is safe to show
 * to users. `spawnSyncImpl` is injectable so tests never need a real Python or
 * network access.
 */
export function discoverPython({
  platform = process.platform,
  arch = process.arch,
  spawnSyncImpl = spawnSync,
} = {}) {
  if (platform !== "win32") {
    throw new Error(
      "Browser3 npm wrapper supports Windows x64 only; the Python browser3 client " +
      "does not provide a cross-platform runtime installer.",
    );
  }
  if (arch !== "x64") {
    throw new Error(
      "Browser3 npm wrapper supports Windows x64 only; this process is not x64.",
    );
  }

  // The Python launcher is intentionally tried in descending explicit minor
  // version order. The final `python` fallback is retained for installations
  // that do not ship the Windows `py` launcher.
  const candidates = [
    { command: "py", prefixArgs: ["-3.13"] },
    { command: "py", prefixArgs: ["-3.12"] },
    { command: "py", prefixArgs: ["-3.11"] },
    { command: "py", prefixArgs: ["-3.10"] },
    { command: "python", prefixArgs: [] },
  ];
  const attempts = [];

  for (const candidate of candidates) {
    let result;
    try {
      result = spawnSyncImpl(
        candidate.command,
        [...candidate.prefixArgs, ...PROBE],
        {
          encoding: "utf8",
          stdio: ["ignore", "pipe", "pipe"],
          windowsHide: true,
        },
      );
    } catch (error) {
      attempts.push(`${displayCandidate(candidate)} unavailable`);
      continue;
    }
    const probed = parseProbe(result);
    if (!probed) {
      attempts.push(`${displayCandidate(candidate)} did not return a usable probe`);
      continue;
    }
    if (
      probed.major < MIN_PYTHON[0] ||
      (probed.major === MIN_PYTHON[0] && probed.minor < MIN_PYTHON[1])
    ) {
      attempts.push(`${displayCandidate(candidate)} is older than Python 3.10`);
      continue;
    }
    if (!probed.hasClient) {
      attempts.push(`${displayCandidate(candidate)} has no Python browser3 client`);
      continue;
    }
    return {
      command: candidate.command,
      prefixArgs: [...candidate.prefixArgs],
      version: `${probed.major}.${probed.minor}`,
    };
  }

  throw new Error(
    "Browser3 npm wrapper requires an installed Python 3.10+ interpreter with the " +
      "Python browser3 client. It does not install Python or fetch a package from " +
      "PyPI automatically. Install the client first with `python -m pip install " +
      "browser3`, then retry. Checked: " + attempts.join(", "),
  );
}
