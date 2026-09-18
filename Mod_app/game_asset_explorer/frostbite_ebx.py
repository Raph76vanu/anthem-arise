"""Small, schema-independent reader for Anthem-generation EBX headers."""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import uuid

from .geometry import MeshFormatError, SkeletonData


EBX_VERSION_2 = 0x0FB2D1CE
EBX_VERSION_4 = 0x0FB4D1CE
MAX_EBX_IMPORTS = 100_000


@dataclass(frozen=True)
class AnthemEbxHeader:
    file_guid: uuid.UUID
    imports: tuple[tuple[uuid.UUID, uuid.UUID], ...]
    strings_offset: int
    strings_length: int
    data_length: int
    arrays: tuple[tuple[int, int, int], ...]


def parse_anthem_ebx_header(raw: bytes) -> AnthemEbxHeader:
    """Read the file GUID and external references without requiring type schemas."""
    if len(raw) < 56:
        raise MeshFormatError("The Frostbite EBX record is truncated.")
    magic, strings_offset, _strings_and_data, import_count = struct.unpack_from("<4I", raw, 0)
    if magic not in (EBX_VERSION_2, EBX_VERSION_4):
        raise MeshFormatError("The record is not a supported Anthem-generation EBX file.")
    if import_count > MAX_EBX_IMPORTS:
        raise MeshFormatError("The EBX import table exceeds the safety limit.")
    strings_length, array_count, data_length = struct.unpack_from("<3I", raw, 28)
    file_guid = uuid.UUID(bytes_le=raw[40:56])
    import_offset = 64 if magic == EBX_VERSION_4 else (56 + (-56 % 16))
    import_size = import_count * 32
    if import_offset + import_size > len(raw):
        raise MeshFormatError("The EBX import table points outside the record.")
    imports = tuple(
        (
            uuid.UUID(bytes_le=raw[offset:offset + 16]),
            uuid.UUID(bytes_le=raw[offset + 16:offset + 32]),
        )
        for offset in range(import_offset, import_offset + import_size, 32)
    )
    table_span = (array_count * 12 + 15) & ~15
    table_offset = strings_offset - table_span
    if table_offset < import_offset + import_size:
        raise MeshFormatError("The EBX array table overlaps its header data.")
    arrays = tuple(
        struct.unpack_from("<IIi", raw, table_offset + index * 12)
        for index in range(array_count)
    )
    arrays_base = strings_offset + strings_length + data_length
    if arrays_base > len(raw) or any(
        offset > len(raw) - arrays_base or count > 1_000_000
        for offset, count, _class_ref in arrays
    ):
        raise MeshFormatError("The EBX array data points outside the record.")
    return AnthemEbxHeader(
        file_guid, imports, strings_offset, strings_length, data_length, arrays,
    )


def _cstring(raw: bytes, offset: int, limit: int) -> str | None:
    if not 0 <= offset < limit:
        return None
    end = raw.find(b"\0", offset, limit)
    if end < 0:
        return None
    try:
        value = raw[offset:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return value if value and all(character.isprintable() for character in value) else None


def inspect_anthem_ebx_record(raw: bytes) -> dict[str, object]:
    """Return bounded structural facts for an indexed animation/config EBX.

    This deliberately does not label an arbitrary animation-path EBX as a clip.
    It gives the UI enough verified information to distinguish a readable EBX
    record from the still-unknown compressed keyframe payload it may reference.
    """
    header = parse_anthem_ebx_header(raw)
    strings_end = header.strings_offset + header.strings_length
    strings: list[str] = []
    cursor = header.strings_offset
    while cursor < strings_end and len(strings) < 256:
        end = raw.find(b"\0", cursor, strings_end)
        if end < 0:
            break
        if end > cursor:
            try:
                value = raw[cursor:end].decode("utf-8")
            except UnicodeDecodeError:
                value = ""
            if value and len(value) <= 260 and all(character.isprintable() for character in value):
                strings.append(value)
        cursor = end + 1
    hints = tuple(
        value for value in strings
        if any(token in value.casefold() for token in (
            "anim", "clip", "pose", "skeleton", "ant", "motion",
        ))
    )[:24]
    return {
        "file_guid": str(header.file_guid),
        "imports": len(header.imports),
        "arrays": len(header.arrays),
        "strings": len(strings),
        "hints": hints,
    }


def decode_anthem_skeleton(raw: bytes, name: str = "Anthem skeleton") -> SkeletonData:
    """Decode SkeletonAsset names, parents and model-space bind positions.

    Anthem's EBX classes are described by an installation-wide type database,
    so this reader identifies the strongly constrained SkeletonAsset arrays by
    their counts and contents rather than depending on game-specific field IDs.
    """
    header = parse_anthem_ebx_header(raw)
    if not header.arrays:
        raise MeshFormatError("The EBX record contains no SkeletonAsset arrays.")
    strings_end = header.strings_offset + header.strings_length
    arrays_base = strings_end + header.data_length

    name_candidates: list[tuple[int, list[str]]] = []
    for array_index, (offset, count, _class_ref) in enumerate(header.arrays):
        if not 2 <= count <= 4096 or arrays_base + offset + count * 4 > len(raw):
            continue
        values = struct.unpack_from(f"<{count}I", raw, arrays_base + offset)
        names = [
            _cstring(raw, header.strings_offset + value, strings_end)
            for value in values
        ]
        if all(names):
            name_candidates.append((array_index, [str(value) for value in names]))
    if not name_candidates:
        raise MeshFormatError("No verified bone-name array was found in the skeleton EBX.")
    name_index, bone_names = max(name_candidates, key=lambda item: len(item[1]))
    bone_count = len(bone_names)

    parent_candidates: list[tuple[int, tuple[int, ...], int]] = []
    for array_index, (offset, count, _class_ref) in enumerate(header.arrays):
        if array_index == name_index or count != bone_count:
            continue
        if arrays_base + offset + count * 4 > len(raw):
            continue
        values = struct.unpack_from(f"<{count}i", raw, arrays_base + offset)
        if not values or not all(-1 <= value < bone_count for value in values):
            continue
        roots = sum(value == -1 for value in values)
        ordered = sum(value == -1 or value < index for index, value in enumerate(values))
        if roots:
            parent_candidates.append((array_index, values, ordered))
    if not parent_candidates:
        raise MeshFormatError("No verified parent-index array was found in the skeleton EBX.")
    parent_index, parents, _score = max(parent_candidates, key=lambda item: item[2])

    transform_arrays: dict[int, list[tuple[float, ...]]] = {}
    for array_index, (offset, count, _class_ref) in enumerate(header.arrays):
        if count != bone_count or array_index in (name_index, parent_index):
            continue
        end = arrays_base + offset + count * 64
        if end > len(raw):
            continue
        matrices = [
            struct.unpack_from("<16f", raw, arrays_base + offset + index * 64)
            for index in range(count)
        ]
        if all(all(math.isfinite(value) for value in matrix) for matrix in matrices):
            transform_arrays[array_index] = matrices
    if len(transform_arrays) < 2:
        raise MeshFormatError("The skeleton EBX does not expose complete bind-pose transforms.")

    # For a valid local/model pair, each model-space parent-child distance is
    # the magnitude of the child's local translation (rotation preserves it).
    best: tuple[float, int, int] | None = None
    for local_index, local in transform_arrays.items():
        for model_index, model in transform_arrays.items():
            if local_index == model_index:
                continue
            errors = []
            for bone, parent in enumerate(parents):
                if parent < 0:
                    continue
                local_length = math.sqrt(sum(value * value for value in local[bone][12:15]))
                model_distance = math.dist(model[bone][12:15], model[parent][12:15])
                errors.append(abs(local_length - model_distance))
            if not errors:
                continue
            score = sum(errors) / len(errors)
            if best is None or score < best[0]:
                best = (score, local_index, model_index)
    if best is None or best[0] > 0.01:
        raise MeshFormatError("The skeleton bind-pose arrays failed hierarchy validation.")
    model_pose = transform_arrays[best[2]]
    joints = [
        (bone_name, parents[index], *model_pose[index][12:15])
        for index, bone_name in enumerate(bone_names)
    ]
    return SkeletonData(name, joints)
