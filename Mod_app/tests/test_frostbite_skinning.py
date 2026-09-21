import unittest

from game_asset_explorer.frostbite_animation_playback import evaluate_pose_transforms
from game_asset_explorer.frostbite_skinning import skin_meshes
from game_asset_explorer.geometry import MeshData, SkeletonData


class SkinningTests(unittest.TestCase):
    def test_rotates_a_weighted_vertex_about_bone_and_preserves_unweighted_vertex(self):
        skeleton = SkeletonData("rig", [("root", -1, 0.0, 0.0, 0.0)])
        mesh = MeshData("part", [(1.0, 0.0, 0.0), (2.0, 0.0, 0.0)], [],
                        [(0, 0, 0, 0)] * 2,
                        [(1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)])
        identity = ((0.0, 0.0, 0.0, 1.0),)
        half_turn = ((0.0, 0.0, 1.0, 0.0),)
        result = skin_meshes([mesh], skeleton, skeleton, identity, half_turn)[0]
        self.assertAlmostEqual(result.vertices[0][0], -1.0)
        self.assertAlmostEqual(result.vertices[0][1], 0.0)
        self.assertEqual(result.vertices[1], mesh.vertices[1])
        self.assertEqual(mesh.vertices[0], (1.0, 0.0, 0.0))

    def test_rest_pose_does_not_distort_weighted_mesh(self):
        skeleton = SkeletonData("rig", [("root", -1, 0.0, 0.0, 0.0)])
        identity = [(0.0, 0.0, 0.0, 1.0)]
        pose, rotations = evaluate_pose_transforms(skeleton, identity, [], [], 0.0)
        mesh = MeshData("part", [(1.0, 2.0, 3.0)], [], [(0, 0, 0, 0)], [(1, 0, 0, 0)])
        result = skin_meshes([mesh], skeleton, pose, rotations, rotations)[0]
        self.assertEqual(result.vertices, mesh.vertices)


if __name__ == "__main__":
    unittest.main()
