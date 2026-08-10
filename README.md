# Browser3 public layer

Browser3 is a Windows Chromium-based browser that keeps each generated browser identity deterministic and internally consistent. This repository snapshot contains the public Python orchestration and profile-generation layer, the public GPU template catalog, and release documentation.

## Download

Download the installable Windows package from the
[latest official Browser3 release](https://github.com/Radek-B3/browser3/releases/latest).
The application archive is named `Browser3-<version>-windows-x64.zip`.

GitHub's automatically generated `Source code (zip)` and `Source code (tar.gz)` files
are source snapshots, not Browser3 installers, and are not part of the signed asset set.
For the signed public-source snapshot, use the release asset named
`Browser3-public-source-<version>.zip`.

## Important scope notice

This repository is not the complete source code of the Browser3 executable and cannot build `chrome.exe`. The native fingerprint-masking engine, its Chromium integration patches, internal detector tests, baselines, and research material are private. The exact boundary is documented in [PUBLIC_BOUNDARY.md](PUBLIC_BOUNDARY.md).

Files in this public snapshot are licensed under MPL-2.0 unless a file or third-party notice says otherwise. The downloadable Browser3 binary is governed by [BINARY_EULA.txt](BINARY_EULA.txt) together with the applicable open-source licenses documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The public GitHub distribution model and recipient restrictions are described in [EXPORT_AND_SANCTIONS.md](EXPORT_AND_SANCTIONS.md).

## System requirements

| Component | Requirement |
|---|---|
| Operating system | Windows 10 version 1809 (build 17763) or newer, x64 |
| Python | Python 3.7 or newer, available as `python` in `PATH` |
| Graphics | A physical GPU with working hardware acceleration |
| Display scaling | Any Windows scaling level; 125% and 150% are supported |

Browser3 refuses to generate or start a profile when it cannot establish a coherent host
configuration. Unsupported cases include a SwiftShader/software fallback adapter,
Windows older than 10 version 1809, and a missing host probe.

Windows N/KN editions require the Microsoft Media Feature Pack for H.264/AAC playback.
Browser3 checks the required operating-system codecs during startup.

Widevine CDM is not included, so DRM-protected streaming services may not work.

## Using the binary package

From the extracted release directory:

```powershell
python scripts/probe_host.py --force
python generate_profiles.py
python launcher.py --profile 1
```

The packaged Chromium runtime is self-contained below `runtime/`. Keep that directory
intact; `chrome.exe` depends on its adjacent DLL, resource, locale, and data files.

Run `python launcher.py --help` for profile, proxy, desktop, and build options. Do not move generated profile JSON or Chromium user data back into the installation directory; runtime state belongs under `%LOCALAPPDATA%\Browser3`.

The source snapshot alone intentionally omits production reference inputs and the proprietary executable. Use the matching binary package when running Browser3.

## Runtime data

Browser3 stores mutable state per user:

- generated identities and Chromium user data: `%LOCALAPPDATA%\Browser3\profiles`
- hardware and network caches: `%LOCALAPPDATA%\Browser3\cache`
- sticky mappings and migration state: `%LOCALAPPDATA%\Browser3\state`
- logs: `%LOCALAPPDATA%\Browser3\logs`

The installation directory is treated as read-only.

## Verify a download

Verify `SHA256SUMS.txt.asc` with the public key in `browser3-release-key.asc`, then compare the archive SHA-256 with `SHA256SUMS.txt`. Browser3 PE files are not Authenticode-signed; do not disable SmartScreen or antivirus protections to run it.

## Safety and support

Use Browser3 only on systems and accounts you own or are authorized to test. The [Acceptable Use Policy](ACCEPTABLE_USE.md) applies to official binaries, services, and support, without restricting the rights granted for MPL-licensed source code.

Browser3 must not be supplied or used in violation of applicable export-control or
sanctions laws. This release has no account, payment, download-approval, or country-blocking
service; see [EXPORT_AND_SANCTIONS.md](EXPORT_AND_SANCTIONS.md) for the exact scope and the
Provider's self-classification notice.

See [PRIVACY.md](PRIVACY.md) for local and network data behavior. For ordinary help, see [SUPPORT.md](SUPPORT.md). Report security issues privately as described in [SECURITY.md](SECURITY.md).
