"""Repeatable EXM animation hypotheses, deliberately separate from playback.

The contact sheets are visual evidence, not a decoder-selection algorithm.
None of the hypotheses is assumed to be the actual Eclipse rotation codec.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from math import acos, cos, dist, sin, sqrt
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw, ImageFont

from .frostbite_animation import QuaternionChannel, decode_anthem_animation_stream
from .frostbite_animation_playback import (
    IDENTITY_QUATERNION, evaluate_pose_transforms, quaternion_multiply, quaternion_slerp,
)
from .frostbite_ebx import decode_anthem_skeleton, decode_anthem_skeleton_bind_rotations
from .frostbite_rigamate import decode_rigamate_bone_mapping
from .geometry import SkeletonData


@dataclass(frozen=True)
class Hypothesis:
    name: str
    rotation_mode: str = "bind_delta"
    source: str = "bank_delta"


HYPOTHESES = (
    Hypothesis("current"),
    Hypothesis("old_xyz_decoder", source="old_xyz"),
    Hypothesis("delta_before_bind", rotation_mode="delta_bind"),
    Hypothesis("raw_as_delta", source="raw"),
    Hypothesis("raw_as_absolute", rotation_mode="absolute", source="raw"),
    Hypothesis("inverse_bank_delta", source="inverse_delta"),
    Hypothesis("half_strength_delta", source="half_delta"),
)

PHASES = (0.0, 0.25, 0.5, 0.75)
TRACKED_JOINTS = (
    "Hips", "Pelvis", "Spine", "Spine1", "Spine2", "Neck", "Neck1", "Head", "HeadEnd",
    "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand", "RightShoulder", "RightArm",
    "RightForeArm", "RightHand", "LeftUpLeg", "LeftLeg", "LeftLowLeg", "LeftFoot",
    "LeftToe", "RightUpLeg", "RightLeg", "RightLowLeg", "RightFoot", "RightToe",
)
PANEL_WIDTH, PANEL_HEIGHT = 430, 360


def _conjugate(q):
    return (-q[0], -q[1], -q[2], q[3])


def _angular_distance(a, b):
    return 2 * acos(min(1.0, abs(sum(x * y for x, y in zip(a, b)))))


def infer_missing_w_signs(values, bank_default):
    """Choose per-key signs by continuity, anchored to the bank's rest value.

    This is a diagnostic heuristic. Angular speed alone cannot distinguish
    a genuine reversal from an incorrect W sign or identify a codec.
    """
    if not values:
        return ()
    first = values[0]
    costs = [_angular_distance(bank_default, first),
             _angular_distance(bank_default, (first[0], first[1], first[2], -first[3]))]
    paths = [(0,), (1,)]
    for previous, current in zip(values, values[1:]):
        sources = (previous, (*previous[:3], -previous[3]))
        targets = (current, (*current[:3], -current[3]))
        next_costs, next_paths = [], []
        for target in range(2):
            candidates = [costs[source] + _angular_distance(sources[source], targets[target])
                          for source in range(2)]
            source = min(range(2), key=lambda index: candidates[index])
            next_costs.append(candidates[source])
            next_paths.append(paths[source] + (target,))
        costs, paths = next_costs, next_paths
    best = paths[min(range(2), key=lambda index: costs[index])]
    return tuple((*value[:3], -value[3] if sign else value[3])
                 for value, sign in zip(values, best))


def invalid_component_counts(clip) -> tuple[int, int]:
    """Count raw words that cannot all be unit-quaternion XYZ components."""
    invalid = total = 0
    for channel in clip.quaternion_channels:
        for words in channel.packed_words:
            total += 1
            x, y, z = ((word - 32768.0) / 32767.0 for word in words)
            if x * x + y * y + z * z > 1.000001:
                invalid += 1
    return invalid, total


def _channels(clip, mapping, hypothesis: Hypothesis) -> list[QuaternionChannel]:
    if hypothesis.source == "old_xyz":
        channels = []
        for index, default in zip(mapping.channel_indices, mapping.default_rotations):
            source = clip.quaternion_channels[index]
            values = []
            for words in source.packed_words:
                xyz = tuple((word - 32768.0) / 32767.0 for word in words)
                w = sqrt(max(0.0, 1.0 - sum(x * x for x in xyz)))
                norm = sqrt(sum(x * x for x in xyz) + w * w)
                values.append(tuple(x / norm for x in (*xyz, w)))
            channels.append(QuaternionChannel(source.times, tuple(
                quaternion_multiply(_conjugate(default), value) for value in values
            )))
        return channels
    if hypothesis.source == "bank_delta":
        return mapping.pose_channels(clip)
    if hypothesis.source == "half_delta":
        return [QuaternionChannel(ch.times, tuple(quaternion_slerp(IDENTITY_QUATERNION, q, 0.5)
                                                 for q in ch.values))
                for ch in mapping.pose_channels(clip)]
    if hypothesis.source == "inverse_delta":
        return [QuaternionChannel(ch.times, tuple(_conjugate(q) for q in ch.values))
                for ch in mapping.pose_channels(clip)]
    sources = [clip.quaternion_channels[i] for i in mapping.channel_indices]
    if hypothesis.source == "raw":
        return sources
    raise ValueError(f"Unknown channel hypothesis: {hypothesis.source}")


def _tracked_indices(skeleton: SkeletonData) -> list[int]:
    names = set(TRACKED_JOINTS)
    return [i for i, entry in enumerate(skeleton.joints) if entry[0] in names]


def _project(joint, center, scale):
    _, _, px, py, pz = joint
    yaw, pitch = -0.6, -0.2
    x, z = px * cos(yaw) + pz * sin(yaw), -px * sin(yaw) + pz * cos(yaw)
    y = py * cos(pitch) - z * sin(pitch)
    return (int(PANEL_WIDTH / 2 + (x - center[0]) * scale),
            int(PANEL_HEIGHT / 2 - (y - center[1]) * scale))


def _render_pose(draw, skeleton: SkeletonData, indices: list[int], x0: int, y0: int,
                 center, scale: float) -> None:
    visible = set(indices)
    coords = {i: _project(skeleton.joints[i], center, scale) for i in indices}
    for index in indices:
        parent = skeleton.joints[index][1]
        while parent >= 0 and parent not in visible:
            parent = skeleton.joints[parent][1]
        if parent in coords:
            a, b = coords[parent], coords[index]
            draw.line((x0 + a[0], y0 + a[1], x0 + b[0], y0 + b[1]),
                      fill="#61abd6", width=3)
    for px, py in coords.values():
        draw.ellipse((x0 + px - 3, y0 + py - 3, x0 + px + 3, y0 + py + 3),
                     fill="#ffcd75")


def _shared_camera(bind: SkeletonData, poses: list[list[SkeletonData]], indices: list[int]):
    all_poses = [bind, *(pose for variant in poses for pose in variant)]
    projected = []
    for pose in all_poses:
        for i in indices:
            x, y = _project(pose.joints[i], (0, 0), 1)
            projected.append((x - PANEL_WIDTH / 2, PANEL_HEIGHT / 2 - y))
    xs, ys = [p[0] for p in projected], [p[1] for p in projected]
    span = max((max(xs) - min(xs)) / (PANEL_WIDTH - 44),
               (max(ys) - min(ys)) / (PANEL_HEIGHT - 55), 0.001)
    # The same framing applies to every cell within a clip's contact sheet.
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2), 1 / span


def _contact_sheet(path: Path, clip_name: str, skeleton: SkeletonData,
                   poses: list[list[SkeletonData]], indices: list[int]) -> None:
    labels = ["bind pose", *(mode.name for mode in HYPOTHESES)]
    image = Image.new("RGB", (PANEL_WIDTH * len(PHASES), PANEL_HEIGHT * len(labels)), "#151922")
    draw = ImageDraw.Draw(image)
    center, scale = _shared_camera(skeleton, poses, indices)
    font = ImageFont.load_default()
    for row, name in enumerate(labels):
        for col, phase in enumerate(PHASES):
            x0, y0 = col * PANEL_WIDTH, row * PANEL_HEIGHT
            draw.rectangle((x0, y0, x0 + PANEL_WIDTH - 1, y0 + PANEL_HEIGHT - 1), outline="#455260")
            draw.text((x0 + 10, y0 + 8), f"{name}   phase {phase:.2f}", fill="#f4f4f4", font=font)
            pose = skeleton if row == 0 else poses[row - 1][col]
            _render_pose(draw, pose, indices, x0, y0, center, scale)
    image.save(path)


def diagnose(res_path: Path, skeleton_path: Path, bank_path: Path, output: Path) -> list[dict]:
    """Write four reproducible comparison sheets and CSV; never modify the inputs."""
    output.mkdir(parents=True, exist_ok=True)
    skeleton_raw = skeleton_path.read_bytes()
    skeleton = decode_anthem_skeleton(skeleton_raw)
    bind_rotations = decode_anthem_skeleton_bind_rotations(skeleton_raw)
    bank = bank_path.read_bytes()
    clips = decode_anthem_animation_stream(res_path.read_bytes())
    indices = _tracked_indices(skeleton)
    if not indices or len(bind_rotations) != len(skeleton.joints):
        raise ValueError("The EXM skeleton is incomplete or has no tracked joints.")
    results: list[dict] = []
    raw_validity: list[tuple[str, int, int]] = []
    for clip_number, clip in enumerate(clips, 1):
        invalid, total = invalid_component_counts(clip)
        raw_validity.append((clip.name, invalid, total))
        mapping = decode_rigamate_bone_mapping(bank, clip, skeleton)
        if mapping is None:
            raise ValueError(f"No named EXM bone mapping for clip {clip_number}: {clip.name}")
        all_poses: list[list[SkeletonData]] = []
        for mode in HYPOTHESES:
            channels = _channels(clip, mapping, mode)
            def evaluate(phase):
                return evaluate_pose_transforms(
                    skeleton, bind_rotations, list(mapping.bone_indices), channels, phase,
                    rotation_mode=mode.rotation_mode,
                )[0]
            pictures = [evaluate(phase) for phase in PHASES]
            all_poses.append(pictures)
            samples = [evaluate(i / 32) for i in range(33)]
            shifts = [dist(pose.joints[j][2:], skeleton.joints[j][2:])
                      for pose in samples for j in indices]
            steps = [dist(a.joints[j][2:], b.joints[j][2:])
                     for a, b in zip(samples, samples[1:]) for j in indices]
            results.append({
                "clip": clip.name, "variant": mode.name, "mapped_rotations": len(channels),
                "skipped_helper_channels": len(mapping.skipped_names),
                "median_bind_displacement": round(median(shifts), 4),
                "max_bind_displacement": round(max(shifts), 4),
                "max_step_displacement": round(max(steps), 4),
            })
        filename = f"clip_{clip_number:02d}_comparison.png"
        _contact_sheet(output / filename, clip.name, skeleton, all_poses, indices)
    with (output / "measurements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    report_lines = [
        "# EXM animation comparison", "",
        f"{len(clips)} decoded clips × {len(HYPOTHESES)} hypotheses; "
        f"{len(skeleton.joints)} skeleton joints. Each PNG includes the bind pose.", "",
        "Smaller displacement does not imply a more accurate decode. "
        "These tests cannot select a correct six-byte codec without known poses.", "",
        "## Raw component validity", "",
        "If all three raw words represented direct unit-quaternion XYZ,",
        "their squares must add to at most one. The old preview clamped",
        "and normalized exceptions. The current decoder restores the omitted",
        "component selected by two low bits, with a smaller component range.", "",
        "| Clip | Impossible XYZ keys | Total dynamic keys | Fraction |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, invalid, total in raw_validity:
        report_lines.append(f"| {name} | {invalid} | {total} | {invalid / total:.1%} |")
    report_lines.extend((
        "", "## Visual hypotheses", "",
        "`current` uses the 48-bit smallest-three layout and `old_xyz_decoder`",
        "shows the former interpretation. Other rows test composition choices.", "",
        "| Clip | Hypothesis | Median distance from bind | Largest single step |",
        "| --- | --- | ---: | ---: |",
    ))
    for row in results:
        report_lines.append(
            f"| {row['clip']} | {row['variant']} | "
            f"{row['median_bind_displacement']:.4f} | {row['max_step_displacement']:.4f} |"
        )
    report_lines.extend((
        "", "**Next step:** compare these snapshots against a known in-game pose or a",
        "known-correct animation export. Smooth motion and a valid unit",
        "quaternion are strong checks but do not establish game-accurate posing.", "",
    ))
    (output / "report.md").write_text("\n".join(report_lines), encoding="utf-8")
    (output / "README.txt").write_text(
        "EXM ANIMATION EXPERIMENT (no variant is verified)\n\n"
        "Each image is one Eclipse clip. Rows are explicit hypotheses; columns are\n"
        "four phases. The first row shows the untouched bind pose. All cells on\n"
        "one sheet use the same camera and scale. Only the main body joints are\n"
        "drawn; all 182 joints are still evaluated.\n\n"
        "current = smallest-three 48-bit playback; old_xyz_decoder = prior\n"
        "incorrect XYZ assumption; delta_before_bind changes multiplication order;\n"
        "raw_as_delta ignores Rigamate defaults; raw_as_absolute treats stored\n"
        "quaternions as local orientations; inverse_bank_delta flips the bank\n"
        "offset;\n"
        "half_strength_delta scales motion to 50% for sensitivity inspection;\n"
        "it is not a proposed codec. These four clips all use a shared 0-to-end\n"
        "time range already, so a second shared-clock row would be identical.\n\n"
        "Measurements are descriptive only. Small movements or a natural-looking\n"
        "still pose do NOT prove game-accurate animation. Game-accurate\n"
        "reference poses are needed to validate the decoder.\n",
        encoding="utf-8",
    )
    return results
