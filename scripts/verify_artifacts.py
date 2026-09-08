"""Fast standard-library checks suitable for CI; Blender is checked separately."""

import argparse
import base64
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import struct
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"


class OfflinePage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.modules = []
        self.module_parts = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        assert not (tag == "script" and "src" in attrs), "Offline page references an external script"
        assert not (tag == "link" and attrs.get("rel") == "stylesheet"), "Offline page references external CSS"
        if tag == "script" and attrs.get("type") == "module":
            self.module_parts = []

    def handle_data(self, data):
        if self.module_parts is not None:
            self.module_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.module_parts is not None:
            self.modules.append("".join(self.module_parts))
            self.module_parts = None


def main(write_catalog=False):
    model = (PUBLIC / "assets/rain-gallery.glb").read_bytes()
    magic, version, length = struct.unpack_from("<III", model)
    assert magic == 0x46546C67 and version == 2 and length == len(model)
    chunk_length, chunk_type = struct.unpack_from("<II", model, 12)
    assert chunk_type == 0x4E4F534A
    gltf = json.loads(model[20:20 + chunk_length])
    assert len(gltf["meshes"]) >= 30
    assert len(gltf["materials"]) >= 25
    assert any(node.get("name") == "Camera_Reference" for node in gltf["nodes"])
    assert all("uri" not in image for image in gltf["images"]), "GLB has external images"
    assert all("uri" not in buffer for buffer in gltf["buffers"]), "GLB has external buffers"
    manifest = json.loads((PUBLIC / "assets/scene-manifest.json").read_text())
    triangles = sum(gltf["accessors"][p["indices"]]["count"] // 3 for mesh in gltf["meshes"] for p in mesh["primitives"])
    assert triangles == manifest["stats"]["triangles"]
    html = (PUBLIC / "downloads/gallery-offline.html").read_text()
    page = OfflinePage()
    page.feed(html)
    assert len(page.modules) == 1, "Offline module missing or duplicated"
    syntax = subprocess.run(
        ["node", "--input-type=module", "--check"], input=page.modules[0],
        text=True, capture_output=True, timeout=30,
    )
    assert syntax.returncode == 0, f"Invalid offline JavaScript:\n{syntax.stderr[-2000:]}"
    marker = "<script>window.__SCENE_EMBED__="
    assert html.count(marker) == 1, "Offline payload missing or duplicated"
    embedded, _ = json.JSONDecoder().raw_decode(html.split(marker, 1)[1])
    assert embedded["manifest"] == manifest, "Offline manifest is stale"
    assert base64.b64decode(embedded["glbBase64"], validate=True) == model, "Offline GLB is stale"
    for key, path, mime in [("reference", "reference.jpg", "image/jpeg"), ("beauty", "beauty.webp", "image/webp")]:
        assert embedded[f"{key}MimeType"] == mime
        assert base64.b64decode(embedded[f"{key}Base64"], validate=True) == (PUBLIC / "assets" / path).read_bytes(), f"Offline {key} is stale"
    with zipfile.ZipFile(PUBLIC / "downloads/godot-gallery.zip") as archive:
        assert archive.testzip() is None
        for name in ("project.godot", "main.tscn", "scripts/main.gd", "tests/verify_scene.gd"):
            assert archive.read(f"rain-gallery-godot/{name}") == (ROOT / "godot" / name).read_bytes(), f"Missing or stale Godot runtime: {name}"
        assert archive.read("rain-gallery-godot/README.md") == (ROOT / "docs/GODOT.md").read_bytes(), "Godot instructions are stale"
        assert archive.read("rain-gallery-godot/assets/scene.glb") == model
        assert json.loads(archive.read("rain-gallery-godot/assets/scene-manifest.json")) == manifest
        assert not any("/.godot/" in name for name in archive.namelist())
    files = ["downloads/rain-gallery.blend", "downloads/gallery-offline.html", "downloads/godot-gallery.zip", "assets/rain-gallery.glb", "assets/beauty.webp", "assets/detail.webp", "assets/city.webp", "assets/reverse.webp"]
    catalog = []
    for path in files:
        file = PUBLIC / path
        assert file.is_file() and file.stat().st_size > 1000, f"Missing or empty artifact: {path}"
        assert file.stat().st_size < 95 * 1024 * 1024, f"Artifact exceeds GitHub file limit: {path}"
        with file.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        catalog.append({"path": path, "bytes": file.stat().st_size, "sha256": digest})
    target = PUBLIC / "assets/artifact-manifest.json"
    report = {"artifacts": catalog, "geometryTriangles": triangles, "glbSelfContained": True}
    if write_catalog:
        target.write_text(json.dumps(report, indent=2) + "\n")
    else:
        assert target.is_file(), "Missing artifact catalog; run npm run build:catalog"
        assert json.loads(target.read_text()) == report, "Stale artifact catalog; run npm run build:catalog"
    print(f"Verified {len(catalog)} deliverables; {triangles:,} triangles; Godot GLB byte-identical; offline payload embedded")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Generate the catalog after rebuilding assets")
    main(write_catalog=parser.parse_args().write)
