"""Read-only Frostbite CAS block decoding and MeshSet header inspection.

Oodle is proprietary, so this project never bundles it.  On Windows we load a
compatible Oodle runtime from the game directory selected by the user.
"""
from __future__ import annotations

import ctypes
import os
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class FrostbiteCasError(RuntimeError):
    pass


@dataclass(frozen=True)
class CasBlock:
    output_size: int
    compression: int
    payload_size: int


@dataclass(frozen=True)
class MeshSetHeader:
    name: str
    mesh_type: int
    lod_count: int
    section_count: int
    bounding_box: tuple[float, float, float, float, float, float]
    chunk_ids: tuple[str, ...]


def inspect_cas_blocks(raw: bytes) -> tuple[CasBlock, ...]:
    """Validate a complete CAS record and return its block table."""
    blocks: list[CasBlock] = []
    cursor = 0
    while cursor < len(raw):
        if len(raw) - cursor < 8:
            raise FrostbiteCasError(f"Truncated CAS block header at byte {cursor:,}.")
        output_size, compression, payload_size = struct.unpack_from(">IHH", raw, cursor)
        cursor += 8
        stored_size = output_size if compression == 0x71 else payload_size
        if compression == 0x70 and output_size != payload_size:
            raise FrostbiteCasError("Invalid uncompressed CAS block size.")
        if compression not in (0x70, 0x71, 0x1170):
            raise FrostbiteCasError(f"Unsupported Frostbite CAS compression 0x{compression:04x}.")
        if stored_size < 0 or cursor + stored_size > len(raw):
            raise FrostbiteCasError(f"CAS block payload at byte {cursor:,} is truncated.")
        blocks.append(CasBlock(output_size, compression, stored_size))
        cursor += stored_size
    return tuple(blocks)


def find_oodle_runtime(game_root: Path) -> Path:
    """Find a 64-bit Oodle runtime already shipped with the selected game."""
    root = game_root.resolve()
    if not root.is_dir():
        raise FrostbiteCasError(f"Game directory does not exist: {root}")
    preferred = (
        "oo2core_9_win64.dll", "oo2core_8_win64.dll", "oo2core_7_win64.dll",
        "oo2core_6_win64.dll", "oo2core_5_win64.dll",
    )
    by_name: dict[str, Path] = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [item for item in dirs if item.lower() not in {"__installer", "support", "redist"}]
        for filename in files:
            lower = filename.lower()
            if lower.startswith("oo2core_") and lower.endswith("_win64.dll"):
                by_name.setdefault(lower, Path(directory, filename))
    for filename in preferred:
        if filename in by_name:
            return by_name[filename]
    if by_name:
        return sorted(by_name.values(), key=lambda item: item.name.lower(), reverse=True)[0]
    raise FrostbiteCasError(
        "No 64-bit Oodle runtime was found in the selected game directory. "
        "The explorer does not download or bundle proprietary game DLLs."
    )


class OodleDecoder:
    """Small, bounded wrapper around the game's OodleLZ_Decompress export."""

    def __init__(self, library_path: Path):
        if os.name != "nt":
            raise FrostbiteCasError("Oodle decoding must run on Windows with the game's own runtime.")
        try:
            library = ctypes.WinDLL(str(library_path))
            function = library.OodleLZ_Decompress
        except (OSError, AttributeError) as error:
            raise FrostbiteCasError(f"Could not load Oodle from {library_path.name}: {error}") from error
        function.restype = ctypes.c_ssize_t
        function.argtypes = [
            ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_ssize_t,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_ssize_t, ctypes.c_int,
        ]
        self.library_path = library_path
        self._library = library
        self._decompress = function

    def __call__(self, payload: bytes, output_size: int) -> bytes:
        source = ctypes.create_string_buffer(payload)
        destination = ctypes.create_string_buffer(output_size)
        written = self._decompress(
            source, len(payload), destination, output_size,
            1, 0, 0, None, 0, None, None, None, 0, 3,
        )
        if written != output_size:
            raise FrostbiteCasError(
                f"Oodle returned {written:,} bytes; the CAS block requires {output_size:,}."
            )
        return destination.raw


def decode_cas_record(
    raw: bytes,
    oodle: Callable[[bytes, int], bytes] | None = None,
    *,
    max_output_bytes: int = 512 * 1024 * 1024,
) -> bytes:
    """Decode one bounded Frostbite CAS record."""
    inspect_cas_blocks(raw)
    result = bytearray()
    cursor = 0
    while cursor < len(raw):
        output_size, compression, payload_size = struct.unpack_from(">IHH", raw, cursor)
        cursor += 8
        stored_size = output_size if compression == 0x71 else payload_size
        payload = raw[cursor:cursor + stored_size]
        cursor += stored_size
        if len(result) + output_size > max_output_bytes:
            raise FrostbiteCasError("Decoded CAS record exceeds the configured safety limit.")
        if compression in (0x70, 0x71):
            decoded = payload
        else:
            if oodle is None:
                raise FrostbiteCasError(
                    "This CAS record uses Oodle compression (0x1170); select its game directory "
                    "so the explorer can use the game's installed Oodle runtime."
                )
            decoded = oodle(payload, output_size)
        if len(decoded) != output_size:
            raise FrostbiteCasError("CAS decoder produced an unexpected block size.")
        result.extend(decoded)
    return bytes(result)


def _read_c_string(raw: bytes, offset: int) -> str:
    if not 0 <= offset < len(raw):
        return ""
    end = raw.find(b"\0", offset, min(len(raw), offset + 1024))
    if end < 0:
        return ""
    return raw[offset:end].decode("utf-8", errors="replace")


def inspect_anthem_meshset(raw: bytes) -> MeshSetHeader:
    """Read the stable Anthem MeshSet header and external geometry chunk IDs.

    This does not guess vertex data: it only follows the offsets and fields used
    by Anthem's Frostbite generation.
    """
    if len(raw) < 148:
        raise FrostbiteCasError("Decoded MeshSet header is too small.")
    bounds = struct.unpack_from("<8f", raw, 0)
    lod_offsets = struct.unpack_from("<6Q", raw, 32)
    full_name_offset, name_offset = struct.unpack_from("<QQ", raw, 88)
    mesh_type = struct.unpack_from("<I", raw, 108)[0]
    lod_count, section_count = struct.unpack_from("<HH", raw, 144)
    if mesh_type not in (0, 1, 2) or not 1 <= lod_count <= 6 or section_count > 4096:
        raise FrostbiteCasError("The decoded record is not a plausible Anthem MeshSet header.")
    chunk_ids: list[str] = []
    for lod_offset in lod_offsets[:lod_count]:
        if lod_offset == 0 or lod_offset + 120 > len(raw):
            raise FrostbiteCasError("MeshSet LOD offset points outside the decoded record.")
        chunk_raw = raw[lod_offset + 100:lod_offset + 116]
        if chunk_raw != b"\0" * 16:
            chunk_ids.append(str(uuid.UUID(bytes_le=chunk_raw)))
    name = _read_c_string(raw, full_name_offset) or _read_c_string(raw, name_offset)
    return MeshSetHeader(
        name=name,
        mesh_type=mesh_type,
        lod_count=lod_count,
        section_count=section_count,
        bounding_box=(bounds[0], bounds[1], bounds[2], bounds[4], bounds[5], bounds[6]),
        chunk_ids=tuple(chunk_ids),
    )


def decode_meshset_slice(source: Path, game_root: Path, destination: Path) -> MeshSetHeader:
    runtime = find_oodle_runtime(game_root)
    decoded = decode_cas_record(source.read_bytes(), OodleDecoder(runtime))
    header = inspect_anthem_meshset(decoded)
    destination.write_bytes(decoded)
    return header
