#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Radek Cisar
# SPDX-License-Identifier: MPL-2.0

r"""
Measure hardware-bound properties of the current computer for portable profiles.

Browser3 must never reuse GPU, display, hardware, font, locale, or operating-system
claims measured on another computer. This probe starts the unmasked local Browser3
binary, collects native browser-visible properties, and stores a machine-bound cache
below `%LOCALAPPDATA%\Browser3\cache`.

The cache is invalidated when the machine identity or schema changes. Profile
generation stops when a valid probe is unavailable; there is no fabricated fallback.
The probe measures data only and contains no fingerprint masking logic.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import browser3_paths as paths

CACHE = paths.HOST_CACHE_FILE

# Verze schématu cache. Zvýšení = stará cache je neplatná a proboduje se znovu
# (v1 = jen GPU + screen, bez `version` klíče).
SCHEMA_VERSION = 2


def _launcher():
    """LAZY import launcheru (chrome_exe_path/parse_build_arg = jediný zdroj pravdy pro
    buildy). Nesmí být na úrovni modulu: launcher importuje generate_profiles a ten
    volá load_cache() už při importu → cyklický import by tiše vypnul detekci hosta."""
    import launcher as L
    return L


def _gp():
    """LAZY import generátoru (font seznamy = jediný zdroj pravdy). Stejná past jako
    u _launcher(): generate_profiles při importu čte cache tohohle modulu."""
    import generate_profiles as G
    return G


def font_candidates():
    """Kandidátní familiesy k proměření = sjednocení všech seznamů, se kterými generátor
    pracuje (FP.com probe list ∪ jazykové balíčky ∪ optional balíčky ∪ CreepJS markery).
    Když se seznamy v generátoru změní, je potřeba probe zopakovat (--force) — nová
    familiesa by jinak vypadala jako nepřítomná (bezpečný směr: balíček se nepoužije)."""
    G = _gp()
    fams = set(G.FP_PROBED_FONTS)
    for _key, fonts in G.FONT_BUNDLES:
        fams.update(fonts)
    for _key, fonts, _p in G.OPTIONAL_FONT_BUNDLES:
        fams.update(fonts)
    for fonts in G.CREEPJS_WIN_FONT_MARKERS.values():
        fams.update(fonts)
    # Core familiesy přidáváme taky — ne kvůli skrývání (to je zakázané), ale aby šlo
    # poznat „font-chudý stroj" (chybí i to, co má mít každý Windows).
    fams.update(["Arial", "Times New Roman", "Courier New", "Segoe UI", "Tahoma",
                 "Verdana", "Calibri", "Consolas"])
    return sorted(fams)


def font_candidates_hash(cands=None):
    """Otisk kandidátního seznamu — generátor podle něj pozná, že se font seznamy
    od posledního probu změnily (a že je tedy potřeba `--force`)."""
    cands = cands if cands is not None else font_candidates()
    return hashlib.sha256("\n".join(cands).encode("utf-8")).hexdigest()[:12]


PROBE_HTML_TMPL = """<!doctype html><meta charset="utf-8"><title>host probe</title>
<body>probing…<script>
const FONT_CANDIDATES = __FONT_CANDIDATES__;
function dumpGL(kind){
  const c=document.createElement('canvas'); const gl=c.getContext(kind);
  if(!gl) return {available:false};
  const dbg=gl.getExtension('WEBGL_debug_renderer_info');
  const P={}, names=['MAX_TEXTURE_SIZE','MAX_CUBE_MAP_TEXTURE_SIZE','MAX_RENDERBUFFER_SIZE',
    'MAX_VIEWPORT_DIMS','MAX_VERTEX_ATTRIBS','MAX_VERTEX_UNIFORM_VECTORS','MAX_VARYING_VECTORS',
    'MAX_FRAGMENT_UNIFORM_VECTORS','MAX_TEXTURE_IMAGE_UNITS','MAX_VERTEX_TEXTURE_IMAGE_UNITS',
    'MAX_COMBINED_TEXTURE_IMAGE_UNITS','ALIASED_LINE_WIDTH_RANGE','ALIASED_POINT_SIZE_RANGE',
    'RED_BITS','GREEN_BITS','BLUE_BITS','ALPHA_BITS','DEPTH_BITS','STENCIL_BITS',
    'MAX_3D_TEXTURE_SIZE','MAX_ARRAY_TEXTURE_LAYERS','MAX_DRAW_BUFFERS','MAX_COLOR_ATTACHMENTS',
    'MAX_SAMPLES','MAX_UNIFORM_BUFFER_BINDINGS','MAX_UNIFORM_BLOCK_SIZE'];
  for(const n of names){ if(gl[n]!==undefined){ try{ const v=gl.getParameter(gl[n]);
    P[n]=(v&&v.length!==undefined)?Array.from(v):v; }catch(e){} } }
  const an=gl.getExtension('EXT_texture_filter_anisotropic');
  if(an){ try{P.MAX_TEXTURE_MAX_ANISOTROPY_EXT=gl.getParameter(an.MAX_TEXTURE_MAX_ANISOTROPY_EXT);}catch(e){} }
  return {available:true,
    version:gl.getParameter(gl.VERSION),
    shadingLanguageVersion:gl.getParameter(gl.SHADING_LANGUAGE_VERSION),
    vendor:gl.getParameter(gl.VENDOR), renderer:gl.getParameter(gl.RENDERER),
    unmaskedVendor:dbg?gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL):null,
    unmaskedRenderer:dbg?gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL):null,
    extensions:(gl.getSupportedExtensions()||[]).sort(), parameters:P};
}
async function dumpGPU(){
  if(!navigator.gpu) return {available:false, reason:'no navigator.gpu'};
  let a=null; try{ a=await navigator.gpu.requestAdapter(); }catch(e){ return {available:false,reason:String(e)}; }
  if(!a) return {available:false, reason:'no adapter'};
  const i=a.info||{}, lim={};
  for(const k in a.limits) lim[k]=a.limits[k];
  const sorted={}; Object.keys(lim).sort().forEach(k=>sorted[k]=lim[k]);
  return {available:true, isFallbackAdapter:a.isFallbackAdapter,
    preferredCanvasFormat:navigator.gpu.getPreferredCanvasFormat(),
    info:{vendor:i.vendor,architecture:i.architecture,device:i.device,description:i.description,
          subgroupMinSize:i.subgroupMinSize,subgroupMaxSize:i.subgroupMaxSize},
    features:Array.from(a.features).sort(), limits:sorted};
}
// Fonts: measure sample width (the detector technique); do not enumerate.
// A family is present when its width differs from at least one of three fallbacks.
function dumpFonts(){
  const c=document.createElement('canvas'); const ctx=c.getContext('2d');
  const bases=['monospace','sans-serif','serif'];
  const S='mmmmmmmmmmlliWWWWWWWWWW0123456789ABCabc@#$%';
  const base={};
  for(const b of bases){ ctx.font='72px '+b; base[b]=ctx.measureText(S).width; }
  const present=[];
  for(const fam of FONT_CANDIDATES){
    let found=false;
    for(const b of bases){
      ctx.font='72px "'+fam+'", '+b;
      if(Math.abs(ctx.measureText(S).width-base[b])>0.5){ found=true; break; }
    }
    if(found) present.push(fam);
  }
  return {present:present, candidates:FONT_CANDIDATES.length, method:'measure'};
}
async function dumpVoices(){
  let v=speechSynthesis.getVoices(); const t0=Date.now();
  while((!v || !v.length) && Date.now()-t0 < 4000){
    await new Promise(r=>setTimeout(r,100)); v=speechSynthesis.getVoices();
  }
  return (v||[]).map(x=>({name:x.name, lang:x.lang, default:x.default}));
}
// Pre-permission, without labels: exactly what a site sees before getUserMedia.
async function dumpMedia(){
  try{
    const d=await navigator.mediaDevices.enumerateDevices();
    const c={videoinput:0, audioinput:0, audiooutput:0};
    for(const x of d){ if(c[x.kind]!==undefined) c[x.kind]++; }
    c.labels_visible = d.some(x=>!!x.label);
    return c;
  }catch(e){ return {error:String(e)}; }
}
async function dumpUACH(){
  try{
    if(!navigator.userAgentData) return {available:false};
    const h=await navigator.userAgentData.getHighEntropyValues(
      ['platformVersion','architecture','bitness','model','uaFullVersion','fullVersionList']);
    return {available:true, platform:navigator.userAgentData.platform,
            mobile:navigator.userAgentData.mobile, high:h};
  }catch(e){ return {available:false, error:String(e)}; }
}
(async()=>{
  let out;
  try{
    const dt=Intl.DateTimeFormat().resolvedOptions();
    out={webgl1:dumpGL('webgl'), webgl2:dumpGL('webgl2'), webgpu:await dumpGPU(),
         screen:{colorDepth:screen.colorDepth, pixelDepth:screen.pixelDepth,
                 width:screen.width, height:screen.height,
                 availWidth:screen.availWidth, availHeight:screen.availHeight,
                 devicePixelRatio:devicePixelRatio,
                 isExtended:(screen.isExtended===undefined?null:screen.isExtended)},
         hardware:{hardwareConcurrency:navigator.hardwareConcurrency,
                   deviceMemory:(navigator.deviceMemory===undefined?null:navigator.deviceMemory),
                   platform:navigator.platform, maxTouchPoints:navigator.maxTouchPoints},
         fonts:dumpFonts(),
         media_devices:await dumpMedia(),
         speech_voices:await dumpVoices(),
         locale:{ui:navigator.language, languages:Array.from(navigator.languages||[]),
                 locale:dt.locale, timezone:dt.timeZone},
         uach:await dumpUACH()};
  }
  catch(e){ out={error:String(e)}; }
  await fetch('/result',{method:'POST',headers:{'Content-Type':'application/json'},
                         body:JSON.stringify(out)});
  document.body.textContent='done';
})();
</script>"""


def probe_html():
    return PROBE_HTML_TMPL.replace("__FONT_CANDIDATES__",
                                   json.dumps(font_candidates(), ensure_ascii=False))


class _Handler(BaseHTTPRequestHandler):
    result = None
    page = None

    def do_GET(self):
        body = (_Handler.page or "").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            _Handler.result = json.loads(raw)
        except Exception as e:
            _Handler.result = {"error": f"bad json: {e}"}
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass  # ticho


def machine_id():
    """Otisk stroje — při neshodě se cache zahodí a proboduje znovu."""
    return f"{platform.node()}|{platform.machine()}|{platform.processor()}"


def machine_slug():
    """Krátký stabilní identifikátor stroje pro názvy adresářů (per-machine baseline).
    Hash, ne surové jméno — do gitu se tak nedostane hostname."""
    return hashlib.sha256(machine_id().encode("utf-8")).hexdigest()[:12]


def host_os():
    """OS hostitele z Pythonu (spolehlivější než UA-CH platformVersion, které je
    mapované na UniversalApiContract, ne na build number)."""
    info = {"product": platform.system(), "version": platform.version(),
            "release": platform.release()}
    if platform.system() == "Windows":
        try:
            rel, ver, sp, ptype = platform.win32_ver()
            info.update({"release": rel, "version": ver, "service_pack": sp,
                         "type": ptype})
        except Exception:
            pass
        try:
            info["build"] = int(str(info.get("version", "")).split(".")[-1])
        except Exception:
            info["build"] = None
    return info


def load_cache():
    """Vrací cache dictu, jen když sedí na TENHLE stroj A na aktuální schema; jinak
    None (v1 cache bez `version` je tím pádem neplatná a proboduje se znovu)."""
    paths.initialize_runtime_state()
    if not os.path.isfile(CACHE):
        return None
    try:
        with open(CACHE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if d.get("machine") != machine_id():
        return None
    if d.get("version") != SCHEMA_VERSION:
        return None
    return d


def _font_summary(present):
    """Odvozené pohledy na inventář (pro report a rychlé guardy v generátoru).
    Autoritativní zůstává `present` — generátor si politiku počítá sám z něj."""
    G = _gp()
    pres = {f.lower() for f in present}
    bundles = ([(k, f) for k, f in G.FONT_BUNDLES] +
               [(k, f) for k, f, _p in G.OPTIONAL_FONT_BUNDLES])
    return {
        "fp_probed_present": [f for f in G.FP_PROBED_FONTS if f.lower() in pres],
        "optional_present": {k: any(f.lower() in pres for f in fonts)
                             for k, fonts in bundles},
        # per skupina seznam PŘÍTOMNÝCH markerů — generátor z toho odvodí ANY/ALL
        # pravidla CreepJS getWindows() (skupina '7' vyžaduje všechny).
        "creepjs_markers": {g: [f for f in fonts if f.lower() in pres]
                            for g, fonts in G.CREEPJS_WIN_FONT_MARKERS.items()},
    }


def probe(build=None, timeout=90, quiet=False):
    """Spustí nativní chrome, vrátí dict (a zapíše cache). Vyhazuje RuntimeError."""
    L = _launcher()
    build = build or L.DEFAULT_BUILD
    exe = L.chrome_exe_path(build)
    if not os.path.isfile(exe):
        raise RuntimeError(f"chrome.exe neexistuje: {exe} (invalid --build or incomplete build)")

    _Handler.result = None
    _Handler.page = probe_html()
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    udd = tempfile.mkdtemp(prefix="fp_hostprobe_")
    proc = None
    try:
        # NATIVNÍ běh: žádné --fp-profile-config/--fp-profile-data → nulové maskování.
        cmd = [exe, f"--user-data-dir={udd}", "--no-first-run", "--no-default-browser-check",
               "--disable-sync", "--window-size=520,400", f"http://127.0.0.1:{port}/"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while _Handler.result is None and time.time() - t0 < timeout:
            if proc.poll() is not None and time.time() - t0 > 5:
                raise RuntimeError("Chrome exited before returning a result")
            time.sleep(0.25)
        if _Handler.result is None:
            raise RuntimeError(f"probe timeout ({timeout}s)")
        data = _Handler.result
    finally:
        srv.shutdown()
        if proc and proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                proc.kill()
        shutil.rmtree(udd, ignore_errors=True)

    if data.get("error"):
        raise RuntimeError(f"probe page error: {data['error']}")

    gl2 = data.get("webgl2") or {}
    gl1 = data.get("webgl1") or {}
    gl = gl2 if gl2.get("available") else gl1
    wgpu = data.get("webgpu") or {}
    wg = wgpu.get("info") or {}
    scr = data.get("screen") or {}
    hw = data.get("hardware") or {}
    fonts_raw = data.get("fonts") or {}
    present = fonts_raw.get("present") or []
    out = {
        "version": SCHEMA_VERSION,
        "machine": machine_id(),
        "machine_slug": machine_slug(),
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "build": build,
        # Zkratky, které konzumuje generate_profiles (host_webgl / gpu_pool):
        "webgl": {"vendor": gl.get("unmaskedVendor"), "renderer": gl.get("unmaskedRenderer")},
        "webgpu": {"vendor": wg.get("vendor"), "architecture": wg.get("architecture"),
                   "subgroup_min_size": wg.get("subgroupMinSize"),
                   "subgroup_max_size": wg.get("subgroupMaxSize"),
                   # SwiftShader/fallback adaptér = sám o sobě VM/tampering tell
                   # (paměť angle-backend-switch-vm-tell) → preflight na to reaguje.
                   "is_fallback_adapter": bool(wgpu.get("isFallbackAdapter"))},
        "screen": {
            "width": scr.get("width"), "height": scr.get("height"),
            "avail_width": scr.get("availWidth"), "avail_height": scr.get("availHeight"),
            "color_depth": scr.get("colorDepth"), "pixel_depth": scr.get("pixelDepth"),
            "device_pixel_ratio": scr.get("devicePixelRatio"),
            "is_extended": scr.get("isExtended"),
            # v1 názvy (colorDepth/…) si necháváme pro zpětnou čitelnost starých nástrojů
            "colorDepth": scr.get("colorDepth"), "pixelDepth": scr.get("pixelDepth"),
            "devicePixelRatio": scr.get("devicePixelRatio"),
        },
        "hardware": {
            "hardware_concurrency": hw.get("hardwareConcurrency"),
            # nativní bucket z prohlížeče (Chromium sám clampuje na spec hodnoty),
            # NE reálná RAM z WMI — web vidí právě tohle
            "device_memory": hw.get("deviceMemory"),
            "platform": hw.get("platform"),
            "max_touch_points": hw.get("maxTouchPoints"),
        },
        "fonts": dict({"present": present,
                       "candidates": fonts_raw.get("candidates"),
                       "candidates_hash": font_candidates_hash(),
                       "method": fonts_raw.get("method", "measure")},
                      **_font_summary(present)),
        "media_devices": data.get("media_devices") or {},
        "speech_voices": [v.get("name") for v in (data.get("speech_voices") or [])
                          if isinstance(v, dict) and v.get("name")],
        "locale": data.get("locale") or {},
        "os": host_os(),
        # Plný dump pro audit/clamp:
        "raw": data,
    }
    if not out["webgl"]["renderer"] or not out["webgpu"]["vendor"]:
        raise RuntimeError("the probe returned no usable identity (WebGL/WebGPU is missing)")
    if not out["screen"].get("width") or not out["hardware"].get("hardware_concurrency"):
        raise RuntimeError("the probe returned incomplete screen/hardware data (schema v2)")
    paths.write_json_atomic(CACHE, out)
    if not quiet:
        print_summary(out)
        print(f"[probe] saved -> {_display_path(CACHE)}")
    return out


def _display_path(path, root=ROOT):
    """Vrať čitelnou cestu i tehdy, když cache a instalace leží na jiných discích."""
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return os.path.abspath(path)


def print_summary(d):
    """Lidský přehled toho, co se na stroji naměřilo (co uvidí generátor)."""
    s = d.get("screen") or {}
    hw = d.get("hardware") or {}
    fo = d.get("fonts") or {}
    wg = d.get("webgpu") or {}
    md = d.get("media_devices") or {}
    print(f"[probe] gpu      {(d.get('webgl') or {}).get('renderer')}")
    print(f"[probe] webgpu   {wg.get('vendor')}/{wg.get('architecture')} "
          f"subgroup={wg.get('subgroup_min_size')}/{wg.get('subgroup_max_size')} "
          f"fallback_adapter={wg.get('is_fallback_adapter')}")
    print(f"[probe] screen   {s.get('width')}x{s.get('height')} "
          f"avail={s.get('avail_width')}x{s.get('avail_height')} "
          f"depth={s.get('color_depth')} dpr={s.get('device_pixel_ratio')}")
    print(f"[probe] hardware conc={hw.get('hardware_concurrency')} "
          f"mem={hw.get('device_memory')} platform={hw.get('platform')}")
    print(f"[probe] fonts    {len(fo.get('present') or [])}/{fo.get('candidates')} "
          f"present, FP.com probe list {len(fo.get('fp_probed_present') or [])}"
          f" families")
    missing_bundles = [k for k, v in (fo.get("optional_present") or {}).items() if not v]
    if missing_bundles:
        print(f"[probe]          bundles with no installed family: {', '.join(missing_bundles)}")
    print(f"[probe] media    {md.get('videoinput')} cameras / {md.get('audioinput')} microphones "
          f"/ {md.get('audiooutput')} outputs (before permission)")
    print(f"[probe] voices   {len(d.get('speech_voices') or [])}")
    lo = d.get("locale") or {}
    o = d.get("os") or {}
    print(f"[probe] locale   {lo.get('ui')} / {lo.get('timezone')}   "
          f"OS {o.get('product')} {o.get('version')}")


def get_host(build=None, quiet=True):
    """Cache-first přístup pro ostatní moduly. None, když probe není k dispozici."""
    d = load_cache()
    if d:
        return d
    try:
        return probe(build=build, quiet=quiet)
    except Exception as e:
        if not quiet:
            print(f"[probe] failed: {e}")
        return None


def main():
    quiet = "--quiet" in sys.argv
    build = _launcher().parse_build_arg(sys.argv)
    if "--force" not in sys.argv:
        cached = load_cache()
        if cached:
            if not quiet:
                print(f"[probe] cache is valid (schema v{cached.get('version')})")
                print_summary(cached)
                print("[probe] use --force to run a new measurement")
            return 0
    try:
        probe(build=build, quiet=quiet)
    except Exception as e:
        print(f"[probe] ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
