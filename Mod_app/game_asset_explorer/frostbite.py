"""Bounded, read-only Frostbite container inspection.

This module deliberately provides metadata inventory rather than claiming that a
TOC or superbundle is itself a renderable asset.  Frostbite stores payloads in
CAS files and its resource schemas vary by engine generation/build.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from .geometry import MeshData, MeshFormatError
from .models import AssetRecord

FROSTBITE_EXTENSIONS = frozenset({".toc", ".sb", ".cas", ".cat"})
_STRING = re.compile(rb"[\x20-\x7e]{5,260}")
_KNOWN_SUFFIXES = (
    ".mesh", ".anim", ".animation", ".dds", ".png", ".tga", ".texture",
    ".rig", ".skeleton", ".skel", ".ebx", ".res",
)


def select_metadata_containers(
    assets: list[AssetRecord], category: str, *, limit: int = 12,
) -> list[AssetRecord]:
    """Choose a small, useful set of metadata containers for one inventory pass.

    TOC/SB files normally come in same-name pairs. SB is preferred because it
    carries the bundle record metadata; inspecting both mostly duplicates work.
    Neutral category words only influence order and do not identify a game.
    """
    pairs: dict[Path, AssetRecord] = {}
    for asset in assets:
        if asset.engine != "Frostbite" or asset.extension not in {".toc", ".sb"}:
            continue
        key = asset.path.with_suffix("")
        current = pairs.get(key)
        if current is None or (current.extension == ".toc" and asset.extension == ".sb"):
            pairs[key] = asset

    hints = {
        "Animations": ("anim", "motion", "cinematic", "conversation"),
        "Textures": ("texture", "ui", "frontend", "visual"),
        "Images": ("ui", "frontend", "texture"),
        "Props": ("prop", "object", "item", "default"),
        "Map parts": ("level", "world", "terrain", "environment"),
        "Skeletons": ("rig", "skeleton", "character", "default"),
        "Weapons & carryables": ("weapon", "item", "equipment", "default"),
    }.get(category, ("character", "default", "global"))

    def rank(asset: AssetRecord) -> tuple[int, int, str]:
        name = asset.relative_path.lower()
        return (-sum(term in name for term in hints), asset.size, name)

    return sorted(pairs.values(), key=rank)[:limit]


def _kind_for_reference(name: str) -> str | None:
    value = name.lower().replace("\\", "/")
    # Checked before "animation": confirmed on real data that a texture
    # reference can sit inside an "animations/" folder tree (e.g. a
    # character's head texture bundled alongside its facial animations,
    # such as ".../head/textures/hmf_x_pcatexture_diffhf_..._antstate.ebx").
    # That kind of path was being misclassified as "animation" because the
    # word "animation" matched earlier in the SAME path, even though the
    # file's own name and immediate folder clearly say texture. Also added
    # "pcatexture" and "textures/" (plural) here: the original "_diffuse"
    # check doesn't match real Anthem naming, which abbreviates it
    # (diffhf, diffflf), not spelling out "diffuse".
    if value.endswith((".dds", ".png", ".tga", ".texture")) or any(
        term in value for term in (
            "/texture", "textures/", "pcatexture", "_diffuse", "_normal", "_albedo",
        )
    ):
        return "texture"
    if any(term in value for term in ("animation", "/anim/", "_anim", "animclip")):
        return "animation"
    if any(term in value for term in ("skeleton", "/rig/", "_rig", "/skel", "bonehierarchy")):
        return "skeleton"
    if value.endswith(".mesh") or any(term in value for term in (
        "/mesh", "_mesh", "/characters/", "/weapons/", "/props/", "/levels/",
        "/world/", "/vehicles/",
    )):
        return "mesh"
    return None


def inspect_frostbite_metadata(
    root: Path,
    asset: AssetRecord,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    max_references: int = 2_000,
) -> tuple[list[AssetRecord], list[str]]:
    """Extract named asset references with strict per-file work limits.

    References are useful inventory records, not decoded payloads.  They are
    tagged accordingly so preview never routes them to another engine reader.
    """
    if asset.engine != "Frostbite" or asset.extension not in {".toc", ".sb"}:
        return [], [f"{asset.relative_path} is not a Frostbite metadata container."]
    try:
        with asset.path.open("rb") as stream:
            raw = stream.read(max_bytes)
    except OSError as error:
        return [], [f"Could not inspect {asset.relative_path}: {error}"]

    found: list[AssetRecord] = []
    seen: set[str] = set()
    for match in _STRING.finditer(raw):
        try:
            name = match.group().decode("utf-8").strip("\0 \t\r\n")
        except UnicodeDecodeError:
            continue
        normalized = name.replace("\\", "/")
        lower = normalized.lower()
        # Property names and arbitrary prose are common in binary metadata.
        # Retain only path-like strings or explicit resource filenames.
        if not ("/" in normalized or lower.endswith(_KNOWN_SUFFIXES)):
            continue
        kind = _kind_for_reference(normalized) or "other"
        if lower in seen:
            continue
        seen.add(lower)
        found.append(AssetRecord(
            path=asset.path,
            relative_path=f"{asset.relative_path}::{normalized}",
            kind=kind,
            extension=Path(normalized).suffix.lower(),
            size=0,
            engine="Frostbite",
            directly_viewable=False,
            container_path=asset.path,
            internal_path=normalized,
            metadata={"reader": "frostbite-reference", "decoded": False},
        ))
        if len(found) >= max_references:
            break

    notices: list[str] = []
    if asset.size > len(raw):
        notices.append(
            f"Inspected the first {len(raw):,} bytes of {asset.relative_path}; "
            "the bounded inventory did not scan the complete container."
        )
    return found, notices


def frostbite_container_summary(asset: AssetRecord, all_assets: list[AssetRecord]) -> str:
    """Return a concise explanation of a TOC/SB/CAS container relationship."""
    siblings = [item for item in all_assets if item.engine == "Frostbite"]
    counts = {suffix: sum(item.extension == suffix for item in siblings) for suffix in FROSTBITE_EXTENSIONS}
    cas_bytes = sum(item.size for item in siblings if item.extension == ".cas")
    paired_suffix = ".sb" if asset.extension == ".toc" else ".toc" if asset.extension == ".sb" else None
    paired = asset.path.with_suffix(paired_suffix).is_file() if paired_suffix else False
    pair_text = "paired metadata file found" if paired else "no same-name metadata pair"
    return (
        f"Frostbite {asset.extension.upper().lstrip('.')} container\n\n"
        f"{asset.relative_path}\n"
        f"Size: {asset.size:,} bytes\n"
        f"{pair_text}\n\n"
        f"Installation inventory: {counts['.toc']:,} TOC, {counts['.sb']:,} SB, "
        f"{counts['.cat']:,} CAT, {counts['.cas']:,} CAS\n"
        f"CAS payload size: {cas_bytes / (1024 ** 3):.2f} GiB\n\n"
        "TOC/SB files index EBX, RES and chunk records; their renderable payloads "
        "live primarily in CAS archives. Named references are inventoried with "
        "bounded reads, but this container is not itself a mesh or image."
    )


def _record_from_dict(data: dict[str, object]) -> AssetRecord:
    container = data.get("container_path")
    return AssetRecord(
        path=Path(str(data["path"])), relative_path=str(data["relative_path"]),
        kind=str(data["kind"]), extension=str(data["extension"]),  # type: ignore[arg-type]
        size=int(data["size"]), engine=str(data["engine"]) if data.get("engine") else None,
        directly_viewable=bool(data.get("directly_viewable")),
        container_path=Path(str(container)) if container else None,
        internal_path=str(data["internal_path"]) if data.get("internal_path") else None,
        metadata=dict(data.get("metadata") or {}),
    )


def _worker(
    action: str, root: Path, asset: AssetRecord | None, timeout: int,
    lod_index: int | None = None, virtual_key: bytes | None = None,
    assets: list[AssetRecord] | None = None,
) -> dict[str, object]:
    request: dict[str, object] = {"action": action, "root": str(root)}
    if asset is not None:
        request["asset"] = asset.to_dict()
    if lod_index is not None:
        request["lod"] = lod_index
    if virtual_key is not None:
        request["key"] = virtual_key.hex()
    if assets is not None:
        request["assets"] = [item.to_dict() for item in assets]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "game_asset_explorer.frostbite_worker"],
            input=json.dumps(request), capture_output=True, text=True,
            timeout=timeout, creationflags=flags,
        )
    except subprocess.TimeoutExpired as error:
        raise MeshFormatError(
            f"Frostbite {action} exceeded the {timeout}-second safety limit and was stopped."
        ) from error
    if completed.returncode:
        detail = completed.stderr.strip().splitlines()
        raise MeshFormatError(
            f"Frostbite reader failed safely: {detail[-1] if detail else completed.returncode}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise MeshFormatError("Frostbite reader returned an invalid response.") from error
    if response.get("error"):
        raise MeshFormatError(str(response["error"]))
    return response


def inspect_frostbite_installation_isolated(
    root: Path, *, timeout: int = 300,
) -> tuple[list[AssetRecord], list[str]]:
    """Index typed MeshSet records outside the GUI process."""
    response = _worker("inspect", root, None, timeout)
    return (
        [_record_from_dict(item) for item in response.get("assets", [])],
        [str(item) for item in response.get("warnings", [])],
    )


def preview_frostbite_meshset_isolated(
    root: Path, asset: AssetRecord, *, lod_index: int | None = None, timeout: int = 120,
) -> tuple[str, list[MeshData], int, str, list[int], dict[str, object]] | tuple[str, str]:
    """Decode one MeshSet outside the GUI process, including Oodle calls."""
    response = _worker("preview", root, asset, timeout, lod_index)
    if response.get("kind") == "unavailable":
        return "unavailable", str(response.get("reason") or "No previewable geometry was found.")
    meshes = [
        MeshData(
            str(item["name"]),
            [tuple(vertex) for vertex in item["vertices"]],
            [tuple(face) for face in item["faces"]],
            [tuple(values) for values in item["skin_bones"]]
            if item.get("skin_bones") is not None else None,
            [tuple(values) for values in item["skin_weights"]]
            if item.get("skin_weights") is not None else None,
        )
        for item in response.get("meshes", [])
    ]
    if not meshes:
        raise MeshFormatError("Frostbite reader returned no mesh sections.")
    return (
        "mesh", meshes, int(response["lod"]),
        str(response.get("name") or asset.internal_path),
        [int(item) for item in response.get("available_lods", [])],
        dict(response.get("rig_diagnostic") or {
            "summary": "Rigging evidence was not inspected.",
            "likely_skinned": False,
            "weights_decoded": False,
            "sections": [],
        }),
    )


def extract_frostbite_record_isolated(
    root: Path, asset: AssetRecord, *, timeout: int = 120,
) -> bytes:
    """Decode one typed EBX/RES CAS record outside the GUI process."""
    response = _worker("extract_record", root, asset, timeout)
    if response.get("kind") != "record" or not isinstance(response.get("data"), str):
        raise MeshFormatError("Frostbite reader returned an invalid extracted record.")
    try:
        raw = base64.b64decode(str(response["data"]), validate=True)
    except (ValueError, TypeError) as error:
        raise MeshFormatError("Frostbite reader returned corrupt extracted bytes.") from error
    if len(raw) != int(response.get("size", -1)):
        raise MeshFormatError("Frostbite reader returned a truncated extracted record.")
    return raw


def count_frostbite_animation_clips_isolated(
    root: Path, assets: list[AssetRecord], *, timeout: int = 1800,
) -> list[int | None]:
    """Count clips across many RES records in one crash-contained worker."""
    response = _worker(
        "count_animation_clips", root, None, timeout, assets=assets,
    )
    if response.get("kind") != "animation_clip_counts":
        raise MeshFormatError("Frostbite reader returned invalid animation clip counts.")
    raw_counts = response.get("counts")
    if not isinstance(raw_counts, list) or len(raw_counts) != len(assets):
        raise MeshFormatError("Frostbite reader returned an incomplete animation count table.")
    return [None if value is None else max(0, int(value)) for value in raw_counts]


def extract_frostbite_record_dependencies_isolated(
    root: Path, asset: AssetRecord, *, timeout: int = 600,
) -> tuple[bytes, list[tuple[AssetRecord, bytes]]]:
    """Decode an EBX wrapper and the direct external EBX files it references."""
    response = _worker("extract_record_dependencies", root, asset, timeout)
    if response.get("kind") != "record_dependencies":
        raise MeshFormatError("Frostbite reader returned invalid dependency extraction data.")

    def decoded(value: object, size: object) -> bytes:
        try:
            result = base64.b64decode(str(value), validate=True)
        except (ValueError, TypeError) as error:
            raise MeshFormatError("Frostbite reader returned corrupt extracted bytes.") from error
        if len(result) != int(size):
            raise MeshFormatError("Frostbite reader returned a truncated extracted record.")
        return result

    primary = decoded(response.get("data"), response.get("size", -1))
    dependencies: list[tuple[AssetRecord, bytes]] = []
    for item in response.get("dependencies", []):
        if not isinstance(item, dict) or not isinstance(item.get("asset"), dict):
            raise MeshFormatError("Frostbite reader returned invalid dependency metadata.")
        dependencies.append((
            _record_from_dict(dict(item["asset"])),
            decoded(item.get("data"), item.get("size", -1)),
        ))
    return primary, dependencies


def resolve_frostbite_virtual_asset_key_isolated(
    root: Path, asset: AssetRecord, key: bytes, *, timeout: int = 900,
) -> tuple[AssetRecord | None, bytes | None, int, bool]:
    """Resolve one Anthem GD.DATA key in a crash-contained worker."""
    if len(key) != 8:
        raise MeshFormatError("A Gameplay Data virtual-asset key must be exactly eight bytes.")
    response = _worker(
        "resolve_virtual_asset_key", root, asset, timeout, virtual_key=key,
    )
    if response.get("kind") != "virtual_asset_resolution":
        raise MeshFormatError("Frostbite reader returned invalid virtual-asset data.")
    scanned = int(response.get("scanned_records", 0))
    exhaustive = bool(response.get("exhaustive"))
    asset_data = response.get("asset")
    encoded = response.get("data")
    if asset_data is None and encoded is None:
        return None, None, scanned, exhaustive
    if not isinstance(asset_data, dict) or not isinstance(encoded, str):
        raise MeshFormatError("Frostbite reader returned incomplete virtual-asset data.")
    resolved = _record_from_dict(asset_data)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise MeshFormatError("Frostbite reader returned corrupt virtual-asset bytes.") from error
    if len(raw) != int(response.get("size", -1)):
        raise MeshFormatError("Frostbite reader returned a truncated virtual asset.")
    return resolved, raw, scanned, exhaustive


def resolve_frostbite_primary_rig_key_isolated(
    root: Path, asset: AssetRecord, key: bytes, *, timeout: int = 900,
) -> tuple[AssetRecord | None, bytes | None, int, bool]:
    """Resolve and return the compact named mapping for one PrimaryRig key."""
    if len(key) != 8:
        raise MeshFormatError("A PrimaryRig virtual-asset key must be exactly eight bytes.")
    response = _worker(
        "resolve_primary_rig_key", root, asset, timeout, virtual_key=key,
    )
    if response.get("kind") != "primary_rig_resolution":
        raise MeshFormatError("Frostbite reader returned invalid PrimaryRig data.")
    scanned = int(response.get("scanned_records", 0))
    exhaustive = bool(response.get("exhaustive"))
    asset_data = response.get("asset")
    encoded = response.get("data")
    if asset_data is None and encoded is None:
        return None, None, scanned, exhaustive
    if not isinstance(asset_data, dict) or not isinstance(encoded, str):
        raise MeshFormatError("Frostbite reader returned incomplete PrimaryRig data.")
    resolved = _record_from_dict(asset_data)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise MeshFormatError("Frostbite reader returned corrupt PrimaryRig data.") from error
    if len(raw) != int(response.get("size", -1)):
        raise MeshFormatError("Frostbite reader returned a truncated PrimaryRig mapping.")
    return resolved, raw, scanned, exhaustive
