from __future__ import annotations

import math
import struct
import unittest
import uuid

from game_asset_explorer.frostbite_ebx import (
    _rotation_matrix_to_quaternion, decode_anthem_skeleton,
    decode_anthem_skeleton_bind_rotations, inspect_anthem_ebx_record,
    parse_anthem_ebx_header,
)
from game_asset_explorer.geometry import MeshFormatError


class FrostbiteEbxTests(unittest.TestCase):
    def test_reads_file_guid_and_external_imports(self) -> None:
        file_guid = uuid.UUID("f518aa37-0590-11e5-8c8f-9eb968afc1f2")
        dependency = uuid.UUID("035ea995-d979-11e5-a04d-fcac4e43f881")
        class_guid = uuid.UUID("6335ec79-f32d-0615-68c7-eec98216a2f7")
        raw = bytearray(128)
        struct.pack_into("<4I", raw, 0, 0x0FB4D1CE, 112, 16, 1)
        struct.pack_into("<I", raw, 36, 16)
        raw[40:56] = file_guid.bytes_le
        raw[64:80] = dependency.bytes_le
        raw[80:96] = class_guid.bytes_le

        header = parse_anthem_ebx_header(bytes(raw))

        self.assertEqual(header.file_guid, file_guid)
        self.assertEqual(header.imports, ((dependency, class_guid),))

    def test_rejects_a_truncated_import_table(self) -> None:
        raw = bytearray(80)
        struct.pack_into("<4I", raw, 0, 0x0FB4D1CE, 64, 16, 1)
        with self.assertRaisesRegex(MeshFormatError, "import table"):
            parse_anthem_ebx_header(bytes(raw))

    def test_inspects_animation_ebx_without_claiming_clip_playback(self) -> None:
        raw = bytearray(160)
        struct.pack_into("<4I", raw, 0, 0x0FB4D1CE, 128, 0, 0)
        struct.pack_into("<3I", raw, 28, 24, 0, 8)
        raw[40:56] = uuid.UUID("035ea995-d979-11e5-a04d-fcac4e43f881").bytes_le
        raw[128:151] = b"AnimationClipAsset\0idle\0"

        report = inspect_anthem_ebx_record(bytes(raw))

        self.assertEqual(report["arrays"], 0)
        self.assertEqual(report["imports"], 0)
        self.assertIn("AnimationClipAsset", report["hints"])

    def test_decodes_names_parents_and_model_bind_positions(self) -> None:
        strings_offset, strings_length, data_length = 0x100, 16, 16
        arrays_base = strings_offset + strings_length + data_length
        descriptors = (
            (0, 2, 4), (8, 2, 6), (16, 2, 7),
            (144, 2, 7), (272, 2, 7),
        )
        raw = bytearray(arrays_base + 400)
        struct.pack_into("<4I", raw, 0, 0x0FB4D1CE, strings_offset, 0, 0)
        struct.pack_into("<3I", raw, 28, strings_length, len(descriptors), data_length)
        raw[40:56] = uuid.uuid4().bytes_le
        table_offset = strings_offset - 64
        for index, descriptor in enumerate(descriptors):
            struct.pack_into("<IIi", raw, table_offset + index * 12, *descriptor)
        raw[strings_offset:strings_offset + 11] = b"Root\0Child\0"
        struct.pack_into("<2I", raw, arrays_base, 0, 5)
        struct.pack_into("<2i", raw, arrays_base + 8, -1, 0)

        identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        child = identity.copy()
        child[12] = 1.0
        for offset in (16, 144, 272):
            struct.pack_into("<16f", raw, arrays_base + offset, *identity)
            struct.pack_into("<16f", raw, arrays_base + offset + 64, *child)

        skeleton = decode_anthem_skeleton(bytes(raw), "test")

        self.assertEqual(skeleton.joints[0], ("Root", -1, 0.0, 0.0, 0.0))
        self.assertEqual(skeleton.joints[1], ("Child", 0, 1.0, 0.0, 0.0))

        rotations = decode_anthem_skeleton_bind_rotations(bytes(raw))
        self.assertEqual(len(rotations), 2)
        for x, y, z, w in rotations:
            self.assertAlmostEqual(x * x + y * y + z * z + w * w, 1.0, places=5)
            # every candidate array here uses an identity rotation submatrix
            self.assertAlmostEqual(w, 1.0, places=5)

    def test_rotation_matrix_to_quaternion_round_trips_a_known_rotation(self) -> None:
        # A 90 degree rotation around Z: x'=-y, y'=x, z'=z (row-major).
        rotation_90z = (0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        x, y, z, w = _rotation_matrix_to_quaternion(rotation_90z)
        self.assertAlmostEqual(x * x + y * y + z * z + w * w, 1.0, places=5)
        angle_deg = math.degrees(2 * math.acos(min(1.0, abs(w))))
        self.assertAlmostEqual(angle_deg, 90.0, places=2)
        self.assertAlmostEqual(z, math.copysign(math.sin(math.radians(45)), z), places=5)
        self.assertAlmostEqual(x, 0.0, places=5)
        self.assertAlmostEqual(y, 0.0, places=5)


if __name__ == "__main__":
    unittest.main()
