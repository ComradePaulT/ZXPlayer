# Third-party notices

ZXPlayer source code is licensed under `GPL-3.0-or-later`. The following
separately licensed components and data are used by or distributed alongside
the application. Their original licences continue to apply.

## Runtime dependencies

These dependencies are installed from Raspberry Pi OS packages and are not
vendored in this repository.

| Component | Licence | Project |
| --- | --- | --- |
| libspectrum | GPL-2.0-or-later | https://fuse-emulator.sourceforge.net/libspectrum.php |
| pygame | LGPL-2.1-or-later | https://www.pygame.org/ |
| NumPy | BSD-3-Clause | https://numpy.org/ |
| ALSA library | LGPL-2.1-or-later | https://www.alsa-project.org/ |

## Bundled font

`zxSpectrumStrict.ttf` is ZX Spectrum Strict, licensed under GPL-3.0. Its
source and attribution are recorded in `ZX-FONT-LICENSE.txt`; the licence text
is in `ZX-FONT-GPL-3.0.txt`.

## Bundled database

`artwork_catalog.json` is a derived database based on ZXDB and is distributed
under the Open Database License 1.0 (`ODbL-1.0`). Attribution, source details
and the licence URL are in `ZXDB-NOTICE.txt`. The database licence does not
grant rights to images or game files referenced by its metadata.

## Wordmarks

`sinclair-wordmark.png` and `zx-spectrum-wordmark.png` were derived from the
Wikimedia Commons sources identified in `DATACORDER-BRANDING-NOTICE.txt`.
Those source pages classify the simple text logos as public domain. Names and
marks may remain protected as trademarks.

## User-downloaded material

ZXPlayer can locate or download box scans, cassette scans and publisher logos
from third-party archives. Those files are not part of the licensed ZXPlayer
distribution and remain subject to their respective rights and archive terms.
Commercial cassette files and downloaded artwork are excluded by `.gitignore`.
