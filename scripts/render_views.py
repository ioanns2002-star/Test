"""Render alternative cameras from the saved source file without changing it."""

from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
scene = bpy.context.scene
scene.render.resolution_x = 1102
scene.render.resolution_y = 787
scene.render.resolution_percentage = 100
scene.cycles.samples = 80
scene.cycles.adaptive_threshold = .025
for camera, filename in [("Camera_CorridorDetail", "detail.png"), ("Camera_CityOverview", "city.png"), ("Camera_Reverse", "reverse.png")]:
    scene.camera = bpy.data.objects[camera]
    scene.render.filepath = str(ROOT / "public/assets" / filename)
    bpy.ops.render.render(write_still=True)
    print(f"Rendered {filename}", flush=True)
