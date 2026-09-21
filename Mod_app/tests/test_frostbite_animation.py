from __future__ import annotations

import math
import struct
import unittest

from game_asset_explorer.frostbite_animation import (
    BANK_POINTER_HASH,
    ECLIPSE_ANIMATION_HASH,
    _decode_quaternion_channels,
    _read_times,
    decode_packed_quaternion_48,
    decode_anthem_animation_stream,
    decode_bank_pointers,
    has_gd_data_asset_key,
)
from game_asset_explorer.geometry import MeshFormatError


class EclipseKeyTimeTests(unittest.TestCase):
    def test_reads_one_byte_key_times(self) -> None:
        self.assertEqual(_read_times(bytes((0, 7, 230)), 0, 3, 1), (0, 7, 230))

    def test_reads_two_byte_key_times_as_big_endian(self) -> None:
        stream = struct.pack(">3H", 0, 280, 65535)
        self.assertEqual(_read_times(stream, 0, 3, 2), (0, 280, 65535))

    def test_rejects_non_monotonic_times(self) -> None:
        with self.assertRaises(MeshFormatError):
            _read_times(bytes((0, 9, 3)), 0, 3, 1)


class EclipseQuaternionTests(unittest.TestCase):
    def test_dynamic_values_are_unit_quaternions_not_euler_angles(self) -> None:
        # One channel table record followed by its independent stream. The
        # The low bits of the first two words select omitted W (11), leaving
        # three components quantized within [-1/sqrt(2), 1/sqrt(2)].
        table = struct.pack("<III", 2, 0, 4)
        stream = struct.pack(">2H", 0, 10)
        stream += struct.pack(">6H", 32769, 32769, 32768, 55937, 32769, 32768)
        channels = _decode_quaternion_channels(table, 0, 1, stream, 2)

        self.assertEqual(channels[0].times, (0, 10))
        identity = channels[0].values[0]
        self.assertAlmostEqual(identity[0], 0.0, places=4)
        self.assertAlmostEqual(identity[3], 1.0, places=6)
        rotated = channels[0].values[1]
        self.assertAlmostEqual(sum(component * component for component in rotated), 1.0, places=6)
        self.assertAlmostEqual(rotated[0], 0.5, places=4)
        self.assertAlmostEqual(rotated[3], math.sqrt(0.75), places=4)

    def test_two_selector_bits_restore_each_of_four_omitted_axes(self) -> None:
        for first, second, omitted in ((32768, 32768, 0),
                                       (32768, 32769, 1),
                                       (32769, 32768, 2),
                                       (32769, 32769, 3)):
            with self.subTest(omitted=omitted):
                quaternion = decode_packed_quaternion_48((first, second, 32768))
                self.assertAlmostEqual(quaternion[omitted], 1.0, places=7)
                self.assertAlmostEqual(sum(c*c for c in quaternion), 1.0, places=7)

    def test_rejects_impossible_packed_rotation_instead_of_normalizing_it(self) -> None:
        with self.assertRaises(MeshFormatError):
            decode_packed_quaternion_48((65535, 65535, 65535))

    def test_constant_inline_codec_is_preserved_instead_of_guessed(self) -> None:
        inline = bytes.fromhex("0102030405060708")
        table = struct.pack("<I", 1) + inline
        channel = _decode_quaternion_channels(table, 0, 1, b"", 1)[0]
        self.assertEqual(channel.times, ())
        self.assertEqual(channel.values, ())
        self.assertEqual(channel.constant_encoding, inline)


class EclipseArrayDescriptorTests(unittest.TestCase):
    def test_zero_float_channels_keep_vector_and_rotation_curves(self) -> None:
        block = _gd_data_block(ECLIPSE_ANIMATION_HASH, 256)
        block[148:165] = b"1234567890ABCDEF\0"
        struct.pack_into("<IIQ", block, 112, 0, 0, 0)
        struct.pack_into("<IIQ", block, 80, 1, 1, 160)
        struct.pack_into("<IIQ", block, 96, 1, 1, 208)
        struct.pack_into("<IIQ", block, 48, 1, 1, 220)
        struct.pack_into("<III", block, 176, 2, 0, 12)
        struct.pack_into("<3f", block, 192, 2.0, 3.0, 4.0)
        struct.pack_into("<I", block, 224, 1)

        clip, = decode_anthem_animation_stream(bytes(block))

        self.assertEqual(clip.float_channels, ())
        self.assertEqual(clip.vector_channels[0].values[0], (2.0, 3.0, 4.0))
        self.assertEqual(clip.quaternion_channels[0].constant_encoding, bytes(8))

    def test_uses_vector_descriptor_when_float_table_has_alignment_padding(self) -> None:
        block = _gd_data_block(ECLIPSE_ANIMATION_HASH, 264)
        block[148:165] = b"1234567890ABCDEF\0"
        # GD.DATA array offsets are relative to byte 16 of the object.
        for header, count, offset in ((112, 1, 152), (80, 1, 180),
                                      (96, 1, 228)):
            struct.pack_into("<IIQ", block, header, count, count, offset)
        struct.pack_into("<IIQ", block, 48, 1, 1, 240)
        struct.pack_into("<IIIff", block, 168, 2, 0, 10, 1.0, 0.0)
        # Eight bytes between the float table (ending at 188) and vector table.
        struct.pack_into("<III", block, 196, 2, 0, 10)
        struct.pack_into("<3f", block, 212, 2.0, 3.0, 4.0)
        struct.pack_into("<I", block, 244, 1)

        clip, = decode_anthem_animation_stream(bytes(block))

        self.assertEqual(clip.frame_count, 10)
        self.assertEqual(clip.vector_channels[0].values[0], (2.0, 3.0, 4.0))
        self.assertEqual(clip.quaternion_channels[0].constant_encoding, bytes(8))


def _gd_data_block(type_hash: int, size: int = 124) -> bytearray:
    raw = bytearray(size)
    raw[:8] = b"GD.DATAl"
    struct.pack_into("<I", raw, 8, size)
    struct.pack_into("<I", raw, 32, type_hash)
    return raw


class BankPointerTests(unittest.TestCase):
    def test_decodes_external_rigamate_subject_key(self) -> None:
        own = bytes.fromhex("2382f3ffa3f9ba62")
        subject = bytes.fromhex("cdb55c0a6ebc15cd")
        block = _gd_data_block(BANK_POINTER_HASH)
        block[72:80] = own
        block[80:88] = subject
        block[88:113] = b"BankPointer.Rigamate [1]\0"

        pointers = decode_bank_pointers(bytes(block))

        self.assertEqual(len(pointers), 1)
        self.assertEqual(pointers[0].asset_key, own)
        self.assertEqual(pointers[0].subject_key, subject)
        self.assertTrue(pointers[0].external)

    def test_marks_subject_local_when_another_object_contains_it(self) -> None:
        subject = bytes.fromhex("4982554a47d96db4")
        pointer = _gd_data_block(BANK_POINTER_HASH)
        pointer[72:80] = b"A" * 8
        pointer[80:88] = subject
        pointer[88:115] = b"BankPointer.ActionStation1\0"
        target = _gd_data_block(0x7883215E, 164)
        target[88:96] = subject
        raw = bytes(pointer + target)

        self.assertFalse(decode_bank_pointers(raw)[0].external)
        self.assertTrue(has_gd_data_asset_key(raw, subject))


if __name__ == "__main__":
    unittest.main()
