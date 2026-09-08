from pathlib import Path
import json

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build"


def main():
    OUT.mkdir(exist_ok=True)
    a = Image.open(ROOT / "public/assets/reference.jpg").convert("RGB")
    b = Image.open(ROOT / "public/assets/beauty.png").convert("RGB").resize(a.size, Image.Resampling.LANCZOS)
    for name, img in [("reference-grid", a), ("render-grid", b)]:
        grid = img.copy()
        d = ImageDraw.Draw(grid)
        for x in range(0, a.width, 50):
            d.line((x, 0, x, a.height), fill=(151, 101, 65), width=1)
            d.text((x + 2, 8), str(x), fill="white", stroke_width=1, stroke_fill="black")
        for y in range(0, a.height, 50):
            d.line((0, y, a.width, y), fill=(151, 101, 65), width=1)
            d.text((3, y + 2), str(y), fill="white", stroke_width=1, stroke_fill="black")
        grid.save(OUT / f"{name}.png")
    Image.blend(a, b, .5).save(OUT / "alignment-overlay.png")
    regions = {"sky": (640, 15, 730, 150), "left plaster": (7, 211, 34, 258), "parapet": (450, 430, 650, 500), "floor": (160, 406, 278, 515), "main facade": (580, 210, 730, 305)}
    print(json.dumps({name: {"referenceMeanRGB": np.array(a.crop(bounds)).mean(axis=(0, 1)).round(1).tolist(), "renderMeanRGB": np.array(b.crop(bounds)).mean(axis=(0, 1)).round(1).tolist()} for name, bounds in regions.items()}, indent=2))


if __name__ == "__main__":
    main()
