from __future__ import annotations

from pathlib import Path
import unittest

from game_asset_explorer.gui import App
from game_asset_explorer.models import AssetRecord


class AnimationStateTests(unittest.TestCase):
    def test_javelin_family_is_shared_by_mesh_and_animation_paths(self) -> None:
        mesh = AssetRecord(
            Path("forttarsis.sb"), "mesh", "mesh", ".meshset", 1,
            internal_path="exo/exh_colossus/exh_colossus_base_model_mesh.meshset",
        )
        animation = AssetRecord(
            Path("forttarsis.sb"), "animation", "animation", ".ebx", 1,
            internal_path="animation/exh/exh_locomotion.ebx",
        )

        self.assertEqual(App._javelin_family(mesh), "exh")
        self.assertEqual(App._javelin_family(animation), "exh")


if __name__ == "__main__":
    unittest.main()
