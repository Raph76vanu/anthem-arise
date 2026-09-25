"""Evidence-only EXM pose audit for decoded curves and pose-space checks."""
from __future__ import annotations

from math import acos, degrees, dist
from statistics import median
from dataclasses import asdict

from .frostbite_animation import decode_anthem_animation_stream
from .frostbite_animation_playback import evaluate_pose_transforms
from .frostbite_ebx import decode_anthem_skeleton, decode_anthem_skeleton_bind_rotations
from .frostbite_rigamate import decode_rigamate_bone_mapping
from .frostbite_sequence import inspect_eclipse_sequences
from .frostbite_animation_playback import quaternion_multiply


def _angle(a, b):
    return round(degrees(2 * acos(min(1.0, abs(sum(x * y for x, y in zip(a, b)))))), 2)


def audit_exm_pose(animation: bytes, skeleton_record: bytes, bank: bytes) -> dict:
    """Account for every mapped bone rotation before any animation is rendered.

    The 48/64-bit key layouts have structural and cross-asset checks. The
    bank/bind composition and non-rotation channels remain experimental.
    """
    skeleton = decode_anthem_skeleton(skeleton_record)
    bind = decode_anthem_skeleton_bind_rotations(skeleton_record)
    if len(bind) != len(skeleton.joints):
        raise ValueError("The skeleton has no complete bind rotations.")
    reconstructed, _ = evaluate_pose_transforms(skeleton, bind, [], [], 0.0)
    bind_error = max(
        dist(a[2:], b[2:])
        for a, b in zip(skeleton.joints, reconstructed.joints)
    )
    clips = decode_anthem_animation_stream(animation)
    result = {"skeleton_bones": len(skeleton.joints),
              "bind_reconstruction_max_error": round(bind_error, 8), "clips": [],
              "sequences": [asdict(sequence) for sequence in inspect_eclipse_sequences(animation)]}
    world_bind = []
    for index, joint in enumerate(skeleton.joints):
        parent = joint[1]
        world_bind.append(quaternion_multiply(world_bind[parent], bind[index])
                          if 0 <= parent < index else bind[index])
    inverse = lambda q: (-q[0], -q[1], -q[2], q[3])
    for clip in clips:
        # Unlike the viewer's mapping, the audit retains constant channels.
        mapping = decode_rigamate_bone_mapping(bank, clip, skeleton, include_constants=True)
        if mapping is None:
            result["clips"].append({"name": clip.name, "error": "No complete named DOF mapping"})
            continue
        channels = []
        for channel_index, joint_index, default in zip(
            mapping.channel_indices, mapping.bone_indices, mapping.default_rotations,
        ):
            source = clip.quaternion_channels[channel_index]
            first = source.values[0] if source.values else None
            channels.append({
                "bone": skeleton.joints[joint_index][0],
                "channel_index": channel_index,
                "kind": "constant_decoded" if source.constant_encoding else "moving_candidate",
                "constant_bytes_hex": source.constant_encoding.hex() if source.constant_encoding else None,
                "key_count": len(source.times),
                "first_time": min(source.times) if source.times else None,
                "last_time": max(source.times) if source.times else None,
                "bank_vs_bind_degrees": _angle(default, bind[joint_index]),
                "bank_vs_inverse_bind_degrees": _angle(default, inverse(bind[joint_index])),
                "bank_vs_world_bind_degrees": _angle(default, world_bind[joint_index]),
                "bank_vs_inverse_world_bind_degrees": _angle(default, inverse(world_bind[joint_index])),
                "first_key_vs_bank_degrees": _angle(first, default) if first else None,
            })
        moving = [row for row in channels if row["kind"] == "moving_candidate"]
        constants = [row for row in channels if row["kind"] == "constant_decoded"]
        intervals = sorted({(row["first_time"], row["last_time"]) for row in moving})
        result["clips"].append({
            "name": clip.name, "frames": clip.frame_count,
            "quaternion_channels": len(clip.quaternion_channels),
            "mapped_moving": len(moving), "mapped_constants_decoded": len(constants),
            "helper_rotations_outside_skeleton": len(mapping.skipped_names),
            "vector_channels_not_applied": len(clip.vector_channels),
            "float_channels_not_applied": len(clip.float_channels),
            "moving_time_ranges": intervals,
            "median_bank_vs_bind_degrees": round(median(row["bank_vs_bind_degrees"] for row in channels), 2),
            "bank_bind_candidate_checks": {
                label: {"median_degrees": round(median(row[field] for row in channels), 2),
                        "joints_within_one_degree": sum(row[field] < 1 for row in channels)}
                for label, field in (
                    ("local_bind", "bank_vs_bind_degrees"),
                    ("inverse_local_bind", "bank_vs_inverse_bind_degrees"),
                    ("world_bind", "bank_vs_world_bind_degrees"),
                    ("inverse_world_bind", "bank_vs_inverse_world_bind_degrees"))},
            "status": "UNVERIFIED: pose space, translations, and sequence blending lack a reference",
            "bones": channels,
        })
    return result
