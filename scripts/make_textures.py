"""Deterministic, original tileable PBR textures; no downloaded texture packs."""

from pathlib import Path
import json
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "textures"
SEED = 14092026
SIZE = 1024
RNG = np.random.default_rng(SEED)


def field(cells_x, cells_y, size=SIZE):
    values = RNG.random((cells_y, cells_x)).astype(np.float32)
    x = np.arange(size, dtype=np.float32) * cells_x / size
    y = np.arange(size, dtype=np.float32) * cells_y / size
    ix, iy = x.astype(int), y.astype(int)
    fx, fy = x - ix, y - iy
    fx = fx * fx * (3 - 2 * fx)
    fy = fy * fy * (3 - 2 * fy)
    a = values[iy[:, None] % cells_y, ix[None, :] % cells_x]
    b = values[iy[:, None] % cells_y, (ix[None, :] + 1) % cells_x]
    c = values[(iy[:, None] + 1) % cells_y, ix[None, :] % cells_x]
    d = values[(iy[:, None] + 1) % cells_y, (ix[None, :] + 1) % cells_x]
    return (a * (1 - fx) + b * fx) * (1 - fy[:, None]) + (c * (1 - fx) + d * fx) * fy[:, None]


def fractal():
    return sum(field(n, n) * weight for n, weight in [(3, .37), (9, .28), (28, .19), (90, .11), (270, .05)])


def image(name, array):
    Image.fromarray(np.clip(array, 0, 255).astype(np.uint8)).save(OUT / name, optimize=True)


def normal(height, strength):
    dx = (np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)) * strength
    dy = (np.roll(height, -1, axis=0) - np.roll(height, 1, axis=0)) * strength
    v = np.dstack((-dx, dy, np.ones_like(height)))
    v /= np.linalg.norm(v, axis=2, keepdims=True)
    return (v * .5 + .5) * 255


def surface(name, base, kind):
    f = fractal()
    fine = RNG.random((SIZE, SIZE))
    streak = np.maximum(0, field(110, 3) - .46) * field(13, 7)
    chips = (field(90, 90) > .86) * (field(12, 12) > .61)
    grain = (fine - .5) * 5.5
    shade = (f - .5) * 49 + grain
    height = f * .2 + fine * .012
    rough = np.clip(.74 + (f - .5) * .35, .45, .98)
    if kind == "paint":
        shade -= streak * 40 + chips * 16
        height += chips * .025
        rough = np.clip(rough + chips * .12, .45, 1)
    elif kind == "ceiling":
        peeled = (field(17, 17) > .73) * (field(45, 45) > .55)
        shade -= peeled * 30 + streak * 15
        height -= peeled * .025
    elif kind == "floor":
        wet = np.clip((field(7, 11) - .39) * 4.1, 0, 1)
        shade = (f - .5) * 28 - wet * 12 + grain * .5
        rough = .12 + (1 - wet) * .27 + (f - .5) * .04
        height = f * .16 + fine * .008
        ripple = np.zeros_like(f)
        yy, xx = np.mgrid[0:SIZE, 0:SIZE]
        for _ in range(32):
            cx, cy = RNG.integers(0, SIZE, 2)
            dx = np.minimum(np.abs(xx - cx), SIZE - np.abs(xx - cx))
            dy = np.minimum(np.abs(yy - cy), SIZE - np.abs(yy - cy))
            r = np.sqrt(dx * dx + dy * dy)
            radius = RNG.uniform(12, 76)
            ripple += np.sin(r * .52) * np.exp(-((r - radius) / 16) ** 2) * .008
        height += ripple * wet
    elif kind == "metal":
        rust = np.clip((field(35, 35) - .67) * 3, 0, 1)
        shade -= rust * 15
        height += rust * .018
        rough = .68 + rust * .28
    rgb = np.array(base, dtype=float)[None, None, :] + shade[:, :, None]
    if kind == "metal":
        rgb[:, :, 0] += rust * 12
        rgb[:, :, 2] -= rust * 11
    image(f"{name}_color.jpg", rgb)
    image(f"{name}_roughness.png", rough * 255)
    image(f"{name}_normal.png", normal(height, 7 if kind == "floor" else 4))


def exit_sign():
    img = Image.new("RGB", (512, 224), (102, 146, 77))
    d = ImageDraw.Draw(img)
    white = (217, 237, 186)
    d.rounded_rectangle((7, 7, 505, 217), radius=13, outline=(74, 100, 67), width=12)
    d.rectangle((323, 36, 391, 185), fill=white)
    d.rectangle((337, 49, 381, 185), fill=(80, 125, 68))
    d.ellipse((239, 39, 269, 69), fill=white)
    d.line([(249, 80), (220, 119), (279, 136), (293, 183)], fill=white, width=20)
    d.line([(241, 81), (286, 103), (318, 94)], fill=white, width=16)
    d.line([(225, 113), (211, 153), (177, 183)], fill=white, width=19)
    d.line([(225, 86), (199, 99), (189, 120)], fill=white, width=15)
    d.polygon([(39, 108), (90, 64), (90, 90), (163, 90), (163, 125), (90, 125), (90, 152)], fill=white)
    img.save(OUT / "exit_sign.png")


def dirty_glass():
    f = fractal()
    streak = field(200, 4)
    rgb = np.zeros((SIZE, SIZE, 3))
    rgb[:] = [41, 53, 54]
    rgb += ((f - .5) * 15 + (streak - .5) * 9)[:, :, None]
    image("glass_color.jpg", rgb)


def reference():
    original = ROOT / ".hoplite/attachments/art_upload_3d3ab601236a4b74818b81b47938d312/b963a78c1ce5ec5ce77c883144794a02.jpg"
    target = ROOT / "public/assets/reference.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    if original.exists() and not target.exists():
        target.write_bytes(original.read_bytes())


def main():
    OUT.mkdir(exist_ok=True)
    surface("plaster", [140, 147, 148], "paint")
    surface("lower_paint", [57, 76, 74], "paint")
    surface("ceiling", [102, 113, 108], "ceiling")
    surface("parapet", [64, 64, 66], "paint")
    surface("floor", [67, 70, 76], "floor")
    surface("facade", [176, 178, 177], "paint")
    surface("door", [98, 105, 105], "metal")
    surface("pipe", [101, 111, 114], "metal")
    exit_sign()
    dirty_glass()
    reference()
    (OUT / "manifest.json").write_text(json.dumps({"seed": SEED, "resolution": SIZE, "generator": "scripts/make_textures.py", "source": "original deterministic procedural textures"}, indent=2) + "\n")
    print(f"Generated {len(list(OUT.glob('*')))} texture files in {OUT}")


if __name__ == "__main__":
    main()
