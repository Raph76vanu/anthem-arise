"""Validated asset links for the sampled Anthem Eclipse sequence layout.

The records identify which controllers participate in a sequence. Their
blend-curve payload is not yet a decoded blend algorithm; no posed animation
should be composed from these references alone.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct

from .frostbite_animation import _ascii_strings, _chunks
from .geometry import MeshFormatError


SEQUENCE_PLAYER_HASH = 0x9089AB84
CONTROLLER_HASH = 0x2FA96633
BLEND_CURVE_HASH = 0x0F66838D
CLIP_INIT_HASH = 0xA30D82B5


@dataclass(frozen=True)
class SequenceSlot:
    controller: str
    controller_key: str
    blend_curve_key: str
    clip_init_key: str
    # These bytes are exposed for comparison, not interpreted as blend rules.
    curve_scalar: float


@dataclass(frozen=True)
class EclipseSequence:
    name: str
    slots: tuple[SequenceSlot, ...]


def inspect_eclipse_sequences(raw: bytes) -> tuple[EclipseSequence, ...]:
    """Resolve the exact three references in each known sequence-player slot.

    The sample layout has an offset table at +0x1c0, with 72-byte typed
    records addressed at each offset + 16. Reject unrecognized layouts rather
    than assigning names or blend semantics from adjacent file order.
    """
    blocks = list(_chunks(raw))
    keyed: dict[tuple[int, bytes], tuple[int, int]] = {}
    for start, end, type_hash in blocks:
        if end - start >= 80 and type_hash in (CONTROLLER_HASH, BLEND_CURVE_HASH, CLIP_INIT_HASH):
            key = raw[start + 72:start + 80]
            if key == bytes(8) or (type_hash, key) in keyed:
                raise MeshFormatError("The sequence resource has a duplicate or empty asset key.")
            keyed[type_hash, key] = (start, end)
    sequences = []
    for start, end, type_hash in blocks:
        if type_hash != SEQUENCE_PLAYER_HASH:
            continue
        length = end - start
        if length < 0x1D0:
            raise MeshFormatError("The Eclipse sequence player is truncated.")
        count, capacity, offset = struct.unpack_from("<IIQ", raw, start + 0x1A0)
        if not 1 <= count <= 256 or count != capacity or offset != 0x1B0 or 0x1C0 + count * 8 > length:
            raise MeshFormatError("The Eclipse sequence slot table has an unfamiliar layout.")
        names = [s for s in _ascii_strings(raw, start, end) if s.startswith("SEQ_")]
        if len(names) != 1:
            raise MeshFormatError("The Eclipse sequence player has no unique sequence name.")
        slots = []
        for index in range(count):
            relative = struct.unpack_from("<Q", raw, start + 0x1C0 + index * 8)[0]
            slot = relative + 16
            if slot < 0x1C0 + count * 8 or slot + 72 > length:
                raise MeshFormatError("An Eclipse sequence slot points outside its object.")
            if struct.unpack_from("<I", raw, start + slot + 16)[0] != 0x6D77D289:
                raise MeshFormatError("An Eclipse sequence slot has an unknown record type.")
            references = [raw[start + slot + 32 + n * 8:start + slot + 40 + n * 8]
                          for n in range(3)]
            resolved = [keyed.get((kind, key)) for kind, key in zip(
                (CONTROLLER_HASH, BLEND_CURVE_HASH, CLIP_INIT_HASH), references)]
            if any(record is None for record in resolved):
                raise MeshFormatError("An Eclipse sequence slot references a missing asset.")
            controller_start, controller_end = resolved[0]
            curve_start, curve_end = resolved[1]
            controller_names = [s for s in _ascii_strings(raw, controller_start, controller_end)
                                if s.startswith("EX")]
            if len(controller_names) != 1 or curve_end - curve_start < 0xAC:
                raise MeshFormatError("An Eclipse sequence slot has an unfamiliar controller or curve.")
            scalar = struct.unpack_from("<f", raw, curve_start + 0xA8)[0]
            slots.append(SequenceSlot(controller_names[0], *(key.hex() for key in references), scalar))
        sequences.append(EclipseSequence(names[0], tuple(slots)))
    return tuple(sequences)
