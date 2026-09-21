from __future__ import annotations

import re
import os
from pathlib import Path, PurePosixPath
from typing import Callable, TypeVar

from .models import AssetRecord, CharacterBundle

T = TypeVar("T")

CHARACTER_TERMS = frozenset({
    "character", "characters", "char", "player", "hero", "enemy", "npc", "soldier",
    "body", "head", "face", "human", "creature", "javelin", "pilot", "armor", "armour",
    "avatar", "skin", "humanoid", "mob", "zombie", "boss",
})
ACCESSORY_TERMS = frozenset({"weapon", "gun", "rifle", "sword", "vehicle", "prop", "environment", "building", "terrain"})
WEAPON_TERMS = frozenset({
    "weapon", "gun", "rifle", "pistol", "sword", "knife", "bat", "carryable", "grenade",
    "launcher", "shotgun", "sniper", "cannon", "rocket", "ammo", "bullet", "melee", "bow",
})
MAP_TERMS = frozenset({
    "map", "world", "level", "scene", "building", "road", "street", "terrain", "environment",
    "interior", "bridge", "wall", "ground", "floor", "city", "landscape",
})
MAP_ARCHIVE_HINTS = frozenset({"map", "world", "level", "levels", "roads", "terrain", "interiors"})
PROP_TERMS = frozenset({
    "prop", "furniture", "chair", "table", "sign", "traffic", "lamp", "plant", "radar",
    "crate", "barrel", "door", "pickup", "decoration", "deco",
})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"})
TEXTURE_EXTENSIONS = frozenset({".dds", ".tga", ".ktx", ".ktx2", ".exr", ".tif", ".tiff", ".wtd"})
NOISE = frozenset({
    "mesh", "model", "geo", "geometry", "skel", "skeleton", "rig", "armature", "anim",
    "animation", "material", "mat", "texture", "tex", "diffuse", "normal", "albedo",
    "roughness", "metallic", "specular", "ao", "lod", "low", "high", "final", "export",
})


def tokens(value: str) -> set[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    parts = re.split(r"[^A-Za-z0-9]+", spaced.lower())
    result = set()
    for part in parts:
        part = re.sub(r"^(lod|l)[0-9]+$", "", part)
        part = re.sub(r"[0-9]+$", "", part)
        if len(part) >= 2 and part not in NOISE:
            result.add(part)
    return result


def asset_stem(asset: AssetRecord) -> str:
    return PurePosixPath(asset.internal_path).stem if asset.internal_path else asset.path.stem


def asset_parent(asset: AssetRecord):
    if asset.internal_path:
        return (asset.container_path or asset.path, str(PurePosixPath(asset.internal_path).parent).lower())
    return asset.path.parent


def asset_folder_key(asset: AssetRecord) -> tuple[str, str]:
    """Return a stable key for a loose or virtual container folder."""
    if asset.internal_path:
        container = asset.container_path or asset.path
        internal_parent = str(PurePosixPath(asset.internal_path).parent).lower()
        return (str(container).lower(), internal_parent)
    return (str(asset.path.parent).lower(), "")


def asset_folder_label(asset: AssetRecord) -> str:
    """Return the folder users recognize in a right-click action."""
    if asset.internal_path:
        parent = str(PurePosixPath(asset.internal_path).parent)
        return parent if parent not in {"", "."} else asset.path.name
    parent = str(PurePosixPath(asset.relative_path.replace("\\", "/")).parent)
    return parent if parent not in {"", "."} else asset.path.parent.name


def parse_virtual_location(value: str) -> tuple[Path, str] | None:
    """Parse ``archive.sb::internal/folder`` without confusing it for a disk path."""
    physical, separator, internal = value.partition("::")
    prefix = internal.replace("\\", "/").strip("/")
    if not separator or not physical.strip() or not prefix:
        return None
    return Path(physical.strip()), prefix


def asset_in_virtual_scope(
    asset: AssetRecord, container: Path, internal_prefix: str,
) -> bool:
    """Match a typed archive child beneath one exact virtual folder."""
    if not asset.internal_path:
        return False
    asset_container = asset.container_path or asset.path
    if os.path.normcase(str(asset_container.resolve())) != os.path.normcase(str(container.resolve())):
        return False
    internal = asset.internal_path.replace("\\", "/").strip("/").casefold()
    prefix = internal_prefix.replace("\\", "/").strip("/").casefold()
    return internal == prefix or internal.startswith(prefix + "/")


def folder_rows(
    items: list[T],
    asset_for: Callable[[T], AssetRecord],
    search_matches: Callable[[T], bool],
    expanded: set[tuple[str, str]],
) -> tuple[list[T], dict[tuple[str, str], int]]:
    """Collapse matching assets to one row per folder unless explicitly expanded.

    Search chooses which folders are visible and which matching item represents
    each collapsed folder. An expanded folder then exposes every item in the
    selected category, even when sibling names do not contain the query.
    """
    groups: dict[tuple[str, str], list[T]] = {}
    representatives: dict[tuple[str, str], T] = {}
    visible_keys: list[tuple[str, str]] = []
    for item in items:
        key = asset_folder_key(asset_for(item))
        groups.setdefault(key, []).append(item)
        if search_matches(item) and key not in representatives:
            representatives[key] = item
            visible_keys.append(key)

    rows: list[T] = []
    for key in visible_keys:
        rows.extend(groups[key] if key in expanded else (representatives[key],))
    return rows, {key: len(groups[key]) for key in visible_keys}


def items_in_folder(
    items: list[T], asset_for: Callable[[T], AssetRecord], key: tuple[str, str],
) -> list[T]:
    """Return only category items from one loose or virtual asset folder."""
    return [item for item in items if asset_folder_key(asset_for(item)) == key]


def asset_in_physical_scope(
    asset: AssetRecord, selected: Path, engine_root: Path,
) -> bool:
    """Match an indexed virtual asset to the user's selected disk subtree."""
    selected = selected.resolve()
    engine_root = engine_root.resolve()
    if selected == engine_root:
        return True
    try:
        selected.relative_to(engine_root)
    except ValueError:
        return True

    candidates: list[Path] = [asset.path]
    if asset.container_path:
        candidates.append(asset.container_path)
    for key in ("cas_path", "lod0_cas_path"):
        raw = asset.metadata.get(key)
        if raw:
            path = Path(str(raw))
            candidates.append(path if path.is_absolute() else engine_root / path)
    lod_paths = asset.metadata.get("available_lod_cas_paths")
    if isinstance(lod_paths, dict):
        for raw in lod_paths.values():
            path = Path(str(raw))
            candidates.append(path if path.is_absolute() else engine_root / path)
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(selected)
            return True
        except ValueError:
            continue
    return False


def display_name(asset: AssetRecord) -> str:
    stem = asset_stem(asset)
    useful = [t for t in tokens(stem) if t not in CHARACTER_TERMS]
    return " ".join(sorted(useful)) or stem


def category_for_asset(asset: AssetRecord) -> str:
    """Return a capability category for the UI's generic asset filter."""
    if asset.kind == "animation":
        return "Animations"
    if asset.kind == "skeleton":
        return "Skeletons"
    if asset.kind == "texture":
        return "Images" if asset.extension in IMAGE_EXTENSIONS else "Textures"
    if asset.kind == "other":
        # A reference string that didn't match any known naming pattern --
        # e.g. an Anthem texture .res file, since (like meshes, animations
        # and skeletons before it) Anthem doesn't name texture files
        # helpfully. Previously these were silently dropped entirely rather
        # than showing up anywhere; routing them here instead of the mesh
        # fallback keeps them out of "Characters / meshes" while still
        # making them findable via this category or "All".
        return "Other / unclassified"
    if asset.kind == "container":
        # A generic engine container is an archive, not the semantic asset its
        # filename happens to mention. Its typed children are classified after
        # the matching format adapter has opened it. Rockstar IMG/RPF archive
        # names remain useful because that adapter exposes their entries.
        if asset.engine != "Rockstar RAGE (GTA IV)":
            return "Containers / archives"
        name = asset.relative_path.lower()
        if "pedprops" in name or any(term in name for term in PROP_TERMS):
            return "Props"
        if any(term in name for term in WEAPON_TERMS):
            return "Weapons & carryables"
        if any(term in name for term in MAP_TERMS) or any(term in name for term in MAP_ARCHIVE_HINTS):
            return "Map parts"
        return "Characters / meshes"
    if asset.kind != "mesh":
        return "Characters / meshes"
    # WFT means RAGE fragment/model, not "skeleton".  A fragment may reference
    # a real rig, but it only belongs in Skeletons after an adapter has decoded
    # an actual parent/child hierarchy.
    terms = tokens(asset.relative_path)
    if terms & WEAPON_TERMS:
        return "Weapons & carryables"
    if terms & MAP_TERMS:
        return "Map parts"
    if any(term in asset.relative_path.lower() for term in ("\\maps\\", "/maps/", "\\world\\", "/world/", "\\levels\\", "/levels/")):
        return "Map parts"
    if terms & PROP_TERMS:
        return "Props"
    if asset.extension in IMAGE_EXTENSIONS:
        return "Images"
    if asset.extension in TEXTURE_EXTENSIONS:
        return "Textures"
    return "Characters / meshes"


def relationship(mesh: AssetRecord, other: AssetRecord) -> float:
    if mesh.path == other.path and mesh.internal_path == other.internal_path:
        return -1
    mesh_tokens = tokens(asset_stem(mesh))
    other_tokens = tokens(asset_stem(other))
    shared = mesh_tokens & other_tokens
    same_dir = asset_parent(mesh) == asset_parent(other)
    if mesh.internal_path or other.internal_path:
        relative_depth = 1 if same_dir else 99
    else:
        try:
            relative_depth = len(other.path.relative_to(mesh.path.parent).parts)
        except ValueError:
            relative_depth = 99
    score = len(shared) * 4.0
    if same_dir:
        score += 3.0
    elif relative_depth <= 2:
        score += 1.5
    if other.kind in {"skeleton", "animation"} and same_dir:
        score += 2.0
    return score


def build_bundles(assets: list[AssetRecord]) -> list[CharacterBundle]:
    meshes = [asset for asset in assets if asset.kind == "mesh"]
    supporting = [asset for asset in assets if asset.kind in {"skeleton", "animation", "material", "texture"}]
    bundles: list[CharacterBundle] = []

    # Candidate indexes keep grouping close to linear on games with tens of
    # thousands of textures instead of comparing every mesh with every asset.
    by_directory: dict[Path, list[int]] = {}
    by_token: dict[str, list[int]] = {}
    for index, item in enumerate(supporting):
        by_directory.setdefault(asset_parent(item), []).append(index)
        for token in tokens(asset_stem(item)):
            by_token.setdefault(token, []).append(index)

    for mesh in meshes:
        mesh_tokens = tokens(mesh.relative_path)
        candidate_indexes = set(by_directory.get(asset_parent(mesh), ()))
        for token in tokens(asset_stem(mesh)):
            candidate_indexes.update(by_token.get(token, ()))
        related_scores = [(relationship(mesh, supporting[index]), supporting[index]) for index in candidate_indexes]
        related_scores.sort(key=lambda pair: (-pair[0], pair[1].relative_path.lower()))
        related = [item for score, item in related_scores if score >= 4.0][:80]

        score = 10.0 if mesh.directly_viewable else 1.0
        reasons: list[str] = []
        character_hits = mesh_tokens & CHARACTER_TERMS
        accessory_hits = mesh_tokens & ACCESSORY_TERMS
        if character_hits:
            score += 18.0 + len(character_hits) * 2
            reasons.append("character-like name/path")
        if accessory_hits and not character_hits:
            score -= 10.0
            reasons.append("looks like a prop/weapon/environment asset")
        if mesh.size >= 50_000:
            score += 4.0
        if any(item.kind == "skeleton" for item in related):
            score += 16.0
            reasons.append("matching skeleton/rig nearby")
        if any(item.kind == "animation" for item in related):
            score += 8.0
            reasons.append("matching animation nearby")
        if any(item.kind == "material" for item in related):
            score += 3.0
        if any(item.kind == "texture" for item in related):
            score += 3.0
        if mesh.extension in {".fbx", ".gltf", ".glb", ".blend"}:
            score += 5.0
            reasons.append("format may embed rig/animation data")
        if mesh.engine == "Rockstar RAGE (GTA IV)" and mesh.extension in {".wdd", ".wdr", ".rsc"}:
            score += 12.0
            reasons.append("GTA IV resource found inside a character archive")
        if mesh.engine == "Frostbite" and mesh.extension == ".meshset":
            score += 12.0
            reasons.append("decoded Frostbite MeshSet resource")
        if mesh.metadata.get("skinned") is True:
            score += 20.0
            reasons.append("mesh is referenced by a skinned renderer")
        bundles.append(CharacterBundle(display_name(mesh), mesh, related, score, reasons))

    bundles.sort(key=lambda b: (-b.score, b.mesh.relative_path.lower()))
    return bundles
