# Browser3 FFmpeg Patch

`ffmpeg-l2-no-proprietary-decoders.patch` applies to the Chromium FFmpeg submodule based on
upstream revision `f45bab87ce4c5fafc67fd53fcde777578d01bfa0`.

The patch removes the AAC decoder and H.264 decoder/parser from the compiled source list.
It is retained so the modified source used by Browser3 can be independently reconstructed.
The full corresponding source, rather than the patch alone, is available under the LGPL
written offer in the Third-Party Notices document (`THIRD_PARTY_NOTICES.txt` in the
binary package and `THIRD_PARTY_NOTICES.md` in the public source snapshot).

The patch is packaged at `licenses/ffmpeg/ffmpeg-l2-no-proprietary-decoders.patch`.
Apply it to the pinned FFmpeg submodule from any Chromium checkout; no Browser3-private
directory layout is required:

```powershell
$packageRoot = "C:\path\to\extracted\Browser3"
$chromiumSrc = "C:\path\to\chromium\src"
git -C "$chromiumSrc\third_party\ffmpeg" apply "$packageRoot\licenses\ffmpeg\ffmpeg-l2-no-proprietary-decoders.patch"
```

Recipients using the companion LGPL source/object bundle should follow its `RELINK.md`
to relink `chrome.dll` with a modified FFmpeg build.
