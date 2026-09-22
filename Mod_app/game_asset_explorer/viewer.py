from __future__ import annotations

import math
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .geometry import MeshData, SkeletonData


def render_mesh_raster(
    meshes: list[MeshData], width: int, height: int, yaw: float, pitch: float,
    zoom: float, wireframe: bool = False,
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
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    scale = min(width, height) * 0.72 * zoom
    triangles = []
    for mesh in meshes:
        projected = []
        rotated = []
        for source_x, source_y, source_z in mesh.vertices:
            x = source_x * cy + source_z * sy
            z = -source_x * sy + source_z * cy
            y = source_y * cp - z * sp
            z = source_y * sp + z * cp
            rotated.append((x, y, z))
            perspective = 1.0 / max(0.35, 2.2 - z)
            projected.append((
                width / 2 + x * scale * perspective,
                height / 2 - y * scale * perspective,
                z,
            ))
        for face in mesh.faces:
            if min(face) < 0 or max(face) >= len(projected):
                continue
            a, b, c = (projected[index] for index in face)
            pa, pb, pc = (rotated[index] for index in face)
            ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
            vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nx, ny, nz = nx / length, ny / length, nz / length
            depth = (a[2] + b[2] + c[2]) / 3
            key = abs(nx * -0.35 + ny * 0.55 + nz * 0.76)
            fill = abs(nx * 0.72 + ny * 0.18 + nz * 0.42)
            light = min(1.0, 0.18 + 0.68 * key + 0.18 * fill)
            color = tuple(int(channel * light) for channel in (112, 166, 202))
            triangles.append((depth, (a[:2], b[:2], c[:2]), color))
    triangles.sort(key=lambda item: item[0])
    for _depth, points, color in triangles:
        if wireframe:
            draw.line((*points, points[0]), fill=(131, 183, 217), width=1, joint="curve")
        else:
            draw.polygon(points, fill=color)
    return image


class MeshViewer(ttk.Frame):
    """Dependency-free interactive 3D viewport rendered on a Tk canvas."""
    def __init__(self, master) -> None:
        super().__init__(master)
        toolbar = ttk.Frame(self)
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
        self._raster_generation = 0
        self._raster_running = False
        self._raster_requested = False

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
        self.image = None
        self._image_tk = None
        self.title_var.set(message)
        self.request_draw()

    def set_detail_levels(
        self, lods: list[int], current_lod: int, callback: Callable[[int], None],
    ) -> None:
        """Expose real stored LODs from low detail on the left to high on the right."""
        ordered = tuple(dict.fromkeys(lods))
        if current_lod not in ordered:
            ordered = (*ordered, current_lod)
        self._detail_lods = ordered
        self._detail_current_lod = current_lod
        self._detail_callback = callback
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

    def reset(self) -> None:
        self.yaw, self.pitch, self.zoom = -0.6, -0.25, 1.0
        self.request_draw()

    def _press(self, event) -> None:
        self.last = (event.x, event.y)

    def _drag(self, event) -> None:
        self.yaw += (event.x - self.last[0]) * 0.012
        self.pitch = max(-1.5, min(1.5, self.pitch + (event.y - self.last[1]) * 0.012))
        self.last = (event.x, event.y)
        self._interactive = True
        self.request_draw(interactive=True)
        if self._refine_job is not None:
            self.after_cancel(self._refine_job)
        self._refine_job = self.after(110, self._finish_interaction)

    def _wheel(self, event) -> None:
        self._zoom(1.12 if event.delta > 0 else 0.89)

    def _zoom(self, factor: float) -> None:
        self.zoom = max(0.2, min(8.0, self.zoom * factor))
        self.request_draw(interactive=True)
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
        if not interactive:
            self._request_full_raster(width, height)
            return
        self._raster_generation += 1
        self._raster_requested = False
        self.canvas.delete("all")
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

        def worker() -> None:
            try:
                rendered = render_mesh_raster(
                    meshes, width, height, yaw, pitch, zoom, wireframe,
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
        for index, (_name, parent, *_coords) in enumerate(skeleton.joints):
            if 0 <= parent < len(points):
                segments.append(((points[parent][2] + points[index][2]) / 2, parent, index))
        for _depth, parent, index in sorted(segments):
            self.canvas.create_line(
                points[parent][0], points[parent][1], points[index][0], points[index][1],
                fill="#ffb347", width=2,
            )
        for x, y, _z in points:
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
