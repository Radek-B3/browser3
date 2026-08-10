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
CHROME_VERSION = "149.0.7827.201"       # == cílový build tag
CHROME_MAJOR = CHROME_VERSION.split(".")[0]
# major.minor.build sdílené všemi profily — MUSÍ se rovnat reálnému buildu, mění se
# jen patch (viz CHROME_PATCH_DIST). C++ call-site tenhle prefix validuje a claim
# s jiným prefixem zahodí.
CHROME_BASE = CHROME_VERSION.rsplit(".", 1)[0]

# Per-profil patch verze (UA-CH fullVersionList). Celá flotila hlásící identický
# patch je sama o sobě linkovací signál — reálná populace je přes patche rozprostřená.
# Hodnoty OVĚŘENÉ ze syrového chromiumdash API (fetch_releases, Stable/Windows), NE
# vymyšlené: neexistující patch by byl horší než žádný split.
#   .199/.200/.201 mají IDENTICKÝ timestamp (2026-06-25) => souběžné rollout arms,
#   reálné stroje mezi nimi opravdu mix mají → dominantní.
#   .198 (06-24) a .196/.197 (06-23) byly během dní nahrazeny → jen tenký chvost.
# Starší clustery (.22 z 05-20 atd.) VĚDOMĚ vynechány: stroj 2 měsíce bez updatu při
# aktivním auto-update je implauzibilní, a vzácnost = nápadnost (stejná lekce jako
# font-hiding magnitude strop).
CHROME_PATCH_DIST = [
    ("149.0.7827.201", 30),
    ("149.0.7827.200", 25),
    ("149.0.7827.199", 20),
    ("149.0.7827.198", 12),
    ("149.0.7827.197", 8),
    ("149.0.7827.196", 5),
]

# Bez proxy = host defaulty (OS je cs-CZ, pravidlo 4). S proxy přepíše launcher dle
# geo (Tier 1, zatím odloženo — timezone/locale spoofing v C++ ještě neexistuje).
# POZN.: pole `languages`/`accept_language` jsou jen DEKLARATIVNÍ — FpProfileConfig z profilu
# čte pouze `timezone`, navigator.languages i Accept-Language řídí VÝHRADNĚ launcher přes pref
# intl.accept_languages (ensure_lang_pref). Držíme je zarovnané se stock cs-CZ Chrome (3 položky,
# bez zbytečného en-US, aby JSON nelhal o tom, co se reálně aplikuje.
DEFAULT_LOCALE = {
    "languages": ["cs-CZ", "cs", "en"],
    "accept_language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "timezone": "Europe/Prague",
    "country": "CZ",
}

# --- HOST (přenositelnost): všechno hardware-vázané se ČTE Z PROBU ---------------
# Reálná GPU/screen/hardware/fonty hostitele pochází ze `scripts/probe_host.py`
# (cache gpu_profiles/host_current.json, schema v2). ŽÁDNÝ tichý fallback: dřív tu
# byla konstanta RX 580, takže na NVIDIA/Intel stroji by profily mlčky claimovaly
# cizí kartu jako „host identitu" = přesně ta hardware-bound nekoherence, kvůli které
# je cross-vendor povolen jen atomickým GPU blokem podle ověřeného hardware ceilingu.
# Bez probu se tedy generování ZASTAVÍ; konstanta smí být jen explicitně vyžádaný
# override (env FP_HOST_WEBGL="vendor|renderer") pro diagnostiku.
_HOST_CACHE = {"loaded": False, "data": None}
_WARNED = set()


def _warn_once(key, msg):
    """Hlasité varování, ale bez zaplavení výstupu (5+ profilů = 5× stejná hláška)."""
    if key not in _WARNED:
        _WARNED.add(key)
        print(msg)


def host_info(quiet=True):
    """Skutečný host z cache probu, nebo None. Nespouští prohlížeč — jen čte cache
    (probe se pouští z launcheru / scripts/probe_host.py)."""
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
    """Host je POVINNÝ. Vrací dict z probu, jinak tvrdě končí s návodem."""
    h = host_info()
    if not h:
        raise SystemExit(f"{what}: {NO_PROBE_HINT}")
    return h


def _env_host_webgl():
    """Explicitní override pro diagnostiku: FP_HOST_WEBGL="vendor|renderer"."""
    raw = os.environ.get("FP_HOST_WEBGL", "").strip()
    if not raw:
        return None
    if "|" not in raw:
        raise SystemExit('FP_HOST_WEBGL must use the form "vendor|renderer"')
    vendor, renderer = raw.split("|", 1)
    return {"vendor": vendor.strip(), "renderer": renderer.strip()}


def host_webgl():
    """Top-level webgl.* = REÁLNÁ identita hostitele (z probu). Bez probu = chyba."""
    env = _env_host_webgl()
    if env:
        _warn_once("env_webgl", f"POZOR: FP_HOST_WEBGL override -> {env['renderer']}")
        return env
    h = host_require("host GPU")
    gl = h.get("webgl") or {}
    if not gl.get("renderer"):
        raise SystemExit(f"host GPU: probe neobsahuje webgl.renderer.\n  {NO_PROBE_HINT}")
    return {"vendor": gl.get("vendor"), "renderer": gl["renderer"]}


def __getattr__(name):
    """PEP 562: HOST_WEBGL / GPU_RENDERER_DIST zůstávají dostupné jako atributy modulu
    (zpětná kompatibilita), ale vyhodnocují se AŽ PŘI PŘÍSTUPU. Eager verze by při
    chybějícím probu shodila celý import — a tím i launcher, který ten probe teprve
    umí spustit."""
    if name == "HOST_WEBGL":
        return host_webgl()
    if name == "GPU_RENDERER_DIST":
        return gpu_renderer_dist()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# GPU name-rotace = per-profil osa, která rotuje JEN marketingový NÁZEV renderer
# stringu v rámci REÁLNÉ host device ID 0x67DF (Ellesmere/Polaris, GCN4). RX 470/480/
# 570/580 jsou všechny legitimně 0x67DF → NENÍ to fabrikace, jen jiný obchodní název
# téhož křemíku. device ID zůstává reálná → pixel-hash i WebGPU (amd/gcn-4) sedí →
# KOHERENTNÍ. Rozbíjí renderer same-host linker (dřív všech 5 profilů = identický
# string, distinct 1/5). vendor konstantní AMD (rotace vendoru netřeba).
#
# Ověřeno scratch/gpu_string_experiment.py (N=3, --no-proxy, deterministické skóre):
#   RX 580 = 0.044 (control/host), RX 570 = 0.037, RX 480 = 0.039  → tampering ≤ control.
#   RX 470 VYŘAZEN (0.107 + vm 0.23). Cross-vendor / jiná device ID (NVIDIA 0x2504,
#   Navi 0x73FF) NEpoužita: WebGPU stále hlásí amd/gcn-4 → WebGL↔WebGPU nekoherence,
#   a produkční (proxy) + CreepJS arm zatím neproběhly (viz paměť gpu-spoof-hardware-
#   ceiling — původní cross-vendor regrese 0.5-0.65 byla nejspíš proxy-konfoundovaná).
_GPU_SFX = "Direct3D11 vs_5_0 ps_5_0, D3D11)"


def gpu_renderer_dist():
    """Name-rotace je vázaná na KONKRÉTNÍ křemík (0x67DF Ellesmere) — proto se smí
    použít JEN na tomto AMD gcn-4 hostu. Na jiném stroji by RX 570/480 stringy byly
    fabrikace (jiná device ID než reálná karta) → vracíme jen host renderer beze změny;
    diverzitu tam dělá atomický gpu blok (režim `family`)."""
    host = host_webgl()
    h = host_info()
    is_ellesmere = "0x000067DF" in (host.get("renderer") or "")
    if h:
        wg = h.get("webgpu") or {}
        is_ellesmere = is_ellesmere and wg.get("vendor") == "amd" and wg.get("architecture") == "gcn-4"
    if not is_ellesmere:
        return [(host["renderer"], 100)]
    return [
        (host["renderer"], 40),  # reálná host karta (nejčastější, on-manifold)
        ("ANGLE (AMD, Radeon RX 570 Series (0x000067DF) " + _GPU_SFX, 30),
        ("ANGLE (AMD, Radeon RX 480 Graphics (0x000067DF) " + _GPU_SFX, 30),
    ]


# GPU_RENDERER_DIST je dostupné přes modulové __getattr__ (lazy) — viz výše.

# --- Cross-vendor GPU template (atomická WebGL+WebGPU identita) -------------------
# EXPERIMENTÁLNÍ osa (plán PLAN_CROSS_VENDOR_GPU_MASKOVANI). Default VYPNUTO: bez
# aktivní šablony se generuje původní AMD name-rotace (GPU_RENDERER_DIST) a ŽÁDNÝ
# gpu blok → WebGPU zůstává nativní amd/gcn-4, vše koherentní jako dosud.
#
# Aktivace přes env FP_GPU_TEMPLATE=<name> (Python jen ORCHESTRUJE — vybere hotovou
# šablonu z gpu_profiles/templates/<name>.json; validace/clamp a runtime maskování
# jsou v C++ FpProfileConfig, ne tady). Aplikuje se na VŠECHNY profily, aby šel
# 5-profilový gate izolovat jako čistou NVIDIA/Intel větev vs zmrazená baseline.
#
# Kontrakt atomicity: top-level webgl.* se drží HOST (bezpečný fallback), cross-vendor
# identita jde JEN do gpu bloku. Když gpu blok neprojde C++ validací → webgl padne na
# host + webgpu nativní amd → koherentní host, nikdy půl NVIDIA. Viz ParseGpuBlock.
GPU_TEMPLATES_FILE = os.path.join(ROOT, "resources", "gpu_templates.json")


def load_gpu_template(name):
    """Načte minimální gpu šablonu (name/webgl/webgpu) z gpu_profiles/templates.
    Vrací dict připravený jako profil["gpu"] blok, nebo None při chybě/prázdnu."""
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
    """Vrací (name, block) aktivní cross-vendor šablony z env, nebo (None, None).
    FP_GPU_TEMPLATE připne JEDNU konkrétní kartu na VŠECHNY profily — override nad
    režimy níže (používá to scripts/verify_all_gpu_templates.py pro izolaci karty)."""
    name = os.environ.get("FP_GPU_TEMPLATE", "").strip()
    if not name:
        return None, None
    return name, load_gpu_template(name)


# --- Režimy výběru GPU (přepínač --gpu) -----------------------------------------
# off    = žádný gpu blok → nativní host GPU (dosavadní chování, AMD name-rotace)
# family = stejný vendor A stejná architektura jako host → pixely/limity/arch sedí
#          na reálný křemík; nejbezpečnější cross-card diverzita
# common = běžné moderní diskrétní karty (rarity=common) POD stropem tamperingu;
#          DEFAULT pro nové profily
# all    = všechny šablony včetně vzácných (vyšší tampering — viz VERIFICATION_121)
GPU_MODES = ("off", "family", "common", "all")
DEFAULT_GPU_MODE = "common"
_TEMPLATE_CACHE = {"loaded": False, "items": []}

# Strop měřeného tamperingu pro populační pooly (`common`, `family`).
# PROČ: `rarity` je klasifikace podle rozšířenosti karty, NE podle toho, jak na ni
# reaguje detektor — a ty dvě věci spolu nekorelují. Ve sweepu má `common` karta
# GTX 1080 hodnotu 0.5044 a RTX 4060 Ti 0.4279, zatímco `rare` UHD 750 jen 0.2354.
# Bez tohohle filtru byla přidělená karta loterie: 2026-07-26 měřeno na gate 1-5
# Intel HD 615 = 0.3849 a Iris Plus = 0.1742 proti 0.0049-0.0153 u moderních
# diskrétních adapters, při jinak identické konfiguraci. U produktu, který se rozdává
# bez podpory, je taková loterie
# nejhorší možná vlastnost — uživatel nepozná, že si vytáhl špatnou kartu.
#
# Hodnota je na ŠKÁLE SWEEPU (`measured_tampering_p1`), ne na dnešní absolutní
# škále — sweep měřil za jiných podmínek (RX 6750 XT: sweep 0.067, dnes 0.0153).
# Pro filtr je rozhodující POŘADÍ, které sedí; práh 0.15 nechává 22 z 48 měřených.
# Karty BEZ měření se do populačních poolů nepouští: nezměřená karta je nezměřené
# riziko a `all` je od toho, aby šly zkoušet.
GPU_MAX_MEASURED_TAMPERING = 0.15

# Hostitelé, u kterých `common` NENÍ bezpečný default: capabilities se nefabrikují
# (WebGL extensions/parametry i WebGPU features/limits pochází z reálného Dawn/ANGLE
# adaptéru), takže na slabé integrované grafice by claim „RTX 4070" seděl vedle limitů,
# které taková karta mít nemůže. Cross-vendor bylo bez regrese ověřeno na diskrétní
# kartě (VERIFICATION_121 = měření z JEDNOHO hostu), ne na iGPU.
# Konzervativní řešení do doby, než bude capability-tier filtr ověřen na druhém stroji:
# na slabém/integrovaném hostu se defaultně jede `family` (stejný vendor
# i architektura = limity sedí na reálný křemík).
_WEAK_GPU_MARKERS = ("Intel(R) HD", "Intel(R) UHD", "Iris", "Radeon(TM) Graphics",
                     "Vega .* Graphics", "Microsoft Basic", "SwiftShader", "llvmpipe")


def host_gpu_is_weak():
    """Heuristika „slabá/integrovaná grafika" z renderer stringu hosta + fallback
    adaptéru. Konzervativní: při pochybnostech False (= chová se jako dosud)."""
    h = host_info()
    if not h:
        return False
    if (h.get("webgpu") or {}).get("is_fallback_adapter"):
        return True
    ren = (h.get("webgl") or {}).get("renderer") or ""
    return any(re.search(m, ren) for m in _WEAK_GPU_MARKERS)


def default_gpu_mode():
    """Default režim --gpu pro NOVÉ profily. Viz _WEAK_GPU_MARKERS."""
    if host_gpu_is_weak():
        _warn_once("weak_gpu",
                   "[host] weak/integrated graphics: defaulting to --gpu family "
                   "to avoid unsupported performance claims.")
        return "family"
    return DEFAULT_GPU_MODE


def all_gpu_templates():
    """Načte všechny šablony (jednou) jako list dictů; seřazeno podle jména."""
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
    """Seřazený seznam JMEN šablon pro daný režim. Prázdný = žádný gpu blok.
    Pořadí je deterministické (podle jména), aby výběr ze seedu byl stabilní."""
    if mode not in GPU_MODES:
        raise SystemExit(f"unknown --gpu mode {mode!r}, allowed: {', '.join(GPU_MODES)}")
    if mode == "off":
        return []
    # host-* jsou identity-control šablony (duplikují win-* kartu) — do populačních
    # poolů nepatří, jinak by ta karta měla dvojnásobnou váhu.
    items = [t for t in all_gpu_templates() if not t.get("name", "").startswith("host-")]
    if mode == "family":
        # bez probu neumíme určit rodinu — a tichý prázdný pool by znamenal „nativní
        # host GPU", což u cizího stroje NENÍ to, co uživatel žádal (3.1) → tvrdá chyba
        h = host_require("--gpu family")
        wg = h.get("webgpu") or {}
        v, a = wg.get("vendor"), wg.get("architecture")
        items = [t for t in items
                 if (t.get("webgpu") or {}).get("vendor") == v
                 and (t.get("webgpu") or {}).get("architecture") == a]
    elif mode == "common":
        items = [t for t in items if t.get("rarity") == "common"]
    # "all" = beze změny; strop tamperingu se tam schválně NEuplatňuje (je to režim
    # „chci i vzácné karty", tedy vědomý experiment).
    if mode in ("family", "common"):
        items = _under_tampering_cap(items, mode)
    return sorted({t["name"] for t in items})


def _under_tampering_cap(items, mode):
    """Vyřadí karty nad `GPU_MAX_MEASURED_TAMPERING` a karty bez měření.
    Když by tím pool zůstal prázdný (typicky `family` na staré/exotické rodině),
    vrátí původní seznam a HLASITĚ varuje — prázdný pool by tiše znamenal „žádný
    gpu blok", tedy nativní host GPU místo režimu, který uživatel žádal."""
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
    """Deterministický per-profil výběr karty (pravidlo 8: stabilní pro daný
    profil+režim, různý napříč profily). Vrací jméno šablony nebo None."""
    pool = gpu_pool(mode)
    if not pool:
        return None
    return pool[_seed_int(seed, "gpu:template") % len(pool)]


def gpu_block_for(seed, mode):
    """(name, block) pro profil: FP_GPU_TEMPLATE override > režim. (None, None) = off."""
    env_name, env_block = active_gpu_template()
    if env_block:
        return env_name, env_block
    name = pick_gpu_template(seed, mode)
    if not name:
        return None, None
    return name, load_gpu_template(name)

# --- Screen: SOFT osa (per-profil), ALE s tvrdými koherenčními mantinely --------
# 1) color_depth/pixel_depth MUSÍ sedět s reálnou hloubkou displeje hostitele —
#    fingerprint.com ji cross-checkuje přes GPU/canvas; přepis 32->24 zvedl
#    tampering o +0.38 (CHANGELOG 2026-07-07). Tento host hlásí 32 → pin.
# 2) devicePixelRatio NENÍ spoofovatelný (C++ nemá accessor → drží reálný host =
#    1.0). Proto smíme claimovat JEN rozlišení plauzibilní při dpr=1.0 (nativní,
#    100% scaling). NIKDY 1536x864 apod. — to implikuje 125% scaling → dpr 1.25,
#    což by nesedělo s reálným dpr 1.0 = tell.
# 3) Okno musí fyzicky sednout na reálný monitor (PHYS) A být <= claimovaného
#    screen.avail (jinak innerWidth > screen.width = nemožné = tell). Řeší
#    pick_window() + launcher --window-size.
# Rozdělení ~ reálný desktop share (dpr~1). Váhy jsou celočíselné (sčítají ~100).
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
TASKBAR_H = 40                 # Windows spodní taskbar → avail_height = height-40

# hardwareConcurrency: claim MUSÍ být <= reálnému počtu jader hostitele (claim výš =
# VM-ish tell, paměť profile-04-elevated-tampering: 24 = VM). Distribuce níže je
# populační tvar; host_hwconc_dist() ji ořízne stropem konkrétního stroje.
HWCONC_DIST = [(8, 40), (16, 25), (12, 15), (6, 10), (4, 10)]
# deviceMemory: spec buckety, strop 8 — i když host hlásí víc, >8 je out-of-spec
# tampering tell (C++ GetDeviceMemory to navíc clampuje). host_devmem_dist() ořízne
# na min(host, 8).
DEVMEM_DIST = [(8, 65), (4, 35)]
DEVMEM_SPEC_MAX = 8


def host_color_depth():
    """screen.colorDepth REÁLNÉHO hosta (hardware+verze vázané, pin). Bylo natvrdo 32;
    hodnota ale není vlastností stroje, nýbrž Chromium VERZE (149 hlásí syrový
    BITSPIXEL 32, od 150 upstream vrací 24 — CHANGELOG 2026-07-24). Čtení z probu tedy
    řeší i rebase-note: probe měří nemaskovanou hodnotu na TOMHLE buildu."""
    s = host_require("screen.colorDepth").get("screen") or {}
    d = s.get("color_depth") or s.get("colorDepth")
    if not d:
        raise SystemExit(f"screen.colorDepth: probe neobsahuje hloubku.\n  {NO_PROBE_HINT}")
    return int(d)


def host_dpr():
    """Reálný devicePixelRatio hosta, tj. škálování Windows (100 % = 1.0, 150 % = 1.5).
    NENÍ spoofovatelný (C++ accessor neexistuje) a celý pool RESOLUTIONS platí jen pro
    dpr==1.0 — na HiDPI hostu ho proto srovná launcher, viz hidpi_scale_flags()."""
    s = host_require("devicePixelRatio").get("screen") or {}
    dpr = s.get("device_pixel_ratio", s.get("devicePixelRatio"))
    if not dpr:
        raise SystemExit(f"devicePixelRatio: probe neobsahuje dpr.\n  {NO_PROBE_HINT}")
    return float(dpr)


def hidpi_scale_flags(quiet=True):
    """Flagy, kterými se HiDPI host srovná na dpr 1.0. Prázdný list = není potřeba.

    ZMĚŘENO 2026-07-26 (gate 1-5, Release, bez proxy, škálování Windows 150 %):
      - bez zásahu (stránka vidí dpr 1.5 vedle maskovaného screen) avg tampering
        0.0920, tedy BEZ postihu — původní tvrdé odmítnutí HiDPI nebylo podložené;
      - s `--force-device-scale-factor=1` se dpr vrátí na 1, screen sedí na claim
        a tampering je bit po bitu shodný s během při 100 % (0.3849/0.1742/0.0094/
        0.0049/0.0153).
    Volí se ta druhá varianta: je to přesně referenční stav, takže platí celý pool
    RESOLUTIONS i všechna dosavadní měření. Cena je kosmetická — UI prohlížeče je na
    150% displeji malé.

    Defenzivní: bez probu vrací [] (launcher probe teprve umí spustit)."""
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
    """(šířka, použitelná výška) REÁLNÉHO monitoru ve FYZICKÝCH pixelech — okno musí
    fyzicky sednout. Z probu (screen.avail*, tedy už bez taskbaru); dřív natvrdo
    1920×1040, což nesedělo ani na tenhle host (1680×1050).

    Probe měří v CSS pixelech při aktuálním škálování, takže na HiDPI hostu vrací
    avail/dpr (při 150 %: 1120×660). Prohlížeč ale poběží s vynuceným scale factorem 1
    (hidpi_scale_flags), kde je použitelná plocha zase fyzická → násobíme zpět dpr.
    Při dpr==1.0 je to no-op, tedy pro běžný host beze změny."""
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
    """Ořízne distribuci na hodnoty <= cap (claim nikdy nad realitu hosta). Kdyby
    ořez vyprázdnil pool (slabý stroj), nechá jen nejnižší bucket."""
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
    """HWCONC_DIST oříznutá na reálný počet logických jader hostitele."""
    hw = host_require("hardwareConcurrency").get("hardware") or {}
    cap = hw.get("hardware_concurrency")
    if not cap:
        raise SystemExit(f"hardwareConcurrency: the probe contains no CPU count.\n  {NO_PROBE_HINT}")
    return _clamp_dist(HWCONC_DIST, int(cap), "hardware_concurrency")


def host_devmem_dist():
    """DEVMEM_DIST oříznutá na min(host, spec strop 8)."""
    hw = host_require("deviceMemory").get("hardware") or {}
    cap = hw.get("device_memory") or DEVMEM_SPEC_MAX
    return _clamp_dist(DEVMEM_DIST, min(int(cap), DEVMEM_SPEC_MAX), "device_memory")

# prefers-color-scheme = ČISTÁ per-profil osa diverzity (A1). Žádný hardware
# cross-check (je to uživatelská/OS preference, nekoheruje s GPU/canvas/screen) a
# NEkonzumuje font rozpočet → ortogonální ke všem stávajícím osám. Reálná populace
# je rozdělená zhruba na půl (adopce dark módu ~40-50 %). Override teče přes
# WebPreferences → Settings → StyleEngine, takže media dotaz `prefers-color-scheme`
# I rendering/system colors jsou koherentní (ne jen matchMedia = žádný mismatch tell).
COLOR_SCHEME_DIST = [("light", 55), ("dark", 45)]


def _seed_int(seed, key):
    """32bit deterministická hodnota z (seed, key) — JEDINÝ zdroj náhody pro všechny
    osy. Nahradil dřívější krájení profile_id na slice ([0:8], [8:16], ...): to bylo
    vyčerpané (62 ze 64 hex znaků) a každá nová osa by musela hledat volné místo.
    Salted hash je nezávislý napříč klíči, ortogonální bez ruční správy rozsahů a
    škáluje na libovolný počet os. Stabilní per-profil (pravidlo 8) — stejný seed
    a klíč dá vždy stejnou hodnotu.

    Konvence klíčů: "domena" nebo "domena:polozka" (např. "hw:conc", "font:mask",
    "speech:de"). Klíč je součást otisku profilu — jeho PŘEJMENOVÁNÍ přehodí danou
    osu u všech profilů, takže se klíče nemění bez důvodu."""
    h = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _seed_frac(seed, key):
    """Rovnoměrné 0..1 z (seed, key) — pro prevalenční prahy ("má tenhle profil ten
    balíček nainstalovaný?"). Viz _seed_int."""
    return _seed_int(seed, key) / float(1 << 32)


def _weighted_pick(value_int, dist):
    """Deterministický vážený výběr: dist=[(item,weight),...]. value_int ze seedu
    (dnes vždy _seed_int → 0..2^32-1; modulo bias je při té šířce zanedbatelný)."""
    total = sum(w for _, w in dist)
    r = value_int % total
    cum = 0
    for item, w in dist:
        cum += w
        if r < cum:
            return item
    return dist[-1][0]


def bump_ua_to_build(ua: str) -> str:
    """Přepíše Chrome verzi v UA stringu na CHROME_VERSION (pravidlo 8)."""
    # UA nese formu Chrome/143.0.0.0 -> Chrome/149.0.0.0 (minor/build/patch 0 dle Chrome konvence v UA)
    return re.sub(r"Chrome/\d+\.\d+\.\d+\.\d+", f"Chrome/{CHROME_MAJOR}.0.0.0", ua)


def pick_chrome_version(seed):
    """Per-profil Chrome patch verze (klíč "ua:patch", viz _seed_int).
    Major.minor.build zůstává == build (pravidlo 8), liší se jen patch."""
    return _weighted_pick(_seed_int(seed, "ua:patch"), CHROME_PATCH_DIST)


def build_ua_ch(full_version):
    """UA Client Hints. Jediné pole, které C++ z `ua` reálně čte, jsou `brands`
    (GetExtraBrand → dopíše branded "Google Chrome" k Chromium buildu) a `full_version`
    (GetChromeFullVersion → fullVersionList/uaFullVersion). Zbytek je DEKLARATIVNÍ:

      user_agent, platform_version, architecture, bitness, wow64, model
        → no shipped Python consumer reads these fields. The UA string and
          platformVersion generuje Chromium nativně z hostu. Držíme je tu jen jako
          dokumentaci očekávaného stavu; ZMĚNA TĚCHTO POLÍ NEMÁ ŽÁDNÝ ÚČINEK.

    platform_version se vědomě NESPOOFUJE: CreepJS getWindows() odvozuje verzi Windows
    z FONTŮ (Segoe Fluent Icons = Win11) a fonty umíme jen skrývat, ne přidávat — claim
    Win11 na Win10 hostu by byl přímý rozpor platformVersion vs fonty."""
    return {
        "platform": "Windows",
        "mobile": False,
        # low-entropy brands: autoritativní zdroj je nativní GREASE algoritmus Chrome 149.
        "brands": [
            {"brand": "Chromium", "version": CHROME_MAJOR},
            {"brand": "Google Chrome", "version": CHROME_MAJOR},
        ],
        # high-entropy (getHighEntropyValues / Accept-CH) — per-profil patch:
        "full_version": full_version,
        "full_version_list": [
            {"brand": "Chromium", "version": full_version},
            {"brand": "Google Chrome", "version": full_version},
        ],
        "platform_version": "10.0.0",   # deklarativní; reálně z hostu (Windows 10)
        "architecture": "x86",
        "bitness": "64",
        "wow64": False,
        "model": "",
    }


def pick_hwconc(seed):
    """Per-profil hardwareConcurrency z vážené distribuce (klíč "hw:conc").
    Distribuce je oříznutá na reálný počet jader hosta (claim méně netripuje — gate
    ověřeno; claim víc = VM tell)."""
    return _weighted_pick(_seed_int(seed, "hw:conc"), host_hwconc_dist())


def pick_devmem(seed):
    """Per-profil deviceMemory {4,8} z vážené distribuce (klíč "hw:mem"),
    oříznuté na min(host, spec strop 8)."""
    return _weighted_pick(_seed_int(seed, "hw:mem"), host_devmem_dist())


def pick_webgl_renderer(seed):
    """Per-profil marketingový název GPU (name-rotace), klíč "gpu".
    Rotuje JEN NÁZEV; device ID 0x67DF (=pixel-hash, WebGPU amd/gcn-4) i vendor (AMD)
    zůstávají reálný host → koherentní. Ověřeno gate. Viz GPU_RENDERER_DIST."""
    return _weighted_pick(_seed_int(seed, "gpu"), gpu_renderer_dist())


def pick_color_scheme(seed):
    """Per-profil prefers-color-scheme dark|light (klíč "media:colorscheme")."""
    return _weighted_pick(_seed_int(seed, "media:colorscheme"), COLOR_SCHEME_DIST)


def pick_media_devices(seed):
    """Per-profil cap na počet media zařízení (A2, hide-only). Cap jen SNIŽUJE reálný
    počet zařízení daného druhu (C++ nikdy nefabrikuje) → 'profil tu periferii nemá'
    = on-manifold (reálné stroje se liší webkamerou/mikrofonem). Rozbíjí same-host
    enumerateDevices linker (jinak všechny profily na 1 stroji = identický seznam).
    Absentní klíč = bez capu (nativní počet). Klíče "media:videoin" / "media:audioin"
    / "media:audioout"."""
    devices = {}
    # webkamera: desktopy ji často nemají → část profilů ji skryje (cap 0)
    if _weighted_pick(_seed_int(seed, "media:videoin"), [(1, 55), (0, 45)]) == 0:
        devices["max_video_input"] = 0
    # mikrofon: ~ půl na půl
    if _weighted_pick(_seed_int(seed, "media:audioin"), [(1, 50), (0, 50)]) == 0:
        devices["max_audio_input"] = 0
    # audiovýstup: capuje se na 1 ("stroj má jen reproduktory"), NIKDY na 0 —
    # nula zvukových výstupů je VM tell, proto se tenhle druh dřív necapoval vůbec.
    # Cap 1 je on-manifold: reálné stroje mají 1 (repro) nebo 2+ (repro + headset).
    # Na hostu s jediným výstupem je to no-op (C++ cap jen snižuje, nefabrikuje).
    if _weighted_pick(_seed_int(seed, "media:audioout"), [(0, 60), (1, 40)]) == 1:
        devices["max_audio_output"] = 1
    return devices


def pick_screen(seed):
    """Per-profil rozlišení z RESOLUTIONS (klíč "screen:res"). Vrací dict se
    screen poli + koherentním oknem (viz mantinely u RESOLUTIONS)."""
    (w, h) = _weighted_pick(_seed_int(seed, "screen:res"), RESOLUTIONS)
    avail_w, avail_h = w, h - TASKBAR_H
    win_w, win_h = pick_window(seed, avail_w, avail_h)
    depth = host_color_depth()
    return {
        "width": w, "height": h,
        "avail_width": avail_w, "avail_height": avail_h,
        "color_depth": depth, "pixel_depth": depth,
        "device_pixel_ratio": host_dpr(),
        # Outer velikost okna — launcher ji předá jako --window-size. Garantuje
        # okno <= claimovaného screen.avail A <= reálného monitoru → innerWidth
        # nikdy nepřeteče screen.width (jinak nemožné = tell).
        "window_width": win_w, "window_height": win_h,
    }


def pick_window(seed, avail_w, avail_h):
    """Koherentní outer velikost okna: <= claimovaný avail A <= reálný monitor
    (physical monitor hosta z probu). Mírný per-profil jitter dolů (klíče
    "screen:winw"/"screen:winh") přidá outerWidth entropii a napodobí
    ne-maximalizované okno."""
    phys_w, phys_avail_h = host_physical()
    max_w = min(avail_w, phys_w)
    max_h = min(avail_h, phys_avail_h)
    jw = _seed_int(seed, "screen:winw") % 120
    jh = _seed_int(seed, "screen:winh") % 120
    win_w = max(1024, max_w - jw)
    win_h = max(640, max_h - jh)
    return win_w, win_h


# Text-rendering edging = PRVNÍ čistá per-profil diverzita otisku na jednom stroji
# (paměť text-rendering-edging-clean-lever). AA mód protéká nativně do rasteru glyfů
# → mění text/canvas hash A netripuje (grayscale/alias jsou koherentní reálné Windows
# configy, tampering dokonce klesá). Ověřeno hash probe + detekční gate.
TEXT_EDGING_CYCLE = ["lcd", "grayscale", "alias"]


def pick_text_edging(seed):
    """Deterministicky ze seedu (stabilní per-profil, různé napříč profily). ~3
    buckety (kolize při >3 profilech nevadí — pořád reálná osa). subpixel/hinting
    se NEmění: hinting je na Windows no-op (DWrite ignoruje), subpixel-off při LCD
    je nekoherentní = tampering 0.98. Měníme jen edging (překlápí AA koherentně).
    Klíč "text:edging"."""
    return TEXT_EDGING_CYCLE[_seed_int(seed, "text:edging") % len(TEXT_EDGING_CYCLE)]


# Font hiding = DRUHÁ čistá per-profil osa (ortogonální k edgingu). Font
# enumeration (measureText/src:local) byl PŘED tím identický u všech profilů =
# dokonalý cross-profile linker (probe: fontlist_hash 1/5). Řešení: každý profil
# "nemá" koherentní podmnožinu OPTIONAL fontů. Skrýváme celé jazykové balíčky
# (East-Asian + supplemental skripty), které Windows instaluje on-demand přes
# language features — na reálném cs-CZ stroji bez těch packů legitimně chybí, tedy
# on-manifold (jiný, ale možný stroj). NIKDY neskrýváme core Latin/UI fonty (jejich
# absence = tell). Skrytí neexistujícího fontu = no-op → list degraduje graceful
# na jiných verzích Windows i na Linuxu (tam prostě nic neskryje). C++ FontCache
# vrací pro skrytou rodinu nullopt → fallback jako u reálně chybějícího fontu.
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


# --- Font inventář HOSTA (přenositelnost) ---------------------------------------
# Skrýt neexistující font = no-op. Na font-chudém stroji se tedy osa TIŠE smrskne
# (profily zkolabují na stejný font set = ztráta diverzity), a hůř: rozpočtový guard
# počítaný proti statickému FP_PROBED_FONTS by nadhodnocoval, kolik rodin reálně
# zbyde → implauzibilní řídkost = tampering 0.94 (paměť font-hiding-magnitude-ceiling).
# Proto se všechno počítá proti REÁLNÉMU inventáři z probu.


def host_font_set():
    """set(lowercase) rodin, které probe na hostu NAMĚŘIL. Prázdné/chybějící pole =
    None → volající se chová jako dřív (statické seznamy)."""
    fonts = (host_info() or {}).get("fonts") or {}
    present = fonts.get("present")
    if not present:
        return None
    # Když se od posledního probu změnily font seznamy v tomhle souboru, nové rodiny
    # nebyly změřené a tvářily by se jako nepřítomné (= balíček se tiše nepoužije).
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
    """Rodiny z FP.com probe listu, které host reálně má (= náš skutečný rozpočet)."""
    hs = host_font_set()
    if hs is None:
        return list(FP_PROBED_FONTS)
    return [f for f in FP_PROBED_FONTS if f.lower() in hs]


def font_hiding_enabled():
    """Font-hiding se na font-CHUDÉM stroji VYPÍNÁ. Když už samotný host nese míň než
    MIN_FP_FONTS_VISIBLE rodin z probe listu, každé další skrytí jde pod červenou
    čáru — a raději žádná diverzita než implauzibilní řídkost. Není to chyba profilu,
    takže se negeneruje výjimka, ale hlasité varování (vědomý, ne tichý stav)."""
    have = len(host_fp_probed_present())
    if have < MIN_FP_FONTS_VISIBLE:
        _warn_once("font_poor",
                   f"WARNING: this computer has a sparse font inventory ({have} FP.com "
                   f"families < {MIN_FP_FONTS_VISIBLE}); font hiding is disabled for all profiles.")
        return False
    return True


def _bundle_available(fonts):
    """Má host aspoň jednu rodinu z balíčku? Skrývat balíček, který stroj nemá, je
    no-op — jen by zbytečně lhal v profilu a snižoval reálnou diverzitu osy."""
    hs = host_font_set()
    if hs is None:
        return True
    return any(f.lower() in hs for f in fonts)


def pick_hidden_fonts(seed):
    """Deterministická per-profil podmnožina OPTIONAL balíčků (viz FONT_BUNDLES) ke
    skrytí. Celé balíčky (koherence: jazyk buď máš, nebo ne). Maska z klíče
    "font:mask". BEZ forced de-kolize: kolize font-setů
    jsou on-manifold (reálná populace je sdílí) a škáluje na libovolné N; u stovek
    profilů nelze zaručit distinct, ani to není žádoucí (byl by to nereálně rovný
    rozptyl). Seed dává přirozený, sticky per-profil výběr.

    Balíčky, které host nemá, se přeskakují — ale POZICE BITU v masce zůstává, takže
    na stroji s plným inventářem je výsledek identický jako dřív (žádný drift)."""
    if not font_hiding_enabled():
        return []
    n = len(FONT_BUNDLES)
    mask = _seed_int(seed, "font:mask") % (1 << n)
    hidden = []
    for i, (_, fonts) in enumerate(FONT_BUNDLES):
        if mask & (1 << i) and _bundle_available(fonts):
            hidden.extend(fonts)
    return hidden


# --- Rozšířený font-pool (KONZERVATIVNÍ fáze) -----------------------------------
# Vedle CJK/regionálních balíčků (FONT_BUNDLES výše) skrýváme i NE-jazykové OPTIONAL
# fonty, kterými se reálné Windows stroje běžně liší podle NAINSTALOVANÉHO SOFTWARE
# (Office symboly, CorelDRAW/Bitstream pack, HP ovladače, dev nástroje, aplikační
# webfonty, Segoe handwriting). Absence celého balíčku = "ten software nemám" =
# on-manifold jiný, ale reálný stroj. Rodiny jsou vybrané z REÁLNĚ nainstalovaných
# rodin tohoto hostu (registry, 402 rodin) — skrytí neexistující rodiny je no-op,
# takže list degraduje graceful na jiných strojích i verzích Windows. NIKDY se
# neskrývá core/web-safe font (viz CORE_NEVER_HIDE + guard). Každý balíček má vlastní
# hide_prob = reálná prevalence ABSENCE toho software (těžkochvostá populace, NE
# uniformní mřížka — ta by byla sama tell). Velký "office_supplemental" balíček
# (~60 rodin, největší swing) je zatím ODLOŽEN — přidá se až po ověření, že gate
# nedriftuje (rozhodnutí uživatele: konzervativně nejdřív).
OPTIONAL_FONT_BUNDLES = [
    # (klíč, [CSS family jména], hide_prob = P(profil ten software "nemá"))
    # POZN. k prevalencím: volíme je tak, aby balíček populaci REÁLNĚ štěpil.
    # Prevalence blízko 0 nebo 1 = balíček je konstanta, ne proměnná → skoro nulová
    # entropie (Bernoulli H(0.85)=0.61 bit vs H(0.5)=1.0). Kde nemáme silný realistický
    # prior, držíme se blíž 0.5.
    ("office_symbols", [
        "MS Outlook", "MS Reference Specialty", "MS Reference Sans Serif",
        "MT Extra", "Bookshelf Symbol 7",
    ], 0.35),
    # Bitstream (BT) a Letraset (LET) = RŮZNÍ výrobci; CorelDRAW je bundloval spolu,
    # ale reálně mohou přijít odděleně → dva nezávislé balíčky = 2 bity místo 1.
    # BT obsahuje FP-probovaný "Staccato222 BT" (čerpá rozpočet, viz FP_PROBED_FONTS);
    # LET NEobsahuje žádný FP-probovaný font → entropie ZDARMA (hýbe font_hash a
    # CreepJS enumeration, ale nesahá na FP.com `fonts` pole).
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
    # Cascadia se dodává s Windows Terminalem (default na Win11, běžný na Win10) →
    # 0.80 bylo nerealisticky vysoko I entropicky slabé; 0.55 štěpí a sedí líp.
    ("dev_tools", [
        "Cascadia Code", "Cascadia Mono",
    ], 0.55),
    ("webapp_fonts", [
        "Lato", "Open Sans", "Open Sans Light", "Open Sans Semibold",
        "Open Sans Extrabold", "Charis SIL",
    ], 0.55),
    # ODSTRANĚN "segoe_handwriting" (Segoe Print/Script): jsou to Windows 10 DEFAULT
    # fonty — jejich absence není "nemám software", ale okleštěný Windows = tell.
    # Gate ho navíc nikdy neexerciroval (0/5 profilů ho skrývalo) → neověřené riziko.
]

# Core / web-safe / systémové rodiny — NIKDY neskrývat (absence = tell nebo rozbití
# fallbacku). Guard (_assert_no_core_in_bundles) tvrdě ověří, že je žádný balíček
# neobsahuje. POZN.: rodiny záměrně skrývané CJK balíčky (MS UI Gothic, SimSun,
# Nirmala UI, Leelawadee UI, …) zde NEJSOU — ty jsou legitimně optional na cs-CZ.
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


# --- FP-probe rozpočet (zakódovaná lekce z neúspěšného `office` balíčku) ---------
# fingerprint.com neprobuje "všechny fonty", ale FIXNÍ seznam; na tomto hostu z něj
# detekuje těchto 17 rodin podle auditovaného referenčního měření.
# Do `fonts` pole se promítne JEN tento průnik → to je náš rozpočet.
# EMPIRICKY OVĚŘENO (2026-07-16, rollback): skrytí velké Office kolekce srazilo pole
# na 4 rodiny → tampering 0.9430 / 0.7386 (fakticky odhaleno). Tell NENÍ které rodiny
# skrýváme, ale kolik jich ZBYDE — Windows stroj se 4 fonty z probe-listu neexistuje.
# Konzervativní pool zbývá 12-16 = ověřeně čisté (tampering 0.0706). Proto tvrdý floor.
FP_PROBED_FONTS = [
    "Agency FB", "Calibri", "Century", "Century Gothic", "Franklin Gothic",
    "Haettenschweiler", "Lucida Bright", "Lucida Sans", "MS Outlook",
    "MS Reference Specialty", "MS UI Gothic", "MT Extra", "Marlett",
    "Monotype Corsiva", "Pristina", "Segoe UI Light", "Staccato222 BT",
]
MIN_FP_FONTS_VISIBLE = 12   # červená čára: pod tímhle začíná detekce (ověřeno)


def _assert_fp_font_budget(hidden, idx):
    """Pojistka proti opakování `office` chyby: po skrytí musí ve FP.com `fonts` poli
    zbýt >= MIN_FP_FONTS_VISIBLE rodin. Chrání před implauzibilně řídkým font setem
    (= hlavní tampering tell font osy). Selže při generování, ne až v gate.

    Počítá se proti REÁLNÉMU inventáři hosta (host_fp_probed_present), ne proti
    statické konstantě — na stroji, kde část těch rodin není nainstalovaná, byl by
    statický výpočet nadhodnocený a rozpočet by se tiše prorazil. Když je host sám
    pod čarou, font-hiding je vypnutý (font_hiding_enabled) a guard se nespouští —
    za inventář stroje profil nemůže."""
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


# --- CreepJS marker fonty (getWindows()) -----------------------------------------
# CreepJS neodvozuje verzi Windows z UA, ale z FONTŮ (src/fonts/index.ts):
#   fontVersion = { '11': map['11'].find(x => fonts.includes(x)), ... }
#   hash '10,7,8,8.1' -> "Windows 10"
# Skupiny 11/10/8.1/8 používají .find() => stačí, aby ZBYL JEDEN font ze skupiny.
# Skupina '7' používá .filter().length == len => musí zbýt VŠECHNY.
# Když hash nesedí na žádný klíč hashMapy, getWindows() vrátí undefined = OS verze
# nedetekovatelná z fontů, což je pro Windows stroj anomálie.
# Dnes to drží jen NÁHODOU (CORE_NEVER_HIDE shodou okolností kryje aspoň jeden font
# v každé skupině) — tenhle guard z toho dělá vynucenou invariantu.
CREEPJS_WIN_FONT_MARKERS = {
    "11": ["Segoe Fluent Icons"],
    "10": ["HoloLens MDL2 Assets", "Segoe MDL2 Assets", "Bahnschrift", "Ink Free"],
    "8.1": ["Leelawadee UI", "Javanese Text", "Segoe UI Emoji"],
    "8": ["Aldhabi", "Gadugi", "Myanmar Text", "Nirmala UI"],
    "7": ["Cambria Math", "Lucida Console"],
}
# Hlídat se dají jen skupiny, které host REÁLNĚ má: na Win10 se '11' nedetekuje
# (Segoe Fluent Icons chybí) a přidat font neumíme; na Win11 stroji je to naopak.
# Konstanty níže jsou fallback pro případ, že probe nenese font inventář — jinak se
# skupiny odvozují z hosta (creepjs_win_groups()).
CREEPJS_WIN_GROUPS_ANY = ("10", "8.1", "8")
CREEPJS_WIN_GROUPS_ALL = ("7",)


def creepjs_win_groups():
    """(any_groups, all_groups) podle toho, co host reálně má. `any` skupina se hlídá,
    jen když z ní host aspoň jednu rodinu má; `all` skupina jen když má všechny —
    jinak by guard vynucoval detekovatelnost něčeho, co ani na nemaskovaném stroji
    detekovat nejde."""
    markers = ((host_info() or {}).get("fonts") or {}).get("creepjs_markers")
    if not markers:
        return CREEPJS_WIN_GROUPS_ANY, CREEPJS_WIN_GROUPS_ALL
    any_g = tuple(g for g in ("11", "10", "8.1", "8") if markers.get(g))
    all_g = tuple(g for g in ("7",)
                  if len(markers.get(g) or []) == len(CREEPJS_WIN_FONT_MARKERS[g]))
    return any_g, all_g


def _assert_creepjs_win_markers(hidden, idx):
    """Každá CreepJS marker skupina musí zůstat detekovatelná, jinak getWindows()
    vrátí undefined a Windows stroj bez zjistitelné verze OS je tell."""
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
    """Deterministicky per-profil A per-balíček: je tento balíček u tohoto profilu
    'nenainstalovaný' (skrytý)? Práh = hide_prob (prevalence absence software).
    Viz _seed_frac."""
    return _seed_frac(seed, f"font:{key}") < hide_prob


def pick_optional_hidden(seed):
    """Per-profil skryté OPTIONAL (ne-jazykové) rodiny z OPTIONAL_FONT_BUNDLES dle
    prevalence. Ortogonální k CJK balíčkům (pick_hidden_fonts) i edgingu.
    Balíčky, které host nemá, se přeskakují (skrytí by bylo no-op)."""
    if not font_hiding_enabled():
        return []
    hidden = []
    for key, fonts, hide_prob in OPTIONAL_FONT_BUNDLES:
        if _bundle_is_hidden(seed, key, hide_prob) and _bundle_available(fonts):
            hidden.extend(fonts)
    return hidden


# ── Speech voices = osa D (hide-only, stejná mechanika jako fonty) ────────────
# speechSynthesis.getVoices() vrací na Windows SAPI hlasy hostitele — bez maskování
# je identický u všech profilů na jednom stroji = cross-profile linker. Skrýváme
# per-profil koherentní podmnožinu ("tenhle stroj nemá nainstalovaný ten jazykový
# balíček hlasů") — na reálném Windows je to běžný stav, tedy on-manifold.
#
# POZOR na dvě věci, které tuhle osu odlišují od fontů:
#  1) MATCHOVÁNÍ JE PODŘETĚZCOVÉ, ne přesné. Chrome hlásí jméno včetně locale
#     přípony — "Microsoft David - English (United States)" — kdežto konfigurace
#     nese jen stabilní prefix "Microsoft David". Přesná shoda jako u fontů by
#     nikdy nesedla → C++ IsVoiceHidden dělá case-insensitive `contains`.
#  2) ENUMEROVAT HLASY HOSTA MUSÍŠ PŘES CHROME, ne přes SAPI5. Dotaz přes .NET
#     System.Speech vrací na tomhle stroji jen 2 hlasy (David/Zira "Desktop"),
#     ale Chrome jich hlásí 4 — bere i OneCore hlasy (klíč Speech_OneCore), které
#     klasické SAPI5 nevidí: Microsoft Jakub (cs-CZ), David, Mark, Zira (en-US).
#     ZMĚŘENO na out/Dev, ne odhad. Kdo se spolehne na System.Speech, podcení
#     rozsah osy a hlavně přehlédne, že český hlas TU JE (a je default) — proto
#     je locale guard v pick_hidden_voices reálná ochrana, ne no-op.
# Neexistující hlas skrýt = no-op, stejně jako u fontů → seznam degraduje
# graceful napříč stroji.
SPEECH_VOICE_BUNDLES = [
    # en-US Desktop hlasy jsou samostatné instalace → hideable jednotlivě.
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

# Jazyk → klíč balíčku, kvůli locale koherenci (viz pick_hidden_voices).
_VOICE_LANG_TO_BUNDLE = {
    "cs": ("cs",), "de": ("de",), "fr": ("fr",), "es": ("es",), "it": ("it",),
    "ru": ("ru",), "pl": ("pl",), "ja": ("ja",), "zh": ("zh",), "ko": ("ko",),
    "pt": ("pt",),
    "en": ("en-us-david", "en-us-zira", "en-us-mark", "en-gb"),
}


def pick_hidden_voices(seed, languages):
    """Per-profil skryté hlasy dle prevalence (klíč "speech:<balíček>").

    GUARD 1 (locale koherence): balíček jazyka, který profil claimuje v
    locale.languages, se NIKDY neskrývá — profil s cs-CZ, který nemá český hlas,
    zatímco stroj ho má, je nekoherence. (Na hostu bez toho hlasu je to stejně
    no-op.) U angličtiny se chrání jen tak, aby nezmizely VŠECHNY en balíčky.

    GUARD 2 (nikdy neskrýt vše) je AUTORITATIVNĚ v C++ na call-site — jen tam je
    znám skutečný seznam hlasů hostitele. Tady ho nelze spolehlivě vynutit,
    protože generátor neví, které hlasy cílový stroj má. Viz speech_synthesis_impl.cc."""
    protected = set()
    for lang in languages or []:
        primary = lang.split("-")[0].lower()
        keys = _VOICE_LANG_TO_BUNDLE.get(primary, ())
        if primary == "en":
            # u angličtiny chráníme jen jeden balíček (aby aspoň jeden en hlas zbyl),
            # jinak by en profily neměly na téhle ose žádnou diverzitu
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
    """Pojistka (pravidlo 8): žádný skrývatelný balíček nesmí obsahovat core/web-safe
    rodinu. Selže hlasitě při generování, ne až za běhu prohlížeče."""
    all_bundles = [(k, f) for (k, f) in FONT_BUNDLES] + \
                  [(k, f) for (k, f, _p) in OPTIONAL_FONT_BUNDLES]
    for key, fonts in all_bundles:
        for fam in fonts:
            if fam.lower() in CORE_NEVER_HIDE:
                raise AssertionError(
                    f"CORE font '{fam}' is in bundle '{key}' — never hide it")


def random_seed():
    """Nový náhodný 256-bit hex seed (profile_id) pro ČERSTVÝ profil. Všechny
    per-profil osy z něj vycházejí DETERMINISTICKY (pravidlo 8) — náhodný je jen
    samotný seed, ne per-session šum. 64 hex znaků == stejný formát jako
    base_profile_id v profiles.json (seed-slice jdou do [58:62])."""
    return secrets.token_hex(32)


def make_profile(ref, idx, seed=None, gpu_mode=None):
    """seed=None → použij base_profile_id z reference (deterministický, sticky
    profil jako doteď). seed=<hex> → čerstvý náhodný profil (launcher bez čísla).
    gpu_mode: off|family|common|all (None = default_gpu_mode()). Karta se vybírá
    DETERMINISTICKY ze seedu, takže je per-profil stabilní a napříč profily různá."""
    nav = ref["browser_data"]["navigator"]
    if seed is None:
        seed = ref["base_profile_id"]
    label = f"profile_{idx:02d}"
    full_version = pick_chrome_version(seed)
    # Per-profil GPU template dle režimu (FP_GPU_TEMPLATE ji přebíjí na jednu kartu).
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
            # Per-profil soft osy (seed-driven distribuce). hwConc <= reálných 16,
            # deviceMemory <= spec cap 8 (reálná RAM 20GB stejně hlásí 8).
            "hardware_concurrency": pick_hwconc(seed),
            "device_memory": pick_devmem(seed),
        },
        "locale": {
            "languages": DEFAULT_LOCALE["languages"],
            "accept_language": DEFAULT_LOCALE["accept_language"],
        },
        "timezone": DEFAULT_LOCALE["timezone"],
        # Screen: per-profil rozlišení + koherentní okno (viz pick_screen). dpr a
        # color_depth pinnuté na reálný host.
        "screen": pick_screen(seed),
        # prefers-color-scheme (A1): čistá per-profil osa dark|light. C++ override
        # v prohlížečovém procesu (WebPreferences::preferred_color_scheme) → media
        # dotaz i StyleEngine rendering koherentní. Viz pick_color_scheme.
        "media": {
            "color_scheme": pick_color_scheme(seed),
            # media zařízení (A2): per-profil cap (hide-only) na webkameru/mikrofon.
            # C++ jen snižuje reálný počet, nikdy nefabrikuje. Viz pick_media_devices.
            "devices": pick_media_devices(seed),
        },
        # GPU (režim --gpu, viz gpu_pool/pick_gpu_template):
        #  - off:    name-rotace v rámci reálné host device ID (jen na AMD gcn-4 hostu,
        #            jinak host renderer beze změny) → koherentní, žádný gpu blok.
        #  - jinak:  top-level webgl.* se drží HOST (bezpečný fallback pro případ, že
        #            C++ gpu blok neprojde validací) a per-profil identita jde do
        #            atomického "gpu" bloku níže (WebGL+WebGPU spolu, ParseGpuBlock).
        "webgl": {
            "vendor": _host_gl["vendor"],
            "renderer": (_host_gl["renderer"] if _gpu_block
                         else pick_webgl_renderer(seed)),
        },
        # Canvas/audio noise: per-profil unikátnost přes deterministický šum
        # (seed z profile_id). DEFAULT VYPNUTO — naivní šum byl silně detekován
        # (viz paměť canvas-audio-noise-detected); zapíná se jen experimentálně
        # editací JSON (C++ čte za běhu, bez rebuildu). Konzistence napříč všemi
        # readback API (getImageData/toDataURL/toBlob; getChannelData/
        # copyFromChannel) je nutná, jinak = tampering tell.
        "canvas": {
            "noise": {
                "enabled": False,
                "mode": "variance_gamma",  # variance_gamma (hrany+gamma) | additive | micro (diagnostic 1px)
                "density_bits": 3,   # perturbuj 1 z 2^3 pixelů (jen additive)
                "magnitude": 1,      # ±1 na kanál (jen additive)
                "webgl": False,      # readPixels zatím nepokryt
            },
        },
        "audio": {
            "noise": {
                "enabled": False,
                "mantissa_bits": 3,  # přepiš spodní 3 mantisové bity
            },
            "reference_sum": ref["browser_data"].get("audio_fingerprint_sum"),
        },
        # Speech voices (osa D): per-profil "nenainstalované" balíčky SAPI hlasů.
        # Hide-only, podřetězcové matchování, guard proti vyprázdnění je v C++ na
        # call-site (jen tam je znám reálný seznam). Viz pick_hidden_voices.
        "speech": {
            "hidden_voices": pick_hidden_voices(seed, DEFAULT_LOCALE["languages"]),
        },
        # Text rendering: per-profil edging (viz pick_text_edging) — jediná ověřená
        # čistá osa canvas/text diverzity na jednom stroji. Mění NATIVNĚ rasterizaci
        # glyfů (Skia CreateSkFont na Windows) → jiný canvas+DOM text hash, bez
        # tamperingu. LIMIT: text-only (geometrie canvasu zůstává hardware-pinnutá).
        # C++ čte za běhu; edging=lcd == nativní na tomto ClearType-on hostu.
        "text": {
            "render": {
                "enabled": True,
                "edging": pick_text_edging(seed),  # lcd | grayscale | alias
                "hinting": "keep",   # no-op na Windows (DWrite ignoruje) — needit
                "subpixel": "keep",  # držet koherentní; off při LCD = tampering tell
            },
        },
        # Font hiding: per-profil koherentní podmnožina optional fontů "chybí"
        # (viz pick_hidden_fonts) — druhá čistá osa. Rozbije font-enumeration linker
        # a zároveň diverzifikuje canvas hash tam, kde text propadne fallbackem přes
        # skrytou rodinu. C++ FontCache čte za běhu; prázdný list = nativní.
        "fonts": {
            # CJK/regionální balíčky (pick_hidden_fonts) + rozšířený optional pool
            # (pick_optional_hidden: Office symboly, Corel/BT, HP, dev, webfonty,
            # Segoe handwriting). Obojí koherentní per-profil ze seedu.
            "hidden": pick_hidden_fonts(seed) + pick_optional_hidden(seed),
        },
        "proxy": None,   # doplní launcher (index + geo) pokud je proxy
        "user_data_dir": paths.profile_user_data_dir(label),
    }
    # Atomický cross-vendor GPU blok (jen když je aktivní šablona). C++ ParseGpuBlock
    # ho validuje celý-nebo-nic; při nevalidě padne na host webgl + nativní webgpu.
    if _gpu_block:
        prof["gpu"] = _gpu_block
    return prof


# --- Preflight (5.5): předpoklady o cílovém stroji ------------------------------
# Jedno místo, jedna hláška. Kontroluje se to, co maskování neumí zachránit — když
# je porušené, nemá smysl profily vůbec generovat.
WIN_MIN_BUILD = 17763          # Windows 10 1809 = minimum pro Chrome 149
_PREFLIGHT = {"done": False}


def preflight(strict=True):
    """Ověří, že tenhle stroj je podporovaná cílová konfigurace. Volá se jednou
    za běh (z build_profile). `FP_ALLOW_HOST=1` degraduje chyby na varování —
    jen pro diagnostiku, ne pro produkci."""
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

    # dpr != 1.0 (HiDPI) NENÍ důvod k odmítnutí — změřeno 2026-07-26, viz
    # hidpi_scale_flags(). Launcher takový host srovná na dpr 1.0 a otisk je pak
    # bit po bitu shodný s během při 100% škálování. Jen o tom řekneme nahlas.
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
    """Jednořádkový přehled toho, z čeho generátor odvozuje policy (co je host)."""
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
    """Sestaví JEDEN profil a prožene ho VŠEMI pojistkami (core fonty, FP.com font
    rozpočet, CreepJS getWindows markery). Vrací (prof, fp_visible). Jediný zdroj
    pravdy pro generování — používá ho main() (5 sticky profilů) i launcher.py
    (čerstvý profil bez čísla, se seed override). NEZAPISUJE na disk."""
    preflight()   # předpoklady o stroji (dpr, fallback adaptér, verze Windows)
    prof = make_profile(ref, idx, seed=seed, gpu_mode=gpu_mode)
    # Hlasy: aspoň jeden balíček musí zůstat viditelný. Dnes to drží z konstrukce
    # (locale-chráněné balíčky se nikdy neskrývají), ale kdyby někdo tu ochranu
    # rozvolnil, ať to spadne tady a ne až v prohlížeči s prázdným getVoices().
    _hidden_voices = prof["speech"]["hidden_voices"]
    _all_voices = [v for _, vs, _ in SPEECH_VOICE_BUNDLES for v in vs]
    assert len(_hidden_voices) < len(_all_voices), (
        f"profile {idx}: every voice bundle is hidden -> empty getVoices()")
    # belt-and-suspenders: žádný core font nesmí propadnout do hidden listu
    bad = [f for f in prof["fonts"]["hidden"] if f.lower() in CORE_NEVER_HIDE]
    assert not bad, f"profile {idx}: core font ve hidden listu: {bad}"
    # magnitude strop: ve FP.com fonts poli musí zbýt dost rodin (viz office rollback)
    fp_visible = _assert_fp_font_budget(prof["fonts"]["hidden"], idx)
    # CreepJS getWindows(): každá marker skupina musí zůstat detekovatelná
    _assert_creepjs_win_markers(prof["fonts"]["hidden"], idx)
    return prof, fp_visible


def _short_renderer(renderer):
    """Krátký název karty z ANGLE stringu — host-nezávisle (ne jen 'Radeon')."""
    m = re.search(r"ANGLE \([^,]+, (.+?) \(0x", renderer or "")
    if m:
        return m.group(1)
    return (renderer or "?")[:28]


def profile_summary(path, prof, fp_visible):
    """Jednořádkový přehled vygenerovaného profilu (sdílí main() i launcher)."""
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
    """--gpu {off|family|common|all} ze surového argv (sdílí launcher i generátor).
    Podporuje '--gpu X' i '--gpu=X'. Jediný zdroj pravdy pro GPU_MODES/default."""
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
    _assert_no_core_in_bundles()   # pojistka: žádný core font ve skrývatelných balíčcích
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
