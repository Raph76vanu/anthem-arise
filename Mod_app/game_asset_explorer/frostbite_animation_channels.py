"""Decode EclipseAnimationFloatChannel / EclipseAnimationVectorChannel /
EclipseAnimationQuaternionChannel keyframe data out of an already-decoded
Anthem AntState resource (see frostbite_state.py for the container format
this sits inside).

Everything here is validated against real Anthem samples across at least
two independent animation clips (an idle and a crouch), cross-checked
multiple ways:

1. Every Float/Vector value is checked to fall inside its own channel's
   declared [Min, Min + Range] bound. Read this caveat carefully: on its
   own this is a WEAK check, since min + byte/255*range is trivially
   in-bounds for any byte 0-255 -- a wrong offset reading pure garbage
   still "passes" it. It only became strong evidence once a real sample's
   maximum landed on Min + Range to four decimal places, and once a real
   offset bug (below) was found and fixed by a much stronger check.
2. Vector3 channels (bone-offset-like data) decode into visibly smooth,
   continuous curves when sorted by time. Some Float channels do not look
   smooth even when correctly decoded -- they appear to carry
   procedural/trigger-style parameters rather than spatial curves.
3. Quaternion channel times are checked for a low proportion of exact-zero
   values (`_quaternion_channel_looks_valid`) -- this is what actually
   caught the real offset bug below, since garbage data produces a highly
   distinctive run of zero/duplicate times that bounds-checking alone
   cannot see. A validated Quaternion channel's raw x/y/z values were also
   confirmed to move together smoothly, including correctly wrapping
   through the signed 16-bit boundary (e.g. 32599 -> -32543 is a small
   continuous step once wraparound is accounted for, not a jump).

Fixed (real bug, not just a caveat): times_off/values_off stored in a
channel struct are relative to the *enclosing GD.DATA block's own file
offset*, not to the start of the whole resource. Every decode function
here takes a block_offset parameter for this reason -- passing the wrong
one (or 0, when the caller doesn't know better) will read the wrong bytes
silently for Float/Vector (see caveat 1) and will visibly fail the
Quaternion time check.

Confirmed encoding, per keyframe:

- Time: one 16-bit BIG-ENDIAN integer, for all three channel types.
- Float channel value: one byte, dequantized as ``min + (byte / 255) * range``.
- Vector3 channel value: three 16-bit BIG-ENDIAN integers (x, y, z), each
  dequantized per-axis as ``min[axis] + (raw / 65535) * range[axis]``.
- Quaternion channel value: three independent SIGNED 16-bit BIG-ENDIAN
  integers (x, y, z), each scaled to roughly +/-pi (see
  frostbite_animation_playback.ANGLE_SCALE) -- NOT a bit-packed
  "smallest three" scheme, and NOT accompanied by a derivable W the way a
  true smallest-three encoding would be. Confirmed by three independent
  axes moving together smoothly and by correct wraparound behaviour at the
  int16 boundary. Which rotation axis order Frostbite composes these in
  (X-Y-Z? Z-Y-X? intrinsic/extrinsic?) was NOT determined from the data
  alone -- see frostbite_animation_playback.py.
- Quaternion channel record: 12 bytes. KeyCount == 1 uses an INLINE format
  (time + xyz packed directly into the remaining 8 bytes, no offset
  pointers needed for a single unchanging sample); KeyCount > 1 uses the
  same offset-pair format as Float/Vector.

What is NOT yet known:

- The byte offset from a channel-array's header to where its Float/Vector
  element structs actually begin was found empirically (164 bytes for
  Float, 592 for Vector) and held on the samples tested, but it is not
  proven to be a fixed constant versus depending on earlier arrays' counts
  (the tested samples happen to share the same Float/Vector counts).
  Quaternion arrays use an adaptive search instead (find_quaternion_array_start)
  specifically because a fixed offset was observed to NOT hold reliably --
  worth revisiting whether Float/Vector need the same treatment on a file
  with different channel counts.
- Some Quaternion channels in a real array (about 8% in one tested sample)
  don't decode cleanly even after the adaptive search finds a good start
  point -- skipped rather than guessed at, cause not yet identified.
- A DEEPER, related, concrete lead worth chasing first next time: even
  channels that pass the current checks can still have a real corruption
  event partway through their OWN sequence, not just at the array's start.
  On one real channel (55 keys), keyframes were smooth and clean from time
  0 to time 280 (matching independently-validated data), then the very
  next time-sorted sample jumped straight to time 32435 -- and every
  subsequent "keyframe" clustered in the 32435-34211 range, right around
  32768 (exactly half of the 65536 raw time range), before the channel's
  declared KeyCount was exhausted. This pattern (clean low cluster, then a
  cliff straight to a cluster hugging the half-range boundary) showed up
  on more than one channel. Current mitigation
  (`_quaternion_channel_looks_valid`) only checks the first
  `_SMOOTHNESS_CHECK_WINDOW` (15) time-sorted samples for exactly this
  reason -- checking the whole channel rejected every channel tested,
  including ones with a long confirmed-good prefix, which throws away a
  lot of real data over a problem that seems to have a specific, findable
  cause (possibly: the true KeyCount for affected channels is smaller than
  what's stored, and the remainder reads into unrelated subsequent data;
  or a sign/format distinction at exactly the half-range boundary that
  isn't handled yet).
- Which specific quaternion channel index maps to which skeleton joint
  (see frostbite_animation_playback.guess_bone_mapping -- an unverified
  guess, not derived from data).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from .frostbite_state import iter_gd_data_blocks
from .geometry import SkeletonData


class ChannelDecodeError(RuntimeError):
    pass


# Empirically confirmed on two real samples -- see module docstring for scope.
FLOAT_CHANNEL_STRUCT_SIZE = 20
VECTOR_CHANNEL_STRUCT_SIZE = 48
FLOAT_ARRAY_OFFSET_FROM_HEADER = 164
VECTOR_ARRAY_OFFSET_FROM_HEADER = 592


@dataclass(frozen=True)
class ChannelArrayHeader:
    """One of the (count, count, declared_value, 0) quadruples that precede
    an EclipseAnimationAsset's channel arrays. Order seen in real samples is
    Float, Vector, Quaternion, then one more of unknown purpose."""
    offset: int
    count: int
    declared_value: int


def find_channel_array_headers(raw: bytes, search_start: int = 0, search_end: int | None = None):
    """Find every run of 4 consecutive (N, N, value, 0) quadruples.

    This is the header block for one EclipseAnimationAsset-shaped object's
    channel arrays. A resource can contain more than one (e.g. several clip
    variants), so this returns every group found, each as a 4-tuple of
    ChannelArrayHeader in file order (conventionally Float, Vector,
    Quaternion, unknown).
    """
    if search_end is None:
        search_end = len(raw)
    groups = []
    addr = search_start
    while addr + 64 <= search_end:
        quads = []
        ok = True
        for i in range(4):
            a, b, c, d = struct.unpack_from("<4i", raw, addr + i * 16)
            if not (a == b and 1 <= a <= 500 and d == 0 and c >= 0):
                ok = False
                break
            quads.append(ChannelArrayHeader(offset=addr + i * 16, count=a, declared_value=c))
        if ok:
            groups.append(tuple(quads))
            addr += 64
        else:
            addr += 4
    return groups


def _finite_and_bounded(value: float, limit: float = 1e6) -> bool:
    return math.isfinite(value) and abs(value) <= limit


@dataclass(frozen=True)
class FloatChannel:
    header_offset: int
    struct_offset: int
    key_count: int
    times: tuple[int, ...]
    values: tuple[float, ...]
    min_value: float
    range_value: float

    @property
    def is_degenerate(self) -> bool:
        return self.key_count <= 2 and self.range_value == 0.0

    def values_in_bounds(self) -> bool:
        lo, hi = self.min_value, self.min_value + self.range_value
        return all(lo - 1e-6 <= v <= hi + 1e-6 for v in self.values)


def decode_float_channel(raw: bytes, struct_addr: int, block_offset: int) -> FloatChannel:
    """block_offset is the enclosing GD.DATA block's own file offset:
    times_off/values_off stored in the struct are relative to it, not to
    the start of the resource (confirmed by cross-checking against a raw
    byte comparison -- the absolute-offset reading passes the tautological
    values_in_bounds() check on pure garbage, since any byte 0-255 always
    satisfies min + byte/255*range by construction; only the block-relative
    reading produces genuinely smooth, monotonic real data)."""
    if struct_addr + FLOAT_CHANNEL_STRUCT_SIZE > len(raw):
        raise ChannelDecodeError(f"Float channel struct at {struct_addr:#x} runs past end of data.")
    key_count, times_off, values_off, min_v, range_v = struct.unpack_from("<3i2f", raw, struct_addr)
    if not (0 < key_count <= 2000):
        raise ChannelDecodeError(f"Implausible KeyCount {key_count} at {struct_addr:#x}.")
    if not (_finite_and_bounded(min_v) and _finite_and_bounded(range_v) and range_v >= 0):
        raise ChannelDecodeError(f"Implausible Min/Range ({min_v}, {range_v}) at {struct_addr:#x}.")
    times_addr = block_offset + times_off
    values_addr = block_offset + values_off
    if times_addr + key_count * 2 > len(raw) or values_addr + key_count > len(raw):
        raise ChannelDecodeError(f"Float channel at {struct_addr:#x} points outside the resource.")
    times = struct.unpack_from(f">{key_count}H", raw, times_addr)
    raw_values = raw[values_addr:values_addr + key_count]
    values = tuple(min_v + (v / 255.0) * range_v for v in raw_values)
    return FloatChannel(struct_addr, struct_addr, key_count, times, values, min_v, range_v)


@dataclass(frozen=True)
class VectorChannel:
    header_offset: int
    struct_offset: int
    key_count: int
    times: tuple[int, ...]
    values: tuple[tuple[float, float, float], ...]
    min_value: tuple[float, float, float]
    range_value: tuple[float, float, float]

    @property
    def is_degenerate(self) -> bool:
        return self.key_count <= 2 and all(r == 0.0 for r in self.range_value)

    def values_in_bounds(self) -> bool:
        for axis in range(3):
            lo = self.min_value[axis]
            hi = self.min_value[axis] + self.range_value[axis]
            for v in self.values:
                if not (lo - 1e-6 <= v[axis] <= hi + 1e-6):
                    return False
        return True


def decode_vector_channel(raw: bytes, struct_addr: int, block_offset: int) -> VectorChannel:
    if struct_addr + VECTOR_CHANNEL_STRUCT_SIZE > len(raw):
        raise ChannelDecodeError(f"Vector channel struct at {struct_addr:#x} runs past end of data.")
    key_count, times_off, values_off = struct.unpack_from("<3i", raw, struct_addr)
    min_v = struct.unpack_from("<3f", raw, struct_addr + 16)
    range_v = struct.unpack_from("<3f", raw, struct_addr + 32)
    if not (0 < key_count <= 2000):
        raise ChannelDecodeError(f"Implausible KeyCount {key_count} at {struct_addr:#x}.")
    for value in (*min_v, *range_v):
        if not _finite_and_bounded(value):
            raise ChannelDecodeError(f"Implausible Min/Range at {struct_addr:#x}.")
    if any(r < 0 for r in range_v):
        raise ChannelDecodeError(f"Negative Range at {struct_addr:#x}.")
    times_addr = block_offset + times_off
    values_addr = block_offset + values_off
    if times_addr + key_count * 2 > len(raw) or values_addr + key_count * 6 > len(raw):
        raise ChannelDecodeError(f"Vector channel at {struct_addr:#x} points outside the resource.")
    times = struct.unpack_from(f">{key_count}H", raw, times_addr)
    values = []
    for i in range(key_count):
        cx, cy, cz = struct.unpack_from(">3H", raw, values_addr + i * 6)
        values.append((
            min_v[0] + (cx / 65535.0) * range_v[0],
            min_v[1] + (cy / 65535.0) * range_v[1],
            min_v[2] + (cz / 65535.0) * range_v[2],
        ))
    return VectorChannel(struct_addr, struct_addr, key_count, times, tuple(values), min_v, range_v)


@dataclass(frozen=True)
class QuaternionChannel:
    """A channel of three independent signed 16-bit angle-like values per
    key (NOT a bit-packed "smallest three" scheme -- see module docstring
    update below). No Min/Range: the schema confirms Quaternion channels
    carry no such field, consistent with a fixed angle range rather than a
    per-channel dequantization range."""
    struct_offset: int
    key_count: int
    times: tuple[int, ...]
    raw_xyz: tuple[tuple[int, int, int], ...]  # signed 16-bit range, angle-like

    @property
    def is_degenerate(self) -> bool:
        return self.key_count <= 1


CORRUPTION_TIME_THRESHOLD = 30000
"""Every channel inspected closely so far (idle and crouch clips, multiple
bones) shows the same pattern: genuinely smooth, plausible data up to some
point, then an abrupt jump to a small cluster of implausible values
starting somewhere in the 31700-32800 range, for the rest of the channel.
Truncating at 30000 (safely below the observed onset) keeps the confirmed-
good portion of every channel inspected and drops the confirmed-bad
portion. This is a real, consistent, empirically-grounded pattern, not a
guess -- but the ROOT CAUSE (why the data pool starts producing garbage
past this point) is not understood."""


def _truncate_at_corruption(times: tuple[int, ...], raw_xyz: tuple[tuple[int, int, int], ...]):
    kept = [(t, v) for t, v in zip(times, raw_xyz) if t < CORRUPTION_TIME_THRESHOLD]
    if not kept:
        return times, raw_xyz  # nothing survives; let the caller's own checks reject it
    kept_times, kept_xyz = zip(*kept)
    return kept_times, kept_xyz


def decode_quaternion_channel(raw: bytes, struct_addr: int, block_offset: int) -> QuaternionChannel:
    """Decode one 12-byte QuaternionChannel record.

    KeyCount == 1 uses an inline format (time + xyz packed directly into the
    remaining 8 bytes, since a single sample needs no offset pointers).
    KeyCount > 1 uses the same offset-pair format as Float/Vector channels,
    with times_off/values_off relative to block_offset (see decode_float_channel).
    """
    if struct_addr + 12 > len(raw):
        raise ChannelDecodeError(f"Quaternion channel struct at {struct_addr:#x} runs past end of data.")
    key_count = struct.unpack_from("<i", raw, struct_addr)[0]
    if key_count == 1:
        t = struct.unpack_from(">H", raw, struct_addr + 4)[0]
        xyz = struct.unpack_from(">3h", raw, struct_addr + 6)
        return QuaternionChannel(struct_addr, 1, (t,), (xyz,))
    if not (1 < key_count <= 2000):
        raise ChannelDecodeError(f"Implausible KeyCount {key_count} at {struct_addr:#x}.")
    times_off, values_off = struct.unpack_from("<2i", raw, struct_addr + 4)
    if times_off < 0 or values_off < 0:
        raise ChannelDecodeError(f"Quaternion channel at {struct_addr:#x} has a negative offset.")
    times_addr = block_offset + times_off
    values_addr = block_offset + values_off
    if times_addr + key_count * 2 > len(raw) or values_addr + key_count * 6 > len(raw):
        raise ChannelDecodeError(f"Quaternion channel at {struct_addr:#x} points outside the resource.")
    times = struct.unpack_from(f">{key_count}H", raw, times_addr)
    raw_xyz = tuple(struct.unpack_from(">3h", raw, values_addr + i * 6) for i in range(key_count))
    times, raw_xyz = _truncate_at_corruption(times, raw_xyz)
    return QuaternionChannel(struct_addr, len(times), times, raw_xyz)


def _quaternion_channel_looks_valid(channel: QuaternionChannel) -> bool:
    if channel.key_count <= 3:
        return True  # too few samples for either heuristic below to mean anything
    zeros = channel.times.count(0)
    if zeros > max(2, channel.key_count // 10):
        return False
    # Catch corruption the zero-ratio check can't see: a real channel found
    # on actual game data had a stretch of implausible small values (e.g.
    # (-31135,-30485,0) followed immediately by (6,12,13)) spliced into
    # otherwise-smooth data -- a genuine rotation shouldn't swing across
    # roughly half the representable range between adjacent time-sorted
    # samples. period=65536 accounts for the confirmed signed-16-bit
    # wraparound so a real wrap isn't mistaken for a jump.
    #
    # IMPORTANT SCOPE NOTE: this is only checked over the first
    # _SMOOTHNESS_CHECK_WINDOW time-sorted samples, not the whole channel.
    # Checking the whole channel rejected EVERY channel tested, including
    # ones with a long confirmed-smooth prefix (e.g. one channel was smooth
    # for its first ~30 samples out of 55, then showed this same corruption
    # pattern for the remainder). That looks like a real, separate,
    # not-yet-understood degradation affecting longer channels' tails --
    # worth investigating on its own, but rejecting the whole channel over
    # it throws away a lot of genuinely good early data. Checking only the
    # start catches the clearly worse case (corruption from the first few
    # samples, which ruins most of a playback loop) without that cost.
    period = 65536
    max_jump_threshold = period // 4
    order = sorted(range(len(channel.times)), key=lambda i: channel.times[i])
    window = order[:_SMOOTHNESS_CHECK_WINDOW]
    for a, b in zip(window, window[1:]):
        for axis in range(3):
            diff = abs(channel.raw_xyz[a][axis] - channel.raw_xyz[b][axis])
            wrapped_diff = min(diff, period - diff)
            if wrapped_diff > max_jump_threshold:
                return False
    return True


_SMOOTHNESS_CHECK_WINDOW = 15


def find_quaternion_array_start(raw: bytes, header_addr: int, count: int, declared_offset: int, block_offset: int,
                                 search_radius: int = 96) -> int | None:
    """Find the true start of a Quaternion channel array near header_addr + declared_offset.

    The declared offset from the channel-array header quadruple does not
    always land exactly on the first channel struct (seen empirically -- the
    leading bytes can belong to unrelated preceding data). This searches a
    small window around the declared offset for a position where MOST of the
    first few channels decode as plausible, low-zero-ratio, locally-smooth
    structs -- not ALL of them, since a genuinely corrupted individual
    channel (confirmed on real data) can sit right at the start of an
    otherwise correctly-located array; requiring every checked channel to
    validate rejects the correct position entirely in that case.
    """
    base_candidate = header_addr + declared_offset
    check_count = min(8, count)
    min_valid = max(1, -(-check_count * 4 // 10))  # ceil(40% of check_count) -- see module note on corruption rate
    for delta in range(0, search_radius, 4):
        for signed_delta in (delta, -delta) if delta else (0,):
            addr = base_candidate + signed_delta
            if addr < 0:
                continue
            valid_count = 0
            decode_failed = False
            for i in range(check_count):
                try:
                    channel = decode_quaternion_channel(raw, addr + i * 12, block_offset)
                except ChannelDecodeError:
                    decode_failed = True
                    break
                if _quaternion_channel_looks_valid(channel):
                    valid_count += 1
            if not decode_failed and valid_count >= min_valid:
                return addr
    return None


def decode_quaternion_array(raw: bytes, header_addr: int, count: int, declared_offset: int,
                             block_offset: int) -> tuple[list[QuaternionChannel], int]:
    """Decode as many of a Quaternion array's channels as validate cleanly.

    Returns (channels, error_count). Channels that fail to decode or look
    implausible are skipped rather than guessed at -- see
    find_quaternion_array_start for why a fixed stride can drift partway
    through a real array (variable single-key inline records included).

    Stops early after 2 consecutive STRUCTURAL decode failures (implausible
    KeyCount or an out-of-bounds offset -- not the same as
    _quaternion_channel_looks_valid failing on in-bounds-but-corrupted
    data). Confirmed on real data: the header's declared count can
    genuinely overstate the true array length (one real file declared 89
    channels; entries 0-84 decoded with plausible, self-consistent
    KeyCounts one after another, then 85-88 abruptly produced huge
    geometrically-patterned garbage values, i.e. the walk had run past the
    end of real channel data into unrelated bytes). Trusting the declared
    count blindly in that case corrupts nothing new, but wastes the error
    budget on bytes that were never channels to begin with.
    """
    start = find_quaternion_array_start(raw, header_addr, count, declared_offset, block_offset)
    if start is None:
        return [], count
    channels = []
    errors = 0
    consecutive_structural_errors = 0
    addr = start
    for _ in range(count):
        try:
            channel = decode_quaternion_channel(raw, addr, block_offset)
        except ChannelDecodeError:
            errors += 1
            consecutive_structural_errors += 1
            if consecutive_structural_errors >= 2:
                break
            addr += 12
            continue
        consecutive_structural_errors = 0
        if not _quaternion_channel_looks_valid(channel):
            errors += 1
            addr += 12
            continue
        channels.append(channel)
        addr += 12
    return channels, errors


def decode_quaternion_array_positional(raw: bytes, header_addr: int, count: int, declared_offset: int,
                                        block_offset: int) -> list[QuaternionChannel | None]:
    """Like decode_quaternion_array, but returns one entry PER DECLARED
    CHANNEL POSITION (None where a channel fails to decode), instead of a
    shorter, renumbered list with smoothness-failing entries silently
    dropped.

    Needed to combine with an externally-resolved bone mapping (e.g. a real
    Rigamate bank's DOF-to-channel-position table): that mapping refers to
    channels by their raw position in the declared array, which an
    independent decoder (not ours) may include even when our own
    smoothness heuristic would reject them. Renumbering after dropping
    entries -- as decode_quaternion_array does for direct playback use --
    would silently misalign every position after the first drop. Still
    stops early on genuine structural garbage (the array-overrun case),
    since positions past that point aren't real channels at all.
    """
    start = find_quaternion_array_start(raw, header_addr, count, declared_offset, block_offset)
    if start is None:
        return [None] * count
    channels: list[QuaternionChannel | None] = []
    consecutive_structural_errors = 0
    addr = start
    for _ in range(count):
        try:
            channel = decode_quaternion_channel(raw, addr, block_offset)
        except ChannelDecodeError:
            channels.append(None)
            consecutive_structural_errors += 1
            if consecutive_structural_errors >= 2:
                break
            addr += 12
            continue
        consecutive_structural_errors = 0
        channels.append(channel)
        addr += 12
    return channels



@dataclass(frozen=True)
class ChannelHealth:
    """Per-channel diagnostic result, with a specific named reason rather
    than a bare pass/fail -- built so the app can surface "which bones are
    suspect and why" immediately after loading a clip, instead of needing
    a manual pose-debug dump and a round trip to figure out what's wrong.
    """
    channel_index: int
    bone_name: str
    key_count: int
    healthy: bool
    issues: tuple[str, ...]


def _duplicate_time_count(times: tuple[int, ...]) -> int:
    return len(times) - len(set(times))


def _extreme_pinned_axes(raw_xyz: tuple[tuple[int, int, int], ...]) -> list[int]:
    """Axes where more than half the samples sit at exactly the signed
    16-bit boundary (-32768, 32767, or the -32767 near-boundary value) --
    confirmed on real data as a corruption signature distinct from the
    duplicate-timestamp one (e.g. a 2-key channel with x=y=-32767 on both
    keys, or a 16-key channel with x pinned at +/-32768 on every sample)."""
    extremes = {-32768, 32767, -32767}
    pinned = []
    for axis in range(3):
        values = [xyz[axis] for xyz in raw_xyz]
        if not values:
            continue
        if sum(1 for v in values if v in extremes) > len(values) * 0.5:
            pinned.append(axis)
    return pinned


def analyze_quaternion_channel_health(
    channels: list[QuaternionChannel], bone_mapping: list[int], skeleton: SkeletonData,
) -> list[ChannelHealth]:
    """Per-mapped-channel health check, combining every corruption
    signature found on real Anthem data so far: duplicate timestamps,
    axis values pinned at the int16 boundary, and the early-window
    smoothness check already used during decoding. Channels with too few
    keys for any of these to mean anything (key_count <= 3) are reported
    healthy by default, matching _quaternion_channel_looks_valid.
    """
    results = []
    for channel_index, joint_index in enumerate(bone_mapping):
        if channel_index >= len(channels):
            break
        channel = channels[channel_index]
        bone_name = skeleton.joints[joint_index][0] if joint_index < len(skeleton.joints) else f"joint[{joint_index}]"
        issues = []
        if channel.key_count > 3:
            dupes = _duplicate_time_count(channel.times)
            if dupes > 0:
                issues.append(f"{dupes} duplicate timestamp(s)")
            pinned = _extreme_pinned_axes(channel.raw_xyz)
            if pinned:
                axis_names = ",".join("xyz"[a] for a in pinned)
                issues.append(f"axis {axis_names} pinned at int16 boundary")
            if not _quaternion_channel_looks_valid(channel):
                issues.append("failed early-window smoothness check")
        results.append(ChannelHealth(
            channel_index=channel_index, bone_name=bone_name, key_count=channel.key_count,
            healthy=not issues, issues=tuple(issues),
        ))
    return results


def format_channel_health_summary(results: list[ChannelHealth]) -> str:
    """One-line summary suitable for a status bar."""
    healthy = sum(1 for r in results if r.healthy)
    return f"{healthy}/{len(results)} channels look clean"


def format_channel_health_report(results: list[ChannelHealth]) -> str:
    """Full per-channel breakdown suitable for a text file dump."""
    lines = [format_channel_health_summary(results), ""]
    flagged = [r for r in results if not r.healthy]
    if not flagged:
        lines.append("No channels flagged.")
    else:
        lines.append("Flagged channels:")
        for r in flagged:
            lines.append(f"  [{r.channel_index}] {r.bone_name} (keys={r.key_count}): {'; '.join(r.issues)}")
    lines.append("")
    lines.append("All channels:")
    for r in results:
        status = "OK" if r.healthy else "FLAGGED"
        lines.append(f"  [{r.channel_index}] {r.bone_name:<20} keys={r.key_count:<5} {status}")
    return "\n".join(lines)


@dataclass(frozen=True)
class ChannelGroupReport:
    header_addr: int
    float_channels: tuple[FloatChannel, ...]
    vector_channels: tuple[VectorChannel, ...]
    quaternion_channels: tuple[QuaternionChannel, ...]
    float_decode_errors: int
    vector_decode_errors: int
    quaternion_decode_errors: int


def find_enclosing_block_offset(raw: bytes, addr: int) -> int:
    """Find the file offset of the GD.DATA block that contains addr.

    times_off/values_off in a channel struct are relative to this, not to
    the start of the resource -- see decode_float_channel.
    """
    best = 0
    for block in iter_gd_data_blocks(raw):
        if block.offset <= addr < block.offset + len(block.body):
            return block.offset
        if block.offset <= addr:
            best = block.offset
    return best


def analyze_channel_group(raw: bytes, headers: tuple[ChannelArrayHeader, ...]) -> ChannelGroupReport:
    """Decode every Float, Vector, and Quaternion channel in one header group.

    headers is one 4-tuple as returned by find_channel_array_headers, in
    (Float, Vector, Quaternion, unknown) order.
    """
    float_header, vector_header = headers[0], headers[1]
    header_addr = float_header.offset
    block_offset = find_enclosing_block_offset(raw, header_addr)

    float_channels = []
    float_errors = 0
    float_array_addr = header_addr + FLOAT_ARRAY_OFFSET_FROM_HEADER
    for i in range(float_header.count):
        try:
            float_channels.append(
                decode_float_channel(raw, float_array_addr + i * FLOAT_CHANNEL_STRUCT_SIZE, block_offset)
            )
        except ChannelDecodeError:
            float_errors += 1

    vector_channels = []
    vector_errors = 0
    vector_array_addr = header_addr + VECTOR_ARRAY_OFFSET_FROM_HEADER
    for i in range(vector_header.count):
        try:
            vector_channels.append(
                decode_vector_channel(raw, vector_array_addr + i * VECTOR_CHANNEL_STRUCT_SIZE, block_offset)
            )
        except ChannelDecodeError:
            vector_errors += 1

    quaternion_channels: list[QuaternionChannel] = []
    quaternion_errors = 0
    if len(headers) > 2:
        quat_header = headers[2]
        quaternion_channels, quaternion_errors = decode_quaternion_array(
            raw, header_addr, quat_header.count, quat_header.declared_value, block_offset,
        )

    return ChannelGroupReport(
        header_addr=header_addr,
        float_channels=tuple(float_channels),
        vector_channels=tuple(vector_channels),
        quaternion_channels=tuple(quaternion_channels),
        float_decode_errors=float_errors,
        vector_decode_errors=vector_errors,
        quaternion_decode_errors=quaternion_errors,
    )
