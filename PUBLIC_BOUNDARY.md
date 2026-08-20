# Public and private boundary

This document defines the release boundary for Browser3. The machine-readable source of truth is `release/public_source_allowlist.json` in the private development repository; the public snapshot is built only from that allowlist.

## Public source layer (MPL-2.0)

- Python orchestration shipped to end users: launcher, runtime path handling, proxy forwarding, desktop handling, host probing, and codec preflight.
- Profile-generation policy in `generate_profiles.py`.
- The single read-only GPU identity catalog at `resources/gpu_templates.json`.
- User, security, support, and licensing documentation.
- The FFmpeg codec-removal patch and LGPL compliance material required to document the distributed third-party build.

## Not included in the public source layer

- The private Chromium working tree, native masking implementation, and Chromium/Blink/V8/content/net integration changes.
- Build infrastructure and private Chromium working-tree history.
- Internal detector harnesses, baselines, screenshots, measurements, and research captures.
- Runtime profiles, Chromium user data, caches, proxy configuration, credentials, logs, signing private keys, and release evidence archives.
- Private planning documents, prompts, CHANGELOG, and agent instructions.
- Internal migration, compatibility, release-integrity, catalog-build, and host-coherence
  tools: `fix_paths.py`, `scripts/probe_host_gpu.py`,
  `scripts/check_codec_integrity.py`, `scripts/build_gpu_templates_public.py`, and
  `scripts/verify_host_coherence.py`.

## Relationship to the binary release

The binary release combines:

1. this MPL-2.0 public layer;
2. a proprietary native Browser3 layer distributed in object form under `BINARY_EULA.txt`; and
3. third-party components under their own licenses, documented in `THIRD_PARTY_NOTICES.md` and the `licenses/` directory.

The complete Chromium runtime is packaged below `runtime/`; the private development
path and build-directory names are not reproduced in the binary archive.

MPL-2.0 does not apply to the private native layer or replace third-party license terms. The public repository is intentionally not sufficient to rebuild the distributed Browser3 executable.

Production reference inputs packaged with the binary are runtime data, not raw research captures or detector baselines. They are not part of the MPL-licensed source snapshot.
