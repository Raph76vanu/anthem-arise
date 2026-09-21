"""Bounded decoder for Anthem's embedded Eclipse animation streams.

The Gameplay Data resource used by Anthem stores one or more
``EclipseAnimationAsset`` objects.  Each object contains typed channel tables
followed by a shared key stream.  This module deliberately stops before rig
retargeting: ``ChannelToDofAsset`` stores opaque DOF identifiers which require
the referenced animation bank to map them to SkeletonAsset bones.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import struct

from .geometry import MeshFormatError


DATA_MAGIC = b"GD.DATAl"
ECLIPSE_ANIMATION_HASH = 0xB4C3FEA3
CHANNEL_TO_DOF_HASH = 0xCF166370
CLIP_CONTROLLER_HASH = 0x2FA96633
BANK_POINTER_HASH = 0xCD0FFF58
MAX_CHANNELS = 4096
MAX_KEYS = 1_000_000


@dataclass(frozen=True)
class FloatChannel:
    times: tuple[int, ...]
    values: tuple[float, ...]


@dataclass(frozen=True)
class VectorChannel:
    times: tuple[int, ...]
    values: tuple[tuple[float, float, float], ...]


@dataclass(frozen=True)
class QuaternionChannel:
    times: tuple[int, ...]
    values: tuple[tuple[float, float, float, float], ...]
    constant_encoding: bytes | None = None
    # Retain original packed words for independent validation and diagnostics.
    packed_words: tuple[tuple[int, int, int], ...] = ()


@dataclass(frozen=True)
class EclipseAnimationClip:
    name: str
    asset_id: str
    frame_count: int
    float_channels: tuple[FloatChannel, ...]
    vector_channels: tuple[VectorChannel, ...]
    quaternion_channels: tuple[QuaternionChannel, ...]
    dof_ids: tuple[int, ...]

    @property
    def channel_count(self) -> int:
        return (
            len(self.float_channels)
            + len(self.vector_channels)
            + len(self.quaternion_channels)
        )

    @property
    def mapped(self) -> bool:
        return len(self.dof_ids) == self.channel_count


@dataclass(frozen=True)
class BankPointer:
    """One Anthem Gameplay Data pointer to a virtual animation-bank asset."""

    name: str
    asset_key: bytes
    subject_key: bytes
    external: bool


def _u32(raw: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(raw):
        raise MeshFormatError("The Eclipse animation header is truncated.")
    return struct.unpack_from("<I", raw, offset)[0]


def _chunks(raw: bytes):
    cursor = 0
    while True:
        start = raw.find(DATA_MAGIC, cursor)
        if start < 0:
            return
        length = _u32(raw, start + 8)
        if length < 40 or start + length > len(raw):
            raise MeshFormatError("A Gameplay Data object points outside the animation resource.")
        yield start, start + length, _u32(raw, start + 32)
        cursor = start + length


def _ascii_strings(raw: bytes, start: int, end: int) -> list[str]:
    values: list[str] = []
    for match in re.finditer(rb"[ -~]{4,160}\0", raw[start:end]):
        values.append(match.group()[:-1].decode("ascii", "replace"))
    return values


def has_gd_data_asset_key(raw: bytes, key: bytes) -> bool:
    """True when *key* occurs inside a reflected GD.DATA object.

    Inheritance moves ``__key`` between object types (for example it is at
    block+72 on BankPointerAsset and block+88 on PrimaryRigFeatureAsset), so
    this intentionally validates the containing object rather than assuming a
    universal byte offset. Callers resolving an external key use this on a
    different resource, where an occurrence is the wanted virtual asset.
    """
    if len(key) != 8:
        return False
    return any(
        type_hash != BANK_POINTER_HASH and key in raw[start:end]
        for start, end, type_hash in _chunks(raw)
    )


def decode_bank_pointers(raw: bytes) -> tuple[BankPointer, ...]:
    """Decode ``BankPointerAsset`` keys from an Anthem AntState payload.

    The common object fields occupy bytes 48..79 of the GD.DATA block. The
    BankPointer's ``SubjectBankAsset`` field is the following eight bytes. A
    pointer is external when no object in the same resource owns that subject
    key; this is the case for Sentinel's Rigamate bank.
    """
    chunks = list(_chunks(raw))
    pointers: list[BankPointer] = []
    for start, end, type_hash in chunks:
        if type_hash != BANK_POINTER_HASH:
            continue
        object_base = start + 48
        if object_base + 40 > end:
            raise MeshFormatError("A BankPointerAsset object is truncated.")
        asset_key = raw[object_base + 24:object_base + 32]
        subject_key = raw[object_base + 32:object_base + 40]
        name_start = object_base + 40
        name_end = raw.find(b"\0", name_start, min(end, name_start + 161))
        if name_end < 0:
            raise MeshFormatError("A BankPointerAsset has no bounded name string.")
        try:
            name = raw[name_start:name_end].decode("ascii")
        except UnicodeDecodeError as error:
            raise MeshFormatError("A BankPointerAsset name is not ASCII.") from error
        if not name.startswith("BankPointer."):
            raise MeshFormatError("A BankPointerAsset has an unexpected object layout.")
        locally_resolved = any(
            other_start != start and subject_key in raw[other_start:other_end]
            for other_start, other_end, _other_hash in chunks
        )
        pointers.append(BankPointer(
            name=name,
            asset_key=asset_key,
            subject_key=subject_key,
            external=not locally_resolved,
        ))
    return tuple(pointers)


def _read_times(
    stream: bytes, offset: int, count: int, item_size: int,
) -> tuple[int, ...]:
    byte_count = count * item_size
    if count <= 0 or count > MAX_KEYS or offset < 0 or offset + byte_count > len(stream):
        raise MeshFormatError("An Eclipse key-time range points outside its stream.")
    if item_size == 1:
        result = tuple(stream[offset:offset + count])
    elif item_size == 2:
        # Like quantized values, 16-bit Eclipse stream scalars are big endian.
        result = struct.unpack_from(f">{count}H", stream, offset)
    else:
        raise MeshFormatError("The Eclipse key-time format is not supported.")
    if any(current < previous for previous, current in zip(result, result[1:])):
        raise MeshFormatError("An Eclipse key-time channel is not monotonic.")
    return result


def _decode_float_channels(
    raw: bytes, start: int, count: int, stream: bytes, time_size: int,
) -> tuple[FloatChannel, ...]:
    result: list[FloatChannel] = []
    for index in range(count):
        key_count, times_offset, values_offset, minimum, value_range = struct.unpack_from(
            "<IIIff", raw, start + index * 20,
        )
        if key_count == 2 and times_offset == 0:
            result.append(FloatChannel((0, values_offset), (minimum, minimum)))
            continue
        times = _read_times(stream, times_offset, key_count, time_size)
        if values_offset + key_count > len(stream):
            raise MeshFormatError("An Eclipse float-value range points outside its stream.")
        values = tuple(
            minimum + value_range * value / 255.0
            for value in stream[values_offset:values_offset + key_count]
        )
        result.append(FloatChannel(times, values))
    return tuple(result)


def _decode_vector_channels(
    raw: bytes, start: int, count: int, stream: bytes, time_size: int,
) -> tuple[VectorChannel, ...]:
    result: list[VectorChannel] = []
    for index in range(count):
        offset = start + index * 48
        key_count, times_offset, values_offset = struct.unpack_from("<III", raw, offset)
        minimum = struct.unpack_from("<3f", raw, offset + 16)
        value_range = struct.unpack_from("<3f", raw, offset + 32)
        if key_count == 2 and times_offset == 0:
            value = tuple(float(component) for component in minimum)
            result.append(VectorChannel((0, values_offset), (value, value)))
            continue
        times = _read_times(stream, times_offset, key_count, time_size)
        byte_count = key_count * 6
        if values_offset + byte_count > len(stream):
            raise MeshFormatError("An Eclipse vector-value range points outside its stream.")
        values = []
        for key in range(key_count):
            # Eclipse writes the quantized words in network byte order even
            # though the surrounding Gameplay Data structures are little endian.
            quantized = struct.unpack_from(">3H", stream, values_offset + key * 6)
            values.append(tuple(
                minimum[axis] + value_range[axis] * quantized[axis] / 65535.0
                for axis in range(3)
            ))
        result.append(VectorChannel(times, tuple(values)))
    return tuple(result)


def _decode_quaternion_channels(
    raw: bytes, start: int, count: int, stream: bytes, time_size: int,
) -> tuple[QuaternionChannel, ...]:
    result: list[QuaternionChannel] = []
    for index in range(count):
        offset = start + index * 12
        key_count, times_offset, values_offset = struct.unpack_from("<III", raw, offset)
        if key_count == 1:
            # Constant quaternions use the two offset words as a separate
            # 64-bit inline codec. Preserve it verbatim. Dynamic channels
            # below use a distinct 48-bit dynamic-key interpretation.
            result.append(QuaternionChannel((), (), raw[offset + 4:offset + 12]))
            continue
        times = _read_times(stream, times_offset, key_count, time_size)
        byte_count = key_count * 6
        if values_offset + byte_count > len(stream):
            raise MeshFormatError("An Eclipse quaternion-value range points outside its stream.")
        values = []
        packed_words = []
        for key in range(key_count):
            packed = struct.unpack_from(">3H", stream, values_offset + key * 6)
            packed_words.append(packed)
            values.append(decode_packed_quaternion_48(packed))
        result.append(QuaternionChannel(times, tuple(values), packed_words=tuple(packed_words)))
    return tuple(result)


def decode_packed_quaternion_48(words: tuple[int, int, int]) -> tuple[float, float, float, float]:
    """Decode the smallest-three dynamic rotation layout.

    The low bit in each of the first two big-endian words selects the omitted
    quaternion component (00=X, 01=Y, 10=Z, 11=W). Their remaining 15 bits
    and the full third word encode the other three values in increasing axis
    order, within [-1/sqrt(2), +1/sqrt(2)]. The omitted component is positive.
    Cross-checked against Outlaw Ranger, Lancer preview and shared EXM curves;
    game-accurate pose validation remains to be done.
    """
    first, second, third = words
    omitted = ((first & 1) << 1) | (second & 1)
    components = (
        ((first >> 1) - 16383.5) / (16383.5 * math.sqrt(2)),
        ((second >> 1) - 16383.5) / (16383.5 * math.sqrt(2)),
        (third - 32767.5) / (32767.5 * math.sqrt(2)),
    )
    result = [0.0] * 4
    others = (axis for axis in range(4) if axis != omitted)
    for axis, component in zip(others, components):
        result[axis] = component
    remaining = 1.0 - sum(component * component for component in components)
    if remaining < -0.0001:
        raise MeshFormatError("A packed Eclipse quaternion exceeds unit length.")
    result[omitted] = math.sqrt(max(0, remaining))
    return tuple(result)


def _controller_names_and_keys(raw: bytes, chunks: list[tuple[int, int, int]]):
    result: dict[bytes, str] = {}
    for start, end, type_hash in chunks:
        if type_hash != CLIP_CONTROLLER_HASH:
            continue
        names = [value for value in _ascii_strings(raw, start, end) if value.startswith("EX")]
        if not names:
            continue
        # ClipControllerAsset contains a DataRef to its EclipseAnimationAsset.
        # Match it against the verified eight-byte asset keys rather than
        # relying on object order.
        result[raw[start + 80:start + 88]] = names[0]
    return result


def _dof_sets(raw: bytes, chunks: list[tuple[int, int, int]]) -> list[tuple[int, ...]]:
    result: list[tuple[int, ...]] = []
    for start, end, type_hash in chunks:
        if type_hash != CHANNEL_TO_DOF_HASH:
            continue
        count = _u32(raw, start + 48)
        if not 1 <= count <= MAX_CHANNELS:
            raise MeshFormatError("A ChannelToDofAsset has an invalid identifier count.")
        strings = list(re.finditer(rb"ToolChannelToDofSetVirtualAssetData\0", raw[start:end]))
        if not strings:
            raise MeshFormatError("A ChannelToDofAsset has no recognizable data payload.")
        values_start = start + strings[0].end()
        if values_start + count * 4 > end:
            raise MeshFormatError("A ChannelToDofAsset identifier table is truncated.")
        result.append(struct.unpack_from(f"<{count}I", raw, values_start))
    return result


def decode_anthem_animation_stream(raw: bytes) -> tuple[EclipseAnimationClip, ...]:
    """Decode Eclipse curve data and verified ChannelToDof associations.

    Bone names are intentionally not assigned here.  The DOF identifiers are
    bank-local values, not SkeletonAsset indices; applying them without the
    referenced rig bank would visibly corrupt the character.
    """
    chunks = list(_chunks(raw))
    controllers = _controller_names_and_keys(raw, chunks)
    dof_sets = _dof_sets(raw, chunks)
    decoded: list[EclipseAnimationClip] = []
    for start, end, type_hash in chunks:
        if type_hash != ECLIPSE_ANIMATION_HASH:
            continue
        vector_count = _u32(raw, start + 80)
        quaternion_count = _u32(raw, start + 96)
        float_count = _u32(raw, start + 112)
        # Some shared EXM locomotion clips have no scalar curves at all.
        # Their empty float descriptor is (count=0, capacity=0, offset=0).
        if (float_count > MAX_CHANNELS or not 0 < vector_count <= MAX_CHANNELS
                or not 0 < quaternion_count <= MAX_CHANNELS):
            raise MeshFormatError("An Eclipse animation has invalid channel counts.")
        identifier = re.search(rb"[0-9A-F]{16}\0", raw[start:min(end, start + 192)])
        if identifier is None:
            raise MeshFormatError("An Eclipse animation has no asset identifier.")
        asset_id = identifier.group()[:-1].decode("ascii")
        # The array descriptors carry offsets anchored after GD.DATA's
        # 16-byte header. Some clips align the vector table with eight bytes
        # of padding after the float table; deriving the next address by
        # adding record sizes misreads their first vector as a key count.
        float_size, float_capacity, float_offset = struct.unpack_from("<IIQ", raw, start + 112)
        float_start = start + 16 + float_offset if float_count else start + 16
        vector_start = start + 16 + struct.unpack_from("<Q", raw, start + 88)[0]
        quaternion_start = start + 16 + struct.unpack_from("<Q", raw, start + 104)[0]
        stream_count, stream_capacity, stream_offset = struct.unpack_from("<IIQ", raw, start + 48)
        stream_start = start + 16 + stream_offset
        if (stream_count != stream_capacity or not 0 < stream_count <= MAX_KEYS * 8
                or float_size != float_capacity
                or (float_count == 0 and float_offset != 0)
                or (float_count > 0 and float_start != start + ((identifier.end() + 3) & ~3))
                or (float_count > 0 and float_start + float_count * 20 > vector_start)
                or (float_count == 0 and vector_start < start + identifier.end())
                or vector_start + vector_count * 48 > quaternion_start
                or quaternion_start + quaternion_count * 12 > stream_start
                or stream_start + stream_count > end):
            raise MeshFormatError("The Eclipse channel offsets exceed or overlap their Gameplay Data object.")
        stream = raw[stream_start:stream_start + stream_count]
        key_time_format = raw[start + 147]
        time_size = {0: 1, 1: 2}.get(key_time_format)
        if time_size is None:
            raise MeshFormatError(
                f"Eclipse key-time format {key_time_format} is not supported."
            )
        floats = _decode_float_channels(
            raw, float_start, float_count, stream, time_size,
        )
        vectors = _decode_vector_channels(
            raw, vector_start, vector_count, stream, time_size,
        )
        quaternions = _decode_quaternion_channels(
            raw, quaternion_start, quaternion_count, stream, time_size,
        )
        key = raw[start + 136:start + 144]
        name = controllers.get(key, f"Eclipse clip {asset_id}")
        channel_count = float_count + vector_count + quaternion_count
        matches = [values for values in dof_sets if len(values) == channel_count]
        dof_ids = matches[0] if len(matches) == 1 else ()
        frame_count = max(
            (times[-1] for channel in (*floats, *vectors, *quaternions)
             if (times := channel.times)),
            default=0,
        )
        decoded.append(EclipseAnimationClip(
            name, asset_id, frame_count, floats, vectors, quaternions, dof_ids,
        ))
    if not decoded:
        raise MeshFormatError("No EclipseAnimationAsset was found in this RES payload.")
    return tuple(decoded)


def inspect_anthem_animation_stream(raw: bytes) -> dict[str, object]:
    clips = decode_anthem_animation_stream(raw)
    bank_pointers = decode_bank_pointers(raw)
    return {
        "decoded_clips": len(clips),
        "clip_names": tuple(clip.name for clip in clips),
        "channel_counts": tuple(clip.channel_count for clip in clips),
        "frame_counts": tuple(clip.frame_count for clip in clips),
        "dof_mappings": tuple(clip.mapped for clip in clips),
        "dynamic_quaternion_channels": tuple(
            sum(bool(channel.times) for channel in clip.quaternion_channels)
            for clip in clips
        ),
        "needs_rig_bank": True,
        "bank_pointers": tuple(pointer.name for pointer in bank_pointers),
        "bank_subject_keys": tuple(pointer.subject_key.hex() for pointer in bank_pointers),
        "external_bank_pointers": tuple(
            pointer.name for pointer in bank_pointers if pointer.external
        ),
    }
