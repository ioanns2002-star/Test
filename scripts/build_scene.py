"""Build the editable scene, render it in Cycles, and export a batched glTF copy.

blender -b -t 4 --python scripts/build_scene.py -- --quality draft
"""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
import sys
import time

import bpy
from mathutils import Matrix, Vector


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "public/assets"
DOWNLOADS = ROOT / "public/downloads"
TEXTURES = ROOT / "textures"
SEED = 14092026
RNG = random.Random(SEED)
COLLECTIONS = {}
MATERIALS = {}
WIDTH = 1.70
LENGTH = 20.0
CEILING = 3.40
CAMERA_POSITION = (.77, 0.0, 1.65)
CAMERA_TARGET = (4.66, 18.7, .92)


def rgb(hex_color):
    h = hex_color.lstrip("#")
    srgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in srgb) + (1,)


def collection(name):
    if name not in COLLECTIONS:
        c = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(c)
        COLLECTIONS[name] = c
    return COLLECTIONS[name]


def attach(obj, group):
    collection(group).objects.link(obj)
    return obj


def material(name, color, roughness=.65, metallic=0, texture=None, coat=0, emission=None, strength=0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    p = m.node_tree.nodes.get("Principled BSDF")
    p.inputs["Base Color"].default_value = rgb(color)
    p.inputs["Roughness"].default_value = roughness
    p.inputs["Metallic"].default_value = metallic
    p.inputs["Coat Weight"].default_value = coat
    p.inputs["Coat Roughness"].default_value = .13 if texture == "floor" else .17
    m.diffuse_color = rgb(color)
    if emission:
        p.inputs["Emission Color"].default_value = rgb(emission)
        p.inputs["Emission Strength"].default_value = strength
    if texture:
        for suffix, colorspace, target in [("color.jpg", "sRGB", "Base Color"), ("roughness.png", "Non-Color", "Roughness"), ("normal.png", "Non-Color", "Normal")]:
            path = TEXTURES / f"{texture}_{suffix}"
            if not path.exists():
                continue
            tex = m.node_tree.nodes.new("ShaderNodeTexImage")
            tex.image = bpy.data.images.load(str(path), check_existing=True)
            tex.image.colorspace_settings.name = colorspace
            tex.label = f"Original procedural {target}"
            if target == "Normal":
                n = m.node_tree.nodes.new("ShaderNodeNormalMap")
                n.inputs["Strength"].default_value = .6 if texture == "floor" else .45
                m.node_tree.links.new(tex.outputs["Color"], n.inputs["Color"])
                m.node_tree.links.new(n.outputs["Normal"], p.inputs["Normal"])
                m.node_tree.links.new(n.outputs["Normal"], p.inputs["Coat Normal"])
            else:
                m.node_tree.links.new(tex.outputs["Color"], p.inputs[target])
    MATERIALS[name] = m
    return m


def make_materials():
    material("Plaster · rain stained", "7b8985", texture="plaster")
    material("Paint · petrol lower band", "394c4a", texture="lower_paint", coat=.18)
    material("Ceiling · peeling mineral paint", "66716c", texture="ceiling")
    material("Parapet · saturated concrete", "3d4f4d", texture="parapet", coat=.08)
    material("Floor · uneven wet concrete", "323e3c", texture="floor", coat=.75)
    material("Facade · chalky concrete", "98a5a0", texture="facade")
    material("Door · worn enamel", "586963", texture="door", metallic=.25)
    material("Pipe · galvanized grey", "656f72", texture="pipe", metallic=.12)
    material("Steel · oxidised", "343d3b", .59, .63)
    material("Hardware · worn nickel", "7c8580", .29, .84)
    material("Rust · exposed fasteners", "51443a", .93, .18)
    material("Seams · deep shadow", "182524", .92)
    material("Rubber · gaskets", "28302d", .86)
    material("Glass · dusty blue", "293536", .22, .26, texture="glass", coat=.45)
    material("Window · dim interior", "1f2c2d", .88)
    material("Window · pale curtain", "68716a", .94)
    material("Window · warm interior", "6c735b", .72, emission="858d62", strength=.17)
    material("Aluminium · window frame", "72827e", .48, .3)
    material("AC · faded enamel", "89958e", .61, .1)
    material("Puddle · shallow water", "283a37", .16, .04, texture="floor", coat=.75)
    material("Ripple · water meniscus", "465951", .13, .05, coat=1)
    material("Concrete · exposed aggregate", "434c46", .98)
    material("Leaf · waterlogged", "38372a", .59)
    material("Lamp · aged diffuser", "b8c391", .34, emission="a4bb84", strength=.65)
    material("Exit · phosphor green", "799f62", .33, emission="a7d677", strength=1.7)
    material("City · sodium light", "949069", .56, emission="d2c58a", strength=2.2)
    for i, color in enumerate(["68787d", "5c6c70", "718087", "627278", "77868b"]):
        material(f"Skyline · layer {i}", color, .91)
    sign = material("Exit · running figure", "7b9f57", .34, emission="c2dc99", strength=1.5)
    p = sign.node_tree.nodes.get("Principled BSDF")
    tex = sign.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = bpy.data.images.load(str(TEXTURES / "exit_sign.png"), check_existing=True)
    sign.node_tree.links.new(tex.outputs["Color"], p.inputs["Base Color"])
    sign.node_tree.links.new(tex.outputs["Color"], p.inputs["Emission Color"])
    rain = material("Rain · fine droplets", "93aeb1", .17, .02, coat=1)
    rain.node_tree.nodes.get("Principled BSDF").inputs["Alpha"].default_value = .30
    if hasattr(rain, "surface_render_method"):
        rain.surface_render_method = "DITHERED"
    elif hasattr(rain, "blend_method"):
        rain.blend_method = "HASHED"


def mat(name):
    return MATERIALS[name]


def mesh(name, verts, faces, material_name, group, uv_scale=.5):
    data = bpy.data.meshes.new(name)
    data.from_pydata(verts, [], faces)
    data.materials.append(mat(material_name))
    data.update()
    uv = data.uv_layers.new(name="UVMap")
    for p in data.polygons:
        axis = max(range(3), key=lambda a: abs(p.normal[a]))
        for idx in p.loop_indices:
            v = data.vertices[data.loops[idx].vertex_index].co
            uv.data[idx].uv = (v.y * uv_scale, v.z * uv_scale) if axis == 0 else ((v.x * uv_scale, v.z * uv_scale) if axis == 1 else (v.x * uv_scale, v.y * uv_scale))
    obj = bpy.data.objects.new(name, data)
    attach(obj, group)
    return obj


def box(name, loc, size, material_name, group="01 · Architecture", bevel=0, uv_scale=.5):
    x, y, z = loc
    a, b, c = (s / 2 for s in size)
    verts = [(x + dx, y + dy, z + dz) for dx, dy, dz in [(-a, -b, -c), (a, -b, -c), (a, b, -c), (-a, b, -c), (-a, -b, c), (a, -b, c), (a, b, c), (-a, b, c)]]
    obj = mesh(name, verts, [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (3, 7, 6, 2), (0, 4, 7, 3), (1, 2, 6, 5)], material_name, group, uv_scale)
    if bevel:
        mod = obj.modifiers.new("Soft manufactured edges", "BEVEL")
        mod.width = bevel
        mod.segments = 2
    return obj


def tube(name, points, radius, material_name, group="02 · Gallery details", segments=10, caps=True):
    points = [Vector(p) for p in points]
    verts, faces = [], []
    for i, p in enumerate(points):
        t = (points[min(i + 1, len(points) - 1)] - points[max(i - 1, 0)]).normalized()
        helper = Vector((0, 0, 1)) if abs(t.z) < .9 else Vector((0, 1, 0))
        a = t.cross(helper).normalized()
        b = t.cross(a).normalized()
        for j in range(segments):
            angle = j * math.tau / segments
            verts.append(tuple(p + radius * (a * math.cos(angle) + b * math.sin(angle))))
    for i in range(len(points) - 1):
        for j in range(segments):
            a = i * segments + j
            b = i * segments + (j + 1) % segments
            faces.append((a, b, b + segments, a + segments))
    if caps:
        faces.extend([tuple(reversed(range(segments))), tuple((len(points) - 1) * segments + j for j in range(segments))])
    obj = mesh(name, verts, faces, material_name, group)
    for p in obj.data.polygons:
        p.use_smooth = len(p.vertices) == 4
    return obj


def cylinder(name, a, b, radius, material_name, group="02 · Gallery details", segments=12):
    return tube(name, [a, b], radius, material_name, group, segments)


def ring(name, center, radius, thickness, material_name, axis="Z", group="02 · Gallery details", segments=28):
    cx, cy, cz = center
    points = []
    for i in range(segments + 1):
        a = i * math.tau / segments
        p, q = radius * math.cos(a), radius * math.sin(a)
        points.append((cx + p, cy + q, cz) if axis == "Z" else ((cx + p, cy, cz + q) if axis == "Y" else (cx, cy + p, cz + q)))
    return tube(name, points, thickness, material_name, group, 6, False)


def text(name, value, loc, size, material_name, orientation="front", group="02 · Gallery details"):
    data = bpy.data.curves.new(name, "FONT")
    data.body = value
    data.size = size
    data.extrude = .0005
    data.align_x = "CENTER"
    data.materials.append(mat(material_name))
    obj = bpy.data.objects.new(name, data)
    attach(obj, group)
    obj.location = loc
    obj.rotation_euler = (math.pi / 2, 0, math.pi / 2 if orientation == "left" else 0)
    return obj


def point_light(name, loc, color, power, radius=.15):
    data = bpy.data.lights.new(name, "POINT")
    data.energy = power
    data.color = rgb(color)[:3]
    data.shadow_soft_size = radius
    obj = bpy.data.objects.new(name, data)
    attach(obj, "06 · Lighting")
    obj.location = loc
    return obj


def area_light(name, loc, target, color, power, size):
    data = bpy.data.lights.new(name, "AREA")
    data.energy = power
    data.color = rgb(color)[:3]
    data.shape = "DISK"
    data.size = size
    obj = bpy.data.objects.new(name, data)
    attach(obj, "06 · Lighting")
    obj.location = loc
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    return obj


def build_architecture():
    box("Gallery structural floor", (.83, 8.5, -.15), (2.2, 24.0, .3), "Floor · uneven wet concrete", bevel=.008, uv_scale=.38)
    box("Wall · upper plaster", (-.17, 8.5, 2.43), (.34, 24, 1.94), "Plaster · rain stained")
    box("Wall · dark painted base", (-.165, 8.5, .73), (.35, 24, 1.46), "Paint · petrol lower band")
    box("Ceiling slab", (.88, 8.5, CEILING + .12), (2.38, 24, .24), "Ceiling · peeling mineral paint")
    box("Outer slab fascia", (2.04, 8.5, CEILING + .055), (.12, 24, .33), "Plaster · rain stained")
    box("Parapet · solid rain-wet wall", (WIDTH + .11, 8.5, .615), (.24, 24, 1.23), "Parapet · saturated concrete", bevel=.006)
    box("Parapet · weathered coping", (WIDTH + .11, 8.5, 1.27), (.29, 24.0, .08), "Parapet · saturated concrete", bevel=.012)
    box("Gallery end wall", (.81, LENGTH, 1.7), (2.03, .3, 3.4), "Plaster · rain stained")
    box("End wall lower paint", (.81, LENGTH - .161, .73), (2.03, .019, 1.46), "Paint · petrol lower band")
    box("Foreground structural pier", (2.41, 10.56, 3.1), (1.38, .72, 7.0), "Plaster · rain stained", bevel=.009)
    box("Pier exterior face", (3.114, 10.56, 3.1), (.028, .72, 7.0), "Facade · chalky concrete")
    for i, y in enumerate([14.6, 18.6]):
        box(f"Open gallery pier {i + 2}", (1.95, y, 1.62), (.35, .28, 3.24), "Plaster · rain stained", bevel=.007)
    for y in [2.8, 5.9, 9.1, 12.3, 15.5, 18.55]:
        box("Floor construction joint", (.87, y, .003), (1.6, .013, .004), "Seams · deep shadow")
    box("Left wall skirting", (.035, 8, .058), (.065, 23, .116), "Seams · deep shadow", bevel=.005)
    box("Drainage channel", (1.61, 8, .007), (.075, 23, .014), "Seams · deep shadow")
    for y in [4.2, 8.6, 13.4, 17.2]:
        box("Drain frame", (1.6, y, .012), (.17, .26, .018), "Steel · oxidised")
        for dy in range(7):
            box("Drain grate slot", (1.6, y - .11 + dy * .035, .023), (.128, .014, .007), "Seams · deep shadow")
    for _ in range(42):
        x, y = RNG.uniform(.08, 1.95), RNG.uniform(2.6, 18)
        rx, ry = RNG.uniform(.025, .16), RNG.uniform(.035, .34)
        verts = [(x, y, CEILING - .0015)]
        for k in range(10):
            a = k * math.tau / 10
            r = RNG.uniform(.5, 1.1)
            verts.append((x + math.cos(a) * rx * r, y + math.sin(a) * ry * r, CEILING - .0018))
        mesh("Ceiling spall · irregular flaked paint", verts, [(0, 1 + k, 1 + (k + 1) % 10) for k in range(10)], "Concrete · exposed aggregate", "02 · Gallery details")


def door_on_left(index, y):
    group = "02 · Gallery details"
    width, height = .97, 2.65
    box(f"Door {index:02d} · shadow reveal", (.025, y, height / 2), (.045, width + .13, height + .13), "Seams · deep shadow", group)
    box(f"Door {index:02d} · enamel leaf", (.055, y, height / 2), (.035, width, height), "Door · worn enamel", group, .006)
    for edge in [-1, 1]:
        box(f"Door {index:02d} · jamb", (.071, y + edge * (width / 2 + .023), height / 2), (.07, .047, height + .08), "Pipe · galvanized grey", group, .004)
    box(f"Door {index:02d} · head frame", (.072, y, height + .025), (.07, width + .09, .06), "Pipe · galvanized grey", group, .004)
    box(f"Door {index:02d} · threshold", (.066, y, .017), (.15, width, .034), "Hardware · worn nickel", group)
    box(f"Door {index:02d} · kick panel", (.076, y, .26), (.007, .80, .39), "Pipe · galvanized grey", group)
    handle_y = y - .32
    cylinder("Lock escutcheon", (.08, handle_y, 1.00), (.097, handle_y, 1.00), .038, "Steel · oxidised")
    cylinder("Door handle stem", (.086, handle_y, 1.00), (.145, handle_y, 1.00), .014, "Hardware · worn nickel")
    cylinder("Rounded door handle", (.145, handle_y - .045, 1.0), (.145, handle_y + .043, 1.0), .023, "Hardware · worn nickel", segments=16)
    cylinder("Peephole", (.08, y, 1.61), (.091, y, 1.61), .012, "Hardware · worn nickel")
    for z in [.33, 1.76]:
        cylinder("Door hinge", (.10, y + .48, z - .043), (.10, y + .48, z + .043), .016, "Steel · oxidised")
    box("Door number plate", (.083, y, 1.87), (.014, .18, .085), "Steel · oxidised", group)
    text("Apartment number", str(706 + index), (.094, y, 1.845), .045, "AC · faded enamel", "left")
    box("Old bell push housing", (.056, y - .66, 1.46), (.065, .069, .105), "Pipe · galvanized grey", group, .004)
    cylinder("Bell button", (.09, y - .66, 1.46), (.102, y - .66, 1.46), .011, "Rubber · gaskets")


def build_gallery_details():
    group = "02 · Gallery details"
    for index, y in enumerate([3.62, 7.63, 11.38, 15.08], 1):
        door_on_left(index, y)
    for y in [5.40, 9.42, 13.3, 17.05]:
        box("High utility recess", (.017, y, 2.23), (.035, .63, .71), "Seams · deep shadow", group)
        box("Utility window glass", (.038, y, 2.23), (.017, .55, .64), "Glass · dusty blue", group)
        for z in [1.89, 2.57]:
            box("Utility window sill", (.067, y, z), (.14, .68, .052), "Plaster · rain stained", group, .004)
        for dy in [-.29, .29]:
            box("Utility window frame", (.059, y + dy, 2.23), (.06, .045, .65), "Aluminium · window frame", group)
        box("Utility window mullion", (.063, y, 2.23), (.055, .025, .65), "Aluminium · window frame", group)
    box("Service exit · reveal", (.80, 19.81, 1.40), (1.11, .05, 2.80), "Seams · deep shadow", group)
    box("Service exit · leaf", (.8, 19.77, 1.375), (1.01, .045, 2.75), "Door · worn enamel", group, .008)
    for x in [.26, 1.34]:
        box("Service exit · frame", (x, 19.76, 1.41), (.06, .105, 2.82), "Pipe · galvanized grey", group)
    box("Service exit · frame head", (.80, 19.76, 2.79), (1.14, .10, .065), "Pipe · galvanized grey", group)
    box("Service exit · kick plate", (.8, 19.741, .23), (.92, .018, .35), "Pipe · galvanized grey", group)
    cylinder("Service exit handle", (1.13, 19.685, .98), (1.13, 19.685, 1.18), .015, "Hardware · worn nickel")
    box("Exit sign · metal enclosure", (.80, 19.67, 3.11), (.34, .10, .23), "Pipe · galvanized grey", group, .014)
    sign = mesh("Exit sign · running person and arrow", [(.65, 19.612, 3.015), (.95, 19.612, 3.015), (.95, 19.612, 3.205), (.65, 19.612, 3.205)], [(0, 1, 2, 3)], "Exit · running figure", group)
    for loop, uv in zip(sign.data.uv_layers.active.data, [(0, 0), (1, 0), (1, 1), (0, 1)]):
        loop.uv = uv
    point_light("Exit sign · reflected green light", (.8, 19.25, 3.10), "a9ce83", 5, .16)
    for y in [7.8, 14.0]:
        box("Step light · backplate", (.055, y, .39), (.10, .21, .24), "Steel · oxidised", group, .012)
        box("Step light · yellow-green frosted glass", (.115, y, .39), (.025, .15, .177), "Lamp · aged diffuser", group, .009)
        for z in [.316, .40, .467]:
            cylinder("Step light · protective cage", (.135, y - .085, z), (.135, y + .085, z), .0045, "Steel · oxidised")
        point_light("Step light · grazing wall illumination", (.24, y, .38), "b1c794", .75, .11)
    for y in [3.0, 10.5, 16.3]:
        cylinder("Vertical service riser", (.138, y, -.2), (.138, y, 3.4), .039 if y != 3.0 else .040, "Pipe · galvanized grey", segments=16)
        for z in [.33, 1.12, 2.2, 2.8]:
            cylinder("Riser collar", (.138, y, z - .016), (.138, y, z + .016), .05, "Pipe · galvanized grey", segments=16)
            box("Riser mounting strap", (.06, y, z), (.10, .105, .026), "Steel · oxidised", group)
            for dy in [-.066, .066]:
                cylinder("Riser mounting bolt", (.042, y + dy, z), (.083, y + dy, z), .008, "Hardware · worn nickel", segments=6)
    cylinder("Ceiling conduit · continuous", (.13, -1.5, 2.87), (.13, 18.6, 2.87), .019, "Steel · oxidised")
    for y in [3.3, 4.7, 6.4, 8.1, 9.9, 11.7, 13.5, 15.3, 17.1]:
        box("Conduit saddle", (.128, y, 2.884), (.078, .027, .065), "Pipe · galvanized grey", group)
    cylinder("Cross-gallery utility main", (.05, 7.15, 2.88), (2.47, 7.15, 2.88), .043, "Steel · oxidised", segments=20)
    for x in [.11, .22, .98, 1.48, 2.04, 2.13, 2.22, 2.31, 2.4]:
        cylinder("Cross-pipe sleeve and rib", (x - .011, 7.15, 2.88), (x + .011, 7.15, 2.88), .056, "Pipe · galvanized grey", segments=16)
    cylinder("Cross-pipe capped branch", (1.55, 7.15, 2.88), (1.55, 7.15, 3.13), .027, "Steel · oxidised")
    cylinder("Pipe hanging rod", (.52, 7.15, 2.90), (.52, 7.15, CEILING), .009, "Rust · exposed fasteners")
    path = [(2.20, 9.97, -.15), (2.20, 9.97, 2.96)]
    for i in range(1, 10):
        a = (i / 9) * math.pi / 2
        path.append((2.08 + .12 * math.cos(a), 9.97, 2.96 + .12 * math.sin(a)))
    path.extend([(1.83, 9.97, 3.08), (1.70, 9.97, 3.08)])
    tube("Large drainpipe · swept elbow", path, .082, "Pipe · galvanized grey", segments=20)
    for z in [.35, 1.50, 2.87]:
        cylinder("Downpipe socket", (2.20, 9.97, z - .034), (2.20, 9.97, z + .034), .095, "Pipe · galvanized grey", segments=20)
        box("Downpipe wall bracket", (2.20, 10.11, z), (.21, .3, .07), "Steel · oxidised", group)
    for y, z in [(3.18, 2.0), (10.65, 2.2)]:
        box("Electrical junction box", (.109, y, z), (.15, .21, .255), "Pipe · galvanized grey", group, .018)
        box("Electrical junction face", (.19, y, z), (.02, .17, .21), "Door · worn enamel", group, .006)
        for dy in [-.066, .066]:
            for dz in [-.083, .083]:
                cylinder("Junction cover screw", (.2, y + dy, z + dz), (.206, y + dy, z + dz), .006, "Hardware · worn nickel", segments=6)
        tube("Loose service cable", [(.08, y - .17, z + .62), (.11, y - .19, z + .15), (.18, y - .15, z - .23), (.13, y, z - .35), (.12, y + .14, z - .19), (.15, y + .1, z + .13)], .009, "Rubber · gaskets")
    cylinder("Meter round body", (.16, 3.19, 2.40), (.235, 3.19, 2.40), .071, "Pipe · galvanized grey", segments=24)
    cylinder("Meter glass face", (.236, 3.19, 2.40), (.244, 3.19, 2.40), .055, "Glass · dusty blue", segments=24)
    ring("Meter rim", (.244, 3.19, 2.40), .059, .008, "Hardware · worn nickel", "X")


def build_water():
    group = "03 · Water and surface detail"
    for index, (x, y, rx, ry) in enumerate([(.77, 4.8, .39, .80), (1.15, 7.3, .39, 1.3), (.55, 10.4, .32, 1.1), (1.21, 13.5, .31, .83), (.86, 16.8, .47, .67), (.94, 3.0, .31, .69)]):
        verts = [(x, y, .0008)]
        n = 52
        for i in range(n):
            a = math.tau * i / n
            noise = 1 + RNG.uniform(-.04, .04)
            verts.append((x + math.cos(a) * rx * noise, y + math.sin(a) * ry * noise, .0008))
        mesh(f"Puddle {index + 1} · irregular shallow film", verts, [(0, i + 1, (i + 1) % n + 1) for i in range(n)], "Puddle · shallow water", group, .38)
    for index in range(13):
        x, y = RNG.uniform(1.33, 1.55), RNG.uniform(3.2, 17.2)
        for radius in [.027, .047, .073]:
            ring("Raindrop ripple · concentric meniscus", (x, y, .009), radius * RNG.uniform(.85, 1.1), .0009, "Ripple · water meniscus", group=group, segments=22)
    for _ in range(11):
        x, y = RNG.choice([RNG.uniform(.08, .18), RNG.uniform(1.47, 1.58)]), RNG.uniform(3.0, 17.8)
        mesh("Waterlogged leaf in drainage edge", [(x, y, .013), (x + .015, y + .032, .017), (x + .047, y + .043, .014), (x + .035, y + .011, .013)], [(0, 1, 2, 3)], "Leaf · waterlogged", group)


def air_conditioner(name, x, y, z, scale=1):
    group = "04 · Opposite apartment block"
    box(name + " · casing", (x, y - .16, z), (.73 * scale, .37 * scale, .56 * scale), "AC · faded enamel", group, .017)
    cylinder(name + " · dark fan opening", (x - .11 * scale, y - .356 * scale, z), (x - .11 * scale, y - .371 * scale, z), .202 * scale, "Seams · deep shadow", group, 16)
    for radius in [.07, .125, .185]:
        ring(name + " · grille", (x - .11 * scale, y - .38 * scale, z), radius * scale, .009 * scale, "Aluminium · window frame", "Y", group, 20)
    for i in range(7):
        zz = z + (i - 3) * .055 * scale
        box(name + " · condenser fins", (x + .24 * scale, y - .353 * scale, zz), (.13 * scale, .011, .014 * scale), "Steel · oxidised", group)
    for dx in [-.24, .24]:
        box(name + " · wall bracket", (x + dx * scale, y - .08, z - .32 * scale), (.045, .47 * scale, .055), "Steel · oxidised", group)
    tube(name + " · refrigerant line", [(x + .37 * scale, y - .07, z), (x + .46 * scale, y - .06, z - .19), (x + .46 * scale, y + .04, z - .48)], .018, "Pipe · galvanized grey", group, 6)


def build_apartments():
    group = "04 · Opposite apartment block"
    front, roof, bottom, height = 46.0, 4.32, -17.2, 2.98
    box("Apartment block · main core", (30, front + 2.3, (roof + bottom) / 2), (20, 4.6, roof - bottom), "Facade · chalky concrete", group)
    box("Stairwell · shadow recess", (22.8, front - .06, (roof + bottom) / 2), (4.9, .18, roof - bottom - .1), "Window · dim interior", group)
    for floor in range(8):
        ztop = roof - floor * height
        if floor < 7:
            for bay in range(5):
                x = 27.15 + bay * 2.64
                bottom_window = ztop - 2.15
                interior = "Window · warm interior" if (floor, bay) in [(2, 0), (5, 3), (3, 4)] else "Window · dim interior"
                box("Recessed apartment window", (x, front - .081, bottom_window + .85), (2.34, .14, 1.72), interior, group)
                box("Dusty window glazing", (x, front - .181, bottom_window + .85), (2.26, .037, 1.62), "Glass · dusty blue", group)
                if (bay + floor * 3) % 4 == 0:
                    box("Uneven closed curtain", (x - .50, front - .205, bottom_window + .91), (.47, .008, 1.51), "Window · pale curtain", group)
                for dx in [-1.15, 0, 1.15]:
                    box("Window frame · vertical", (x + dx, front - .217, bottom_window + .85), (.052, .11, 1.75), "Aluminium · window frame", group)
                for zz in [bottom_window, bottom_window + 1.15, bottom_window + 1.71]:
                    box("Window frame · horizontal", (x, front - .22, zz), (2.37, .11, .046), "Aluminium · window frame", group)
                if (floor + bay) % 3 != 0:
                    air_conditioner(f"External AC · floor {floor + 1} bay {bay + 1}", x + .6, front - .12, bottom_window - .29, .90)
                if bay == 0 and floor % 2 == 0:
                    air_conditioner("External AC · old twin installation", x - .31, front - .12, bottom_window - .29, .81)
                if floor in [2, 4]:
                    for dx in [-.96, -.48, 0, .48, .96]:
                        cylinder("Window security bars", (x + dx, front - .34, bottom_window), (x + dx, front - .34, bottom_window + 1.15), .014, "Steel · oxidised", group, 6)
            for j in range(6):
                box("Facade vertical pier", (25.8 + j * 2.64, front - .18, ztop - 1.44), (.20, .43, 2.9), "Facade · chalky concrete", group)
        box("Facade continuous floor band", (32.1, front - .20, ztop), (14.5, .48, .18), "Facade · chalky concrete", group, .007)
        box("Stairwell landing · cantilever slab", (22.45, front - .58, ztop - .05), (5.3, 1.14, .21), "Facade · chalky concrete", group)
        if floor < 7:
            box("Stairwell · inner dark doorway", (23.5, front - .20, ztop - 1.35), (1.85, .04, 2.05), "Window · dim interior", group)
            if floor in [2, 4, 6]:
                box("Stairwell · dim light", (23.47, front - .228, ztop - 1.20), (.32, .03, .65), "Window · warm interior", group)
            verts = [(19.8, front - 1.12, ztop - .14), (21.35, front - 1.12, ztop - .14), (21.35, front - 1.12, ztop - 1.64), (20.48, front - 1.12, ztop - 1.64)]
            mesh("Stairwell · angled concrete cheek", verts, [(0, 1, 2, 3)], "Facade · chalky concrete", group)
            box("Stairwell service spine", (24.2, front - .08, ztop - 1.52), (.55, .65, 2.9), "Facade · chalky concrete", group)
    box("Roof coping · street face", (30.0, front - .12, roof + .15), (20.4, .44, .30), "Facade · chalky concrete", group, .018)
    box("Roof · flat weathered slab", (30, front + 2.3, roof + .10), (20.1, 4.6, .16), "Ceiling · peeling mineral paint", group)
    for y in [front + .3, front + 4.5]:
        box("Rooftop parapet", (30, y, roof + .39), (20, .20, .61), "Facade · chalky concrete", group)
    box("Lift machinery room", (22.1, front + 2.4, roof + .52), (2.6, 2.0, 1.04), "Facade · chalky concrete", group)
    box("Lift machinery canopy", (22.1, front + 2.4, roof + 1.09), (3.3, 2.4, .15), "Pipe · galvanized grey", group)
    for x in [27.2, 32.4, 35.7]:
        cylinder("Roof vent stack", (x, front + 3.5, roof), (x, front + 3.5, roof + .91), .074, "Pipe · galvanized grey", group)
        cylinder("Vent mushroom cap", (x, front + 3.5, roof + .88), (x, front + 3.5, roof + .93), .16, "Steel · oxidised", group)
    cylinder("Rooftop TV aerial mast", (23.1, front + 4, roof + 1), (23.1, front + 4, roof + 4.9), .022, "Steel · oxidised", group, 8)
    for z in [roof + 3.8, roof + 4.45]:
        cylinder("TV aerial crossbar", (22.2, front + 4, z), (24.0, front + 4, z), .012, "Steel · oxidised", group, 6)
    for x in [30, 31.3, 36.7]:
        tube("Roof utility pipe", [(x, front + .4, roof + .35), (x, front + .4, roof + .85), (x + .40, front + .4, roof + .85), (x + .40, front + .4, roof + .4)], .024, "Steel · oxidised", group, 8)
    for x in [25.1, 39.5]:
        cylinder("Facade rainwater downpipe", (x, front - .37, bottom), (x, front - .37, roof + .2), .046, "Pipe · galvanized grey", group, 8)


def build_city():
    group = "05 · Distant city"
    for i in range(37):
        x = RNG.uniform(33, 65)
        y = RNG.uniform(140, 185)
        w = RNG.uniform(2.2, 3.8)
        d = RNG.uniform(3.7, 7.2)
        top = RNG.uniform(14, 21)
        base = -31
        color = f"Skyline · layer {i % 5}"
        box(f"Distant high-rise {i + 1:02d}", (x, y, (base + top) / 2), (w, d, top - base), color, group)
        box("High-rise rooftop plant room", (x + .24, y, top + .52), (w * .46, d * .6, 1.04), color, group)
        for column in range(3):
            xx = x - w * .33 + column * w * .32
            box("High-rise vertical facade rib", (xx, y - d / 2 - .04, (base + top) / 2), (.09, .09, top - base), f"Skyline · layer {(i + 1) % 5}", group)
            for floor in range(1, int((top - base) / 3.0)):
                if RNG.random() < .28:
                    z = base + floor * 3.0
                    glow = "City · sodium light" if RNG.random() < .07 else f"Skyline · layer {(i + 2) % 5}"
                    box("Distant narrow window", (xx, y - d / 2 - .095, z), (.37, .016, .83), glow, group)
    for i, (x, y, z, sx, sy) in enumerate([(6.5, 42, -3.8, 6, 8), (12, 55, -2.9, 5.5, 10), (16, 72, -1.4, 5, 7), (8, 67, -6, 8, 8), (44, 70, -.8, 7, 9)]):
        box("Low-rise urban infill", (x, y, z - 8), (sx, sy, 16), "Skyline · layer 1", group)
        box("Low-rise rooftop coping", (x, y, z + .06), (sx + .2, sy + .2, .12), "Pipe · galvanized grey", group)
        for j in range(2):
            box("Rooftop utility housing", (x + j - .5, y, z + .40), (.7, 1.1, .68), "Steel · oxidised", group)
    for i in range(14):
        x, y, z = 14.5 + RNG.uniform(-.65, 1.1), 54 + RNG.uniform(-2, 3), RNG.uniform(-.6, .65)
        box("Small distant shop light", (x, y, z), (RNG.uniform(.08, .27), .06, RNG.uniform(.07, .17)), "City · sodium light", group)
    box("Street · wet ground far below", (15, 55, -19.1), (100, 150, .3), "Floor · uneven wet concrete", group)


def build_rain():
    group = "07 · Rain particle system"
    droplet = cylinder("Rain droplet instance · source", (0, 0, -.11), (.003, 0, .11), .00085, "Rain · fine droplets", group, 5)
    droplet.location = (-90, -90, -90)
    emitter = mesh("Rain emitter · editable particle system", [(2.5, -6, 12), (27, -6, 12), (27, 48, 12), (2.5, 48, 12)], [(0, 1, 2, 3)], "Rain · fine droplets", group)
    bpy.ops.object.select_all(action="DESELECT")
    emitter.select_set(True)
    bpy.context.view_layer.objects.active = emitter
    bpy.ops.object.particle_system_add()
    ps = emitter.particle_systems[-1].settings
    ps.name = "Rain · 6500 falling droplets"
    ps.type = "EMITTER"
    ps.count = 6500
    ps.frame_start = -60
    ps.frame_end = 240
    ps.lifetime = 58
    ps.normal_factor = -8.5
    ps.factor_random = .25
    ps.particle_size = 1
    ps.size_random = .55
    ps.render_type = "OBJECT"
    ps.instance_object = droplet
    ps.effector_weights.gravity = .48
    ps.display_percentage = 35
    ps.use_rotations = False
    emitter.show_instancer_for_render = False
    emitter.show_instancer_for_viewport = False
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 180
    for frame in range(-60, 49):
        scene.frame_set(frame)
    scene.frame_set(48)


def camera(name, location, target, lens=35):
    data = bpy.data.cameras.new(name)
    data.lens = lens
    data.sensor_width = 36
    data.sensor_fit = "HORIZONTAL"
    data.clip_start = .06
    data.clip_end = 350
    obj = bpy.data.objects.new(name, data)
    attach(obj, "08 · Cameras")
    obj.location = location
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    return obj


def setup_scene(quality):
    scene = bpy.context.scene
    scene.name = "Rain Gallery · reference reconstruction"
    world = bpy.data.worlds.new("Overcast blue-grey sky")
    world.use_nodes = True
    scene.world = world
    nodes, links = world.node_tree.nodes, world.node_tree.links
    bg = nodes.get("Background")
    bg.inputs["Strength"].default_value = .65
    coord = nodes.new("ShaderNodeTexCoord")
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 2.8
    noise.inputs["Detail"].default_value = 3.5
    noise.inputs["Roughness"].default_value = .68
    links.new(coord.outputs["Normal"], noise.inputs["Vector"])
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = .17
    ramp.color_ramp.elements[0].color = rgb("7e94b3")
    ramp.color_ramp.elements[1].position = .84
    ramp.color_ramp.elements[1].color = rgb("9fb1c8")
    links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bg.inputs["Color"])
    area_light("Sky · broad diffuse opening", (9, 7, 15), (0, 9, 0), "b5c7cf", 1550, 17)
    area_light("Sky · low horizon fill", (13, 16, 5), (.3, 10, 1.2), "a9bec5", 320, 13)
    area_light("Sky · reflected foreground fill", (1.15, -.6, 2.05), (.5, 7, 1.3), "b4c0c5", 600, 4)
    reference = camera("Camera_Reference", CAMERA_POSITION, CAMERA_TARGET)
    camera("Camera_CorridorDetail", (1.35, 5.0, 1.62), (.34, 13, 1.35), 30)
    camera("Camera_CityOverview", (3.7, 8, 3.2), (24, 52, -1.2), 36)
    camera("Camera_Reverse", (1.15, 16.1, 1.63), (.7, 2.3, 1.48), 26)
    scene.camera = reference
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = {"draft": 24, "preview": 72, "final": 160}[quality]
    scene.cycles.use_denoising = True
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.adaptive_threshold = .025 if quality != "final" else .012
    scene.cycles.max_bounces = 7
    scene.cycles.diffuse_bounces = 3
    scene.cycles.glossy_bounces = 4
    scene.cycles.transmission_bounces = 4
    scene.cycles.transparent_max_bounces = 5
    scene.cycles.sample_clamp_indirect = 3
    scene.render.resolution_x = {"draft": 735, "preview": 1102, "final": 1470}[quality]
    scene.render.resolution_y = {"draft": 525, "preview": 787, "final": 1050}[quality]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.film_transparent = False
    scene.render.fps = 24
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.view_settings.exposure = -.10
    scene.world.mist_settings.start = 10
    scene.world.mist_settings.depth = 220
    scene.world.mist_settings.falloff = "LINEAR"
    bpy.context.view_layer.use_pass_mist = True
    bpy.context.view_layer.use_pass_z = True
    scene.use_nodes = True
    tree = scene.node_tree
    tree.nodes.clear()
    render = tree.nodes.new("CompositorNodeRLayers")
    haze = tree.nodes.new("CompositorNodeMixRGB")
    haze.blend_type = "MIX"
    haze.inputs[2].default_value = rgb("718793")
    sky_mask = tree.nodes.new("CompositorNodeMath")
    sky_mask.operation = "LESS_THAN"
    sky_mask.inputs[1].default_value = 999
    tree.links.new(render.outputs["Depth"], sky_mask.inputs[0])
    mist_mask = tree.nodes.new("CompositorNodeMath")
    mist_mask.operation = "MULTIPLY"
    tree.links.new(render.outputs["Mist"], mist_mask.inputs[0])
    tree.links.new(sky_mask.outputs[0], mist_mask.inputs[1])
    tree.links.new(mist_mask.outputs[0], haze.inputs[0])
    tree.links.new(render.outputs["Image"], haze.inputs[1])
    glow = tree.nodes.new("CompositorNodeGlare")
    glow.glare_type = "FOG_GLOW"
    glow.quality = "HIGH"
    glow.threshold = 1.1
    glow.size = 7
    tree.links.new(haze.outputs[0], glow.inputs[0])
    out = tree.nodes.new("CompositorNodeComposite")
    tree.links.new(glow.outputs[0], out.inputs[0])
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "METERS"
    scene["reference"] = "User supplied photograph, 735 × 525; not used as scene geometry or a camera-facing backdrop."
    scene["reconstruction_limit"] = "Visible forms interpreted from one image; hidden surfaces and metric scale inferred."
    scene["seed"] = SEED
    scene["controls"] = "Numpad 0: matching camera; F12: Cycles render; Space: native rain particles; other cameras in collection 08."
    return scene


def configure_workspace():
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.scene.camera.select_set(True)
    bpy.context.view_layer.objects.active = bpy.context.scene.camera
    for workspace in bpy.data.workspaces:
        for screen in [workspace.screens[0]] if len(workspace.screens) else []:
            for area in screen.areas:
                if area.type == "VIEW_3D":
                    space = area.spaces.active
                    space.region_3d.view_perspective = "CAMERA"
                    space.region_3d.view_camera_zoom = 0
                    space.overlay.show_overlays = False
                    space.shading.type = "MATERIAL"
                    space.shading.use_scene_world = True
                    space.shading.use_scene_lights = True
                    space.clip_end = 400
    notes = bpy.data.texts.new("START HERE · ПРОЧТИ МЕНЯ")
    notes.write("RAIN GALLERY / ДОЖДЛИВАЯ ГАЛЕРЕЯ\n\nСамостоятельная реконструкция одного пользовательского снимка.\nЭто редактируемая 3D-геометрия, а не изображение на плоскости.\n\nNumPad 0 — основной ракурс. F12 — рендер Cycles.\nSpace — проигрывание дождя, кадры 1–180.\nКамеры деталей и города находятся в коллекции 08.\nМатериалы и оригинальные PBR-текстуры упакованы в этот файл.\n\nСкрытые поверхности, масштаб и план города предполагаются.\nБраузерный и Godot просмотр используют тот же экспорт геометрии,\nно освещение реального времени не равно трассировке Cycles.\nГенератор: scripts/build_scene.py; карты: scripts/make_textures.py.\n")


def export_model(scene):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    groups = defaultdict(lambda: {"verts": [], "faces": [], "uvs": []})
    objects = [o for o in scene.objects if o.type in {"MESH", "FONT", "CURVE"} and any(c.name.startswith(("01", "02", "03", "04", "05")) for c in o.users_collection)]
    original_count = len(objects)
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        data = evaluated.to_mesh()
        if not data or not data.materials:
            if data:
                evaluated.to_mesh_clear()
            continue
        label = obj.users_collection[0].name
        data.calc_loop_triangles()
        uv_layer = data.uv_layers.active
        by_mat = defaultdict(list)
        for polygon in data.polygons:
            by_mat[polygon.material_index].append(polygon)
        for material_index, polygons in by_mat.items():
            material = data.materials[material_index]
            batch = groups[(label, material.name)]
            remap = {}
            for poly in polygons:
                face = []
                for li in poly.loop_indices:
                    vi = data.loops[li].vertex_index
                    if vi not in remap:
                        remap[vi] = len(batch["verts"])
                        batch["verts"].append(tuple(obj.matrix_world @ data.vertices[vi].co))
                    face.append(remap[vi])
                    batch["uvs"].append(tuple(uv_layer.data[li].uv) if uv_layer else (0, 0))
                batch["faces"].append(tuple(face))
        evaluated.to_mesh_clear()
    bpy.ops.object.select_all(action="DESELECT")
    export_collection = collection("EXPORT · batched copy, not saved to source blend")
    total_vertices, total_triangles = 0, 0
    for (label, material_name), batch in groups.items():
        data = bpy.data.meshes.new(label + " / " + material_name)
        data.from_pydata(batch["verts"], [], batch["faces"])
        data.materials.append(mat(material_name))
        uv = data.uv_layers.new(name="UVMap")
        for loop, value in zip(uv.data, batch["uvs"]):
            loop.uv = value
        data.update()
        obj = bpy.data.objects.new(label + " / " + material_name, data)
        export_collection.objects.link(obj)
        obj.select_set(True)
        total_vertices += len(data.vertices)
        total_triangles += sum(len(p.vertices) - 2 for p in data.polygons)
    scene.camera.select_set(True)
    bpy.context.view_layer.objects.active = scene.camera
    bpy.ops.export_scene.gltf(filepath=str(ASSETS / "rain-gallery.glb"), export_format="GLB", use_selection=True, export_yup=True, export_cameras=True, export_lights=False, export_animations=False, export_extras=True, export_materials="EXPORT", export_tangents=True)
    position = scene.camera.location
    target = Vector(CAMERA_TARGET)
    fov = math.degrees(2 * math.atan((36 / 35) * (525 / 735) / 2))
    manifest = {
        "title": "Дождливая галерея",
        "version": "1.0.0",
        "seed": SEED,
        "camera": {"position": [position.x, position.z, -position.y], "target": [target.x, target.z, -target.y], "fov": fov, "near": .06, "far": 350, "aspect": 735 / 525},
        "bounds": {"min": [.20, 1.65, -18.6], "max": [1.16, 1.65, 1.0]},
        "assets": {"model": "/assets/rain-gallery.glb", "reference": "/assets/reference.jpg", "beauty": "/assets/beauty.webp"},
        "stats": {"editableObjects": original_count, "meshes": len(groups), "vertices": total_vertices, "triangles": total_triangles, "materials": len(MATERIALS), "textureResolution": 1024},
        "lighting": {"fogColor": "#718793", "fogNear": 10, "fogFar": 220, "exitLight": [.80, 3.10, -19.25], "stepLights": [[.24, .38, -7.8], [.24, .38, -14.0]]},
        "reference": {"width": 735, "height": 525, "source": "User-supplied reference; original rights retained", "usedAsBackdrop": False},
        "limitations": ["Single-image reconstruction: unseen geometry and scale are inferred.", "Real-time engines approximate Cycles lighting and wet reflections.", "Native Blender rain is recreated as real-time particles, not exported as a simulation."]
    }
    (ASSETS / "scene-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print("EXPORT_STATS " + json.dumps(manifest["stats"]))


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--quality", choices=["draft", "preview", "final"], default="preview")
    parser.add_argument("--skip-render", action="store_true")
    parser.add_argument("--skip-export", action="store_true")
    options = parser.parse_args(args)
    start = time.time()
    ASSETS.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0
    make_materials()
    build_architecture()
    build_gallery_details()
    build_water()
    build_apartments()
    build_city()
    build_rain()
    scene = setup_scene(options.quality)
    configure_workspace()
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=str(DOWNLOADS / "rain-gallery.blend"), compress=True)
    if not options.skip_render:
        scene.render.filepath = str(ASSETS / "beauty.png")
        bpy.ops.render.render(write_still=True)
    if not options.skip_export:
        export_model(scene)
    print(f"FINISHED in {time.time() - start:.1f}s, Blender {bpy.app.version_string}")


if __name__ == "__main__":
    main()
