"""Read-only reader for Anthem's "AntState" / BundleGen state-graph resources.

These are a different beast from the compiled EBX game assets that
frostbite_ebx.py handles. Files under an animation-path with a ``.res``
extension whose payload starts with the tag ``GD.REFL`` are not raw compressed
keyframe data: they are a small generic reflection-serialized object graph
(a blend-tree / state-machine description authored by Anthem's tooling), with
three named sections:

- ``GD.STRM`` -- a string pool. Not decoded here.
- ``GD.REFL`` -- a type/reflection table describing every primitive and class
  referenced by this resource (Bool, Int32, Float, Vector3, Quaternion, and
  authored classes such as ``EclipseAnimationFloatChannel``).
- ``GD.DATA`` -- one block per authored object instance, tagged with a 32-bit
  type hash rather than a readable name.

What this module actually decodes, verified against real extracted Anthem
Sentinel/Lancer "AntState" samples:

- The three section headers and the REFL type-index table.
- Every primitive type record (``Bool``, ``Int8``, ... ``Float``): a name and
  two accompanying integers this module calls ``size``/``align``. Their
  values do NOT match native C storage sizes (e.g. ``UInt32`` reads as 8,
  ``Int64`` reads as 4, ``Float`` reads as 16) and what they actually mean is
  not understood yet -- they are exposed as-read rather than reinterpreted,
  and nothing else in this module depends on them being "correct" in that
  sense. Each field descriptor's own ``size`` (used for byte-layout
  purposes) is read directly from that field's record, independent of this.
- The field layout of any class whose byte layout follows the plain
  "N x 32-byte scalar-field descriptor immediately before the type's own
  name" shape -- this covers ``Vector3``, ``Quaternion``, and, importantly,
  the three ``EclipseAnimation*Channel`` classes that describe compressed
  keyframe channels (``KeyCount``, ``KeyTimes``, ``KeyValues``, and for
  Float/Vector channels a ``Min``/``Range`` pair used for dequantization;
  Quaternion channels carry no Min/Range, consistent with a unit-quaternion
  bit-packing scheme rather than linear dequantization).
- Every ``GD.DATA`` instance block: its offset, declared size, and the
  32-bit type hash used to distinguish object types in this format (this
  hash does not match FNV-1, FNV-1a, DJB2, SDBM, Jenkins one-at-a-time, or
  CRC32 of the class name in any case/encoding variant tried so far, so it
  cannot yet be resolved back to a class name purely from the hash --
  correlating a block's hash to a class currently means inspecting its
  content, the way ``find_gd_data_blocks_by_hash`` groups blocks together
  for exactly that purpose).

What this module does NOT decode yet:

- Fields on classes that are arrays or nested references (for example
  ``EclipseAnimationAsset.FloatChannels``) -- these use a different, larger
  field-descriptor shape that has not been reverse engineered.
- The actual bit layout of a Data blob's packed keyframe bytes (the
  ``QuatBits`` / ``FloatFormat`` / ``VectorFormat`` / ``KeyTimeFormat``
  enumeration values that would tell us exactly how to unpack them).
- Which GD.DATA block, concretely, is *this* AntState's
  ``EclipseAnimationAsset`` instance for its embedded clip(s).

This module raises ``AntStateFormatError`` rather than guessing when a
record does not match one of the validated shapes above.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator


class AntStateFormatError(RuntimeError):
    pass


_SECTION_TAGS = (b"GD.STRM", b"GD.REFL", b"GD.DATA")
_TRAILING_PAD = 16  # reserved bytes following every REFL type record


def is_antstate_resource(raw: bytes) -> bool:
    """True if this decoded RES payload is a GD.REFL-style state graph."""
    return raw.find(b"GD.REFL") >= 0


@dataclass(frozen=True)
class GdSection:
    tag: str
    offset: int
    length_a: int
    length_b: int


def find_gd_sections(raw: bytes) -> dict[str, GdSection]:
    """Locate the first GD.STRM and GD.REFL section headers.

    GD.DATA occurs many times (one per object instance) and is handled
    separately by iter_gd_data_blocks.
    """
    sections: dict[str, GdSection] = {}
    for tag in (b"GD.STRM", b"GD.REFL"):
        offset = raw.find(tag)
        if offset < 0:
            continue
        after = offset + len(tag) + 1  # tag + 1 reserved byte
        if after + 8 > len(raw):
            raise AntStateFormatError(f"{tag!r} section header is truncated.")
        length_a, length_b = struct.unpack_from("<2I", raw, after)
        sections[tag.decode("ascii")] = GdSection(tag.decode("ascii"), offset, length_a, length_b)
    return sections


@dataclass(frozen=True)
class ReflectionField:
    name: str
    type_index: int  # 1-based index into the type table returned by parse_primitive_types
    size: int
    byte_offset: int


@dataclass(frozen=True)
class ReflectionType:
    index: int
    name: str
    kind: str  # "primitive" or "composite"
    size: int
    align: int
    fields: tuple[ReflectionField, ...] = ()


def _cstr(raw: bytes, offset: int, limit: int) -> tuple[str, int]:
    end = raw.find(b"\0", offset, limit)
    if end < 0:
        raise AntStateFormatError(f"Unterminated string at {offset:#x}.")
    return raw[offset:end].decode("ascii"), end + 1


def _try_read_primitive(raw: bytes, pos: int, limit: int):
    if pos + 16 > limit or raw[pos] != 0:
        return None
    empty_name = raw[pos + 1] == 0
    header_len = 16 if empty_name else 24
    if pos + header_len > limit:
        return None
    if empty_name:
        name = ""
        _base_type, size, align = struct.unpack_from("<3i", raw, pos + 4)
    else:
        name_end = raw.find(b"\0", pos + 1, pos + 12)
        if name_end < 0:
            return None
        name_bytes = raw[pos + 1:name_end]
        if not name_bytes or not all(32 <= b < 127 for b in name_bytes):
            return None
        name = name_bytes.decode("ascii")
        _base_type, size, align = struct.unpack_from("<3i", raw, pos + 12)
    if not (0 < size <= 64 and 0 < align <= 64):
        return None
    return ReflectionType(index=-1, name=name, kind="primitive", size=size, align=align), header_len + _TRAILING_PAD


def parse_primitive_types(raw: bytes) -> tuple[ReflectionType, ...]:
    """Decode the REFL type table's primitive entries (Bool .. Float, etc).

    Stops (without error) at the first entry it cannot validate as a plain
    primitive record -- in every sample seen this is exactly where the
    composite/class entries begin, since primitives are declared first.
    Composite class layouts are not general-purpose decoded here; use
    find_class_fields for a specific known class name instead.
    """
    sections = find_gd_sections(raw)
    refl = sections.get("GD.REFL")
    if refl is None:
        raise AntStateFormatError("No GD.REFL section found in this resource.")
    after = refl.offset + 8
    type_count = struct.unpack_from("<I", raw, after + 8)[0]
    table_start = after + 16
    table_end = table_start + type_count * 8
    section_limit = after + refl.length_a

    types: list[ReflectionType] = []
    pos = table_end
    index = 0
    while pos < section_limit and index < type_count:
        result = _try_read_primitive(raw, pos, section_limit)
        if result is None:
            break
        rec, record_len = result
        types.append(ReflectionType(index=index, name=rec.name, kind="primitive", size=rec.size, align=rec.align))
        pos += record_len
        index += 1
    return tuple(types)


def find_class_fields(raw: bytes, class_name: str, known_type_count: int = 64) -> ReflectionType:
    """Decode one named class's scalar field layout by walking backwards from its name.

    This works for classes whose members are plain scalar/value-type fields
    stored as 32-byte descriptors immediately before the class's own name
    string (validated against Vector3, Quaternion, and the three
    EclipseAnimation*Channel classes). It does NOT work for classes with
    array or nested-reference fields (e.g. EclipseAnimationAsset itself,
    whose FloatChannels/VectorChannels/QuaternionChannels members use a
    different, larger descriptor shape) -- such classes raise
    AntStateFormatError rather than returning a wrong or partial field list.
    """
    name_bytes = class_name.encode("ascii") + b"\0"
    idx = raw.find(name_bytes)
    if idx < 0:
        raise AntStateFormatError(f"{class_name!r} does not appear in this resource.")
    flag_pos = idx - 1
    if flag_pos < 0 or raw[flag_pos] != 0:
        raise AntStateFormatError(
            f"{class_name!r} was found, but the preceding byte is not a record "
            "flag -- this occurrence is a reference, not the type's own definition."
        )

    fields_rev: list[ReflectionField] = []
    cursor = flag_pos - 32
    while cursor >= 0:
        type_ref, size, byte_offset, name_idx, const1, _packed, _c2, _c3 = struct.unpack_from("<8i", raw, cursor)
        if not (1 <= type_ref <= known_type_count and 0 <= size <= 4096 and 0 <= byte_offset <= 65536 and const1 == 1):
            break
        fields_rev.append(ReflectionField(name="", type_index=type_ref, size=size, byte_offset=byte_offset))
        cursor -= 32
    fields = list(reversed(fields_rev))
    if not fields:
        raise AntStateFormatError(
            f"{class_name!r} has no fields in the plain scalar-descriptor shape this "
            "reader supports (it likely has array or nested-reference fields instead)."
        )

    p = idx + len(name_bytes)
    named_fields = []
    for field in fields:
        name, p = _cstr(raw, p, len(raw))
        named_fields.append(ReflectionField(name=name, type_index=field.type_index,
                                             size=field.size, byte_offset=field.byte_offset))
    return ReflectionType(index=-1, name=class_name, kind="composite", size=0, align=0,
                           fields=tuple(named_fields))


@dataclass(frozen=True)
class GdDataBlock:
    offset: int
    declared_size: int
    type_hash: int
    body: bytes


def iter_gd_data_blocks(raw: bytes) -> Iterator[GdDataBlock]:
    """Yield every authored object instance in this AntState resource.

    Each block is tagged with a 32-bit type hash (see the module docstring:
    this hash is not yet resolvable to a class name algorithmically), so
    identifying what an individual block *is* currently means inspecting
    its content -- see find_gd_data_blocks_by_hash.
    """
    cursor = 0
    tag = b"GD.DATA"
    while True:
        offset = raw.find(tag, cursor)
        if offset < 0:
            return
        header_start = offset + len(tag) + 1  # tag + 1 reserved byte
        if header_start + 40 > len(raw):
            raise AntStateFormatError(f"GD.DATA header at {offset:#x} is truncated.")
        length_a, length_b = struct.unpack_from("<2I", raw, header_start)
        type_hash = struct.unpack_from("<I", raw, header_start + 24)[0]
        body_end = offset + len(tag) + 1 + length_a
        if body_end > len(raw):
            # Seen in practice for the final block in a resource: its declared
            # length runs past EOF by exactly the 16-byte trailing pad other
            # records carry. Clip rather than fail on what is very likely just
            # an unwritten trailing pad on the last record.
            body_end = len(raw)
        yield GdDataBlock(offset=offset, declared_size=length_a, type_hash=type_hash, body=raw[offset:body_end])
        cursor = offset + len(tag)


def find_gd_data_blocks_by_hash(raw: bytes) -> dict[int, list[GdDataBlock]]:
    """Group every GD.DATA instance in this resource by its type hash.

    Useful for exploring an unfamiliar AntState file: each group is one
    authored class, even though we don't yet know most classes' names from
    the hash alone. The largest groups/blocks are the best candidates for
    embedded EclipseAnimationAsset curve data.
    """
    groups: dict[int, list[GdDataBlock]] = {}
    for block in iter_gd_data_blocks(raw):
        groups.setdefault(block.type_hash, []).append(block)
    return groups
