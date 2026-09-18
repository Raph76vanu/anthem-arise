"""Capability-based adapter registry and lazy dependency resolution."""
from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from .models import AssetRecord


class AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdapterSpec:
    capability: str
    label: str
    engines: frozenset[str]
    extensions: frozenset[str]
    import_names: tuple[str, ...]
    requirements: tuple[str, ...]

    def matches(self, asset: AssetRecord) -> bool:
        return bool(
            (asset.engine and asset.engine in self.engines)
            or asset.extension in self.extensions
        )


# This registry scales by file capability, not by adding UI buttons for games.
# A future archive reader is one data entry plus its implementation module.
ADAPTERS = (
    AdapterSpec(
        capability="frostbite-metadata-read",
        label="Frostbite metadata inventory",
        engines=frozenset({"Frostbite"}),
        extensions=frozenset({".toc", ".sb", ".cas", ".cat"}),
        import_names=(),
        requirements=(),
    ),
    AdapterSpec(
        capability="image-read",
        label="2D image reader",
        engines=frozenset(),
        extensions=frozenset({".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga", ".dds", ".ktx", ".ktx2", ".exr", ".tif", ".tiff"}),
        import_names=("PIL",),
        requirements=("Pillow>=10",),
    ),
    AdapterSpec(
        capability="rpf3-read",
        label="RPF3 archive reader",
        engines=frozenset({"Rockstar RAGE (GTA IV)"}),
        extensions=frozenset({".rpf", ".img"}),
        import_names=("Crypto",),
        requirements=("pycryptodome>=3.19",),
    ),
    AdapterSpec(
        capability="unity-read",
        label="Unity asset reader",
        engines=frozenset({"Unity"}),
        extensions=frozenset({".assets", ".bundle", ".unity3d"}),
        import_names=("UnityPy",),
        # The implementation uses UnityPy's modern parse_as_object API.  Keep
        # the major version bounded because UnityPy explicitly warns that its
        # API may change between minor releases.
        requirements=("UnityPy>=1.20,<2",),
    ),
)


def adapter_for(asset: AssetRecord, capability: str) -> AdapterSpec | None:
    return next((item for item in ADAPTERS if item.capability == capability and item.matches(asset)), None)


def _imports_available(spec: AdapterSpec) -> bool:
    try:
        for name in spec.import_names:
            importlib.import_module(name)
        return True
    except (ImportError, ModuleNotFoundError):
        return False


def ensure_adapter(
    asset: AssetRecord,
    capability: str,
    progress: Callable[[str], None] | None = None,
) -> AdapterSpec:
    spec = adapter_for(asset, capability)
    if not spec:
        raise AdapterError(f"No {capability} adapter is registered for {asset.extension or asset.engine}.")
    if _imports_available(spec):
        return spec
    if progress:
        progress(f"Loading {spec.label} automatically…")
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "install", *spec.requirements],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout)[-1600:].strip()
        raise AdapterError(f"Could not load {spec.label}: {detail or 'package installer returned an error'}")
    importlib.invalidate_caches()
    if not _imports_available(spec):
        raise AdapterError(f"{spec.label} was installed but could not be imported by this Python process.")
    return spec
