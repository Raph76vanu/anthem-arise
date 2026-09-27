from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .engines import engine_for
from .grouping import build_bundles
from .models import AssetKind, AssetRecord, ScanResult

EXTENSIONS: dict[AssetKind, frozenset[str]] = {
    "mesh": frozenset({".fbx", ".obj", ".dae", ".gltf", ".glb", ".blend", ".abc", ".ply", ".stl", ".3ds", ".x", ".mesh", ".meshset", ".wdd", ".wdr", ".wft"}),
    "skeleton": frozenset({".skel", ".skeleton", ".rig", ".armature"}),
    "animation": frozenset({".anim", ".animation", ".anm", ".ifp", ".hkx", ".hka", ".xanim", ".ycd", ".wad"}),
    "material": frozenset({".mat", ".material", ".mtl", ".shader"}),
    "texture": frozenset({".png", ".jpg", ".jpeg", ".tga", ".dds", ".kt", ".ktx", ".ktx2", ".exr", ".bmp", ".tif", ".tiff", ".wtd"}),
    # Unity .resS files are byte streams referenced by SerializedFiles, not
    # independently inspectable archives, so they are intentionally omitted.
    "container": frozenset({".cas", ".cat", ".toc", ".sb", ".pak", ".utoc", ".ucas", ".uasset", ".uexp", ".assets", ".bundle", ".unity3d", ".vpk", ".rpf"}),
}

VIEWABLE = frozenset({".fbx", ".obj", ".dae", ".gltf", ".glb", ".blend", ".abc", ".ply", ".stl", ".meshset"})
IGNORED_DIRS = frozenset({"__pycache__", ".git", ".svn", "$recycle.bin", "system volume information"})


def classify(path: Path) -> AssetKind:
    suffix = path.suffix.lower()
    name = path.name.lower()
    if name == "globalgamemanagers" or name == "layout.toc":
        return "container"
    # GTA IV .tune files are object tuning/configuration sidecars. Names such
    # as parking_meter or even *_skel_* do not make them skeleton resources.
    if suffix == ".tune":
        return "unknown"
    # Embedded FBX/glTF data can contain mesh, skeleton and animation; mesh is the seed.
    for kind in ("container", "mesh", "skeleton", "animation", "material", "texture"):
        if suffix in EXTENSIONS[kind]:
            return kind
    # Some engines use opaque extensions but retain useful names. Surface
    # these conservatively so a category filter does not look empty.
    path_text = path.as_posix().lower()
    if any(token in path_text for token in ("animation", "anims", "anim_", "_anim")):
        return "animation"
    if any(token in path_text for token in ("skeleton", "skel_", "_skel", "rigs")):
        return "skeleton"
    return "unknown"


def _opaque_container(path: Path) -> tuple[AssetKind, str | None]:
    """Recognize extensionless Unity bundles by their documented signatures."""
    if path.suffix or not any(part.lower() in {"streamingassets", "bundles", "cache"} for part in path.parts):
        return "unknown", None
    try:
        with path.open("rb") as stream:
            signature = stream.read(16)
    except OSError:
        return "unknown", None
    if signature.startswith((b"UnityFS\0", b"UnityWeb\0", b"UnityRaw\0")):
        return "container", "Unity"
    return "unknown", None


def scan(
    root: Path,
    *,
    max_files: int = 500_000,
    include_hidden: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> ScanResult:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    assets: list[AssetRecord] = []
    engine_paths: list[Path] = []
    warnings: list[str] = []
    files_seen = 0

    def on_error(error: OSError) -> None:
        warnings.append(str(error))

    for current, dirs, files in os.walk(root, onerror=on_error, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs
            if d.lower() not in IGNORED_DIRS and (include_hidden or not d.startswith("."))
        )
        for filename in sorted(files):
            if not include_hidden and filename.startswith("."):
                continue
            files_seen += 1
            if files_seen > max_files:
                warnings.append(f"Stopped at the --max-files limit ({max_files:,}).")
                break
            path = Path(current) / filename
            engine = engine_for(path)
            if engine == "Rockstar RAGE (GTA IV)" and path.suffix.lower() == ".img":
                kind = "container"
            else:
                # Magic wins over path hints. A hashed Unity bundle may live in
                # a folder named "exoskeletons"; that does not make the opaque
                # container itself a skeleton asset.
                magic_kind, magic_engine = _opaque_container(path)
                kind = magic_kind if magic_kind != "unknown" else classify(path)
                engine = engine or magic_engine
            if engine:
                engine_paths.append(path)
            if kind != "unknown":
                try:
                    size = path.stat().st_size
                except OSError as error:
                    warnings.append(str(error))
                    continue
                assets.append(AssetRecord(
                    path=path,
                    relative_path=str(path.relative_to(root)),
                    kind=kind,
                    extension=path.suffix.lower(),
                    size=size,
                    engine=engine,
                    directly_viewable=path.suffix.lower() in VIEWABLE,
                ))
            if progress and files_seen % 1000 == 0:
                progress(files_seen, str(path))
        if files_seen > max_files:
            break

    from .engines import detect_engines, parent_engine_root
    engine_hints = detect_engines(engine_paths)
    for detected in sorted({asset.engine for asset in assets if asset.engine}):
        if detected not in engine_hints:
            engine_hints.append(detected)
    parent_engine, engine_root = parent_engine_root(root)
    if parent_engine and parent_engine not in engine_hints:
        engine_hints.append(parent_engine)
    if engine_root and engine_root != root:
        warnings.append(
            f"Searching only {root}, while using parent {parent_engine} layout data from {engine_root}."
        )
    bundles = build_bundles(assets)
    from .rage import known_archive_bundles
    bundles.extend(known_archive_bundles(assets, bundles))
    bundles.sort(key=lambda bundle: (-bundle.score, bundle.mesh.relative_path.lower()))
    return ScanResult(root, engine_hints, assets, bundles, warnings, files_seen, engine_root)
