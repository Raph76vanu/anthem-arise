from __future__ import annotations

import struct
import unittest

from game_asset_explorer.frostbite_animation_channels import (
    ChannelDecodeError, QuaternionChannel, analyze_channel_group,
    analyze_quaternion_channel_health, decode_float_channel,
    decode_quaternion_channel, decode_vector_channel, find_channel_array_headers,
    format_channel_health_report, format_channel_health_summary,
)
from game_asset_explorer.geometry import SkeletonData


def _pack_float_channel(key_count, times_addr, values_addr, min_v, range_v):
    return struct.pack("<3i2f", key_count, times_addr, values_addr, min_v, range_v)


def _pack_vector_channel(key_count, times_addr, values_addr, min_v, range_v):
    header = struct.pack("<3i", key_count, times_addr, values_addr)
    header += b"\0" * 4  # pad to the 16-byte slot before Min
    min_field = struct.pack("<3f", *min_v) + b"\0" * 4  # Min occupies a 16-byte slot too
    range_field = struct.pack("<3f", *range_v) + b"\0" * 4
    return header + min_field + range_field


class FloatChannelTests(unittest.TestCase):
    def test_decodes_a_real_channel_within_its_declared_bounds(self) -> None:
        raw = bytearray(200)
        times_addr, values_addr = 100, 120
        struct.pack_into(">4H", raw, times_addr, 10, 20, 30, 40)
        raw[values_addr:values_addr + 4] = bytes([0, 85, 170, 255])
        struct_bytes = _pack_float_channel(4, times_addr, values_addr, min_v=1.0, range_v=2.0)
        raw[0:len(struct_bytes)] = struct_bytes

        channel = decode_float_channel(bytes(raw), 0, block_offset=0)

        self.assertEqual(channel.key_count, 4)
        self.assertEqual(channel.times, (10, 20, 30, 40))
        self.assertAlmostEqual(channel.values[0], 1.0)   # byte 0   -> min
        self.assertAlmostEqual(channel.values[3], 3.0)   # byte 255 -> min + range
        self.assertTrue(channel.values_in_bounds())
        self.assertFalse(channel.is_degenerate)

    def test_rejects_an_implausible_key_count(self) -> None:
        raw = bytearray(64)
        struct_bytes = _pack_float_channel(999999, 0, 0, 0.0, 1.0)
        raw[0:len(struct_bytes)] = struct_bytes
        with self.assertRaises(ChannelDecodeError):
            decode_float_channel(bytes(raw), 0, block_offset=0)

    def test_recognizes_the_degenerate_two_key_constant_channel(self) -> None:
        raw = bytearray(64)
        struct.pack_into(">2H", raw, 20, 0, 0)
        raw[40:42] = bytes([0, 0])
        struct_bytes = _pack_float_channel(2, 20, 40, min_v=1.0, range_v=0.0)
        raw[0:len(struct_bytes)] = struct_bytes

        channel = decode_float_channel(bytes(raw), 0, block_offset=0)

        self.assertTrue(channel.is_degenerate)


class VectorChannelTests(unittest.TestCase):
    def test_decodes_a_real_channel_within_its_declared_bounds(self) -> None:
        raw = bytearray(200)
        times_addr, values_addr = 100, 120
        struct.pack_into(">2H", raw, times_addr, 5, 15)
        struct.pack_into(">6H", raw, values_addr, 0, 0, 0, 65535, 65535, 65535)
        min_v = (-1.0, -2.0, -3.0)
        range_v = (2.0, 4.0, 6.0)
        struct_bytes = _pack_vector_channel(2, times_addr, values_addr, min_v, range_v)
        raw[0:len(struct_bytes)] = struct_bytes

        channel = decode_vector_channel(bytes(raw), 0, block_offset=0)

        self.assertEqual(channel.times, (5, 15))
        self.assertEqual(len(channel.values), 2)
        for axis in range(3):
            self.assertAlmostEqual(channel.values[0][axis], min_v[axis], places=4)
            self.assertAlmostEqual(channel.values[1][axis], min_v[axis] + range_v[axis], places=4)
        self.assertTrue(channel.values_in_bounds())

    def test_rejects_a_negative_range(self) -> None:
        raw = bytearray(200)
        struct_bytes = _pack_vector_channel(2, 100, 120, (0, 0, 0), (-1.0, 1.0, 1.0))
        raw[0:len(struct_bytes)] = struct_bytes
        with self.assertRaises(ChannelDecodeError):
            decode_vector_channel(bytes(raw), 0, block_offset=0)

    def test_offsets_are_resolved_relative_to_the_enclosing_block_not_the_file_start(self) -> None:
        # This is the bug this project actually hit: times_off/values_off are
        # relative to the enclosing GD.DATA block, not absolute file offsets.
        raw = bytearray(400)
        block_offset = 200
        times_addr, values_addr = block_offset + 20, block_offset + 40  # absolute
        struct.pack_into(">2H", raw, times_addr, 1, 2)
        struct.pack_into(">6H", raw, values_addr, 100, 200, 300, 400, 500, 600)
        # struct stores OFFSETS RELATIVE TO block_offset, i.e. 20 and 40, not the absolute addresses
        struct_bytes = _pack_vector_channel(2, 20, 40, (0, 0, 0), (1.0, 1.0, 1.0))
        raw[0:len(struct_bytes)] = struct_bytes

        channel = decode_vector_channel(bytes(raw), 0, block_offset=block_offset)

        self.assertEqual(channel.times, (1, 2))


class QuaternionChannelTests(unittest.TestCase):
    def test_decodes_the_inline_single_key_format(self) -> None:
        raw = bytearray(64)
        struct.pack_into("<i", raw, 0, 1)  # KeyCount == 1 -> inline
        struct.pack_into(">H", raw, 4, 500)  # time
        struct.pack_into(">3h", raw, 6, 1000, -2000, 3000)  # x, y, z

        channel = decode_quaternion_channel(bytes(raw), 0, block_offset=0)

        self.assertEqual(channel.key_count, 1)
        self.assertEqual(channel.times, (500,))
        self.assertEqual(channel.raw_xyz, ((1000, -2000, 3000),))
        self.assertTrue(channel.is_degenerate)

    def test_decodes_the_pointer_format_relative_to_block_offset(self) -> None:
        raw = bytearray(200)
        block_offset = 50
        times_addr, values_addr = block_offset + 20, block_offset + 40
        struct.pack_into(">2H", raw, times_addr, 10, 20)
        struct.pack_into(">3h", raw, values_addr, 100, 200, 300)
        struct.pack_into(">3h", raw, values_addr + 6, 400, 500, 600)
        struct.pack_into("<3i", raw, 0, 2, 20, 40)  # offsets stored relative to block_offset

        channel = decode_quaternion_channel(bytes(raw), 0, block_offset=block_offset)

        self.assertEqual(channel.key_count, 2)
        self.assertEqual(channel.times, (10, 20))
        self.assertEqual(channel.raw_xyz, ((100, 200, 300), (400, 500, 600)))
        self.assertFalse(channel.is_degenerate)

    def test_truncates_samples_at_and_past_the_observed_corruption_threshold(self) -> None:
        # This is the real pattern found on actual game data: smooth, valid
        # samples followed by an abrupt jump to implausible small values
        # starting somewhere in the 31700-32800 range. Truncation should
        # keep only the confirmed-good samples below the threshold.
        raw = bytearray(400)
        block_offset = 50
        times_addr, values_addr = block_offset + 20, block_offset + 60
        good_times = (10, 20, 30)
        corrupted_times = (31800, 32000, 33000)
        all_times = good_times + corrupted_times
        struct.pack_into(f">{len(all_times)}H", raw, times_addr, *all_times)
        good_xyz = [(-27000, 2000, 28500), (-26900, 2100, 28400), (-26800, 2200, 28300)]
        corrupted_xyz = [(6, 12, 13), (24, 35, 42), (48, 68, 74)]
        for i, (x, y, z) in enumerate(good_xyz + corrupted_xyz):
            struct.pack_into(">3h", raw, values_addr + i * 6, x, y, z)
        struct.pack_into("<3i", raw, 0, len(all_times), 20, 60)

        channel = decode_quaternion_channel(bytes(raw), 0, block_offset=block_offset)

        self.assertEqual(channel.key_count, 3)
        self.assertEqual(channel.times, good_times)
        self.assertEqual(channel.raw_xyz, tuple(good_xyz))


class ChannelHealthTests(unittest.TestCase):
    def _skeleton(self, names: list[str]) -> SkeletonData:
        joints = [(name, -1 if i == 0 else 0, float(i), 0.0, 0.0) for i, name in enumerate(names)]
        return SkeletonData("test", joints)

    def test_reports_a_clean_channel_as_healthy(self) -> None:
        channel = QuaternionChannel(
            struct_offset=0, key_count=10,
            times=tuple(range(0, 100, 10)),
            raw_xyz=tuple((i * 100, i * 50, i * 25) for i in range(10)),
        )
        skeleton = self._skeleton(["Root", "Spine"])
        health = analyze_quaternion_channel_health([channel], [1], skeleton)

        self.assertEqual(len(health), 1)
        self.assertTrue(health[0].healthy)
        self.assertEqual(health[0].bone_name, "Spine")
        self.assertEqual(health[0].issues, ())

    def test_flags_duplicate_timestamps(self) -> None:
        # Confirmed real corruption signature: e.g. time 0 appearing 3 times
        # with 3 different values in an otherwise-plausible channel.
        times = (0, 0, 0, 50, 100, 150, 200, 250, 300, 350)
        channel = QuaternionChannel(
            struct_offset=0, key_count=len(times), times=times,
            raw_xyz=tuple((i * 100, i * 50, i * 25) for i in range(len(times))),
        )
        skeleton = self._skeleton(["Root", "RightUpLeg"])
        health = analyze_quaternion_channel_health([channel], [1], skeleton)

        self.assertFalse(health[0].healthy)
        self.assertTrue(any("duplicate timestamp" in issue for issue in health[0].issues))

    def test_flags_values_pinned_at_the_int16_boundary(self) -> None:
        # Confirmed real corruption signature: an axis stuck at exactly
        # -32768/32767 across most samples (e.g. a "LeftLeg" channel with x
        # pinned at the boundary on every one of 16 keys).
        times = tuple(range(0, 200, 10))
        raw_xyz = tuple((-32768, i * 10, 0) for i in range(len(times)))
        channel = QuaternionChannel(struct_offset=0, key_count=len(times), times=times, raw_xyz=raw_xyz)
        skeleton = self._skeleton(["Root", "LeftLeg"])
        health = analyze_quaternion_channel_health([channel], [1], skeleton)

        self.assertFalse(health[0].healthy)
        self.assertTrue(any("pinned" in issue for issue in health[0].issues))

    def test_short_channels_are_not_flagged_by_the_statistical_checks(self) -> None:
        # key_count <= 3 is too short for the duplicate/pinning heuristics
        # to mean anything (matches _quaternion_channel_looks_valid's own
        # threshold) -- a 2-key channel pinned at an extreme could just be a
        # legitimate constant pose, not corruption.
        channel = QuaternionChannel(
            struct_offset=0, key_count=2, times=(0, 100), raw_xyz=((-32768, 0, 0), (-32768, 0, 0)),
        )
        skeleton = self._skeleton(["Root", "Pelvis"])
        health = analyze_quaternion_channel_health([channel], [1], skeleton)

        self.assertTrue(health[0].healthy)

    def test_summary_and_report_formatting(self) -> None:
        healthy = QuaternionChannel(0, 10, tuple(range(0, 100, 10)), tuple((i, i, i) for i in range(10)))
        unhealthy = QuaternionChannel(0, 10, (0, 0) + tuple(range(20, 100, 10)),
                                       tuple((i, i, i) for i in range(10)))
        skeleton = self._skeleton(["Root", "Spine", "Neck"])
        health = analyze_quaternion_channel_health([healthy, unhealthy], [1, 2], skeleton)

        summary = format_channel_health_summary(health)
        self.assertIn("1/2", summary)

        report = format_channel_health_report(health)
        self.assertIn("Neck", report)
        self.assertIn("duplicate timestamp", report)


class ChannelArrayHeaderTests(unittest.TestCase):
    def test_finds_a_group_of_four_count_quadruples(self) -> None:
        raw = b"junk" + struct.pack("<4i", 3, 3, 40, 0) + struct.pack("<4i", 5, 5, 80, 0) \
            + struct.pack("<4i", 7, 7, 120, 0) + struct.pack("<4i", 2, 2, 20, 0) + b"more junk"
        groups = find_channel_array_headers(raw)
        self.assertEqual(len(groups), 1)
        counts = [entry.count for entry in groups[0]]
        self.assertEqual(counts, [3, 5, 7, 2])

    def test_ignores_a_run_that_does_not_match_the_quadruple_shape(self) -> None:
        raw = struct.pack("<4i", 3, 4, 40, 0)  # first two values differ -> not a valid header entry
        groups = find_channel_array_headers(raw)
        self.assertEqual(groups, [])


class ChannelGroupAnalysisTests(unittest.TestCase):
    def test_reports_decode_errors_separately_from_successes(self) -> None:
        # A header claiming 2 float channels and 1 vector channel, but with
        # no real channel data behind it -- every decode should fail cleanly
        # rather than reading garbage, and the report should say so.
        raw = bytearray(1024)
        header_addr = 0
        struct.pack_into("<4i", raw, 0, 2, 2, 999999999, 0)   # float header: implausible declared_value is fine, just a count
        struct.pack_into("<4i", raw, 16, 1, 1, 0, 0)          # vector header
        struct.pack_into("<4i", raw, 32, 1, 1, 0, 0)          # quaternion header (unused here)
        struct.pack_into("<4i", raw, 48, 1, 1, 0, 0)          # unknown 4th header

        headers = find_channel_array_headers(bytes(raw))[0]
        report = analyze_channel_group(bytes(raw), headers)

        # With all-zero bytes at the expected offsets, KeyCount reads as 0,
        # which is implausible and must be rejected, not treated as real data.
        self.assertEqual(len(report.float_channels), 0)
        self.assertGreater(report.float_decode_errors, 0)


if __name__ == "__main__":
    unittest.main()
