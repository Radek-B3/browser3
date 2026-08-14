<div align="center">

# Browser3

**A multi-profile browser for people and AI agents.**

*One installation. Multiple independent, persistent browser identities.*

[![Windows](https://img.shields.io/badge/Windows-10%2F11-0078D4?logo=windows&logoColor=white)](#system-requirements)
[![Chromium](https://img.shields.io/badge/Chromium-149-4285F4?logo=googlechrome&logoColor=white)](#)
[![Public layer](https://img.shields.io/badge/public_layer-MPL--2.0-2C3E50)](LICENSE)
[![Latest release](https://img.shields.io/badge/download-latest_release-2EA44F)](https://github.com/Radek-B3/browser3/releases/latest)

</div>

Browser3 lets people and AI agents create and run multiple isolated browser profiles from one installation. Each profile has its own browsing data and a deterministic, internally consistent browser identity that remains stable across reloads and restarts.

Instead of blanking fingerprint surfaces or changing values on every page load, Browser3 builds each profile around the real Windows host and keeps its signals coherent. Each profile therefore behaves like an independent, persistent browser identity.

## Why Browser3?

| | |
|---|---|
| **Multiple browser identities** | Create as many persistent profiles as you need and run them separately or together. |
| **Agent-friendly CLI** | Create, reopen, and launch isolated browser identities through predictable command-line workflows. |
| **Stable by design** | A profile keeps the same deterministic identity across page reloads and browser restarts. |
| **Internally consistent** | Browser, hardware, screen, locale, time zone, GPU, and network-facing values are kept coherent. |
| **Native Chromium integration** | Fingerprint handling lives inside the browser rather than in injected page scripts or JavaScript hooks. |
| **Isolated profile data** | Every browser identity uses its own Chromium user-data directory, cookies, history, and settings. |
| **Optional sticky proxies** | A proxy can remain paired with the same profile so its network identity does not unexpectedly rotate between launches. |
| **Local-first** | Profiles, browsing data, hardware measurements, proxy credentials, and logs stay on your computer. Browser3 adds no vendor telemetry. |

## Quick start

Download `Browser3-<version>-windows-x64.zip` from the [latest official release](https://github.com/Radek-B3/browser3/releases/latest), extract it, open PowerShell in the extracted directory, and run:

```powershell
python launcher.py
```

That single command measures the host when needed, creates a fresh browser profile, and launches it. Run it again whenever you want another independent browser identity.

The packaged Chromium runtime is self-contained below `runtime/`. Keep that directory intact; `chrome.exe` depends on its adjacent DLL, resource, locale, and data files.

### Working with multiple profiles

```powershell
# Reopen an existing browser identity
python launcher.py --profile 1

# Launch directly using host network (ignores proxy.txt)
python launcher.py --profile 1 --no-proxy

# Show all profile, proxy, desktop, and build options
python launcher.py --help
```

### Optional: Using proxies

Browser3 can automatically route profiles through HTTP or SOCKS5 proxies. Create a `proxy.txt` file in the root directory:

```text
# HTTP proxy without authentication
192.0.2.10:8080

# HTTP proxy with authentication
192.0.2.11:8080:myuser:mypass

# SOCKS5 or URL format
socks5://myuser:mypass@192.0.2.12:1080
http://myuser:mypass@192.0.2.13:8080
```

**Key features:**
- **Sticky mapping:** Each profile stays persistently paired with its proxy across restarts to prevent location hopping.
- **Auto-coherence:** Automatically syncs browser timezone, `Accept-Language`, and fonts to match the proxy exit IP.
- **WebRTC leak protection:** Automatically blocks non-proxied UDP traffic to protect your direct IP.

For advanced CLI options, AI agent automation (Playwright/CDP examples), and background execution via isolated desktops, see the [CLI & Automation Guide](USAGE.md).

## Download the right file

Use the installable Windows archive named:

```text
Browser3-<version>-windows-x64.zip
```

GitHub's automatically generated `Source code (zip)` and `Source code (tar.gz)` downloads are source snapshots, not Browser3 installers. The signed public-source snapshot is published separately as `Browser3-public-source-<version>.zip`.

## System requirements

| Component | Requirement |
|---|---|
| Operating system | Windows 10 version 1809 (build 17763) or newer, x64 |
| Python | Python 3.7 or newer, available as `python` in `PATH` |
| Graphics | A physical GPU with working hardware acceleration |
| Display scaling | Any Windows scaling level; 125% and 150% are supported |

Browser3 stops instead of creating an incoherent profile when it cannot establish a valid host configuration. Unsupported cases include a SwiftShader/software fallback adapter, Windows older than version 1809, and a failed host probe.

Windows N/KN editions require the Microsoft Media Feature Pack for H.264/AAC playback. Widevine CDM is not included, so DRM-protected streaming services may not work.

## Where your data lives

Browser3 treats its installation directory as read-only and stores mutable state below `%LOCALAPPDATA%\Browser3`:

| Data | Location |
|---|---|
| Generated identities and browser data | `%LOCALAPPDATA%\Browser3\profiles` |
| Hardware and network caches | `%LOCALAPPDATA%\Browser3\cache` |
| Sticky proxy mappings and migration state | `%LOCALAPPDATA%\Browser3\state` |
| Logs | `%LOCALAPPDATA%\Browser3\logs` |

Profiles are generated for the computer on which they run. Copying a profile to a different machine is unsupported because its hardware claims would no longer match the host.

## Verify your download

Verify `SHA256SUMS.txt.asc` with the public key in `browser3-release-key.asc`, then compare the archive's SHA-256 hash with `SHA256SUMS.txt`.

Browser3 PE files are not Authenticode-signed. Do not disable Microsoft SmartScreen or antivirus protection to run them.

## Public source and native browser

This repository contains the public Python orchestration and profile-generation layer, the public GPU template catalog, and release documentation. It is not the complete source code of the Browser3 executable and cannot build `chrome.exe`.

The native fingerprint engine, Chromium integration patches, detector tests, baselines, and research material remain private. See [Public and private boundary](PUBLIC_BOUNDARY.md) for the exact split.

Public files are licensed under [MPL-2.0](LICENSE) unless stated otherwise. The downloadable binary is governed by [BINARY_EULA.txt](BINARY_EULA.txt) together with the applicable open-source licenses in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Responsible use and support

Use Browser3 only on systems and accounts you own or are authorized to test. The [Acceptable Use Policy](ACCEPTABLE_USE.md) applies to official binaries, services, and support. Distribution and use must also comply with applicable export-control and sanctions laws; see [Export and sanctions](EXPORT_AND_SANCTIONS.md).

For local and network data behavior, read [Privacy](PRIVACY.md). For help, see [Support](SUPPORT.md). Report security issues privately as described in [Security](SECURITY.md).
