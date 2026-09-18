"""Validated Frostbite MeshSet geometry decoding.

The reader is format/generation based: it follows Anthem-generation MeshSet
relocation pointers and geometry declarations, then reads the external chunk's
vertex and index buffers.  It does not contain a game asset name list.
"""
from __future__ import annotations

import struct
from dataclasses import asdict, dataclass
from pathlib import Path

from .geometry import MeshData, MeshFormatError


ANTHEM_SECTION_SIZE = 224
MAX_SECTIONS = 4096


@dataclass(frozen=True)
class AnthemSection:
    name: str
    primitive_count: int
    start_index: int
    vertex_offset: int
    vertex_count: int
    primitive_type: int
    bones_per_vertex: int
    bone_palette: tuple[int, ...]
    elements: tuple[tuple[int, int, int, int], ...]
    streams: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class AnthemLod:
    index: int
    sections: tuple[AnthemSection, ...]
    visible_sections: tuple[int, ...]
    index_format: int
    index_buffer_size: int
    vertex_buffer_size: int


@dataclass(frozen=True)
class AnthemSkinSectionDiagnostic:
    """Decoded skin inputs for one renderable MeshSet section.

    Bone indices are section-local palette slots. ``skeleton_bone_ids`` maps
    the referenced slots through the MeshSet palette. Bone names and parent
    relationships still require the matching SkeletonAsset.
    """
    section_index: int
    name: str
    vertex_count: int
    index_format: int
    index_stream: int
    weight_format: int
    weight_stream: int
    values_decoded: bool
    weighted_vertices: int = 0
    zero_weight_vertices: int = 0
    palette_slots: tuple[int, ...] = ()
    palette_size: int = 0
    skeleton_bone_ids: tuple[int, ...] = ()
    weight_sum_min: int | None = None
    weight_sum_max: int | None = None
    normalized_vertices: int = 0
    sample_indices: tuple[int, int, int, int] | None = None
    sample_weights: tuple[int, int, int, int] | None = None
    error: str | None = None


@dataclass(frozen=True)
class AnthemRigDiagnostic:
    """Conservative skinning evidence from declarations and optional bytes."""
    visible_sections: int
    candidate_skin_sections: int
    decoded_skin_sections: int = 0
    weighted_vertices: int = 0
    total_skin_vertices: int = 0
    palette_slots: tuple[int, ...] = ()
    skeleton_bone_ids: tuple[int, ...] = ()
    sections: tuple[AnthemSkinSectionDiagnostic, ...] = ()

    @property
    def likely_skinned(self) -> bool:
        return self.candidate_skin_sections > 0

    @property
    def weights_decoded(self) -> bool:
        return self.decoded_skin_sections > 0 and self.weighted_vertices > 0

    @property
    def summary(self) -> str:
        if self.weights_decoded:
            return (
                f"Skin weights decoded ({self.decoded_skin_sections}/"
                f"{self.candidate_skin_sections} candidate sections; "
                f"{self.weighted_vertices:,}/{self.total_skin_vertices:,} vertices weighted; "
                f"{len(self.palette_slots):,} palette slots mapped to "
                f"{len(self.skeleton_bone_ids):,} skeleton bone IDs). "
                "The matching skeleton can now supply bone names, hierarchy, and bind pose; "
                "animation clips are not decoded yet."
            )
        if self.likely_skinned:
            return (
                f"Likely skinned ({self.candidate_skin_sections}/"
                f"{self.visible_sections} visible sections expose paired skin streams); "
                "skeleton and animation data are not decoded yet."
            )
        return (
            "No verified skinning streams were found in this LOD; this does not prove "
            "the complete asset is unrigged."
        )

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result.update({
            "likely_skinned": self.likely_skinned,
            "weights_decoded": self.weights_decoded,
            "summary": self.summary,
        })
        return result


def _require(raw: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(raw):
        raise MeshFormatError(f"Anthem MeshSet {label} points outside the resource.")


def _c_string(raw: bytes, offset: int) -> str:
    if not 0 <= offset < len(raw):
        return ""
    end = raw.find(b"\0", offset, min(len(raw), offset + 1024))
    if end < 0:
        return ""
    return raw[offset:end].decode("utf-8", errors="replace")


def parse_anthem_lod(raw: bytes, lod_index: int) -> AnthemLod:
    """Parse one Anthem-generation LOD and its renderable section metadata."""
    if not 0 <= lod_index < 6:
        raise MeshFormatError("Anthem LOD index must be between 0 and 5.")
    _require(raw, 32 + lod_index * 8, 8, "LOD table")
    lod_offset = struct.unpack_from("<Q", raw, 32 + lod_index * 8)[0]
    if not lod_offset:
        raise MeshFormatError(f"The MeshSet has no LOD{lod_index} record.")
    _require(raw, lod_offset, 120, f"LOD{lod_index}")

    mesh_type, _max_instances, _unknown, section_count, section_offset = struct.unpack_from(
        "<IIIiQ", raw, lod_offset
    )
    if mesh_type not in (0, 1, 2) or not 0 < section_count <= MAX_SECTIONS:
        raise MeshFormatError(f"LOD{lod_index} has an invalid section table.")
    _require(raw, section_offset, section_count * ANTHEM_SECTION_SIZE, "section table")

    cursor = lod_offset + 24
    categories: list[tuple[int, ...]] = []
    for category_index in range(5):
        count, category_offset = struct.unpack_from("<iQ", raw, cursor)
        cursor += 12
        if count < 0 or count > section_count:
            raise MeshFormatError(f"LOD{lod_index} category {category_index} has an invalid count.")
        _require(raw, category_offset, count, f"category {category_index}")
        values = tuple(raw[category_offset:category_offset + count])
        if any(value >= section_count for value in values):
            raise MeshFormatError(f"LOD{lod_index} category {category_index} references an invalid section.")
        categories.append(values)

    _flags, index_format, index_buffer_size, vertex_buffer_size = struct.unpack_from("<IiII", raw, cursor)
    sections: list[AnthemSection] = []
    for section_index in range(section_count):
        base = section_offset + section_index * ANTHEM_SECTION_SIZE
        name_offset = struct.unpack_from("<Q", raw, base + 8)[0]
        primitive_count, start_index, vertex_offset, vertex_count = struct.unpack_from(
            "<IIII", raw, base + 24
        )
        primitive_type = raw[base + 41]
        bones_per_vertex = raw[base + 42]
        bone_count = struct.unpack_from("<I", raw, base + 44)[0]
        bone_palette_offset = struct.unpack_from("<Q", raw, base + 48)[0]
        if bone_count > 65_536:
            raise MeshFormatError(f"LOD{lod_index} section {section_index} has an invalid bone palette.")
        _require(raw, bone_palette_offset, bone_count * 2, f"section {section_index} bone palette")
        bone_palette = (
            struct.unpack_from(f"<{bone_count}H", raw, bone_palette_offset)
            if bone_count else ()
        )
        element_count, stream_count = struct.unpack_from("<BB", raw, base + 160)
        if element_count > 16 or stream_count > 16:
            raise MeshFormatError(f"LOD{lod_index} section {section_index} has an invalid declaration.")
        elements = tuple(
            struct.unpack_from("<BBBB", raw, base + 64 + element_index * 4)
            for element_index in range(element_count)
        )
        streams = tuple(
            struct.unpack_from("<BB", raw, base + 128 + stream_index * 2)
            for stream_index in range(stream_count)
        )
        sections.append(AnthemSection(
            name=_c_string(raw, name_offset) or f"section_{section_index}",
            primitive_count=primitive_count,
            start_index=start_index,
            vertex_offset=vertex_offset,
            vertex_count=vertex_count,
            primitive_type=primitive_type,
            bones_per_vertex=bones_per_vertex,
            bone_palette=bone_palette,
            elements=elements,
            streams=streams,
        ))

    # Opaque, transparent and transparent-decal categories are renderable.
    # Depth/shadow categories intentionally duplicate the visible geometry.
    visible = tuple(dict.fromkeys(index for category in categories[:3] for index in category))
    if not visible:
        visible = tuple(index for index, section in enumerate(sections) if section.primitive_count)
    return AnthemLod(
        index=lod_index,
        sections=tuple(sections),
        visible_sections=visible,
        index_format=index_format,
        index_buffer_size=index_buffer_size,
        vertex_buffer_size=vertex_buffer_size,
    )


def _position_element(section: AnthemSection) -> tuple[int, int, int, int]:
    try:
        return next(element for element in section.elements if element[0] == 0x01)
    except StopIteration as error:
        raise MeshFormatError(f"{section.name} has no position vertex element.") from error


def _stream_base(section: AnthemSection, stream_index: int) -> int:
    if not 0 <= stream_index < len(section.streams):
        raise MeshFormatError(f"{section.name} references invalid vertex stream {stream_index}.")
    return section.vertex_offset + sum(
        stride * section.vertex_count
        for stride, _classification in section.streams[:stream_index]
    )


def _decode_skin_section(
    chunk_raw: bytes, section_index: int, section: AnthemSection,
    index_element: tuple[int, int, int, int],
    weight_element: tuple[int, int, int, int],
) -> AnthemSkinSectionDiagnostic:
    _index_usage, index_format, index_offset, index_stream = index_element
    _weight_usage, weight_format, weight_offset, weight_stream = weight_element
    try:
        index_stride = section.streams[index_stream][0]
        weight_stride = section.streams[weight_stream][0]
        if index_format == 0x17:  # UShort4
            index_size, index_unpack = 8, "<4H"
        elif index_format in (0x0A, 0x0B, 0x0C, 0x0D):  # Byte4 / UByte4 variants
            index_size, index_unpack = 4, "<4B"
        else:
            raise MeshFormatError(
                f"bone-index format 0x{index_format:02X} is not supported yet"
            )
        if weight_format not in (0x0A, 0x0B, 0x0C, 0x0D):
            raise MeshFormatError(
                f"bone-weight format 0x{weight_format:02X} is not supported yet"
            )
        if index_stride < index_offset + index_size or weight_stride < weight_offset + 4:
            raise MeshFormatError("the declared skin element does not fit its vertex stream")
        index_base = _stream_base(section, index_stream)
        weight_base = _stream_base(section, weight_stream)
        palette_slots: set[int] = set()
        weighted_vertices = normalized_vertices = 0
        sums: list[int] = []
        sample_indices = sample_weights = None
        for vertex in range(section.vertex_count):
            indices_at = index_base + vertex * index_stride + index_offset
            weights_at = weight_base + vertex * weight_stride + weight_offset
            _require(chunk_raw, indices_at, index_size, "skin-index stream")
            _require(chunk_raw, weights_at, 4, "skin-weight stream")
            indices = struct.unpack_from(index_unpack, chunk_raw, indices_at)
            weights = struct.unpack_from("<4B", chunk_raw, weights_at)
            if sample_indices is None:
                sample_indices, sample_weights = indices, weights
            total = sum(weights)
            sums.append(total)
            if total:
                weighted_vertices += 1
                if 254 <= total <= 256:
                    normalized_vertices += 1
                palette_slots.update(
                    bone_index for bone_index, weight in zip(indices, weights) if weight
                )
        invalid_slots = sorted(slot for slot in palette_slots if slot >= len(section.bone_palette))
        if invalid_slots:
            raise MeshFormatError(
                f"skin indices reference {len(invalid_slots)} slot(s) outside the "
                f"{len(section.bone_palette)}-entry section palette"
            )
        skeleton_bone_ids = tuple(sorted({section.bone_palette[slot] for slot in palette_slots}))
        return AnthemSkinSectionDiagnostic(
            section_index=section_index,
            name=section.name,
            vertex_count=section.vertex_count,
            index_format=index_format,
            index_stream=index_stream,
            weight_format=weight_format,
            weight_stream=weight_stream,
            values_decoded=True,
            weighted_vertices=weighted_vertices,
            zero_weight_vertices=section.vertex_count - weighted_vertices,
            palette_slots=tuple(sorted(palette_slots)),
            palette_size=len(section.bone_palette),
            skeleton_bone_ids=skeleton_bone_ids,
            weight_sum_min=min(sums) if sums else None,
            weight_sum_max=max(sums) if sums else None,
            normalized_vertices=normalized_vertices,
            sample_indices=sample_indices,
            sample_weights=sample_weights,
        )
    except (IndexError, MeshFormatError, struct.error) as error:
        return AnthemSkinSectionDiagnostic(
            section_index=section_index,
            name=section.name,
            vertex_count=section.vertex_count,
            index_format=index_format,
            index_stream=index_stream,
            weight_format=weight_format,
            weight_stream=weight_stream,
            values_decoded=False,
            palette_size=len(section.bone_palette),
            error=str(error),
        )


def _decode_skin_vertices(
    chunk_raw: bytes, section: AnthemSection,
) -> tuple[list[tuple[int, int, int, int]], list[tuple[float, float, float, float]]] | tuple[None, None]:
    """Decode animation-ready per-vertex bone IDs and normalized weights."""
    by_usage = {element[0]: element for element in section.elements}
    if 0x02 not in by_usage or 0x04 not in by_usage:
        return None, None
    _index_usage, index_format, index_offset, index_stream = by_usage[0x02]
    _weight_usage, weight_format, weight_offset, weight_stream = by_usage[0x04]
    if index_format == 0x17:
        index_size, index_unpack = 8, "<4H"
    elif index_format in (0x0A, 0x0B, 0x0C, 0x0D):
        index_size, index_unpack = 4, "<4B"
    else:
        return None, None
    if weight_format not in (0x0A, 0x0B, 0x0C, 0x0D):
        return None, None
    index_stride = section.streams[index_stream][0]
    weight_stride = section.streams[weight_stream][0]
    if index_stride < index_offset + index_size or weight_stride < weight_offset + 4:
        return None, None
    index_base = _stream_base(section, index_stream)
    weight_base = _stream_base(section, weight_stream)
    bones: list[tuple[int, int, int, int]] = []
    weights_out: list[tuple[float, float, float, float]] = []
    for vertex in range(section.vertex_count):
        indices_at = index_base + vertex * index_stride + index_offset
        weights_at = weight_base + vertex * weight_stride + weight_offset
        _require(chunk_raw, indices_at, index_size, "skin-index stream")
        _require(chunk_raw, weights_at, 4, "skin-weight stream")
        slots = struct.unpack_from(index_unpack, chunk_raw, indices_at)
        raw_weights = struct.unpack_from("<4B", chunk_raw, weights_at)
        if any(weight and slot >= len(section.bone_palette) for slot, weight in zip(slots, raw_weights)):
            raise MeshFormatError(f"{section.name} contains a skin slot outside its bone palette.")
        bones.append(tuple(
            section.bone_palette[slot] if weight and slot < len(section.bone_palette) else 0
            for slot, weight in zip(slots, raw_weights)
        ))
        total = sum(raw_weights)
        divisor = float(total or 1)
        weights_out.append(tuple(weight / divisor for weight in raw_weights))
    return bones, weights_out


def inspect_anthem_rig(
    meshset_raw: bytes, lod_index: int, chunk_raw: bytes | None = None,
) -> AnthemRigDiagnostic:
    """Report rigging evidence without inventing a skeleton hierarchy.

    Frostbite declares primary BoneIndices/BoneWeights as usages 0x02/0x04.
    Their presence proves that the geometry carries skinning inputs, but the
    external SkeletonAsset and animation clips still have to be decoded and
    linked before playback is possible. TexCoord0/1 are
    usages 0x21/0x22 and must never be mistaken for rigging evidence.
    """
    lod = parse_anthem_lod(meshset_raw, lod_index)
    visible = [
        lod.sections[index] for index in lod.visible_sections
        if lod.sections[index].primitive_count and lod.sections[index].vertex_count
    ]
    diagnostics: list[AnthemSkinSectionDiagnostic] = []
    for section_index in lod.visible_sections:
        section = lod.sections[section_index]
        if not section.primitive_count or not section.vertex_count:
            continue
        by_usage = {element[0]: element for element in section.elements}
        if 0x02 not in by_usage or 0x04 not in by_usage:
            continue
        index_element, weight_element = by_usage[0x02], by_usage[0x04]
        if chunk_raw is None:
            diagnostics.append(AnthemSkinSectionDiagnostic(
                section_index=section_index,
                name=section.name,
                vertex_count=section.vertex_count,
                index_format=index_element[1],
                index_stream=index_element[3],
                weight_format=weight_element[1],
                weight_stream=weight_element[3],
                values_decoded=False,
                palette_size=len(section.bone_palette),
            ))
        else:
            diagnostics.append(_decode_skin_section(
                chunk_raw, section_index, section, index_element, weight_element,
            ))
    decoded = [item for item in diagnostics if item.values_decoded]
    palette_slots = tuple(sorted({
        slot for item in decoded for slot in item.palette_slots
    }))
    skeleton_bone_ids = tuple(sorted({
        bone_id for item in decoded for bone_id in item.skeleton_bone_ids
    }))
    return AnthemRigDiagnostic(
        visible_sections=len(visible),
        candidate_skin_sections=len(diagnostics),
        decoded_skin_sections=len(decoded),
        weighted_vertices=sum(item.weighted_vertices for item in decoded),
        total_skin_vertices=sum(item.vertex_count for item in decoded),
        palette_slots=palette_slots,
        skeleton_bone_ids=skeleton_bone_ids,
        sections=tuple(diagnostics),
    )


def _position(raw: bytes, offset: int, vertex_format: int) -> tuple[float, float, float]:
    if vertex_format in (0x03, 0x04):  # Float3 / Float4
        _require(raw, offset, 12, "position stream")
        return struct.unpack_from("<3f", raw, offset)
    if vertex_format in (0x07, 0x08):  # Half3 / Half4
        _require(raw, offset, 6, "position stream")
        return struct.unpack_from("<3e", raw, offset)
    raise MeshFormatError(f"Position vertex format 0x{vertex_format:02X} is not supported yet.")


def decode_anthem_geometry(meshset_raw: bytes, chunk_raw: bytes, lod_index: int) -> list[MeshData]:
    """Decode visible triangle sections from one external MeshSet chunk."""
    lod = parse_anthem_lod(meshset_raw, lod_index)
    required_size = lod.vertex_buffer_size + lod.index_buffer_size
    if len(chunk_raw) < required_size:
        raise MeshFormatError(
            f"LOD{lod_index} chunk is truncated ({len(chunk_raw):,} of {required_size:,} bytes)."
        )
    # Anthem's RenderFormat_R16_UINT value is 0x21. Keep unsupported formats
    # explicit rather than accidentally treating 32-bit indices as 16-bit.
    if lod.index_format != 0x21:
        raise MeshFormatError(
            f"LOD{lod_index} index format 0x{lod.index_format & 0xFFFFFFFF:08X} is not supported yet."
        )

    meshes: list[MeshData] = []
    for section_index in lod.visible_sections:
        section = lod.sections[section_index]
        if not section.primitive_count or not section.vertex_count:
            continue
        if section.primitive_type != 3:
            raise MeshFormatError(
                f"{section.name} uses primitive type {section.primitive_type}; only triangle lists are supported."
            )
        _usage, vertex_format, element_offset, stream_index = _position_element(section)
        if stream_index >= len(section.streams):
            raise MeshFormatError(f"{section.name} position stream index is invalid.")
        stream_base = _stream_base(section, stream_index)
        stride = section.streams[stream_index][0]
        if not stride:
            raise MeshFormatError(f"{section.name} position stream has a zero stride.")
        vertices = [
            _position(chunk_raw, stream_base + vertex * stride + element_offset, vertex_format)
            for vertex in range(section.vertex_count)
        ]

        index_count = section.primitive_count * 3
        index_offset = lod.vertex_buffer_size + section.start_index * 2
        _require(chunk_raw, index_offset, index_count * 2, "index buffer")
        indices = struct.unpack_from(f"<{index_count}H", chunk_raw, index_offset)
        if indices and max(indices) >= section.vertex_count:
            raise MeshFormatError(
                f"{section.name} contains an index outside its {section.vertex_count:,}-vertex range."
            )
        faces = [tuple(indices[index:index + 3]) for index in range(0, index_count, 3)]
        skin_bones, skin_weights = _decode_skin_vertices(chunk_raw, section)
        meshes.append(MeshData(
            section.name, vertices, faces, skin_bones, skin_weights,
        ))
    if not meshes:
        raise MeshFormatError(f"LOD{lod_index} contains no visible triangle sections.")
    return meshes


def find_anthem_chunk(meshset_path: Path, meshset_raw: bytes, lod_index: int) -> Path:
    """Resolve a loose decoded chunk next to its MeshSet without asset-name tables."""
    lod = parse_anthem_lod(meshset_raw, lod_index)
    exact_names = (
        f"{meshset_path.stem}_lod{lod_index}.chunk",
        f"{meshset_path.stem}.lod{lod_index}.chunk",
        f"{meshset_path.stem}.chunk",
    )
    for name in exact_names:
        candidate = meshset_path.with_name(name)
        if candidate.is_file():
            return candidate
    expected_size = lod.vertex_buffer_size + lod.index_buffer_size
    sized = [path for path in meshset_path.parent.glob("*.chunk") if path.stat().st_size == expected_size]
    if len(sized) == 1:
        return sized[0]
    raise MeshFormatError(
        f"Could not find the decoded LOD{lod_index} chunk beside {meshset_path.name}. "
        f"Expected {meshset_path.stem}_lod{lod_index}.chunk ({expected_size:,} bytes)."
    )


def load_anthem_meshset(path: Path) -> tuple[list[MeshData], int, Path]:
    """Load the best locally available LOD for a loose decoded MeshSet."""
    raw = path.read_bytes()
    errors: list[str] = []
    for lod_index in range(6):
        try:
            chunk_path = find_anthem_chunk(path, raw, lod_index)
            return decode_anthem_geometry(raw, chunk_path.read_bytes(), lod_index), lod_index, chunk_path
        except MeshFormatError as error:
            errors.append(str(error))
    detail = errors[-1] if errors else "No LOD records were found."
    raise MeshFormatError(f"No decoded geometry chunk could be paired with {path.name}. {detail}")
