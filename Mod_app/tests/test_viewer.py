from unittest.mock import Mock, patch
import unittest

from game_asset_explorer.geometry import MeshData
from game_asset_explorer.viewer import render_mesh_raster


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


if __name__ == "__main__":
    unittest.main()
