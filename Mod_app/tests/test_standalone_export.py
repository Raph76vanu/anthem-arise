import tempfile
import unittest
from pathlib import Path

from game_asset_explorer.geometry import MeshData, SkeletonData
from game_asset_explorer.standalone_export import load_bundle, save_bundle


class StandaloneExportTests(unittest.TestCase):
    def test_decoded_bundle_round_trips_without_game_files(self):
        payload = {
            "lod_meshes": {
                5: [MeshData("low", [(0.0, 0.0, 0.0)], [], None, None)],
                0: [MeshData("high", [(1.0, 2.0, 3.0)], [], None, None)],
            },
            "skeleton": SkeletonData("rig", [("Root", -1, 0.0, 0.0, 0.0)]),
            "bind_rotations": [(0.0, 0.0, 0.0, 1.0)],
            "animations": {"idle": {"name": "EXF_Idle"}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prototype.gae"
            save_bundle(path, payload)
            decoded = load_bundle(path)
        self.assertEqual(sorted(decoded["lod_meshes"]), [0, 5])
        self.assertEqual(decoded["lod_meshes"][0][0].vertices[0], (1.0, 2.0, 3.0))
        self.assertEqual(decoded["skeleton"].joints[0][0], "Root")

    def test_rejects_unknown_bundle_format(self):
        import gzip
        import pickle
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prototype.gae"
            with gzip.open(path, "wb") as stream:
                pickle.dump({"format": 999, "payload": {}}, stream)
            with self.assertRaisesRegex(ValueError, "unsupported format"):
                load_bundle(path)


if __name__ == "__main__":
    unittest.main()
