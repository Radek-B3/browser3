#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0

r"""
Launch Browser3 profiles without injecting masking logic.

The launcher only orchestrates native Browser3 behavior. It loads a generated
profile, assigns an optional sticky proxy, derives proxy-consistent locale and
time-zone settings, and starts one `chrome.exe` with an isolated user-data
directory. All fingerprint masking remains in native Chromium code.

`proxy.txt` and `profiles.json` are read-only inputs.
Mutable state is stored below `%LOCALAPPDATA%\Browser3`.

Examples:
  python launcher.py
  python launcher.py --profile 1
  python launcher.py --profile 1 --no-proxy
  python launcher.py --all
  python launcher.py --profile 1 --dry-run
  python launcher.py --profile 1 --build Dev

`Release` is the production and validation build. `Release2` is available for A/B
comparisons, and `Dev` is an iteration-only component build. `FP_CHROME_EXE`
overrides `--build`.

Without a profile number, the launcher creates a new deterministic identity from a
random seed and a read-only reference profile, then assigns the next free index.
"""
import argparse
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.request

import browser3_paths as paths
from proxy_forwarder import ForwarderConfig, ProxyForwarder
from socks5_forwarder import Socks5Config, Socks5Forwarder, socks5_open_connect
from generate_profiles import FONT_BUNDLES  # source of truth for locale-aware fonts
import generate_profiles as gp  # fresh profile generation without a profile number
from windows_desktop import IsolatedDesktop

ROOT = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = paths.PROFILES_DIR
PROXY_FILE = os.path.join(ROOT, "proxy.txt")          # READ-ONLY
MAPPING_FILE = paths.PROXY_MAP_FILE  # persistent sticky assignment
PACKAGED_RUNTIME_DIR = os.path.join(ROOT, "runtime")
OUT_DIR = os.path.join(ROOT, "chromium_fork", "chromium", "src", "out")
# Build directories (out/<name>/chrome.exe), selectable with --build:
#   Release  = default production build and the only final-validation target.
#   Release2 = second production output for A/B build comparisons.
#   Dev      = fast component build for development only.
BUILD_DIRS = ("Release", "Release2", "Dev")
DEFAULT_BUILD = "Release"
CONTROL_MODES = ("none", "cdp")
DESKTOP_MODES = ("current", "isolated")
DEVTOOLS_ACTIVE_PORT = "DevToolsActivePort"


class ProfileInUseError(RuntimeError):
    """Another launcher or agent process already owns the persistent profile."""


class BrowserStartError(RuntimeError):
    """Chrome failed to start or become ready before the timeout."""


class ProfileLock:
    """Small inter-process OS lock bound to a persistent user-data directory."""

    def __init__(self, user_data_dir):
        self.path = os.path.join(user_data_dir, ".browser3-profile.lock")
        self._file = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        lock_file = open(self.path, "a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            lock_file.close()
            raise ProfileInUseError("Another browser session is already using this persistent profile.")
        self._file = lock_file
        return self

    def release(self):
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class BrowserLaunch:
    """Own a Chrome process, forwarder, lock, and optional CDP metadata."""

    def __init__(self, proc, forwarder, profile_lock, cdp_url=None,
                 devtools_active_port=None, desktop=None):
        self.proc = proc
        self.forwarder = forwarder
        self.profile_lock = profile_lock
        self.cdp_url = cdp_url
        self.devtools_active_port = devtools_active_port
        self.desktop = desktop
        self._cleaned = False

    def __iter__(self):
        # Backward compatibility for `proc, forwarder = launch_one(...)` callers.
        yield self.proc
        yield self.forwarder

    def cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        if self.forwarder:
            self.forwarder.stop()
        # Remove metadata while locked so a new launch cannot lose its file.
        if self.devtools_active_port:
            try:
                os.remove(self.devtools_active_port)
            except OSError:
                pass
        if hasattr(self.proc, "close") and self.proc.poll() is not None:
            self.proc.close()
        if self.desktop:
            self.desktop.close()
            self.desktop = None
        self.profile_lock.release()

    def wait(self):
        try:
            return self.proc.wait()
        finally:
            self.cleanup()

    def terminate(self, timeout=10.0):
        """Terminate only the owned Chrome process and release its resources."""
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        finally:
            self.cleanup()


def chrome_exe_path(build=DEFAULT_BUILD):
    """Return the packaged runtime or the selected development build.

    Public releases keep the complete Chromium runtime below ``runtime/`` and do not
    expose the private source-tree layout. Development checkouts continue to use
    ``chromium_fork/.../out/<build>``. ``FP_CHROME_EXE`` has highest priority.
    """
    override = os.environ.get("FP_CHROME_EXE")
    if override:
        return override
    packaged = os.path.join(PACKAGED_RUNTIME_DIR, "chrome.exe")
    if build == DEFAULT_BUILD and os.path.isfile(packaged):
        return packaged
    return os.path.join(OUT_DIR, build, "chrome.exe")


CHROME_EXE = chrome_exe_path()  # Release default retained for import compatibility


def parse_build_arg(argv):
    """Extract --build from raw argv for legacy callers and diagnostic tools.

    Accept both supported argument forms and validate early against BUILD_DIRS so
    a typo does not surface later as a missing executable.
    """
    allowed = ", ".join(BUILD_DIRS)
    for i, a in enumerate(argv):
        if a.startswith("--build="):
            val = a.split("=", 1)[1]
        elif a == "--build":
            if i + 1 >= len(argv):
                sys.exit(f"Error: --build requires a value ({allowed}).")
            val = argv[i + 1]
        else:
            continue
        if val not in BUILD_DIRS:
            sys.exit(f"Error: invalid build ''{val}'. Allowed: {allowed}")
        return val
    return DEFAULT_BUILD


# ---------- proxy.txt parser (three supported formats) ----------
def parse_proxy_line(line):
    """Return {host, port, user, pass} or None. Supported formats:
    ip:port | ip:port:user:pass | scheme://user:pass@host:port
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:  # scheme://user:pass@host:port  (scheme: http | socks5)
        scheme, rest = line.split("://", 1)
        cred = None
        if "@" in rest:
            cred, rest = rest.rsplit("@", 1)
        host, port = rest.rsplit(":", 1)
        user = pw = None
        if cred and ":" in cred:
            user, pw = cred.split(":", 1)
        return {"scheme": scheme.lower(), "host": host, "port": int(port), "user": user, "pass": pw}
    parts = line.split(":")
    if len(parts) == 2:
        return {"scheme": "http", "host": parts[0], "port": int(parts[1]), "user": None, "pass": None}
    if len(parts) == 4:
        return {"scheme": "http", "host": parts[0], "port": int(parts[1]), "user": parts[2], "pass": parts[3]}
    raise ValueError(f"Unknown proxy format: {line!r}")


def load_proxies():
    if not os.path.exists(PROXY_FILE):
        return []
    with open(PROXY_FILE, "r", encoding="utf-8") as f:
        out = []
        for ln in f:
            p = parse_proxy_line(ln)
            if p:
                out.append(p)
        return out


# ---------- sticky profile-to-proxy assignment across runs ----------
def load_mapping():
    return paths.load_json(MAPPING_FILE, {})


def save_mapping(m):
    paths.write_json_atomic(MAPPING_FILE, m)


def sticky_proxy_index(profile_id, n_proxies):
    """Return the profile's persistent proxy index.

    Randomness is used only for the initial assignment. The mapping then remains
    stable because one visitor ID appearing in different countries is detectable.
    If the proxy list shrinks, remap and persist an in-range index.
    """
    if n_proxies == 0:
        return None
    def assign(current):
        m = current if isinstance(current, dict) else {}
        pi = m.get(profile_id)
        if pi is None or not (0 <= pi < n_proxies):
            m[profile_id] = random.randrange(n_proxies)
        return m

    m = paths.update_json(MAPPING_FILE, assign, default={})
    return m[profile_id]


# ---------- proxy GeoIP -> coherent time zone and locale ----------
GEO_CACHE_FILE = paths.GEO_CACHE_FILE

# ISO country code -> Accept-Language / navigator.languages in stock Chrome format.
COUNTRY_LANGS = {
    "CZ": "cs-CZ,cs,en", "SK": "sk-SK,sk,cs,en-US,en", "DE": "de-DE,de,en-US,en",
    "AT": "de-AT,de,en-US,en", "CH": "de-CH,de,fr,en-US,en", "US": "en-US,en",
    "GB": "en-GB,en-US,en", "IE": "en-IE,en-US,en", "CA": "en-CA,en,fr-CA,fr",
    "AU": "en-AU,en-US,en", "FR": "fr-FR,fr,en-US,en", "PL": "pl-PL,pl,en-US,en",
    "NL": "nl-NL,nl,en-US,en", "BE": "nl-BE,nl,fr,en-US,en", "ES": "es-ES,es,en-US,en",
    "IT": "it-IT,it,en-US,en", "PT": "pt-PT,pt,en-US,en", "RU": "ru-RU,ru,en-US,en",
    "UA": "uk-UA,uk,ru,en-US,en", "RO": "ro-RO,ro,en-US,en", "HU": "hu-HU,hu,en-US,en",
    "SE": "sv-SE,sv,en-US,en", "NO": "nb-NO,no,en-US,en", "DK": "da-DK,da,en-US,en",
    "FI": "fi-FI,fi,sv,en-US,en", "GR": "el-GR,el,en-US,en", "TR": "tr-TR,tr,en-US,en",
    "JP": "ja-JP,ja,en-US,en", "KR": "ko-KR,ko,en-US,en", "CN": "zh-CN,zh,en-US,en",
    "TW": "zh-TW,zh,en-US,en", "HK": "zh-HK,zh,en-US,en", "BR": "pt-BR,pt,en-US,en",
    "MX": "es-MX,es,en-US,en", "AR": "es-AR,es,en-US,en", "IN": "en-IN,en,hi",
    "ID": "id-ID,id,en-US,en", "TH": "th-TH,th,en-US,en", "VN": "vi-VN,vi,en-US,en",
    "IL": "he-IL,he,en-US,en", "SA": "ar-SA,ar,en-US,en", "AE": "ar-AE,ar,en-US,en",
    "ZA": "en-ZA,en,af",
}
DEFAULT_LANGS = "en-US,en"

# ISO country -> FONT_BUNDLES expected for a local user and therefore never hidden.
COUNTRY_KEEP_BUNDLES = {
    "JP": ["jp"], "KR": ["kr"], "CN": ["sc"],
    "TW": ["tc"], "HK": ["tc"], "MO": ["tc"],
    "TH": ["sea"], "IN": ["indic"], "LK": ["indic"], "BD": ["indic"], "NP": ["indic"],
}


def _apply_locale_fonts(profile, country):
    """Unhide font families expected in the proxy country's locale.

    Other diversity bundles stay hidden. Unmapped locales are unchanged.
    """
    keep_keys = COUNTRY_KEEP_BUNDLES.get((country or "").upper())
    if not keep_keys:
        return
    fonts = profile.get("fonts") or {}
    hidden = fonts.get("hidden") or []
    if not hidden:
        return
    keep = set()
    for key, fams in FONT_BUNDLES:
        if key in keep_keys:
            keep.update(fams)
    new_hidden = [f for f in hidden if f not in keep]
    if len(new_hidden) != len(hidden):
        fonts["hidden"] = new_hidden
        profile["fonts"] = fonts
        print(f"  [fonts] locale {country}: preserved bundles {keep_keys} "
              f"(removed {len(hidden) - len(new_hidden)} families from hidden)")


def warn_locale_without_proxy(profile, geo):
    """Warn when the no-proxy cs-CZ fallback conflicts with the host locale.

    Browser3 does not change the OS locale, so ICU/Intl formats and SAPI voices
    would otherwise conflict with the claimed profile locale.
    """
    if geo:
        return   # With a proxy, GeoIP determines the locale.
    host = gp.host_info()
    hl = (host or {}).get("locale") or {}
    host_ui, host_tz = hl.get("ui"), hl.get("timezone")
    claim_lang = (profile.get("locale") or {}).get("languages") or []
    claim_lang = claim_lang[0] if claim_lang else None
    claim_tz = profile.get("timezone")
    bad = []
    if host_ui and claim_lang and host_ui.lower() != claim_lang.lower():
        bad.append(f"language {claim_lang} vs. host {host_ui}")
    if host_tz and claim_tz and host_tz != claim_tz:
        bad.append(f"timezone {claim_tz} vs. host {host_tz}")
    if bad:
        print(f"  ! WARNING (no proxy): the profile claims {', '.join(bad)}. "
              f"Intl formats and voices remain host-bound, creating a mismatch.\n"
              f"    Use a proxy or align the operating-system locale.")


def _load_geo_cache():
    cached = paths.load_json(GEO_CACHE_FILE, {})
    return cached if isinstance(cached, dict) else {}


def geo_from_proxy(px):
    """Resolve proxy-exit GeoIP through ip-api.com via the proxy itself.

    Results are cached per sticky proxy. Return country, time zone, languages,
    and exit IP, or None on failure. The free ip-api.com tier uses HTTP.
    """
    key = f"{px['host']}:{px['port']}"
    cache = _load_geo_cache()
    if key in cache:
        return cache[key]
    fwd = None
    try:
        fields = "status,message,countryCode,timezone,query"
        if px.get("scheme", "http") == "socks5":
            # Query through SOCKS5 with remote DNS, then issue an HTTP GET.
            sock = socks5_open_connect(
                Socks5Config(px["host"], px["port"], px["user"], px["pass"]), "ip-api.com", 80)
            sock.sendall(f"GET /json/?fields={fields} HTTP/1.1\r\n"
                         "Host: ip-api.com\r\nConnection: close\r\n\r\n".encode())
            buf = b""
            while True:
                c = sock.recv(4096)
                if not c:
                    break
                buf += c
            sock.close()
            raw = buf.split(b"\r\n\r\n", 1)[-1].decode(errors="replace")
        else:
            if px.get("user"):
                fwd = ProxyForwarder(ForwarderConfig(px["host"], px["port"], px["user"], px["pass"]))
                fwd.start()
                proxy_url = f"http://127.0.0.1:{fwd.port}"
            else:
                proxy_url = f"http://{px['host']}:{px['port']}"
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
            raw = opener.open(f"http://ip-api.com/json/?fields={fields}", timeout=15).read().decode()
        d = json.loads(raw)
        if d.get("status") != "success":
            print(f"  [geo] ip-api request failed: {d.get('message', '?')}")
            return None
        cc = d.get("countryCode", "")
        geo = {"country": cc, "timezone": d.get("timezone"),
               "accept_languages": COUNTRY_LANGS.get(cc, DEFAULT_LANGS),
               "exit_ip": d.get("query")}
        cache[key] = geo
        def merge(current):
            merged = current if isinstance(current, dict) else {}
            merged[key] = geo
            return merged
        paths.update_json(GEO_CACHE_FILE, merge, default={})
        print(f"  [geo] {key} -> {cc} {geo['timezone']} langs={geo['accept_languages']} "
              f"(exit {geo['exit_ip']})")
        return geo
    except Exception as e:
        print(f"  [geo] lookup failed: {e}")
        return None
    finally:
        if fwd:
            fwd.stop()


def next_profile_index():
    """Return the next free profile_NN.json index after generated profiles."""
    mx = 0
    for f in os.listdir(paths.PROFILES_DIR):
        m = re.match(r"profile_(\d+)\.json$", f)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def ensure_host_probe(gpu_mode, build, blocking=True):
    """Ensure that a schema-v2 cache of measured host properties exists.

    Profile generation requires the measured GPU, screen, core count, memory, and
    font inventory. Run synchronously once per machine; there is no development-host
    fallback. `blocking` remains only for caller compatibility and is ignored.
    """
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import probe_host as P
    except Exception as e:
        print(f"  ! host probe unavailable ({e}) — profiles cannot be generated safely.")
        return None
    cached = P.load_cache()
    if cached:
        return cached
    print("[host] measuring this computer (once, about 15 seconds)...")
    try:
        return P.probe(build=build, quiet=False)
    except Exception as e:
        print(f"  ! host probe failed: {e}")
        return None


def ensure_codec_support(exe):
    """Verify actual AVC/AAC decoding once per machine.

    If the optional preflight module is unavailable, warn clearly but allow launch.
    """
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import preflight_codecs
    except Exception as exc:
        print(f"  ! codec preflight unavailable ({exc}) — decoding was NOT VERIFIED.")
        return None
    return preflight_codecs.check(exe, quiet=True)


def warn_if_gpu_implausible(prof, host):
    """Warn without blocking when a claimed GPU is implausible for the host."""
    blk = prof.get("gpu")
    if not blk or not host:
        return
    hv = (host.get("webgpu") or {}).get("vendor")
    hw_ren = (host.get("webgl") or {}).get("renderer") or ""
    cv = (blk.get("webgpu") or {}).get("vendor")
    if hv and cv and hv != cv:
        weak = any(k in hw_ren for k in ("Intel(R) HD", "Intel(R) UHD", "Iris"))
        if weak:
            print(f"  ! WARNING: the host uses a weak integrated GPU ({_gp_short(hw_ren)}), but the profile claims "
                  f"{blk.get('profile')} — capability and performance claims may not match.")


def _gp_short(renderer):
    return gp._short_renderer(renderer)


def apply_gpu_mode(idx, gpu_mode, host):
    """Apply and persist a GPU mode to an existing profile.

    Return True on change and warn because changing the GPU breaks identity continuity.
    """
    path = os.path.join(paths.PROFILES_DIR, f"profile_{idx:02d}.json")
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        prof = json.load(f)
    seed = prof.get("profile_id") or ""
    new_name, new_block = gp.gpu_block_for(seed, gpu_mode)
    old_block = prof.get("gpu")
    old_name = (old_block or {}).get("profile")
    if (old_name or None) == (new_name or None):
        return False
    if new_block:
        prof["gpu"] = new_block
        # Keep top-level webgl host-bound as the fallback if C++ rejects the GPU block.
        prof["webgl"] = {"vendor": gp.host_webgl()["vendor"],
                         "renderer": gp.host_webgl()["renderer"]}
    else:
        prof.pop("gpu", None)
        prof["webgl"] = {"vendor": gp.host_webgl()["vendor"],
                         "renderer": gp.pick_webgl_renderer(seed)}
    paths.write_json_atomic(path, prof)
    print(f"[gpu] profile {idx:02d}: {old_name or 'native host'} -> {new_name or 'native host'} "
          f"(--gpu {gpu_mode}, written to profile)")
    print("  ! WARNING: changing the GPU creates a NEW PROFILE IDENTITY; "
          "visitor IDs will not continue from earlier runs.")
    warn_if_gpu_implausible(prof, host)
    return True


def create_new_profile(gpu_mode=None):
    """Create, persist, and return the index of a fresh deterministic profile.

    Select a read-only reference fingerprint and a random seed. All supported axes
    derive deterministically from that seed, preserving reload stability. The
    reference profiles.json remains read-only.
    """
    gp._assert_no_core_in_bundles()  # Same module invariant as generate_profiles.main.
    with open(os.path.join(ROOT, "profiles.json"), "r", encoding="utf-8") as f:
        refs = json.load(f)
    ref = random.choice(refs)
    seed = gp.random_seed()
    idx = next_profile_index()
    prof, fp_visible = gp.build_profile(ref, idx, seed=seed,
                                        gpu_mode=gpu_mode or gp.default_gpu_mode())
    path = os.path.join(paths.PROFILES_DIR, f"profile_{idx:02d}.json")
    paths.write_json_atomic(path, prof)
    print(f"[new profile] random seed -> {os.path.basename(path)}")
    print(gp.profile_summary(path, prof, fp_visible))
    return idx


def load_profile(idx):
    path = os.path.join(paths.PROFILES_DIR, f"profile_{idx:02d}.json")
    if not os.path.exists(path):
        gp.ensure_profiles()
    if not os.path.exists(path):
        sys.exit(f"Profile does not exist: {path} (run generate_profiles.py)")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f), path


def stealth_flags():
    """Return a conservative set of determinism and automation-hygiene flags."""
    return [
        "--disable-field-trial-config",          # fingerprint determinism across reloads
        # TLSTrustAnchorIDs preserves the stock Chrome JA4 value t13d1516h2.
        # ReduceAcceptLanguage keeps the full stock Accept-Language header. The
        # intl.accept_languages preference supplies both that header and
        # navigator.languages. This does not alter the demo's server-side browser-name
        # downgrade, which genuine Chrome also exhibits (measured 2026-07-11).
        "--disable-features=OptimizationHints,TLSTrustAnchorIDs,ReduceAcceptLanguage",
        "--no-default-browser-check",
        "--no-first-run",
        # Do not add AutomationControlled or webdriver-related flags; native C++ owns it.
    ]


def angle_flags(profile):
    """Select an optional per-profile ANGLE backend.

    A real backend change affects canvas and WebGL natively. It is safe only when
    the renderer remains native; otherwise the claimed renderer and actual output
    conflict. A missing or unknown value falls back to Chrome's default.
    """
    backend = profile.get("angle_backend")
    return [f'--use-angle={backend}'] if backend else []


def webrtc_flags(proxy_arg):
    """Declare WebRTC public-IP protection whenever a proxy is active.

    The API remains functional, but public UDP candidates must not bypass a TCP-only
    proxy and reveal a different WAN IP. Chrome 149 ignores this command-line switch
    and reads `webrtc.ip_handling_policy`; `ensure_webrtc_pref()` is therefore the
    effective mechanism. The harmless flag documents intent and guards future rebases.
    """
    return ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"] if proxy_arg else []


def ensure_webrtc_pref(user_data_dir, active):
    """Set WebRTC proxy protection in the profile Preferences file.

    Merge the policy when a proxy is active. Remove it otherwise so the setting
    cannot persist into a direct connection and change normal no-proxy behavior.
    """
    default_dir = os.path.join(user_data_dir, "Default")
    pref_file = os.path.join(default_dir, "Preferences")
    if active:
        os.makedirs(default_dir, exist_ok=True)
        prefs = {}
        if os.path.exists(pref_file):
            try:
                with open(pref_file, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
            except Exception:
                prefs = {}  # Chrome will replace corrupt preferences; ensure our key.
        if not isinstance(prefs.get("webrtc"), dict):
            prefs["webrtc"] = {}
        prefs["webrtc"]["ip_handling_policy"] = "disable_non_proxied_udp"
        with open(pref_file, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    else:
        if not os.path.exists(pref_file):
            return
        try:
            with open(pref_file, "r", encoding="utf-8") as f:
                prefs = json.load(f)
        except Exception:
            return
        if isinstance(prefs.get("webrtc"), dict) and "ip_handling_policy" in prefs["webrtc"]:
            del prefs["webrtc"]["ip_handling_policy"]
            with open(pref_file, "w", encoding="utf-8") as f:
                json.dump(prefs, f)


def ensure_lang_pref(user_data_dir, accept_languages):
    """Keep navigator.languages and Accept-Language coherent with proxy GeoIP.

    Merge `intl.accept_languages` and `selected_languages` when provided; remove
    them otherwise to preserve the host's no-proxy default.
    """
    default_dir = os.path.join(user_data_dir, "Default")
    pref_file = os.path.join(default_dir, "Preferences")
    if accept_languages:
        os.makedirs(default_dir, exist_ok=True)
        prefs = {}
        if os.path.exists(pref_file):
            try:
                with open(pref_file, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
            except Exception:
                prefs = {}
        if not isinstance(prefs.get("intl"), dict):
            prefs["intl"] = {}
        prefs["intl"]["accept_languages"] = accept_languages
        prefs["intl"]["selected_languages"] = accept_languages
        with open(pref_file, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    else:
        if not os.path.exists(pref_file):
            return
        try:
            with open(pref_file, "r", encoding="utf-8") as f:
                prefs = json.load(f)
        except Exception:
            return
        intl = prefs.get("intl")
        if isinstance(intl, dict) and ("accept_languages" in intl or "selected_languages" in intl):
            intl.pop("accept_languages", None)
            intl.pop("selected_languages", None)
            with open(pref_file, "w", encoding="utf-8") as f:
                json.dump(prefs, f)


def window_flags(profile):
    """Return a profile-coherent outer window size, if one is configured."""
    s = profile.get("screen", {})
    w, h = s.get("window_width"), s.get("window_height")
    flags = [f'--window-size={w},{h}'] if w and h else []
    # Normalize a HiDPI host to DPR 1 so window and claimed screen use equal units.
    return gp.hidpi_scale_flags() + flags


def pick_loopback_port():
    """Select an available loopback port without the special automation port 0."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def control_flags(control, cdp_port=None):
    """Return opt-in CDP flags; none adds no automation or debugging switch."""
    if control == "none":
        return []
    if control == "cdp":
        port = cdp_port if cdp_port is not None else pick_loopback_port()
        if not 1 <= int(port) <= 65535:
            raise ValueError("The CDP port must be a non-zero TCP port.")
        return ["--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=%d" % int(port)]
    raise ValueError("Unknown control mode: %s" % control)


def wait_for_cdp_endpoint(user_data_dir, proc, timeout=30.0, expected_port=None):
    """Read the actual browser WebSocket endpoint once Chrome exposes it."""
    active_port = os.path.join(user_data_dir, DEVTOOLS_ACTIVE_PORT)
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise BrowserStartError(
                "Chrome exited before CDP became ready (exit %s)." % proc.returncode)
        try:
            if expected_port is not None:
                metadata_url = "http://127.0.0.1:%d/json/version" % expected_port
                with urllib.request.urlopen(metadata_url, timeout=0.5) as response:
                    metadata = json.loads(response.read().decode("utf-8"))
                endpoint = metadata.get("webSocketDebuggerUrl")
                prefix = "ws://127.0.0.1:%d/devtools/browser/" % expected_port
                if endpoint and endpoint.startswith(prefix):
                    return endpoint, None
            else:
                with open(active_port, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f.readlines()]
                if (len(lines) >= 2 and lines[0].isdigit() and
                        lines[1].startswith("/devtools/browser/")):
                    port = int(lines[0])
                    if 1 <= port <= 65535:
                        return "ws://127.0.0.1:%d%s" % (port, lines[1]), active_port
        except (IOError, OSError, ValueError) as exc:
            last_error = exc
        time.sleep(0.05)
    detail = " (%s)" % last_error if last_error else ""
    raise BrowserStartError("The CDP endpoint was not ready within %.1f seconds%s." % (timeout, detail))


def write_control_info(path, info):
    """Write an optional machine-readable handoff for the launching process."""
    if not path:
        return
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp = target + ".tmp-%d" % os.getpid()
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    os.replace(temp, target)


def build_cmdline(profile, config_path, proxy_arg, chrome_exe=None, control="none",
                  cdp_port=None):
    cmd = [chrome_exe or CHROME_EXE,
           f'--user-data-dir={profile["user_data_dir"]}',
           f'--fp-profile-config={config_path}']
    if proxy_arg:
        cmd.append(f'--proxy-server={proxy_arg}')
    cmd += window_flags(profile)
    cmd += angle_flags(profile)
    cmd += webrtc_flags(proxy_arg)   # Prevent public-IP leakage only behind a proxy.
    cmd += stealth_flags()
    cmd += control_flags(control, cdp_port=cdp_port)

    # Load optional environment flags, for example extension-test switches.
    extra_flags_env = os.environ.get("FP_LAUNCHER_EXTRA_FLAGS")
    if extra_flags_env:
        import shlex
        cmd.extend(shlex.split(extra_flags_env, posix=(os.name != "nt")))

    return cmd


def _launch_one_impl(idx, with_proxy, dry_run, chrome_exe=None, control="none",
                     cdp_timeout=30.0, control_output=None, profile_lock=None,
                     desktop="current"):
    """chrome_exe=None → packaged runtime or out/Release, see chrome_exe_path()."""
    exe = chrome_exe or CHROME_EXE
    profile, path = load_profile(idx)
    os.makedirs(profile["user_data_dir"], exist_ok=True)

    forwarder = None
    proxy_arg = None
    geo = None
    if with_proxy:
        proxies = load_proxies()
        pi = sticky_proxy_index(profile["profile_id"], len(proxies))
        if pi is None:
            print(f"[profile {idx}] proxy.txt is empty; continuing without a proxy")
        else:
            px = proxies[pi]
            # Add GeoIP data to keep time zone, locale, and WebRTC coherent with the proxy.
            geo = geo_from_proxy(px)
            if geo:
                profile["proxy"] = {"index": pi, "geo": geo}
                profile["timezone"] = geo.get("timezone") or profile.get("timezone")
                _apply_locale_fonts(profile, geo.get("country"))  # locale-aware fonts
                # Persist the update so native time-zone/font masking sees coherent values.
                paths.write_json_atomic(path, profile)
            if px.get("scheme", "http") == "socks5":
                # Chrome cannot authenticate SOCKS5 from CLI, so use a no-auth local forwarder.
                # UDP ASSOCIATE also permits proxied WebRTC UDP when the upstream supports it.
                fwd = Socks5Forwarder(Socks5Config(px["host"], px["port"], px["user"], px["pass"]))
                fwd.start()
                forwarder = fwd
                proxy_arg = f'socks5://127.0.0.1:{fwd.port}'
                print(f"[profile {idx}] SOCKS5 forwarder 127.0.0.1:{fwd.port} -> {px['host']}:{px['port']} (auth)")
            elif px["user"]:
                # Authenticated HTTP upstream through a local CONNECT forwarder; TLS stays intact.
                fwd = ProxyForwarder(ForwarderConfig(px["host"], px["port"], px["user"], px["pass"]))
                fwd.start()
                forwarder = fwd
                proxy_arg = f'http://127.0.0.1:{fwd.port}'
                print(f"[profile {idx}] forwarder 127.0.0.1:{fwd.port} -> {px['host']}:{px['port']} (auth)")
            else:
                proxy_arg = f'http://{px["host"]}:{px["port"]}'

    # Chrome reads the WebRTC anti-leak preference and ignores the CLI switch.
    ensure_webrtc_pref(profile["user_data_dir"], bool(proxy_arg))
    # Keep navigator.languages and Accept-Language coherent with proxy GeoIP. Without
    # GeoIP, use the cs-CZ host fallback shared with the CZ-proxy path. Stock cs-CZ
    # Chrome reports [cs-CZ, cs, en], not [cs-CZ, cs, en-US, en].
    ensure_lang_pref(profile["user_data_dir"], geo["accept_languages"] if geo else COUNTRY_LANGS["CZ"])
    warn_locale_without_proxy(profile, geo)   # host locale vs. no-proxy fallback (3.8)

    cdp_port = pick_loopback_port() if control == "cdp" else None
    cmd = build_cmdline(profile, path, proxy_arg, exe, control=control,
                        cdp_port=cdp_port)
    print(f"[profile {idx}] cmd:\n  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

    if dry_run:
        if forwarder:
            forwarder.stop()
        return None
    if not os.path.exists(exe):
        print(f"  ! chrome.exe does not exist ({exe}); the build is incomplete or --build is incorrect.")
        if forwarder:
            forwarder.stop()
        return None

    # Browser3 does not distribute AVC/AAC implementations; Windows decodes them.
    # Verify actual support because build flags control the advertised capability.
    # Cache the measured result per machine.
    ensure_codec_support(exe)

    active_port = os.path.join(profile["user_data_dir"], DEVTOOLS_ACTIVE_PORT)
    if control == "cdp" and os.path.exists(active_port):
        try:
            os.remove(active_port)
        except OSError as exc:
            if forwarder:
                forwarder.stop()
            profile_lock.release()
            raise BrowserStartError("Cannot remove a stale DevToolsActivePort: %s" % exc)

    desktop_owner = None
    try:
        if desktop == "isolated":
            desktop_owner = IsolatedDesktop.create()
            proc = desktop_owner.launch(cmd, cwd=ROOT)
        elif desktop == "current":
            proc = subprocess.Popen(cmd, cwd=ROOT)
        else:
            raise ValueError("Unknown desktop mode: %s" % desktop)
    except Exception:
        if desktop_owner:
            desktop_owner.close()
        if forwarder:
            forwarder.stop()
        profile_lock.release()
        raise

    cdp_url = None
    try:
        if control == "cdp":
            cdp_url, active_port = wait_for_cdp_endpoint(
                profile["user_data_dir"], proc, timeout=cdp_timeout,
                expected_port=cdp_port)
            info = {"profile": idx, "control": control, "cdp_url": cdp_url,
                    "pid": proc.pid, "desktop": desktop,
                    "desktop_name": desktop_owner.full_name if desktop_owner else None}
            write_control_info(control_output, info)
            print("BROWSER3_CONTROL " + json.dumps(info, ensure_ascii=False), flush=True)
        return BrowserLaunch(proc, forwarder, profile_lock, cdp_url,
                             active_port if control == "cdp" else None,
                             desktop=desktop_owner)
    except Exception:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if forwarder:
            forwarder.stop()
        if hasattr(proc, "close"):
            proc.close()
        if desktop_owner:
            desktop_owner.close()
        profile_lock.release()
        raise


def launch_one(idx, with_proxy, dry_run, chrome_exe=None, control="none",
               cdp_timeout=30.0, control_output=None, desktop="current"):
    """Acquire the profile lock before runtime mutation or forwarder startup."""
    if dry_run:
        return _launch_one_impl(idx, with_proxy, True, chrome_exe, control,
                                cdp_timeout, control_output, profile_lock=None,
                                desktop=desktop)
    profile, _path = load_profile(idx)
    profile_lock = ProfileLock(profile["user_data_dir"]).acquire()
    try:
        result = _launch_one_impl(idx, with_proxy, False, chrome_exe, control,
                                  cdp_timeout, control_output, profile_lock=profile_lock,
                                  desktop=desktop)
        if result is None:
            profile_lock.release()
        return result
    except Exception:
        profile_lock.release()
        raise


def main():
    paths.initialize_runtime_state()
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", type=int, help="profile index (1..N)")
    ap.add_argument("--all", action="store_true", help="start every generated profile")
    ap.add_argument("--with-proxy", action="store_true",
                    help="force the sticky proxy assignment (default when proxy.txt is not empty)")
    ap.add_argument("--no-proxy", action="store_true", help="start without a proxy")
    ap.add_argument("--dry-run", action="store_true", help="print the command line only")
    ap.add_argument("--control", choices=CONTROL_MODES, default="none",
                    help="opt in to browser control (default: none, without CDP)")
    ap.add_argument("--desktop", choices=DESKTOP_MODES, default="current",
                    help="headful desktop (current or an isolated WinSta0 desktop)")
    ap.add_argument("--control-output",
                    help="optional JSON hand-off file containing the CDP endpoint")
    ap.add_argument("--cdp-timeout", type=float, default=30.0,
                    help=argparse.SUPPRESS)
    ap.add_argument("--build", choices=BUILD_DIRS, default=DEFAULT_BUILD,
                    help=f"select the out/ build directory (default: {DEFAULT_BUILD}; "
                         f"use Release for validation)")
    ap.add_argument("--gpu", choices=gp.GPU_MODES, default=None,
                    help="GPU identity mode: off (native host), family (same vendor and "
                         "architecture), common (common discrete adapters), or all. Existing "
                         f"profiles are unchanged when omitted; new profiles default to "
                         f"'{gp.DEFAULT_GPU_MODE}' ('family' on weak/integrated graphics).")
    args = ap.parse_args()

    # A non-empty proxy.txt enables proxies by default. Explicit --no-proxy prevents
    # accidentally changing a profile's GeoIP; --with-proxy remains for compatibility.
    use_proxy = not args.no_proxy and (args.with_proxy or bool(load_proxies()))
    if args.no_proxy and args.with_proxy:
        sys.exit("--with-proxy and --no-proxy are mutually exclusive")

    exe = chrome_exe_path(args.build)
    if os.environ.get("FP_CHROME_EXE"):
        print(f"[build] FP_CHROME_EXE override (--build {args.build} ignored) -> {exe}")
    else:
        print(f"[build] {args.build} -> {exe}")

    # Generation or GPU rewrites require measured GPU, screen, cores, memory, and fonts.
    # Launching an unchanged existing profile does not run the probe.
    generating_fresh = not (args.all or args.profile)
    host = None
    if args.gpu or generating_fresh:
        # Even --gpu off needs the measured host GPU for top-level webgl values.
        host = ensure_host_probe(args.gpu or "common", args.build)
        if not host:
            sys.exit("Generating or rewriting a profile requires a valid host probe, but the probe failed.\n"
                     "  Run manually: python scripts/probe_host.py --force --build " + args.build)
    # Derive a fresh profile's default GPU mode only after the host is known.
    effective_gpu = args.gpu or (gp.default_gpu_mode() if generating_fresh else None)

    if args.all or args.profile:
        gp.ensure_profiles(gpu_mode=args.gpu, build=args.build)

    n = len([f for f in os.listdir(paths.PROFILES_DIR) if f.startswith("profile_") and f.endswith(".json")])
    if args.all:
        indices = list(range(1, n + 1))
    elif args.profile:
        indices = [args.profile]
    else:
        # No profile number: generate and immediately launch a fresh seeded profile.
        indices = [create_new_profile(gpu_mode=effective_gpu)]

    # Apply an explicit GPU mode persistently to existing profiles.
    if args.gpu and (args.all or args.profile):
        for i in indices:
            if not apply_gpu_mode(i, args.gpu, host):
                print(f"[gpu] profile {i:02d}: GPU unchanged (--gpu {args.gpu})")

    running = []
    try:
        for i in indices:
            r = launch_one(i, use_proxy, args.dry_run, exe, control=args.control,
                           cdp_timeout=args.cdp_timeout,
                           control_output=args.control_output,
                           desktop=args.desktop)
            if r:
                running.append(r)

        for launch in running:
            launch.wait()
    except (ProfileInUseError, BrowserStartError) as exc:
        for launch in running:
            launch.terminate()
        sys.exit("Error: %s" % exc)
    except KeyboardInterrupt:
        for launch in running:
            launch.terminate()


if __name__ == "__main__":
    main()
