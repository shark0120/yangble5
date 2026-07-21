# assets/

Self-authored artwork for this repository. No external image hosts, no binary blobs, no
third-party logos.

| File | What it is |
|---|---|
| `social-preview.svg` | 1280x640 social card. Plain SVG, system fonts only, no external references, no embedded raster data. |

---

## social-preview.svg

Every claim on the card carries its own qualifier, because a social card is exactly where
numbers get quoted without their footnotes:

| On the card | What it actually means | Source |
|---|---|---|
| **99.53%** prompt-cache hit rate, token-weighted, **WARM rounds only (2-4 of 4)** | Rounds 2, 3 and 4 of a 4-round session. `2,236,290 / 2,246,844`. Fold the cold round back in and the same run reports 74.6%. Prefix-size dependent: a ~91K prefix measured 94.00%. | [FINDINGS.md](../docs/FINDINGS.md#finding-2-9953-token-weighted-prompt-cache-hit-rate-on-warm-rounds) |
| **748,918** tokens in one prompt, ingested with no truncation, **recall at that size not tested** | The upstream's own `promptTokenCount`, relayed as `input_tokens`. Ingestion only - no needle-in-a-haystack test was run. We did **not** verify 1,000,000. | [FINDINGS.md](../docs/FINDINGS.md#finding-3-748918-tokens-ingested-with-no-truncation) |
| **0%** on round 1 of every session | The first request of any session is a cold cache write. True by construction, not by failure. No configuration removes it. | [cache-lifecycle.md](../docs/diagrams/cache-lifecycle.md) |

Footer, present on the card itself: measured 2026-07-21 on **one** Windows 11 machine, one run
per configuration, no repetitions and no error bars; built on
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (third-party, MIT); and **no live web
search** goes through this proxy.

If you edit the numbers, edit the qualifiers with them. A number without its qualifier is not a
shorter version of the truth.

---

## Converting to PNG for GitHub's social-preview field

GitHub's **Settings -> General -> Social preview** field does **not** accept SVG. It takes PNG,
JPG or GIF, at most 1 MB, and recommends 1280x640 - which is exactly the size this SVG is
authored at, so no scaling is needed. Pick whichever converter you already have:

```bash
# librsvg (best fidelity for this file; handles the <style> block correctly)
rsvg-convert -w 1280 -h 640 assets/social-preview.svg -o social-preview.png

# Inkscape 1.x
inkscape assets/social-preview.svg --export-type=png --export-filename=social-preview.png \
         --export-width=1280 --export-height=640

# ImageMagick (delegates to librsvg or Inkscape if installed; its internal MSVG
# renderer ignores CSS and will render this card as unstyled black text - check the output)
magick -background none -density 96 assets/social-preview.svg -resize 1280x640 social-preview.png

# Python, no system tooling
pip install cairosvg
python -c "import cairosvg; cairosvg.svg2png(url='assets/social-preview.svg', write_to='social-preview.png', output_width=1280, output_height=640)"
```

Headless Chrome also works and is the closest match to what a browser shows:

```bash
chrome --headless --disable-gpu --screenshot=social-preview.png \
       --window-size=1280,640 --default-background-color=00000000 \
       file://$PWD/assets/social-preview.svg
```

Then upload `social-preview.png` in **Settings -> General -> Social preview -> Upload an image**.

**Do not commit the PNG.** It is a derived artifact; the SVG is the source. Regenerate it when
the card changes.

### Things that will bite you

* **Styling comes from a `<style>` block, not presentation attributes.** Converters with no CSS
  support (notably ImageMagick's built-in MSVG renderer) drop every fill, size and weight and
  emit unstyled black-on-black text. Always look at the PNG before uploading it. If your
  converter mangles it, use `rsvg-convert` or headless Chrome.
* **Fonts are resolved by the converting machine, not embedded.** The stack is
  `Segoe UI -> -apple-system / BlinkMacSystemFont -> Helvetica Neue -> Arial -> Liberation Sans`,
  so a Windows, macOS or normally-provisioned Linux box all land on something sensible. A
  minimal container with no fonts installed will substitute something ugly or draw nothing.
  Install `fonts-liberation` (Debian/Ubuntu) or `liberation-fonts` (Fedora) in that case.
* **The card is deliberately ASCII-only.** A Traditional Chinese line would read well here, but
  it renders as tofu boxes on any converter without a CJK font installed - which is most CI
  containers. If you want a Chinese variant, make it a second file and install
  `fonts-noto-cjk` in whatever converts it.
* **Legibility when shrunk.** Link unfurls in Slack, Discord and X render this card far below
  1280px wide. The smallest text on it is 18px at authored size, and the three numbers are 58px,
  so the numbers survive a 4x downscale even when the footnote text does not. That is the
  intended reading order: the number, then the qualifier next to it, then the footer.
