"""Read named rig DOFs from the referenced Anthem Rigamate resource."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
import math
import struct

from .frostbite_animation import EclipseAnimationClip, QuaternionChannel, VectorChannel
from .frostbite_state import iter_gd_data_blocks
from .geometry import SkeletonData


@dataclass(frozen=True)
class RigamateDofTypes:
    joint_names: tuple[str, ...]
    quaternion_ids: frozenset[int]
    vector_ids: frozenset[int]
    # The scalar defaults live in a third array which is not decoded yet.
    quaternion_channel_ids: tuple[int, ...]
    vector_channel_ids: tuple[int, ...]
    float_channel_ids: tuple[int, ...]


@dataclass(frozen=True)
class RigamateBoneMapping:
    """Rotation channels paired with their exact names in the bank's DOF sets."""

    channel_indices: tuple[int, ...]
    bone_indices: tuple[int, ...]
    channel_names: tuple[str, ...]
    skipped_names: tuple[str, ...]
    default_rotations: tuple[tuple[float, float, float, float], ...]
    vector_channel_indices: tuple[int, ...] = ()
    vector_bone_indices: tuple[int, ...] = ()
    vector_channel_names: tuple[str, ...] = ()
    default_positions: tuple[tuple[float, float, float], ...] = ()

    def pose_channels(self, clip: EclipseAnimationClip) -> list[QuaternionChannel]:
        """Return the mapped Eclipse rotations in their stored local space.

        Contact sheets from the supplied EXM Twitch clips show that these
        values replace the skeleton's local bind rotation.  Treating them as
        offsets from Rigamate defaults folds the body into a compact knot;
        applying them as absolute local rotations produces a coherent upright
        figure at every sampled phase.  Keep the original encoding evidence on
        each channel so later codec work remains independently inspectable.
        """
        return [clip.quaternion_channels[index] for index in self.channel_indices]

    def pose_vector_channels(self, clip: EclipseAnimationClip) -> list[VectorChannel]:
        """Return mapped translations in their stored local channel space."""
        return [clip.vector_channels[index] for index in self.vector_channel_indices]


def extract_rigamate_mapping_bank(raw: bytes, expected_rig_key: bytes) -> bytes | None:
    """Keep one RigAsset and only the named DOF sets it references.

    Anthem's PrimaryRig key can resolve to the very large global animation
    bank containing several Javelin rigs.  Passing that whole resource back
    to the GUI is wasteful and also makes an unqualified "one rig" check
    ambiguous.  The selected RigAsset plus its referenced DofSet blocks are
    self-contained for exact channel-to-joint mapping.
    """
    if len(expected_rig_key) != 8:
        return None
    blocks = tuple(iter_gd_data_blocks(raw))
    rigs = [
        block.body for block in blocks
        if block.type_hash == 0xB1240F5A
        and _has_skeleton_name(block.body[:320])
        and len(block.body) >= 224
        and block.body[216:224] == expected_rig_key
    ]
    if len(rigs) != 1:
        return None
    rig = rigs[0]
    if len(rig) < 192:
        return None
    group_count, group_capacity, group_offset = struct.unpack_from("<IIQ", rig, 128)
    if (group_count != group_capacity or not 1 <= group_count <= 4096
            or group_offset + 16 + group_count * 8 > len(rig)):
        return None
    keys = set(struct.unpack_from(f"<{group_count}Q", rig, group_offset + 16))
    groups: dict[int, bytes] = {}
    for block in blocks:
        if block.type_hash != 0xBB267D6A or len(block.body) <= 104:
            continue
        key = struct.unpack_from("<Q", block.body, 88)[0]
        if key in keys:
            if key in groups:
                return None
            groups[key] = block.body
    # Some global banks omit an unused set body.  The decoder already rejects
    # a clip when one of *its* identifiers belongs to a missing set, so retain
    # the useful partial bank rather than rejecting every clip in the rig.
    def detached(body: bytes) -> bytes:
        # GdDataBlock.body follows the declared extent, which in Anthem ends
        # after the next block's eight-byte tag/reserved prefix.  Remove that
        # overlap before joining non-adjacent blocks into a compact resource.
        return body[:-8] if len(body) >= 8 and body[-8:-1] == b"GD.DATA" else body

    return b"".join((detached(rig), *(detached(groups[key]) for key in sorted(groups))))


def stabilize_component_swaps(values: tuple[tuple[float, float, float, float], ...]
                              ) -> tuple[tuple[float, float, float, float], ...]:
    """Legacy 0.27.6 heuristic retained for regression comparisons only.

    For example, the real Sentinel idle RightShoulder has adjacent decoded
    triples (-.234, -.176, .956) and (-.234, .956, -.175). Treating these as
    fixed XYZ components invents a roughly 130-degree shoulder twitch. Only
    switch slots when the old jump is >80 degrees and the reordered key is
    within 15 degrees of the preceding key. The active 48-bit codec handles
    omitted-component selection and never invokes this heuristic.
    """
    if not values:
        return values
    repaired = [values[0]]
    for value in values[1:]:
        previous = repaired[-1]

        def dot(candidate):
            return abs(sum(a * b for a, b in zip(previous, candidate)))

        original_dot = dot(value)
        if original_dot < 0.7660444431:  # rotation jump > 80 degrees
            options = (xyz + (value[3],) for xyz in set(permutations(value[:3])))
            best = max(options, key=dot)
            if dot(best) > 0.9914448614:  # corrected jump < 15 degrees
                value = best
        repaired.append(value)
    return tuple(repaired)


def decode_rigamate_bone_mapping(
    raw: bytes, clip: EclipseAnimationClip, skeleton: SkeletonData, *,
    include_constants: bool = False, expected_rig_key: bytes | None = None,
) -> RigamateBoneMapping | None:
    """Resolve clip quaternion IDs through RigAsset -> DofSetList -> joint name.

    The bank's RigDofSets array partitions DofIds; matching set keys own the
    same-length arrays of strings (e.g. Head.q). Only .q entries with an exact
    named joint in the selected bind skeleton are eligible for posing. Missing
    bank groups are tolerated only when none of this clip's DOFs use them.
    """
    if not clip.mapped or len(set(clip.dof_ids)) != len(clip.dof_ids):
        return None
    rigs = []
    groups: dict[int, bytes] = {}
    for block in iter_gd_data_blocks(raw):
        body = block.body
        if block.type_hash == 0xB1240F5A and _has_skeleton_name(body[:320]):
            rigs.append(body)
        elif block.type_hash == 0xBB267D6A and len(body) > 104:
            key = struct.unpack_from("<Q", body, 88)[0]
            if key in groups:
                return None
            groups[key] = body
    if expected_rig_key is not None:
        if len(expected_rig_key) != 8:
            return None
        rigs = [body for body in rigs if len(body) >= 224 and body[216:224] == expected_rig_key]
    if len(rigs) != 1:
        return None
    rig = rigs[0]
    if len(rig) < 192:
        return None
    # RigAsset.__key occupies byte 216 in the supplied EXM bank. When the
    # animation explicitly names a PrimaryRigFeature.Rig, do not accept a
    # bank solely because its DOF names happen to match this skeleton.
    if expected_rig_key is not None and (
        len(rig) < 224 or rig[216:224] != expected_rig_key
    ):
        return None
    dof_count, dof_capacity, dof_offset = struct.unpack_from("<IIQ", rig, 48)
    quat_count, quat_capacity, quat_offset = struct.unpack_from("<IIQ", rig, 144)
    vector_count, vector_capacity, vector_offset = struct.unpack_from("<IIQ", rig, 160)
    group_count, group_capacity, group_offset = struct.unpack_from("<IIQ", rig, 128)
    index_count, index_capacity, index_offset = struct.unpack_from("<IIQ", rig, 64)
    if (dof_count != dof_capacity or group_count != group_capacity or
            group_count != index_count or index_count != index_capacity or
            quat_count != quat_capacity or vector_count != vector_capacity or
            not 1 <= quat_count <= 100_000 or not 0 <= vector_count <= 100_000 or
            not 1 <= group_count <= 4096 or not 1 <= dof_count <= 100_000 or
            dof_offset + 16 + dof_count * 4 > len(rig) or
            quat_offset + quat_count * 32 > len(rig) or
            vector_offset + vector_count * 32 > len(rig) or
            group_offset + 16 + group_count * 8 > len(rig) or
            index_offset + 16 + index_count * 4 > len(rig)):
        return None
    # The array offsets are anchored at the object header; payloads start 16
    # bytes later. Validate every index and name before pairing any channel.
    dofs = struct.unpack_from(f"<{dof_count}I", rig, dof_offset + 16)
    defaults = {}
    for i in range(quat_count):
        pos = quat_offset + i * 32
        identifier = struct.unpack_from("<I", rig, pos)[0]
        rotation = struct.unpack_from("<4f", rig, pos + 16)
        if identifier in defaults or not 0.98 <= sum(x * x for x in rotation) <= 1.02:
            return None
        defaults[identifier] = rotation
    vector_defaults = {}
    for i in range(vector_count):
        pos = vector_offset + i * 32
        identifier = struct.unpack_from("<I", rig, pos)[0]
        value = struct.unpack_from("<3f", rig, pos + 16)
        if identifier in vector_defaults or not all(math.isfinite(x) for x in value):
            return None
        vector_defaults[identifier] = value
    set_keys = struct.unpack_from(f"<{group_count}Q", rig, group_offset + 16)
    indices = struct.unpack_from(f"<{index_count}I", rig, index_offset + 16)
    if indices[0] != 0 or any(a >= b for a, b in zip(indices, indices[1:])) or indices[-1] >= dof_count:
        return None
    clip_ids = set(clip.dof_ids)
    resolved: dict[int, str] = {}
    for group_i, key in enumerate(set_keys):
        first = indices[group_i]
        last = indices[group_i + 1] if group_i + 1 < group_count else dof_count
        body = groups.get(key)
        if body is None:
            if clip_ids.intersection(dofs[first:last]):
                return None
            continue
        if len(body) <= 104 or struct.unpack_from("<I", body, 64)[0] != last - first or \
                struct.unpack_from("<I", body, 68)[0] != last - first:
            return None
        end = body.find(b"\0", 101, min(len(body), 220))
        if end < 0:
            return None
        descriptor = (end + 8) & ~7
        if descriptor + (last - first) * 24 > len(body):
            return None
        for local_i in range(last - first):
            size, capacity, name_offset = struct.unpack_from("<IIQ", body, descriptor + local_i * 24)
            name_offset += 16
            if size != capacity or not 2 <= size <= 128 or name_offset + size > len(body):
                return None
            name_bytes = body[name_offset:name_offset + size]
            if name_bytes[-1] != 0:
                return None
            try:
                name = name_bytes[:-1].decode("ascii")
            except UnicodeDecodeError:
                return None
            identifier = dofs[first + local_i]
            if identifier in resolved:
                return None
            resolved[identifier] = name
    if not clip_ids <= resolved.keys():
        return None
    joints = {entry[0]: index for index, entry in enumerate(skeleton.joints)}
    channel_indices: list[int] = []
    bone_indices: list[int] = []
    channel_names: list[str] = []
    skipped: list[str] = []
    default_rotations: list[tuple[float, float, float, float]] = []
    used_bones: set[int] = set()
    for channel_index, identifier in enumerate(clip.dof_ids[:len(clip.quaternion_channels)]):
        name = resolved[identifier]
        if not name.endswith(".q") or identifier not in defaults:
            return None
        joint = joints.get(name[:-2])
        if joint is None:
            skipped.append(name)
            continue
        if joint in used_bones:
            return None  # multiple curves targeting one joint need blending
        used_bones.add(joint)
        if include_constants or clip.quaternion_channels[channel_index].values:
            channel_indices.append(channel_index)
            bone_indices.append(joint)
            channel_names.append(name)
            # Keep defaults in the same compact order as channel_indices and
            # bone_indices.  Rigamate contains helper DOFs which are absent
            # from a render skeleton; appending their defaults here shifted
            # every later default onto the wrong bone.
            default_rotations.append(defaults[identifier])
    vector_channel_indices: list[int] = []
    vector_bone_indices: list[int] = []
    vector_channel_names: list[str] = []
    default_positions: list[tuple[float, float, float]] = []
    used_vector_bones: set[int] = set()
    vector_ids = clip.dof_ids[
        len(clip.quaternion_channels):
        len(clip.quaternion_channels) + len(clip.vector_channels)
    ]
    for channel_index, identifier in enumerate(vector_ids):
        name = resolved[identifier]
        if "." not in name or identifier not in vector_defaults:
            return None
        joint = joints.get(name.rsplit(".", 1)[0])
        if joint is None:
            skipped.append(name)
            continue
        if joint in used_vector_bones:
            return None
        used_vector_bones.add(joint)
        vector_channel_indices.append(channel_index)
        vector_bone_indices.append(joint)
        vector_channel_names.append(name)
        default_positions.append(vector_defaults[identifier])
    if not channel_indices:
        return None
    return RigamateBoneMapping(
        tuple(channel_indices), tuple(bone_indices), tuple(channel_names),
        tuple(skipped), tuple(default_rotations), tuple(vector_channel_indices),
        tuple(vector_bone_indices), tuple(vector_channel_names), tuple(default_positions),
    )


def inspect_rigamate_dof_types(raw: bytes, clip: EclipseAnimationClip) -> RigamateDofTypes | None:
    """Cross-check a clip against the named EXM skeleton and its typed DOFs.

    Return None for unfamiliar layouts, incomplete banks, mismatched groups,
    or repeated channel identifiers. No guessed bone assignment is returned.
    """
    if not clip.mapped or len(set(clip.dof_ids)) != len(clip.dof_ids):
        return None
    name_blocks = []
    dof_blocks = []
    for block in iter_gd_data_blocks(raw):
        if block.type_hash == 0x14A6DA9B and _has_skeleton_name(block.body[:160]):
            name_blocks.append(block.body)
        elif block.type_hash == 0xB1240F5A and _has_skeleton_name(block.body[:320]):
            dof_blocks.append(block.body)
    if len(name_blocks) != 1 or len(dof_blocks) != 1:
        return None
    names_raw, data = name_blocks[0], dof_blocks[0]
    if len(names_raw) < 128 or len(data) < 192:
        return None
    count = struct.unpack_from("<I", names_raw, 64)[0]
    if not 1 <= count <= 4096 or struct.unpack_from("<I", names_raw, 68)[0] != count:
        return None
    # Name descriptors are 24 bytes each, starting after the object's name.
    first_descriptor = 120
    if first_descriptor + count * 24 > len(names_raw):
        return None
    names = []
    for i in range(count):
        at = first_descriptor + i * 24
        size, capacity, name_offset = struct.unpack_from("<IIQ", names_raw, at)
        # This object's string offsets are relative to the end of the
        # sixteen-byte array descriptor (unlike the DOF table offsets).
        name_offset += 16
        if size != capacity or not 1 <= size <= 128 or name_offset + size > len(names_raw):
            return None
        word = names_raw[name_offset:name_offset + size]
        if word[-1] != 0:
            return None
        try:
            names.append(word[:-1].decode("ascii"))
        except UnicodeDecodeError:
            return None
    if not names or names[0] != "Reference" or len(set(names)) != len(names):
        return None

    # AntState array headers carry count, capacity and offset. The first
    # packed transform table is 32-byte vectors, the second 32-byte quats.
    vector_count, vector_capacity, vector_start = struct.unpack_from("<IIQ", data, 160)
    quat_count, quat_capacity, quat_start = struct.unpack_from("<IIQ", data, 144)
    if (vector_count != vector_capacity or quat_count != quat_capacity or
            not 1 <= vector_count <= 100_000 or not 1 <= quat_count <= 100_000 or
            vector_start + vector_count * 32 > len(data) or
            quat_start + quat_count * 32 > len(data)):
        return None
    vectors = frozenset(struct.unpack_from("<I", data, vector_start + i * 32)[0]
                        for i in range(vector_count))
    quats = frozenset(struct.unpack_from("<I", data, quat_start + i * 32)[0]
                      for i in range(quat_count))
    if len(vectors) != vector_count or len(quats) != quat_count or vectors & quats:
        return None
    nq, nv, nf = len(clip.quaternion_channels), len(clip.vector_channels), len(clip.float_channels)
    q, v, f = clip.dof_ids[:nq], clip.dof_ids[nq:nq + nv], clip.dof_ids[nq + nv:]
    if (len(f) != nf or not set(q) <= quats or not set(v) <= vectors or
            set(f) & (quats | vectors)):
        return None
    return RigamateDofTypes(tuple(names), quats, vectors, q, v, f)


def _has_skeleton_name(raw: bytes) -> bool:
    """Recognize any named Javelin rig, not only Lancer's EXM skeleton.

    Anthem uses the same RigAsset/DofSet layout for EXM, EXF, EXH and EXL,
    while the embedded object name carries the family prefix.  Requiring the
    literal ``EXM_skeleton`` made valid Interceptor banks invisible and forced
    playback onto the unsafe sequential-bone fallback.
    """
    lower = raw.lower()
    return any(marker in lower for marker in (
        b"exm_skeleton\0", b"exf_skeleton\0",
        b"exh_skeleton\0", b"exl_skeleton\0",
    ))
