"""Read-only Rockstar RPF2/RPF3 and IMG3 discovery and extraction.

RPF3 stores names as hashes, so resource flags and binary structure are the
source of truth. Filenames are treated only as optional hints.
"""
from __future__ import annotations

import hashlib
import struct
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import AssetRecord, CharacterBundle

CHARACTER_ARCHIVES = frozenset({"componentpeds.img", "playerped.rpf", "playerped.img"})
IMG3_MAGIC = 0xA94E2A52
KEY_SHA1 = "DEA375EF1E6EF2223A1221C2C575C47BF17EFA5E"
KEY_OFFSETS = (
    0xA94204, 0xB607C4, 0xB56BC4, 0xB75C9C, 0xB7AEF4, 0xBE1370,
    0xBE6540, 0xBE7540, 0xC95FD8, 0xC5B33C, 0xC5B73C,
    0xB5B65C, 0xB569F4, 0xB76CB4, 0xB7AEFC,
    0xB8813C, 0xB8C38C, 0xBE6510,
)


@dataclass(frozen=True)
class RpfEntry:
    index: int
    name_hash: int
    name: str
    file_offset: int
    file_size: int
    resource_flags: int
    resource_type: int
    is_resource: bool


def _gtaiv_executable(root: Path) -> Path | None:
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower() == "gtaiv.exe":
            return path
    return None


@lru_cache(maxsize=4)
def _aes_key(executable: Path) -> bytes:
    data = executable.read_bytes()

    def candidate_at(offset: int) -> bytes | None:
        candidate = data[offset:offset + 32]
        if len(candidate) == 32 and hashlib.sha1(candidate).hexdigest().upper() == KEY_SHA1:
            return candidate
        return None

    for offset in KEY_OFFSETS:
        candidate = candidate_at(offset)
        if candidate is not None:
            return candidate
    # Complete Edition and regional builds occasionally move the embedded key.
    # SparkIV's original reader falls back to a 32-byte aligned scan as well.
    for offset in range(0, max(0, len(data) - 31), 32):
        candidate = candidate_at(offset)
        if candidate is not None:
            return candidate
    raise ValueError(
        "This GTAIV.exe build uses an AES-key location the RPF reader does not know yet. "
        "The game files were not modified."
    )


def _decrypt_toc(data: bytes, key: bytes) -> bytes:
    """Decrypt the complete AES blocks, preserving an IMG3 trailing fragment."""
    from Crypto.Cipher import AES
    aligned = len(data) & ~0x0F
    decrypted = data[:aligned]
    for _ in range(16):
        decrypted = AES.new(key, AES.MODE_ECB).decrypt(decrypted)
    return decrypted + data[aligned:]


def _read_cstring(data: bytes, offset: int) -> str | None:
    if not 0 <= offset < len(data):
        return None
    end = data.find(b"\0", offset)
    if end < 0:
        return None
    return data[offset:end].decode("utf-8", errors="replace") or None


def parse_rpf_entries(archive: Path, executable: Path | None) -> list[RpfEntry]:
    """Parse an RPF2/RPF3 table without relying on resolved extensions."""
    archive_size = archive.stat().st_size
    with archive.open("rb") as stream:
        header = stream.read(20)
        if len(header) != 20 or header[:4] not in {b"RPF2", b"RPF3"}:
            raise ValueError(f"{archive.name} is not an RPF2/RPF3 archive.")
        version = header[:4]
        toc_size, entry_count, _unknown, encryption_tag = struct.unpack_from("<IIII", header, 4)
        if entry_count > 5_000_000 or toc_size < entry_count * 16 or toc_size > archive_size:
            raise ValueError("The RPF table header contains unreasonable sizes.")
        stream.seek(0x800)
        toc = stream.read(toc_size)
    if len(toc) != toc_size:
        raise ValueError("The RPF table is truncated.")
    if encryption_tag:
        if executable is None:
            raise FileNotFoundError("The archive table is encrypted and GTAIV.exe was not found in the selected folder.")
        toc = _decrypt_toc(toc, _aes_key(executable))

    entries_data = toc[:entry_count * 16]
    names_data = toc[entry_count * 16:]
    entries: list[RpfEntry] = []
    for index in range(entry_count):
        dword0, dword4, dword8, dwordc = struct.unpack_from("<IIII", entries_data, index * 16)
        if dword8 & 0x80000000:
            continue
        is_resource = bool(dwordc & 0x80000000)
        resource_type = dword8 & 0xFF if is_resource else 0
        file_offset = dword8 & (0x7FFFFF00 if is_resource else 0x7FFFFFFF)
        resource_flags = dwordc & 0x3FFFFFFF if is_resource else 0
        disk_size = dword4 if is_resource else ((dwordc & 0x00FFFFFF) if dwordc & 0x40000000 else dword4)
        name = f"{dword0:08X}" if version == b"RPF3" else (_read_cstring(names_data, dword0) or f"file_{index}")
        if file_offset > archive_size or disk_size > archive_size - file_offset:
            continue
        entries.append(RpfEntry(
            index, dword0, name, file_offset, disk_size,
            resource_flags, resource_type, is_resource,
        ))
    return entries


def parse_img3_entries(archive: Path, key: bytes | None = None) -> list[RpfEntry]:
    """Parse the flat RAGE IMG3 container used by GTA IV component archives."""
    archive_size = archive.stat().st_size
    with archive.open("rb") as stream:
        header = stream.read(20)
        if key is not None:
            header = _decrypt_toc(header, key)
        if len(header) != 20 or struct.unpack_from("<I", header)[0] != IMG3_MAGIC:
            raise ValueError(f"{archive.name} is not an IMG3 archive.")
        _magic, _version, entry_count, header_size, entry_size = struct.unpack_from("<IIIIH", header)
        entry_size = entry_size or 16
        if entry_count > 5_000_000 or entry_size < 16:
            raise ValueError("The IMG3 table header contains unreasonable sizes.")
        entries_size = entry_count * entry_size
        if header_size < entries_size or 20 + header_size > archive_size:
            raise ValueError("The IMG3 table is truncated or has invalid sizes.")
        table = stream.read(header_size)
        if key is not None:
            table = _decrypt_toc(table, key)
    if len(table) != header_size:
        raise ValueError("The IMG3 table is truncated.")

    entries_data = table[:entries_size]
    names_data = table[entries_size:]
    name_offset = 0
    entries: list[RpfEntry] = []
    for index in range(entry_count):
        offset = index * entry_size
        dword0, dword4, sector_offset, sector_count, flags = struct.unpack_from(
            "<IIIHH", entries_data, offset
        )
        end = names_data.find(b"\0", name_offset)
        if end < 0:
            raise ValueError(f"IMG3 filename table ends at entry {index:,}.")
        name = names_data[name_offset:end].decode("utf-8", errors="replace") or f"file_{index}"
        name_offset = end + 1

        # IMG3 stores a 2048-byte sector count and the unused byte count in
        # the low 11 bits of flags. Bit 13 marks a RAGE resource record.
        file_offset = sector_offset << 11
        padding = flags & 0x7FF
        file_size = (sector_count << 11) - padding
        # Retail IMG3 files use the high bits of RSCFlags; some tools also set
        # the explicit 0x2000 marker in the entry flags. Accept both variants.
        is_resource = bool((dword0 & 0xC0000000) or (flags & 0x2000))
        if file_size <= 0 or file_offset > archive_size or file_size > archive_size - file_offset:
            continue
        entries.append(RpfEntry(
            index=index,
            name_hash=0,
            name=name,
            file_offset=file_offset,
            file_size=file_size,
            resource_flags=dword0 if is_resource else 0,
            resource_type=dword4 if is_resource else 0,
            is_resource=is_resource,
        ))
    return entries


def parse_archive_entries(archive: Path, executable: Path | None) -> tuple[str, list[RpfEntry]]:
    """Detect an archive from its signature and route it to a format reader."""
    with archive.open("rb") as stream:
        signature = stream.read(4)
    if len(signature) == 4 and struct.unpack("<I", signature)[0] == IMG3_MAGIC:
        return "IMG3", parse_img3_entries(archive)
    if signature in {b"RPF2", b"RPF3"}:
        return signature.decode("ascii"), parse_rpf_entries(archive, executable)
    if executable is not None:
        key = _aes_key(executable)
        with archive.open("rb") as stream:
            encrypted_header = stream.read(20)
        header = _decrypt_toc(encrypted_header, key)
        if len(header) == 20 and struct.unpack_from("<I", header)[0] == IMG3_MAGIC:
            return "IMG3-AES", parse_img3_entries(archive, key)
    shown = signature.hex(" ").upper() or "empty file"
    raise ValueError(f"unsupported archive signature ({shown}); expected IMG3, encrypted IMG3, RPF2, or RPF3.")


def inspect_gtaiv_archives(root: Path, assets: list[AssetRecord]) -> tuple[list[AssetRecord], list[str]]:
    # This reader is capability-based: any GTA IV IMG/RPF passed by the UI is
    # inspected, including props, weapons, and map archives.
    archives = [asset for asset in assets if asset.kind == "container"]
    if not archives:
        return [], []
    executable = _gtaiv_executable(root)
    discovered: list[AssetRecord] = []
    warnings: list[str] = []
    for archive in archives:
        try:
            archive_format, entries = parse_archive_entries(archive.path, executable)
            supported_names = {".wdr", ".wdd", ".wft", ".wtd"}
            resources = [
                entry for entry in entries
                if entry.file_size >= 12 and (
                    entry.is_resource
                    # Some RPF2/RPF3 map archives retain named WDR/WDD records
                    # without the resource bit used by character archives.
                    or PurePosixPath(entry.name).suffix.lower() in supported_names
                )
            ]
            for entry in resources:
                suffix = PurePosixPath(entry.name).suffix.lower()
                if suffix in {".wdr", ".wdd", ".wft"}:
                    kind = "mesh"
                    extension = suffix
                elif suffix == ".wtd":
                    kind = "texture"
                    extension = suffix
                else:
                    kind = "mesh"
                    extension = ".rsc"
                internal = entry.name if suffix else f"{entry.name}.rsc"
                discovered.append(AssetRecord(
                    path=archive.path,
                    relative_path=f"{archive.relative_path}::{internal}",
                    kind=kind,
                    extension=extension,
                    size=entry.file_size,
                    engine="Rockstar RAGE (GTA IV)",
                    directly_viewable=True,
                    container_path=archive.path,
                    internal_path=internal,
                    metadata={
                        "rpf_index": entry.index,
                        "name_hash": entry.name_hash,
                        "file_offset": entry.file_offset,
                        "resource_flags": entry.resource_flags,
                        "resource_type": entry.resource_type,
                        "archive_format": archive_format,
                    },
                ))
            if not resources:
                warnings.append(f"{archive.relative_path}: its {archive_format} table contains no resource records.")
        except Exception as error:
            warnings.append(f"Could not inspect {archive.relative_path} read-only: {error}")
    return discovered, warnings


def inspect_gtaiv_archive(root: Path, archive: AssetRecord) -> tuple[list[AssetRecord], list[str]]:
    if archive.kind != "container":
        raise ValueError("The selected asset is not an archive.")
    return inspect_gtaiv_archives(root, [archive])


def known_archive_bundles(assets: list[AssetRecord], existing: list[CharacterBundle]) -> list[CharacterBundle]:
    represented = {bundle.mesh.path for bundle in existing if bundle.mesh.kind == "container"}
    result: list[CharacterBundle] = []
    for asset in assets:
        if asset.kind != "container" or asset.path.name.lower() not in CHARACTER_ARCHIVES or asset.path in represented:
            continue
        result.append(CharacterBundle(
            name=asset.path.stem,
            mesh=asset,
            score=42.0 if asset.path.name.lower() == "componentpeds.img" else 36.0,
            reasons=["known GTA IV pedestrian/character archive", "Preview resolves the archive reader automatically"],
        ))
    return result


def read_internal_asset(asset: AssetRecord) -> bytes:
    if not asset.container_path or not asset.internal_path:
        raise ValueError("This asset is not an internal archive record.")
    offset = asset.metadata.get("file_offset")
    if not isinstance(offset, int):
        raise ValueError("The archive record has no validated file offset.")
    with asset.container_path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(asset.size)
    if len(data) != asset.size:
        raise ValueError(f"Resource {asset.internal_path} ends outside {asset.container_path.name}.")
    return data


def extract_bundle(bundle: CharacterBundle, root: Path, destination: Path) -> list[Path]:
    """Copy selected internal resources without changing the game archive."""
    del root
    internals = [asset for asset in bundle.all_assets if asset.internal_path and asset.container_path]
    if not internals:
        raise ValueError("Preview the archive first so the program can discover its internal resources.")
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    for asset in internals:
        output = destination / PurePosixPath(asset.internal_path).name
        output.write_bytes(read_internal_asset(asset))
        extracted.append(output)
    return extracted
