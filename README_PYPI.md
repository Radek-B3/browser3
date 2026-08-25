# Browser3 Python client

The `browser3` package is a small Windows x64 orchestrator. It does not contain
Chromium or fingerprint-masking code. The first runtime is pinned to
`149.0.7827.201-r3` and is downloaded only by an explicit install or first launch.

```powershell
python -m pip install browser3
browser3 install
browser3 launch --profile 1
```

The runtime pin can be selected explicitly for one command with
`browser3 launch --runtime-version <version>`, or persistently for the process with
`BROWSER3_VERSION`. The launcher arguments come after the Browser3 options, for
example `browser3 launch --runtime-version 149.0.7827.201-r3 --profile 1`.

The client verifies the detached OpenPGP signature of `SHA256SUMS.txt`, checks the
ZIP SHA-256, rejects unsafe archives, and activates a versioned cache below
`%LOCALAPPDATA%\\Browser3\\browsers`. It never silently upgrades Chromium. Use
`browser3 update <version>` for an explicit upgrade; older runtimes remain available
for rollback. `BROWSER3_VERSION` selects a supported pin and
`BROWSER3_BINARY_PATH` selects an absolute, user-managed `chrome.exe` (which the
client does not claim to have verified as an official release). The override only
changes the executable selected by the verified runtime's launcher: `browser3 launch`
still requires the signed runtime root (including `launcher.py` and the orchestration
modules) to be installed. It never allows an unverified custom directory to supply
the launcher or profile-management code. Accordingly, `browser3 doctor` reports an
override-only state as unhealthy until that verified runtime root is present.

The Browser3 name/PyPI publication remains owner-controlled; building this wheel or
source distribution does not publish it.
