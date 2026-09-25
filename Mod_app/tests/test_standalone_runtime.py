import math
import unittest
from types import SimpleNamespace

from game_asset_explorer.geometry import MeshData, SkeletonData
from game_asset_explorer.standalone_runtime import (
    StandalonePrototype,
    quaternion_yaw,
    rotate_character_y,
)


class StandaloneHeadingTests(unittest.TestCase):
    def test_quaternion_yaw_extracts_vertical_heading(self):
        half = math.pi / 4
        self.assertAlmostEqual(
            quaternion_yaw((0.0, math.sin(half), 0.0, math.cos(half))),
            math.pi / 2,
        )

    def test_controller_heading_rotates_mesh_and_pose_together(self):
        mesh = MeshData("body", [(0.0, 0.0, 1.0)], [])
        skeleton = SkeletonData("rig", [("Hips", -1, 0.0, 0.0, 1.0)])
        meshes, pose = rotate_character_y([mesh], skeleton, math.pi / 2)
        self.assertAlmostEqual(meshes[0].vertices[0][0], 1.0)
        self.assertAlmostEqual(meshes[0].vertices[0][2], 0.0, places=7)
        self.assertAlmostEqual(pose.joints[0][2], 1.0)
        self.assertAlmostEqual(pose.joints[0][4], 0.0, places=7)

    def test_standalone_accepts_sprint_and_descent_modifiers(self):
        self.assertEqual(
            StandalonePrototype._key_name(SimpleNamespace(keysym="Shift_L")), "shift",
        )
        self.assertEqual(
            StandalonePrototype._key_name(SimpleNamespace(keysym="Control_R")), "control",
        )


if __name__ == "__main__":
    unittest.main()
