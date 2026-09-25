from __future__ import annotations

import math
import unittest

from game_asset_explorer.frostbite_animation import VectorChannel
from game_asset_explorer.frostbite_animation_channels import QuaternionChannel
from game_asset_explorer.frostbite_animation_playback import (
    axis_angle_to_quaternion, evaluate_pose, evaluate_pose_transforms, guess_bone_mapping,
    compose_transition_loop_channels, prepare_preview_translations,
    quaternion_angle_degrees, quaternion_multiply, quaternion_rotate_vector,
)
from game_asset_explorer.geometry import SkeletonData


def _rotate_point_around_parent(parent_pos, offset, rotation):
    rotated = quaternion_rotate_vector(rotation, offset)
    return (parent_pos[0] + rotated[0], parent_pos[1] + rotated[1], parent_pos[2] + rotated[2])


class BindPoseReconstructionTests(unittest.TestCase):
    """These guard the specific bug found and fixed in this session: with
    zero animation channels, applying the skeleton's own bind rotations
    through the FK hierarchy must reconstruct the EXACT original bind-pose
    positions. It didn't -- a child's world-space bind offset was being
    rotated by the parent's bind rotation a second time (once already
    baked into the offset, once again in the FK loop), producing a 2.28
    unit position error at a leaf joint on real skeleton data, with zero
    animation involved. If this regresses, every rendered pose is wrong
    again, animated or not.
    """

    def test_reconstructs_exact_bind_pose_with_a_nontrivial_root_rotation(self) -> None:
        # A root with a real (non-identity) bind rotation -- this is the
        # case that actually exposed the bug: real skeleton data has a
        # ~120 degree bind rotation at the root (a coordinate-system
        # realignment), and the bug only shows up once a parent's own
        # rotation is non-trivial.
        root_rotation = axis_angle_to_quaternion((0.0, 1.0, 0.0), math.radians(90))
        child_rotation = axis_angle_to_quaternion((1.0, 0.0, 0.0), math.radians(30))

        root_pos = (0.0, 0.0, 0.0)
        child_offset = (0.0, 1.0, 0.0)
        child_pos = _rotate_point_around_parent(root_pos, child_offset, root_rotation)
        grandchild_offset = (0.5, 0.0, 0.0)
        grandchild_world_rotation = quaternion_multiply(root_rotation, child_rotation)
        grandchild_pos = _rotate_point_around_parent(child_pos, grandchild_offset, grandchild_world_rotation)

        skeleton = SkeletonData("test", [
            ("Root", -1, *root_pos),
            ("Child", 0, *child_pos),
            ("Grandchild", 1, *grandchild_pos),
        ])
        bind_rotations = [root_rotation, child_rotation, (0.0, 0.0, 0.0, 1.0)]

        pose = evaluate_pose(skeleton, bind_rotations, [], [], 0.0)

        for original, reconstructed in zip(skeleton.joints, pose.joints):
            for axis in range(3):
                self.assertAlmostEqual(original[2 + axis], reconstructed[2 + axis], places=6)

    def test_reconstructs_exact_bind_pose_on_a_longer_chain(self) -> None:
        # A 5-joint chain with a different rotation at every link, to catch
        # a fix that only happens to work for a single parent/child pair.
        rotations = [
            axis_angle_to_quaternion((0.0, 1.0, 0.0), math.radians(45)),
            axis_angle_to_quaternion((1.0, 0.0, 0.0), math.radians(20)),
            axis_angle_to_quaternion((0.0, 0.0, 1.0), math.radians(-35)),
            axis_angle_to_quaternion((0.7071067811865476, 0.7071067811865476, 0.0), math.radians(60)),
            (0.0, 0.0, 0.0, 1.0),
        ]
        offsets = [(0.0, 0.0, 0.0), (0.0, 0.5, 0.0), (0.1, 0.4, 0.0), (0.0, 0.3, -0.1), (0.2, 0.0, 0.0)]

        positions = [offsets[0]]
        world_rotation = rotations[0]
        for i in range(1, len(rotations)):
            positions.append(_rotate_point_around_parent(positions[i - 1], offsets[i], world_rotation))
            world_rotation = quaternion_multiply(world_rotation, rotations[i])

        joints = [(f"joint{i}", i - 1, *positions[i]) for i in range(len(rotations))]
        skeleton = SkeletonData("test", joints)

        pose = evaluate_pose(skeleton, rotations, [], [], 0.0)

        for original, reconstructed in zip(skeleton.joints, pose.joints):
            for axis in range(3):
                self.assertAlmostEqual(original[2 + axis], reconstructed[2 + axis], places=5)

    def test_identity_bind_rotations_are_a_no_op(self) -> None:
        # Sanity check at the simple end: with no rotation anywhere, FK
        # should trivially reproduce the input positions regardless of the
        # bug above (this passed even before the fix, so it wouldn't have
        # caught the regression on its own -- kept as a baseline).
        skeleton = SkeletonData("test", [
            ("Root", -1, 0.0, 0.0, 0.0),
            ("Child", 0, 0.0, 1.0, 0.0),
        ])
        identity = (0.0, 0.0, 0.0, 1.0)
        pose = evaluate_pose(skeleton, [identity, identity], [], [], 0.0)
        self.assertEqual(pose.joints[1][2:5], (0.0, 1.0, 0.0))

    def test_diagnostic_rotation_rules_differ_without_changing_default_playback(self) -> None:
        from game_asset_explorer.frostbite_animation import QuaternionChannel as EclipseChannel

        bind = axis_angle_to_quaternion((0.0, 0.0, 1.0), math.pi / 2)
        animated = axis_angle_to_quaternion((1.0, 0.0, 0.0), math.pi / 2)
        skeleton = SkeletonData("toy", [
            ("Root", -1, 0.0, 0.0, 0.0),
            ("Elbow", 0, 1.0, 0.0, 0.0),
            ("Hand", 1, 1.0, 1.0, 0.0),
        ])
        rotations = [(0.0, 0.0, 0.0, 1.0), bind, (0.0, 0.0, 0.0, 1.0)]
        channels = [EclipseChannel((0, 1), (animated, animated))]
        default = evaluate_pose_transforms(skeleton, rotations, [1], channels, 0.5)[0]
        before = evaluate_pose_transforms(
            skeleton, rotations, [1], channels, 0.5, rotation_mode="delta_bind",
        )[0]
        self.assertNotEqual(default.joints[2][2:], before.joints[2][2:])
        self.assertEqual(default.joints[2][2:],
                         evaluate_pose_transforms(skeleton, rotations, [1], channels, 0.5)[0].joints[2][2:])

    def test_absolute_mode_replaces_animated_local_bind_rotation(self) -> None:
        from game_asset_explorer.frostbite_animation import QuaternionChannel as EclipseChannel

        bind = axis_angle_to_quaternion((0.0, 0.0, 1.0), math.pi / 2)
        animated = axis_angle_to_quaternion((1.0, 0.0, 0.0), math.pi / 2)
        skeleton = SkeletonData("toy", [
            ("Root", -1, 0.0, 0.0, 0.0),
            ("Elbow", 0, 1.0, 0.0, 0.0),
            ("Hand", 1, 1.0, 1.0, 0.0),
        ])
        rotations = [(0.0, 0.0, 0.0, 1.0), bind, (0.0, 0.0, 0.0, 1.0)]
        channels = [EclipseChannel((0,), (animated,))]
        pose, world = evaluate_pose_transforms(
            skeleton, rotations, [1], channels, 0.0, rotation_mode="absolute",
        )
        self.assertAlmostEqual(world[1][0], animated[0])
        self.assertAlmostEqual(world[1][3], animated[3])
        self.assertAlmostEqual(pose.joints[2][2], 2.0)
        self.assertAlmostEqual(pose.joints[2][3], 0.0, places=6)
        self.assertAlmostEqual(pose.joints[2][4], 0.0, places=6)

    def test_absolute_vector_channel_replaces_joint_local_offset(self) -> None:
        from game_asset_explorer.frostbite_animation import VectorChannel

        skeleton = SkeletonData("toy", [
            ("Root", -1, 0.0, 0.0, 0.0),
            ("Hips", 0, 0.0, 1.0, 0.0),
            ("Knee", 1, 0.0, 2.0, 0.0),
        ])
        identity = [(0.0, 0.0, 0.0, 1.0)] * 3
        translation = VectorChannel((0,), ((0.25, 0.5, -0.25),))
        pose, _ = evaluate_pose_transforms(
            skeleton, identity, [], [], 0.0,
            vector_mapping=[1], vector_channels=[translation],
        )
        self.assertEqual(pose.joints[1][2:], (0.25, 0.5, -0.25))
        self.assertEqual(pose.joints[2][2:], (0.25, 1.5, -0.25))


class PreviewTranslationTests(unittest.TestCase):
    def test_rebases_scene_root_and_hides_controller_plane(self) -> None:
        trajectory = VectorChannel(
            (0, 10), ((0.0, 0.0, 5.855), (1.0, 0.0, 7.0)),
        )
        ground = VectorChannel((0,), ((-6.03, 0.0, 0.0),))
        hips = VectorChannel((0,), ((0.0, 1.2, 0.0),))
        mapping, channels, rebased, held = prepare_preview_translations(
            [1, 8, 10], [trajectory, ground, hips],
            ["AITrajectory.t", "GroundPlane.t", "Hips.t"],
        )
        self.assertEqual(mapping, [1, 8, 10])
        self.assertEqual(channels[0].values[0], (0.0, 0.0, 0.0))
        self.assertEqual(channels[0].values[1][:2], (1.0, 0.0))
        self.assertAlmostEqual(channels[0].values[1][2], 1.145)
        self.assertEqual(channels[1], ground)
        self.assertEqual(channels[2], hips)
        self.assertEqual((rebased, held), (1, 1))


class RigamateDeltaCompositionTests(unittest.TestCase):
    def test_composes_default_before_delta_without_changing_timing(self) -> None:
        from game_asset_explorer.frostbite_animation import QuaternionChannel as EclipseChannel

        default = axis_angle_to_quaternion((0.0, 0.0, 1.0), math.pi / 2)
        delta = axis_angle_to_quaternion((1.0, 0.0, 0.0), math.pi / 2)
        identity = (0.0, 0.0, 0.0, 1.0)
        channel = EclipseChannel((0, 7), (identity, delta), b"constant", ((1, 2, 3),))
        composed = compose_transition_loop_channels([channel], [default])[0]
        expected = quaternion_multiply(default, delta)
        self.assertEqual(composed.times, channel.times)
        self.assertEqual(composed.constant_encoding, channel.constant_encoding)
        self.assertEqual(composed.packed_words, channel.packed_words)
        for actual, wanted in zip(composed.values[1], expected):
            self.assertAlmostEqual(actual, wanted)

    def test_rejects_shifted_default_table(self) -> None:
        with self.assertRaises(ValueError):
            compose_transition_loop_channels([], [(0.0, 0.0, 0.0, 1.0)])

    def test_missing_base_keeps_channel_unchanged(self) -> None:
        from game_asset_explorer.frostbite_animation import QuaternionChannel as EclipseChannel

        channel = EclipseChannel((0,), ((0.0, 0.0, 0.0, 1.0),))
        self.assertIs(compose_transition_loop_channels([channel], [None])[0], channel)

    def test_nonidentity_first_frame_is_removed_before_loop_motion(self) -> None:
        from game_asset_explorer.frostbite_animation import QuaternionChannel as EclipseChannel

        reference = axis_angle_to_quaternion((0.0, 1.0, 0.0), math.pi / 3)
        motion = axis_angle_to_quaternion((1.0, 0.0, 0.0), math.pi / 8)
        second = quaternion_multiply(reference, motion)
        base = axis_angle_to_quaternion((0.0, 0.0, 1.0), math.pi / 4)
        channel = EclipseChannel((0, 1), (reference, second))
        result = compose_transition_loop_channels([channel], [base])[0]
        for actual, wanted in zip(result.values[0], base):
            self.assertAlmostEqual(actual, wanted)
        expected = quaternion_multiply(base, motion)
        for actual, wanted in zip(result.values[1], expected):
            self.assertAlmostEqual(actual, wanted)


class BoneMappingTests(unittest.TestCase):
    def test_prefers_primary_rig_order_over_raw_skeleton_order(self) -> None:
        skeleton = SkeletonData("test", [
            ("Hips", -1, 0.0, 0.0, 0.0),
            ("LeftHandThumb1", 0, 0.0, 0.0, 0.0),
            ("RightArm", 0, 0.0, 0.0, 0.0),
        ])
        # Raw order would hit the finger before the arm; primary order fixes that.
        mapping = guess_bone_mapping(skeleton, channel_count=2, primary_rig_order=[0, 2])
        self.assertEqual(mapping, [0, 2])

    def test_falls_back_to_raw_order_without_a_primary_rig_order(self) -> None:
        skeleton = SkeletonData("test", [
            ("Hips", -1, 0.0, 0.0, 0.0),
            ("Camera", 0, 0.0, 0.0, 0.0),
            ("Spine", 0, 0.0, 0.0, 0.0),
        ])
        mapping = guess_bone_mapping(skeleton, channel_count=2, primary_rig_order=None)
        # Camera is a helper joint and should be skipped either way.
        self.assertEqual(mapping, [0, 2])


class FilterHealthyChannelsTests(unittest.TestCase):
    """Guards the fix made after a real pose debug dump showed a corrupted
    Spine2 channel (already flagged unhealthy, with 50 duplicate
    timestamps) dragging its entire downstream subtree -- shoulders, neck,
    head -- into extreme rotation. Filtering it out before playback should
    leave that one joint at its bind pose while everything else is
    unaffected, rather than silently using known-bad data."""

    def test_drops_unhealthy_channels_and_keeps_the_rest_in_lockstep(self) -> None:
        from game_asset_explorer.frostbite_animation_channels import ChannelHealth
        from game_asset_explorer.frostbite_animation_playback import filter_healthy_channels

        mapping = [10, 11, 12, 13]  # e.g. Hips, Spine, Spine1, Spine2
        channels = [
            QuaternionChannel(0, 10, tuple(range(10)), tuple((i, i, i) for i in range(10))),
            QuaternionChannel(0, 10, tuple(range(10)), tuple((i * 2, i, i) for i in range(10))),
            QuaternionChannel(0, 10, tuple(range(10)), tuple((i * 3, i, i) for i in range(10))),
            QuaternionChannel(0, 10, tuple(range(10)), tuple((i * 4, i, i) for i in range(10))),
        ]
        # Spine1 (channel_index 2) is the one flagged unhealthy.
        health = [
            ChannelHealth(0, "Hips", 10, True, ()),
            ChannelHealth(1, "Spine", 10, True, ()),
            ChannelHealth(2, "Spine1", 10, False, ("50 duplicate timestamp(s)",)),
            ChannelHealth(3, "Spine2", 10, True, ()),
        ]

        filtered_mapping, filtered_channels = filter_healthy_channels(mapping, channels, health)

        self.assertEqual(filtered_mapping, [10, 11, 13])
        self.assertEqual(len(filtered_channels), 3)
        # The remaining channels must still correspond to the right joints,
        # not just be "3 channels" -- check by identity against the originals.
        self.assertIs(filtered_channels[0], channels[0])
        self.assertIs(filtered_channels[1], channels[1])
        self.assertIs(filtered_channels[2], channels[3])

    def test_unflagged_channel_indices_pass_through(self) -> None:
        # A channel_index with no corresponding health entry at all should
        # be treated as healthy (kept), not silently dropped.
        from game_asset_explorer.frostbite_animation_playback import filter_healthy_channels

        mapping = [5]
        channels = [QuaternionChannel(0, 10, tuple(range(10)), tuple((i, i, i) for i in range(10)))]
        filtered_mapping, filtered_channels = filter_healthy_channels(mapping, channels, health=[])
        self.assertEqual(filtered_mapping, [5])
        self.assertEqual(len(filtered_channels), 1)


if __name__ == "__main__":
    unittest.main()
