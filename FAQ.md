# Browser3 FAQ

## Is Browser3 Chromium?

Yes. Browser3 ships a patched Chromium runtime. Python prepares profiles and launches
the browser; fingerprint handling is implemented in the native browser layer.

## How is Browser3 different from regular Chromium profiles?

Both keep browsing data in separate user-data directories. Browser3 additionally gives
each profile a deterministic identity configuration that its native Chromium layer
consumes when the profile runs.

## How is Browser3 different from JavaScript fingerprint spoofing?

Browser3 does not inject fingerprint hooks or proxy objects into pages. The public
Python layer handles orchestration and profile policy, while fingerprint surfaces are
handled inside the packaged Chromium runtime.

## Does a profile keep its identity after Browser3 restarts?

Reopening a profile reuses its saved identity configuration and Chromium user-data
directory:

```powershell
python launcher.py --profile 1
```

This is the persistence model implemented by Browser3. The dated
[public validation report](PUBLIC_VALIDATION.md) records 15/15 same-profile reload
comparisons, 5/5 close/reopen comparisons, 5/5 new browser-process comparisons and
five distinct profile signatures in each lifecycle phase on the stated release and
host. These observations are not a universal detector or future-release guarantee.

## Can I run several profiles at the same time?

Distinct profiles use separate user-data directories and can be launched independently.
Browser3 locks each profile while it is running to prevent concurrent writes to the
same profile directory.

## Can an AI agent reopen the same profile later?

Yes. An agent or script can select an existing profile with `--profile` (or via the local
Session API). Reopening the profile reuses the same deterministic identity configuration,
user data directory, and persistent cookies across runs.

## Does Browser3 support Playwright, Puppeteer or CDP?

Yes. Browser3 supports Playwright, Puppeteer, and raw CDP automation via an opt-in control
path (`--control cdp` or the local Session API). Default launches keep CDP disabled so
normal sessions remain completely CDP-free. When enabled, automation connects to a loopback
CDP endpoint while preserving persistent profile identities and OS-level file locks without
injecting JavaScript hooks.

The release includes a minimal [`puppeteer-core@25.3.0` example](examples/puppeteer/README.md).
It attaches to the endpoint returned by the Session API; it does not launch a second
Chromium, choose a user-data directory, or own profile cleanup.

Because an active client may enable the CDP Runtime domain, detector tooling can report
Developer Tools. An open endpoint without a client and a Puppeteer attach are separate
operating modes. Browser3 does not claim that either automation mode is undetectable.

## Where are profiles, cookies and settings stored?

Browser3-managed state is stored below `%LOCALAPPDATA%\Browser3`. Identity
configurations and Chromium user data are under `profiles`; caches, sticky proxy
mappings and logs use their own subdirectories. See [Privacy](PRIVACY.md) for details.

## Does Browser3 upload fingerprint or browsing data?

Browser3 adds no vendor telemetry or browsing-activity reporting service. Chromium
still uses its own network services. When a proxy is configured, Browser3 also queries
`ip-api.com` through that proxy to derive country and time-zone data. The free endpoint
uses unencrypted HTTP. See [Privacy](PRIVACY.md) for the complete disclosure.

## Is Browser3 open source?

The Python orchestration, profile-generation policy, GPU catalog and public
documentation are available under MPL-2.0. The native fingerprint engine and Chromium
integration patches are private and are distributed only in object form with the
binary release. The public snapshot cannot build `chrome.exe`; see
[Public and private boundary](PUBLIC_BOUNDARY.md).

## Can I copy a profile to another computer?

That workflow is unsupported. Profiles are generated from measurements of the current
Windows host, so moving one to different hardware can make its identity inconsistent.

## Are proxies required?

No. Browser3 can use the host network directly or assign configured proxies persistently
to profiles. Proxy and multi-geo operation were not part of the current release's tested
GO, and proxy or IP reputation can materially affect network-risk results.

## Is there an installer or standalone Browser3 CLI?

There is no installer or standalone native CLI. The release includes a one-command
Windows wrapper. Extract the official archive, keep `runtime\` intact, and run:

```powershell
.\browser3
```

Python 3.7 or newer must still be available through `py.exe -3` or `python.exe`.

## Do GitHub's automatic source archives contain a runnable browser?

No. Use the official `Browser3-<version>-windows-x64.zip` release asset. GitHub's
automatic source archives and the signed public-source snapshot do not contain the
proprietary Browser3 executable.

## Does Browser3 guarantee that anti-bot or fingerprinting systems will not detect it?

No. Browser3 focuses on persistent, internally consistent browser identities. Detection
systems change continuously, and Browser3 does not guarantee bypasses, detector scores
or account outcomes.
