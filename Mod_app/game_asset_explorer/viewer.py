from __future__ import annotations

import math
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .geometry import MeshData, SkeletonData


def _training_platform_mesh(scene: dict[str, object]) -> MeshData:
    """Build a finite tiled slab in the same 3D space as the character."""
    half = float(scene.get("platform_half_size", 18.0))
    tile = float(scene.get("platform_tile_size", 2.0))
    camera_x = float(scene.get("camera_x", 0.0))
    camera_y = float(scene.get("camera_y", 0.0))
    camera_z = float(scene.get("camera_z", 0.0))
    center = tuple(scene.get("normalization_center", (0.0, 0.0, 0.0)))
    extent = max(0.001, float(scene.get("normalization_extent", 1.0)))
    count = max(2, round((half * 2) / tile))
    tile = half * 2 / count

    def normalized(x: float, y: float, z: float):
        return (
            (x - camera_x - center[0]) / extent,
            (y - camera_y - center[1]) / extent,
            (z - camera_z - center[2]) / extent,
        )

    vertices = []
    for row in range(count + 1):
        z = -half + row * tile
        for column in range(count + 1):
            x = -half + column * tile
            vertices.append(normalized(x, 0.0, z))
    faces = []
    stride = count + 1
    for row in range(count):
        for column in range(count):
            a = row * stride + column
            b, d = a + 1, a + stride
            c = d + 1
            faces.extend(((a, d, c), (a, c, b)))

    # Four dark vertical sides give the floor visible thickness in the void.
    top = [(-half, 0.0, -half), (half, 0.0, -half),
           (half, 0.0, half), (-half, 0.0, half)]
    bottom_y = -0.45
    for side in range(4):
        first, second = top[side], top[(side + 1) % 4]
        base = len(vertices)
        vertices.extend((
            normalized(*first), normalized(*second),
            normalized(second[0], bottom_y, second[2]),
            normalized(first[0], bottom_y, first[2]),
        ))
        faces.extend(((base, base + 2, base + 1), (base, base + 3, base + 2)))
    return MeshData("__training_platform__", vertices, faces)


def animation_helper_visible(name: str) -> bool:
    """False for controller/camera joints that are not render-skeleton bones."""
    folded = name.casefold()
    return not (
        folded in {"reference", "aitrajectory", "trajectory", "groundplane", "climbplane"}
        or folded.startswith("camera")
        or folded.startswith("connect")
        or folded.endswith("camera")
    )


def _clip_to_camera_near_plane(
    points: tuple[tuple[float, float, float], ...], maximum_z: float = 2.02,
) -> list[tuple[float, float, float]]:
    """Clip a camera-space polygon before perspective projection.

    The software camera sits at Z=2.2. Without clipping, a finite floor that
    extends behind it produces enormous inverted triangles at the near plane.
    """
    clipped: list[tuple[float, float, float]] = []
    for current, following in zip(points, (*points[1:], points[0])):
        current_inside = current[2] <= maximum_z
        following_inside = following[2] <= maximum_z
        if current_inside:
            clipped.append(current)
        if current_inside != following_inside:
            dz = following[2] - current[2]
            amount = 0.0 if abs(dz) < 1e-12 else (maximum_z - current[2]) / dz
            clipped.append(tuple(
                current[axis] + (following[axis] - current[axis]) * amount
                for axis in range(3)
            ))
    return clipped


def render_mesh_raster(
    meshes: list[MeshData], width: int, height: int, yaw: float, pitch: float,
    zoom: float, wireframe: bool = False, face_budget: int | None = None,
    scene: dict[str, object] | None = None,
):
    """Render every triangle to one bitmap without creating thousands of Tk items.

    The interactive canvas intentionally uses a small face sample for responsive
    dragging.  This final pass keeps the complete model: no triangle is removed
    merely because a higher LOD crossed a UI face budget.
    """
    from PIL import Image, ImageDraw

    width, height = max(width, 2), max(height, 2)
    image = Image.new("RGB", (width, height), "#171a21")
    draw = ImageDraw.Draw(image)
    if scene and not scene.get("world_scene"):
        _draw_raster_training_ground(draw, width, height, scene)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    scale = min(width, height) * 0.72 * zoom
    triangles = []
    total_faces = sum(len(mesh.faces) for mesh in meshes)
    face_step = max(1, math.ceil(total_faces / face_budget)) if face_budget else 1
    render_meshes = ([_training_platform_mesh(scene)] if scene and scene.get("world_scene") else []) + list(meshes)
    for mesh in render_meshes:
        is_platform = mesh.name == "__training_platform__"
        rotated = []
        for source_x, source_y, source_z in mesh.vertices:
            x = source_x * cy + source_z * sy
            z = -source_x * sy + source_z * cy
            y = source_y * cp - z * sp
            z = source_y * sp + z * cp
            rotated.append((x, y, z))
        for face_index, face in enumerate(mesh.faces[::face_step]):
            if min(face) < 0 or max(face) >= len(rotated):
                continue
            pa, pb, pc = (rotated[index] for index in face)
            visible = _clip_to_camera_near_plane((pa, pb, pc))
            if len(visible) < 3:
                continue
            points = []
            for x, y, z in visible:
                perspective = 1.0 / (2.2 - z)
                points.append((
                    width / 2 + x * scale * perspective,
                    height / 2 - y * scale * perspective,
                ))
            ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
            vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nx, ny, nz = nx / length, ny / length, nz / length
            depth = sum(point[2] for point in visible) / len(visible)
            key = abs(nx * -0.35 + ny * 0.55 + nz * 0.76)
            fill = abs(nx * 0.72 + ny * 0.18 + nz * 0.42)
            light = min(1.0, 0.18 + 0.68 * key + 0.18 * fill)
            if is_platform:
                # Every pair of triangles is one top tile. The final eight
                # triangles are slab sides and remain darker.
                top_face_count = len(mesh.faces) - 8
                if face_index < top_face_count:
                    cell = face_index // 2
                    grid_width = max(1, round(math.sqrt(top_face_count / 2)))
                    checker = (cell // grid_width + cell % grid_width) & 1
                    base_color = (126, 132, 139) if checker else (92, 99, 107)
                else:
                    base_color = (38, 44, 52)
                color = tuple(int(channel * max(0.45, light)) for channel in base_color)
            else:
                color = tuple(int(channel * light) for channel in (112, 166, 202))
            triangles.append((depth, tuple(points), color))
    triangles.sort(key=lambda item: item[0])
    for _depth, points, color in triangles:
        if wireframe:
            draw.line((*points, points[0]), fill=(131, 183, 217), width=1, joint="curve")
        else:
            draw.polygon(points, fill=color)
    return image


def _draw_raster_training_ground(draw, width: int, height: int, scene: dict[str, object]) -> None:
    """Draw a cheap camera-follow training pad behind the character."""
    world_x = float(scene.get("camera_x", scene.get("x", 0.0)))
    world_y = max(0.0, float(scene.get("y", 0.0)))
    world_z = float(scene.get("camera_z", scene.get("z", 0.0)))
    horizon = min(height - 24, int(height * 0.68 + min(world_y, 12.0) * height * 0.018))
    # A finite-looking tiled test platform. The checker cells move underneath
    # the following camera, making forward/sideways/diagonal travel readable.
    draw.rectangle((0, horizon, width, height), fill="#121820")
    spacing = 2.0
    z_phase = (world_z % spacing) / spacing
    x_phase = (world_x % spacing) / spacing
    vanish_x = width / 2
    bottom_spacing = max(34, width // 14)
    rows = [horizon]
    for row in range(1, 15):
        progress = max(0.0, min(1.0, (row - z_phase) / 13.0))
        rows.append(horizon + (height - horizon) * progress * progress)
    rows.append(height)

    def grid_x(column: int, screen_y: float) -> float:
        depth = (screen_y - horizon) / max(1, height - horizon)
        return vanish_x + (column - x_phase) * bottom_spacing * depth

    for row_index, (top, bottom) in enumerate(zip(rows, rows[1:])):
        for column in range(-14, 14):
            fill = "#303b47" if (row_index + column + int(world_x / spacing)) & 1 else "#26313c"
            draw.polygon((
                (grid_x(column, top), top),
                (grid_x(column + 1, top), top),
                (grid_x(column + 1, bottom), bottom),
                (grid_x(column, bottom), bottom),
            ), fill=fill)
        draw.line((0, bottom, width, bottom), fill="#465563", width=1)
    for column in range(-14, 15):
        bottom_x = vanish_x + (column - x_phase) * bottom_spacing
        draw.line((vanish_x, horizon, bottom_x, height), fill="#465563", width=1)
    # High-contrast axes make direction and diagonal travel obvious.
    axis_x = vanish_x - x_phase * bottom_spacing
    draw.line((vanish_x, horizon, axis_x, height), fill="#526b80", width=2)


class MeshViewer(ttk.Frame):
    """Dependency-free interactive 3D viewport rendered on a Tk canvas."""
    def __init__(self, master) -> None:
        super().__init__(master)
        toolbar = ttk.Frame(self)
        self.toolbar = toolbar
        toolbar.pack(fill="x")
        toolbar.columnconfigure(0, weight=1)
        self.title_var = tk.StringVar(value="Select a character and click Preview")
        # A width of one lets grid clip very long internal asset names instead
        # of pushing the controls beyond the right edge of the viewport.
        ttk.Label(
            toolbar, textvariable=self.title_var, width=1, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=6, pady=5)
        self.wireframe = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            toolbar, text="Wireframe", variable=self.wireframe, command=self.request_draw,
        ).grid(row=0, column=3, padx=6)
        self.bones_visible = tk.BooleanVar(value=False)
        self.bones_button = ttk.Checkbutton(
            toolbar, text="Bones", variable=self.bones_visible,
            command=self.request_draw, state="disabled",
        )
        self.bones_button.grid(row=0, column=4, padx=(0, 6))
        self.mesh_visible = tk.BooleanVar(value=True)
        self.mesh_button = ttk.Checkbutton(
            toolbar, text="Mesh", variable=self.mesh_visible,
            command=self._toggle_mesh_visibility, state="disabled",
        )
        self.mesh_button.grid(row=0, column=5, padx=(0, 6))
        ttk.Button(toolbar, text="Reset view", command=self.reset).grid(row=0, column=2)
        self.detail_frame = ttk.Frame(toolbar)
        self.detail_label_var = tk.StringVar(value="Detail")
        ttk.Label(self.detail_frame, textvariable=self.detail_label_var).pack(side="left")
        self.detail_var = tk.DoubleVar(value=0)
        self.detail_scale = ttk.Scale(
            self.detail_frame, variable=self.detail_var, from_=0, to=1,
            length=115, command=self._detail_moved,
        )
        self.detail_scale.pack(side="left", padx=(6, 0))
        self.detail_scale.bind("<ButtonRelease-1>", self._detail_released)
        self.detail_info_button = ttk.Button(
            self.detail_frame, text="LOD info…", command=self._show_detail_info,
        )
        self.detail_info_button.pack(side="left", padx=(6, 0))
        self.detail_frame.grid(row=0, column=1, padx=(8, 10))
        self.detail_frame.grid_remove()
        self.canvas = tk.Canvas(self, bg="#171a21", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _: self.request_draw())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda _: self._zoom(1.1))
        self.canvas.bind("<Button-5>", lambda _: self._zoom(0.9))
        self.meshes: list[MeshData] = []
        self.skeleton: SkeletonData | None = None
        self.mesh_skeleton: SkeletonData | None = None
        self._mesh_center = (0.0, 0.0, 0.0)
        self._mesh_extent = 1.0
        self.image = None
        self._image_tk = None
        self.yaw = -0.6
        self.pitch = -0.25
        self.zoom = 1.0
        self.last = (0, 0)
        self._draw_job = None
        self._refine_job = None
        self._interactive = False
        self._detail_lods: tuple[int, ...] = ()
        self._detail_current_lod: int | None = None
        self._detail_callback: Callable[[int], None] | None = None
        self._detail_declared_lods: tuple[int, ...] = ()
        self._detail_related_assets: tuple[str, ...] = ()
        self._raster_generation = 0
        self._raster_running = False
        self._raster_requested = False
        self._prototype_overlay: dict[str, object] | None = None
        self.prototype_face_budget: int | None = 2_500
        self._game_mode = False

    def set_game_mode(self, enabled: bool) -> None:
        """Use a clean full-window viewport for exported playable builds."""
        self._game_mode = enabled
        if enabled:
            self.toolbar.pack_forget()
            self.canvas.configure(cursor="crosshair")
        elif not self.toolbar.winfo_manager():
            self.toolbar.pack(fill="x", before=self.canvas)
        self.request_draw()

    def set_meshes(self, meshes: list[MeshData], title: str) -> None:
        self.clear_detail_levels()
        all_vertices = [vertex for mesh in meshes for vertex in mesh.vertices]
        if not all_vertices:
            raise ValueError("The model has no vertices.")
        mins = [min(v[i] for v in all_vertices) for i in range(3)]
        maxs = [max(v[i] for v in all_vertices) for i in range(3)]
        center = [(mins[i] + maxs[i]) / 2 for i in range(3)]
        extent = max(maxs[i] - mins[i] for i in range(3)) or 1.0
        self.meshes = [MeshData(
            mesh.name,
            [tuple((vertex[i] - center[i]) / extent for i in range(3)) for vertex in mesh.vertices],
            mesh.faces,
            mesh.skin_bones,
            mesh.skin_weights,
        ) for mesh in meshes]
        self._mesh_center = tuple(center)
        self._mesh_extent = extent
        self.image = None
        self.skeleton = None
        self.mesh_skeleton = None
        self.bones_visible.set(False)
        self.bones_button.configure(state="disabled")
        self.mesh_button.configure(state="normal")
        self._image_tk = None
        self.title_var.set(f"{title} · {sum(len(m.vertices) for m in meshes):,} vertices · drag to rotate, wheel to zoom")
        self.reset()

    def set_image(self, image: Image.Image, title: str) -> None:
        """Display a decoded texture/image in the same embedded viewport."""
        from PIL import Image
        if image.width <= 0 or image.height <= 0:
            raise ValueError("The image has no pixels.")
        self.clear_detail_levels()
        self.meshes = []
        self.skeleton = None
        self.mesh_skeleton = None
        self.bones_visible.set(False)
        self.bones_button.configure(state="disabled")
        self.mesh_button.configure(state="disabled")
        self.image = image.convert("RGBA")
        self.title_var.set(f"{title} · {image.width}×{image.height} · 2D image")
        self.request_draw()

    def clear(self, message: str) -> None:
        self.clear_detail_levels()
        self.meshes = []
        self.skeleton = None
        self.mesh_skeleton = None
        self.bones_visible.set(False)
        self.bones_button.configure(state="disabled")
        self.mesh_button.configure(state="disabled")
        self.image = None
        self._image_tk = None
        self.title_var.set(message)
        self.request_draw()

    def set_detail_levels(
        self, lods: list[int], current_lod: int, callback: Callable[[int], None],
        declared_lods: list[int] | None = None,
        related_assets: list[str] | None = None,
    ) -> None:
        """Expose real stored LODs from low detail on the left to high on the right."""
        ordered = tuple(dict.fromkeys(lods))
        if current_lod not in ordered:
            ordered = (*ordered, current_lod)
        self._detail_lods = ordered
        self._detail_current_lod = current_lod
        self._detail_callback = callback
        self._detail_declared_lods = tuple(dict.fromkeys(declared_lods or lods))
        self._detail_related_assets = tuple(related_assets or ())
        self.detail_scale.configure(from_=0, to=max(0, len(ordered) - 1))
        self.detail_var.set(ordered.index(current_lod))
        if len(ordered) > 1:
            if not self.detail_scale.winfo_manager():
                self.detail_scale.pack(side="left", padx=(6, 0))
        else:
            self.detail_scale.pack_forget()
        self._update_detail_label()
        self.detail_frame.grid()

    def clear_detail_levels(self) -> None:
        self._detail_lods = ()
        self._detail_current_lod = None
        self._detail_callback = None
        self._detail_declared_lods = ()
        self._detail_related_assets = ()
        if hasattr(self, "detail_frame"):
            self.detail_frame.grid_remove()

    def restore_detail_selection(self) -> None:
        if self._detail_current_lod in self._detail_lods:
            self.detail_var.set(self._detail_lods.index(self._detail_current_lod))
            self._update_detail_label()

    def set_detail_loading(self, loading: bool) -> None:
        if self._detail_lods:
            self.detail_scale.configure(state="disabled" if loading else "normal")

    def _detail_moved(self, _value=None) -> None:
        self._update_detail_label()

    def _update_detail_label(self) -> None:
        if not self._detail_lods:
            return
        position = max(0, min(len(self._detail_lods) - 1, round(self.detail_var.get())))
        lod = self._detail_lods[position]
        if len(self._detail_lods) == 1:
            qualifier = " · only"
        else:
            qualifier = " · low" if position == 0 else " · high" if position == len(self._detail_lods) - 1 else ""
        self.detail_label_var.set(f"Detail: LOD{lod}{qualifier}")

    def _detail_released(self, _event=None) -> None:
        if not self._detail_lods or self._detail_callback is None:
            return
        position = max(0, min(len(self._detail_lods) - 1, round(self.detail_var.get())))
        lod = self._detail_lods[position]
        if lod != self._detail_current_lod:
            self._detail_callback(lod)

    def _show_detail_info(self) -> None:
        from tkinter import messagebox
        declared = sorted(self._detail_declared_lods)
        available = sorted(self._detail_lods)
        missing = [lod for lod in declared if lod not in available]
        format_lods = lambda values: ", ".join(f"LOD{lod}" for lod in values) or "none"
        related = (
            "\n\nSeparate cosmetic/customization MeshSets found locally:\n"
            + "\n".join(f"• {item}" for item in self._detail_related_assets)
            if self._detail_related_assets else ""
        )
        messagebox.showinfo(
            "Mesh detail levels",
            "Frostbite numbers detail in reverse: LOD0 is the highest detail.\n\n"
            f"Declared by this MeshSet: {format_lods(declared)}\n"
            f"Currently resolved: {format_lods(available)}\n"
            f"Not yet resolved: {format_lods(missing)}" + related,
        )

    def reset(self) -> None:
        self.yaw, self.pitch, self.zoom = -0.6, -0.25, 1.0
        self.request_draw()

    def _toggle_mesh_visibility(self) -> None:
        if not self.mesh_visible.get() and self.mesh_skeleton is not None:
            self.bones_visible.set(True)
        self.request_draw()

    def _press(self, event) -> None:
        self.last = (event.x, event.y)

    def _drag(self, event) -> None:
        self.yaw += (event.x - self.last[0]) * 0.012
        self.pitch = max(-1.5, min(1.5, self.pitch + (event.y - self.last[1]) * 0.012))
        self.last = (event.x, event.y)
        self._interactive = True
        self.request_draw(interactive=not self._game_mode)
        if self._refine_job is not None:
            self.after_cancel(self._refine_job)
        self._refine_job = self.after(110, self._finish_interaction)

    def _wheel(self, event) -> None:
        self._zoom(1.12 if event.delta > 0 else 0.89)

    def _zoom(self, factor: float) -> None:
        self.zoom = max(0.2, min(8.0, self.zoom * factor))
        self.request_draw(interactive=not self._game_mode)
        if self._refine_job is not None:
            self.after_cancel(self._refine_job)
        self._refine_job = self.after(110, self._finish_interaction)

    def _finish_interaction(self) -> None:
        self._refine_job = None
        self._interactive = False
        self.request_draw()

    def request_draw(self, interactive: bool = False) -> None:
        """Coalesce mouse events so obsolete frames are never rendered."""
        self._interactive = self._interactive or interactive
        if self._draw_job is None:
            self._draw_job = self.after(1, self._run_scheduled_draw)

    def _run_scheduled_draw(self) -> None:
        self._draw_job = None
        self.draw(interactive=self._interactive)

    def draw(self, interactive: bool = False) -> None:
        width, height = max(self.canvas.winfo_width(), 2), max(self.canvas.winfo_height(), 2)
        if self.image is not None:
            self.canvas.delete("all")
            self._draw_image(width, height)
            return
        if self.skeleton is not None:
            self.canvas.delete("all")
            self._draw_skeleton(width, height)
            return
        if not self.meshes:
            self.canvas.delete("all")
            self.canvas.create_text(width / 2, height / 2, text=self.title_var.get(), fill="#9aa4b2", width=width - 40)
            return
        if not self.mesh_visible.get():
            self._raster_generation += 1  # Discard any mesh frame still rendering.
            self._raster_requested = False
            self.canvas.delete("all")
            self._draw_prototype_environment(width, height)
            self._draw_mesh_skeleton_overlay(width, height)
            self._draw_prototype_overlay(width, height)
            return
        if not interactive:
            self._request_full_raster(width, height)
            return
        self._raster_generation += 1
        self._raster_requested = False
        self.canvas.delete("all")
        self._draw_prototype_environment(width, height)
        self._draw_interactive_mesh(width, height)

    def _draw_interactive_mesh(self, width: int, height: int) -> None:
        """Draw a bounded face sample while the camera is actively moving."""
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        scale = min(width, height) * 0.72 * self.zoom
        projected: list[list[tuple[float, float, float]]] = []
        rotated: list[list[tuple[float, float, float]]] = []
        for mesh in self.meshes:
            points = []
            rotated_points = []
            for x, y, z in mesh.vertices:
                x, z = x * cy + z * sy, -x * sy + z * cy
                y, z = y * cp - z * sp, y * sp + z * cp
                rotated_points.append((x, y, z))
                perspective = 1.0 / max(0.35, 2.2 - z)
                points.append((width / 2 + x * scale * perspective, height / 2 - y * scale * perspective, z))
            projected.append(points)
            rotated.append(rotated_points)

        triangles = []
        face_budget = 1_400
        total_faces = sum(len(mesh.faces) for mesh in self.meshes)
        step = max(1, math.ceil(total_faces / face_budget))
        for mesh_index, mesh in enumerate(self.meshes):
            points = projected[mesh_index]
            positions = rotated[mesh_index]
            for face in mesh.faces[::step]:
                if max(face) >= len(points):
                    continue
                a, b, c = (points[index] for index in face)
                pa, pb, pc = (positions[index] for index in face)
                ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
                vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
                nx, ny, nz = nx / length, ny / length, nz / length
                depth = (a[2] + b[2] + c[2]) / 3
                # Two camera-space lights plus ambient. abs() intentionally
                # renders both windings because game resources vary here.
                key = abs(nx * -0.35 + ny * 0.55 + nz * 0.76)
                fill = abs(nx * 0.72 + ny * 0.18 + nz * 0.42)
                light = min(1.0, 0.18 + 0.68 * key + 0.18 * fill)
                red, green, blue = (int(channel * light) for channel in (112, 166, 202))
                color = f"#{red:02x}{green:02x}{blue:02x}"
                triangles.append((depth, (a, b, c), color))
        triangles.sort(key=lambda item: item[0])
        for _, points, color in triangles:
            flat = [coordinate for point in points for coordinate in point[:2]]
            if self.wireframe.get():
                self.canvas.create_polygon(*flat, fill="", outline="#83b7d9")
            else:
                self.canvas.create_polygon(*flat, fill=color, outline="")
        self._draw_mesh_skeleton_overlay(width, height)
        self._draw_prototype_overlay(width, height)

    def _request_full_raster(self, width: int, height: int) -> None:
        """Queue one complete bitmap render; stale background frames are discarded."""
        self._raster_generation += 1
        generation = self._raster_generation
        if self._raster_running:
            self._raster_requested = True
            return
        self._raster_running = True
        self._raster_requested = False
        meshes = self.meshes
        yaw, pitch, zoom = self.yaw, self.pitch, self.zoom
        wireframe = bool(self.wireframe.get())
        scene = dict(self._prototype_overlay) if self._prototype_overlay else None
        if scene is not None and scene.get("world_scene"):
            scene["normalization_center"] = self._mesh_center
            scene["normalization_extent"] = self._mesh_extent

        def worker() -> None:
            try:
                rendered = render_mesh_raster(
                    meshes, width, height, yaw, pitch, zoom, wireframe,
                    face_budget=self.prototype_face_budget if self._prototype_overlay else None,
                    scene=scene,
                )
                self.after(0, self._finish_full_raster, generation, rendered)
            except Exception as error:
                self.after(0, self._finish_full_raster, generation, None, str(error))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_full_raster(self, generation: int, rendered, error: str | None = None) -> None:
        from PIL import ImageTk

        self._raster_running = False
        if rendered is not None and generation == self._raster_generation and self.meshes:
            self._image_tk = ImageTk.PhotoImage(rendered)
            self.canvas.delete("all")
            self.canvas.create_image(
                self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2,
                image=self._image_tk, anchor="center",
            )
            self._draw_mesh_skeleton_overlay(
                self.canvas.winfo_width(), self.canvas.winfo_height(),
            )
            self._draw_prototype_overlay(
                self.canvas.winfo_width(), self.canvas.winfo_height(),
            )
        elif error and generation == self._raster_generation:
            self.canvas.delete("all")
            self.canvas.create_text(
                self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2,
                text=f"Viewport render failed\n\n{error}", fill="#9aa4b2",
                width=max(20, self.canvas.winfo_width() - 40),
            )
        if self._raster_requested:
            self._raster_requested = False
            self.request_draw()

    def _draw_prototype_overlay(self, width: int, height: int) -> None:
        details = self._prototype_overlay
        if not details:
            return
        state = str(details.get("state", "idle")).upper()
        lod = int(details.get("lod", 5))
        speed = float(details.get("speed", 0.0))
        altitude = float(details.get("y", 0.0))
        clip = str(details.get("clip", "finding animation…"))
        self.canvas.create_rectangle(12, 12, 385, 108, fill="#111820", outline="#3d5266")
        self.canvas.create_text(
            24, 24, anchor="nw", fill="#b8e6ff", font=("TkDefaultFont", 11, "bold"),
            text=f"INTERCEPTOR PROTOTYPE  ·  GLOBAL LOD{lod}",
        )
        self.canvas.create_text(
            24, 49, anchor="nw", fill="#eef5fa", font=("TkDefaultFont", 10),
            text=f"{state}   speed {speed:4.1f} m/s   altitude {altitude:4.1f} m",
        )
        self.canvas.create_text(
            24, 73, anchor="nw", fill="#91a4b7", font=("TkDefaultFont", 8),
            text=clip[:62],
        )
        self.canvas.create_text(
            width - 14, height - 14, anchor="se", fill="#91a4b7",
            text="Mouse drag orbit  ·  Wheel zoom  ·  WASD move  ·  Shift sprint/glide\n"
                 "Space jump/up  ·  "
                 "F flight  ·  Ctrl down  ·  Q dash  ·  0–5 or Numpad 0–5 LOD  ·  Esc stop",
        )

    def _draw_prototype_environment(self, width: int, height: int) -> None:
        """Canvas fallback used while rotating or when the mesh is hidden."""
        details = self._prototype_overlay
        if not details:
            return
        # The standalone game draws an actual finite MeshData platform in the
        # raster pass. Never put the legacy screen-space horizon behind it.
        if details.get("world_scene"):
            return
        x = float(details.get("camera_x", details.get("x", 0.0)))
        y_world = max(0.0, float(details.get("y", 0.0)))
        z = float(details.get("camera_z", details.get("z", 0.0)))
        horizon = min(height - 24, int(height * 0.68 + min(y_world, 12.0) * height * 0.018))
        self.canvas.create_rectangle(0, horizon, width, height, fill="#121820", outline="")
        z_phase = (z % 2.0) / 2.0
        spacing = max(34, width // 14)
        x_phase = (x % 2.0) / 2.0
        rows = [horizon]
        for row in range(1, 15):
            progress = max(0.0, min(1.0, (row - z_phase) / 13.0))
            rows.append(horizon + (height - horizon) * progress * progress)
        rows.append(height)

        def grid_x(column: int, screen_y: float) -> float:
            depth = (screen_y - horizon) / max(1, height - horizon)
            return width / 2 + (column - x_phase) * spacing * depth

        for row_index, (top, bottom) in enumerate(zip(rows, rows[1:])):
            for column in range(-14, 14):
                fill = "#303b47" if (row_index + column + int(x / 2.0)) & 1 else "#26313c"
                self.canvas.create_polygon(
                    grid_x(column, top), top,
                    grid_x(column + 1, top), top,
                    grid_x(column + 1, bottom), bottom,
                    grid_x(column, bottom), bottom,
                    fill=fill, outline="",
                )
            self.canvas.create_line(0, bottom, width, bottom, fill="#465563")
        for column in range(-14, 15):
            bottom_x = width / 2 + (column - x_phase) * spacing
            self.canvas.create_line(width / 2, horizon, bottom_x, height, fill="#465563")

    def _draw_image(self, width: int, height: int) -> None:
        from PIL import Image, ImageTk
        image = self.image
        if image is None:
            return
        margin = 24
        scale = min((width - margin * 2) / image.width, (height - margin * 2) / image.height, 1.0)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        rendered = image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image
        self._image_tk = ImageTk.PhotoImage(rendered)
        self.canvas.create_image(width / 2, height / 2, image=self._image_tk, anchor="center")

    def set_skeleton(self, skeleton: SkeletonData, title: str) -> None:
        if not skeleton.joints:
            raise ValueError("The skeleton has no joints.")
        mins = [min(joint[2 + axis] for joint in skeleton.joints) for axis in range(3)]
        maxs = [max(joint[2 + axis] for joint in skeleton.joints) for axis in range(3)]
        center = [(mins[axis] + maxs[axis]) / 2 for axis in range(3)]
        extent = max(maxs[axis] - mins[axis] for axis in range(3)) or 1.0
        normalized = [
            (joint_name, parent, *((coordinate - center[axis]) / extent for axis, coordinate in enumerate((x, y, z))))
            for joint_name, parent, x, y, z in skeleton.joints
        ]
        self.clear_detail_levels()
        self.meshes = []
        self.mesh_skeleton = None
        self.bones_visible.set(False)
        self.bones_button.configure(state="disabled")
        self.mesh_button.configure(state="disabled")
        self.image = None
        self.skeleton = SkeletonData(skeleton.name, normalized)
        self.title_var.set(f"{title} · {len(skeleton.joints):,} joints · drag to rotate, wheel to zoom")
        self.reset()

    def set_mesh_skeleton(self, skeleton: SkeletonData) -> None:
        """Overlay a model-space bind skeleton using the mesh normalization."""
        if not self.meshes or not skeleton.joints:
            return
        center, extent = self._mesh_center, self._mesh_extent
        joints = [
            (
                joint_name, parent,
                (x - center[0]) / extent,
                (y - center[1]) / extent,
                (z - center[2]) / extent,
            )
            for joint_name, parent, x, y, z in skeleton.joints
        ]
        self.mesh_skeleton = SkeletonData(skeleton.name, joints)
        self.bones_visible.set(True)
        self.bones_button.configure(state="normal")
        self.request_draw()

    def set_mesh_pose(
        self, meshes: list[MeshData] | None, skeleton: SkeletonData, *, realtime: bool = False,
    ) -> None:
        """Update the posed bones and optionally skinned geometry without moving the camera."""
        if not self.meshes or (meshes is not None and len(meshes) != len(self.meshes)):
            return
        center, extent = self._mesh_center, self._mesh_extent
        if meshes is not None:
            self.meshes = [MeshData(
                mesh.name,
                [tuple((vertex[i] - center[i]) / extent for i in range(3)) for vertex in mesh.vertices],
                mesh.faces, mesh.skin_bones, mesh.skin_weights,
            ) for mesh in meshes]
        self.mesh_skeleton = SkeletonData(skeleton.name, [
            (name, parent, (x - center[0]) / extent, (y - center[1]) / extent,
             (z - center[2]) / extent)
            for name, parent, x, y, z in skeleton.joints
        ])
        self.request_draw(interactive=realtime)

    def set_prototype_overlay(
        self, details: dict[str, object] | None, *, redraw: bool = True,
    ) -> None:
        """Set prototype HUD and optional world-scene rendering parameters."""
        self._prototype_overlay = details
        if redraw:
            # Controller updates are not mouse-camera interaction. Marking
            # every HUD tick interactive permanently selected the 1,400-face
            # drag preview, which looked like randomly missing triangles.
            self.request_draw(interactive=False)

    def _draw_mesh_skeleton_overlay(self, width: int, height: int) -> None:
        skeleton = self.mesh_skeleton
        if skeleton is None or not self.bones_visible.get() or not self.meshes:
            return
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        scale = min(width, height) * 0.72 * self.zoom
        points = []
        for _name, _parent, source_x, source_y, source_z in skeleton.joints:
            x = source_x * cy + source_z * sy
            z = -source_x * sy + source_z * cy
            y = source_y * cp - z * sp
            z = source_y * sp + z * cp
            perspective = 1.0 / max(0.35, 2.2 - z)
            points.append((
                width / 2 + x * scale * perspective,
                height / 2 - y * scale * perspective,
                z,
            ))
        segments = []
        visible = [animation_helper_visible(joint[0]) for joint in skeleton.joints]
        for index, (_name, parent, *_coords) in enumerate(skeleton.joints):
            if visible[index] and 0 <= parent < len(points) and visible[parent]:
                segments.append(((points[parent][2] + points[index][2]) / 2, parent, index))
        for _depth, parent, index in sorted(segments):
            self.canvas.create_line(
                points[parent][0], points[parent][1], points[index][0], points[index][1],
                fill="#ffb347", width=2,
            )
        for index, (x, y, _z) in enumerate(points):
            if not visible[index]:
                continue
            self.canvas.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5,
                                    fill="#fff0a8", outline="#c97820")

    def _draw_skeleton(self, width: int, height: int) -> None:
        skeleton = self.skeleton
        if skeleton is None:
            return
        scale = min(width, height) * 0.34 * self.zoom
        cx, cy = width / 2, height / 2
        points = []
        for _, _, x, y, z in skeleton.joints:
            px, pz = x * math.cos(self.yaw) + z * math.sin(self.yaw), -x * math.sin(self.yaw) + z * math.cos(self.yaw)
            py = y * math.cos(self.pitch) - pz * math.sin(self.pitch)
            pz = y * math.sin(self.pitch) + pz * math.cos(self.pitch)
            points.append((cx + px * scale, cy - py * scale, pz))
        for index, (_, parent, *_coords) in enumerate(skeleton.joints):
            if 0 <= parent < len(points):
                self.canvas.create_line(points[parent][0], points[parent][1], points[index][0], points[index][1], fill="#79b8df", width=2)
        for index, (name, _parent, *_coords) in enumerate(skeleton.joints):
            x, y, _ = points[index]
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#f0b35a", outline="")
            if len(skeleton.joints) <= 80:
                self.canvas.create_text(x + 7, y, text=name, anchor="w", fill="#d7e3ef", font=("TkDefaultFont", 8))
