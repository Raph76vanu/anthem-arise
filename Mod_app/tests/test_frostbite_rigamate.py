import struct
import unittest

from game_asset_explorer.frostbite_animation import (
    EclipseAnimationClip, FloatChannel, QuaternionChannel, VectorChannel,
)
from game_asset_explorer.frostbite_rigamate import (
    inspect_rigamate_dof_types, decode_rigamate_bone_mapping,
    stabilize_component_swaps,
)
from game_asset_explorer.geometry import SkeletonData


def block(type_hash, length):
    data = bytearray(length)
    data[:8] = b"GD.DATAl"
    struct.pack_into("<I", data, 8, length - 8)
    struct.pack_into("<I", data, 32, type_hash)
    return data


class RigamateTypeTests(unittest.TestCase):
    def test_only_clear_component_swaps_are_repaired(self):
        before = (-.234, -.176, .956, 0.0)
        swapped = (-.234, .956, -.175, 0.0)
        different = (.9, .1, .1, .4)
        result = stabilize_component_swaps((before, swapped, different))
        self.assertAlmostEqual(result[1][1], -.175)
        self.assertAlmostEqual(result[1][2], .956)
        self.assertEqual(result[2], different)

    def test_dof_order_is_rotations_positions_then_scalars(self):
        names = block(0x14A6DA9B, 220)
        names[100:113] = b"EXM_skeleton\0"
        struct.pack_into("<II", names, 64, 1, 1)
        struct.pack_into("<IIQ", names, 120, 10, 10, 184)
        names[200:210] = b"Reference\0"
        dofs = block(0xB1240F5A, 352)
        dofs[208:221] = b"EXM_skeleton\0"
        struct.pack_into("<IIQ", dofs, 144, 2, 2, 256)
        struct.pack_into("<IIQ", dofs, 160, 1, 1, 320)
        struct.pack_into("<I", dofs, 256, 11)
        struct.pack_into("<I", dofs, 288, 12)
        struct.pack_into("<I", dofs, 320, 21)
        resource = bytes(names + dofs)
        clip = EclipseAnimationClip("test", "id", 0, (FloatChannel((), ()),),
                                    (VectorChannel((), ()),),
                                    (QuaternionChannel((), ()), QuaternionChannel((), ())),
                                    (11, 12, 21, 31))
        result = inspect_rigamate_dof_types(resource, clip)
        self.assertIsNotNone(result)
        self.assertEqual(result.quaternion_channel_ids, (11, 12))
        self.assertEqual(result.vector_channel_ids, (21,))
        self.assertEqual(result.float_channel_ids, (31,))
        wrong = EclipseAnimationClip(clip.name, clip.asset_id, 0, clip.float_channels,
                                     clip.vector_channels, clip.quaternion_channels,
                                     (31, 21, 11, 12))
        self.assertIsNone(inspect_rigamate_dof_types(resource, wrong))

    def test_named_dof_sets_join_clip_ids_to_bones_and_reject_wrong_groups(self):
        rig = block(0xB1240F5A, 608)
        rig[208:221] = b"EXM_skeleton\0"
        struct.pack_into("<IIQ", rig, 48, 2, 2, 0x1F0)  # ids at 0x200
        struct.pack_into("<IIQ", rig, 64, 1, 1, 0x220)  # indices at 0x230
        struct.pack_into("<IIQ", rig, 128, 1, 1, 0x210)  # set key at 0x220
        struct.pack_into("<IIQ", rig, 144, 2, 2, 0x120)
        for i, identifier in enumerate((11, 12)):
            struct.pack_into("<I", rig, 0x120 + i * 32, identifier)
            struct.pack_into("<4f", rig, 0x120 + i * 32 + 16, 0, 0, 0, 1)
        struct.pack_into("<2I", rig, 0x200, 11, 12)
        struct.pack_into("<Q", rig, 0x220, 99)
        struct.pack_into("<I", rig, 0x230, 0)
        group = block(0xBB267D6A, 208)
        struct.pack_into("<II", group, 64, 2, 2)
        struct.pack_into("<Q", group, 88, 99)
        group[101:105] = b"Arm\0"
        struct.pack_into("<IIQ", group, 112, 6, 6, 144)
        struct.pack_into("<IIQ", group, 136, 8, 8, 150)
        group[160:166] = b"Arm.q\0"
        group[166:174] = b"Spine.q\0"
        skeleton = SkeletonData("example", [("Spine", -1, 0, 0, 0), ("Arm", 0, 1, 0, 0)])
        clip = EclipseAnimationClip("example", "id", 1, (), (), (
            QuaternionChannel((0, 1), ((0, 0, 0, 1), (0, 0, 0, 1))),
            QuaternionChannel((0, 1), ((0, 0, 0, 1), (0, 0, 0, 1))),
        ), (11, 12))
        result = decode_rigamate_bone_mapping(bytes(rig + group), clip, skeleton)
        self.assertIsNotNone(result)
        self.assertEqual(result.bone_indices, (1, 0))
        self.assertEqual(len(result.pose_channels(clip)), 2)
        struct.pack_into("<Q", group, 88, 100)
        self.assertIsNone(decode_rigamate_bone_mapping(bytes(rig + group), clip, skeleton))


if __name__ == "__main__":
    unittest.main()
