"""Derive every TRINDATRON brand asset the Mobile app ships from the official master artwork.

Nothing is redrawn. The master symbol (assets/brand/trindatron-symbol.png, 792x545)
keeps its original studio RGB; only its alpha matte was defective (hard, ragged
edges that kept part of the studio shadow). This script:

  1. re-segments the master with a salient-object model (BiRefNet) and
     decontaminates edge colours. That untouched result is kept as
     trindatron-symbol-matte.png, so the later steps never need the model again;
  2. cleans the matte's edge band. The segmentation left a faint outer haze of
     studio background (a light halo on dark surfaces) and edge pixels still
     mixed with that light background. The haze is dropped and every edge pixel
     takes the colour of the chrome right next to it;
  3. downscales (premultiplied Lanczos, never upscales) to UI sizes dense enough
     that no screen shrinks a file by more than ~1.3x on the GPU, which is what
     made thin cyan lines look jagged in the header;
  4. cuts the wordmark out of the official lockup (logo-oficial-lockup.jpg);
  5. splits the symbol into the two motion layers of the opening animation along a
     hard partition, so both layers at rest recompose the symbol pixel for pixel;
  6. composes the store icon, the Android adaptive foreground and the favicon.

The master file itself is never written.

Usage (from packages/mobile):
  pip install pillow numpy "rembg[cpu]" pymatting
  python scripts/brand/derive_brand_assets.py --preview <dir>        # look first
  python scripts/brand/derive_brand_assets.py --write                # then ship
  # steps 2-6 from the kept matte; needs only Pillow:
  python scripts/brand/derive_brand_assets.py --from-matte --preview <dir>
  python scripts/brand/derive_brand_assets.py --from-matte --write
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

MOBILE = Path(__file__).resolve().parents[2]
BRAND = MOBILE / "assets" / "brand"
MASTER_SYMBOL = BRAND / "trindatron-symbol.png"
MATTE = BRAND / "trindatron-symbol-matte.png"
MASTER_LOCKUP = MOBILE.parent / "web" / "public" / "brand" / "master" / "source" / "logo-oficial-lockup.jpg"

BACKGROUND = (7, 8, 10)  # theme bg / splash / adaptive icon background
CYAN = (0, 194, 212)  # official accent
SEGMENTATION_MODEL = "birefnet-general-lite"

# The lower loop of the symbol, in master pixels. Everything inside moves as the
# second element of the opening animation; the rest (the upper infinity, including
# the interlaced centre) is the first. A hard partition: at rest the two layers are
# exactly the symbol.
LOWER_LOOP_POLYGON = [(262, 545), (262, 300), (300, 262), (338, 252), (445, 262), (505, 300), (540, 545)]

# Matte alpha at or below HAZE is studio background; at or above SOLID it is chrome.
HAZE = 36
SOLID = 232

UI_WIDTHS = (48, 64, 80, 96, 128, 160, 192, 256, 320, 384, 512)
ICON_MARK_WIDTH = 540  # inside 1024; below the master's own width, so no upscaling
ADAPTIVE_MARK_WIDTH = 500  # inside Android's 66% safe zone
FAVICON_MARK_WIDTH = 26


def premultiplied_resize(image: Image.Image, width: int) -> Image.Image:
    if width > image.width:
        raise ValueError(f"refusing to upscale {image.width}px to {width}px")
    height = round(image.height * width / image.width)
    return image.convert("RGBa").resize((width, height), Image.LANCZOS).convert("RGBA")


def matte_master() -> Image.Image:
    import numpy as np
    from pymatting import estimate_foreground_ml
    from rembg import new_session, remove

    rgb_image = Image.open(MASTER_SYMBOL).convert("RGB")
    rgb = np.asarray(rgb_image).astype(np.float64) / 255.0
    mask = remove(rgb_image, session=new_session(SEGMENTATION_MODEL), only_mask=True, post_process_mask=False)
    alpha = np.asarray(mask).astype(np.float64) / 255.0
    alpha[alpha < 0.02] = 0.0
    alpha[alpha > 0.98] = 1.0
    foreground = np.clip(estimate_foreground_ml(rgb, alpha), 0.0, 1.0)
    rgba = np.dstack([foreground, alpha])
    return Image.fromarray((rgba * 255).round().astype(np.uint8), "RGBA")


def trim(image: Image.Image, pad: int) -> tuple[Image.Image, tuple[int, int]]:
    left, top, right, bottom = image.getbbox()
    left, top = max(0, left - pad), max(0, top - pad)
    right, bottom = min(image.width, right + pad), min(image.height, bottom + pad)
    return image.crop((left, top, right, bottom)), (left, top)


def locate(matte: Image.Image) -> tuple[int, int]:
    """Where the trimmed matte sits inside the master: the offset at which its solid chrome matches the master's RGB."""
    master = Image.open(MASTER_SYMBOL).convert("RGB")
    target = matte.convert("RGB")
    solid = matte.getchannel("A").point(lambda v: 255 if v == 255 else 0)

    def error(left: int, top: int) -> float:
        crop = master.crop((left, top, left + matte.width, top + matte.height))
        difference = ImageChops.difference(crop, target).convert("L")
        return ImageStat.Stat(difference, mask=solid).mean[0]

    span_x, span_y = master.width - matte.width, master.height - matte.height
    coarse = min(((error(x, y), x, y) for x in range(0, span_x + 1, 4) for y in range(0, span_y + 1, 2)))
    _, cx, cy = coarse
    fine = min((error(x, y), x, y) for x in range(max(0, cx - 4), min(span_x, cx + 4) + 1) for y in range(max(0, cy - 2), min(span_y, cy + 2) + 1))
    return fine[1], fine[2]


def clean_edges(matte: Image.Image) -> Image.Image:
    """Drop the matte's outer haze and give every edge pixel the colour of the chrome beside it."""
    red, green, blue, alpha = matte.convert("RGBA").split()
    span = SOLID - HAZE
    alpha = alpha.point(lambda v: 0 if v <= HAZE else 255 if v >= SOLID else round((v - HAZE) * 255 / span))

    solid = alpha.point(lambda v: 255 if v == 255 else 0)
    # Averaging only solid pixels: blur premultiplied by the solid mask, then un-premultiply.
    bleed = Image.merge("RGBa", [ImageChops.multiply(channel, solid) for channel in (red, green, blue)] + [solid])
    nearby = bleed.filter(ImageFilter.GaussianBlur(3)).convert("RGBA").split()[:3]
    edge = alpha.point(lambda v: 255 if 0 < v < 255 else 0)
    channels = [Image.composite(near, own, edge) for near, own in zip(nearby, (red, green, blue))]
    return Image.merge("RGBA", (*channels, alpha))


def split_layers(symbol: Image.Image, offset: tuple[int, int]) -> tuple[Image.Image, Image.Image]:
    lower_mask = Image.new("L", symbol.size, 0)
    ImageDraw.Draw(lower_mask).polygon([(x - offset[0], y - offset[1]) for x, y in LOWER_LOOP_POLYGON], fill=255)
    alpha = symbol.getchannel("A")
    upper, lower = symbol.copy(), symbol.copy()
    upper.putalpha(ImageChops.multiply(alpha, ImageChops.invert(lower_mask)))
    lower.putalpha(ImageChops.multiply(alpha, lower_mask))
    return upper, lower


def wordmark() -> Image.Image:
    import numpy as np

    image = np.asarray(Image.open(MASTER_LOCKUP).convert("RGB")).astype(np.float64)
    luminance = image @ np.array([0.2126, 0.7152, 0.0722])
    height = luminance.shape[0]
    start = int(height * 0.65)
    ink_rows = np.where((luminance[start:] < 45).sum(axis=1) > 12)[0] + start
    runs, run = [], [ink_rows[0]]
    for row in ink_rows[1:]:
        if row == run[-1] + 1:
            run.append(row)
        else:
            runs.append(run)
            run = [row]
    runs.append(run)
    text_rows = max(runs, key=len)
    top, bottom = text_rows[0], text_rows[-1]
    columns = np.where((luminance[top : bottom + 1] < 45).any(axis=0))[0]
    pad = 4
    crop = luminance[top - pad : bottom + pad + 1, columns[0] - pad : columns[-1] + pad + 1]
    paper = np.percentile(crop, 90)
    ink = np.percentile(crop, 1)
    alpha = np.clip((paper - crop) / (paper - ink), 0.0, 1.0)
    # The faint reflection of the symbol above the letters is not ink.
    alpha = np.clip((alpha - 0.12) / 0.88, 0.0, 1.0)
    out = np.zeros(crop.shape + (4,), np.uint8)
    out[..., :3] = 255  # tinted at runtime (theme text colour)
    out[..., 3] = (alpha * 255).round().astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def glow(size: int = 256) -> Image.Image:
    import numpy as np

    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    r = np.hypot(x - (size - 1) / 2, y - (size - 1) / 2) / (size / 2)
    alpha = np.clip(np.exp(-4.0 * r * r) - np.exp(-4.0), 0, 1) / (1 - np.exp(-4.0))
    out = np.zeros((size, size, 4), np.uint8)
    out[..., 0], out[..., 1], out[..., 2] = CYAN
    out[..., 3] = (alpha * 255).round().astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def compose(mark: Image.Image, canvas: int, mark_width: int, background: tuple[int, int, int] | None) -> Image.Image:
    fill = background + (255,) if background else (0, 0, 0, 0)
    base = Image.new("RGBA", (canvas, canvas), fill)
    scaled = premultiplied_resize(mark, mark_width)
    base.alpha_composite(scaled, ((canvas - scaled.width) // 2, (canvas - scaled.height) // 2))
    return base.convert("RGB") if background else base


def build(from_matte: bool) -> dict[str, Image.Image]:
    assets: dict[str, Image.Image] = {}
    if from_matte:
        matte = Image.open(MATTE).convert("RGBA")
        offset = locate(matte)
    else:
        matte, offset = trim(matte_master(), pad=6)
        assets["trindatron-symbol-matte.png"] = matte
        assets["trindatron-wordmark.png"] = wordmark()
        assets["trindatron-glow.png"] = glow()

    symbol = clean_edges(matte)
    upper, lower = split_layers(symbol, offset)
    assets.update({
        "trindatron-symbol-hd.png": symbol,
        "trindatron-intro-upper.png": upper,
        "trindatron-intro-lower.png": lower,
        "trindatron-icon-1024.png": compose(symbol, 1024, ICON_MARK_WIDTH, BACKGROUND),
        "trindatron-adaptive-foreground.png": compose(symbol, 1024, ADAPTIVE_MARK_WIDTH, None),
        "trindatron-icon-32.png": compose(symbol, 32, FAVICON_MARK_WIDTH, BACKGROUND),
    })
    for width in UI_WIDTHS:
        assets[f"trindatron-symbol-{width}.png"] = premultiplied_resize(symbol, width)
    return assets


def on_ground(image: Image.Image, ground: tuple[int, int, int]) -> Image.Image:
    base = Image.new("RGBA", image.size, ground + (255,))
    base.alpha_composite(image.convert("RGBA"))
    return base.convert("RGB")


def preview(assets: dict[str, Image.Image], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    symbol = assets["trindatron-symbol-hd.png"]
    on_ground(symbol, BACKGROUND).save(out / "preview-symbol-dark.png")
    on_ground(symbol, (238, 241, 244)).save(out / "preview-symbol-light.png")
    dark = on_ground(symbol, BACKGROUND)
    for name, box in {"edge-topleft": (0, 60, 140, 200), "edge-bottom": (170, 400, 330, 541)}.items():
        dark.crop(box).resize(((box[2] - box[0]) * 4, (box[3] - box[1]) * 4), Image.NEAREST).save(out / f"preview-{name}-x4.png")
    for width in (80, 128):
        small = on_ground(assets[f"trindatron-symbol-{width}.png"], BACKGROUND)
        small.resize((small.width * 6, small.height * 6), Image.NEAREST).save(out / f"preview-symbol-{width}-x6.png")
    split = Image.new("RGBA", symbol.size, BACKGROUND + (255,))
    tinted_lower = assets["trindatron-intro-lower.png"].copy()
    tint = Image.new("RGBA", symbol.size, (255, 60, 60, 110))
    tinted_lower = Image.composite(Image.alpha_composite(tinted_lower, tint), tinted_lower, tinted_lower.getchannel("A"))
    split.alpha_composite(assets["trindatron-intro-upper.png"])
    split.alpha_composite(tinted_lower)
    split.convert("RGB").save(out / "preview-intro-split.png")
    if "trindatron-wordmark.png" in assets:
        on_ground(assets["trindatron-wordmark.png"], BACKGROUND).save(out / "preview-wordmark.png")
    assets["trindatron-icon-1024.png"].save(out / "preview-icon-1024.png")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--from-matte", action="store_true", help="start from the kept matte (Pillow only)")
    args = parser.parse_args()

    assets = build(args.from_matte)
    if args.preview:
        preview(assets, args.preview)
    if args.write:
        for name, image in assets.items():
            image.save(BRAND / name, optimize=True)
    print(json.dumps({name: image.size for name, image in assets.items()}, indent=2))


if __name__ == "__main__":
    main()
