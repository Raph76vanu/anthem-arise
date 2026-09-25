"""Lightweight offline movement model for the Interceptor sandbox.

This deliberately keeps physics independent from Tk and asset decoding so it
can be tested deterministically.  Values are conservative prototype defaults;
the named Anthem settings assets remain the source of truth once their EBX
fields are decoded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PrototypeInput:
    forward: float = 0.0
    right: float = 0.0
    sprint: bool = False
    jump_pressed: bool = False
    flight_toggled: bool = False
    ascend: bool = False
    descend: bool = False
    dash_pressed: bool = False


@dataclass
class PrototypeState:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    vz: float = 0.0
    yaw: float = 0.0
    mode: str = "idle"
    grounded: bool = True


def _approach(value: float, target: float, amount: float) -> float:
    if value < target:
        return min(target, value + amount)
    return max(target, value - amount)


def _turn_toward(value: float, target: float, amount: float) -> float:
    delta = (target - value + math.pi) % (math.tau) - math.pi
    return value + max(-amount, min(amount, delta))


class InterceptorController:
    """Small third-person controller with ground, air, hover and flight states."""

    def __init__(self) -> None:
        self.state = PrototypeState()
        self._landing_time = 0.0
        self._dash_time = 0.0

    def update(self, elapsed: float, controls: PrototypeInput, camera_yaw: float = 0.0) -> PrototypeState:
        dt = max(0.0, min(float(elapsed), 0.05))
        state = self.state
        if controls.flight_toggled:
            if state.mode in {"hover", "glide", "flight", "dash"}:
                state.mode = "fall"
                state.grounded = False
            else:
                state.mode = "hover"
                state.grounded = False
                state.y = max(state.y, 0.35)
                state.vy = max(state.vy, 0.0)

        input_length = math.hypot(controls.right, controls.forward)
        right = controls.right / max(1.0, input_length)
        forward = controls.forward / max(1.0, input_length)
        sin_yaw, cos_yaw = math.sin(camera_yaw), math.cos(camera_yaw)
        # The raster camera looks down its negative Z axis. Its right basis is
        # (cos, -sin), while forward is (-sin, -cos). The old forward basis
        # used the exact opposite vector, making W retreat and S advance.
        desired_x = right * cos_yaw - forward * sin_yaw
        desired_z = -forward * cos_yaw - right * sin_yaw
        moving = abs(desired_x) + abs(desired_z) > 0.001
        flying = state.mode in {"hover", "glide", "flight", "dash"}

        if controls.dash_pressed and flying:
            self._dash_time = 0.28
        self._dash_time = max(0.0, self._dash_time - dt)

        if flying:
            speed = 17.0 if self._dash_time else (11.0 if controls.sprint else 7.5)
            acceleration = 42.0 if self._dash_time else 18.0
            state.vx = _approach(state.vx, desired_x * speed, acceleration * dt)
            state.vz = _approach(state.vz, desired_z * speed, acceleration * dt)
            vertical = float(controls.ascend) - float(controls.descend)
            state.vy = _approach(state.vy, vertical * 6.0, 20.0 * dt)
            if self._dash_time:
                state.mode = "dash"
            elif moving:
                state.mode = "glide" if controls.sprint else "flight"
            else:
                state.mode = "hover"
        else:
            if state.grounded and controls.jump_pressed:
                state.grounded = False
                state.vy = 7.2
                state.mode = "jump"
                self._landing_time = 0.0
            speed = 6.4 if controls.sprint else 3.4
            acceleration = 24.0 if state.grounded else 7.0
            state.vx = _approach(state.vx, desired_x * speed, acceleration * dt)
            state.vz = _approach(state.vz, desired_z * speed, acceleration * dt)
            if not state.grounded:
                state.vy = max(-32.0, state.vy - 18.0 * dt)
                state.mode = "jump" if state.vy > 0.7 else "fall"
            elif self._landing_time > 0.0:
                self._landing_time = max(0.0, self._landing_time - dt)
                state.mode = "land"
            elif moving:
                state.mode = "sprint" if controls.sprint else "walk"
            else:
                state.mode = "idle"

        state.x += state.vx * dt
        state.y += state.vy * dt
        state.z += state.vz * dt
        if not flying and state.y <= 0.0:
            landed = not state.grounded and state.vy < -0.5
            state.y = 0.0
            state.vy = 0.0
            state.grounded = True
            if landed:
                self._landing_time = 0.24
                state.mode = "land"
        if moving:
            state.yaw = _turn_toward(
                state.yaw, math.atan2(desired_x, desired_z), 8.0 * dt,
            )
        return state


_CLIP_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # StaticPose is a mostly-constant blend-space sample (2 moving rotations),
    # not a useful standalone prototype idle.  The authored NonAdditive body
    # gesture carries a full moving pose and does not require an additive base.
    "idle": (("idle", "gesture", "warmup", "body", "nonadditive"), ("pistol", "rifle")),
    "walk": (("walk", "forward", "unarmed"), ("start", "stop", "incline", "strafe")),
    "sprint": (("sprint", "forward", "unarmed"), ("start", "stop", "incline", "blend", "additive")),
    "jump": (("exp", "inair", "jump", "normal", "forward"), ("fall", "falling", "aimed")),
    "fall": (("exp", "inair", "jump", "normal", "falling"), ("aimed", "pistol", "rifle")),
    "land": (("exp", "fall", "land", "light"), ("walking", "running", "sprinting")),
    "hover": (("exp", "hover", "loop"), ("up", "dn", "add", "wing")),
    # Slow_Forward is another directional pose sample (one moving rotation).
    # Glide_Loop has a verified map and 63 moving rotations, so it is the
    # correct standalone choice until the full blend graph is implemented.
    "flight": (("glide", "loop"), ("start", "exit", "bank", "boost", "underwater", "add")),
    "glide": (("glide", "loop"), ("start", "exit", "bank", "boost", "underwater", "add")),
    "dash": (("dash", "unarmed", "forward", "start"), ("carried", "strafe")),
}


def select_clip_index(names: list[str] | tuple[str, ...], state: str) -> int | None:
    """Pick a stable, plain full-body EXF clip for a controller state."""
    include, exclude = _CLIP_RULES.get(state, _CLIP_RULES["idle"])
    candidates = []
    for index, name in enumerate(names):
        folded = name.casefold().replace(" ", "")
        if not folded.startswith("exf_") or not all(token in folded for token in include):
            continue
        penalty = sum(token in folded for token in exclude)
        penalty += 2 * ("nonadditive" in folded)
        candidates.append((penalty, len(name), name.casefold(), index))
    if candidates:
        return min(candidates)[-1]
    # Graceful fallback: keep the prototype animated even if a different
    # bundle has only a broad state name.
    primary = include[0]
    fallback = [
        (len(name), name.casefold(), index) for index, name in enumerate(names)
        if name.casefold().startswith("exf_") and primary in name.casefold()
        and "additive" not in name.casefold()
    ]
    return min(fallback)[-1] if fallback else None


def transition_base_clip_name(clip_name: str) -> str | None:
    """Return the authored transition whose endpoint anchors an air loop."""
    folded = clip_name.casefold()
    if folded == "exf_nov_glide_loop":
        return "EXF_NOV_Glide_Start"
    if folded == "exf_nov_exp_hover_loop":
        return "EXF_NOV_EXP_Hover_Start"
    return None
