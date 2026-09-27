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


def _rotation_matrix_to_quaternion(m: tuple[float, ...]) -> tuple[float, float, float, float]:
    """Convert a 3x3 rotation matrix (row-major, 9 floats) to a unit
    quaternion (x, y, z, w), using the standard trace-based method."""
    m00, m01, m02, m10, m11, m12, m20, m21, m22 = m
    trace = m00 + m11 + m22
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w = 0.25 * s
        x = (m21 - m12) / s
        y = (m02 - m20) / s
        z = (m10 - m01) / s
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2
        w = (m21 - m12) / s
        x = 0.25 * s
        y = (m01 + m10) / s
        z = (m02 + m20) / s
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2
        w = (m02 - m20) / s
        x = (m01 + m10) / s
        y = 0.25 * s
        z = (m12 + m21) / s
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2
        w = (m10 - m01) / s
        x = (m02 + m20) / s
        y = (m12 + m21) / s
        z = 0.25 * s
    return (x, y, z, w)


def decode_anthem_primary_rig_joint_order(raw: bytes) -> list[int] | None:
    """Find the skeleton's curated "primary rig" joint list, if present:
    a strictly-ascending array of joint indices covering the game's
    main animated bones (both arms, both legs, spine/neck/head, and
    javelin hardware) WITHOUT per-finger detail. Confirmed on a real
    skeleton (exm/Lancer): a 50-entry array with values like
    (0, 1, 3, 6, 10, 11, ... ) resolving to Hips, Spine, Spine1, ...,
    LeftShoulder, LeftArm, LeftForeArm, LeftHand, LeftHandProp,
    RightShoulder, RightArm, RightForeArm, RightHand, RightHandProp, ...
    -- notably WITH both arms present and WITHOUT any individual finger
    joints, unlike the full skeleton's own joint order.

    This matters for animation channel mapping: a real AntState clip was
    found to have far fewer channels (as few as 30) than the skeleton has
    joints (182), and mapping channels to joints in raw skeleton order
    exhausts the channel budget on the left hand's individual fingers
    before ever reaching the right arm, leaving it completely
    unanimated. This ordering is presumed to match how channels are
    actually assigned; unverified beyond looking structurally right and
    fixing the visibly-asymmetric result.

    Returns None if no such array is found (the skeleton may not have
    one, or the heuristic may need adjustment) so callers can fall back
    to raw skeleton order.
    """
    header = parse_anthem_ebx_header(raw)
    if not header.arrays:
        return None
    strings_end = header.strings_offset + header.strings_length
    arrays_base = strings_end + header.data_length

    name_candidates: list[tuple[int, int]] = []
    for array_index, (offset, count, _class_ref) in enumerate(header.arrays):
        if not 2 <= count <= 4096 or arrays_base + offset + count * 4 > len(raw):
            continue
        values = struct.unpack_from(f"<{count}I", raw, arrays_base + offset)
        names = [_cstring(raw, header.strings_offset + value, strings_end) for value in values]
        if all(names):
            name_candidates.append((array_index, count))
    if not name_candidates:
        return None
    name_index, bone_count = max(name_candidates, key=lambda item: item[1])

    best: tuple[int, int] | None = None  # (count, array_index)
    for array_index, (offset, count, _class_ref) in enumerate(header.arrays):
        if array_index == name_index or not (10 <= count < bone_count):
            continue
        if arrays_base + offset + count * 4 > len(raw):
            continue
        values = struct.unpack_from(f"<{count}i", raw, arrays_base + offset)
        if all(0 <= v < bone_count for v in values) and all(a < b for a, b in zip(values, values[1:])):
            if best is None or count > best[0]:
                best = (count, array_index)
                best_values = values
    if best is None:
        return None
    return list(best_values)



def decode_anthem_skeleton_bind_rotations(raw: bytes) -> list[tuple[float, float, float, float]]:
    """Extract each joint's LOCAL bind-pose rotation (relative to its
    parent), as a unit quaternion (x, y, z, w), in the same joint order as
    decode_anthem_skeleton.

    This is a separate function rather than a change to
    decode_anthem_skeleton's return value, to avoid touching that function's
    existing behaviour/callers. It re-runs the same array-selection logic
    (name array, parent array, and the validated local/model transform
    pair) so the joint order and hierarchy are guaranteed consistent with
    decode_anthem_skeleton on the same input.

    Needed because decode_anthem_skeleton only kept bind-pose *positions*;
    animation channels give a joint's rotation relative to its own rest
    orientation, not relative to global identity, so without this a joint
    whose rest pose is far from identity (e.g. an arm in a T-pose bind)
    would visibly snap toward its raw rest orientation instead of its
    natural relaxed pose. See frostbite_animation_playback.py.
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
    local_pose = transform_arrays[best[1]]

    rotations = []
    for matrix in local_pose:
        # The EBX transforms store column vectors in successive groups of
        # four, with translation in slots 12..14.  The converter accepts
        # row-major entries; feeding it the groups directly produces the
        # inverse of each joint's local bind rotation.
        rotation_3x3 = (matrix[0], matrix[4], matrix[8],
                        matrix[1], matrix[5], matrix[9],
                        matrix[2], matrix[6], matrix[10])
        rotations.append(_rotation_matrix_to_quaternion(rotation_3x3))
    return rotations
