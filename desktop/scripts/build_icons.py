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
- desktop/windows/Assets/winhand-offline.ico
    the same icon in grey, shown in the tray while the relay is not connected.

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


def check_filled(icon: Image.Image) -> None:
    """The icon must sit on a solid plate: only the rounded corners may be transparent."""
    alpha = np.asarray(icon.getchannel("A"))
    radius = round(CANVAS * 0.225)
    inset = round(CANVAS * 0.02)
    interior = alpha[inset + radius : CANVAS - inset - radius, inset + 2 : CANVAS - inset - 2]
    if interior.min() < 255 or alpha[inset + 2 : CANVAS - inset - 2, CANVAS // 2].min() < 255:
        raise ValueError("icon plate has transparent pixels inside; the taskbar would show through")


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


WIZARD = ROOT / "desktop/packaging/windows/wizard"
WIZARD_SCALES = (100, 125, 150, 175, 200, 225, 250)


def wizard_images(art: Image.Image, icon: Image.Image) -> list[str]:
    """Inno Setup wizard art (BMP, no alpha): the tall side panel of the welcome/finish pages
    and the small header image, at every scale Inno picks from for the screen's DPI."""
    WIZARD.mkdir(parents=True, exist_ok=True)
    written = []
    for scale in WIZARD_SCALES:
        w, h = round(164 * scale / 100), round(314 * scale / 100)
        ramp = np.linspace(0, 1, h)[:, None, None]
        top, bottom = np.array((150, 170, 240)), np.array(TILE_BOTTOM)
        panel = Image.fromarray(np.broadcast_to(top * (1 - ramp) + bottom * ramp, (h, w, 3)).astype(np.uint8))
        panel = panel.convert("RGBA")
        width = round(w * 0.92)
        figure = art.resize((width, round(art.height * width / art.width)), Image.Resampling.LANCZOS)
        panel.alpha_composite(figure, ((w - figure.width) // 2, h - figure.height - round(h * 0.06)))
        name = f"large-{scale}.bmp"
        panel.convert("RGB").save(WIZARD / name)
        written.append(name)

        size = round(55 * scale / 100)
        small = Image.new("RGBA", (size, size), (255, 255, 255, 255))  # the wizard header is white
        small.alpha_composite(icon.resize((size, size), Image.Resampling.LANCZOS))
        name = f"small-{scale}.bmp"
        small.convert("RGB").save(WIZARD / name)
        written.append(name)
    return written


def main() -> None:
    with Image.open(BRANDING / "source.webp") as source:
        rgb = np.asarray(source.convert("RGB")).astype(float)
    art = remove_checkerboard(rgb)
    mascot = centered(art, CANVAS, 0.96)
    icon = tile(art)
    check_filled(icon)

    WINDOWS_ASSETS.mkdir(parents=True, exist_ok=True)
    for folder in (BRANDING, WINDOWS_ASSETS):
        mascot.save(folder / "mascot.png", optimize=True)
        icon.save(folder / "winhand.png", optimize=True)
    (WINDOWS_ASSETS / "winhand.ico").write_bytes(windows_icon(icon))
    # tray icon while the relay is not connected: same plate, drained of colour
    offline = Image.merge("RGBA", (*icon.convert("LA").convert("RGB").split(), icon.getchannel("A")))
    offline = Image.blend(offline, Image.new("RGBA", offline.size, (150, 150, 150, 255)), 0.25)
    offline.putalpha(icon.getchannel("A"))
    (WINDOWS_ASSETS / "winhand-offline.ico").write_bytes(windows_icon(offline))
    wizard_images(art, icon)
    print("Generated mascot.png, winhand.png, winhand.ico, winhand-offline.ico and installer art; source artwork is unchanged.")


if __name__ == "__main__":
    main()
