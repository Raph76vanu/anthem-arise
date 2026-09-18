from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

AssetKind = Literal["mesh", "skeleton", "animation", "material", "texture", "container", "unknown"]


@dataclass(frozen=True)
class AssetRecord:
    path: Path
    relative_path: str
    kind: AssetKind
    extension: str
    size: int
    engine: str | None = None
    directly_viewable: bool = False
    container_path: Path | None = None
    internal_path: str | None = None
    # Adapter-owned, JSON-compatible metadata.  Keeping this open lets a
    # container reader retain stable object ids or a real bone-id list without
    # teaching the neutral scanner about each engine's object model.
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["path"] = str(self.path)
        if self.container_path:
            result["container_path"] = str(self.container_path)
        return result


@dataclass
class CharacterBundle:
    name: str
    mesh: AssetRecord
    related: list[AssetRecord] = field(default_factory=list)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def all_assets(self) -> list[AssetRecord]:
        return [self.mesh, *self.related]

    def count(self, kind: AssetKind) -> int:
        return sum(item.kind == kind for item in self.all_assets)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": round(self.score, 2),
            "reasons": self.reasons,
            "mesh": self.mesh.to_dict(),
            "related": [item.to_dict() for item in self.related],
        }


@dataclass
class ScanResult:
    root: Path
    engine_hints: list[str]
    assets: list[AssetRecord]
    bundles: list[CharacterBundle]
    warnings: list[str]
    files_seen: int
    # The user-selected folder remains ``root`` (the visible scan scope).
    # Proprietary readers may need an ancestor installation root for layout,
    # dictionary, or shared-payload resolution.
    engine_root: Path | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "root": str(self.root),
            "engine_root": str(self.engine_root) if self.engine_root else None,
            "engine_hints": self.engine_hints,
            "files_seen": self.files_seen,
            "warnings": self.warnings,
            "summary": {
                "assets": len(self.assets),
                "bundles": len(self.bundles),
                "by_kind": {
                    kind: sum(a.kind == kind for a in self.assets)
                    for kind in ("mesh", "skeleton", "animation", "material", "texture", "container")
                },
            },
            "bundles": [bundle.to_dict() for bundle in self.bundles],
            "containers": [a.to_dict() for a in self.assets if a.kind == "container"],
        }
