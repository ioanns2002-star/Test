"""Executed by Blender against the saved deliverable, not the generator's memory."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
scene = bpy.context.scene
assert scene.camera and scene.camera.name == "Camera_Reference", "Wrong startup camera"
assert scene.render.engine == "CYCLES", "Source scene must retain Cycles lighting"
assert len(scene.camera.data.background_images) == 0, "The reference is not a 3D backdrop"
assert scene.unit_settings.system == "METRIC"
objects = [o for o in scene.objects if o.type == "MESH"]
assert len(objects) > 1000, "Missing editable geometry"
assert len(bpy.data.materials) >= 30
images = [i for i in bpy.data.images if i.source == "FILE"]
assert len(images) >= 25, "Missing original PBR maps"
assert all(image.packed_file for image in images), "A texture was not packed into the blend"
assert all("reference" not in image.name.lower() for image in images), "Reference image must not stand in for geometry"
for obj in objects:
    assert obj.data.uv_layers, f"Missing UV map: {obj.name}"
    for vertex in obj.data.vertices:
        assert all(math.isfinite(v) for v in vertex.co), f"Non-finite vertex: {obj.name}"
emitters = [o for o in scene.objects if o.particle_systems]
assert len(emitters) == 1
assert emitters[0].particle_systems[0].settings.count == 6500
assert emitters[0].particle_systems[0].settings.instance_object is not None
bpy.context.view_layer.update()
report = {
    "verifiedAt": datetime.now(timezone.utc).isoformat(),
    "blenderVersion": bpy.app.version_string,
    "file": "public/downloads/rain-gallery.blend",
    "editableMeshes": len(objects),
    "packedImages": len(images),
    "materials": len(bpy.data.materials),
    "camera": scene.camera.name,
    "renderEngine": scene.render.engine,
    "resolution": [scene.render.resolution_x, scene.render.resolution_y],
    "cyclesSamples": scene.cycles.samples,
    "rainParticlesConfigured": emitters[0].particle_systems[0].settings.count,
    "rainParticlesEvaluated": len(emitters[0].evaluated_get(bpy.context.evaluated_depsgraph_get()).particle_systems[0].particles),
    "checks": ["Saved .blend opens in a fresh process", "Textures are packed", "All mesh coordinates finite", "Mesh UV maps present", "Reference camera selected", "Reference not used as a backdrop", "Native rain particle system configured"],
}
(ROOT / "docs").mkdir(exist_ok=True)
(ROOT / "docs/blender-verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
print("BLENDER_VERIFIED " + json.dumps(report))
