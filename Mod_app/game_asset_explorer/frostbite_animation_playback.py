"""Turn decoded animation channels (frostbite_animation_channels.py) into an
actual animated skeleton pose, using a best-guess channel-to-bone mapping.

This module is deliberately upfront about what is proven versus guessed:

PROVEN (validated on real Anthem samples):
- Float/Vector/Quaternion channel decoding itself (times, values, bounds).
- Bind-pose rotation reconstruction (``evaluate_pose`` with no animation
  channels exactly reproduces the skeleton's own bind-pose positions, to
  floating-point precision -- this exposed and fixed a real double-rotation
  bug in how a joint's bind offset combined with its parent's rotation).
- The rotation composition order used inside a single channel
  (``euler_xyz_to_quaternion``): confirmed intrinsic Z, then X, then Y,
  by testing all 6 possible orders against physical plausibility checks
  (hand below shoulder, feet below hips, head above hips) across a full
  idle animation loop -- this order is the only one that passes every
  check at every sampled phase (140/140); the next best passes 139/140,
  and the naive X-Y-Z guess this project started with passes only 1/20.
- Non-animated joints use their own decoded bind-pose local rotation
  (``frostbite_ebx.decode_anthem_skeleton_bind_rotations``), not an
  identity or their parent's rotation -- confirmed necessary because some
  joints (e.g. arm bones) have a real ~46 degree bind rotation consistent
  with a T-pose bind skeleton, and would otherwise visibly snap to a wrong
  pose even when correctly unanimated.

GUESSED, to be refined by how the result actually looks once rendered:
- Which quaternion channel index corresponds to which skeleton joint
  (``guess_bone_mapping``). No stored mapping was found in the animation
  file itself; this assumes channels correspond, in order, to non-helper
  joints as they appear in a curated "primary rig" order.
- Whether an animated joint's local rotation is bind_rotation * channel
  rotation (composed, assumed) versus something else.

Channels flagged unhealthy by frostbite_animation_channels' corruption
checks (duplicate timestamps, values pinned at the int16 boundary) can be
excluded from playback via ``filter_healthy_channels`` -- this doesn't fix
the underlying corruption, but stops one bad channel on a high-leverage
joint from dragging its entire downstream subtree into garbage rotation.

None of this is fatal -- it's exactly the kind of thing a rendered pose lets
you fix by eye far faster than by reading more bytes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .frostbite_animation_channels import ChannelHealth, FloatChannel, QuaternionChannel, VectorChannel
from .geometry import SkeletonData

HELPER_KEYWORDS = ("camera", "trajectory", "ground", "connect", "climb", "ai", "reference")


# ---------------------------------------------------------------------------
# Minimal quaternion helpers (no external dependency).
# ---------------------------------------------------------------------------

Quaternion = tuple[float, float, float, float]  # (x, y, z, w)

IDENTITY_QUATERNION: Quaternion = (0.0, 0.0, 0.0, 1.0)


def quaternion_multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quaternion_rotate_vector(q: Quaternion, v: tuple[float, float, float]) -> tuple[float, float, float]:
    qv = (q[0], q[1], q[2], q[3])
    vq = (v[0], v[1], v[2], 0.0)
    conj = (-q[0], -q[1], -q[2], q[3])
    result = quaternion_multiply(quaternion_multiply(qv, vq), conj)
    return (result[0], result[1], result[2])


def axis_angle_to_quaternion(axis: tuple[float, float, float], angle: float) -> Quaternion:
    """axis MUST be unit-length -- this does not normalize it. Passing a
    non-unit axis produces a non-unit quaternion, which silently corrupts
    any later inverse-rotation computation (e.g. bind_local_offset in
    evaluate_pose) since conjugate-only inversion is only valid for unit
    quaternions. Found the hard way while writing this module's tests."""
    half = angle / 2.0
    s = math.sin(half)
    return (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(half))


def euler_xyz_to_quaternion(rx: float, ry: float, rz: float) -> Quaternion:
    """Rotation order: intrinsic Z, then X, then Y (qy * qx * qz).

    CONFIRMED via systematic testing across all 6 possible orders, done
    AFTER fixing the bind-rotation FK bug (an earlier comparison, done
    before that fix, was testing on broken foundations and its conclusion
    doesn't hold). With the FK math correct, this order scores 140/140 on
    physical plausibility checks (hand stays below shoulder, forearm below
    shoulder, feet below hips, head above hips) across an entire idle
    loop -- the only order that passes all checks at every sampled phase.
    The previous X-Y-Z guess scored 1/20 on the same test once the FK fix
    was in place, i.e. it was the wrong order, not merely imprecise.
    Runner-up Y-X-Z scored 139/140; every other order scored far lower.
    """
    qx = axis_angle_to_quaternion((1.0, 0.0, 0.0), rx)
    qy = axis_angle_to_quaternion((0.0, 1.0, 0.0), ry)
    qz = axis_angle_to_quaternion((0.0, 0.0, 1.0), rz)
    return quaternion_multiply(quaternion_multiply(qy, qx), qz)


# ---------------------------------------------------------------------------
# Keyframe interpolation.
# ---------------------------------------------------------------------------

def _bracket(times: tuple[int, ...], t: float):
    """Return (i0, i1, blend) for the two keyframes (by array position, not
    necessarily sorted times -- channels are stored in an arbitrary order)
    surrounding query time t, using nearest-neighbour outside the range."""
    order = sorted(range(len(times)), key=lambda i: times[i])
    sorted_times = [times[i] for i in order]
    if t <= sorted_times[0]:
        return order[0], order[0], 0.0
    if t >= sorted_times[-1]:
        return order[-1], order[-1], 0.0
    for k in range(len(sorted_times) - 1):
        if sorted_times[k] <= t <= sorted_times[k + 1]:
            span = sorted_times[k + 1] - sorted_times[k]
            blend = 0.0 if span == 0 else (t - sorted_times[k]) / span
            return order[k], order[k + 1], blend
    return order[-1], order[-1], 0.0


def _channel_local_time(times: tuple[int, ...], phase: float) -> float:
    """Map a normalized loop phase [0, 1] to this channel's own time range.

    Channels have wildly different observed max times (confirmed on real
    data: from a few hundred to 65535), which is expected if a channel
    simply stops needing new keyframes once its value settles -- but it
    means sharing one global absolute time across all channels makes any
    channel with a small range freeze for almost the entire loop the
    moment its own last keyframe passes, while a few outlier channels
    dominate the loop length. Normalizing per-channel avoids that; the
    trade-off is that channels meant to stay exactly in sync with very
    different individual ranges may drift slightly out of sync with each
    other. Not proven optimal, just clearly better than freezing.
    """
    if not times:
        return 0.0
    lo, hi = min(times), max(times)
    return lo + phase * (hi - lo)


def sample_float_channel(channel: FloatChannel, phase: float) -> float:
    t = _channel_local_time(channel.times, phase)
    i0, i1, blend = _bracket(channel.times, t)
    return channel.values[i0] + (channel.values[i1] - channel.values[i0]) * blend


def sample_vector_channel(channel: VectorChannel, phase: float) -> tuple[float, float, float]:
    t = _channel_local_time(channel.times, phase)
    i0, i1, blend = _bracket(channel.times, t)
    v0, v1 = channel.values[i0], channel.values[i1]
    return tuple(v0[axis] + (v1[axis] - v0[axis]) * blend for axis in range(3))


ANGLE_SCALE = math.pi / 32768.0  # maps the signed 16-bit range to roughly +/-pi; unverified exact constant


def quaternion_slerp(a: Quaternion, b: Quaternion, blend: float) -> Quaternion:
    """Shortest-arc spherical interpolation between two unit quaternions."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    dot = ax * bx + ay * by + az * bz + aw * bw
    if dot < 0.0:
        bx, by, bz, bw = -bx, -by, -bz, -bw
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        # Nearly identical rotations: linear interpolation avoids a division
        # by ~0 in the general formula below and is visually indistinguishable.
        result = (
            ax + (bx - ax) * blend, ay + (by - ay) * blend,
            az + (bz - az) * blend, aw + (bw - aw) * blend,
        )
        length = math.sqrt(sum(c * c for c in result)) or 1.0
        return tuple(c / length for c in result)
    theta_0 = math.acos(dot)
    theta = theta_0 * blend
    sin_theta = math.sin(theta)
    sin_theta_0 = math.sin(theta_0)
    s0 = math.cos(theta) - dot * sin_theta / sin_theta_0
    s1 = sin_theta / sin_theta_0
    return (
        s0 * ax + s1 * bx, s0 * ay + s1 * by,
        s0 * az + s1 * bz, s0 * aw + s1 * bw,
    )


def _unwrap_component(raw: int, reference: int, period: int = 65536) -> int:
    """Choose the representation of raw (raw, raw-period, or raw+period)
    closest to reference, so consecutive samples don't jump by a full period
    when the true motion crosses the int16 wrap boundary."""
    candidates = (raw - period, raw, raw + period)
    return min(candidates, key=lambda c: abs(c - reference))


def sample_quaternion_channel(channel: QuaternionChannel, phase: float) -> Quaternion:
    t = _channel_local_time(channel.times, phase)
    i0, i1, blend = _bracket(channel.times, t)
    raw0 = channel.raw_xyz[i0]
    if i0 == i1:
        rx, ry, rz = (component * ANGLE_SCALE for component in raw0)
        return euler_xyz_to_quaternion(rx, ry, rz)
    raw1 = tuple(_unwrap_component(raw1_c, raw0_c) for raw1_c, raw0_c in zip(channel.raw_xyz[i1], raw0))
    q0 = euler_xyz_to_quaternion(*(c * ANGLE_SCALE for c in raw0))
    q1 = euler_xyz_to_quaternion(*(c * ANGLE_SCALE for c in raw1))
    return quaternion_slerp(q0, q1, blend)


# ---------------------------------------------------------------------------
# Naive channel-to-bone mapping.
# ---------------------------------------------------------------------------

def guess_bone_mapping(
    skeleton: SkeletonData, channel_count: int, primary_rig_order: list[int] | None = None,
) -> list[int]:
    """Best-guess mapping of quaternion channel index -> skeleton joint index.

    UNVERIFIED -- see module docstring. If primary_rig_order is given (from
    frostbite_ebx.decode_anthem_primary_rig_joint_order), channels are
    assigned in that order instead of raw skeleton order. This matters: a
    real clip had only 30 channels for a 182-joint skeleton, and raw
    skeleton order exhausts that budget on the left hand's individual
    fingers before ever reaching the right arm, leaving it completely
    unanimated (confirmed via a pose debug dump -- every right-arm joint
    showed no assigned channel while its left-side mirror did). The
    primary-rig order covers both arms/legs before any per-finger detail.
    """
    if primary_rig_order:
        candidates = [
            index for index in primary_rig_order
            if not any(keyword in skeleton.joints[index][0].lower() for keyword in HELPER_KEYWORDS)
        ]
        if candidates:
            return candidates[:channel_count]
    candidates = [
        index for index, joint in enumerate(skeleton.joints)
        if not any(keyword in joint[0].lower() for keyword in HELPER_KEYWORDS)
    ]
    return candidates[:channel_count]


def filter_healthy_channels(
    bone_mapping: list[int], channels: list[QuaternionChannel], health: list[ChannelHealth],
) -> tuple[list[int], list[QuaternionChannel]]:
    """Drop channels flagged unhealthy (duplicate timestamps, values pinned
    at the int16 boundary, etc. -- see analyze_quaternion_channel_health)
    before handing data to evaluate_pose.

    Rationale: a corrupted channel on a high-leverage joint (confirmed on
    real data: Spine2, the shoulders/neck/head's shared parent, with 50
    duplicate timestamps) drags its entire downstream subtree into
    whatever garbage rotation it produces -- the corruption is usually
    small in itself but has outsized visual impact purely from where it
    sits in the hierarchy. Excluding it makes that joint fall back to its
    bind pose (stationary but harmless) via evaluate_pose's existing
    "no channel -> bind_rotation" behaviour, rather than spreading garbage
    to everything below it. This does not fix the corruption -- it trades
    "some joints don't move" for "the rest of the body isn't dragged along
    with the ones that are wrong," which is the more useful failure mode
    while the underlying corruption is still being investigated.
    """
    health_by_channel_index = {h.channel_index: h.healthy for h in health}
    filtered_mapping = []
    filtered_channels = []
    for channel_index, joint_index in enumerate(bone_mapping):
        if health_by_channel_index.get(channel_index, True):
            filtered_mapping.append(joint_index)
            filtered_channels.append(channels[channel_index])
    return filtered_mapping, filtered_channels


# ---------------------------------------------------------------------------
# Pose evaluation (forward kinematics from bind-pose positions).
# ---------------------------------------------------------------------------

def evaluate_pose(
    skeleton: SkeletonData,
    bind_rotations: list[Quaternion],
    bone_mapping: list[int],
    quaternion_channels: list[QuaternionChannel],
    phase: float,
) -> SkeletonData:
    """Produce a new SkeletonData with world-space joint positions at a
    normalized point in the loop, phase in [0, 1].

    phase is mapped to each channel's OWN time range independently (see
    _channel_local_time) rather than one shared absolute time, since
    channels have very different observed max times and sharing one
    timeline made short-range channels freeze for most of the loop.

    bind_rotations (from frostbite_ebx.decode_anthem_skeleton_bind_rotations,
    same order as skeleton.joints) is each joint's LOCAL rest orientation
    relative to its parent. This matters because animation channels give a
    rotation relative to that rest orientation, not relative to global
    identity -- a joint whose rest pose is far from identity (confirmed:
    arm bones in this rig have a real ~46 degree bind rotation, consistent
    with a T-pose bind skeleton) would otherwise visibly snap toward
    identity instead of its natural pose. For an animated joint the local
    rotation is bind_rotation * channel_rotation (composed, not replaced);
    for a non-animated joint it's just bind_rotation (not identity).
    UNVERIFIED: whether channel data is truly a delta relative to bind
    (assumed here) versus something else -- this is the next thing to
    confirm visually.
    """
    joints = skeleton.joints

    # Precompute each joint's ACCUMULATED bind-pose world rotation (root to
    # this joint, bind rotations only, no animation). Needed to convert the
    # naive world-space offset (child_bind_pos - parent_bind_pos) into the
    # parent's own local rest frame -- see the bug this fixes, below.
    world_bind_rotation: list[Quaternion] = [IDENTITY_QUATERNION] * len(joints)
    for index, (_name, parent, *_rest) in enumerate(joints):
        bind_rotation = bind_rotations[index] if index < len(bind_rotations) else IDENTITY_QUATERNION
        parent_bind_rotation = world_bind_rotation[parent] if 0 <= parent < index else IDENTITY_QUATERNION
        world_bind_rotation[index] = quaternion_multiply(parent_bind_rotation, bind_rotation)

    bind_local_offset: list[tuple[float, float, float]] = []
    for index, (_name, parent, x, y, z) in enumerate(joints):
        if 0 <= parent < len(joints):
            _, _, px, py, pz = joints[parent]
            world_space_offset = (x - px, y - py, z - pz)
            # BUG THIS FIXES: world_space_offset already reflects the bind
            # pose's fully-resolved rotation (it's computed directly from
            # bind positions). The FK loop below rotates offsets by the
            # parent's rotation to place children correctly when that
            # rotation changes from its bind value -- but applying that to
            # an already-world-space offset double-applies the parent's OWN
            # bind rotation. Confirmed on real data: with bind rotations
            # applied and zero animation, this produced a 2.28 unit
            # position error at a leaf joint (LeftToeEnd) against the
            # actual bind pose -- i.e. the bug was there even with no
            # animation involved, not a data quality issue. Un-rotating by
            # the parent's own accumulated bind rotation first expresses
            # the offset in the parent's local rest frame, so that
            # reapplying the (possibly now-different, animated) parent
            # rotation reconstructs the correct position, and reconstructs
            # EXACTLY the bind pose when rotation is unchanged.
            parent_bind_rotation = world_bind_rotation[parent]
            inverse_parent_bind = (-parent_bind_rotation[0], -parent_bind_rotation[1],
                                    -parent_bind_rotation[2], parent_bind_rotation[3])
            bind_local_offset.append(quaternion_rotate_vector(inverse_parent_bind, world_space_offset))
        else:
            bind_local_offset.append((x, y, z))

    channel_for_joint: dict[int, QuaternionChannel] = {
        joint_index: quaternion_channels[channel_index]
        for channel_index, joint_index in enumerate(bone_mapping)
        if channel_index < len(quaternion_channels)
    }

    world_rotation: list[Quaternion] = [IDENTITY_QUATERNION] * len(joints)
    world_position: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)] * len(joints)

    for index, (_name, parent, x, y, z) in enumerate(joints):
        bind_rotation = bind_rotations[index] if index < len(bind_rotations) else IDENTITY_QUATERNION
        channel = channel_for_joint.get(index)
        if channel is not None:
            local_rotation = quaternion_multiply(bind_rotation, sample_quaternion_channel(channel, phase))
        else:
            local_rotation = bind_rotation

        if 0 <= parent < len(joints) and parent < index:
            parent_rotation = world_rotation[parent]
            parent_position = world_position[parent]
        else:
            parent_rotation = IDENTITY_QUATERNION
            parent_position = (0.0, 0.0, 0.0)

        this_rotation = quaternion_multiply(parent_rotation, local_rotation)
        rotated_offset = quaternion_rotate_vector(parent_rotation, bind_local_offset[index])
        this_position = (
            parent_position[0] + rotated_offset[0],
            parent_position[1] + rotated_offset[1],
            parent_position[2] + rotated_offset[2],
        )
        world_rotation[index] = this_rotation
        world_position[index] = this_position

    new_joints = [
        (joint[0], joint[1], *world_position[index])
        for index, joint in enumerate(joints)
    ]
    return SkeletonData(skeleton.name, new_joints)


def quaternion_angle_degrees(q: Quaternion) -> float:
    """Rotation angle of q from identity, in degrees (0-180)."""
    return math.degrees(2 * math.acos(min(1.0, max(-1.0, abs(q[3])))))


def quaternion_to_euler_degrees_xyz(q: Quaternion) -> tuple[float, float, float]:
    """Decompose a quaternion into approximate intrinsic X-Y-Z Euler angles
    in degrees, for human-readable debugging only -- not used anywhere in
    the actual pose math, which works entirely in quaternions to avoid
    exactly this kind of order-dependent ambiguity."""
    x, y, z, w = q
    sinr_cosp = 2 * (w * x + y * z)
    cosr_cosp = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)
    sinp = 2 * (w * y - z * x)
    pitch = math.asin(min(1.0, max(-1.0, sinp)))
    siny_cosp = 2 * (w * z + x * y)
    cosy_cosp = 1 - 2 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


@dataclass(frozen=True)
class JointPoseDebug:
    index: int
    name: str
    parent_name: str
    channel_index: int | None
    channel_key_count: int | None
    bind_angle_deg: float
    channel_angle_deg: float | None
    """Angle of the raw channel rotation from identity -- NOT how much the
    joint moves during playback (that's sway_deg). A channel can have a
    large, mostly-constant channel_angle_deg (its own rest value happens to
    sit far from identity) while barely swaying at all."""
    sway_deg: float | None
    """How much this joint's LOCAL rotation actually changes across the
    loop, relative to its own value at phase=0 -- the number that actually
    answers "does this bone move a sensible amount for idle." Unaffected by
    whether bind rotation is composed in, since composing with a constant
    on one side doesn't change the relative angle between two rotations."""
    local_angle_deg: float
    local_euler_deg: tuple[float, float, float]
    world_position: tuple[float, float, float]


def describe_pose_debug(
    skeleton: SkeletonData,
    bind_rotations: list[Quaternion],
    bone_mapping: list[int],
    quaternion_channels: list[QuaternionChannel],
    phase: float,
) -> list[JointPoseDebug]:
    """Per-joint diagnostic snapshot at a given phase: which channel (if
    any) drives this joint, its bind rotation, the channel's own rotation
    and actual sway, and the resulting local/world state. Meant to be
    written to a file for direct inspection when a rendered pose looks
    wrong -- much faster to spot a mis-mapped or still-corrupted channel,
    or a compounding-down-a-long-chain effect, this way than by describing
    a screenshot back and forth.
    """
    joints = skeleton.joints
    channel_for_joint: dict[int, tuple[int, QuaternionChannel]] = {
        joint_index: (channel_index, quaternion_channels[channel_index])
        for channel_index, joint_index in enumerate(bone_mapping)
        if channel_index < len(quaternion_channels)
    }
    pose = evaluate_pose(skeleton, bind_rotations, bone_mapping, quaternion_channels, phase)

    def relative_angle_deg(a: Quaternion, b: Quaternion) -> float:
        ax, ay, az, aw = a
        inv_a = (-ax, -ay, -az, aw)
        return quaternion_angle_degrees(quaternion_multiply(b, inv_a))

    records = []
    for index, (name, parent, *_rest) in enumerate(joints):
        parent_name = joints[parent][0] if 0 <= parent < len(joints) else "(none)"
        bind_rotation = bind_rotations[index] if index < len(bind_rotations) else IDENTITY_QUATERNION
        entry = channel_for_joint.get(index)
        channel_index = entry[0] if entry else None
        channel_key_count = entry[1].key_count if entry else None
        channel_angle_deg = None
        sway_deg = None
        if entry is not None:
            channel = entry[1]
            channel_rotation_now = sample_quaternion_channel(channel, phase)
            channel_rotation_start = sample_quaternion_channel(channel, 0.0)
            channel_angle_deg = quaternion_angle_degrees(channel_rotation_now)
            sway_deg = relative_angle_deg(channel_rotation_start, channel_rotation_now)
            local_rotation = quaternion_multiply(bind_rotation, channel_rotation_now)
        else:
            local_rotation = bind_rotation
        records.append(JointPoseDebug(
            index=index,
            name=name,
            parent_name=parent_name,
            channel_index=channel_index,
            channel_key_count=channel_key_count,
            bind_angle_deg=quaternion_angle_degrees(bind_rotation),
            channel_angle_deg=channel_angle_deg,
            sway_deg=sway_deg,
            local_angle_deg=quaternion_angle_degrees(local_rotation),
            local_euler_deg=quaternion_to_euler_degrees_xyz(local_rotation),
            world_position=pose.joints[index][2:5],
        ))
    return records


def format_pose_debug(records: list[JointPoseDebug], phase: float) -> str:
    """Render describe_pose_debug's output as a plain-text table."""
    lines = [f"Pose debug at phase={phase:.4f}", ""]
    header = (
        f"{'idx':>4} {'bone':<28} {'parent':<20} {'chan':>5} {'keys':>5} "
        f"{'bind°':>7} {'chan°':>7} {'sway°':>7} {'local°':>7}  {'euler(x,y,z)°':<28} world_pos"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for r in records:
        chan = str(r.channel_index) if r.channel_index is not None else "-"
        keys = str(r.channel_key_count) if r.channel_key_count is not None else "-"
        chan_angle = f"{r.channel_angle_deg:.1f}" if r.channel_angle_deg is not None else "-"
        sway = f"{r.sway_deg:.1f}" if r.sway_deg is not None else "-"
        euler = ",".join(f"{v:.1f}" for v in r.local_euler_deg)
        pos = ",".join(f"{v:.3f}" for v in r.world_position)
        lines.append(
            f"{r.index:>4} {r.name:<28} {r.parent_name:<20} {chan:>5} {keys:>5} "
            f"{r.bind_angle_deg:>7.1f} {chan_angle:>7} {sway:>7} {r.local_angle_deg:>7.1f}  {euler:<28} {pos}"
        )
    return "\n".join(lines)
