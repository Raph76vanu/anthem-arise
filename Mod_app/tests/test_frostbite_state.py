from __future__ import annotations

import struct
import unittest

from game_asset_explorer.frostbite_state import (
    AntStateFormatError, find_class_fields, find_gd_data_blocks_by_hash,
    find_gd_sections, is_antstate_resource, iter_gd_data_blocks,
)


def _build_gd_data_block(payload: bytes, type_hash: int) -> bytes:
    """Build one synthetic GD.DATA block with the header shape this reader expects.

    After the 8-byte tag: length_a(4) length_b(4), 16 don't-care bytes, then
    the 4-byte type hash (which must land at header_start+24), then payload.
    """
    tag = b"GD.DATAl"
    dont_care = b"\0" * 16
    hash_field = struct.pack("<I", type_hash)
    tail = dont_care + hash_field + payload
    lengths = struct.pack("<2I", len(tail), len(tail))
    return tag + lengths + tail


class AntStateContainerTests(unittest.TestCase):
    def test_recognizes_an_antstate_resource_by_its_refl_tag(self) -> None:
        self.assertTrue(is_antstate_resource(b"junk before GD.REFL junk after"))
        self.assertFalse(is_antstate_resource(b"an ordinary decoded resource with no reflection tags"))

    def test_finds_strm_and_refl_section_headers(self) -> None:
        strm = b"GD.STRM" + b"\0" + struct.pack("<2I", 100, 90)
        refl = b"GD.REFL" + b"\0" + struct.pack("<2I", 200, 180)
        raw = b"leading" + strm + refl + b"trailing"
        sections = find_gd_sections(raw)
        self.assertEqual(sections["GD.STRM"].length_a, 100)
        self.assertEqual(sections["GD.REFL"].length_a, 200)


class GdDataBlockTests(unittest.TestCase):
    def test_iterates_multiple_data_blocks_and_reads_their_type_hash(self) -> None:
        block_a = _build_gd_data_block(b"payload-a" * 4, type_hash=0x11223344)
        block_b = _build_gd_data_block(b"payload-b-longer" * 4, type_hash=0x55667788)
        raw = b"prefix" + block_a + b"gap" + block_b + b"suffix"

        blocks = list(iter_gd_data_blocks(raw))

        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].type_hash, 0x11223344)
        self.assertEqual(blocks[1].type_hash, 0x55667788)

    def test_groups_blocks_by_type_hash(self) -> None:
        same_type = 0xABCDEF01
        block_a = _build_gd_data_block(b"first" * 8, type_hash=same_type)
        block_b = _build_gd_data_block(b"second" * 8, type_hash=same_type)
        block_c = _build_gd_data_block(b"third" * 8, type_hash=0x99999999)
        raw = block_a + block_b + block_c

        groups = find_gd_data_blocks_by_hash(raw)

        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[same_type]), 2)
        self.assertEqual(len(groups[0x99999999]), 1)

    def test_clips_rather_than_fails_when_the_final_block_overruns_eof(self) -> None:
        block = _build_gd_data_block(b"payload" * 8, type_hash=0x1)
        raw = block[:-4]  # truncate as if the trailing pad were cut off at EOF

        blocks = list(iter_gd_data_blocks(raw))

        self.assertEqual(len(blocks), 1)
        self.assertLessEqual(len(blocks[0].body), len(raw))


class ClassFieldTests(unittest.TestCase):
    def _build_scalar_field(self, type_ref: int, size: int, byte_offset: int) -> bytes:
        return struct.pack("<8i", type_ref, size, byte_offset, 0, 1, 0, 0, 0)

    def test_decodes_a_two_field_class_by_walking_backwards_from_its_name(self) -> None:
        field_x = self._build_scalar_field(type_ref=9, size=4, byte_offset=0)
        field_y = self._build_scalar_field(type_ref=9, size=4, byte_offset=4)
        name_section = b"\0TestVec\0x\0y\0"
        raw = b"leading junk" + field_x + field_y + name_section

        result = find_class_fields(raw, "TestVec", known_type_count=16)

        self.assertEqual([f.name for f in result.fields], ["x", "y"])
        self.assertEqual([f.byte_offset for f in result.fields], [0, 4])
        self.assertTrue(all(f.type_index == 9 for f in result.fields))

    def test_raises_rather_than_guessing_when_the_class_has_no_plain_scalar_fields(self) -> None:
        raw = b"no valid 32-byte field descriptor precedes this" + b"\0NoFields\0"
        with self.assertRaises(AntStateFormatError):
            find_class_fields(raw, "NoFields", known_type_count=16)

    def test_raises_when_the_name_occurrence_is_a_reference_not_a_definition(self) -> None:
        raw = b"xReferencedName\0"  # preceding byte is 'x', not a 0x00 record flag
        with self.assertRaises(AntStateFormatError):
            find_class_fields(raw, "ReferencedName", known_type_count=16)


if __name__ == "__main__":
    unittest.main()
