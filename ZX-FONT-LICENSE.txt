# ZX Spectrum Strict

![Font grid](grid.png)

- Font name: ZX Spectrum Strict
- Source URL: https://github.com/48BASIC/zx-spectrum-font
- License and exact licensing notes: GPL-3.0
- Format type: TTF
- Approximate glyph size: 8x8
- Character coverage if known: ASCII + ZX-derived set
- Whether it is monospaced: Yes
- stroke-efficiency (1-10): 9
- Any caveats: Faithful 8-bit look; uppercase-biased style can feel blocky.

## Preview generation

```bash
./tools/snatch-1.0.0-linux-x86_64/snatch \
  --plugin-dir ./tools/snatch-1.0.0-linux-x86_64/plugins \
  --extractor ttf_extractor \
  --extractor-parameters "input=source/fonts/zx_spectrum_strict/zxSpectrumStrict.ttf,first_ascii=32,last_ascii=126,font_size=8" \
  --exporter png \
  --exporter-parameters "output=source/fonts/zx_spectrum_strict/grid.png,columns=16,rows=6" 
```
