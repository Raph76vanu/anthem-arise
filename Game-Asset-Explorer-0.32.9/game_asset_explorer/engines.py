from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EngineSignature:
    name: str
    extensions: frozenset[str]
    marker_names: frozenset[str] = frozenset()


SIGNATURES = (
    EngineSignature(
        "Rockstar RAGE (GTA IV)",
        frozenset({".rpf", ".wdd", ".wdr", ".wft", ".wtd", ".wad"}),
        frozenset({"gtaiv.exe", "launchgtaiv.exe", "componentpeds.img", "pedprops.img"}),
    ),
    EngineSignature("Frostbite", frozenset({".cas", ".cat", ".toc", ".sb", ".meshset"}), frozenset({"layout.toc"})),
    EngineSignature("Unreal Engine", frozenset({".pak", ".utoc", ".ucas", ".uasset", ".uexp"})),
    EngineSignature("Unity", frozenset({".assets", ".bundle", ".unity3d", ".ress"}), frozenset({"globalgamemanagers"})),
    EngineSignature("Source", frozenset({".vpk", ".mdl", ".vvd", ".vtx"})),
)


def engine_for(path: Path) -> str | None:
    suffix = path.suffix.lower()
    name = path.name.lower()
    for signature in SIGNATURES:
        if suffix in signature.extensions or name in signature.marker_names:
            return signature.name
    # GTA IV gives RPF3 archives an .img extension. Do not classify every .img
    # on earth as RAGE; use the characteristic archive names/directories.
    parts = {part.lower() for part in path.parts}
    if suffix == ".img" and (
        name in {"componentpeds.img", "pedprops.img", "vehicles.img", "gtaiv.img"}
        or "cdimages" in parts
        or ("pc" in parts and "maps" in parts)
    ):
        return "Rockstar RAGE (GTA IV)"
    return None


def detect_engines(paths: list[Path]) -> list[str]:
    counts: dict[str, int] = {}
    for path in paths:
        engine = engine_for(path)
        if engine:
            counts[engine] = counts.get(engine, 0) + 1
    return [name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def parent_engine_root(selected: Path) -> tuple[str | None, Path | None]:
    """Find an engine layout above a deliberately narrow scan folder.

    The selected directory remains the result scope.  This ancestor is used
    only by format readers that require shared metadata or payload locations.
    """
    selected = selected.resolve()
    for current in (selected, *selected.parents):
        # A nested selection may be below Data or Patch. Return their common
        # parent so the reader sees both layers with the correct precedence.
        if current.name.casefold() in {"data", "patch"}:
            parent = current.parent
            if (parent / "Data" / "layout.toc").is_file():
                return "Frostbite", parent
        if (current / "Data" / "layout.toc").is_file():
            return "Frostbite", current
        if (current / "Anthem" / "Data" / "layout.toc").is_file():
            return "Frostbite", current
        if (current / "layout.toc").is_file() and current.name.casefold() != "patch":
            return "Frostbite", current
    return None, None
