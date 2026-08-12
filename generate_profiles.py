#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0

r"""
Generate deterministic Browser3 profile configurations.

This is orchestration and policy code, not masking code. It reads the immutable
`profiles.json` reference and writes per-user configurations below
`%LOCALAPPDATA%\Browser3\profiles`.

Only soft signals that can legitimately vary on one physical computer are
diversified, including resolution, hardwareConcurrency, deviceMemory, fonts, and
text edging. Hardware-bound signals remain pinned to the measured host. Every
per-profile choice is derived deterministically from `base_profile_id`, making an
identity stable across runs and reloads.

Invariants:
- `profiles.json` and `proxy.txt` are read-only inputs.
- The claimed Chrome version equals the actual build version.
- `base_profile_id` is the sole deterministic seed for profile variation.
"""
import hashlib
import json
import os
import re
import secrets
import sys

import browser3_paths as paths

ROOT = os.path.dirname(os.path.abspath(__file__))
CHROME_VERSION = "149.0.7827.201"       # Must equal the target build tag.
CHROME_MAJOR = CHROME_VERSION.split(".")[0]
# All profiles share the real major.minor.build; only the patch may vary. Native
# validation rejects a claim with a different prefix.
CHROME_BASE = CHROME_VERSION.rsplit(".", 1)[0]

# Per-profile UA-CH patches follow verified Chromium Dash Stable/Windows releases.
# Versions .199-.201 share the 2026-06-25 timestamp and dominate the distribution;
# .196-.198 form a small recent tail. Older clusters are intentionally excluded as
# implausible on a machine with active automatic updates.
CHROME_PATCH_DIST = [
    ("149.0.7827.201", 30),
    ("149.0.7827.200", 25),
    ("149.0.7827.199", 20),
    ("149.0.7827.198", 12),
    ("149.0.7827.197", 8),
    ("149.0.7827.196", 5),
]

# No proxy uses the cs-CZ host defaults; the launcher replaces them from proxy GeoIP.
# `languages` and `accept_language` are declarative. The launcher applies both web
# surfaces through intl.accept_languages, while native configuration reads time zone.
DEFAULT_LOCALE = {
    "languages": ["cs-CZ", "cs", "en"],
    "accept_language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "timezone": "Europe/Prague",
    "country": "CZ",
}

# --- Host portability: every hardware-bound value comes from the host probe. ---
# There is no silent development-machine fallback. Generation stops without schema-v2
# measurements; FP_HOST_WEBGL is an explicit diagnostic override only.
_HOST_CACHE = {"loaded": False, "data": None}
_WARNED = set()


def _warn_once(key, msg):
    """Emit a warning once instead of repeating it for every profile."""
    if key not in _WARNED:
        _WARNED.add(key)
        print(msg)


def host_info(quiet=True):
    """Return cached host measurements without launching the browser."""
    if not _HOST_CACHE["loaded"]:
        _HOST_CACHE["loaded"] = True
        try:
            sys.path.insert(0, os.path.join(ROOT, "scripts"))
            import probe_host as P
            _HOST_CACHE["data"] = P.load_cache()
        except Exception:
            _HOST_CACHE["data"] = None
    return _HOST_CACHE["data"]


NO_PROBE_HINT = (
    "A valid host measurement is missing for THIS computer (schema v2).\n"
    "  Run: python scripts/probe_host.py --force\n"
    "  Without it, profiles could claim another computer's GPU, display, or fonts;\n"
    "  generation stops instead of fabricating those values.")


def host_require(what="profile generation"):
    """Require and return host measurements, or stop with instructions."""
    h = host_info()
    if not h:
        raise SystemExit(f"{what}: {NO_PROBE_HINT}")
    return h


def _env_host_webgl():
    """Read the explicit diagnostic FP_HOST_WEBGL="vendor|renderer" override."""
    raw = os.environ.get("FP_HOST_WEBGL", "").strip()
    if not raw:
        return None
    if "|" not in raw:
        raise SystemExit('FP_HOST_WEBGL must use the form "vendor|renderer"')
    vendor, renderer = raw.split("|", 1)
    return {"vendor": vendor.strip(), "renderer": renderer.strip()}


def host_webgl():
    """Return the measured host identity for top-level webgl values."""
    env = _env_host_webgl()
    if env:
        _warn_once("env_webgl", f"WARNING: FP_HOST_WEBGL override -> {env['renderer']}")
        return env
    h = host_require("host GPU")
    gl = h.get("webgl") or {}
    if not gl.get("renderer"):
        raise SystemExit(f"host GPU: the probe has no webgl.renderer.\n  {NO_PROBE_HINT}")
    return {"vendor": gl.get("vendor"), "renderer": gl["renderer"]}


def __getattr__(name):
    """Expose backward-compatible host attributes lazily through PEP 562."""
    if name == "HOST_WEBGL":
        return host_webgl()
    if name == "GPU_RENDERER_DIST":
        return gpu_renderer_dist()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# GPU name rotation changes only the marketing name within the real 0x67DF
# Ellesmere/Polaris device identity. Pixel output and WebGPU remain coherent.
# Measurements: RX 580 control 0.044, RX 570 0.037, RX 480 0.039. RX 470 was
# excluded at 0.107 tampering and 0.23 VM score.
_GPU_SFX = "Direct3D11 vs_5_0 ps_5_0, D3D11)"


def gpu_renderer_dist():
    """Return safe renderer-name variants only for an actual 0x67DF host."""
    host = host_webgl()
    h = host_info()
    is_ellesmere = "0x000067DF" in (host.get("renderer") or "")
    if h:
        wg = h.get("webgpu") or {}
        is_ellesmere = is_ellesmere and wg.get("vendor") == "amd" and wg.get("architecture") == "gcn-4"
    if not is_ellesmere:
        return [(host["renderer"], 100)]
    return [
        (host["renderer"], 40),  # Real host adapter remains the dominant option.
        ("ANGLE (AMD, Radeon RX 570 Series (0x000067DF) " + _GPU_SFX, 30),
        ("ANGLE (AMD, Radeon RX 480 Graphics (0x000067DF) " + _GPU_SFX, 30),
    ]


# GPU_RENDERER_DIST remains available lazily through module __getattr__.

# --- Cross-vendor GPU templates: atomic WebGL + WebGPU identities. ---
# FP_GPU_TEMPLATE selects a complete catalog entry for all profiles. Native C++
# validates the block atomically; top-level webgl remains a coherent host fallback.
GPU_TEMPLATES_FILE = os.path.join(ROOT, "resources", "gpu_templates.json")


def load_gpu_template(name):
    """Load and validate the minimal name/WebGL/WebGPU template block."""
    if not name:
        return None
    templates = {item.get("name"): item for item in all_gpu_templates()}
    tpl = templates.get(name)
    if tpl is None:
        raise SystemExit(f"FP_GPU_TEMPLATE={name!r}: template not found in {GPU_TEMPLATES_FILE}")
    webgl = tpl.get("webgl") or {}
    webgpu = tpl.get("webgpu") or {}
    if not webgl.get("vendor") or not webgl.get("renderer"):
        raise SystemExit(f"template {name}: missing webgl.vendor/renderer")
    if not webgpu.get("vendor") or not webgpu.get("architecture"):
        raise SystemExit(f"template {name}: missing webgpu.vendor/architecture")
    block = {
        "profile": tpl.get("name", name),
        "webgl": {"vendor": webgl["vendor"], "renderer": webgl["renderer"]},
        "webgpu": {"vendor": webgpu["vendor"], "architecture": webgpu["architecture"]},
    }
    if "subgroup_min_size" in webgpu and "subgroup_max_size" in webgpu:
        block["webgpu"]["subgroup_min_size"] = webgpu["subgroup_min_size"]
        block["webgpu"]["subgroup_max_size"] = webgpu["subgroup_max_size"]
    return block


def active_gpu_template():
    """Return the environment-selected template, which overrides GPU modes."""
    name = os.environ.get("FP_GPU_TEMPLATE", "").strip()
    if not name:
        return None, None
    return name, load_gpu_template(name)


# --- GPU selection modes (--gpu). ---
# off uses the native host, family preserves vendor and architecture, common uses
# measured common discrete adapters, and all includes rare experimental templates.
GPU_MODES = ("off", "family", "common", "all")
DEFAULT_GPU_MODE = "common"
_TEMPLATE_CACHE = {"loaded": False, "items": []}

# Population pools use a measured-tampering ceiling because catalog rarity does not
# predict detector response. The sweep-scale threshold retains 22 of 48 measured
# adapters. Unmeasured templates remain available only through `all`.
GPU_MAX_MEASURED_TAMPERING = 0.15

# Weak integrated hosts default to `family`, since real Dawn/ANGLE capabilities
# cannot plausibly sit beside a claimed high-end discrete adapter.
_WEAK_GPU_MARKERS = ("Intel(R) HD", "Intel(R) UHD", "Iris", "Radeon(TM) Graphics",
                     "Vega .* Graphics", "Microsoft Basic", "SwiftShader", "llvmpipe")


def host_gpu_is_weak():
    """Conservatively identify weak or integrated graphics from host measurements."""
    h = host_info()
    if not h:
        return False
    if (h.get("webgpu") or {}).get("is_fallback_adapter"):
        return True
    ren = (h.get("webgl") or {}).get("renderer") or ""
    return any(re.search(m, ren) for m in _WEAK_GPU_MARKERS)


def default_gpu_mode():
    """Return the default --gpu mode for new profiles."""
    if host_gpu_is_weak():
        _warn_once("weak_gpu",
                   "[host] weak/integrated graphics: defaulting to --gpu family "
                   "to avoid unsupported performance claims.")
        return "family"
    return DEFAULT_GPU_MODE


def all_gpu_templates():
    """Load all templates once and return them sorted by name."""
    if not _TEMPLATE_CACHE["loaded"]:
        _TEMPLATE_CACHE["loaded"] = True
        if not os.path.isfile(GPU_TEMPLATES_FILE):
            raise SystemExit(f"Missing public GPU template catalog: {GPU_TEMPLATES_FILE}")
        try:
            with open(GPU_TEMPLATES_FILE, "r", encoding="utf-8") as f:
                catalog = json.load(f)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Cannot load GPU template catalog: {exc}")
        if catalog.get("schema_version") != 1 or not isinstance(catalog.get("templates"), list):
            raise SystemExit(f"Unsupported GPU template catalog schema: {GPU_TEMPLATES_FILE}")
        items = sorted(catalog["templates"], key=lambda item: item.get("name", ""))
        if len({item.get("name") for item in items}) != len(items):
            raise SystemExit(f"Duplicate names in GPU template catalog: {GPU_TEMPLATES_FILE}")
        _TEMPLATE_CACHE["items"] = items
    return _TEMPLATE_CACHE["items"]


def gpu_pool(mode):
    """Return a deterministic sorted template-name pool for a GPU mode."""
    if mode not in GPU_MODES:
        raise SystemExit(f"unknown --gpu mode {mode!r}, allowed: {', '.join(GPU_MODES)}")
    if mode == "off":
        return []
    # Exclude host-* control duplicates so their matching win-* cards are not overweighted.
    items = [t for t in all_gpu_templates() if not t.get("name", "").startswith("host-")]
    if mode == "family":
        # Family selection requires measured vendor and architecture.
        h = host_require("--gpu family")
        wg = h.get("webgpu") or {}
        v, a = wg.get("vendor"), wg.get("architecture")
        items = [t for t in items
                 if (t.get("webgpu") or {}).get("vendor") == v
                 and (t.get("webgpu") or {}).get("architecture") == a]
    elif mode == "common":
        items = [t for t in items if t.get("rarity") == "common"]
    # `all` deliberately bypasses the ceiling for explicit experiments.
    if mode in ("family", "common"):
        items = _under_tampering_cap(items, mode)
    return sorted({t["name"] for t in items})


def _under_tampering_cap(items, mode):
    """Filter risky or unmeasured adapters, warning if that would empty the pool."""
    keep = [t for t in items
            if isinstance(t.get("measured_tampering_p1"), (int, float))
            and t["measured_tampering_p1"] <= GPU_MAX_MEASURED_TAMPERING]
    if not keep:
        if items:
            _warn_once(f"gpu_cap_empty:{mode}",
                       f"[gpu] --gpu {mode}: no adapter below the tampering ceiling "
                       f"{GPU_MAX_MEASURED_TAMPERING} (or none were measured) -> "
                       f"using the UNFILTERED pool ({len(items)} adapters). "
                       f"Profiles may receive an identity with a high tampering score.")
        return items
    if len(keep) < len(items):
        _warn_once(f"gpu_cap:{mode}",
                   f"[gpu] --gpu {mode}: {len(keep)}/{len(items)} adapters below the "
                   f"tampering ceiling {GPU_MAX_MEASURED_TAMPERING}; the remainder "
                   f"exceeded the ceiling or had no measurement.")
    return keep


def pick_gpu_template(seed, mode):
    """Select a deterministic per-profile template name, or None."""
    pool = gpu_pool(mode)
    if not pool:
        return None
    return pool[_seed_int(seed, "gpu:template") % len(pool)]


def gpu_block_for(seed, mode):
    """Return (name, block); FP_GPU_TEMPLATE overrides the mode."""
    env_name, env_block = active_gpu_template()
    if env_block:
        return env_name, env_block
    name = pick_gpu_template(seed, mode)
    if not name:
        return None, None
    return name, load_gpu_template(name)

# --- Screen: a soft per-profile axis with hard coherence constraints. ---
# Pin color depth and DPR to the measured host. Candidate resolutions must be
# plausible at DPR 1, and the physical window must fit both host and claimed screen.
RESOLUTIONS = [
    ((1920, 1080), 42),
    ((2560, 1440), 12),
    ((1366, 768), 12),
    ((1600, 900), 10),
    ((1920, 1200), 6),
    ((1440, 900), 6),
    ((1680, 1050), 5),
    ((1280, 720), 4),
    ((1280, 1024), 3),
]
TASKBAR_H = 40                 # Windows bottom taskbar: avail_height = height - 40.

# hardwareConcurrency must not exceed measured host cores; clamp this population shape.
HWCONC_DIST = [(8, 40), (16, 25), (12, 15), (6, 10), (4, 10)]
# deviceMemory uses specification buckets capped at min(host, 8).
DEVMEM_DIST = [(8, 65), (4, 35)]
DEVMEM_SPEC_MAX = 8


def host_color_depth():
    """Return native screen.colorDepth measured on this Chromium build."""
    s = host_require("screen.colorDepth").get("screen") or {}
    d = s.get("color_depth") or s.get("colorDepth")
    if not d:
        raise SystemExit(f"screen.colorDepth: the probe has no depth.\n  {NO_PROBE_HINT}")
    return int(d)


def host_dpr():
    """Return measured host devicePixelRatio; the launcher normalizes HiDPI."""
    s = host_require("devicePixelRatio").get("screen") or {}
    dpr = s.get("device_pixel_ratio", s.get("devicePixelRatio"))
    if not dpr:
        raise SystemExit(f"devicePixelRatio: the probe has no DPR.\n  {NO_PROBE_HINT}")
    return float(dpr)


def hidpi_scale_flags(quiet=True):
    """Return flags that normalize HiDPI to the measured DPR-1 baseline.

    A 2026-07-26 five-profile Release gate at 150% scaling matched the frozen 100%
    baseline after forcing scale factor 1. Return no flags before host measurement.
    """
    h = host_info()
    if not h:
        return []
    s = h.get("screen") or {}
    dpr = s.get("device_pixel_ratio", s.get("devicePixelRatio")) or 1.0
    if abs(float(dpr) - 1.0) <= 1e-6:
        return []
    if not quiet:
        _warn_once("hidpi", f"[host] display scaling {int(float(dpr) * 100)} % "
                            f"(dpr {dpr}) -> --force-device-scale-factor=1; the browser "
                            f"window will look smaller while keeping the reference fingerprint.")
    return ["--force-device-scale-factor=1"]


def host_physical():
    """Return measured physical monitor width and available height in pixels.

    The probe reports CSS pixels at current scaling. Because Browser3 normalizes
    HiDPI to scale factor 1, multiply those measurements back by host DPR.
    """
    s = host_require("physical monitor").get("screen") or {}
    w = s.get("avail_width") or s.get("width")
    h = s.get("avail_height")
    if not h and s.get("height"):
        h = int(s["height"]) - TASKBAR_H
    if not w or not h:
        raise SystemExit(f"physical monitor: the probe contains no dimensions.\n  {NO_PROBE_HINT}")
    dpr = host_dpr()
    return int(round(int(w) * dpr)), int(round(int(h) * dpr))


def _clamp_dist(dist, cap, label):
    """Clamp a distribution to the host ceiling, retaining its lowest bucket."""
    kept = [(v, w) for v, w in dist if v <= cap]
    if not kept:
        lowest = min(v for v, _ in dist)
        _warn_once(f"clamp:{label}", f"WARNING: host {label} = {cap} is below the entire "
                                     f"distribution; every profile will use {lowest}.")
        return [(lowest, 100)]
    if len(kept) != len(dist):
        _warn_once(f"clamp:{label}",
                   f"[host] {label}: distribution capped at <= {cap} "
                   f"({[v for v, _ in kept]}); claiming more than the host is a VM signal.")
    return kept


def host_hwconc_dist():
    """Return HWCONC_DIST capped to measured logical host cores."""
    hw = host_require("hardwareConcurrency").get("hardware") or {}
    cap = hw.get("hardware_concurrency")
    if not cap:
        raise SystemExit(f"hardwareConcurrency: the probe contains no CPU count.\n  {NO_PROBE_HINT}")
    return _clamp_dist(HWCONC_DIST, int(cap), "hardware_concurrency")


def host_devmem_dist():
    """Return DEVMEM_DIST capped to the host and specification maximum of 8."""
    hw = host_require("deviceMemory").get("hardware") or {}
    cap = hw.get("device_memory") or DEVMEM_SPEC_MAX
    return _clamp_dist(DEVMEM_DIST, min(int(cap), DEVMEM_SPEC_MAX), "device_memory")

# prefers-color-scheme is a clean user-preference axis with no hardware cross-check.
# Native WebPreferences propagation keeps media queries and rendered colors coherent.
COLOR_SCHEME_DIST = [("light", 55), ("dark", 45)]


def _seed_int(seed, key):
    """Return the deterministic 32-bit value for a seed and stable axis key.

    Salted keys provide independent axes without slicing a finite profile ID. Key
    names are part of the profile identity and must not be renamed casually.
    """
    h = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _seed_frac(seed, key):
    """Return a uniform 0..1 value for prevalence thresholds."""
    return _seed_int(seed, key) / float(1 << 32)


def _weighted_pick(value_int, dist):
    """Select deterministically from `(item, weight)` pairs."""
    total = sum(w for _, w in dist)
    r = value_int % total
    cum = 0
    for item, w in dist:
        cum += w
        if r < cum:
            return item
    return dist[-1][0]


def bump_ua_to_build(ua: str) -> str:
    """Replace the UA Chrome version with CHROME_VERSION."""
    # Chrome UA convention zeros minor, build, and patch.
    return re.sub(r"Chrome/\d+\.\d+\.\d+\.\d+", f"Chrome/{CHROME_MAJOR}.0.0.0", ua)


def pick_chrome_version(seed):
    """Choose a per-profile patch while preserving the real major.minor.build."""
    return _weighted_pick(_seed_int(seed, "ua:patch"), CHROME_PATCH_DIST)


def build_ua_ch(full_version):
    """Build UA Client Hints configuration.

    Native C++ consumes brands and full_version. Remaining fields document the
    expected native host state. Platform version is deliberately not spoofed because
    Windows-version font markers cannot be fabricated coherently.
    """
    return {
        "platform": "Windows",
        "mobile": False,
        # Native Chrome 149 GREASE remains authoritative for low-entropy brands.
        "brands": [
            {"brand": "Chromium", "version": CHROME_MAJOR},
            {"brand": "Google Chrome", "version": CHROME_MAJOR},
        ],
        # High entropy (getHighEntropyValues / Accept-CH) with per-profile patch.
        "full_version": full_version,
        "full_version_list": [
            {"brand": "Chromium", "version": full_version},
            {"brand": "Google Chrome", "version": full_version},
        ],
        "platform_version": "10.0.0",   # Declarative; native value comes from host.
        "architecture": "x86",
        "bitness": "64",
        "wow64": False,
        "model": "",
    }


def pick_hwconc(seed):
    """Choose hardwareConcurrency without exceeding measured host cores."""
    return _weighted_pick(_seed_int(seed, "hw:conc"), host_hwconc_dist())


def pick_devmem(seed):
    """Choose deviceMemory capped by the host and specification."""
    return _weighted_pick(_seed_int(seed, "hw:mem"), host_devmem_dist())


def pick_webgl_renderer(seed):
    """Choose a safe per-profile GPU marketing-name variant."""
    return _weighted_pick(_seed_int(seed, "gpu"), gpu_renderer_dist())


def pick_color_scheme(seed):
    """Choose per-profile prefers-color-scheme."""
    return _weighted_pick(_seed_int(seed, "media:colorscheme"), COLOR_SCHEME_DIST)


def pick_media_devices(seed):
    """Choose hide-only media-device caps; native C++ never fabricates devices."""
    devices = {}
    # Desktops often have no webcam, so some profiles cap video input at zero.
    if _weighted_pick(_seed_int(seed, "media:videoin"), [(1, 55), (0, 45)]) == 0:
        devices["max_video_input"] = 0
    # Split microphone presence approximately evenly.
    if _weighted_pick(_seed_int(seed, "media:audioin"), [(1, 50), (0, 50)]) == 0:
        devices["max_audio_input"] = 0
    # Audio output may be capped at one, never zero; native C++ only reduces counts.
    if _weighted_pick(_seed_int(seed, "media:audioout"), [(0, 60), (1, 40)]) == 1:
        devices["max_audio_output"] = 1
    return devices


def pick_screen(seed):
    """Choose a per-profile resolution and coherent window geometry."""
    (w, h) = _weighted_pick(_seed_int(seed, "screen:res"), RESOLUTIONS)
    avail_w, avail_h = w, h - TASKBAR_H
    win_w, win_h = pick_window(seed, avail_w, avail_h)
    depth = host_color_depth()
    return {
        "width": w, "height": h,
        "avail_width": avail_w, "avail_height": avail_h,
        "color_depth": depth, "pixel_depth": depth,
        "device_pixel_ratio": host_dpr(),
        # Launcher passes this outer size; it fits both claimed and real screens.
        "window_width": win_w, "window_height": win_h,
    }


def pick_window(seed, avail_w, avail_h):
    """Choose a slightly jittered outer window that fits claimed and real screens."""
    phys_w, phys_avail_h = host_physical()
    max_w = min(avail_w, phys_w)
    max_h = min(avail_h, phys_avail_h)
    jw = _seed_int(seed, "screen:winw") % 120
    jh = _seed_int(seed, "screen:winh") % 120
    win_w = max(1024, max_w - jw)
    win_h = max(640, max_h - jh)
    return win_w, win_h


# Text edging is a clean per-profile axis. Native glyph rasterization changes text
# and canvas hashes while remaining a coherent Windows configuration.
TEXT_EDGING_CYCLE = ["lcd", "grayscale", "alias"]


def pick_text_edging(seed):
    """Choose stable text edging; keep subpixel and hinting unchanged."""
    return TEXT_EDGING_CYCLE[_seed_int(seed, "text:edging") % len(TEXT_EDGING_CYCLE)]


# Font hiding is a second clean axis. Hide coherent optional language bundles that
# may legitimately be absent, never core Latin/UI fonts. Missing families are a no-op.
FONT_BUNDLES = [
    ("jp", ["MS Gothic", "MS PGothic", "MS UI Gothic", "MS Mincho", "MS PMincho",
            "Yu Gothic", "Yu Gothic UI", "Yu Gothic Light", "Yu Gothic Medium",
            "Yu Mincho", "Meiryo", "Meiryo UI"]),
    ("kr", ["Malgun Gothic", "Malgun Gothic Semilight"]),
    ("sc", ["SimSun", "NSimSun", "SimSun-ExtB", "Microsoft YaHei",
            "Microsoft YaHei UI", "Microsoft YaHei Light", "KaiTi", "SimHei",
            "FangSong", "DengXian", "DengXian Light"]),
    ("tc", ["MingLiU", "PMingLiU", "MingLiU-ExtB", "PMingLiU-ExtB", "MingLiU_HKSCS",
            "Microsoft JhengHei", "Microsoft JhengHei UI", "Microsoft JhengHei Light"]),
    ("sea", ["Leelawadee UI", "Leelawadee UI Semilight", "Myanmar Text",
             "Javanese Text", "Microsoft New Tai Lue", "Microsoft Tai Le",
             "Microsoft PhagsPa", "Microsoft Yi Baiti"]),
    ("indic", ["Nirmala UI", "Nirmala UI Semilight"]),
]


# --- Host font inventory portability. ---
# Compute every font budget against measured inventory to avoid implausible sparsity.


def host_font_set():
    """Return measured lowercase host families, or None for legacy probe data."""
    fonts = (host_info() or {}).get("fonts") or {}
    present = fonts.get("present")
    if not present:
        return None
    # Warn when candidate-list changes make the cached inventory incomplete.
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import probe_host as P
        if fonts.get("candidates_hash") and fonts["candidates_hash"] != P.font_candidates_hash():
            _warn_once("font_hash",
                       "WARNING: font lists changed after the last host measurement. Run "
                       "`python scripts/probe_host.py --force`.")
    except Exception:
        pass
    return {f.lower() for f in present}


def host_fp_probed_present():
    """Return FP.com probe families actually present on the host."""
    hs = host_font_set()
    if hs is None:
        return list(FP_PROBED_FONTS)
    return [f for f in FP_PROBED_FONTS if f.lower() in hs]


def font_hiding_enabled():
    """Disable hiding with a warning when the host font inventory is already sparse."""
    have = len(host_fp_probed_present())
    if have < MIN_FP_FONTS_VISIBLE:
        _warn_once("font_poor",
                   f"WARNING: this computer has a sparse font inventory ({have} FP.com "
                   f"families < {MIN_FP_FONTS_VISIBLE}); font hiding is disabled for all profiles.")
        return False
    return True


def _bundle_available(fonts):
    """Return whether the host contains at least one family from a bundle."""
    hs = host_font_set()
    if hs is None:
        return True
    return any(f.lower() in hs for f in fonts)


def pick_hidden_fonts(seed):
    """Choose deterministic complete optional language bundles to hide.

    Natural collisions are retained rather than forcing an unrealistically uniform
    population. Unavailable bundles are skipped without shifting mask bits.
    """
    if not font_hiding_enabled():
        return []
    n = len(FONT_BUNDLES)
    mask = _seed_int(seed, "font:mask") % (1 << n)
    hidden = []
    for i, (_, fonts) in enumerate(FONT_BUNDLES):
        if mask & (1 << i) and _bundle_available(fonts):
            hidden.extend(fonts)
    return hidden


# --- Conservative extended optional-font pool. ---
# Whole software-related bundles may be absent on real Windows machines. Per-bundle
# absence prevalence creates a natural population; core and web-safe fonts are guarded.
OPTIONAL_FONT_BUNDLES = [
    # (key, CSS family names, probability that the profile lacks this software)
    ("office_symbols", [
        "MS Outlook", "MS Reference Specialty", "MS Reference Sans Serif",
        "MT Extra", "Bookshelf Symbol 7",
    ], 0.35),
    # Bitstream and Letraset are independent vendors despite some joint bundling.
    ("bitstream_bt", [
        "Blackletter 686 BT", "Broadway BT", "Calligraphic 421 BT", "Cataneo BT",
        "Holiday Pi BT", "Mister Earl BT", "Old Dreadful No.7 BT", "Park Avenue BT",
        "Square 721 BT", "Staccato 222 BT", "Staccato222 BT",
    ], 0.70),
    ("letraset_let", [
        "Academy Engraved LET", "Highlight LET", "John Handy LET", "La Bamba LET",
        "Milano LET", "Odessa LET", "Orange LET", "Quixley LET", "Ruach LET",
        "Scruff LET", "Smudger LET", "Tiranti Solid LET", "University Roman LET",
        "Victorian LET", "Westwood LET", "Mekanik LET", "One Stroke Script LET",
        "Pump Demi Bold LET",
    ], 0.60),
    ("hp_software", [
        "HP Simplified", "HP Simplified Light",
    ], 0.70),
    # Cascadia commonly arrives with Windows Terminal; 0.55 is a plausible split.
    ("dev_tools", [
        "Cascadia Code", "Cascadia Mono",
    ], 0.55),
    ("webapp_fonts", [
        "Lato", "Open Sans", "Open Sans Light", "Open Sans Semibold",
        "Open Sans Extrabold", "Charis SIL",
    ], 0.55),
    # Segoe Print/Script are Windows defaults and therefore excluded from hiding.
]

# Core, web-safe, and system families must never be hidden. Optional CJK families
# are deliberately absent from this guard list.
CORE_NEVER_HIDE = frozenset(x.lower() for x in [
    "arial", "arial black", "arial narrow", "times new roman", "courier new",
    "courier", "georgia", "verdana", "trebuchet ms", "tahoma", "comic sans ms",
    "impact", "webdings", "wingdings", "wingdings 2", "wingdings 3", "symbol",
    "marlett", "segoe ui", "segoe ui black", "segoe ui light", "segoe ui semibold",
    "segoe ui semilight", "segoe ui emoji", "segoe ui symbol", "segoe ui historic",
    "segoe ui variable", "segoe mdl2 assets", "holo mdl2 assets",
    "ms sans serif", "ms serif", "microsoft sans serif", "small fonts",
    "palatino linotype", "sylfaen", "ebrima", "gadugi",
    "cambria", "cambria math", "calibri", "calibri light", "candara", "consolas",
    "constantia", "corbel", "lucida console", "lucida sans unicode",
    "mv boli", "microsoft himalaya", "mongolian baiti",
    "sitka small", "sitka text", "sitka subheading", "sitka heading",
    "sitka display", "sitka banner",
])


# --- FP.com probe budget. ---
# A failed Office experiment left four detected families and scored 0.943/0.739.
# Keeping 12-16 measured families was clean, so enforce a hard minimum.
FP_PROBED_FONTS = [
    "Agency FB", "Calibri", "Century", "Century Gothic", "Franklin Gothic",
    "Haettenschweiler", "Lucida Bright", "Lucida Sans", "MS Outlook",
    "MS Reference Specialty", "MS UI Gothic", "MT Extra", "Marlett",
    "Monotype Corsiva", "Pristina", "Segoe UI Light", "Staccato222 BT",
]
MIN_FP_FONTS_VISIBLE = 12   # Verified lower bound before detection rises.


def _assert_fp_font_budget(hidden, idx):
    """Enforce the minimum visible FP.com font budget against host inventory."""
    probed = host_fp_probed_present()
    hidden_lc = {f.lower() for f in hidden}
    visible = [f for f in probed if f.lower() not in hidden_lc]
    if len(visible) < MIN_FP_FONTS_VISIBLE and font_hiding_enabled():
        raise AssertionError(
            f"profile {idx}: only {len(visible)} font families remain visible to FP.com "
            f"(of {len(probed)} on the host, < {MIN_FP_FONTS_VISIBLE}) -> implausibly "
            f"sparse font set. Hidden FP-probed families: "
            f"{[f for f in probed if f.lower() in hidden_lc]}")
    return len(visible)


# --- CreepJS marker fonts (getWindows()). ---------------------------------------
# CreepJS derives the Windows version from fonts rather than UA:
#   fontVersion = { '11': map['11'].find(x => fonts.includes(x)), ... }
#   hash '10,7,8,8.1' -> "Windows 10"
# Groups 11/10/8.1/8 require any marker and group 7 requires all markers. Keep
# applicable groups detectable so getWindows() never returns an anomalous undefined.
CREEPJS_WIN_FONT_MARKERS = {
    "11": ["Segoe Fluent Icons"],
    "10": ["HoloLens MDL2 Assets", "Segoe MDL2 Assets", "Bahnschrift", "Ink Free"],
    "8.1": ["Leelawadee UI", "Javanese Text", "Segoe UI Emoji"],
    "8": ["Aldhabi", "Gadugi", "Myanmar Text", "Nirmala UI"],
    "7": ["Cambria Math", "Lucida Console"],
}
# Guard only groups actually present on the host; fonts can be hidden, not added.
# Constants provide a fallback for measurements without font inventory.
CREEPJS_WIN_GROUPS_ANY = ("10", "8.1", "8")
CREEPJS_WIN_GROUPS_ALL = ("7",)


def creepjs_win_groups():
    """Return applicable CreepJS any-marker and all-marker host groups."""
    markers = ((host_info() or {}).get("fonts") or {}).get("creepjs_markers")
    if not markers:
        return CREEPJS_WIN_GROUPS_ANY, CREEPJS_WIN_GROUPS_ALL
    any_g = tuple(g for g in ("11", "10", "8.1", "8") if markers.get(g))
    all_g = tuple(g for g in ("7",)
                  if len(markers.get(g) or []) == len(CREEPJS_WIN_FONT_MARKERS[g]))
    return any_g, all_g


def _assert_creepjs_win_markers(hidden, idx):
    """Require every applicable CreepJS Windows group to remain detectable."""
    hidden_lc = {f.lower() for f in hidden}
    any_groups, all_groups = creepjs_win_groups()
    for group in any_groups:
        fonts = CREEPJS_WIN_FONT_MARKERS[group]
        if all(f.lower() in hidden_lc for f in fonts):
            raise AssertionError(
                f"profile {idx}: CreepJS marker group '{group}' is fully hidden "
                f"({fonts}) -> getWindows() cannot determine the Windows version. "
                f"Keep at least one marker font visible.")
    for group in all_groups:
        fonts = CREEPJS_WIN_FONT_MARKERS[group]
        gone = [f for f in fonts if f.lower() in hidden_lc]
        if gone:
            raise AssertionError(
                f"profile {idx}: CreepJS marker group '{group}' requires EVERY marker "
                f"font {fonts}, but {gone} are hidden -> getWindows() = undefined.")


def _bundle_is_hidden(seed, key, hide_prob):
    """Decide deterministically whether a profile lacks a software bundle."""
    return _seed_frac(seed, f"font:{key}") < hide_prob


def pick_optional_hidden(seed):
    """Choose optional non-language font bundles by absence prevalence."""
    if not font_hiding_enabled():
        return []
    hidden = []
    for key, fonts, hide_prob in OPTIONAL_FONT_BUNDLES:
        if _bundle_is_hidden(seed, key, hide_prob) and _bundle_available(fonts):
            hidden.extend(fonts)
    return hidden


# --- Speech voices: a hide-only axis analogous to fonts. ---
# Configuration stores stable name prefixes, so native matching is case-insensitive
# substring matching. Chrome must enumerate SAPI and OneCore voices; System.Speech
# alone is incomplete. Missing voices are a harmless no-op.
SPEECH_VOICE_BUNDLES = [
    # en-US desktop voices are separate installations and can be hidden individually.
    ("en-us-david", ["Microsoft David"], 0.35),
    ("en-us-zira", ["Microsoft Zira"], 0.30),
    ("en-us-mark", ["Microsoft Mark"], 0.40),
    ("en-gb", ["Microsoft Hazel", "Microsoft George", "Microsoft Susan"], 0.50),
    ("de", ["Microsoft Hedda", "Microsoft Katja", "Microsoft Stefan"], 0.55),
    ("fr", ["Microsoft Hortense", "Microsoft Julie", "Microsoft Paul"], 0.55),
    ("es", ["Microsoft Helena", "Microsoft Laura", "Microsoft Pablo"], 0.55),
    ("it", ["Microsoft Elsa", "Microsoft Cosimo"], 0.55),
    ("ru", ["Microsoft Irina", "Microsoft Pavel"], 0.60),
    ("pl", ["Microsoft Paulina", "Microsoft Adam"], 0.60),
    ("ja", ["Microsoft Haruka", "Microsoft Ayumi", "Microsoft Ichiro"], 0.60),
    ("zh", ["Microsoft Huihui", "Microsoft Yaoyao", "Microsoft Kangkang",
            "Microsoft Tracy", "Microsoft Hanhan"], 0.60),
    ("ko", ["Microsoft Heami"], 0.60),
    ("pt", ["Microsoft Maria", "Microsoft Daniel"], 0.60),
    ("cs", ["Microsoft Jakub"], 0.50),
]

# Language -> bundle key for locale coherence.
_VOICE_LANG_TO_BUNDLE = {
    "cs": ("cs",), "de": ("de",), "fr": ("fr",), "es": ("es",), "it": ("it",),
    "ru": ("ru",), "pl": ("pl",), "ja": ("ja",), "zh": ("zh",), "ko": ("ko",),
    "pt": ("pt",),
    "en": ("en-us-david", "en-us-zira", "en-us-mark", "en-gb"),
}


def pick_hidden_voices(seed, languages):
    """Choose hidden voices by prevalence while preserving claimed locales.

    Native C++ prevents hiding every actual host voice because only that call site
    sees the complete list.
    """
    protected = set()
    for lang in languages or []:
        primary = lang.split("-")[0].lower()
        keys = _VOICE_LANG_TO_BUNDLE.get(primary, ())
        if primary == "en":
            # Preserve one English bundle while retaining diversity among the others.
            protected.add(keys[0] if keys else None)
        else:
            protected.update(keys)
    protected.discard(None)

    hidden = []
    for key, voices, hide_prob in SPEECH_VOICE_BUNDLES:
        if key in protected:
            continue
        if _seed_frac(seed, f"speech:{key}") < hide_prob:
            hidden.extend(voices)
    return hidden


def _assert_no_core_in_bundles():
    """Fail if any hideable bundle contains a core or web-safe family."""
    all_bundles = [(k, f) for (k, f) in FONT_BUNDLES] + \
                  [(k, f) for (k, f, _p) in OPTIONAL_FONT_BUNDLES]
    for key, fonts in all_bundles:
        for fam in fonts:
            if fam.lower() in CORE_NEVER_HIDE:
                raise AssertionError(
                    f"CORE font '{fam}' is in bundle '{key}' — never hide it")


def random_seed():
    """Return a new 256-bit seed; every profile axis derives from it deterministically."""
    return secrets.token_hex(32)


def make_profile(ref, idx, seed=None, gpu_mode=None):
    """Build a deterministic profile from its reference ID or a fresh explicit seed."""
    nav = ref["browser_data"]["navigator"]
    if seed is None:
        seed = ref["base_profile_id"]
    label = f"profile_{idx:02d}"
    full_version = pick_chrome_version(seed)
    # Choose a per-profile template; FP_GPU_TEMPLATE overrides the mode.
    _gpu_block = gpu_block_for(seed, gpu_mode or default_gpu_mode())[1]
    _host_gl = host_webgl()
    prof = {
        "profile_id": seed,
        "label": label,
        "chrome_version": full_version,
        "ua": {
            "user_agent": bump_ua_to_build(nav["userAgent"]),
            **build_ua_ch(full_version),
        },
        "hardware": {
            # Seed-driven soft axes stay below measured/specification ceilings.
            "hardware_concurrency": pick_hwconc(seed),
            "device_memory": pick_devmem(seed),
        },
        "locale": {
            "languages": DEFAULT_LOCALE["languages"],
            "accept_language": DEFAULT_LOCALE["accept_language"],
        },
        "timezone": DEFAULT_LOCALE["timezone"],
        # Per-profile resolution/window; pin DPR and color depth to the host.
        "screen": pick_screen(seed),
        # Native color-scheme selection keeps media queries and rendering coherent.
        "media": {
            "color_scheme": pick_color_scheme(seed),
            # Hide-only device caps; native C++ never fabricates hardware.
            "devices": pick_media_devices(seed),
        },
        # `off` uses safe host name rotation. Other modes add an atomic GPU block
        # while retaining top-level webgl as the coherent host fallback.
        "webgl": {
            "vendor": _host_gl["vendor"],
            "renderer": (_host_gl["renderer"] if _gpu_block
                         else pick_webgl_renderer(seed)),
        },
        # Deterministic canvas/audio noise stays disabled because it was detected.
        # Any experiment must remain consistent across every readback API.
        "canvas": {
            "noise": {
                "enabled": False,
                "mode": "variance_gamma",  # Edges+gamma | additive | diagnostic 1px micro.
                "density_bits": 3,   # Perturb one in 2^3 pixels additively.
                "magnitude": 1,      # Additive ±1 per channel.
                "webgl": False,      # readPixels is not covered yet.
            },
        },
        "audio": {
            "noise": {
                "enabled": False,
                "mantissa_bits": 3,  # Replace the lowest three mantissa bits.
            },
            "reference_sum": ref["browser_data"].get("audio_fingerprint_sum"),
        },
        # Hide-only SAPI voice bundles; native C++ prevents an empty visible list.
        "speech": {
            "hidden_voices": pick_hidden_voices(seed, DEFAULT_LOCALE["languages"]),
        },
        # Native text edging changes glyph rasterization without changing geometry.
        "text": {
            "render": {
                "enabled": True,
                "edging": pick_text_edging(seed),  # lcd | grayscale | alias
                "hinting": "keep",   # DWrite ignores hinting on Windows; do not alter it.
                "subpixel": "keep",  # Preserve coherence; LCD with off is detectable.
            },
        },
        # Coherent optional-font subsets diversify enumeration and fallback text.
        "fonts": {
            # Combine CJK/regional bundles with the conservative optional pool.
            "hidden": pick_hidden_fonts(seed) + pick_optional_hidden(seed),
        },
        "proxy": None,   # Launcher adds proxy index and GeoIP when applicable.
        "user_data_dir": paths.profile_user_data_dir(label),
    }
    # Native C++ validates a cross-vendor GPU block atomically and falls back to host.
    if _gpu_block:
        prof["gpu"] = _gpu_block
    return prof


# --- Target-machine preflight. ---
# Centralize invariants that masking cannot safely repair.
WIN_MIN_BUILD = 17763          # Windows 10 1809 is Chrome 149's minimum.
_PREFLIGHT = {"done": False}


def preflight(strict=True):
    """Verify the host configuration; FP_ALLOW_HOST is for diagnostics only."""
    if _PREFLIGHT["done"]:
        return
    _PREFLIGHT["done"] = True
    h = host_require("preflight")
    problems = []

    os_info = h.get("os") or {}
    if (os_info.get("product") or "Windows") != "Windows":
        problems.append(f"target operating system is {os_info.get('product')}; only "
                        f"Windows x64 is supported because the claimed OS must match the host.")
    build = os_info.get("build")
    if isinstance(build, int) and build < WIN_MIN_BUILD:
        problems.append(f"Windows build {build} < {WIN_MIN_BUILD} (Windows 10 1809); "
                        f"Chrome 149 will not start there.")

    # HiDPI is supported: the launcher normalizes it to the verified DPR-1 baseline.
    hidpi_scale_flags(quiet=False)

    if (h.get("webgpu") or {}).get("is_fallback_adapter"):
        problems.append(
            "The GPU is a fallback adapter (SwiftShader); software rendering is itself a "
            "VM/tampering signal. Install or repair the "
            "D3D11 graphics driver and run the host probe again.")

    if not problems:
        return
    msg = "Preflight FAILED:\n" + "\n".join(f"  - {p}" for p in problems)
    if strict and os.environ.get("FP_ALLOW_HOST") != "1":
        raise SystemExit(msg + "\n  (set FP_ALLOW_HOST=1 to override for diagnostics only)")
    print(msg + "\n  CONTINUING with FP_ALLOW_HOST=1; profiles will NOT be coherent.")


def host_report():
    """Return a one-line summary of host inputs used by profile policy."""
    h = host_require("host summary")
    s = h.get("screen") or {}
    hw = h.get("hardware") or {}
    phys_w, phys_h = host_physical()
    return (f"[host] {_short_renderer((h.get('webgl') or {}).get('renderer'))} | "
            f"monitor {s.get('width')}x{s.get('height')} (available {phys_w}x{phys_h}) "
            f"depth={host_color_depth()} dpr={host_dpr():g} | "
            f"cores<={hw.get('hardware_concurrency')} mem<={hw.get('device_memory')} | "
            f"FP fonts {len(host_fp_probed_present())}/{len(FP_PROBED_FONTS)} | "
            f"probe {h.get('probed_at')} ({h.get('build')})")


def build_profile(ref, idx, seed=None, gpu_mode=None):
    """Build one validated profile without writing it to disk."""
    preflight()   # Host DPR, adapter, and Windows-version assumptions.
    prof = make_profile(ref, idx, seed=seed, gpu_mode=gpu_mode)
    # At least one voice bundle must remain visible.
    _hidden_voices = prof["speech"]["hidden_voices"]
    _all_voices = [v for _, vs, _ in SPEECH_VOICE_BUNDLES for v in vs]
    assert len(_hidden_voices) < len(_all_voices), (
        f"profile {idx}: every voice bundle is hidden -> empty getVoices()")
    # Belt and suspenders: no core font may reach the hidden list.
    bad = [f for f in prof["fonts"]["hidden"] if f.lower() in CORE_NEVER_HIDE]
    assert not bad, f"profile {idx}: core font in hidden list: {bad}"
    # Enforce the font-hiding magnitude ceiling.
    fp_visible = _assert_fp_font_budget(prof["fonts"]["hidden"], idx)
    # Keep every applicable CreepJS Windows marker group detectable.
    _assert_creepjs_win_markers(prof["fonts"]["hidden"], idx)
    return prof, fp_visible


def _short_renderer(renderer):
    """Extract a short adapter name from an ANGLE renderer string."""
    m = re.search(r"ANGLE \([^,]+, (.+?) \(0x", renderer or "")
    if m:
        return m.group(1)
    return (renderer or "?")[:28]


def profile_summary(path, prof, fp_visible):
    """Return a one-line generated-profile summary shared by both entry points."""
    s = prof["screen"]
    return (f"[ok] {path}  id={prof['profile_id'][:12]} "
            f"hwConc={prof['hardware']['hardware_concurrency']} "
            f"mem={prof['hardware']['device_memory']} "
            f"screen={s['width']}x{s['height']} win={s['window_width']}x{s['window_height']} "
            f"edging={prof['text']['render']['edging']} hidden={len(prof['fonts']['hidden'])} "
            f"fp_fonts={fp_visible}/{len(FP_PROBED_FONTS)} "
            f"cs={prof['media']['color_scheme']} "
            f"dev={prof['media']['devices'] or 'full'} "
            f"gpu={prof['gpu']['profile'] if prof.get('gpu') else _short_renderer(prof['webgl']['renderer'])} "
            f"chrome={prof['chrome_version']}")


def parse_gpu_arg(argv, default=DEFAULT_GPU_MODE):
    """Parse either raw --gpu argument form using the shared mode definitions."""
    for i, a in enumerate(argv):
        if a.startswith("--gpu="):
            val = a.split("=", 1)[1]
        elif a == "--gpu":
            if i + 1 >= len(argv):
                raise SystemExit(f"--gpu requires a value ({', '.join(GPU_MODES)})")
            val = argv[i + 1]
        else:
            continue
        if val not in GPU_MODES:
            raise SystemExit(f"unknown --gpu value {val!r}, allowed: {', '.join(GPU_MODES)}")
        return val
    return default


def main():
    paths.initialize_runtime_state()
    _assert_no_core_in_bundles()   # No hideable bundle may contain a core font.
    preflight()
    gpu_mode = parse_gpu_arg(sys.argv, default=default_gpu_mode())
    print(host_report())
    with open(os.path.join(ROOT, "profiles.json"), "r", encoding="utf-8") as f:
        ref = json.load(f)
    out_dir = paths.PROFILES_DIR
    os.makedirs(out_dir, exist_ok=True)
    pool = gpu_pool(gpu_mode)
    if gpu_mode != "off" and not pool:
        print(f"WARNING: --gpu {gpu_mode} has no templates (empty pool"
              f"{' — the host is the only matching template' if gpu_mode == 'family' else ''})"
              f"; profiles will keep the native host GPU.")
    for i, r in enumerate(ref, start=1):
        prof, fp_visible = build_profile(r, i, gpu_mode=gpu_mode)
        path = os.path.join(out_dir, f"profile_{i:02d}.json")
        paths.write_json_atomic(path, prof)
        print(profile_summary(path, prof, fp_visible))
    print(f"\nDone: {len(ref)} profiles, Chrome {CHROME_BASE}.x "
          f"(build {CHROME_VERSION}, per-profile patch), --gpu {gpu_mode} "
          f"(pool {len(pool)} adapters)")


if __name__ == "__main__":
    main()
