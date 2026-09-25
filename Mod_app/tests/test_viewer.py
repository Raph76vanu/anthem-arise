from unittest.mock import Mock, patch
import unittest

from game_asset_explorer.geometry import MeshData
from game_asset_explorer.viewer import (
    _clip_to_camera_near_plane,
    _training_platform_mesh,
    animation_helper_visible,
    render_mesh_raster,
)


class ViewerRasterTests(unittest.TestCase):
    def test_final_raster_draws_every_face(self) -> None:
        mesh = MeshData(
            "quad",
            [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)],
            [(0, 1, 2), (0, 2, 3)],
        )
        drawer = Mock()
        with patch("PIL.ImageDraw.Draw", return_value=drawer):
            rendered = render_mesh_raster([mesh], 320, 240, 0, 0, 1)

        self.assertEqual(rendered.size, (320, 240))
        self.assertEqual(drawer.polygon.call_count, len(mesh.faces))

    def test_prototype_face_budget_bounds_software_render_work(self) -> None:
        mesh = MeshData(
            "many",
            [(-1, -1, 0), (1, -1, 0), (1, 1, 0)],
            [(0, 1, 2)] * 100,
        )
        drawer = Mock()
        with patch("PIL.ImageDraw.Draw", return_value=drawer):
            render_mesh_raster([mesh], 320, 240, 0, 0, 1, face_budget=10)
        self.assertLessEqual(drawer.polygon.call_count, 10)

    def test_world_scene_uses_a_finite_platform_mesh(self) -> None:
        platform = _training_platform_mesh({
            "platform_half_size": 4.0,
            "platform_tile_size": 2.0,
            "normalization_center": (0.0, 1.0, 0.0),
            "normalization_extent": 2.0,
        })
        self.assertEqual(platform.name, "__training_platform__")
        self.assertEqual(len(platform.faces), 40)
        xs = [point[0] for point in platform.vertices]
        zs = [point[2] for point in platform.vertices]
        self.assertEqual((min(xs), max(xs)), (-2.0, 2.0))
        self.assertEqual((min(zs), max(zs)), (-2.0, 2.0))

    def test_world_scene_renders_platform_instead_of_legacy_horizon(self) -> None:
        mesh = MeshData("character", [(0, 0, 0), (0, 1, 0), (1, 0, 0)], [(0, 1, 2)])
        drawer = Mock()
        with patch("PIL.ImageDraw.Draw", return_value=drawer):
            render_mesh_raster(
                [mesh], 320, 240, -.6, -.25, 1,
                scene={
                    "world_scene": True,
                    "platform_half_size": 4.0,
                    "platform_tile_size": 2.0,
                    "normalization_center": (0.0, 1.0, 0.0),
                    "normalization_extent": 2.0,
                },
            )
        self.assertEqual(drawer.polygon.call_count, 41)
        drawer.rectangle.assert_not_called()

    def test_near_plane_clips_crossing_triangle(self) -> None:
        clipped = _clip_to_camera_near_plane((
            (-1.0, 0.0, 1.0), (1.0, 0.0, 1.0), (0.0, 0.0, 3.0),
        ))
        self.assertEqual(len(clipped), 4)
        self.assertTrue(all(point[2] <= 2.02 for point in clipped))


class AnimationOverlayVisibilityTests(unittest.TestCase):
    def test_hides_controller_and_camera_helpers(self) -> None:
        for name in (
            "Reference", "AITrajectory", "Trajectory", "GroundPlane",
            "ClimbPlane", "Camera", "Camera2", "HeadCamera", "Connect",
            "Connect2",
        ):
            with self.subTest(name=name):
                self.assertFalse(animation_helper_visible(name))

    def test_keeps_deforming_skeleton_joints(self) -> None:
        for name in ("Hips", "Spine", "LeftArm", "RightFoot", "Head"):
            with self.subTest(name=name):
                self.assertTrue(animation_helper_visible(name))


if __name__ == "__main__":
    unittest.main()
