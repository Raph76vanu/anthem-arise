"""Neutral mesh loaders used by the embedded viewer.

The GTA IV resource layout support is an independent, UI-free adaptation of
documented structures also implemented by the GPL-3.0 BlenDR project.
"""
from __future__ import annotations

import io
import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MeshData:
    name: str
    vertices: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]
    # Optional Frostbite skin data, parallel to ``vertices``. Bone values are
    # already mapped through the section palette to SkeletonAsset bone IDs.
    skin_bones: list[tuple[int, int, int, int]] | None = None
    skin_weights: list[tuple[float, float, float, float]] | None = None


class MeshFormatError(ValueError):
    pass


@dataclass
class SkeletonData:
    name: str
    joints: list[tuple[str, int, float, float, float]]


def load_texture_bytes(raw: bytes, name: str):
    """Decode a GTA IV WTD resource into a Pillow image (first texture)."""
    from PIL import Image
    payload, system_size = _rsc_payload(raw)
    if system_size <= 0 or system_size >= len(payload):
        raise MeshFormatError("WTD resource has invalid system/graphics memory sizes.")
    system, graphics = payload[:system_size], payload[system_size:]
    def ptr(data: bytes, offset: int, data_offset: bool = False) -> int:
        value = _u32(data, offset)
        if value and (value >> 28) not in ({6} if data_offset else {5}):
            raise MeshFormatError("WTD pointer has an invalid resource segment.")
        return value & 0x0FFFFFFF
    if len(system) < 24:
        raise MeshFormatError("WTD texture dictionary header is truncated.")
    hash_table = ptr(system, 16)
    count = _u16(system, 20)
    texture_list = ptr(system, 24)
    if not count or count > 4096:
        raise MeshFormatError("WTD contains no reasonable texture records.")
    info_offset = ptr(system, texture_list)
    if info_offset + 76 > len(system):
        raise MeshFormatError("WTD texture record points outside system memory.")
    name_offset = ptr(system, info_offset + 20)
    width, height = _u16(system, info_offset + 28), _u16(system, info_offset + 30)
    fmt = _u32(system, info_offset + 32)
    levels = system[info_offset + 39] or 1
    data_offset = ptr(system, info_offset + 72, True)
    if not width or not height or data_offset >= len(graphics):
        raise MeshFormatError("WTD texture record has invalid dimensions or data offset.")
    if fmt == 0x31545844:  # DXT1
        size = max(8, ((width + 3) // 4) * ((height + 3) // 4) * 8)
        rgba = _decode_dxt1(graphics[data_offset:data_offset + size], width, height)
    elif fmt in {0x33545844, 0x35545844}:  # DXT3/DXT5 (colour decode is enough for inspection)
        size = ((width + 3) // 4) * ((height + 3) // 4) * 16
        block_count = ((width + 3) // 4) * ((height + 3) // 4)
        color_blocks = b"".join(graphics[data_offset + index * 16 + 8:data_offset + index * 16 + 16] for index in range(block_count))
        rgba = _decode_dxt1(color_blocks, width, height)
    elif fmt == 0x15:  # A8R8G8B8
        size = width * height * 4
        data = graphics[data_offset:data_offset + size]
        if len(data) < size:
            raise MeshFormatError("WTD pixel data is truncated.")
        rgba = bytes(sum(([b, g, r, a] for b, g, r, a in struct.iter_unpack("<4B", data)), []))
    elif fmt == 0x32:  # L8
        size = width * height
        data = graphics[data_offset:data_offset + size]
        rgba = bytes(channel for value in data for channel in (value, value, value, 255))
    else:
        raise MeshFormatError(f"WTD pixel format 0x{fmt:08X} is not supported yet.")
    if len(rgba) != width * height * 4:
        raise MeshFormatError("WTD decoder produced incomplete pixel data.")
    return Image.frombytes("RGBA", (width, height), rgba), count


def _decode_dxt1(data: bytes, width: int, height: int) -> bytes:
    out = bytearray(width * height * 4)
    blocks_x, blocks_y = (width + 3) // 4, (height + 3) // 4
    for by in range(blocks_y):
        for bx in range(blocks_x):
            offset = (by * blocks_x + bx) * 8
            if offset + 8 > len(data):
                raise MeshFormatError("WTD DXT data is truncated.")
            c0, c1, bits = struct.unpack_from("<HHI", data, offset)
            def rgb(c):
                return ((c >> 11 & 31) * 255 // 31, (c >> 5 & 63) * 255 // 63, (c & 31) * 255 // 31, 255)
            palette = [rgb(c0), rgb(c1)]
            if c0 > c1:
                palette += [tuple((2 * palette[0][i] + palette[1][i]) // 3 for i in range(4)), tuple((palette[0][i] + 2 * palette[1][i]) // 3 for i in range(4))]
            else:
                palette += [tuple((palette[0][i] + palette[1][i]) // 2 for i in range(4)), (0, 0, 0, 0)]
            for py in range(4):
                for px in range(4):
                    x, y = bx * 4 + px, by * 4 + py
                    if x < width and y < height:
                        color = palette[bits & 3]
                        bits >>= 2
                        out[(y * width + x) * 4:(y * width + x + 1) * 4] = bytes(color)
                    else:
                        bits >>= 2
    return bytes(out)


def load_skeleton_bytes(raw: bytes, name: str) -> SkeletonData:
    """Best-effort joint extraction for opaque skeleton sidecar formats.

    Many engines keep bone names as ordinary strings even when transforms are
    proprietary. We expose those names and build a conservative parent graph;
    this is useful for inspecting an unfamiliar file without pretending to
    decode its animation or skinning data.
    """
    import re
    strings = []
    for match in re.finditer(rb"[ -~]{3,64}\x00", raw):
        value = match.group()[:-1].decode("ascii", "ignore").strip()
        if value and not any(ch in value for ch in "{}[]<>|\\/\t"):
            strings.append(value)
    bone_terms = ("root", "pelvis", "spine", "neck", "head", "clav", "arm", "forearm", "hand", "finger", "thigh", "calf", "leg", "foot", "toe", "hip", "jaw", "eye")
    names = []
    seen = set()
    for value in strings:
        lower = value.lower()
        if len(value) > 40 or value.isdigit() or not any(term in lower for term in bone_terms):
            continue
        if lower not in seen:
            seen.add(lower)
            names.append(value)
    if not names:
        raise MeshFormatError(
            f"No verified skeleton hierarchy was found in {name}. Generic transform matrices are not bones, "
            "so Game Asset Explorer will not draw a guessed joint graph."
        )
    names = names[:512]

    def parent_for(index: int, lower: str) -> int:
        def find(*terms: str) -> int:
            for candidate in range(index - 1, -1, -1):
                if any(term in names[candidate].lower() for term in terms):
                    return candidate
            return -1
        if "root" in lower or "pelvis" in lower or ("hip" in lower and "thigh" not in lower):
            return -1
        if "head" in lower or "jaw" in lower or "eye" in lower:
            return find("neck", "head")
        if "neck" in lower:
            return find("spine", "chest", "clav")
        if "clav" in lower or "shoulder" in lower:
            return find("spine", "chest")
        if "forearm" in lower or "lowerarm" in lower:
            return find("upperarm", "arm", "clav")
        if "hand" in lower or "finger" in lower:
            return find("forearm", "arm")
        if "upperarm" in lower or ("arm" in lower and "fore" not in lower):
            return find("clav", "shoulder", "spine")
        if "calf" in lower or "shin" in lower:
            return find("thigh", "leg", "hip")
        if "foot" in lower or "toe" in lower:
            return find("calf", "shin", "leg")
        if "thigh" in lower or "leg" in lower:
            return find("pelvis", "hip", "root")
        return index - 1 if index else -1

    joints = []
    for index, joint_name in enumerate(names):
        lower = joint_name.lower()
        parent = parent_for(index, lower)
        # Stable inspection layout: spine rises, arms spread, legs drop.
        if any(term in lower for term in ("head", "neck", "spine", "chest")):
            x, y, z = 0.0, 1.0 + index * 0.04, 0.0
        elif any(term in lower for term in ("arm", "hand", "clav", "shoulder")):
            side = -1.0 if any(term in lower for term in ("_l", "left", "l_") ) else 1.0
            x, y, z = side * (0.3 + (index % 4) * 0.18), 1.1 - (index % 4) * 0.16, 0.0
        elif any(term in lower for term in ("leg", "thigh", "calf", "foot", "toe")):
            side = -1.0 if any(term in lower for term in ("_l", "left", "l_") ) else 1.0
            x, y, z = side * 0.22, 0.55 - (index % 4) * 0.28, 0.0
        else:
            x, y, z = 0.0, 0.9 - index * 0.03, 0.0
        joints.append((joint_name, parent, x, y, z))
    return SkeletonData(name, joints)


EMBEDDED_PREVIEW_EXTENSIONS = frozenset({".obj", ".stl", ".wdr", ".wdd", ".rsc"})


def load_mesh(path: Path) -> list[MeshData]:
    suffix = path.suffix.lower()
    if suffix == ".obj":
        return [_load_obj(path)]
    if suffix == ".stl":
        return [_load_stl(path)]
    if suffix in {".wdr", ".wdd"}:
        try:
            loader = _load_wdr if suffix == ".wdr" else _load_wdd
            return loader(path.read_bytes(), path.stem)
        except (struct.error, IndexError) as error:
            raise MeshFormatError(
                f"{path.name} is a valid resource file, but it is not a supported character drawable."
            ) from error
    if suffix == ".rsc":
        return load_mesh_bytes(path.read_bytes(), suffix, path.stem)
    raise MeshFormatError(f"The embedded viewer does not support {suffix or 'this format'} yet.")


def load_mesh_bytes(raw: bytes, suffix: str, name: str) -> list[MeshData]:
    """Probe unnamed engine resources by structure rather than file extension."""
    suffix = suffix.lower()
    if suffix == ".wdd":
        return _load_wdd(raw, name)
    if suffix == ".wdr":
        return _load_wdr(raw, name)
    if suffix != ".rsc":
        raise MeshFormatError(f"Binary probing is not available for {suffix or 'this format'}.")
    errors = []
    for label, loader in (("drawable dictionary", _load_wdd), ("drawable", _load_wdr)):
        try:
            return loader(raw, name)
        except (MeshFormatError, struct.error, IndexError) as error:
            errors.append(f"{label}: {error}")
    raise MeshFormatError("Resource is not supported character geometry (" + "; ".join(errors) + ").")


def load_obj_text(raw: str, name: str) -> MeshData:
    """Load OBJ text emitted by an engine adapter without a temporary file."""
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            vertices.append(tuple(map(float, parts[1:4])))
        elif parts[0] == "f" and len(parts) >= 4:
            polygon = []
            for value in parts[1:]:
                index = int(value.split("/", 1)[0])
                polygon.append(index - 1 if index > 0 else len(vertices) + index)
            for index in range(1, len(polygon) - 1):
                faces.append((polygon[0], polygon[index], polygon[index + 1]))
    if not vertices or not faces:
        raise MeshFormatError(f"{name} contains no renderable OBJ triangles.")
    return MeshData(name, vertices, faces)


def _load_obj(path: Path) -> MeshData:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            vertices.append(tuple(map(float, parts[1:4])))
        elif parts[0] == "f" and len(parts) >= 4:
            polygon = []
            for raw in parts[1:]:
                index = int(raw.split("/", 1)[0])
                polygon.append(index - 1 if index > 0 else len(vertices) + index)
            for index in range(1, len(polygon) - 1):
                faces.append((polygon[0], polygon[index], polygon[index + 1]))
    if not vertices or not faces:
        raise MeshFormatError("OBJ contains no renderable triangles.")
    return MeshData(path.stem, vertices, faces)


def _load_stl(path: Path) -> MeshData:
    raw = path.read_bytes()
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    if len(raw) >= 84 and 84 + struct.unpack_from("<I", raw, 80)[0] * 50 == len(raw):
        count = struct.unpack_from("<I", raw, 80)[0]
        for tri in range(count):
            offset = 84 + tri * 50 + 12
            base = len(vertices)
            vertices.extend(struct.unpack_from("<3f", raw, offset + i * 12) for i in range(3))
            faces.append((base, base + 1, base + 2))
    else:
        current: list[int] = []
        for line in raw.decode("ascii", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0].lower() == "vertex":
                current.append(len(vertices))
                vertices.append(tuple(map(float, parts[1:])))
                if len(current) == 3:
                    faces.append(tuple(current))
                    current = []
    if not vertices or not faces:
        raise MeshFormatError("STL contains no renderable triangles.")
    return MeshData(path.stem, vertices, faces)


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise MeshFormatError("A 16-bit field points outside the resource.")
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise MeshFormatError("A 32-bit field points outside the resource.")
    return struct.unpack_from("<I", data, offset)[0]


def _ptr(data: bytes, offset: int) -> int:
    return _u32(data, offset) & 0x0FFFFFFF


def _f32(data: bytes, offset: int) -> float:
    if offset < 0 or offset + 4 > len(data):
        raise MeshFormatError("A vertex field points outside the resource.")
    return struct.unpack_from("<f", data, offset)[0]


def _rsc_payload(raw: bytes) -> tuple[bytes, int]:
    if len(raw) < 12 or raw[:3] != b"RSC":
        raise MeshFormatError("Not a GTA IV RSC resource.")
    flags = _u32(raw, 8)
    system_memory = (flags & 0x7FF) << (((flags >> 11) & 0xF) + 8)
    packed = raw[12:]
    try:
        payload = zlib.decompress(packed)
    except zlib.error:
        payload = packed
    return payload, system_memory


def _read_geometry(payload: bytes, geometry_offset: int, system_memory: int, base: int = 0) -> MeshData:
    # Geometry structure: vertex buffer pointer at +0x0C, index buffer at
    # +0x1C, counts near +0x30, and vertex declaration through the VB.
    if geometry_offset < 0 or geometry_offset + 0x48 > len(payload):
        raise MeshFormatError("Geometry pointer is outside the resource.")
    vertex_buffer = _ptr(payload, geometry_offset + 0x0C) - base
    index_buffer = _ptr(payload, geometry_offset + 0x1C) - base
    if min(vertex_buffer, index_buffer) < 0:
        raise MeshFormatError("Invalid GTA IV buffer pointer.")
    # GTA IV geometry places index count at +0x2C and the 16-bit vertex
    # count at +0x34. (+0x30 is face count, not index count.)
    index_count = _u32(payload, geometry_offset + 0x2C)
    vertex_count = _u16(payload, geometry_offset + 0x34)

    vb_data = _ptr(payload, vertex_buffer + 0x08) - base + system_memory
    declaration = _ptr(payload, vertex_buffer + 0x10) - base
    stride = _u16(payload, declaration + 0x04)
    if stride not in {28, 36, 44, 52, 60, 68}:
        raise MeshFormatError(f"Unsupported GTA IV vertex stride: {stride}")
    if vertex_count > 2_000_000 or index_count > 12_000_000:
        raise MeshFormatError("Unreasonable GTA IV geometry counts.")

    vertices = []
    for index in range(vertex_count):
        offset = vb_data + index * stride
        if offset + 12 > len(payload):
            raise MeshFormatError("Vertex buffer ends unexpectedly.")
        vertex = (_f32(payload, offset), _f32(payload, offset + 4), _f32(payload, offset + 8))
        if not all(math.isfinite(value) for value in vertex):
            raise MeshFormatError("Invalid vertex coordinates.")
        vertices.append(vertex)

    index_data = _ptr(payload, index_buffer + 0x08) - base + system_memory
    faces = []
    for offset in range(0, index_count - 2, 3):
        location = index_data + offset * 2
        if location + 6 > len(payload):
            break
        face = struct.unpack_from("<3H", payload, location)
        if max(face) < vertex_count and len(set(face)) == 3:
            faces.append(face)
    if not vertices or not faces:
        raise MeshFormatError("GTA IV geometry contains no renderable triangles.")
    return MeshData("geometry", vertices, faces)


def _standalone_wdr_geometries(payload: bytes, system_memory: int, name: str) -> list[MeshData]:
    model_collection = _ptr(payload, 0x40)
    model_pointer_array = _ptr(payload, model_collection)
    model = _ptr(payload, model_pointer_array)
    geometry_array = _ptr(payload, model + 0x04)
    geometry_count = _u16(payload, model + 0x0A)
    meshes = []
    for index in range(min(geometry_count, 4096)):
        geometry = _ptr(payload, geometry_array + index * 4)
        try:
            mesh = _read_geometry(payload, geometry, system_memory)
            mesh.name = f"{name}_{index}"
            meshes.append(mesh)
        except (MeshFormatError, struct.error):
            continue
    if not meshes:
        raise MeshFormatError("No supported geometry was found in this WDR.")
    return meshes


def _load_wdr(raw: bytes, name: str) -> list[MeshData]:
    payload, system_memory = _rsc_payload(raw)
    return _standalone_wdr_geometries(payload, system_memory, name)


def _embedded_wdr_geometry(payload: bytes, start: int, system_memory: int, name: str) -> MeshData:
    # Drawable dictionaries omit the standalone model collection. Follow the
    # embedded model pointer chain used by GTA IV WDD resources.
    model_pointer = _ptr(payload, start + 0x40)
    first = _ptr(payload, model_pointer)
    second = _ptr(payload, first)
    model = second
    geometry_array = _ptr(payload, model + 0x04)
    geometry = _ptr(payload, geometry_array)
    mesh = _read_geometry(payload, geometry, system_memory)
    mesh.name = name
    return mesh


def _load_wdd(raw: bytes, name: str) -> list[MeshData]:
    payload, system_memory = _rsc_payload(raw)
    if len(payload) < 0x24:
        raise MeshFormatError("WDD header is truncated.")
    pointer_array = _ptr(payload, 0x18)
    count = _u16(payload, 0x1C)
    meshes = []
    for index in range(min(count, 4096)):
        start = _ptr(payload, pointer_array + index * 4)
        try:
            meshes.append(_embedded_wdr_geometry(payload, start, system_memory, f"{name}_{index}"))
        except (MeshFormatError, struct.error):
            continue
    if not meshes:
        raise MeshFormatError("No supported drawable geometry was found in this WDD (the parser is experimental).")
    return meshes
