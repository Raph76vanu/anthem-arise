"""Executed inside Blender, not regular Python."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import bpy


def import_file(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix == ".blend":
        with bpy.data.libraries.load(str(path), link=False) as (source, target):
            target.objects = source.objects
        for obj in target.objects:
            if obj is not None:
                bpy.context.collection.objects.link(obj)
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))
    elif suffix in {".gltf", ".glb"}:
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif suffix == ".obj":
        op = getattr(bpy.ops.wm, "obj_import", None)
        (op or bpy.ops.import_scene.obj)(filepath=str(path))
    elif suffix == ".dae":
        bpy.ops.wm.collada_import(filepath=str(path))
    elif suffix == ".abc":
        bpy.ops.wm.alembic_import(filepath=str(path))
    elif suffix == ".ply":
        op = getattr(bpy.ops.wm, "ply_import", None)
        (op or bpy.ops.import_mesh.ply)(filepath=str(path))
    elif suffix == ".stl":
        op = getattr(bpy.ops.wm, "stl_import", None)
        (op or bpy.ops.import_mesh.stl)(filepath=str(path))


def main() -> None:
    args_after_separator = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(args_after_separator)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    imported = set()
    for raw in manifest["files"]:
        path = Path(raw)
        # Avoid importing one embedded mesh/animation container twice.
        if path in imported:
            continue
        try:
            import_file(path)
            imported.add(path)
        except Exception as error:
            print(f"Game Asset Explorer: could not import {path}: {error}")

    bpy.context.scene["game_asset_explorer_bundle"] = manifest["name"]
    bpy.ops.object.select_all(action="SELECT")
    # Frame the imported objects in every visible 3D viewport. Blender may start
    # without a VIEW_3D area, so preview framing is best-effort.
    if bpy.context.selected_objects and bpy.context.screen:
        for area in bpy.context.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((item for item in area.regions if item.type == "WINDOW"), None)
            if region:
                try:
                    with bpy.context.temp_override(area=area, region=region):
                        bpy.ops.view3d.view_selected(use_all_regions=False)
                except Exception as error:
                    print(f"Game Asset Explorer: could not frame viewport: {error}")


main()
