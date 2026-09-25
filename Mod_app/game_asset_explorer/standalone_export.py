"""Write a decoded Interceptor prototype as a self-contained runtime folder."""
from __future__ import annotations

import gzip
import pickle
import shutil
from pathlib import Path


BUNDLE_FORMAT = 1
BUNDLE_FILENAME = "interceptor_prototype.gae"


def save_bundle(path: Path, payload: dict[str, object]) -> None:
    """Store already-decoded meshes and animation curves in a compact cache."""
    envelope = {"format": BUNDLE_FORMAT, "payload": payload}
    with gzip.open(path, "wb", compresslevel=6) as stream:
        pickle.dump(envelope, stream, protocol=pickle.HIGHEST_PROTOCOL)


def load_bundle(path: Path) -> dict[str, object]:
    """Load a cache created locally by :func:`save_bundle`."""
    with gzip.open(path, "rb") as stream:
        envelope = pickle.load(stream)
    if not isinstance(envelope, dict) or envelope.get("format") != BUNDLE_FORMAT:
        raise ValueError("This standalone prototype cache uses an unsupported format.")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("The standalone prototype cache is incomplete.")
    return payload


def export_standalone_folder(
    destination: Path, payload: dict[str, object], package_source: Path,
) -> Path:
    """Create a launchable folder that never reads the Anthem installation."""
    destination.mkdir(parents=True, exist_ok=False)
    save_bundle(destination / BUNDLE_FILENAME, payload)
    shutil.copytree(
        package_source,
        destination / "game_asset_explorer",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    (destination / "run_prototype.py").write_text(
        "from game_asset_explorer.standalone_runtime import main\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    (destination / "Play Interceptor Prototype.bat").write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        "where py >nul 2>nul && (py -3 run_prototype.py & goto :done)\r\n"
        "where python >nul 2>nul && (python run_prototype.py & goto :done)\r\n"
        "echo Python 3 was not found. Install Python 3.11 or newer, then try again.\r\n"
        "pause\r\n"
        ":done\r\n",
        encoding="utf-8",
    )
    (destination / "README.txt").write_text(
        "INTERCEPTOR STANDALONE PROTOTYPE\n\n"
        "Double-click 'Play Interceptor Prototype.bat'.\n\n"
        "Controls:\n"
        "  Mouse drag orbit camera\n"
        "  Mouse wheel zoom camera\n"
        "  WASD       move\n"
        "  Shift      sprint / fast glide\n"
        "  Space      jump / rise\n"
        "  F          toggle flight\n"
        "  Ctrl       descend\n"
        "  Q          air dash\n"
        "  Numpad 0-5 force a global LOD (when that LOD was available)\n"
        "  Escape     quit\n\n"
        "This folder contains baked decoded data. It does not scan or read the Anthem game folder.\n"
        "Python 3.11+ and Pillow are required by this first portable build.\n",
        encoding="utf-8",
    )
    return destination
