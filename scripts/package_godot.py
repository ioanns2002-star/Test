"""Copy the shared export and create a portable Godot project, without its cache."""

from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / "godot"


def main():
    assets = GAME / "assets"
    assets.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "public/assets/rain-gallery.glb", assets / "scene.glb")
    shutil.copy2(ROOT / "public/assets/scene-manifest.json", assets / "scene-manifest.json")
    target = ROOT / "public/downloads/godot-gallery.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(GAME.rglob("*")):
            relative = file.relative_to(GAME)
            if file.is_file() and ".godot" not in relative.parts and not file.name.endswith((".import", ".tmp")) and file.name != ".gitignore":
                archive.write(file, Path("rain-gallery-godot") / relative)
        archive.write(ROOT / "docs/GODOT.md", "rain-gallery-godot/README.md")
    print(f"Packaged {target.name}: {target.stat().st_size / 1024 / 1024:.1f} MiB")


if __name__ == "__main__":
    main()
