# FFmpeg LGPL-2.1 Compliance Record

Date: 2026-08-07. This technical record is not legal advice.

Browser3 uses a modified FFmpeg under LGPL-2.1-or-later. FFmpeg is statically linked into
`chrome.dll` because an unsigned component `ffmpeg.dll` failed Windows sandbox code
integrity testing. The release therefore follows LGPL-2.1 section 6(a), supported by the
written offer in the Third-Party Notices document under section 6(c).

## Distributed license material

The user archive includes:

- `COPYING.LGPLv2.1`, FFmpeg's `LICENSE.md`, and `CREDITS.chromium`;
- `ffmpeg-l2-no-proprietary-decoders.patch` and its public description;
- this compliance record; and
- the binding written offer in the Third-Party Notices document.

For each release, the Provider retains a companion LGPL bundle containing:

1. the corresponding FFmpeg source at the exact audited revision, including modifications;
2. all object files and thin-archive members needed to relink `chrome.dll`;
3. external linker inputs and the recorded GN/toolchain configuration;
4. a SHA-256 manifest; and
5. `RELINK.md` with the exact linker invocation.

Recipients may request that bundle under the written offer.

## Browser3 modifications

The FFmpeg build excludes the AAC decoder and the H.264 decoder/parser. The configured
source remains LGPL-only (`CONFIG_GPL=0`, `CONFIG_GPLV3=0`, `CONFIG_NONFREE=0`). The exact
diff is `ffmpeg-l2-no-proprietary-decoders.patch`.

The corresponding-source archive was checked against `ffmpeg_generated.gni`: all 377
compiled source files were present, along with generated configuration and build metadata.
The excluded decoder symbols were absent.

## Relinking verification

The companion bundle is derived from the actual `chrome_dll.ninja` link edge, not a manual
file list. Thin archives are expanded so their referenced object files are included.
External inputs are copied into a self-contained `srcdeps` directory.

Two checks were completed on the audited build:

- an identity relink created a working `chrome.dll` using only bundle inputs; and
- FFmpeg was rebuilt with an identifiable source marker, its objects replaced in the
  bundle, and the Product relinked successfully.

The replacement binary contained the marker while the distributed binary did not. The
replacement build started and passed H.264, AAC, VP9, and WebRTC H.264 preflight checks.
This demonstrates that a recipient can modify FFmpeg, rebuild it, and relink the Product.

## How to request the materials

Email **radekcisar77@gmail.com** with the exact Browser3 version from `MANIFEST.txt` or the
archive name. The written offer remains valid for at least three years after distribution
of that version. See `THIRD_PARTY_NOTICES.txt` in the binary package or the same document
as `THIRD_PARTY_NOTICES.md` in the public source snapshot for the binding terms.
