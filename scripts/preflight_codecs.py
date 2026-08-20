#!/usr/bin/env python3
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0

r"""
Verify at launch time that Windows can decode the media formats Browser3 advertises.

Browser3 does not distribute AAC or H.264 decoders in its FFmpeg build. Playback is
provided by the Windows media stack. This probe checks real AAC/H.264 decoding and
WebRTC H.264 availability, uses VP9/WebM as a control, and caches the machine/build
result below `%LOCALAPPDATA%\Browser3\cache`.

A mismatch is treated as a coherence failure unless `FP_ALLOW_HOST=1` explicitly
downgrades it for diagnostics.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import browser3_paths as paths

PROBE_DIR = os.path.join(ROOT, "scripts", "codec_probe")
CACHE_PATH = paths.CODEC_CACHE_FILE

# Codecs decoded by the operating system after L2 removal. Failure would conflict
# with the capabilities Browser3 advertises. Keys match the probe response.
REQUIRED = {
    "aac": "AAC (Media Foundation)",
    "h264": "H.264 (D3D11 / Media Foundation)",
}

# Forty seconds was insufficient for the first cold start of a monolithic Release
# build. The probe runs once per machine, so a generous ceiling is inexpensive.
PROBE_TIMEOUT_S = 120


class _Handler(BaseHTTPRequestHandler):
    """Serve the probe and samples; `/result` collects the verdict."""

    MIME = {".html": "text/html; charset=utf-8", ".mp4": "video/mp4"}

    def do_GET(self):  # noqa: N802 (name required by BaseHTTPRequestHandler)
        parsed = urlparse(self.path)
        if parsed.path == "/result":
            self.server.result = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        name = os.path.basename(parsed.path) or "index.html"
        path = os.path.join(PROBE_DIR, name)
        # The browser supplies the path; keep it within our probe directory.
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(PROBE_DIR) \
                or not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as fh:
            body = fh.read()

        # The media stack requests byte ranges and may remain in LOADING when a
        # range request is not served instead of reporting a useful error.
        status, start, end = 200, 0, len(body) - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            first, _, last = rng[len("bytes="):].partition("-")
            try:
                start = int(first) if first else 0
                end = int(last) if last else len(body) - 1
            except ValueError:
                start, end = 0, len(body) - 1
            start = max(0, min(start, len(body) - 1))
            end = max(start, min(end, len(body) - 1))
            status = 206

        chunk = body[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type",
                         self.MIME.get(os.path.splitext(name)[1], "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *_args):
        pass

    def handle_one_request(self):
        # Closing the browser at probe completion resets connections. Suppress the
        # resulting traceback because it is expected and obscures real failures.
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True


def _machine_key(chrome_exe):
    """Return inputs that invalidate a cached verdict when they change.

    D3D11/Media Foundation decoding depends on the GPU driver and Windows build;
    the executable identity captures which codecs the binary excludes.
    """
    key = {}
    try:
        st = os.stat(chrome_exe)
        key["binary"] = [os.path.abspath(chrome_exe), int(st.st_mtime), st.st_size]
    except OSError:
        key["binary"] = [os.path.abspath(chrome_exe), None, None]

    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import probe_host as P
        host = P.load_cache() or {}
    except Exception:
        host = {}
    key["win_build"] = (host.get("os") or {}).get("build")
    gpu = host.get("webgpu") or {}
    key["gpu_driver"] = gpu.get("driver") or (host.get("webgl") or {}).get("renderer")
    return key


# Cache an inconclusive verdict briefly: never forever, because the machine may be
# repaired, but long enough to avoid rerunning a slow probe at every launch.
INCONCLUSIVE_TTL_S = 3600


def load_cached(chrome_exe):
    paths.initialize_runtime_state()
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if data.get("key") != _machine_key(chrome_exe):
        return None
    if data.get("inconclusive"):
        if time.time() - (data.get("checked_at_epoch") or 0) > INCONCLUSIVE_TTL_S:
            return None
    return data


def _save(chrome_exe, result, inconclusive=False):
    paths.initialize_runtime_state()
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    payload = {
        "key": _machine_key(chrome_exe),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "checked_at_epoch": int(time.time()),
        "inconclusive": inconclusive,
        "result": result,
    }
    paths.write_json_atomic(CACHE_PATH, payload)
    return payload


def measure(chrome_exe, quiet=False):
    """Run the probe and return its result mapping."""
    if not os.path.isfile(chrome_exe):
        raise RuntimeError(f"chrome.exe was not found: {chrome_exe}")

    # Both media elements load concurrently; a single-threaded server can deadlock
    # them and leave an element indefinitely in networkState=LOADING.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.result = None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]

    udd = tempfile.mkdtemp(prefix="fp-codecprobe-")
    proc = None
    try:
        cmd = [
            chrome_exe,
            f"--user-data-dir={udd}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-field-trial-config",
            "--autoplay-policy=no-user-gesture-required",
            # Use headful mode because headless decoding can differ, but position the
            # window off-screen so the launch-time probe does not flash on screen.
            "--window-position=-32000,-32000", "--window-size=320,240",
            # Required for decoding in the off-screen window. Without these flags,
            # Chrome suspends the hidden renderer and `<video>` remains at
            # readyState=0, which is indistinguishable from a missing codec. Verified
            # on 2026-07-27 with AAC, H.264, and VP9.
            "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            f"http://127.0.0.1:{port}/index.html",
        ]
        if not quiet:
            print("[codecs] verifying real decoding support (once per machine)...")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)

        deadline = time.time() + PROBE_TIMEOUT_S
        while time.time() < deadline:
            if httpd.result is not None:
                return httpd.result
            if proc.poll() is not None:
                # An early browser exit is not a codec verdict; distinguish it so an
                # unrelated crash does not make preflight reject the machine.
                raise RuntimeError(
                    f"the browser exited (code {proc.returncode}) before the probe "
                    "responded; codec support is unknown")
            time.sleep(0.2)
        raise RuntimeError(f"the probe did not respond within {PROBE_TIMEOUT_S} s")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(udd, ignore_errors=True)


def check(chrome_exe, force=False, strict=True, quiet=False):
    """Launcher entry point; raise coherence failures when `strict` is true.

    `FP_ALLOW_HOST=1` downgrades failures to warnings for diagnostics, matching
    the convention used by `generate_profiles.preflight()`.
    """
    cached = None if force else load_cached(chrome_exe)
    if cached:
        result = cached["result"]
    else:
        try:
            result = measure(chrome_exe, quiet=quiet)
        except RuntimeError as exc:
            # A measurement failure is not a machine failure. Do not cache it: one
            # stuck session must not permanently reject the machine.
            print(f"  ! codec preflight could not run: {exc}")
            return None
        _save(chrome_exe, result)

    # Evaluate WebRTC sender capabilities first and independently of playback.
    # Stock Chrome always offers H.264 sending through software OpenH264. Browser3
    # does not distribute it, so a machine without a hardware encoder would expose
    # an identity mismatch rather than merely a missing feature.
    if result.get("rtc_h264_send") == "0":
        msg = (
            "Codec preflight FAILED: this computer has no hardware H.264 encoder.\n"
            "  WebRTC would not advertise H.264 sending while stock Chrome does.\n"
            "  This conflicts with the claimed browser identity, not merely a missing feature.\n"
            "  Browser3 intentionally does not distribute the software OpenH264 encoder."
        )
        if strict and os.environ.get("FP_ALLOW_HOST") != "1":
            raise SystemExit(msg + "\n  (set FP_ALLOW_HOST=1 to override for diagnostics only)")
        print(msg + "\n  CONTINUING with FP_ALLOW_HOST=1; the WebRTC fingerprint will not match Chrome.")
        return result

    missing = [label for key, label in REQUIRED.items() if result.get(key) != "1"]
    if not missing:
        if not quiet and not cached:
            print("  [codecs] AAC and H.264 decode successfully "
                  "and WebRTC advertises H.264.")
        return result

    # VP9/WebM is a control whose codecs remain in the binary. If it also fails,
    # playback as a whole is broken and the result says nothing about the codecs
    # removed for licensing reasons; rejecting the machine would be a false diagnosis.
    if result.get("control") != "1":
        _save(chrome_exe, result, inconclusive=True)
        if not cached:
            print("  ! codec preflight is inconclusive: the VP9/WebM control sample also failed\n"
                  f"    ({result.get('control_why') or 'no details'}).\n"
                  "    Media playback as a whole is broken; this is not specific to excluded codecs.\n"
                  "    AVC/AAC support was not verified by this run.")
        return result

    detail = "; ".join(f"{lbl}: {result.get(k + '_why') or 'not decoded'}"
                       for k, lbl in REQUIRED.items() if result.get(k) != "1")
    msg = (
        "Codec preflight FAILED: this computer cannot decode: "
        + ", ".join(missing) + f"\n  ({detail})\n"
        "  Browser3 intentionally does not distribute AVC/AAC decoder implementations;\n"
        "  decoding must be provided by Windows. The browser would advertise support but fail\n"
        "  to play media, creating a detectable mismatch.\n"
        "  Fix: install the Media Feature Pack on Windows N/KN;\n"
        "       otherwise check the GPU driver and D3D11 video decoding."
    )
    if strict and os.environ.get("FP_ALLOW_HOST") != "1":
        raise SystemExit(msg + "\n  (set FP_ALLOW_HOST=1 to override for diagnostics only)")
    print(msg + "\n  CONTINUING with FP_ALLOW_HOST=1; playback will not match advertised support.")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", default=None, help="Release | Release2 | Dev")
    ap.add_argument("--force", action="store_true", help="ignore the cache and measure again")
    ap.add_argument("--exe", default=None, help="path to chrome.exe (overrides --build)")
    args = ap.parse_args()

    exe = args.exe
    if not exe:
        sys.path.insert(0, ROOT)
        import launcher
        exe = launcher.chrome_exe_path(args.build or launcher.DEFAULT_BUILD)

    result = check(exe, force=args.force, strict=False)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result is None:
        return 2
    return 0 if all(result.get(k) == "1" for k in REQUIRED) else 1


if __name__ == "__main__":
    sys.exit(main())
