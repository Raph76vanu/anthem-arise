#!/usr/bin/env python3
"""Inspect verified Eclipse clips in an extracted Anthem AntState RES.

Usage:
    python verify_antstate_channels.py path/to/extracted.res
    python verify_antstate_channels.py path/to/extracted.res --clip 0 --dump-quaternion 3
    python verify_antstate_channels.py path/to/extracted.res --clip 0 --dump-vector 2 --csv curve.csv

Curve data and ChannelToDof identifiers are decoded. The identifiers are not
assigned to SkeletonAsset bones because that mapping lives in the referenced
Rigamate animation bank.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from game_asset_explorer.frostbite_animation import decode_anthem_animation_stream
from game_asset_explorer.frostbite_state import is_antstate_resource


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("res_file", type=Path)
    parser.add_argument("--clip", type=int, default=0)
    parser.add_argument("--dump-float", type=int, metavar="CHANNEL")
    parser.add_argument("--dump-vector", type=int, metavar="CHANNEL")
    parser.add_argument("--dump-quaternion", type=int, metavar="CHANNEL")
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()

    raw = args.res_file.read_bytes()
    if not is_antstate_resource(raw):
        print(f"{args.res_file} does not look like an AntState resource.")
        return 1
    clips = decode_anthem_animation_stream(raw)
    print(f"Decoded {len(clips)} Eclipse clip(s) from {args.res_file.name}:\n")
    for index, clip in enumerate(clips):
        dynamic = sum(bool(channel.times) for channel in clip.quaternion_channels)
        print(f"[{index}] {clip.name}")
        print(
            f"    frames={clip.frame_count}, float={len(clip.float_channels)}, "
            f"vector={len(clip.vector_channels)}, quaternion={len(clip.quaternion_channels)} "
            f"({dynamic} dynamic), DOF IDs={len(clip.dof_ids)}/{clip.channel_count}"
        )
    if not 0 <= args.clip < len(clips):
        print(f"\nClip {args.clip} does not exist.")
        return 1
    clip = clips[args.clip]
    request = next((
        (kind, index) for kind, index in (
            ("float", args.dump_float),
            ("vector", args.dump_vector),
            ("quaternion", args.dump_quaternion),
        ) if index is not None
    ), None)
    if request is None:
        print("\nCurve data is decoded, but playback needs the referenced Rigamate DOF-to-bone bank.")
        return 0
    kind, channel_index = request
    channels = getattr(clip, f"{kind}_channels")
    if not 0 <= channel_index < len(channels):
        print(f"\nClip {args.clip} has no {kind} channel {channel_index}.")
        return 1
    channel = channels[channel_index]
    if not channel.times:
        print(f"\n{kind.title()} channel {channel_index} uses an unresolved constant inline encoding.")
        return 1
    rows = [("time", "value")] if kind == "float" else [
        ("time", "x", "y", "z", "w") if kind == "quaternion" else ("time", "x", "y", "z")
    ]
    rows.extend((time, *value) if isinstance(value, tuple) else (time, value)
                for time, value in zip(channel.times, channel.values))
    for row in rows:
        print(", ".join(str(value) for value in row))
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(rows)
        print(f"\nWrote {len(rows) - 1} keys to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
