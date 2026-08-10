# Third-Party Notices

This file does not replace the complete attribution inventory. Chromium and its vendored
components are listed on the product's `about:credits` page, generated into this build
with `generate_about_credits` (756 components at the audited revision). The notices below
highlight modified components and obligations that require additional distribution steps.

## Chromium and Blink (BSD and other licenses)

Copyright The Chromium Authors and the individual copyright holders identified in source
files and `about:credits`.

Upstream: https://chromium.googlesource.com/chromium/src

Several modified Blink files carry BSD-3-Clause notices for holders including Apple Inc.,
Google Inc., Nicholas Shanks, Dirk Schulze, and Torch Mobile (Beijing) Co. Their copyright,
license terms, and disclaimers are reproduced by `about:credits`. The Chromium license is
also included in the `licenses` directory.

## FFmpeg (LGPL-2.1-or-later)

Copyright the FFmpeg developers.
Upstream: https://git.ffmpeg.org/ffmpeg.git

FFmpeg is statically linked into `chrome.dll` (`is_component_ffmpeg = false`). This copy is
modified so that the AAC decoder and the H.264 decoder/parser are not compiled. The exact
change is recorded in `ffmpeg-l2-no-proprietary-decoders.patch`. The configured tree is
LGPL-only: `CONFIG_GPL=0`, `CONFIG_GPLV3=0`, and `CONFIG_NONFREE=0`.

The complete LGPL-2.1 text and FFmpeg's license/credit files are included under
`licenses/ffmpeg`.

### Binding written offer under LGPL-2.1 section 6(c)

Provider **Radek Cisar** offers every recipient of this Product the materials required by
LGPL-2.1 section 6(a) to relink the exact Product version with a recipient-modified FFmpeg:

1. the corresponding FFmpeg source used for the distributed binary, including Browser3
   modifications;
2. the relinkable object files for the rest of the application; and
3. `RELINK.md`, the build configuration, input inventory, and exact relinking procedure.

Request these materials by emailing **radekcisar77@gmail.com** and identify the exact
Product version shown in `MANIFEST.txt` or the archive name. Delivery may be by download
or physical media.

This offer remains valid for **at least three years from distribution** of the relevant
Product version. Any charge will not exceed the cost of physically performing distribution,
as permitted by LGPL-2.1 section 6(c). There is no charge for the source code itself.

The relinking procedure has been tested in practice: the release object bundle was relinked
with a rebuilt, modified FFmpeg and the resulting Product started and passed its media
preflight. See `licenses/ffmpeg/LGPL-COMPLIANCE.md` for the public technical record.

## PCI ID Repository data (BSD-3-Clause)

Copyright Martin Mares and Albert Pool.

Upstream: https://pci-ids.ucw.cz/

`resources/gpu_templates.json` contains selected PCI device identifiers, model names,
and chip names derived from PCI ID Repository snapshot 2026.07.21. Browser3 selected,
normalized, and combined these records with other public sources; the upstream database
is not distributed in full.

This distribution elects the repository's BSD-3-Clause option. The complete copyright
notice, conditions, and disclaimer are included at `licenses/pci-ids/LICENSE.txt`.

## BoringSSL (multiple permissive licenses), modified

Copyright the BoringSSL Authors and other holders identified in its source and license file.

Upstream: https://boringssl.googlesource.com/boringssl

Browser3 distributes a modified BoringSSL build as part of `chrome.dll`. Modified source
files carry prominent change notices. BoringSSL's complete bundled license, including the
applicable third-party terms, is included under `licenses/boringssl`.

## Microsoft Visual C++ Runtime

The Windows archive includes Microsoft Visual C++ runtime files redistributed under the
applicable Microsoft Visual Studio licensing terms. They are not modified by Browser3.

The Apache License 2.0 text is available at
https://www.apache.org/licenses/LICENSE-2.0 and is included with Browser3 public source.
