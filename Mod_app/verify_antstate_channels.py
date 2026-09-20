#!/usr/bin/env python3
"""Verify whether the animation-channel decoder is genuinely reading real
keyframe data out of an extracted Anthem AntState resource.

This is a standalone check, independent of the GUI and of anything not
finished yet (Quaternion channels, per-frame playback). Run it against a
``.res`` file you've already extracted and decompressed with the app's
"Extract selected..." action, and it tells you, concretely:

- How many channel-array groups it found in the file (one per embedded
  clip/state object).
- For each group: how many Float/Vector channels decoded at all, how many
  are real (not the constant 2-key placeholder), and -- the important
  number -- how many of those respect their own declared [Min, Min+Range]
  bound. A wrong decode almost never respects this bound; a correct one
  always does.
- Optionally, the full curve for one channel so you can see the actual
  numbers or drop them into a spreadsheet/plot yourself.

Usage:
    python verify_antstate_channels.py path/to/extracted.res
    python verify_antstate_channels.py path/to/extracted.res --dump-vector 0 0
    python verify_antstate_channels.py path/to/extracted.res --dump-float 0 1 --csv out.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from game_asset_explorer.frostbite_animation_channels import (
    analyze_channel_group, find_channel_array_headers,
)
from game_asset_explorer.frostbite_state import is_antstate_resource


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("res_file", type=Path, help="An already-decompressed AntState .res file.")
    parser.add_argument("--dump-vector", nargs=2, type=int, metavar=("GROUP", "CHANNEL"),
                         help="Print the full sorted time/value curve for one real Vector channel.")
    parser.add_argument("--dump-float", nargs=2, type=int, metavar=("GROUP", "CHANNEL"),
                         help="Print the full sorted time/value curve for one real Float channel.")
    parser.add_argument("--csv", type=Path, help="Also write the dumped curve to this CSV file.")
    args = parser.parse_args()

    raw = args.res_file.read_bytes()

    if not is_antstate_resource(raw):
        print(f"{args.res_file} does not look like an AntState (GD.REFL) resource.")
        return 1

    groups = find_channel_array_headers(raw)
    if not groups:
        print("No channel-array header groups found in this file. "
              "Either it doesn't embed EclipseAnimationAsset-shaped data, "
              "or the header pattern didn't match -- worth flagging.")
        return 1

    print(f"Found {len(groups)} channel-array group(s) in {args.res_file.name}\n")

    reports = []
    for index, headers in enumerate(groups):
        report = analyze_channel_group(raw, headers)
        reports.append(report)
        real_float = [c for c in report.float_channels if not c.is_degenerate]
        real_vector = [c for c in report.vector_channels if not c.is_degenerate]
        bounds_ok_f = sum(1 for c in real_float if c.values_in_bounds())
        bounds_ok_v = sum(1 for c in real_vector if c.values_in_bounds())

        print(f"Group {index}  (header @ {report.header_addr:#x})")
        print(f"  Float channels : {len(report.float_channels)} decoded, "
              f"{report.float_decode_errors} decode errors, "
              f"{len(real_float)} carry real data, "
              f"{bounds_ok_f}/{len(real_float)} within their declared bounds")
        print(f"  Vector channels: {len(report.vector_channels)} decoded, "
              f"{report.vector_decode_errors} decode errors, "
              f"{len(real_vector)} carry real data, "
              f"{bounds_ok_v}/{len(real_vector)} within their declared bounds")

        total_real = len(real_float) + len(real_vector)
        total_ok = bounds_ok_f + bounds_ok_v
        if total_real == 0:
            verdict = "NO REAL CHANNELS FOUND -- nothing to verify in this group."
        elif total_ok == total_real and report.float_decode_errors + report.vector_decode_errors <= 2:
            verdict = "LOOKS GOOD -- consistent with the validated Sentinel-idle samples."
        else:
            verdict = "SUSPECT -- meaningful decode errors or out-of-bounds values here; treat this group's offsets as unverified."
        print(f"  Verdict: {verdict}\n")

    def dump_curve(kind: str, group_idx: int, channel_idx: int):
        report = reports[group_idx]
        channels = report.float_channels if kind == "float" else report.vector_channels
        real = [c for c in channels if not c.is_degenerate]
        if channel_idx >= len(real):
            print(f"Group {group_idx} only has {len(real)} real {kind} channel(s).")
            return
        channel = real[channel_idx]
        if kind == "float":
            pairs = sorted(zip(channel.times, channel.values))
            rows = [("time", "value")] + [(t, round(v, 6)) for t, v in pairs]
        else:
            pairs = sorted(zip(channel.times, channel.values))
            rows = [("time", "x", "y", "z")] + [
                (t, round(v[0], 6), round(v[1], 6), round(v[2], 6)) for t, v in pairs
            ]
        print(f"\n{kind.title()} channel {channel_idx} in group {group_idx} "
              f"(key_count={channel.key_count}, min={channel.min_value}, range={channel.range_value}):")
        for row in rows:
            print("  " + ", ".join(str(x) for x in row))
        if args.csv:
            with open(args.csv, "w", newline="") as handle:
                csv.writer(handle).writerows(rows)
            print(f"\nWrote {len(rows) - 1} rows to {args.csv}")

    if args.dump_vector:
        dump_curve("vector", *args.dump_vector)
    if args.dump_float:
        dump_curve("float", *args.dump_float)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
