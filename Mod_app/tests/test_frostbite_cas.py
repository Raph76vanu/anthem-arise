from __future__ import annotations

import struct
import unittest
import uuid

from game_asset_explorer.frostbite_cas import (
    FrostbiteCasError,
    decode_cas_record,
    inspect_anthem_meshset,
    inspect_cas_blocks,
)


class FrostbiteCasTests(unittest.TestCase):
    def test_uncompressed_blocks_decode(self) -> None:
        raw = struct.pack(">IHH", 4, 0x70, 4) + b"test"
        self.assertEqual(decode_cas_record(raw), b"test")
        self.assertEqual(inspect_cas_blocks(raw)[0].compression, 0x70)

    def test_oodle_requires_decoder(self) -> None:
        raw = struct.pack(">IHH", 9, 0x1170, 3) + b"abc"
        with self.assertRaisesRegex(FrostbiteCasError, "Oodle"):
            decode_cas_record(raw)
        self.assertEqual(decode_cas_record(raw, lambda payload, size: b"decoded!!"), b"decoded!!")

    def test_truncated_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(FrostbiteCasError, "truncated"):
            inspect_cas_blocks(struct.pack(">IHH", 100, 0x70, 100) + b"x")

    def test_anthem_meshset_header_and_chunk(self) -> None:
        raw = bytearray(512)
        struct.pack_into("<8f", raw, 0, -1, -2, -3, 0, 4, 5, 6, 0)
        struct.pack_into("<6Q", raw, 32, 160, 0, 0, 0, 0, 0)
        struct.pack_into("<QQ", raw, 88, 320, 330)
        struct.pack_into("<I", raw, 108, 1)
        struct.pack_into("<HH", raw, 144, 1, 3)
        chunk = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
        raw[260:276] = chunk.bytes_le
        raw[320:329] = b"javelin\0"
        header = inspect_anthem_meshset(bytes(raw))
        self.assertEqual(header.name, "javelin")
        self.assertEqual(header.lod_count, 1)
        self.assertEqual(header.chunk_ids, (str(chunk),))


if __name__ == "__main__":
    unittest.main()
