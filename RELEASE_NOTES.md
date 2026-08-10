# Browser3 149.0.7827.201 release notes

## Highlights

- Mutable runtime state now lives under `%LOCALAPPDATA%\Browser3`, leaving the extracted installation directory read-only.
- Generated identities and Chromium user data live under `%LOCALAPPDATA%\Browser3\profiles`.
- GPU identities use one versioned public catalog at `resources/gpu_templates.json`.
- Public documents, CLI help, and user-facing Python messages are in English.
- The MPL-2.0 public source snapshot has an explicit allowlist and a new-history export workflow.
- The private native masking engine and Chromium integration patches are excluded from the public source snapshot.
- Distribution is free through public GitHub Releases without accounts, payments, or country blocking; `EXPORT_AND_SANCTIONS.md` records the sanctions restrictions and the Provider's own export classification.

## Binary characteristics

- Chromium/Chrome claim version: `149.0.7827.201`.
- Windows x64 only.
- Widevine is not included.
- Browser3 PE files are not Authenticode-signed; release authenticity is verified through the signed checksum manifest.
- The installable package is `Browser3-149.0.7827.201-windows-x64.zip`. GitHub's automatically generated source archives are not installers and are not covered by `SHA256SUMS.txt`.
- FFmpeg is statically linked under LGPL-2.1-or-later. Corresponding source, relinkable objects, and relinking instructions are retained for the exact build under the written offer.

## Known limitations

- Windows N/KN may require the Microsoft Media Feature Pack.
- Fingerprint and network-risk scores depend on host hardware, proxy quality, IP reputation, and detector changes; no score is guaranteed.
- The source snapshot cannot build the proprietary Browser3 executable.
- Proxy geolocation uses the free unencrypted HTTP endpoint at `ip-api.com` through the configured proxy.

## Upgrade notes

On first start, Browser3 performs a non-destructive one-time migration of legacy profile and cache state into `%LOCALAPPDATA%\Browser3`. Source files in the old installation tree are preserved. Keep a backup until the migrated profiles have been manually verified.
