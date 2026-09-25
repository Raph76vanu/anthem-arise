import unittest
from types import SimpleNamespace

from game_asset_explorer.gui import App
from game_asset_explorer.interceptor_prototype import (
    InterceptorController, PrototypeInput, select_clip_index,
    transition_base_clip_name,
)


class InterceptorControllerTests(unittest.TestCase):
    def test_walk_jump_land_and_flight(self) -> None:
        controller = InterceptorController()
        for _ in range(20):
            state = controller.update(1 / 60, PrototypeInput(forward=1))
        self.assertEqual(state.mode, "walk")
        self.assertLess(state.z, 0)

        state = controller.update(1 / 60, PrototypeInput(jump_pressed=True))
        self.assertEqual(state.mode, "jump")
        self.assertGreater(state.vy, 0)
        for _ in range(120):
            state = controller.update(1 / 60, PrototypeInput())
        self.assertTrue(state.grounded)
        self.assertEqual(state.y, 0)

        state = controller.update(1 / 60, PrototypeInput(flight_toggled=True))
        self.assertEqual(state.mode, "hover")
        state = controller.update(1 / 60, PrototypeInput(forward=1, sprint=True))
        self.assertEqual(state.mode, "glide")

    def test_large_frame_stall_is_bounded(self) -> None:
        controller = InterceptorController()
        state = controller.update(10.0, PrototypeInput(forward=1))
        self.assertLess(state.z, 0.2)

    def test_diagonal_input_moves_and_faces_diagonally(self) -> None:
        controller = InterceptorController()
        for _ in range(30):
            state = controller.update(1 / 60, PrototypeInput(forward=1, right=1))
        self.assertGreater(state.x, 0)
        self.assertLess(state.z, 0)
        self.assertAlmostEqual(state.yaw, 2.356194, places=3)

    def test_shift_increases_ground_and_flight_speed(self) -> None:
        walking = InterceptorController()
        sprinting = InterceptorController()
        for _ in range(90):
            walk_state = walking.update(1 / 60, PrototypeInput(forward=1))
            sprint_state = sprinting.update(
                1 / 60, PrototypeInput(forward=1, sprint=True),
            )
        self.assertEqual(sprint_state.mode, "sprint")
        self.assertGreater(abs(sprint_state.vz), abs(walk_state.vz))

        normal_flight = InterceptorController()
        fast_flight = InterceptorController()
        normal_flight.update(1 / 60, PrototypeInput(flight_toggled=True))
        fast_flight.update(1 / 60, PrototypeInput(flight_toggled=True))
        for _ in range(90):
            normal_state = normal_flight.update(1 / 60, PrototypeInput(forward=1))
            fast_state = fast_flight.update(
                1 / 60, PrototypeInput(forward=1, sprint=True),
            )
        self.assertEqual(fast_state.mode, "glide")
        self.assertGreater(abs(fast_state.vz), abs(normal_state.vz))


class PrototypeClipSelectionTests(unittest.TestCase):
    def test_prefers_plain_full_body_clips(self) -> None:
        names = [
            "EXF_NOV_Sprint_Start_Forward",
            "EXF_NOV_Sprint_Forward_Unarmed_Incline_50_Blend",
            "EXF_NOV_Sprint_Forward_Unarmed",
            "EXF_NOV_EXP_Hover_Loop_Dn",
            "EXF_NOV_EXP_Hover_Loop",
        ]
        self.assertEqual(select_clip_index(names, "sprint"), 2)
        self.assertEqual(select_clip_index(names, "hover"), 4)

    def test_prototype_avoids_sparse_blend_space_pose_samples(self) -> None:
        names = [
            "EXF_NOV_Idle_Strafe_Standing_StaticPose",
            "EXF_NOVA_Idle_Gesture_Warmup_Body_05.NonAdditive",
            "EXF_NOV_Glide_Slow_Forward",
            "EXF_NOV_Glide_Loop_Boost",
            "EXF_NOV_Glide_Loop",
        ]
        self.assertEqual(select_clip_index(names, "idle"), 1)
        self.assertEqual(select_clip_index(names, "glide"), 4)

    def test_numpad_lod_keys_work_with_numlock_on_or_off(self) -> None:
        self.assertEqual(App._prototype_numpad_lod(SimpleNamespace(keysym="KP_0")), 0)
        self.assertEqual(App._prototype_numpad_lod(SimpleNamespace(keysym="KP_Left")), 4)
        self.assertEqual(App._prototype_numpad_lod(SimpleNamespace(keysym="4")), 4)
        self.assertEqual(
            App._prototype_numpad_lod(SimpleNamespace(keysym="", keycode=101)), 5,
        )

    def test_only_verified_delta_loop_uses_transition_base(self) -> None:
        self.assertEqual(
            transition_base_clip_name("EXF_NOV_Glide_Loop"),
            "EXF_NOV_Glide_Start",
        )
        self.assertEqual(
            transition_base_clip_name("EXF_NOV_EXP_Hover_Loop"),
            "EXF_NOV_EXP_Hover_Start",
        )
        self.assertIsNone(transition_base_clip_name(
            "EXF_NOVA_Idle_Gesture_Warmup_Body_03.NonAdditive",
        ))
