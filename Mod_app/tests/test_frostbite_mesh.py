from __future__ import annotations

import struct
import unittest

from game_asset_explorer.frostbite_mesh import decode_anthem_geometry, inspect_anthem_rig, parse_anthem_lod
from game_asset_explorer.geometry import MeshFormatError


def sample_meshset() -> bytes:
    raw = bytearray(1024)
    lod_offset = 160
    section_offset = 384
    category_offset = 900
    name_offset = 920
    struct.pack_into("<Q", raw, 32 + 5 * 8, lod_offset)
    struct.pack_into("<IIIiQ", raw, lod_offset, 1, 1, 0, 1, section_offset)
    cursor = lod_offset + 24
    struct.pack_into("<iQ", raw, cursor, 1, category_offset)
    cursor += 12
    for _ in range(4):
        struct.pack_into("<iQ", raw, cursor, 0, category_offset + 1)
        cursor += 12
    struct.pack_into("<IiII", raw, cursor, 0x40000041, 0x21, 6, 36)
    raw[category_offset] = 0
    raw[name_offset:name_offset + 5] = b"body\0"

    struct.pack_into("<QQ", raw, section_offset, 0, name_offset)
    struct.pack_into("<iIIIII", raw, section_offset + 16, 0, 0, 1, 0, 0, 3)
    struct.pack_into("<BBBB", raw, section_offset + 40, 12, 3, 0, 0)
    struct.pack_into("<IQ", raw, section_offset + 44, 0, 0)
    struct.pack_into("<BBBB", raw, section_offset + 64, 1, 3, 0, 0)
    struct.pack_into("<BB", raw, section_offset + 128, 12, 0)
    struct.pack_into("<BB", raw, section_offset + 160, 1, 1)
    return bytes(raw)


class FrostbiteMeshTests(unittest.TestCase):
    def test_decodes_declared_vertices_and_triangle_indices(self) -> None:
        chunk = struct.pack("<9f3H", 0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 1, 2)
        lod = parse_anthem_lod(sample_meshset(), 5)
        self.assertEqual(lod.visible_sections, (0,))
        meshes = decode_anthem_geometry(sample_meshset(), chunk, 5)
        self.assertEqual(meshes[0].name, "body")
        self.assertEqual(meshes[0].vertices[2], (0.0, 2.0, 0.0))
        self.assertEqual(meshes[0].faces, [(0, 1, 2)])

    def test_rejects_truncated_chunk(self) -> None:
        with self.assertRaisesRegex(MeshFormatError, "truncated"):
            decode_anthem_geometry(sample_meshset(), b"short", 5)

    def test_rig_diagnostic_does_not_claim_unverified_rig(self) -> None:
        diagnostic = inspect_anthem_rig(sample_meshset(), 5)
        self.assertFalse(diagnostic.likely_skinned)
        self.assertIn("does not prove", diagnostic.summary)

    def test_rig_diagnostic_detects_paired_skin_streams(self) -> None:
        raw = bytearray(sample_meshset())
        section_offset = 384
        struct.pack_into("<BBBB", raw, section_offset + 68, 0x02, 0x17, 0, 1)
        struct.pack_into("<BBBB", raw, section_offset + 72, 0x04, 0x0D, 0, 2)
        struct.pack_into("<BB", raw, section_offset + 130, 8, 0)
        struct.pack_into("<BB", raw, section_offset + 132, 4, 0)
        struct.pack_into("<BB", raw, section_offset + 160, 3, 3)
        struct.pack_into("<IQ", raw, section_offset + 44, 6, 940)
        struct.pack_into("<6H", raw, 940, 100, 101, 102, 103, 104, 105)

        diagnostic = inspect_anthem_rig(bytes(raw), 5)

        self.assertTrue(diagnostic.likely_skinned)
        self.assertIn("Likely skinned", diagnostic.summary)

    def test_rig_inspector_decodes_indices_and_weights_from_chunk(self) -> None:
        raw = bytearray(sample_meshset())
        section_offset = 384
        # Position stream followed by UShort4 indices and UByte4N weights.
        struct.pack_into("<BBBB", raw, section_offset + 68, 0x02, 0x17, 0, 1)
        struct.pack_into("<BBBB", raw, section_offset + 72, 0x04, 0x0D, 0, 2)
        struct.pack_into("<BB", raw, section_offset + 130, 8, 0)
        struct.pack_into("<BB", raw, section_offset + 132, 4, 0)
        struct.pack_into("<BB", raw, section_offset + 160, 3, 3)
        struct.pack_into("<IQ", raw, section_offset + 44, 6, 940)
        struct.pack_into("<6H", raw, 940, 100, 101, 102, 103, 104, 105)
        struct.pack_into("<IiII", raw, 244, 0x40000041, 0x21, 6, 72)

        chunk = bytearray(78)
        struct.pack_into("<9f", chunk, 0, 0, 0, 0, 1, 0, 0, 0, 2, 0)
        struct.pack_into("<12H", chunk, 36, 2, 0, 0, 0, 2, 3, 0, 0, 3, 4, 5, 0)
        chunk[60:72] = bytes((255, 0, 0, 0, 128, 127, 0, 0, 85, 85, 85, 0))
        struct.pack_into("<3H", chunk, 72, 0, 1, 2)

        diagnostic = inspect_anthem_rig(bytes(raw), 5, bytes(chunk))
        meshes = decode_anthem_geometry(bytes(raw), bytes(chunk), 5)

        self.assertTrue(diagnostic.weights_decoded)
        self.assertEqual(diagnostic.weighted_vertices, 3)
        self.assertEqual(diagnostic.palette_slots, (2, 3, 4, 5))
        self.assertEqual(diagnostic.skeleton_bone_ids, (102, 103, 104, 105))
        self.assertEqual(diagnostic.sections[0].palette_size, 6)
        self.assertEqual(diagnostic.sections[0].sample_weights, (255, 0, 0, 0))
        self.assertIn("Skin weights decoded", diagnostic.summary)
        self.assertEqual(meshes[0].skin_bones[0], (102, 0, 0, 0))
        self.assertEqual(meshes[0].skin_weights[0], (1.0, 0.0, 0.0, 0.0))
        self.assertAlmostEqual(sum(meshes[0].skin_weights[1]), 1.0)


if __name__ == "__main__":
    unittest.main()
