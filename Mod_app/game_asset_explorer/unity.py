"""Read-only Unity serialized-file and AssetBundle inspection.

Unity containers are not loose models: a ``.assets`` file or UnityFS bundle can
contain many typed objects, while ``.resS`` files are companion byte streams.
This adapter exposes the typed objects as ordinary AssetRecords so the neutral
category UI and embedded viewer can handle them without a game-specific mode.
"""
from __future__ import annotations

import math
import json
import subprocess
import sys
from pathlib import Path

from .geometry import MeshData, MeshFormatError, SkeletonData, load_obj_text
from .models import AssetRecord


_CATEGORY_HINTS = {
    "Characters / meshes": ("character", "player", "avatar", "hero", "enemy", "skin", "body", "ped", "npc"),
    "Props": ("prop", "item", "object", "decor", "furniture", "pickup", "environment"),
    "Map parts": ("map", "world", "level", "scene", "terrain", "building", "road", "environment"),
    "Weapons & carryables": ("weapon", "gun", "rifle", "pistol", "sword", "melee", "ammo"),
    "Textures": ("texture", "material", "atlas", "sprite"),
    "Images": ("image", "icon", "ui", "sprite", "logo"),
    "Skeletons": ("skeleton", "rig", "bone", "avatar"),
    "Animations": ("animation", "anim", "motion", "controller"),
}
_NON_VISUAL_HINTS = ("localization", "string-table", "string_table", "language", "catalog")


def select_unity_containers(
    containers: list[AssetRecord], category: str, *, limit: int = 12,
) -> list[AssetRecord]:
    """Choose a bounded, category-relevant Unity probe set.

    A Unity installation can contain thousands of bundles. Opening every one
    when a filter changes makes the UI look frozen, so adapters probe the most
    promising names plus a small deterministic sample of unknown bundles.
    """
    hints = _CATEGORY_HINTS.get(category, ())
    ranked: list[tuple[int, int, int, str, AssetRecord]] = []
    for container in containers:
        name = container.relative_path.lower()
        non_visual = any(hint in name for hint in _NON_VISUAL_HINTS)
        hits = sum(hint in name for hint in hints)
        if non_visual and category not in {"Containers / archives"}:
            continue
        # Avoid selecting multi-gigabyte monoliths first. Opening those merely
        # because they are large caused avoidable memory pressure in Unity
        # games. Prefer useful mid-sized bundles, while still allowing a large
        # hinted file to be opened explicitly by the user.
        if container.size > 2 * 1024**3 and hits == 0:
            continue
        useful_size = 256 * 1024 <= container.size <= 256 * 1024**2
        ranked.append((hits, int(useful_size), min(container.size, 2**31 - 1), name, container))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    if not ranked:
        return []
    hinted = [item[4] for item in ranked if item[0] > 0]
    unknown = [item[4] for item in ranked if item[0] == 0]
    # Always reserve a few slots for custom naming conventions.
    fallback_slots = min(6, limit)
    selected = hinted[: max(0, limit - fallback_slots)] + unknown[:fallback_slots]
    if len(selected) < limit:
        selected_ids = {id(asset) for asset in selected}
        selected.extend(
            asset for asset in (*hinted, *unknown)
            if id(asset) not in selected_ids
        )
    return selected[:limit]


def choose_unity_preview_asset(found: list[AssetRecord], category: str) -> AssetRecord | None:
    """Pick a real contained object appropriate for the current filter."""
    from .grouping import category_for_asset

    previewable = [asset for asset in found if asset.directly_viewable]
    matching = [asset for asset in previewable if category_for_asset(asset) == category]
    if matching:
        matching.sort(key=lambda asset: (
            asset.kind != "mesh",
            asset.metadata.get("skinned") is not True,
            asset.relative_path.lower(),
        ))
        return matching[0]
    preferred_kind = {
        "Characters / meshes": "mesh", "Props": "mesh", "Map parts": "mesh",
        "Weapons & carryables": "mesh", "Textures": "texture", "Images": "texture",
        "Skeletons": "skeleton",
    }.get(category)
    fallback = [asset for asset in previewable if not preferred_kind or asset.kind == preferred_kind]
    return fallback[0] if fallback else None


def _reader_file_name(reader) -> str:
    source = getattr(reader, "assets_file", None)
    return str(getattr(source, "name", "") or "")


def _reader_key(reader) -> tuple[str, int]:
    return (_reader_file_name(reader), int(getattr(reader, "path_id", 0)))


def _safe_name(reader, fallback: str) -> str:
    try:
        name = reader.peek_name()
        if name:
            return str(name)
    except Exception:
        pass
    return fallback


def _pointer_id(pointer) -> int:
    return int(getattr(pointer, "path_id", getattr(pointer, "m_PathID", 0)) or 0)


def _game_object_name(pointer, fallback: str) -> str:
    try:
        reader = pointer.deref()
        return _safe_name(reader, fallback)
    except Exception:
        return fallback


def _record(
    root: Path,
    container: AssetRecord,
    reader,
    *,
    name: str,
    kind: str,
    extension: str,
    metadata: dict[str, object] | None = None,
) -> AssetRecord:
    relative = f"{container.relative_path}::{name}#{int(reader.path_id)}"
    details: dict[str, object] = {
        "reader": "unity",
        "unity_type": reader.type.name,
        "unity_path_id": int(reader.path_id),
        "unity_assets_file": _reader_file_name(reader),
    }
    details.update(metadata or {})
    return AssetRecord(
        path=container.path,
        relative_path=relative,
        kind=kind,
        extension=extension,
        size=int(getattr(reader, "byte_size", container.size)),
        engine="Unity",
        directly_viewable=kind in {"mesh", "texture", "skeleton"},
        container_path=container.path,
        internal_path=f"{name}#{int(reader.path_id)}",
        metadata=details,
    )


def inspect_unity_container(root: Path, container: AssetRecord) -> tuple[list[AssetRecord], list[str]]:
    """Enumerate previewable typed objects in one Unity container."""
    del root
    import UnityPy

    environment = UnityPy.load(str(container.path))
    # UnityPy already exposes a reusable object collection. Copying it doubled
    # peak memory on containers with very large object tables.
    readers = environment.objects
    discovered: list[AssetRecord] = []
    warnings: list[str] = []
    skinned_meshes: set[tuple[str, int]] = set()
    skeletons: list[tuple[object, str, list[int]]] = []

    # Establish actual skinning references before publishing meshes. A filename
    # containing "skeleton" is not enough to claim that an object is a rig.
    for reader in readers:
        if reader.type.name != "SkinnedMeshRenderer":
            continue
        try:
            renderer = reader.parse_as_object()
            mesh_pointer = getattr(renderer, "m_Mesh", None)
            mesh_id = _pointer_id(mesh_pointer)
            if mesh_id:
                skinned_meshes.add((_reader_file_name(reader), mesh_id))
            bone_ids = [_pointer_id(pointer) for pointer in getattr(renderer, "m_Bones", ())]
            bone_ids = list(dict.fromkeys(value for value in bone_ids if value))
            if len(bone_ids) >= 2:
                name = _game_object_name(
                    getattr(renderer, "m_GameObject", None),
                    f"Skinned renderer {reader.path_id}",
                )
                skeletons.append((reader, name, bone_ids))
        except Exception as error:
            if len(warnings) < 20:
                warnings.append(f"Could not inspect Unity skinned renderer {reader.path_id}: {error}")

    for reader in readers:
        object_type = reader.type.name
        try:
            if object_type == "Mesh":
                name = _safe_name(reader, f"Mesh {reader.path_id}")
                discovered.append(_record(
                    container.path.parent, container, reader,
                    name=name, kind="mesh", extension=".unitymesh",
                    metadata={"skinned": _reader_key(reader) in skinned_meshes},
                ))
            elif object_type == "Texture2D":
                name = _safe_name(reader, f"Texture {reader.path_id}")
                discovered.append(_record(
                    container.path.parent, container, reader,
                    name=name, kind="texture", extension=".unitytexture",
                ))
            elif object_type == "Sprite":
                name = _safe_name(reader, f"Sprite {reader.path_id}")
                discovered.append(_record(
                    container.path.parent, container, reader,
                    name=name, kind="texture", extension=".png",
                ))
            elif object_type == "AnimationClip":
                name = _safe_name(reader, f"Animation {reader.path_id}")
                discovered.append(_record(
                    container.path.parent, container, reader,
                    name=name, kind="animation", extension=".unityanim",
                ))
        except Exception as error:
            if len(warnings) < 20:
                warnings.append(f"Could not catalogue Unity {object_type} {reader.path_id}: {error}")

    for reader, name, bone_ids in skeletons:
        discovered.append(_record(
            container.path.parent, container, reader,
            name=f"{name} skeleton", kind="skeleton", extension=".unityrig",
            metadata={
                "unity_type": "Skeleton",
                "unity_renderer_path_id": int(reader.path_id),
                "unity_bone_path_ids": bone_ids,
            },
        ))

    if not discovered:
        warnings.append(f"{container.relative_path}: Unity opened the container but found no supported typed assets.")
    return discovered, warnings


def _record_from_dict(data: dict[str, object]) -> AssetRecord:
    container_path = data.get("container_path")
    return AssetRecord(
        path=Path(str(data["path"])),
        relative_path=str(data["relative_path"]),
        kind=str(data["kind"]),  # type: ignore[arg-type]
        extension=str(data["extension"]),
        size=int(data["size"]),
        engine=str(data["engine"]) if data.get("engine") else None,
        directly_viewable=bool(data.get("directly_viewable")),
        container_path=Path(str(container_path)) if container_path else None,
        internal_path=str(data["internal_path"]) if data.get("internal_path") else None,
        metadata=dict(data.get("metadata") or {}),
    )


def _worker_command(action: str, asset: AssetRecord, *, timeout: int) -> dict[str, object]:
    request = {"action": action, "asset": asset.to_dict()}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "game_asset_explorer.unity_worker"],
            input=json.dumps(request), capture_output=True, text=True,
            timeout=timeout, creationflags=flags,
        )
    except subprocess.TimeoutExpired as error:
        raise MeshFormatError(
            f"Unity container {asset.path.name} exceeded the {timeout}-second safety limit and was skipped."
        ) from error
    if completed.returncode:
        detail = completed.stderr.strip().splitlines()
        message = detail[-1] if detail else f"worker exited with code {completed.returncode}"
        raise MeshFormatError(
            f"Unity container {asset.path.name} failed in an isolated reader: {message}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise MeshFormatError(f"Unity reader returned an invalid response for {asset.path.name}.") from error
    if response.get("error"):
        raise MeshFormatError(str(response["error"]))
    return response


def inspect_unity_container_isolated(
    root: Path, container: AssetRecord, *, timeout: int = 45,
) -> tuple[list[AssetRecord], list[str]]:
    """Inspect Unity data outside the GUI process so corrupt bundles cannot kill it."""
    del root
    response = _worker_command("inspect", container, timeout=timeout)
    found = [_record_from_dict(item) for item in response.get("assets", [])]
    return found, [str(item) for item in response.get("warnings", [])]


def preview_unity_asset_isolated(asset: AssetRecord, *, timeout: int = 60):
    """Decode one Unity preview in a crash-contained child process."""
    response = _worker_command("preview", asset, timeout=timeout)
    kind = str(response.get("kind"))
    if kind == "mesh":
        return kind, [
            MeshData(
                str(item["name"]),
                [tuple(vertex) for vertex in item["vertices"]],
                [tuple(face) for face in item["faces"]],
            )
            for item in response.get("meshes", [])
        ]
    if kind == "image":
        import base64
        import io
        from PIL import Image
        raw = base64.b64decode(str(response["png"]))
        with Image.open(io.BytesIO(raw)) as decoded:
            return kind, decoded.copy()
    if kind == "skeleton":
        joints = [tuple(joint) for joint in response.get("joints", [])]
        return kind, SkeletonData(str(response.get("name") or asset.internal_path), joints)
    raise MeshFormatError(f"Unity worker returned unsupported preview type {kind!r}.")


def _find_reader(environment, metadata: dict[str, object], *, path_id_key: str = "unity_path_id"):
    path_id = metadata.get(path_id_key)
    assets_file = metadata.get("unity_assets_file")
    if not isinstance(path_id, int):
        raise MeshFormatError("The Unity asset record has no stable object id.")
    for reader in environment.objects:
        if int(reader.path_id) == path_id and (not assets_file or _reader_file_name(reader) == assets_file):
            return reader
    raise MeshFormatError(f"Unity object {path_id} is no longer present in the container.")


def _vector(value, size: int, default: tuple[float, ...]) -> tuple[float, ...]:
    names = ("x", "y", "z", "w")
    try:
        return tuple(float(getattr(value, names[index])) for index in range(size))
    except (AttributeError, TypeError, ValueError):
        try:
            return tuple(float(value[index]) for index in range(size))
        except (IndexError, KeyError, TypeError, ValueError):
            return default


def _quat_multiply(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _quat_rotate(quaternion, vector):
    x, y, z, w = quaternion
    length = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    quaternion = (x / length, y / length, z / length, w / length)
    pure = (vector[0], vector[1], vector[2], 0.0)
    inverse = (-quaternion[0], -quaternion[1], -quaternion[2], quaternion[3])
    rotated = _quat_multiply(_quat_multiply(quaternion, pure), inverse)
    return rotated[:3]


def _unity_skeleton(environment, asset: AssetRecord) -> SkeletonData:
    wanted = asset.metadata.get("unity_bone_path_ids")
    if not isinstance(wanted, list) or len(wanted) < 2:
        raise MeshFormatError("This Unity object has no verified renderer bone list.")
    wanted_ids = [int(value) for value in wanted]
    assets_file = asset.metadata.get("unity_assets_file")
    reader_by_id = {
        int(reader.path_id): reader for reader in environment.objects
        if reader.type.name == "Transform" and (not assets_file or _reader_file_name(reader) == assets_file)
    }
    parsed: dict[int, object] = {}

    def transform_for(path_id: int):
        if path_id not in parsed:
            reader = reader_by_id.get(path_id)
            if reader is None:
                raise MeshFormatError(f"Unity transform {path_id} referenced by the rig is missing.")
            parsed[path_id] = reader.parse_as_object()
        return parsed[path_id]

    world: dict[int, tuple[tuple[float, float, float], tuple[float, float, float, float], tuple[float, float, float]]] = {}
    visiting: set[int] = set()

    def world_transform(path_id: int):
        if path_id in world:
            return world[path_id]
        if path_id in visiting:
            raise MeshFormatError("The Unity transform hierarchy contains a cycle.")
        visiting.add(path_id)
        transform = transform_for(path_id)
        position = _vector(getattr(transform, "m_LocalPosition", None), 3, (0.0, 0.0, 0.0))
        rotation = _vector(getattr(transform, "m_LocalRotation", None), 4, (0.0, 0.0, 0.0, 1.0))
        scale = _vector(getattr(transform, "m_LocalScale", None), 3, (1.0, 1.0, 1.0))
        father_id = _pointer_id(getattr(transform, "m_Father", None))
        if father_id and father_id in reader_by_id:
            parent_position, parent_rotation, parent_scale = world_transform(father_id)
            scaled = tuple(position[index] * parent_scale[index] for index in range(3))
            offset = _quat_rotate(parent_rotation, scaled)
            position = tuple(parent_position[index] + offset[index] for index in range(3))
            rotation = _quat_multiply(parent_rotation, rotation)
            scale = tuple(parent_scale[index] * scale[index] for index in range(3))
        visiting.remove(path_id)
        world[path_id] = (position, rotation, scale)
        return world[path_id]

    index_for = {path_id: index for index, path_id in enumerate(wanted_ids)}
    joints = []
    for path_id in wanted_ids:
        transform = transform_for(path_id)
        position, _rotation, _scale = world_transform(path_id)
        father_id = _pointer_id(getattr(transform, "m_Father", None))
        seen: set[int] = set()
        while father_id and father_id not in index_for and father_id not in seen:
            seen.add(father_id)
            father = reader_by_id.get(father_id)
            if father is None:
                father_id = 0
                break
            father_id = _pointer_id(getattr(transform_for(father_id), "m_Father", None))
        parent_index = index_for.get(father_id, -1)
        name = _game_object_name(getattr(transform, "m_GameObject", None), f"Bone {path_id}")
        joints.append((name, parent_index, *position))
    return SkeletonData(asset.internal_path or "Unity skeleton", joints)


def preview_unity_asset(asset: AssetRecord):
    """Return ``(viewer_kind, payload)`` for one indexed Unity object."""
    import UnityPy

    environment = UnityPy.load(str(asset.path))
    unity_type = asset.metadata.get("unity_type")
    if unity_type == "Skeleton":
        return "skeleton", _unity_skeleton(environment, asset)
    reader = _find_reader(environment, asset)
    data = reader.parse_as_object()
    if unity_type == "Mesh":
        exported = data.export("obj")
        if isinstance(exported, bytes):
            exported = exported.decode("utf-8", errors="replace")
        return "mesh", [load_obj_text(str(exported), asset.internal_path or "Unity mesh")]
    if unity_type in {"Texture2D", "Sprite"}:
        return "image", data.image.copy()
    if unity_type == "AnimationClip":
        name = getattr(data, "m_Name", asset.internal_path or "Animation")
        sample_rate = float(getattr(data, "m_SampleRate", 0.0) or 0.0)
        raise MeshFormatError(f"Animation clip '{name}' is indexed ({sample_rate:g} fps); timeline playback is not in v0.13 yet.")
    raise MeshFormatError(f"Unity object type {unity_type or reader.type.name} is not previewable yet.")
