<div align="center">

# Browser3

**A multi-profile antidetect Chromium browser for people and AI agents.**

Create isolated browser profiles with persistent, internally consistent identities — designed for manual use, automation and long-running AI agents.

## Persistent, internally consistent browser identities.

[Download / Releases](https://github.com/Radek-B3/browser3/releases/latest) · [Quick Start](#quick-start) · [Documentation](#documentation) · [Issues / feedback](https://github.com/Radek-B3/browser3/issues)

[![Windows](https://img.shields.io/badge/Windows-10%2F11-0078D4?logo=windows&logoColor=white)](#requirements-and-limitations)
[![Chromium](https://img.shields.io/badge/Chromium-149-4285F4?logo=googlechrome&logoColor=white)](#how-it-works)
[![Public layer](https://img.shields.io/badge/public_layer-MPL--2.0-2C3E50)](LICENSE)

</div>

> **One profile = one persistent browser identity.**
>
> For agent workflows: **one agent = one persistent browser identity.**

Each saved profile combines a deterministic identity configuration with its own Chromium user-data directory. Selecting that profile again reuses both; native Chromium code consumes the identity configuration without injected page scripts.

## Why Browser3?

| Benefit | What it means in Browser3 |
|---|---|
| **Persistent identities** | A saved profile reuses its seed, generated configuration and Chromium user-data directory. |
| **Multi-profile isolation** | Each profile has separate cookies, history, storage, preferences and identity data. |
| **Native Chromium integration** | Fingerprint handling lives inside the packaged browser, not in injected JavaScript or CDP injection. |
| **Internally consistent browser identities** | Profile generation uses host measurements and coherence rules across UA/UA-CH, hardware buckets, display, locale, time zone and GPU/WebGL data. |
| **Human + AI workflows** | Profiles run as normal headful sessions. Scriptable profile selection and supported Playwright / CDP automation are available via opt-in controls. |
| **Local-first architecture** | Browser3-managed profiles, browsing data, measurements, caches, state and logs stay local. Browser3 adds no vendor telemetry. |

## Demo

The local TASK-005 v4 demo combines two separate owner-supplied Fingerprint signal
views, the captured Pixelscan observation and a five-profile lifecycle card. The GIF
is the compact preview; the MP4 is the full-quality version.

[![Browser3 TASK-005 v4 demo](assets/TASK005_Browser3_demo_v4.gif)](assets/TASK005_Browser3_demo_v4.mp4)

[Open the MP4 v4 demo](assets/TASK005_Browser3_demo_v4.mp4) · [Fingerprint reference 1](assets/TASK005_fingerprint-reference-1_v4.png) · [Fingerprint reference 2](assets/TASK005_fingerprint-reference-2_v4.png)

The Fingerprint section shows two owner-supplied sanitized screenshots as separate
full-frame views using contain scaling. Concrete IP, geolocation, visitor, request and
event identifiers are not visible. The owner-approved velocity row shows only the
aggregate text “1 IP, 1 Linked ID in the past 24 hours”, not a concrete IP or Linked ID.
The five-slot montage intentionally repeats one full-frame visual reference; it is not
five independent fresh measurements. The measured five-profile lifecycle and
external-service scope remain in the dated [PUBLIC_VALIDATION.md](PUBLIC_VALIDATION.md).


## Quick start

### Python client (Windows x64, Python 3.10+)

```powershell
python -m pip install browser3==0.1.0
browser3 install
browser3 launch --profile 1
```

The optional `@radek-b3/browser3@0.1.0` npm package is a thin wrapper around this
Python client; it is not a separate npm-only runtime.

### Direct release ZIP (Windows x64, Python 3.7+)

Download `Browser3-<version>-windows-x64.zip` from the [latest official release](https://github.com/Radek-B3/browser3/releases/latest). GitHub's automatically generated `Source code (zip)` and `Source code (tar.gz)` archives are not Browser3 installers. The signed public-source snapshot is `Browser3-public-source-<version>.zip`.

Extract the complete archive, keep `runtime\` intact, open PowerShell or Command Prompt in the extracted directory, and run:

```powershell
.\browser3
```

or using Python directly:

```powershell
python launcher.py
```

On first run, Browser3 automatically measures host characteristics (once, about 15 seconds), generates default profiles, and starts the browser session.

To reopen profile 1 and return to the same saved identity:

```powershell
.\browser3 --profile 1
```

Useful existing commands:

```powershell
# Use the host network even when proxy.txt contains entries
.\browser3 --profile 1 --no-proxy

# Inspect the launch command without starting Chromium
.\browser3 --profile 1 --dry-run

# List all implemented options
.\browser3 --help
```

## AI agents and automation

The launcher provides scriptable profile selection, profile locking, optional sticky proxy assignment and headful Chromium launch. It also supports opt-in CDP automation (via `--control cdp` or the local Session API) for Playwright, Puppeteer and AI agents, while default launches keep CDP disabled so normal sessions stay completely CDP-free.

See the [CLI & automation reference](USAGE.md) for implemented controls and their current status.

The release includes a minimal [Puppeteer example](examples/puppeteer/README.md).
It installs `puppeteer-core@25.3.0` in its own directory and attaches to the
loopback endpoint returned by the Session API. Puppeteer is a client only: Browser3
owns Chromium startup, profile selection, the persistent profile lock and cleanup.

## Benchmarks

A sanitized five-profile validation report is included in this source layer. On Browser3
149.0.7827.201 it recorded selected same-profile signals as stable in 15/15 reload
comparisons, 5/5 close/reopen comparisons and 5/5 new browser-process comparisons. The
private isolation comparison found 5/5 distinct profile signatures in each lifecycle
phase.

Fingerprint.com tampering-model averages ranged from 0.0283 to 0.0335 across the three
five-profile phases, compared with the historical per-machine average of 0.1053. The
virtual-machine model ranged from 0.0580 to 0.0600, compared with 0.0466 historically.
VPN is not part of the required release gate. These are observations from one host and
network context, not guarantees of future detector results or anti-bot bypasses.

See the [public validation report](PUBLIC_VALIDATION.md) for the method, native reference,
external cross-checks, screenshot and known limitations. In particular, the recorded GPU
combinations do not support a public host-coherence claim. Browser3 does not claim to be
“undetectable”.

## How it works

1. **Public source** — MPL-2.0 Python orchestration, host probing, profile-generation policy, proxy helpers, GPU catalog and release documentation.
2. **Private native layer** — the fingerprint engine and Chromium integration patches, distributed in object form under the binary license. The public snapshot cannot build the executable.
3. **Profile data** — seeded JSON configurations and separate Chromium user-data directories below `%LOCALAPPDATA%\Browser3`.
4. **Chromium runtime** — the patched runtime below `runtime\`; the launcher supplies the chosen configuration and user-data directory, and native code applies available values.

Python handles orchestration and policy; fingerprint handling remains inside Chromium. See [Public and private boundary](PUBLIC_BOUNDARY.md) for the exact split.

## Requirements and limitations

| Component | Requirement |
|---|---|
| Operating system | Windows 10 version 1809 (build 17763) or newer, x64 |
| Python | Python 3.7 or newer, available as `python` in `PATH` |
| Graphics | A physical GPU with working hardware acceleration |
| Display scaling | Any Windows scaling level; 125% and 150% are supported |

- No MSI/GUI installer or standalone single-file executable is claimed today. The
  supported paths are the version-pinned Python client or the verified release ZIP;
  the npm package delegates to the Python client.
- The PyPI client requires Python 3.10 or newer; the direct release ZIP wrapper supports
  Python 3.7 or newer.
- Profiles are generated for the current machine. Moving them to different hardware is unsupported because hardware claims may no longer match.
- Browser3 PE files are not Authenticode-signed. Verify the signed checksum manifest; do not disable SmartScreen or antivirus protection.
- Widevine is not included. DRM-protected streaming services may not work, and Windows N/KN may require the Microsoft Media Feature Pack for H.264/AAC playback.
- The current release gate was completed without a proxy. Proxy and multi-geo operation were not part of that release's tested GO.
- Proxy geolocation, when used, calls the free unencrypted `ip-api.com` HTTP endpoint through the configured proxy.
- The demo is a visual supplement; validation is limited to the scope documented in `PUBLIC_VALIDATION.md`.
- Fingerprint and network-risk results depend on the host, proxy/IP reputation and changing detector behavior. Browser3 does not guarantee bypasses or scores.

## Documentation

- [Why AI Agents Need Persistent Browser Identities](WHY_AI_AGENTS_NEED_PERSISTENT_BROWSER_IDENTITIES.md)
- [CLI & automation reference](USAGE.md)
- [Frequently asked questions](FAQ.md)
- [Release notes](RELEASE_NOTES.md)
- [Public and private boundary](PUBLIC_BOUNDARY.md)
- [Privacy](PRIVACY.md)
- [Support](SUPPORT.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Acceptable Use Policy](ACCEPTABLE_USE.md)

Use Browser3 only on systems and accounts you own or are authorized to test. Use [GitHub Issues](https://github.com/Radek-B3/browser3/issues) for reproducible bugs or feedback; report vulnerabilities through [SECURITY.md](SECURITY.md).
