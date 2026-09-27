"""Crash-contained UnityPy worker.

The desktop GUI invokes this module in a separate process. Unity bundles are
untrusted binary input; a decoder crash, timeout, or extreme allocation must
not take the Tk process down with it.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
from pathlib import Path

from .models import AssetRecord


def _asset(data: dict[str, object]) -> AssetRecord:
    container_path = data.get("container_path")
    return AssetRecord(
        path=Path(str(data["path"])), relative_path=str(data["relative_path"]),
        kind=str(data["kind"]), extension=str(data["extension"]),  # type: ignore[arg-type]
        size=int(data["size"]), engine=str(data["engine"]) if data.get("engine") else None,
        directly_viewable=bool(data.get("directly_viewable")),
        container_path=Path(str(container_path)) if container_path else None,
        internal_path=str(data["internal_path"]) if data.get("internal_path") else None,
        metadata=dict(data.get("metadata") or {}),
    )


def main() -> int:
    try:
        request = json.load(sys.stdin)
        asset = _asset(request["asset"])
        if request["action"] == "inspect":
            from .unity import inspect_unity_container
            # Third-party decoders occasionally print diagnostics to stdout.
            # stdout is our JSON protocol, so keep those messages on stderr.
            with contextlib.redirect_stdout(sys.stderr):
                found, warnings = inspect_unity_container(asset.path.parent, asset)
            # A single bundle can contain a huge object catalogue. The typed
            # inventory is useful, but retaining millions of UI rows is not.
            capped = found[:3000]
            if len(found) > len(capped):
                warnings.append(
                    f"{asset.relative_path}: showing the first {len(capped):,} of {len(found):,} supported objects."
                )
            response = {"assets": [item.to_dict() for item in capped], "warnings": warnings}
        elif request["action"] == "preview":
            from .unity import preview_unity_asset
            with contextlib.redirect_stdout(sys.stderr):
                kind, payload = preview_unity_asset(asset)
            if kind == "mesh":
                vertices = sum(len(mesh.vertices) for mesh in payload)
                faces = sum(len(mesh.faces) for mesh in payload)
                if vertices > 750_000 or faces > 1_500_000:
                    raise ValueError(
                        f"Mesh preview exceeds the safety limit ({vertices:,} vertices, {faces:,} faces)."
                    )
                response = {
                    "kind": kind,
                    "meshes": [
                        {"name": mesh.name, "vertices": mesh.vertices, "faces": mesh.faces}
                        for mesh in payload
                    ],
                }
            elif kind == "image":
                output = io.BytesIO()
                payload.save(output, format="PNG")
                response = {"kind": kind, "png": base64.b64encode(output.getvalue()).decode("ascii")}
            elif kind == "skeleton":
                response = {"kind": kind, "name": payload.name, "joints": payload.joints}
            else:
                raise ValueError(f"Unsupported preview type {kind!r}")
        else:
            raise ValueError("Unknown Unity worker action")
        json.dump(response, sys.stdout, separators=(",", ":"))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
