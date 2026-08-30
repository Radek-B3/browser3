# Browser3 149.0.7827.201 r3 release notes

## Highlights

- The verified TASK-005 v4 GIF and MP4 demo are included in the public repository and
  binary package together with the dated public validation report.
- First run is now one command: `.\browser3`. The wrapper locates Python, performs the
  one-time host measurement when required, generates default profiles and launches the
  browser.
- Mutable profiles, user data, measurements, caches, state and logs remain below
  `%LOCALAPPDATA%\Browser3`; the extracted installation directory is treated as
  read-only.
- Opt-in CDP launch is available through `--control cdp`. The release also includes
  the loopback-only `browser3_agent.py` Session API for supported Playwright,
  Puppeteer and custom automation clients.
- A minimal, self-contained `examples/puppeteer` directory documents a clean
  `npm ci` setup for the pinned `puppeteer-core@25.3.0` client. It attaches to a
  Session API endpoint and never launches a second Chromium.
- Default launches remain headful and CDP-free. Fingerprint masking remains native to
  the packaged Chromium layer; automation clients do not inject masking hooks.
- The MPL-2.0 public source snapshot is generated from an explicit allowlist. The
  private native masking engine, Chromium integration patches, internal detector
  harnesses, baselines and raw research captures are excluded.

## Validation scope

- Chromium/Chrome claim version: `149.0.7827.201`.
- The dated public lifecycle validation recorded selected same-profile signals as
  stable in 15/15 reload, 5/5 close/reopen and 5/5 new browser-process comparisons.
  Five profile signatures remained distinct in each recorded lifecycle phase.
- The TASK-005 demo uses identified, sanitized observations from the stated Release
  evaluation. External detector behavior can change and no score or bypass is
  guaranteed.
- The mandatory release gate is performed without a proxy. Proxy and multi-geo
  operation are not part of this release's tested GO.

## Binary characteristics

- Windows 10/11 x64 and a physical hardware-accelerated GPU are required.
- Browser3 PE files are not Authenticode-signed. Verify the detached OpenPGP signature
  and SHA-256 manifest; do not disable SmartScreen or antivirus protection.
- The installable package is `Browser3-149.0.7827.201-windows-x64.zip`. GitHub's
  automatically generated source archives are not installers and are not covered by
  `SHA256SUMS.txt`.
- Widevine is not included. FFmpeg is statically linked under LGPL-2.1-or-later with
  the corresponding written offer and archived relink materials for the exact build.

## Known limitations

- Windows N/KN may require the Microsoft Media Feature Pack.
- Active clients that enable the CDP runtime can be reported by detector dashboards as
  Developer Tools. This does not change where Browser3 implements fingerprint masking.
- Profiles are generated for the current physical machine. Moving them to different
  hardware is unsupported.
- Fingerprint and network-risk observations depend on host hardware, proxy/IP
  reputation, network context and changing detector behavior.
- The public source snapshot cannot build the proprietary Browser3 executable.
- Proxy geolocation, when enabled, uses the free unencrypted `ip-api.com` HTTP endpoint
  through the configured proxy.

## Upgrade notes

Existing state below `%LOCALAPPDATA%\Browser3` is reused. Keep a backup before any
upgrade and retain the complete extracted `runtime\` directory. Re-run the host probe
after changing the GPU, graphics driver, monitor, display scaling or a substantial part
of the installed font inventory.
