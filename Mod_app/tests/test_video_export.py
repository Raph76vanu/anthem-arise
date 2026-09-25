from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from game_asset_explorer.video_export import write_mjpeg_avi


class MjpegAviTests(unittest.TestCase):
    def test_writes_indexed_streaming_avi(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.avi"
            count = write_mjpeg_avi(
                path, (Image.new("RGB", (32, 24), color) for color in ("red", "blue")),
                width=32, height=24, fps=12,
            )
            raw = path.read_bytes()
        self.assertEqual(count, 2)
        self.assertEqual(raw[:4], b"RIFF")
        self.assertEqual(raw[8:12], b"AVI ")
        self.assertEqual(struct.unpack_from("<I", raw, 4)[0], len(raw) - 8)
        self.assertIn(b"MJPG", raw)
        self.assertEqual(raw.count(b"00dc"), 4)  # two chunks + two index entries
        self.assertIn(b"idx1", raw)
        strh = raw.index(b"strh") + 8
        self.assertEqual(struct.unpack_from("<I", raw, strh + 32)[0], 2)


if __name__ == "__main__":
    unittest.main()
