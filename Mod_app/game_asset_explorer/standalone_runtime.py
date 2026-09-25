"""Small decoded-data-only runtime for an exported Interceptor prototype."""
from __future__ import annotations

import math
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from .frostbite_animation_playback import evaluate_pose_transforms
from .frostbite_skinning import skin_meshes
from .interceptor_prototype import InterceptorController, PrototypeInput
from .standalone_export import BUNDLE_FILENAME, load_bundle
from .viewer import MeshViewer, animation_helper_visible


def _wrapped_angle(value: float) -> float:
    return (value + math.pi) % math.tau - math.pi


def quaternion_yaw(rotation) -> float:
    """Extract the rotation around Frostbite's vertical Y axis."""
    x, y, z, w = rotation
    return math.atan2(2.0 * (w * y + x * z), 1.0 - 2.0 * (y * y + z * z))


def rotate_character_y(
    meshes, skeleton, angle: float, pivot=(0.0, 0.0, 0.0),
    offset=(0.0, 0.0, 0.0),
):
    """Apply controller heading after skinning, around one stable model pivot."""
    from .geometry import MeshData, SkeletonData
    cosine, sine = math.cos(angle), math.sin(angle)

    def point(value):
        dx, dy, dz = value[0] - pivot[0], value[1] - pivot[1], value[2] - pivot[2]
        return (
            pivot[0] + dx * cosine + dz * sine + offset[0],
            pivot[1] + dy + offset[1],
            pivot[2] - dx * sine + dz * cosine + offset[2],
        )

    rotated_meshes = [MeshData(
        mesh.name, [point(vertex) for vertex in mesh.vertices], mesh.faces,
        mesh.skin_bones, mesh.skin_weights,
    ) for mesh in meshes]
    rotated_skeleton = SkeletonData(skeleton.name, [
        (name, parent, *point((x, y, z)))
        for name, parent, x, y, z in skeleton.joints
    ])
    return rotated_meshes, rotated_skeleton


class StandalonePrototype(tk.Tk):
    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__()
        self.title("Interceptor Prototype")
        self.geometry("1100x720")
        self.minsize(720, 480)
        self.viewer = MeshViewer(self)
        self.viewer.pack(fill="both", expand=True)
        self.viewer.set_game_mode(True)
        self.controller = InterceptorController()
        self.lod_meshes = {int(key): value for key, value in payload["lod_meshes"].items()}
        self.skeleton = payload["skeleton"]
        self.bind_rotations = payload.get("bind_rotations") or []
        self.animations = payload["animations"]
        self.current_lod = 5 if 5 in self.lod_meshes else max(self.lod_meshes)
        self.current_animation_state = ""
        self.current_animation = None
        self.current_authored_heading = 0.0
        self.bind_world = evaluate_pose_transforms(
            self.skeleton, self.bind_rotations, [], [], 0.0,
        )[1]
        self.heading_bone = next((
            index for index, joint in enumerate(self.skeleton.joints)
            if joint[0].casefold() == "hips"
        ), 0)
        self.bind_heading = quaternion_yaw(self.bind_world[self.heading_bone])
        self.held: set[str] = set()
        self.pressed: set[str] = set()
        self.started = time.monotonic()
        self.last_tick = self.started
        self.last_skin = 0.0
        self.camera_x = 0.0
        self.camera_y = 0.0
        self.camera_z = 0.0
        self.platform_half_size = 18.0
        self.playable_half_size = 17.0
        self.skin_running = False
        self.skin_generation = 0
        # The standalone renderer has no inventory UI competing for CPU. Draw
        # every triangle so animation never flickers between sampled/full mesh.
        self.viewer.prototype_face_budget = None
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind_all("<KeyPress>", self._key_press)
        self.bind_all("<KeyRelease>", self._key_release)
        self._set_lod(self.current_lod)
        self.after(16, self._tick)
        self.after(50, self.viewer.canvas.focus_set)

    @staticmethod
    def _key_name(event) -> str | None:
        folded = str(getattr(event, "keysym", "")).casefold()
        folded = {"shift_l": "shift", "shift_r": "shift",
                  "control_l": "control", "control_r": "control"}.get(folded, folded)
        return folded if folded in {
            "w", "a", "s", "d", "shift", "control", "space", "f", "q", "escape",
        } else None

    @staticmethod
    def _numpad_lod(event) -> int | None:
        folded = str(getattr(event, "keysym", "")).casefold()
        names = {f"kp_{value}": value for value in range(6)}
        names.update({"kp_insert": 0, "kp_end": 1, "kp_down": 2,
                      "kp_next": 3, "kp_left": 4, "kp_begin": 5})
        if folded in names:
            return names[folded]
        # Windows reports VK_NUMPAD0..5 as keycodes 96..101 on some Tk builds.
        keycode = int(getattr(event, "keycode", -1))
        if 96 <= keycode <= 101:
            return keycode - 96
        # Number-row fallback is useful on laptops without a physical numpad.
        return int(folded) if folded in {"0", "1", "2", "3", "4", "5"} else None

    def _key_press(self, event):
        lod = self._numpad_lod(event)
        if lod is not None:
            self._set_lod(lod)
            return "break"
        key = self._key_name(event)
        if key == "escape":
            self.destroy()
            return "break"
        if key is not None:
            if key not in self.held:
                self.pressed.add(key)
            self.held.add(key)
            return "break"
        return None

    def _key_release(self, event):
        key = self._key_name(event)
        if key is not None:
            self.held.discard(key)
            return "break"
        return None

    def _set_lod(self, lod: int) -> None:
        meshes = self.lod_meshes.get(lod)
        if meshes is None:
            self.viewer.set_prototype_overlay({
                "x": self.controller.state.x, "y": self.controller.state.y,
                "z": self.controller.state.z, "speed": 0.0,
                "state": f"LOD{lod} unavailable", "lod": self.current_lod,
                "clip": "Available: " + ", ".join(f"LOD{n}" for n in sorted(self.lod_meshes)),
            })
            return
        self.current_lod = lod
        self.skin_generation += 1
        previous_view = (
            (self.viewer.yaw, self.viewer.pitch, self.viewer.zoom)
            if self.viewer.meshes else (-0.6, 0.35, 1.0)
        )
        self.viewer.set_meshes(meshes, f"Interceptor standalone · LOD{lod}")
        self.viewer.yaw, self.viewer.pitch, self.viewer.zoom = previous_view
        self.viewer.set_mesh_skeleton(self.skeleton)
        self.viewer.bones_visible.set(False)
        self.current_animation_state = ""

    def _select_animation(self, state: str) -> None:
        if state == self.current_animation_state:
            return
        animation = self.animations.get(state) or self.animations.get("idle")
        if animation is None:
            return
        self.current_animation_state = state
        self.current_animation = animation
        # Calibrate the clip once. Subtracting the live Hips yaw every frame
        # made ordinary authored torso/hip motion steer the whole character,
        # causing spins and direction snaps. A fixed first-frame reference
        # removes only the clip's cardinal authoring direction.
        _pose, reference_world = self._evaluate_animation(animation, 0.0)
        self.current_authored_heading = _wrapped_angle(
            quaternion_yaw(reference_world[self.heading_bone]) - self.bind_heading,
        )
        self.started = time.monotonic()
        self.skin_generation += 1

    def _evaluate_animation(self, animation, phase: float):
        vector_pairs = [
            (bone, channel) for bone, channel in zip(
                animation.get("vector_mapping", []), animation.get("vector_channels", []),
            ) if 0 <= bone < len(self.skeleton.joints)
            and animation_helper_visible(self.skeleton.joints[bone][0])
        ]
        return evaluate_pose_transforms(
            self.skeleton, self.bind_rotations,
            animation["mapping"], animation["channels"], phase,
            rotation_mode="absolute",
            vector_mapping=[pair[0] for pair in vector_pairs],
            vector_channels=[pair[1] for pair in vector_pairs],
            timeline_end=animation.get("timeline_end"),
        )

    def _request_skin(self, now: float) -> None:
        animation = self.current_animation
        if animation is None or self.skin_running:
            return
        interval = {0: .20, 1: .15, 2: .11, 3: .085, 4: .065, 5: .05}.get(self.current_lod, .1)
        if now - self.last_skin < interval:
            return
        self.last_skin = now
        elapsed = now - self.started
        loop_seconds = max(
            .1, float(animation.get("timeline_end") or 1)
            / (float(animation.get("fps") or 30) * float(animation.get("time_scale") or 1)),
        )
        phase = (elapsed % loop_seconds) / loop_seconds
        pose, world = self._evaluate_animation(animation, phase)
        generation = self.skin_generation
        meshes = self.lod_meshes[self.current_lod]
        controller_heading = self.controller.state.yaw
        authored_heading = self.current_authored_heading
        camera_offset = (
            self.controller.state.x - self.camera_x,
            self.controller.state.y - self.camera_y,
            self.controller.state.z - self.camera_z,
        )
        self.skin_running = True

        def worker() -> None:
            try:
                skinned = skin_meshes(meshes, self.skeleton, pose, self.bind_world, world)
                # Remove clip-authored cardinal direction (some jump clips are
                # authored 90 degrees sideways), then apply movement heading.
                # This reference is intentionally fixed for the whole clip.
                heading = controller_heading - authored_heading
                pivot = pose.joints[self.heading_bone][2:5]
                rotated_meshes, rotated_pose = rotate_character_y(
                    skinned, pose, heading, pivot, camera_offset,
                )
                self.after(0, self._finish_skin, generation, rotated_meshes, rotated_pose)
            except Exception as error:
                self.after(0, self._finish_skin, generation, None, pose, str(error))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_skin(self, generation: int, meshes, pose, error: str | None = None) -> None:
        self.skin_running = False
        if generation == self.skin_generation and meshes is not None:
            self.viewer.set_mesh_pose(meshes, pose, realtime=False)
        elif generation == self.skin_generation and error:
            self.viewer.set_prototype_overlay({
                "x": self.controller.state.x, "y": self.controller.state.y,
                "z": self.controller.state.z, "state": "render error",
                "speed": 0.0, "lod": self.current_lod,
                "clip": error,
            })

    def _tick(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_tick
        self.last_tick = now
        held, pressed = self.held, self.pressed
        controls = PrototypeInput(
            forward=float("w" in held) - float("s" in held),
            right=float("d" in held) - float("a" in held),
            sprint="shift" in held,
            jump_pressed="space" in pressed,
            flight_toggled="f" in pressed,
            ascend="space" in held,
            descend="control" in held,
            dash_pressed="q" in pressed,
        )
        self.pressed.clear()
        state = self.controller.update(elapsed, controls, -self.viewer.yaw)
        # The training slab is finite. Keep the controller on it instead of
        # allowing the player to disappear into an implied infinite horizon.
        if state.x < -self.playable_half_size or state.x > self.playable_half_size:
            state.x = max(-self.playable_half_size, min(self.playable_half_size, state.x))
            state.vx = 0.0
        if state.z < -self.playable_half_size or state.z > self.playable_half_size:
            state.z = max(-self.playable_half_size, min(self.playable_half_size, state.z))
            state.vz = 0.0

        # This is the orbit target, not a second object behind the character.
        # A short follow delay makes movement readable while keeping the player
        # close to the center of the third-person camera.
        follow = min(1.0, elapsed * 7.5)
        self.camera_x += (state.x - self.camera_x) * follow
        self.camera_y += (state.y - self.camera_y) * follow
        self.camera_z += (state.z - self.camera_z) * follow
        animation_state = "glide" if state.mode == "flight" and controls.sprint else state.mode
        self._select_animation(animation_state)
        self._request_skin(now)
        speed = math.sqrt(state.vx * state.vx + state.vy * state.vy + state.vz * state.vz)
        self.viewer.set_prototype_overlay({
            "x": state.x, "y": state.y, "z": state.z,
            "camera_x": self.camera_x, "camera_y": self.camera_y,
            "camera_z": self.camera_z,
            "world_scene": True,
            "platform_half_size": self.platform_half_size,
            "platform_tile_size": 2.0,
            "speed": speed, "state": state.mode, "lod": self.current_lod,
            "heading": state.yaw, "camera_yaw": self.viewer.yaw,
            "clip": str((self.current_animation or {}).get("name", "idle")),
        }, redraw=False)
        self.after(16, self._tick)


def main() -> None:
    try:
        payload = load_bundle(Path(__file__).resolve().parent.parent / BUNDLE_FILENAME)
        app = StandalonePrototype(payload)
    except Exception as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Cannot start Interceptor Prototype", str(error))
        root.destroy()
        return
    app.mainloop()


if __name__ == "__main__":
    main()
