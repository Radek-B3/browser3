# Browser3 npm wrapper

This package is a small Node.js command wrapper for the Browser3 Python client.
It is not an npm-only Browser3 runtime and it does not contain Chromium, the
signed-release verifier, or a runtime cache implementation.

Install the Python client first on supported Windows x64:

```powershell
python -m pip install browser3
npm install --global @radek-b3/browser3
browser3 install
browser3 launch --profile 1
```

The wrapper checks `py -3.13`, `py -3.12`, `py -3.11`, `py -3.10`, then `python`
for Python 3.10 or newer with the `browser3` module installed. It forwards
`install`, `launch`, `update`, `list`, and `doctor` to `python -m browser3`, with
stdin/stdout/stderr and the child exit status preserved. If no suitable Python
client is found, it fails closed with an installation diagnostic. It never
installs Python or downloads a package from PyPI automatically.

Runtime installation, OpenPGP/SHA-256 verification, version pinning,
`BROWSER3_VERSION`, `BROWSER3_BINARY_PATH`, and the no-silent-upgrade policy are
implemented only by the Python client. Use `browser3 update <version>` for an
explicit runtime change; an npm invocation does not silently upgrade Chromium.

The package is Windows x64 only, matching the first supported Python distribution.
Version `@radek-b3/browser3@0.1.0` is published on npm; repository tests and pack checks
do not publish future versions. Browser3 remains a multi-profile antidetect Chromium
browser for people and AI agents; its primary differentiator is persistent, internally
consistent browser identities. It does not guarantee bypass of any anti-bot or
fingerprinting system.

License: MPL-2.0. See the repository public license for the complete text.

Browser3 is declared as dual-use software under the npm Dual-Use Content Policy. See
the `DISCLOSURE` file included in the package. The first npm release was published
interactively with 2FA; later GitHub OIDC releases are staged and require separate
maintainer approval with 2FA before becoming public.
