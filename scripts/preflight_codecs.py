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

# Kodeky, které po L2 dekóduje operační systém a jejichž selhání je rozpor s tím,
# co hlásíme. Klíč = jméno v odpovědi sondy.
REQUIRED = {
    "aac": "AAC (Media Foundation)",
    "h264": "H.264 (D3D11 / Media Foundation)",
}

# 40 s nestačilo: monolitický Release při PRVNÍM startu (studená cache, čerstvý build)
# odpovídal déle a preflight hlásil „sonda neodpověděla" na každém novém stroji.
# Sonda běží jednou za stroj, takže velkorysý strop nic nestojí.
PROBE_TIMEOUT_S = 120


class _Handler(BaseHTTPRequestHandler):
    """Odbaví sondu a vzorky; `/result` sebere verdikt."""

    MIME = {".html": "text/html; charset=utf-8", ".mp4": "video/mp4"}

    def do_GET(self):  # noqa: N802 (jméno vynucuje BaseHTTPRequestHandler)
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
        # Vzorky i stránka jsou naše, ale cesta přichází z prohlížeče — držet ji uvnitř.
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(PROBE_DIR) \
                or not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as fh:
            body = fh.read()

        # Range: mediální stack si o rozsahy říká a na neobsloužený požadavek umí
        # zůstat viset v networkState=LOADING místo aby ohlásil chybu.
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
        # Prohlížeč se na konci sondy zavírá a rozpojená spojení jinak vysypou do
        # logu ConnectionResetError traceback. Není to chyba a plete se s reálnými.
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True


def _machine_key(chrome_exe):
    """Na čem verdikt závisí. Změna kteréhokoli z toho = přeměřit.

    Ovladač GPU a build Windows proto, že na nich stojí D3D11/MF dekódování; identita
    binárky proto, že právě její obsah (jaké kodeky jsme z ní vyřadili) se ověřuje."""
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


# Jak dlouho platí NEPRŮKAZNÝ verdikt. Trvale se cachovat nesmí (stroj se opraví
# a my bychom to nikdy nezjistili), zahodit hned taky ne — na rozbitém stroji by se
# čtyřicetisekundová sonda pouštěla při každém startu.
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
    """Spustí sondu a vrátí slovník výsledků (`{'aac': '1', 'h264': '0', ...}`)."""
    if not os.path.isfile(chrome_exe):
        raise RuntimeError(f"chrome.exe was not found: {chrome_exe}")

    # Vláknový server: dva mediální elementy načítají souběžně a jednovláknový
    # HTTPServer je navzájem zablokuje (element pak jen visí v networkState=LOADING).
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
            # Headful (headless umí dekódování rozhodnout jinak), ale mimo obrazovku,
            # ať to při startu launcheru nebliká uživateli přes plochu.
            "--window-position=-32000,-32000", "--window-size=320,240",
            # NUTNÉ k tomu, aby okno mimo obrazovku vůbec dekódovalo. Bez těchhle dvou
            # přepínačů považuje Chrome renderer za skrytý, uspí ho a `<video>` uvázne
            # na readyState=0 — tedy přesně tak, jako by kodek chyběl. Stálo to celý den
            # chybné diagnózy „rozbitý stroj"; kontrolní vzorek to neodhalí, protože
            # uvázne stejně. Ověřeno 2026-07-27: s nimi projde AAC, H.264 i VP9.
            "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            f"http://127.0.0.1:{port}/index.html",
        ]
        if not quiet:
            print("[codecs] verifying real decoding support (once per machine)...")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        deadline = time.time() + PROBE_TIMEOUT_S
        while time.time() < deadline:
            if httpd.result is not None:
                return httpd.result
            if proc.poll() is not None:
                # Prohlížeč spadl dřív, než stránka odpověděla. To samo o sobě není
                # verdikt o kodeku — rozlišit se to musí, jinak by preflight odmítl
                # stroj kvůli úplně jiné závadě.
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
    """Hlavní vstup pro launcher. Vrací výsledek; při `strict` chybu vyhodí.

    `FP_ALLOW_HOST=1` degraduje chybu na varování — stejná úmluva jako u
    `generate_profiles.preflight()`, a stejně tak jen pro diagnostiku."""
    cached = None if force else load_cached(chrome_exe)
    if cached:
        result = cached["result"]
    else:
        try:
            result = measure(chrome_exe, quiet=quiet)
        except RuntimeError as exc:
            # Neúspěch MĚŘENÍ není neúspěch stroje. Necachovat, jen upozornit —
            # jinak by jedna zaseknutá relace stroj trvale odsoudila.
            print(f"  ! codec preflight could not run: {exc}")
            return None
        _save(chrome_exe, result)

    # WebRTC sender capabilities se posuzují PRVNÍ a samostatně: nezávisí na přehrávací
    # pipeline, takže jsou průkazné i na stroji, kde dekódovací sondy selžou, a naopak
    # nesmí projít jen proto, že dekódování je v pořádku. Reálný Chrome H.264
    # k odesílání nabízí vždy (nese softwarový OpenH264); my ho nedistribuujeme, takže
    # na stroji bez hardwarového encoderu by se z nabídky ztratil — a to je rozpor
    # s claimovanou identitou, ne jen chybějící funkce.
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

    # Kontrolní vzorek (VP9/WebM) používá kodeky, které v binárce máme a nikdy
    # neodstraníme. Když neprojde ani ten, je rozbité přehrávání jako celek a nemá
    # to s licenčně odstraněnými kodeky nic společného — takový stroj odmítnout
    # nesmíme, byla by to chybná diagnóza. Verdikt se navíc necachuje.
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
