# Why AI Agents Need Persistent Browser Identities

*Randomizing browser fingerprints is easy. Keeping them consistent is hard.*

AI agents rarely perform only one browser task. They authenticate, pause, return after
a process restart, and sometimes run several unrelated workflows in parallel. Cookies
and local storage matter, but they are only part of the browser identity that a site
observes.

A browser also exposes related signals such as User-Agent, User-Agent Client Hints,
platform, screen characteristics, hardware buckets, locale, time zone, graphics data,
and network context. Changing one value independently can create contradictions.
Changing values again on every launch can make the same agent look like a different
browser after a reload.

The useful engineering question is therefore not just:

> Can a browser report a different fingerprint?

It is:

> Can one saved browser identity remain coherent and reproducible across time?

## Four separate properties

These ideas are easy to conflate:

- **Masking** changes selected browser-visible values.
- **Consistency** keeps related values coherent with one another.
- **Persistence** keeps the same profile identity and user state across later launches.
- **Isolation** prevents one profile from inheriting another profile's state.

Masking without consistency can produce a plausible-looking collection of contradictory
values. Consistency without persistence still gives an agent a disposable session.
Persistence without isolation risks mixing unrelated workflows.

Browser3 is **a multi-profile antidetect Chromium browser for people and AI agents.**
Its primary differentiator is **persistent, internally consistent browser identities.**
Native Chromium-level fingerprint masking is the implementation hook; consistency and
persistence are what make each saved identity testable over time.

## One agent, one saved profile

Each Browser3 profile combines a generated identity configuration with its own Chromium
user-data directory. The configuration has a persistent `profile_id`. Profile-specific
choices derive deterministically from that seed instead of fresh per-session randomness.
The user-data directory stores normal state such as cookies and web storage.

Selecting the same profile again reuses both:

```powershell
.\browser3 --profile 1
```

Different profiles use separate user-data directories and profile locks. The intended
mapping is simple:

```text
one agent -> one persistent Browser3 profile
```

That mapping does not give an agent new permissions, solve authentication, or guarantee
how a website will react. It creates a stable browser boundary that can be reproduced
and debugged.

## Masking lives inside Chromium

The Python layer probes the host, generates deterministic configuration, manages
profiles, and launches the browser. It does not inject fingerprint JavaScript into
pages. Browser-visible masking is implemented in the packaged native Chromium layer.

The [public source boundary](PUBLIC_BOUNDARY.md) is deliberate: the repository exposes
the MPL-2.0 orchestration, profile-generation policy, documentation, and release
verification path. It does not expose the private native masking implementation or
enough source to rebuild the distributed executable.

This distinction matters because a page-level JavaScript hook can change one readout
without making related browser surfaces agree. Browser3 passes validated profile data
to native browser code and preserves native behavior when a safe override is not
available.

Normal launches are headful and CDP-free. Automation is opt-in through a loopback-only
CDP control path or the local Session API. In a five-profile Release measurement, an
active Playwright or Puppeteer client was visible to detector tooling as Developer
Tools in `5/5` profiles. That limitation is documented rather than treated as a
fingerprint-masking result.

## Version details are part of consistency

The distributed Browser3 runtime is based on Chromium `149.0.7827.201`. That does not
mean every browser surface reports the same four-part string.

The reduced User-Agent currently follows Chromium's reduced format and reports
`149.0.0.0`. High-entropy UA-CH and generated profile full-version values use
deterministic verified patches from `149.0.7827.196` through `149.0.7827.201`.
Browser3 therefore does not claim exact `.201` on every surface.

Hardware-bound capabilities, limits, color depth, and the ANGLE backend remain native.
A GPU identity override is accepted only as one validated WebGL/WebGPU block; invalid
data falls back to host behavior. Canvas and audio noise remain off by default because
earlier testing detected them. The currently measured clean seeded diversity axes are
text edging and font hiding.

## What the five-profile validation found

The public lifecycle report is a dated observation for Browser3/Chromium
`149.0.7827.201` on one physical Windows host and one network context. It used five
persistent profiles in a headful, CDP-free run without a proxy. It did not select one
favorable profile.

| Lifecycle check | Recorded result |
|---|---:|
| Selected signals stable across reloads | `15/15` |
| Close and reopen | `5/5` |
| New browser-process restart | `5/5` |
| Distinct private profile signatures in each lifecycle phase | `5/5` |

The `15/15` reload total represents five profiles across three process cycles. Raw
identifiers and raw profile signatures are not published.

The immutable r3 release then passed its own headful, CDP-free, no-proxy gate on
2026-08-20. All five profiles returned scores and reload measurements. The recorded
five-profile averages were `0.0412` for the Fingerprint.com tampering model, `0.0654`
for its virtual-machine model, and `0.1250` for the optional VPN model. These numbers
are observations of those models on that release and host, not targets or bypass
guarantees.

The deeper public cross-check dated 2026-08-15 also kept adverse results visible:

| Service | Recorded observation | What it does not prove |
|---|---|---|
| Fingerprint.com Playground | Tampering-model averages `0.0283-0.0335`; VM-model averages `0.0580-0.0600` across three five-profile lifecycle phases | No universal PASS; a `visitorId` is not proof of masking |
| Pixelscan | Navigator, webdriver, and CDP checks clear on `15/15`; User-Agent check clear on `0/15` | Mixed result, not an overall pass |
| OverpoweredJS | Automated collection errored on `15/15` | No conclusion |
| CreepJS | A red warning appeared on `15/15` | No overall pass |
| BrowserLeaks | Features Detection captured for `5/5`; the reviewed viewport showed `322/322` features | Surface availability, not a bypass verdict |

These services measure different things and can change independently of Browser3. A
visitor identifier, a tampering signal, a bot verdict, and a lifecycle comparison are
not interchangeable metrics. The complete method and limitations are in the
[public validation report](PUBLIC_VALIDATION.md).

## A reproducible way to challenge the claim

Use the official Windows x64 r3 release and verify the detached OpenPGP signature and
SHA-256 manifest. Browser3-owned PE files are not Authenticode-signed. Keep the complete
`runtime\` directory intact and use a physical hardware-accelerated GPU.

For a gate-compatible direct-network run, launch all five profiles without a proxy:

```powershell
.\browser3 --profile 1 --no-proxy
.\browser3 --profile 2 --no-proxy
.\browser3 --profile 3 --no-proxy
.\browser3 --profile 4 --no-proxy
.\browser3 --profile 5 --no-proxy
```

For each profile:

1. Load the same test with a fresh document between observations.
2. Compare only selected stable signals.
3. Close and reopen the same profile.
4. Start a new browser process and repeat.
5. Compare the result with the other four profiles.
6. Record the release, date, host context, proxy mode, and whether CDP was active.

Do not publish cookies, IP or geolocation data, proxy credentials, visitor/request IDs,
personal data, or raw profile signatures. The internal harness and raw baselines remain
private; the public report exposes sanitized aggregates and enough protocol detail to
challenge the conclusion without exposing sensitive test data.

## Known limits

This is not a universal detector-evasion claim.

- The cited evidence covers one physical Windows host and one network context.
- The release gate ran without a proxy; proxy and multi-geo behavior were not part of
  that tested GO.
- Profiles are generated for the current machine and should not be moved to different
  hardware.
- The current runtime requires Windows x64 and a physical hardware-accelerated GPU.
- Widevine is not included; Windows N/KN may require the Media Feature Pack.
- The public source layer cannot rebuild the proprietary native executable.
- External detector models change over time, and the recorded results include mixed and
  unavailable outcomes.

Browser3 does not claim to be "undetectable" and does not guarantee an anti-bot bypass
or a particular detector score.

## Try it, then report a contradiction

Download the immutable
[Browser3 `v149.0.7827.201-r3` release](https://github.com/Radek-B3/browser3/releases/tag/v149.0.7827.201-r3)
and follow the repository [Quick Start](README.md#quick-start). A Python 3.10+ client is
also published as `browser3==0.1.0`; the optional npm package
`@radek-b3/browser3@0.1.0` is a thin wrapper around that Python client, not a separate
runtime.

The most useful experiments are simple:

- reopen the same profile after a full browser restart;
- compare its selected signals with another profile;
- test an automation lifecycle separately from a clean CDP-free run;
- report a reproducible inconsistency, limitation, or detector change.

Use Browser3 only on systems and accounts you own or are authorized to test. Please file
sanitized, reproducible feedback through [GitHub Issues](https://github.com/Radek-B3/browser3/issues)
or [GitHub Discussions](https://github.com/Radek-B3/browser3/discussions).

I maintain Browser3. This article is a request for technical criticism and reproduction,
not an independent review or a promise of detector evasion.
