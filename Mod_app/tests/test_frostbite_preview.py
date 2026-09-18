from pathlib import Path
import unittest
from unittest.mock import patch

from game_asset_explorer.frostbite import preview_frostbite_meshset_isolated
from game_asset_explorer.models import AssetRecord


class FrostbitePreviewTests(unittest.TestCase):
    def test_requested_lod_is_forwarded_and_available_lods_are_returned(self) -> None:
        asset = AssetRecord(
            path=Path("Data/default.sb"), relative_path="Data/default.sb::javelin.meshset",
            kind="mesh", extension=".meshset", size=100, engine="Frostbite",
            internal_path="exo/javelin.meshset",
        )
        response = {
            "kind": "mesh", "lod": 1, "name": "javelin",
            "available_lods": [5, 3, 1, 0],
            "rig_diagnostic": {
                "summary": "Skin weights decoded (1/1 candidate sections).",
                "likely_skinned": True,
                "weights_decoded": True,
                "sections": [],
            },
            "meshes": [{
                "name": "body",
                "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                "faces": [[0, 1, 2]],
                "skin_bones": [[11, 0, 0, 0], [12, 11, 0, 0], [13, 0, 0, 0]],
                "skin_weights": [[1, 0, 0, 0], [0.5, 0.5, 0, 0], [1, 0, 0, 0]],
            }],
        }

        with patch("game_asset_explorer.frostbite._worker", return_value=response) as worker:
            result = preview_frostbite_meshset_isolated(
                Path("Anthem"), asset, lod_index=1,
            )

        worker.assert_called_once_with("preview", Path("Anthem"), asset, 120, 1)
        self.assertEqual(result[0], "mesh")
        self.assertEqual(result[2], 1)
        self.assertEqual(result[4], [5, 3, 1, 0])
        self.assertTrue(result[5]["weights_decoded"])
        self.assertIn("Skin weights decoded", result[5]["summary"])
        self.assertEqual(result[1][0].skin_bones[1], (12, 11, 0, 0))
        self.assertEqual(result[1][0].skin_weights[1], (0.5, 0.5, 0, 0))


if __name__ == "__main__":
    unittest.main()
