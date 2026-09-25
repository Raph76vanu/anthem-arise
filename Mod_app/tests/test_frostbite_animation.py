from __future__ import annotations

import math
import struct
import unittest

from game_asset_explorer.frostbite_animation import (
    BANK_POINTER_HASH,
    CHANNEL_TO_DOF_HASH,
    ECLIPSE_ANIMATION_HASH,
    PRIMARY_RIG_FEATURE_HASH,
    _decode_quaternion_channels,
    _read_times,
    decode_packed_quaternion_48,
    decode_packed_quaternion_64,
    channel_map_candidates,
    count_anthem_animation_clips,
    decode_anthem_animation_stream,
    decode_bank_pointers,
    decode_primary_rig_keys,
    has_gd_data_asset_key,
    resolve_clip_channel_map,
)
from game_asset_explorer.geometry import MeshFormatError


class EclipseKeyTimeTests(unittest.TestCase):
    def test_counts_clip_objects_without_decoding_curve_payloads(self) -> None:
        raw = bytes(
            _gd_data_block(ECLIPSE_ANIMATION_HASH, 160)
            + _gd_data_block(CHANNEL_TO_DOF_HASH, 120)
            + _gd_data_block(ECLIPSE_ANIMATION_HASH, 160)
        )
        self.assertEqual(count_anthem_animation_clips(raw), 2)

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

    def test_constant_inline_codec_is_decoded_and_preserved(self) -> None:
        # Real Anthem EXM identity: X/Y/Z are effectively zero and the high
        # bit restores negative W (the same rotation as positive identity).
        inline = bytes.fromhex("0300200000040080")
        table = struct.pack("<I", 1) + inline
        channel = _decode_quaternion_channels(table, 0, 1, b"", 1)[0]
        self.assertEqual(channel.times, (0,))
        self.assertEqual(len(channel.values), 1)
        self.assertAlmostEqual(channel.values[0][3], -1.0, places=9)
        self.assertAlmostEqual(sum(c*c for c in channel.values[0]), 1.0, places=9)
        self.assertEqual(channel.constant_encoding, inline)

    def test_unverified_constant_is_preserved_without_rejecting_its_clip(self) -> None:
        inline = bytes.fromhex("bc6db023f101a825")
        table = struct.pack("<I", 1) + inline
        channel = _decode_quaternion_channels(table, 0, 1, b"", 1)[0]
        self.assertFalse(channel.decode_valid)
        self.assertEqual(channel.constant_encoding, inline)
        self.assertEqual(channel.values, ((0.0, 0.0, 0.0, 1.0),))

    def test_constant_fields_preserve_low_component_bits_and_w_sign(self) -> None:
        word = 3 | (1 << 21) | (1 << 42)
        positive = decode_packed_quaternion_64(word.to_bytes(8, "little"))
        negative = decode_packed_quaternion_64((word | (1 << 63)).to_bytes(8, "little"))
        self.assertGreater(positive[0], positive[1])
        self.assertAlmostEqual(positive[1], positive[2], places=12)
        self.assertGreater(positive[3], 0)
        self.assertLess(negative[3], 0)
        self.assertAlmostEqual(sum(c*c for c in negative), 1.0, places=7)

    def test_real_wide_constant_fits_eclipse_sqrt_two_range(self) -> None:
        # Present in all three standing-guard idle clips. Treating the signed
        # fields as [-1,+1] makes XYZ length^2=1.735 and incorrectly rejects it.
        quaternion = decode_packed_quaternion_64(bytes.fromhex("18b2b5391d8a082a"))
        self.assertAlmostEqual(sum(c*c for c in quaternion), 1.0)

    def test_real_rear_toe_constant_is_a_unit_quaternion(self) -> None:
        quaternion = decode_packed_quaternion_64(bytes.fromhex("f2ff1f00000c0080"))
        self.assertAlmostEqual(sum(c*c for c in quaternion), 1.0, places=7)

    def test_constant_decoder_rejects_wrong_size(self) -> None:
        with self.assertRaises(MeshFormatError):
            decode_packed_quaternion_64(b"short")


class EclipseArrayDescriptorTests(unittest.TestCase):
    def test_lists_same_sized_channel_maps_as_inference_candidates(self) -> None:
        bank = _gd_data_block(CHANNEL_TO_DOF_HASH, 192)
        struct.pack_into("<II", bank, 48, 2, 2)
        bank[88:96] = b"CANDIDAT"
        label = b"ToolChannelToDofSetVirtualAssetData\0"
        bank[96:96 + len(label)] = label
        struct.pack_into("<2I", bank, 96 + len(label), 41, 42)

        self.assertEqual(channel_map_candidates(bytes(bank), 2), ((41, 42),))
        self.assertEqual(channel_map_candidates(bytes(bank), 3), ())

    def test_resolves_channel_map_from_external_bank(self) -> None:
        clip = _gd_data_block(ECLIPSE_ANIMATION_HASH, 320)
        clip[148:165] = b"1234567890ABCDEF\0"
        struct.pack_into("<IIQ", clip, 112, 0, 0, 0)
        struct.pack_into("<IIQ", clip, 80, 1, 1, 160)
        struct.pack_into("<IIQ", clip, 96, 1, 1, 208)
        struct.pack_into("<IIQ", clip, 48, 1, 1, 220)
        struct.pack_into("<III", clip, 176, 2, 0, 12)
        struct.pack_into("<3f", clip, 192, 2.0, 3.0, 4.0)
        struct.pack_into("<I", clip, 224, 1)
        key = b"BANK-MAP"
        clip[-64:-56] = key
        decoded, = decode_anthem_animation_stream(bytes(clip))
        self.assertFalse(decoded.mapped)

        bank = _gd_data_block(CHANNEL_TO_DOF_HASH, 192)
        struct.pack_into("<II", bank, 48, 2, 2)
        bank[88:96] = key
        label = b"ToolChannelToDofSetVirtualAssetData\0"
        bank[96:96 + len(label)] = label
        struct.pack_into("<2I", bank, 96 + len(label), 41, 42)
        resolved = resolve_clip_channel_map(decoded, bytes(bank))
        self.assertTrue(resolved.mapped)
        self.assertEqual(resolved.dof_ids, (41, 42))

    def test_uses_the_clip_dof_reference_when_two_maps_have_the_same_size(self) -> None:
        clip = _gd_data_block(ECLIPSE_ANIMATION_HASH, 320)
        clip[148:165] = b"1234567890ABCDEF\0"
        struct.pack_into("<IIQ", clip, 112, 0, 0, 0)
        struct.pack_into("<IIQ", clip, 80, 1, 1, 160)
        struct.pack_into("<IIQ", clip, 96, 1, 1, 208)
        struct.pack_into("<IIQ", clip, 48, 1, 1, 220)
        struct.pack_into("<III", clip, 176, 2, 0, 12)
        struct.pack_into("<3f", clip, 192, 2.0, 3.0, 4.0)
        struct.pack_into("<I", clip, 224, 1)

        def dof_asset(key: bytes, ids: tuple[int, int]) -> bytearray:
            asset = _gd_data_block(CHANNEL_TO_DOF_HASH, 192)
            struct.pack_into("<I", asset, 48, len(ids))
            asset[88:96] = key
            label = b"ToolChannelToDofSetVirtualAssetData\0"
            asset[96:96 + len(label)] = label
            struct.pack_into("<2I", asset, 96 + len(label), *ids)
            return asset

        first = b"FIRSTMAP"
        second = b"SECONDMAP"
        clip[-64:-56] = second
        raw = bytes(dof_asset(first, (10, 11)) + clip + dof_asset(second, (20, 21)))
        decoded, = decode_anthem_animation_stream(raw)
        self.assertEqual(decoded.dof_ids, (20, 21))

        clip[-64:-56] = b"MISSING!"
        unmatched, = decode_anthem_animation_stream(bytes(dof_asset(first, (10, 11)) + clip))
        self.assertFalse(unmatched.mapped)

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

    def test_constant_only_clip_does_not_require_a_key_stream(self) -> None:
        block = _gd_data_block(ECLIPSE_ANIMATION_HASH, 256)
        block[148:165] = b"1234567890ABCDEF\0"
        struct.pack_into("<IIQ", block, 112, 0, 0, 0)
        struct.pack_into("<IIQ", block, 80, 1, 1, 160)
        struct.pack_into("<IIQ", block, 96, 1, 1, 208)
        struct.pack_into("<IIQ", block, 48, 0, 0, 0)
        struct.pack_into("<III", block, 176, 2, 0, 12)
        struct.pack_into("<3f", block, 192, 2.0, 3.0, 4.0)
        struct.pack_into("<I", block, 224, 1)

        clip, = decode_anthem_animation_stream(bytes(block))

        self.assertEqual(clip.vector_channels[0].values[0], (2.0, 3.0, 4.0))
        self.assertTrue(clip.quaternion_channels[0].decode_valid)

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
    def test_primary_rig_feature_references_a_specific_rig_key(self) -> None:
        key = b"EXM-RIG!"
        feature = _gd_data_block(PRIMARY_RIG_FEATURE_HASH, 120)
        feature[80:88] = key
        self.assertEqual(decode_primary_rig_keys(bytes(feature)), (key,))

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
