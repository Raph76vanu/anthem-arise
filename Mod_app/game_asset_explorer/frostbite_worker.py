"""Crash-contained Frostbite metadata and CAS decoding worker."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from .frostbite_index import (
    extract_frostbite_ebx_dependencies, extract_frostbite_record,
    inspect_frostbite_meshsets, preview_frostbite_meshset,
)
from .geometry import MeshFormatError
from .models import AssetRecord


def _asset(data: dict[str, object]) -> AssetRecord:
    container = data.get("container_path")
    return AssetRecord(
        path=Path(str(data["path"])),
        relative_path=str(data["relative_path"]),
        kind=str(data["kind"]),  # type: ignore[arg-type]
        extension=str(data["extension"]),
        size=int(data["size"]),
        engine=str(data["engine"]) if data.get("engine") else None,
        directly_viewable=bool(data.get("directly_viewable")),
        container_path=Path(str(container)) if container else None,
        internal_path=str(data["internal_path"]) if data.get("internal_path") else None,
        metadata=dict(data.get("metadata") or {}),
    )


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        root = Path(str(request["root"]))
        action = request.get("action")
        if action == "inspect":
            assets, warnings = inspect_frostbite_meshsets(root)
            response = {"assets": [asset.to_dict() for asset in assets], "warnings": warnings}
        elif action == "preview":
            asset = _asset(dict(request["asset"]))
            requested_lod = request.get("lod")
            lod_index = int(requested_lod) if requested_lod is not None else None
            try:
                meshes, lod, name, available_lods, rig_diagnostic = preview_frostbite_meshset(
                    root, asset, lod_index,
                )
                response = {
                    "kind": "mesh", "lod": lod, "name": name,
                    "available_lods": available_lods,
                    "rig_diagnostic": rig_diagnostic,
                    "meshes": [
                        {
                            "name": mesh.name,
                            "vertices": mesh.vertices,
                            "faces": mesh.faces,
                            "skin_bones": mesh.skin_bones,
                            "skin_weights": mesh.skin_weights,
                        }
                        for mesh in meshes
                    ],
                }
            except MeshFormatError as error:
                message = str(error)
                if message.startswith("No previewable MeshSet LOD could be decoded."):
                    response = {"kind": "unavailable", "reason": message}
                else:
                    raise
        elif action == "extract_record":
            asset = _asset(dict(request["asset"]))
            raw = extract_frostbite_record(root, asset)
            response = {
                "kind": "record",
                "data": base64.b64encode(raw).decode("ascii"),
                "size": len(raw),
            }
        elif action == "extract_record_dependencies":
            asset = _asset(dict(request["asset"]))
            raw, dependencies = extract_frostbite_ebx_dependencies(root, asset)
            response = {
                "kind": "record_dependencies",
                "data": base64.b64encode(raw).decode("ascii"),
                "size": len(raw),
                "dependencies": [
                    {
                        "asset": linked.to_dict(),
                        "data": base64.b64encode(linked_raw).decode("ascii"),
                        "size": len(linked_raw),
                    }
                    for linked, linked_raw in dependencies
                ],
            }
        else:
            raise ValueError("Unknown Frostbite worker action.")
    except Exception as error:
        response = {"error": f"{type(error).__name__}: {error}"}
    sys.stdout.write(json.dumps(response, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
