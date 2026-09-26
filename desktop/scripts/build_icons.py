# /// script
# requires-python = ">=3.10"
# dependencies = ["Pillow==12.3.0", "numpy>=2", "scipy>=1.14"]
# ///
"""Derive the winhand mascot and icons from the owner's artwork.

The source is a sticker drawn on a *painted* transparency checkerboard (no alpha).
The checkerboard is removed by flooding the grey squares from the border (the
sticker's dark outline stops the flood), plus enclosed pockets that still show the
two-tone pattern. Outputs:

- assets/branding/mascot.png, desktop/windows/Assets/mascot.png
    transparent, for the bottom-left of the navigation pane.
- assets/branding/winhand.png, desktop/windows/Assets/winhand.png / winhand.ico
    the mascot on a filled rounded tile, for the taskbar, window, tray and installer.

Run: uv run desktop/scripts/build_icons.py
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as nd

ROOT = Path(__file__).resolve().parents[2]
BRANDING = ROOT / "assets/branding"
WINDOWS_ASSETS = ROOT / "desktop/windows/Assets"
WINDOWS_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
CANVAS = 1024
TILE_TOP, TILE_BOTTOM = (128, 152, 232), (104, 128, 216)  # periwinkle, same family as smart-search
OUTLINE = np.array([22, 32, 48], dtype=float)  # the sticker's navy outline


def remove_checkerboard(rgb: np.ndarray) -> Image.Image:
    luma = rgb @ np.array([0.299, 0.587, 0.114])
    sat = rgb.max(-1) - rgb.min(-1)
    grey = (sat < 22) & (luma > 95) & (luma < 225)
    labels, _ = nd.label(grey)
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))) - {0}
    bg = np.isin(labels, list(border))
    for index, box in enumerate(nd.find_objects(labels), 1):
        if index in border or box is None:
            continue
        component = labels[box] == index
        if component.sum() < 150:
            continue
        values = luma[box][component]
        # the checkerboard alternates ~130 and ~192 grey; artwork shading does not
        if ((values > 115) & (values < 150)).mean() > 0.2 and ((values > 175) & (values < 215)).mean() > 0.2:
            bg[box] |= component
    bg |= nd.binary_closing(bg, iterations=2)
    fg = ~bg

    # soften the outer edge: estimate coverage from how far a pixel is from the nearby grey
    inside = nd.distance_transform_edt(fg)
    band = fg & (inside <= 3)
    nearby_bg = nd.grey_dilation(np.where(bg, luma, 0), size=7)
    coverage = np.clip((nearby_bg - luma) / np.maximum(nearby_bg - OUTLINE.mean(), 1), 0, 1)
    alpha = fg.astype(float)
    alpha[band] = np.maximum(coverage[band], inside[band] >= 3)
    out = rgb.copy()
    out[band & (alpha < 1)] = OUTLINE  # edge pixels are outline mixed with grey: keep only the outline colour
    rgba = np.dstack([out, alpha * 255]).clip(0, 255).astype(np.uint8)
    image = Image.fromarray(rgba, "RGBA")
    return image.crop(image.getbbox())


def centered(image: Image.Image, size: int, fill: float, center_y: float = 0.5) -> Image.Image:
    canvas = Image.new("RGBA", (size, size))
    scale = fill * size / max(image.size)
    art = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    canvas.alpha_composite(art, ((size - art.width) // 2, round(size * center_y - art.height / 2)))
    return canvas


def tile(mascot: Image.Image) -> Image.Image:
    ramp = np.linspace(0, 1, CANVAS)[:, None, None]
    gradient = np.array(TILE_TOP) * (1 - ramp) + np.array(TILE_BOTTOM) * ramp
    background = Image.fromarray(np.broadcast_to(gradient, (CANVAS, CANVAS, 3)).astype(np.uint8)).convert("RGBA")
    mask = Image.new("L", (CANVAS, CANVAS))
    inset = round(CANVAS * 0.02)
    ImageDraw.Draw(mask).rounded_rectangle(
        (inset, inset, CANVAS - inset - 1, CANVAS - inset - 1), round(CANVAS * 0.225), fill=255
    )
    icon = Image.new("RGBA", (CANVAS, CANVAS))
    icon.paste(background, (0, 0), mask)
    icon.alpha_composite(centered(mascot, CANVAS, 0.84, 0.52))
    return icon


def windows_icon(image: Image.Image) -> bytes:
    frames = []
    for size in WINDOWS_SIZES:
        stream = io.BytesIO()
        image.resize((size, size), Image.Resampling.LANCZOS).save(stream, format="PNG")
        frames.append(stream.getvalue())
    directory = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    offset = 6 + 16 * len(frames)
    for size, frame in zip(WINDOWS_SIZES, frames):
        dimension = 0 if size == 256 else size
        directory.extend(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(frame), offset))
        offset += len(frame)
    return bytes(directory) + b"".join(frames)


def main() -> None:
    with Image.open(BRANDING / "source.webp") as source:
        rgb = np.asarray(source.convert("RGB")).astype(float)
    art = remove_checkerboard(rgb)
    mascot = centered(art, CANVAS, 0.96)
    icon = tile(art)

    WINDOWS_ASSETS.mkdir(parents=True, exist_ok=True)
    for folder in (BRANDING, WINDOWS_ASSETS):
        mascot.save(folder / "mascot.png", optimize=True)
        icon.save(folder / "winhand.png", optimize=True)
    (WINDOWS_ASSETS / "winhand.ico").write_bytes(windows_icon(icon))
    print("Generated mascot.png, winhand.png and winhand.ico; source artwork is unchanged.")


if __name__ == "__main__":
    main()
