from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "public/assets"


def main():
    for name in ("beauty", "detail", "city", "reverse"):
        source = ASSETS / f"{name}.png"
        if source.exists():
            image = Image.open(source).convert("RGB")
            image.save(ASSETS / f"{name}.webp", quality=95, method=6)
    ref = Image.open(ASSETS / "reference.jpg").convert("RGB")
    render = Image.open(ASSETS / "beauty.png").convert("RGB").resize(ref.size, Image.Resampling.LANCZOS)
    sheet = Image.new("RGB", (ref.width * 2 + 36, ref.height + 105), (16, 23, 25))
    d = ImageDraw.Draw(sheet)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font = ImageFont.truetype(font_path, 16) if Path(font_path).exists() else ImageFont.load_default()
    d.text((14, 17), "REFERENCE / USER IMAGE", fill=(165, 190, 190), font=font)
    d.text((ref.width + 28, 17), "RECONSTRUCTION / BLENDER CYCLES", fill=(165, 190, 190), font=font)
    sheet.paste(ref, (12, 50))
    sheet.paste(render, (ref.width + 24, 50))
    d.text((14, ref.height + 67), "Single-image interpretation. Real geometry; no photographic backdrop. Hidden surfaces are inferred.", fill=(139, 155, 159), font=font)
    sheet.save(ASSETS / "comparison.webp", quality=94, method=6)
    print("Prepared web images and honest side-by-side comparison")


if __name__ == "__main__":
    main()
