"""Bounded Frostbite TOC/SB/CAS indexing for Anthem-generation archives.

This module follows the container tables themselves.  It does not identify a
game by filename and it does not contain an asset-name allowlist.  The resource
type id selects MeshSet records; layout.toc maps CAS ids to package paths.
"""
from __future__ import annotations

import hashlib
import io
import itertools
import sqlite3
import struct
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .frostbite_cas import (
    FrostbiteCasError,
    OodleDecoder,
    decode_cas_record,
    find_oodle_runtime,
    inspect_anthem_meshset,
)
from .frostbite_mesh import decode_anthem_geometry, inspect_anthem_rig
from .frostbite_ebx import parse_anthem_ebx_header
from .geometry import MeshData, MeshFormatError
from .models import AssetRecord


TOC_MAGIC = 0x00D1CE01
INDEX_MAGIC = 0x30
BUNDLE_MAGIC = 0x20
BUNDLE_META_MAGIC = 0x9D798ED6
MESHSET_RESOURCE_TYPE = 0x49B156D4
MAX_BUNDLES_PER_TOC = 100_000
MAX_FILES_PER_BUNDLE = 250_000
MAX_MESHSETS = 75_000
MAX_ANIMATION_RECORDS = 50_000
MAX_STRING = 16_384
MAX_CAS_RECORD = 64 * 1024 * 1024


class FrostbiteIndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class CasLocation:
    cas_id: int
    cas_path: Path
    offset: int
    packed_size: int
    original_size: int
    sha1: bytes


@dataclass(frozen=True)
class BundleFile:
    kind: str
    name: str
    resource_type: int
    uid: bytes
    location: CasLocation


@dataclass(frozen=True)
class BundleInfo:
    name: str
    sb_path: Path
    offset: int
    files: tuple[BundleFile, ...]


@dataclass(frozen=True)
class LayoutMap:
    game_root: Path
    data_root: Path
    patch_root: Path | None
    packages: dict[tuple[bool, int], Path]
    cas_files: dict[int, Path]

    def cas_path(self, cas_id: int) -> Path | None:
        return self.cas_files.get(cas_id)


class _Entry:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get(self, key: str, default=None):
        return self.values.get(key, default)


def frostbite_rig_record_kind(item: BundleFile) -> str | None:
    """Classify rig-related EBX/RES records, prioritizing skeletons over their path."""
    if item.kind not in {"ebx", "res"}:
        return None
    if item.kind == "res" and item.resource_type == MESHSET_RESOURCE_TYPE:
        return None
    name = item.name.replace("\\", "/").strip("/").casefold()
    base = name.rsplit("/", 1)[-1]
    if any(token in base for token in ("_model_mesh", "_mesh", "meshset")):
        return None
    if any(token in name for token in (
        "skeleton", "bonehierarchy", "/rig/", "_rig/", "_rig.", "_rig_",
    )):
        return "skeleton"
    # Checked before the "animation" folder-path check below: confirmed on
    # real Anthem data that a texture reference can sit inside an
    # "animations/" folder tree (e.g. a character's head texture bundled
    # alongside its facial animations, such as
    # ".../head/textures/hmf_x_pcatexture_diffhf_..._antstate.ebx"). Without
    # this, that file's own specific texture naming loses to the broader
    # "animations/" folder segment matched below. Real Anthem naming
    # abbreviates "diffuse" (diffhf, diffflf) rather than spelling it out.
    if any(token in name for token in (
        "/texture", "textures/", "pcatexture", "_diffuse", "_normal", "_albedo",
    )) or base.endswith((".dds", ".png", ".tga", ".texture")):
        return "texture"
    if (
        name.startswith("animation/")
        or "/animation/" in name
        or name.startswith("animations/")
        or "/animations/" in name
        or "/anim/" in name
        or "animationclip" in name
        or base.endswith("_anim")
    ):
        return "animation"
    return None


def is_animation_record(item: BundleFile) -> bool:
    """Compatibility predicate used by callers interested in clip candidates."""
    return frostbite_rig_record_kind(item) == "animation"


def _exact(stream: BinaryIO, size: int) -> bytes:
    raw = stream.read(size)
    if len(raw) != size:
        raise FrostbiteIndexError("Frostbite metadata ended unexpectedly.")
    return raw


def _u32(stream: BinaryIO) -> int:
    return struct.unpack(">I", _exact(stream, 4))[0]


def _leb(stream: BinaryIO) -> int:
    result = 0
    for shift in range(0, 64, 7):
        value = _exact(stream, 1)[0]
        result |= (value & 0x7F) << shift
        if not value & 0x80:
            return result
    raise FrostbiteIndexError("Frostbite LEB128 value is too large.")


def _cstring(stream: BinaryIO, *, limit: int = MAX_STRING) -> str:
    raw = bytearray()
    for _ in range(limit):
        value = _exact(stream, 1)
        if value == b"\0":
            return raw.decode("utf-8", errors="replace")
        raw.extend(value)
    raise FrostbiteIndexError("Frostbite metadata string exceeds the safety limit.")


def _read_entry(stream: BinaryIO, depth: int = 0) -> _Entry:
    if depth > 20:
        raise FrostbiteIndexError("Frostbite layout nesting exceeds the safety limit.")
    item = _exact(stream, 1)[0]
    result = _Entry()
    if item in (0x82, 0x02):
        if item == 0x02:
            _cstring(stream)
        size = _leb(stream)
        start = stream.tell()
        if size > 128 * 1024 * 1024:
            raise FrostbiteIndexError("Frostbite layout entry exceeds the safety limit.")
        while stream.tell() - start < size:
            _read_field(stream, result, depth + 1)
        if stream.tell() - start != size:
            raise FrostbiteIndexError("Frostbite layout entry has an invalid size.")
    elif item == 0x87:
        size = _leb(stream)
        if not 1 <= size <= 128 * 1024 * 1024:
            raise FrostbiteIndexError("Frostbite byte entry has an invalid size.")
        result.values["data"] = _exact(stream, size - 1)
        if _exact(stream, 1) != b"\0":
            raise FrostbiteIndexError("Frostbite byte entry has no terminator.")
    elif item == 0x8F:
        result.values["data"] = _exact(stream, 16)
    else:
        raise FrostbiteIndexError(f"Unsupported Frostbite layout item 0x{item:02X}.")
    return result


def _read_field(stream: BinaryIO, target: _Entry, depth: int) -> None:
    offset = stream.tell()
    field_type = _exact(stream, 1)[0]
    if field_type == 0:
        return
    key = _cstring(stream)
    if field_type == 0x0F:
        value: object = _exact(stream, 16)
    elif field_type == 0x09:
        value = struct.unpack("<Q", _exact(stream, 8))[0]
    elif field_type == 0x08:
        value = struct.unpack("<I", _exact(stream, 4))[0]
    elif field_type == 0x06:
        value = _exact(stream, 1) == b"\x01"
    elif field_type == 0x02:
        stream.seek(offset)
        value = _read_entry(stream, depth)
    elif field_type == 0x13:
        value = _exact(stream, _leb(stream))
    elif field_type == 0x10:
        value = _exact(stream, 20)
    elif field_type == 0x07:
        size = _leb(stream)
        value = _exact(stream, size - 1).decode("utf-8", errors="replace")
        if _exact(stream, 1) != b"\0":
            raise FrostbiteIndexError("Frostbite string has no terminator.")
    elif field_type == 0x0C:
        value = struct.unpack(">Q", _exact(stream, 8))[0]
    elif field_type == 0x01:
        size = _leb(stream)
        start = stream.tell()
        value = []
        while stream.tell() - start < size - 1:
            if len(value) >= 1_000_000:
                raise FrostbiteIndexError("Frostbite list exceeds the safety limit.")
            value.append(_read_entry(stream, depth))
        if _exact(stream, 1) != b"\0":
            raise FrostbiteIndexError("Frostbite list has no terminator.")
    else:
        raise FrostbiteIndexError(f"Unsupported Frostbite field type 0x{field_type:02X}.")
    target.values[key] = value


def _toc_payload(path: Path) -> io.BytesIO:
    with path.open("rb") as stream:
        if _u32(stream) != TOC_MAGIC:
            raise FrostbiteIndexError(f"{path.name} is not a supported Frostbite TOC.")
        stream.seek(0x22C)
        return io.BytesIO(stream.read())


def _find_layout_roots(root: Path) -> tuple[Path, Path | None]:
    candidates: list[Path] = []
    direct = (root / "Data", root / "Anthem" / "Data", root)
    for candidate in direct:
        if (candidate / "layout.toc").is_file() and candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        for path in root.glob("*/Data/layout.toc"):
            candidates.append(path.parent)
    if not candidates:
        raise FrostbiteIndexError("No Frostbite Data/layout.toc was found below the selected folder.")
    data_root = candidates[0]
    patch = data_root.parent / "Patch"
    return data_root, patch if (patch / "layout.toc").is_file() else None


def _layout_packages(layout_root: Path, *, patch: bool) -> dict[tuple[bool, int], Path]:
    source = _read_entry(_toc_payload(layout_root / "layout.toc"))
    manifest = source.get("installManifest")
    chunks = manifest.get("installChunks", []) if isinstance(manifest, _Entry) else []
    result: dict[tuple[bool, int], Path] = {}
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, _Entry):
            continue
        bundle = chunk.get("installBundle")
        if isinstance(bundle, str) and bundle:
            result[(patch, index)] = layout_root.joinpath(*bundle.strip("/").split("/"))
    return result


def load_layout_map(root: Path) -> LayoutMap:
    data_root, patch_root = _find_layout_roots(root.resolve())
    packages = _layout_packages(data_root, patch=False)
    if patch_root:
        packages.update(_layout_packages(patch_root, patch=True))
    if not packages:
        raise FrostbiteIndexError("Frostbite layout contains no install packages.")
    cas_files: dict[int, Path] = {}
    for (is_patch, package_index), package in packages.items():
        if not package.is_dir():
            continue
        for path in package.glob("cas_*.cas"):
            try:
                cas_index = int(path.stem[-2:])
            except ValueError:
                continue
            if cas_index:
                cas_id = (int(is_patch) << 16) | (package_index << 8) | cas_index
                cas_files[cas_id] = path
    return LayoutMap(root.resolve(), data_root, patch_root, packages, cas_files)


def _string_at(stream: BinaryIO, offset: int) -> str:
    current = stream.tell()
    try:
        stream.seek(offset)
        return _cstring(stream)
    finally:
        stream.seek(current)


_CAS_STREAMS: dict[Path, BinaryIO] = {}


def _is_cas_record(path: Path, offset: int) -> bool:
    if offset < 0:
        return False
    try:
        stream = _CAS_STREAMS.get(path)
        if stream is None:
            stream = path.open("rb")
            _CAS_STREAMS[path] = stream
        stream.seek(offset + 4)
        raw = stream.read(2)
        return len(raw) == 2 and struct.unpack(">H", raw)[0] in (0x70, 0x71, 0x1170)
    except OSError:
        return False


def parse_bundle(layout: LayoutMap, sb_path: Path, offset: int, name: str) -> BundleInfo:
    with sb_path.open("rb") as stream:
        stream.seek(offset)
        if _u32(stream) != BUNDLE_MAGIC:
            raise FrostbiteIndexError(f"Invalid bundle marker in {sb_path.name} at 0x{offset:X}.")
        _exact(stream, 4)
        bundle_length = _u32(stream)
        _exact(stream, 20)
        meta_size = _u32(stream)
        meta_offset = stream.tell()
        header = struct.unpack(">8I", _exact(stream, 32))
        magic, total, ebx_count, res_count, chunk_count, string_offset, _, _ = header
        if magic != BUNDLE_META_MAGIC:
            raise FrostbiteIndexError("Invalid Frostbite bundle metadata marker.")
        if total != ebx_count + res_count + chunk_count or total > MAX_FILES_PER_BUNDLE:
            raise FrostbiteIndexError("Frostbite bundle file count exceeds the safety limit.")
        if not 0 <= meta_size <= bundle_length <= sb_path.stat().st_size - offset:
            raise FrostbiteIndexError("Frostbite bundle declares an invalid size.")
        hashes = [_exact(stream, 20) for _ in range(total)]
        descriptors: list[dict[str, object]] = []
        strings_base = meta_offset + string_offset
        for index in range(ebx_count):
            name_offset, original_size = struct.unpack(">II", _exact(stream, 8))
            descriptors.append({"kind": "ebx", "name": _string_at(stream, strings_base + name_offset),
                                "original": original_size, "sha1": hashes[index], "type": 0, "uid": b""})
        resources: list[dict[str, object]] = []
        for index in range(res_count):
            name_offset, original_size = struct.unpack(">II", _exact(stream, 8))
            resources.append({"kind": "res", "name": _string_at(stream, strings_base + name_offset),
                              "original": original_size, "sha1": hashes[ebx_count + index], "uid": b""})
        for resource in resources:
            resource["type"] = _u32(stream)
        for _resource in resources:
            _exact(stream, 16)
        for _resource in resources:
            _exact(stream, 8)
        descriptors.extend(resources)
        for index in range(chunk_count):
            uid = _exact(stream, 16)
            _exact(stream, 8)
            descriptors.append({"kind": "chunk", "name": str(uuid.UUID(bytes=uid)), "original": 0,
                                "sha1": hashes[ebx_count + res_count + index], "type": 0, "uid": uid})

        stream.seek(meta_offset + meta_size)
        current_cas = _u32(stream)
        files: list[BundleFile] = []
        for descriptor in descriptors:
            value = _u32(stream)
            candidate = layout.cas_path(value)
            previous = layout.cas_path(current_cas)
            if candidate is not None and (previous is None or not _is_cas_record(previous, value)):
                current_cas = value
                address = _u32(stream)
            else:
                address = value
            packed_size = _u32(stream)
            cas_path = layout.cas_path(current_cas)
            if cas_path is None:
                continue
            if packed_size <= 0 or packed_size > MAX_CAS_RECORD:
                continue
            location = CasLocation(
                current_cas, cas_path, address, packed_size,
                int(descriptor["original"]), bytes(descriptor["sha1"]),
            )
            files.append(BundleFile(
                str(descriptor["kind"]), str(descriptor["name"]), int(descriptor["type"]),
                bytes(descriptor["uid"]), location,
            ))
    return BundleInfo(name, sb_path, offset, tuple(files))


def iter_toc_bundle_refs(layout: LayoutMap, toc_path: Path):
    """Yield the TOC's cheap bundle name/path/offset index without parsing payloads."""
    sb_path = toc_path.with_suffix(".sb")
    if not sb_path.is_file() or sb_path.stat().st_size < 36:
        return
    stream = _toc_payload(toc_path)
    if _u32(stream) != INDEX_MAGIC:
        raise FrostbiteIndexError(f"{toc_path.name} has an unsupported TOC index.")
    values = [_u32(stream) for _ in range(10)]
    item_count = values[1]
    strings_offset = values[7]
    if item_count > MAX_BUNDLES_PER_TOC or strings_offset > stream.getbuffer().nbytes:
        raise FrostbiteIndexError(f"{toc_path.name} exceeds the TOC safety limits.")
    _exact(stream, item_count * 4)
    _exact(stream, 4)
    while stream.tell() % 8:
        _exact(stream, 1)
    for _ in range(item_count):
        string_offset, _size, _unknown, bundle_offset = struct.unpack(">4I", _exact(stream, 16))
        name = _string_at(stream, strings_offset + string_offset)
        yield name, sb_path, bundle_offset


def iter_toc_bundles(layout: LayoutMap, toc_path: Path):
    for name, sb_path, bundle_offset in iter_toc_bundle_refs(layout, toc_path):
        yield parse_bundle(layout, sb_path, bundle_offset, name)


def _toc_paths(layout: LayoutMap) -> list[Path]:
    paths: list[Path] = []
    for layout_root in (layout.data_root, layout.patch_root):
        if layout_root is None:
            continue
        for path in layout_root.rglob("*.toc"):
            if path.name.lower() != "layout.toc" and path.with_suffix(".sb").is_file():
                paths.append(path)
    return sorted(set(paths), key=lambda path: str(path).lower())


def _chunk_cache_path(root: Path) -> Path:
    identity = hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / "game_asset_explorer" / f"frostbite_chunks_{identity}.sqlite3"


def _initialize_chunk_database(database: sqlite3.Connection, root: Path) -> None:
    database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    database.execute(
        "CREATE TABLE chunks ("
        "uid BLOB PRIMARY KEY, cas_id INTEGER NOT NULL, cas_path TEXT NOT NULL, "
        "offset INTEGER NOT NULL, packed_size INTEGER NOT NULL, "
        "original_size INTEGER NOT NULL, sha1 BLOB NOT NULL)"
    )
    database.executemany(
        "INSERT INTO metadata VALUES (?, ?)",
        (("version", "1"), ("root", str(root.resolve()))),
    )


def _chunk_row(uid: bytes, item: BundleFile) -> tuple[object, ...]:
    return (
        uid, item.location.cas_id, str(item.location.cas_path),
        item.location.offset, item.location.packed_size,
        item.location.original_size, item.location.sha1,
    )


def save_chunk_index(root: Path, chunks: dict[bytes, BundleFile]) -> None:
    """Persist the installation-wide UID index produced by the full scan."""
    cache = _chunk_cache_path(root)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(temporary) as database:
        _initialize_chunk_database(database, root)
        database.executemany(
            "INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_chunk_row(uid, item) for uid, item in chunks.items()),
        )
    temporary.replace(cache)


def load_chunk_index(
    root: Path, requested_uids: set[bytes] | None = None,
) -> dict[bytes, BundleFile]:
    """Load requested UID locations without rescanning every bundle per click."""
    cache = _chunk_cache_path(root)
    try:
        with sqlite3.connect(f"file:{cache}?mode=ro", uri=True) as database:
            metadata = dict(database.execute("SELECT key, value FROM metadata"))
            if metadata.get("version") != "1" or Path(metadata.get("root", "")).resolve() != root.resolve():
                return {}
            if requested_uids is None:
                rows = database.execute(
                    "SELECT uid, cas_id, cas_path, offset, packed_size, original_size, sha1 FROM chunks"
                ).fetchall()
            elif not requested_uids:
                return {}
            else:
                rows = []
                requested = tuple(requested_uids)
                # Keep below SQLite's common 999-variable limit. Large games
                # can expose thousands of MeshSets in a single inventory pass.
                for start in range(0, len(requested), 500):
                    batch = requested[start:start + 500]
                    placeholders = ",".join("?" for _ in batch)
                    rows.extend(database.execute(
                        "SELECT uid, cas_id, cas_path, offset, packed_size, original_size, sha1 "
                        f"FROM chunks WHERE uid IN ({placeholders})",
                        batch,
                    ).fetchall())
        result: dict[bytes, BundleFile] = {}
        for uid, cas_id, raw_cas_path, offset, packed_size, original_size, sha1 in rows:
            uid = bytes(uid)
            cas_path = Path(raw_cas_path)
            location = CasLocation(
                int(cas_id), cas_path, int(offset), int(packed_size),
                int(original_size), bytes(sha1),
            )
            if len(uid) == 16 and cas_path.is_file() and 0 < location.packed_size <= MAX_CAS_RECORD:
                result[uid] = BundleFile("chunk", str(uuid.UUID(bytes=uid)), 0, uid, location)
        return result
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {}


def _is_patch_path(layout: LayoutMap, path: Path) -> bool:
    if layout.patch_root is None:
        return False
    try:
        path.resolve().relative_to(layout.patch_root.resolve())
        return True
    except ValueError:
        return False


def find_bundle_chunks(
    layout: LayoutMap, bundle_name: str, current_bundle: BundleInfo,
) -> dict[bytes, BundleFile]:
    """Merge chunk references from matching base and patch bundle layers.

    Frostbite may keep an unchanged high-detail chunk in the base installation
    while a patch bundle supplies the MeshSet or another LOD.  TOC names let us
    locate the matching bundles cheaply; only exact-name matches are parsed.
    Patch records are applied after base records, mirroring runtime precedence.
    """
    matches: list[tuple[bool, Path, int, str]] = []
    for toc_path in _toc_paths(layout):
        try:
            for name, sb_path, offset in iter_toc_bundle_refs(layout, toc_path):
                if name.casefold() == bundle_name.casefold():
                    matches.append((_is_patch_path(layout, sb_path), sb_path, offset, name))
        except (FrostbiteIndexError, OSError):
            continue
    current_key = (current_bundle.sb_path.resolve(), current_bundle.offset)
    if not any((path.resolve(), offset) == current_key for _patch, path, offset, _name in matches):
        matches.append((
            _is_patch_path(layout, current_bundle.sb_path), current_bundle.sb_path,
            current_bundle.offset, current_bundle.name,
        ))
    chunks: dict[bytes, BundleFile] = {}
    for _patch, sb_path, offset, name in sorted(
        matches, key=lambda item: (item[0], str(item[1]).lower(), item[2]),
    ):
        try:
            bundle = (
                current_bundle
                if (sb_path.resolve(), offset) == current_key
                else parse_bundle(layout, sb_path, offset, name)
            )
        except (FrostbiteIndexError, OSError):
            continue
        for item in bundle.files:
            if item.kind == "chunk" and item.uid:
                chunks[item.uid] = item
    return chunks


def inspect_frostbite_meshsets(root: Path) -> tuple[list[AssetRecord], list[str]]:
    """Catalogue typed MeshSet and animation-related records from an install."""
    layout = load_layout_map(root)
    discovered: list[AssetRecord] = []
    warnings: list[str] = []
    seen_meshsets: set[tuple[bytes, str]] = set()
    seen_animations: set[tuple[str, bytes, str]] = set()
    meshset_count = 0
    rig_record_count = 0
    cache = _chunk_cache_path(root)
    temporary = cache.with_suffix(".tmp")
    chunk_database: sqlite3.Connection | None = None
    indexed_bundles = 0
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        chunk_database = sqlite3.connect(temporary)
        _initialize_chunk_database(chunk_database, root)
    except (OSError, sqlite3.Error) as error:
        if chunk_database is not None:
            chunk_database.close()
        chunk_database = None
        warnings.append(f"Could not start the Frostbite chunk index: {error}")
    for toc_path in _toc_paths(layout):
        try:
            for bundle in iter_toc_bundles(layout, toc_path):
                if chunk_database is not None:
                    chunk_database.executemany(
                        "INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            _chunk_row(item.uid, item) for item in bundle.files
                            if item.kind == "chunk" and item.uid
                        ),
                    )
                    indexed_bundles += 1
                    if indexed_bundles % 250 == 0:
                        chunk_database.commit()
                for index, item in enumerate(bundle.files):
                    is_meshset = item.kind == "res" and item.resource_type == MESHSET_RESOURCE_TYPE
                    rig_kind = frostbite_rig_record_kind(item)
                    if not is_meshset and rig_kind is None:
                        continue
                    if is_meshset:
                        key = (item.location.sha1, item.name.lower())
                        if key in seen_meshsets:
                            continue
                        seen_meshsets.add(key)
                        if meshset_count >= MAX_MESHSETS:
                            if not any("MeshSet display limit" in warning for warning in warnings):
                                warnings.append(
                                    f"MeshSet display limit reached at {MAX_MESHSETS:,}; "
                                    "the scan continued to catalogue shared geometry chunks."
                                )
                            continue
                        meshset_count += 1
                    else:
                        animation_key = (item.kind, item.location.sha1, item.name.lower())
                        if animation_key in seen_animations:
                            continue
                        seen_animations.add(animation_key)
                        if rig_record_count >= MAX_ANIMATION_RECORDS:
                            if not any("rig-record display limit" in warning for warning in warnings):
                                warnings.append(
                                    f"Frostbite rig-record display limit reached at "
                                    f"{MAX_ANIMATION_RECORDS:,}."
                                )
                            continue
                        rig_record_count += 1
                    relative_container = str(bundle.sb_path.relative_to(root))
                    internal = f"{item.name}.meshset" if is_meshset else f"{item.name}.{item.kind}"
                    discovered.append(AssetRecord(
                        path=bundle.sb_path,
                        relative_path=f"{relative_container}::{internal}",
                        kind="mesh" if is_meshset else rig_kind,
                        extension=".meshset" if is_meshset else f".{item.kind}",
                        size=item.location.original_size,
                        engine="Frostbite", directly_viewable=is_meshset,
                        container_path=bundle.sb_path, internal_path=internal,
                        metadata={
                            "reader": (
                                "frostbite-meshset-cas" if is_meshset
                                else f"frostbite-{rig_kind}-record"
                            ),
                            "record_kind": item.kind,
                            "classification": (
                                "typed-meshset" if is_meshset
                                else "named-skeleton-candidate" if rig_kind == "skeleton"
                                else "animation-path-candidate"
                            ),
                            "bundle_name": bundle.name,
                            "bundle_offset": bundle.offset,
                            "resource_index": index,
                            "cas_id": item.location.cas_id,
                            "cas_path": str(item.location.cas_path),
                            "cas_offset": item.location.offset,
                            "packed_size": item.location.packed_size,
                            "original_size": item.location.original_size,
                            "sha1": item.location.sha1.hex(),
                        },
                    ))
        except Exception as error:
            if len(warnings) < 40:
                warnings.append(f"Could not index {toc_path.relative_to(root)}: {error}")
    meshsets = [asset for asset in discovered if asset.extension == ".meshset"]
    if not meshsets:
        warnings.append("No readable MeshSet RES records were found in the Frostbite metadata.")
    if chunk_database is not None:
        try:
            chunk_database.commit()
            chunk_database.close()
            chunk_database = None
            temporary.replace(cache)
        except (OSError, sqlite3.Error) as error:
            warnings.append(f"Could not cache the Frostbite chunk index: {error}")
        finally:
            if chunk_database is not None:
                chunk_database.close()
    _annotate_meshset_lod_availability(root, meshsets, warnings)
    return discovered, warnings


def _annotate_meshset_lod_availability(
    root: Path, assets: list[AssetRecord], warnings: list[str],
) -> None:
    """Resolve declared LOD GUIDs against the installation-wide chunk index."""
    if not assets:
        return
    try:
        oodle = OodleDecoder(find_oodle_runtime(root))
    except FrostbiteCasError as error:
        warnings.append(f"Could not inspect MeshSet LOD availability: {error}")
        return

    declared_by_asset: list[tuple[AssetRecord, list[bytes]]] = []
    requested_uids: set[bytes] = set()
    failures = 0
    for asset in assets:
        try:
            location = _location_from_asset(asset)
            raw = decode_cas_record(
                _read_cas(location), oodle, max_output_bytes=MAX_CAS_RECORD,
            )
            header = inspect_anthem_meshset(raw)
            declared = [uuid.UUID(chunk_id).bytes for chunk_id in header.chunk_ids]
            asset.metadata["declared_lods"] = list(range(len(declared)))
            asset.metadata["lod_chunk_ids"] = [uid.hex() for uid in declared]
            declared_by_asset.append((asset, declared))
            requested_uids.update(declared)
        except (FrostbiteCasError, MeshFormatError, OSError, ValueError) as error:
            failures += 1
            asset.metadata["lod_probe_error"] = str(error)

    available_chunks = load_chunk_index(root, requested_uids)
    for asset, declared in declared_by_asset:
        available_lods = [index for index, uid in enumerate(declared) if uid in available_chunks]
        asset.metadata["available_lods"] = available_lods
        asset.metadata["lod0_available"] = 0 in available_lods
        lod_paths: dict[str, str] = {}
        for index in available_lods:
            location = available_chunks[declared[index]].location
            try:
                path_text = str(location.cas_path.relative_to(root))
            except ValueError:
                path_text = str(location.cas_path)
            lod_paths[str(index)] = path_text
        asset.metadata["available_lod_cas_paths"] = lod_paths
        if declared and declared[0] in available_chunks:
            lod0_location = available_chunks[declared[0]].location
            try:
                lod0_path = str(lod0_location.cas_path.relative_to(root))
            except ValueError:
                lod0_path = str(lod0_location.cas_path)
            asset.metadata["lod0_cas_path"] = lod0_path
            asset.metadata["lod0_cas_offset"] = lod0_location.offset
    if failures:
        warnings.append(
            f"Could not inspect LOD declarations for {failures:,} MeshSet record(s); "
            "they are excluded from the LOD0-only filter."
        )


def _read_cas(location: CasLocation) -> bytes:
    with location.cas_path.open("rb") as stream:
        stream.seek(location.offset)
        raw = stream.read(location.packed_size)
    if len(raw) != location.packed_size:
        raise MeshFormatError(f"CAS record in {location.cas_path.name} is truncated.")
    if hashlib.sha1(raw).digest() != location.sha1:
        raise MeshFormatError(f"CAS record hash mismatch in {location.cas_path.name}.")
    return raw


def _location_from_asset(asset: AssetRecord) -> CasLocation:
    meta = asset.metadata
    try:
        return CasLocation(
            int(meta["cas_id"]), Path(str(meta["cas_path"])), int(meta["cas_offset"]),
            int(meta["packed_size"]), int(meta["original_size"]), bytes.fromhex(str(meta["sha1"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MeshFormatError("Frostbite MeshSet record has incomplete CAS metadata.") from error


def extract_frostbite_record(root: Path, asset: AssetRecord) -> bytes:
    """Decode one indexed EBX/RES record from CAS without interpreting its schema."""
    if asset.metadata.get("reader") not in {
        "frostbite-animation-record", "frostbite-skeleton-record",
    }:
        raise MeshFormatError("The selected asset is not an indexed Frostbite rig record.")
    location = _location_from_asset(asset)
    oodle = OodleDecoder(find_oodle_runtime(root))
    return decode_cas_record(
        _read_cas(location), oodle, max_output_bytes=MAX_CAS_RECORD,
    )


def extract_frostbite_ebx_dependencies(
    root: Path, asset: AssetRecord,
) -> tuple[bytes, list[tuple[AssetRecord, bytes]]]:
    """Extract an EBX wrapper and resolve its direct external EBX references.

    Named master-skeleton and animation records can be tiny aliases whose only
    useful field is an external pointer. Resolution starts in the owning bundle
    and falls back to the complete installation metadata.
    """
    primary = extract_frostbite_record(root, asset)
    if asset.metadata.get("record_kind") != "ebx":
        return primary, []
    header = parse_anthem_ebx_header(primary)
    targets = {file_guid for file_guid, _class_guid in header.imports}
    if not targets:
        return primary, []

    layout = load_layout_map(root)
    oodle = OodleDecoder(find_oodle_runtime(root))
    bundles: list[BundleInfo] = []
    seen_bundles: set[tuple[Path, int]] = set()
    current_key = (asset.path, int(asset.metadata.get("bundle_offset", -1)))
    if current_key[1] >= 0:
        bundles.append(parse_bundle(
            layout, current_key[0], current_key[1],
            str(asset.metadata.get("bundle_name", "")),
        ))
        seen_bundles.add(current_key)

    def remaining_bundles():
        for toc_path in _toc_paths(layout):
            for name, sb_path, offset in iter_toc_bundle_refs(layout, toc_path):
                key = (sb_path, offset)
                if key in seen_bundles:
                    continue
                seen_bundles.add(key)
                yield parse_bundle(layout, sb_path, offset, name)

    matches: list[tuple[AssetRecord, bytes]] = []
    seen_records: set[tuple[bytes, str]] = set()
    for bundle in itertools.chain(bundles, remaining_bundles()):
        for index, item in enumerate(bundle.files):
            if item.kind != "ebx":
                continue
            key = (item.location.sha1, item.name.casefold())
            if key in seen_records:
                continue
            seen_records.add(key)
            try:
                decoded = decode_cas_record(
                    _read_cas(item.location), oodle, max_output_bytes=MAX_CAS_RECORD,
                )
                candidate = parse_anthem_ebx_header(decoded)
            except (FrostbiteCasError, MeshFormatError, OSError, ValueError):
                continue
            if candidate.file_guid not in targets:
                continue
            try:
                relative_container = str(bundle.sb_path.relative_to(root))
            except ValueError:
                relative_container = str(bundle.sb_path)
            internal = f"{item.name}.ebx"
            dependency_kind = asset.kind if asset.kind in {"skeleton", "animation"} else "unknown"
            linked = AssetRecord(
                path=bundle.sb_path,
                relative_path=f"{relative_container}::{internal}",
                kind=dependency_kind, extension=".ebx", size=item.location.original_size,
                engine="Frostbite", directly_viewable=False,
                container_path=bundle.sb_path, internal_path=internal,
                metadata={
                    "reader": f"frostbite-{dependency_kind}-record",
                    "record_kind": "ebx",
                    "classification": f"linked-{dependency_kind}-dependency",
                    "file_guid": str(candidate.file_guid),
                    "referenced_by": asset.internal_path,
                    "bundle_name": bundle.name,
                    "bundle_offset": bundle.offset,
                    "resource_index": index,
                    "cas_id": item.location.cas_id,
                    "cas_path": str(item.location.cas_path),
                    "cas_offset": item.location.offset,
                    "packed_size": item.location.packed_size,
                    "original_size": item.location.original_size,
                    "sha1": item.location.sha1.hex(),
                },
            )
            matches.append((linked, decoded))
            targets.remove(candidate.file_guid)
            if not targets:
                return primary, matches
    unresolved = ", ".join(sorted(str(value) for value in targets))
    raise MeshFormatError(
        "The master-skeleton wrapper was extracted, but its linked EBX payload "
        f"could not be found in the scanned installation (missing GUID: {unresolved})."
    )


def preview_frostbite_meshset(
    root: Path, asset: AssetRecord, lod_index: int | None = None,
) -> tuple[list[MeshData], int, str, list[int], dict[str, object]]:
    """Decode one indexed MeshSet LOD and report its selectable real LODs."""
    layout = load_layout_map(root)
    runtime = find_oodle_runtime(root)
    oodle = OodleDecoder(runtime)
    mesh_location = _location_from_asset(asset)
    meshset_raw = decode_cas_record(_read_cas(mesh_location), oodle, max_output_bytes=MAX_CAS_RECORD)
    header = inspect_anthem_meshset(meshset_raw)
    bundle_offset = int(asset.metadata.get("bundle_offset", -1))
    bundle = parse_bundle(layout, asset.path, bundle_offset, str(asset.metadata.get("bundle_name", "")))
    chunks = find_bundle_chunks(layout, bundle.name, bundle)
    # The full installation scan sees chunk UIDs across every bundle, not only
    # bundles sharing the MeshSet's logical name. This catches high-detail LODs
    # stored in shared streaming/customization bundles.
    declared_uids = {uuid.UUID(chunk_id).bytes for chunk_id in header.chunk_ids}
    chunks.update(load_chunk_index(root, declared_uids))
    errors: list[str] = []
    # LOD numbers run in the opposite direction to intuitive detail: LOD0 is
    # highest-detail. Expose the slider low-to-high while retaining the cheap
    # lowest-detail LOD as the initial preview.
    declared = list(enumerate(header.chunk_ids))
    available_lods = [
        index for index, chunk_id in reversed(declared)
        if uuid.UUID(chunk_id).bytes in chunks
    ]
    if lod_index is not None:
        if lod_index not in range(len(header.chunk_ids)):
            raise MeshFormatError(f"LOD{lod_index} is not declared by this MeshSet.")
        candidates = [(lod_index, header.chunk_ids[lod_index])]
    else:
        candidates = list(reversed(declared))
    for candidate_lod, chunk_id in candidates:
        uid = uuid.UUID(chunk_id).bytes
        chunk = chunks.get(uid)
        if chunk is None:
            errors.append(
                f"LOD{candidate_lod} chunk is not present in the matching base or patch bundle layers."
            )
            continue
        try:
            chunk_raw = decode_cas_record(
                _read_cas(chunk.location), oodle, max_output_bytes=256 * 1024 * 1024,
            )
            return (
                decode_anthem_geometry(meshset_raw, chunk_raw, candidate_lod),
                candidate_lod,
                header.name or asset.internal_path or "MeshSet",
                available_lods,
                inspect_anthem_rig(meshset_raw, candidate_lod, chunk_raw).to_dict(),
            )
        except (FrostbiteCasError, MeshFormatError, OSError) as error:
            errors.append(f"LOD{candidate_lod}: {error}")
    detail = errors[-1] if errors else "The MeshSet declared no geometry chunks."
    raise MeshFormatError(f"No previewable MeshSet LOD could be decoded. {detail}")
