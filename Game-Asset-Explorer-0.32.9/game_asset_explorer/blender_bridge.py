from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .models import CharacterBundle


def find_blender(explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    found = shutil.which("blender")
    if found:
        return Path(found)
    if os.name == "nt":
        base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"
        candidates = sorted(base.glob("Blender */blender.exe"), reverse=True) if base.exists() else []
        return candidates[0] if candidates else None
    for candidate in (Path("/Applications/Blender.app/Contents/MacOS/Blender"), Path("/snap/bin/blender")):
        if candidate.is_file():
            return candidate
    return None


def launch_blender(bundle: CharacterBundle, blender: str | None = None, dry_run: bool = False) -> list[str]:
    executable = find_blender(blender)
    if not executable:
        raise FileNotFoundError("Blender was not found. Install it or pass --blender PATH.")

    importable = [asset for asset in bundle.all_assets if asset.extension in {
        ".fbx", ".obj", ".dae", ".gltf", ".glb", ".blend", ".abc", ".ply", ".stl"
    }]
    if not importable:
        raise ValueError("This bundle has no loose format Blender can import; it needs an engine-specific extractor first.")

    package_dir = Path(__file__).resolve().parent
    script = package_dir / "blender_preview.py"
    manifest_path = Path(tempfile.gettempdir()) / f"game_asset_explorer_{os.getpid()}.json"
    manifest_path.write_text(json.dumps({
        "name": bundle.name,
        "files": [str(asset.path) for asset in importable],
    }, indent=2), encoding="utf-8")
    command = [str(executable), "--python", str(script), "--", "--manifest", str(manifest_path)]
    if not dry_run:
        subprocess.Popen(command, close_fds=os.name != "nt")
    return command

