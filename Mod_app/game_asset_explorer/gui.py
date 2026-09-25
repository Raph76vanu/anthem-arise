from __future__ import annotations

import json
import io
import re
import struct
import threading
import tkinter as tk
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk

from .geometry import EMBEDDED_PREVIEW_EXTENSIONS, MeshFormatError, load_mesh, load_mesh_bytes, load_skeleton_bytes, load_texture_bytes
from .grouping import (
    asset_folder_key, asset_folder_label, asset_in_physical_scope,
    asset_in_virtual_scope, category_for_asset, folder_rows, items_in_folder,
    parse_virtual_location,
)
from .models import CharacterBundle
from .scanner import scan
from .viewer import MeshViewer

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga", ".dds", ".ktx", ".ktx2", ".exr", ".tif", ".tiff"})


class App(tk.Tk):
    CATEGORY_OPTIONS = (
        "Characters / meshes", "Animations", "Textures", "Props", "Images",
        "Map parts", "Skeletons", "Weapons & carryables", "Containers / archives",
        "Other / unclassified", "All",
    )

    def __init__(self) -> None:
        super().__init__()
        self.title("Game Asset Explorer")
        self.geometry("1050x650")
        self.minsize(800, 500)
        self.result = None
        self.path_var = tk.StringVar()
        self.show_all_var = tk.BooleanVar(value=False)
        self.lod0_only_var = tk.BooleanVar(value=False)
        self.animation_res_only_var = tk.BooleanVar(value=False)
        self.animation_clip_search_var = tk.BooleanVar(value=False)
        self._animation_clip_indexing = False
        self.category_var = tk.StringVar(value=self.CATEGORY_OPTIONS[0])
        self.search_var = tk.StringVar()
        self.exclude_search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose a legally obtained game installation folder.")
        self._displayed_bundles = []
        self._archive_previews = {}
        self._indexed_archives: set[Path] = set()
        self._indexing_categories: set[str] = set()
        self._unity_indexed_categories: set[str] = set()
        self._frostbite_inventory_started = False
        self._search_after_id = None
        self._expanded_folders: dict[str, set[tuple[str, str]]] = {}
        self._folder_focus: dict[str, tuple[str, str]] = {}
        self._folder_focus_labels: dict[str, str] = {}
        self._folder_match_counts: dict[tuple[str, str], int] = {}
        self._virtual_scope: tuple[Path, str] | None = None
        self._rig_report: dict[str, object] | None = None
        self._rig_asset = None
        self._frostbite_skeleton_cache = {}
        self._frostbite_virtual_asset_cache = {}
        self._preview_character: dict[str, object] | None = None
        self._assembly_source = None
        self._active_character: dict[str, object] | None = None
        self._active_animation: dict[str, object] | None = None
        self._animation_after_id: str | None = None
        self._animation_start_time: float | None = None
        self._last_animation_phase: float = 0.0
        self._animation_playback: dict[str, object] | None = None
        self._animation_generation = 0
        self._animation_skin_running = False
        self._video_export_running = False
        self._resolved_rigamate_asset = None
        self._resolved_rigamate_raw: bytes | None = None
        # Verified family banks can also help preview later configuration RES
        # files which contain curves but omit their own bank pointer.
        self._rigamate_bank_cache: dict[str, tuple[object, bytes]] = {}
        self._primary_rig_bank_cache: dict[str, tuple[object, bytes, bytes]] = {}
        self.use_for_animation_var = tk.BooleanVar(value=False)
        self.active_character_var = tk.StringVar(value="No active character")
        self.animation_clip_var = tk.StringVar(value="Animation: none selected")
        self.animation_state_var = tk.StringVar(value="Select an animation record to inspect it.")
        self.animation_loop_var = tk.BooleanVar(value=True)
        self.animation_filter_unhealthy_var = tk.BooleanVar(value=True)
        self.animation_group_var = tk.StringVar()
        self._animation_group_options: list[dict[str, object]] = []
        self.animation_clip_filter_var = tk.StringVar()
        self.animation_clip_match_var = tk.StringVar(value="")
        self._animation_clip_search_cursor: int | None = None
        self.animation_speed_var = tk.StringVar(value="1.0×")
        self._prototype_running = False
        self._prototype_controller = None
        self._prototype_after_id: str | None = None
        self._prototype_last_time: float | None = None
        self._prototype_held: set[str] = set()
        self._prototype_pressed: set[str] = set()
        self._prototype_animation_cache: dict[int, dict[str, object]] = {}
        self._prototype_clip_index: int | None = None
        self._prototype_global_lod = 5
        self._standalone_export_running = False
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        ttk.Entry(top, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Browse…", command=self.choose).pack(side="left", padx=(8, 0))
        self.scan_button = ttk.Button(top, text="Scan", command=self.start_scan)
        self.scan_button.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            top,
            text="Show low-confidence assets",
            variable=self.show_all_var,
            command=self._refresh_result,
        ).pack(side="left", padx=(10, 0))

        filter_bar = ttk.Frame(self, padding=(12, 0, 12, 8))
        filter_bar.pack(fill="x")
        ttk.Label(filter_bar, text="Asset category:").pack(side="left")
        category_box = ttk.Combobox(
            filter_bar, textvariable=self.category_var, values=self.CATEGORY_OPTIONS,
            state="readonly", width=25,
        )
        category_box.pack(side="left", padx=(8, 0))
        category_box.bind("<<ComboboxSelected>>", self._category_changed)
        ttk.Checkbutton(
            filter_bar, text="LOD0 available only", variable=self.lod0_only_var,
            command=self._refresh_result,
        ).pack(side="left", padx=(12, 0))
        self.search_label = ttk.Label(filter_bar, text="Search:")
        self.search_label.pack(side="left", padx=(18, 0))
        self.animation_res_only_checkbox = ttk.Checkbutton(
            filter_bar, text=".res only", variable=self.animation_res_only_var,
            command=self._refresh_result,
        )
        self.animation_clip_search_checkbox = ttk.Checkbutton(
            filter_bar, text="Search clip names", variable=self.animation_clip_search_var,
            command=self._refresh_result,
        )
        self.animation_count_button = ttk.Button(
            filter_bar, text="Count clips", command=self._count_animation_clips,
        )
        search_entry = ttk.Entry(filter_bar, textvariable=self.search_var, width=26)
        search_entry.pack(side="left", padx=(8, 0), fill="x", expand=True)
        search_entry.bind("<KeyRelease>", self._schedule_search_refresh)
        ttk.Label(filter_bar, text="Exclude:").pack(side="left", padx=(12, 0))
        exclude_entry = ttk.Entry(filter_bar, textvariable=self.exclude_search_var, width=26)
        exclude_entry.pack(side="left", padx=(8, 0), fill="x", expand=True)
        exclude_entry.bind("<KeyRelease>", self._schedule_search_refresh)

        self.folder_scope = ttk.Frame(self, padding=(12, 0, 12, 8))
        self.folder_scope_var = tk.StringVar()
        ttk.Label(self.folder_scope, textvariable=self.folder_scope_var).pack(
            side="left", fill="x", expand=True,
        )
        ttk.Button(
            self.folder_scope, text="Back to all results", command=self._leave_folder_view,
        ).pack(side="right")

        self.animation_bar = ttk.Frame(self, padding=(12, 0, 12, 8))
        self.animation_bar_row1 = ttk.Frame(self.animation_bar)
        self.animation_bar_row1.pack(side="top", fill="x")
        self.animation_bar_row2 = ttk.Frame(self.animation_bar)
        self.animation_bar_row2.pack(side="top", fill="x", pady=(4, 0))
        self.animation_bar_row3 = ttk.Frame(self.animation_bar)
        self.animation_bar_row3.pack(side="top", fill="x", pady=(4, 0))
        self.animation_bar_row4 = ttk.Frame(self.animation_bar)
        self.animation_bar_row4.pack(side="top", fill="x", pady=(4, 0))
        ttk.Label(
            self.animation_bar_row1, textvariable=self.active_character_var,
            foreground="#176b3a",
        ).pack(side="left")
        ttk.Separator(self.animation_bar_row1, orient="vertical").pack(
            side="left", fill="y", padx=10,
        )
        self.animation_play_button = ttk.Button(
            self.animation_bar_row1, text="Play", command=self._play_active_animation,
            state="disabled",
        )
        self.animation_play_button.pack(side="left", padx=(0, 4))
        self.animation_stop_button = ttk.Button(
            self.animation_bar_row1, text="Stop", command=self._stop_active_animation,
            state="disabled",
        )
        self.animation_stop_button.pack(side="left", padx=4)
        self.prototype_button = ttk.Button(
            self.animation_bar_row1, text="Play prototype",
            command=self._toggle_interceptor_prototype, state="disabled",
        )
        self.prototype_button.pack(side="left", padx=4)
        self.prototype_export_button = ttk.Button(
            self.animation_bar_row1, text="Export standalone…",
            command=self._export_standalone_prototype, state="disabled",
        )
        self.prototype_export_button.pack(side="left", padx=4)
        ttk.Label(
            self.animation_bar_row1, textvariable=self.animation_state_var,
            foreground="#6b7280",
        ).pack(side="right")
        # The clip name can be a long real filename (e.g.
        # "ast_exm_sentinel_idle_1_bundlegenbp_bundlegen_win32_antstate.res")
        # -- packed last and left-aligned so it's the first thing clipped by
        # a narrow window, never the Play/Stop buttons above.
        ttk.Label(self.animation_bar_row1, textvariable=self.animation_clip_var).pack(
            side="left", padx=(8, 0),
        )
        self.animation_dump_button = ttk.Button(
            self.animation_bar_row2, text="Dump pose debug…", command=self._dump_animation_pose_debug,
            state="disabled",
        )
        self.animation_dump_button.pack(side="left", padx=(0, 4))
        self.animation_health_button = ttk.Button(
            self.animation_bar_row2, text="Channel health…", command=self._dump_animation_channel_health,
            state="disabled",
        )
        self.animation_health_button.pack(side="left", padx=4)
        self.animation_video_button = ttk.Button(
            self.animation_bar_row2, text="Save animation video…",
            command=self._save_animation_video, state="disabled",
        )
        self.animation_video_button.pack(side="left", padx=4)
        self.rigamate_extract_button = ttk.Button(
            self.animation_bar_row2, text="Extract Rigamate bank…",
            command=self._extract_resolved_rigamate_bank, state="disabled",
        )
        self.rigamate_extract_button.pack(side="left", padx=4)
        self.animation_loop_checkbox = ttk.Checkbutton(
            self.animation_bar_row2, text="Loop", variable=self.animation_loop_var,
            state="disabled",
        )
        self.animation_loop_checkbox.pack(side="left", padx=4)
        self.animation_filter_checkbox = ttk.Checkbutton(
            self.animation_bar_row2, text="Skip flagged channels", variable=self.animation_filter_unhealthy_var,
            state="disabled",
        )
        self.animation_filter_checkbox.pack(side="left", padx=4)
        ttk.Label(self.animation_bar_row2, textvariable=self.animation_speed_var).pack(
            side="left", padx=4,
        )
        ttk.Label(self.animation_bar_row3, text="Clip:").pack(side="left", padx=(0, 4))
        self.animation_group_combo = ttk.Combobox(
            self.animation_bar_row3, textvariable=self.animation_group_var,
            state="disabled", width=70,
        )
        self.animation_group_combo.pack(side="left", fill="x", expand=True)
        self.animation_group_combo.bind("<<ComboboxSelected>>", self._on_animation_group_selected)
        self.animation_clip_position_var = tk.StringVar(value="0 / 0")
        ttk.Label(self.animation_bar_row3, textvariable=self.animation_clip_position_var).pack(
            side="left", padx=(8, 4),
        )
        self.animation_previous_button = ttk.Button(
            self.animation_bar_row3, text="◀ Previous", command=lambda: self._step_animation_group(-1),
            state="disabled",
        )
        self.animation_previous_button.pack(side="left", padx=3)
        self.animation_next_button = ttk.Button(
            self.animation_bar_row3, text="Next ▶", command=lambda: self._step_animation_group(1),
            state="disabled",
        )
        self.animation_next_button.pack(side="left", padx=3)
        ttk.Label(self.animation_bar_row4, text="Find clip:").pack(side="left", padx=(0, 4))
        self.animation_clip_filter_entry = ttk.Entry(
            self.animation_bar_row4, textvariable=self.animation_clip_filter_var, width=36,
        )
        self.animation_clip_filter_entry.pack(side="left", fill="x", expand=True)
        self.animation_clip_filter_entry.bind("<KeyRelease>", self._update_clip_search_matches)
        self.animation_clip_filter_entry.bind("<Return>", lambda _event: self._step_clip_search(1))
        self.animation_clip_filter_entry.bind("<Shift-Return>", lambda _event: self._step_clip_search(-1))
        ttk.Label(
            self.animation_bar_row4, textvariable=self.animation_clip_match_var, width=16,
            anchor="center",
        ).pack(side="left", padx=(8, 4))
        self.animation_clip_match_previous_button = ttk.Button(
            self.animation_bar_row4, text="◀ Match",
            command=lambda: self._step_clip_search(-1), state="disabled",
        )
        self.animation_clip_match_previous_button.pack(side="left", padx=3)
        self.animation_clip_match_next_button = ttk.Button(
            self.animation_bar_row4, text="Next match ▶",
            command=lambda: self._step_clip_search(1), state="disabled",
        )
        self.animation_clip_match_next_button.pack(side="left", padx=3)

        pane = ttk.Panedwindow(self, orient="horizontal")
        self.pane = pane
        pane.pack(fill="both", expand=True, padx=12)
        list_frame = ttk.Frame(pane)
        columns = ("score", "mesh", "rig", "animations", "textures", "viewable")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        for key, label, width in (
            ("score", "Score", 65), ("mesh", "Character candidate / archive", 490), ("rig", "Rig", 55),
            ("animations", "Animations", 85), ("textures", "Textures", 70), ("viewable", "Preview", 80),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w" if key == "mesh" else "center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _: self.preview_selected())
        self.tree.bind("<Button-3>", self._show_folder_menu)
        self.viewer = MeshViewer(pane)
        pane.add(list_frame, weight=3)
        pane.add(self.viewer, weight=2)

        bottom = ttk.Frame(self, padding=12)
        bottom.pack(fill="x")
        bottom.columnconfigure(0, weight=1)
        # Width=1 prevents a long post-preview status message from requesting
        # thousands of pixels and pushing every action off the right edge.
        ttk.Label(
            bottom, textvariable=self.status_var, width=1, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.preview_button = ttk.Button(bottom, text="Preview", command=self.preview_selected)
        self.preview_button.grid(row=0, column=1, padx=(0, 8))
        self.assemble_button = ttk.Button(
            bottom, text="Assemble cosmetics", command=self._assemble_high_detail,
            state="disabled",
        )
        self.assemble_button.grid(row=0, column=2, padx=(0, 8))
        self.use_for_animation_check = ttk.Checkbutton(
            bottom, text="Use for animation", variable=self.use_for_animation_var,
            command=self._toggle_active_character, state="disabled",
        )
        self.use_for_animation_check.grid(row=0, column=3, padx=(0, 8))
        self.rig_button = ttk.Button(
            bottom, text="Inspect rig…", command=self.show_rig_inspector, state="disabled",
        )
        self.rig_button.grid(row=0, column=4, padx=(0, 8))
        ttk.Button(
            bottom, text="Extract selected…", command=self.extract_selected,
        ).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(
            bottom, text="Export report…", command=self.export_report,
        ).grid(row=0, column=6, padx=(0, 8))
        ttk.Button(bottom, text="Notices…", command=self.show_notices).grid(
            row=0, column=7,
        )
        self.bind_all("<KeyPress>", self._prototype_key_press, add="+")
        self.bind_all("<KeyRelease>", self._prototype_key_release, add="+")

    def choose(self) -> None:
        chosen = filedialog.askdirectory(title="Choose game installation")
        if chosen:
            self.path_var.set(chosen)

    @staticmethod
    def _javelin_family(asset) -> str | None:
        """Use explicit rig folders and Javelin aliases, not arbitrary words.

        Gameplay folders such as ``beam`` or ``wrist`` are not skeleton
        families. An unknown animation family is safer than a false mismatch.
        """
        value = (asset.internal_path or asset.relative_path).replace("\\", "/").casefold()
        directory = (value.rsplit("/", 1)[0] if "/" in value else "") + "/"
        segments = directory.strip("/").split("/")
        # "exo" is a generic archive directory, not the EXO Javelin rig.
        explicit = [match.group(1) for segment in segments if segment != "exo"
                    if (match := re.match(r"^(ex[a-z])(?:_|$)", segment))]
        if explicit:
            return explicit[-1]
        # Player-preview AntState records live directly under exo/playerpreview;
        # their filename, rather than a family folder, carries the exact rig.
        # Require the known family and Javelin name to agree before allowing
        # playback, since a mistaken match can distort the posed character.
        if "exo" in segments and asset.kind == "animation":
            filename = value.rsplit("/", 1)[-1]
            for family, name in (("exm", "lancer"), ("exh", "colossus"),
                                 ("exf", "interceptor"), ("exl", "storm")):
                if filename.startswith(f"{family}_{name}_"):
                    return family
        aliases = {"lancer": "exm", "colossus": "exh",
                   "interceptor": "exf", "storm": "exl"}
        for segment in segments:
            if segment in aliases:
                return aliases[segment]
        # Preserve the existing creature mesh convention (ara_worker, etc.)
        # without treating animation action/equipment folders as rig IDs.
        if asset.kind in {"mesh", "skeleton"}:
            for segment in segments:
                match = re.match(r"^([a-z]{2,8})_[a-z]", segment)
                if match and match.group(1) not in {"exo", "animation", "animations"}:
                    return match.group(1)
        return None

    def _category_changed(self, _event=None) -> None:
        if self.category_var.get() == "Animations":
            self.animation_res_only_checkbox.pack(
                side="left", padx=(12, 0), before=self.search_label,
            )
            self.animation_clip_search_checkbox.pack(
                side="left", padx=(8, 0), before=self.search_label,
            )
            self.animation_count_button.pack(
                side="left", padx=(8, 0), before=self.search_label,
            )
        else:
            self.animation_res_only_checkbox.pack_forget()
            self.animation_clip_search_checkbox.pack_forget()
            self.animation_count_button.pack_forget()
        self._refresh_result()
        self._update_animation_bar()

    @staticmethod
    def _animation_res_visible(category: str, res_only: bool, asset) -> bool:
        return (category != "Animations" or not res_only
                or asset.extension.casefold() == ".res")

    @staticmethod
    def _animation_display_count(bundle) -> int | str:
        """Show decoded clips, not the number of files represented by a row."""
        asset = bundle.mesh
        if asset.kind != "animation":
            return bundle.count("animation")
        if "decoded_clip_count" in asset.metadata:
            try:
                return max(0, int(asset.metadata["decoded_clip_count"]))
            except (TypeError, ValueError):
                pass
        if "clip_names" in asset.metadata:
            names = asset.metadata.get("clip_names")
            if isinstance(names, (tuple, list)):
                return len(names)
        if (asset.metadata.get("reader") == "frostbite-animation-record"
                and asset.extension.casefold() == ".res"):
            return "…"
        return bundle.count("animation")

    def _update_animation_count_cell(self, asset) -> None:
        for index, bundle in enumerate(self._displayed_bundles):
            candidate = bundle.mesh
            if not (candidate is asset or (
                candidate.path == asset.path
                and candidate.internal_path == asset.internal_path
            )):
                continue
            row = f"bundle-{index}"
            if not self.tree.exists(row):
                return
            values = list(self.tree.item(row, "values"))
            if len(values) >= 4:
                values[3] = self._animation_display_count(bundle)
                self.tree.item(row, values=values)
            return

    @staticmethod
    def _asset_matches_search(asset, include_terms: tuple[str, ...],
                              exclude_terms: tuple[str, ...]) -> bool:
        haystack = " ".join((
            asset.relative_path,
            asset.internal_path or "",
            str(asset.metadata.get("bundle_name", "")),
            " ".join(str(name) for name in asset.metadata.get("clip_names", ())),
        )).casefold()
        return (all(term in haystack for term in include_terms)
                and not any(term in haystack for term in exclude_terms))

    def _start_animation_clip_index(self, assets) -> None:
        if self._animation_clip_indexing or not self.result:
            return
        candidates = [asset for asset in assets
                      if asset.extension.casefold() == ".res"
                      and asset.metadata.get("reader") == "frostbite-animation-record"
                      and "clip_names" not in asset.metadata]
        if not candidates:
            return
        self._animation_clip_indexing = True
        self.animation_count_button.configure(state="disabled")
        self.status_var.set(f"Indexing clip names in {len(candidates):,} animation records…")
        root = self.result.engine_root or self.result.root

        def worker():
            indexed = 0
            try:
                from .frostbite import extract_frostbite_record_isolated
                from .frostbite_animation import decode_anthem_animation_stream
                from .frostbite_state import is_antstate_resource
                for asset in candidates:
                    try:
                        raw = extract_frostbite_record_isolated(root, asset)
                        names = tuple(clip.name for clip in decode_anthem_animation_stream(raw)) \
                            if is_antstate_resource(raw) else ()
                        asset.metadata["clip_names"] = names
                        asset.metadata["decoded_clip_count"] = len(names)
                        indexed += 1
                    except Exception:
                        asset.metadata["clip_names"] = ()
            finally:
                self.after(0, self._finish_animation_clip_index, indexed)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_animation_clip_index(self, indexed: int) -> None:
        self._animation_clip_indexing = False
        self.animation_count_button.configure(state="normal")
        self.status_var.set(f"Indexed clip names in {indexed:,} animation records.")
        self._refresh_result()

    def _count_animation_clips(self) -> None:
        if self._animation_clip_indexing or not self.result:
            return
        candidates = [
            asset for asset in self.result.assets
            if asset.extension.casefold() == ".res"
            and asset.metadata.get("reader") == "frostbite-animation-record"
            and "decoded_clip_count" not in asset.metadata
        ]
        if not candidates:
            self.status_var.set("All indexed animation RES records already have clip counts.")
            return
        self._animation_clip_indexing = True
        self.animation_count_button.configure(state="disabled")
        self.status_var.set(
            f"Counting clips in {len(candidates):,} animation records in the background…"
        )
        root = self.result.engine_root or self.result.root

        def worker() -> None:
            try:
                from .frostbite import count_frostbite_animation_clips_isolated
                counts = count_frostbite_animation_clips_isolated(root, candidates)
                self.after(0, self._finish_animation_clip_count, candidates, counts, None)
            except Exception as error:
                self.after(0, self._finish_animation_clip_count, candidates, [], error)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_animation_clip_count(self, candidates, counts, error) -> None:
        self._animation_clip_indexing = False
        self.animation_count_button.configure(state="normal")
        if error is not None:
            self.status_var.set(f"Animation clip counting failed safely: {error}")
            return
        resolved = 0
        for asset, count in zip(candidates, counts):
            if count is None:
                continue
            asset.metadata["decoded_clip_count"] = int(count)
            resolved += 1
        self._refresh_result()
        self.status_var.set(
            f"Clip counts loaded for {resolved:,}/{len(candidates):,} animation records."
        )

    def _update_animation_bar(self) -> None:
        visible = self._active_character is not None or self.category_var.get() == "Animations"
        if visible:
            if not self.animation_bar.winfo_manager():
                self.animation_bar.pack(fill="x", before=self.pane)
        else:
            self.animation_bar.pack_forget()

    def _toggle_active_character(self) -> None:
        if self._prototype_running and not self.use_for_animation_var.get():
            self._stop_interceptor_prototype()
        if self.use_for_animation_var.get():
            candidate = self._preview_character
            if candidate is None:
                self.use_for_animation_var.set(False)
                messagebox.showinfo(
                    "No rigged preview",
                    "Preview a skinned Javelin mesh with its decoded skeleton first.",
                )
                return
            self._active_character = candidate
            family = candidate.get("family") or "unknown rig"
            self.active_character_var.set(
                f"Active character: {candidate['name']} · {family} · "
                f"{len(candidate['skeleton'].joints):,} bones"
            )
            self.animation_clip_var.set("Animation: none selected")
            self.animation_state_var.set("Choose an animation and click Preview.")
            self.status_var.set(
                f"{candidate['name']} is locked as the active animation character. "
                "You can switch to Animations without unloading it."
            )
        else:
            self._active_character = None
            self.active_character_var.set("No active character")
            self.animation_clip_var.set("Animation: none selected")
            self.animation_state_var.set("Select a rigged mesh and enable Use for animation.")
            self.animation_play_button.configure(state="disabled")
            self.animation_stop_button.configure(state="disabled")
            self.animation_video_button.configure(state="disabled")
        self._update_animation_bar()
        self._update_prototype_button()
        if self.category_var.get() == "Animations":
            self._refresh_result()

    def _show_active_character(self) -> None:
        active = self._active_character
        if active is None:
            return
        self.viewer.set_meshes(active["meshes"], f"{active['name']} · active character")
        self.viewer.set_mesh_skeleton(active["skeleton"])
        self._rig_report = active.get("rig_report")
        self._rig_asset = active.get("asset")
        self.rig_button.configure(state="normal")
        self.use_for_animation_var.set(True)
        self.use_for_animation_check.configure(state="normal")

    def _animation_compatibility(self, asset) -> tuple[str, bool | None]:
        active = self._active_character
        if active is None:
            return "needs character", None
        active_family = active.get("family")
        clip_family = self._javelin_family(asset)
        if active_family and clip_family:
            if active_family == clip_family:
                return f"{clip_family} candidate", True
            return f"{clip_family} mismatch", False
        return "compatibility unknown", None

    def _on_animation_group_selected(self, _event=None) -> None:
        index = self.animation_group_combo.current()
        if index < 0 or index >= len(self._animation_group_options):
            return
        source = getattr(self, "_animation_group_source", None)
        if source is None:
            return
        if self._animation_start_time is not None:
            self._stop_active_animation()
        asset, title, original_info = source
        selected_info = dict(original_info)
        selected_info["_animation_group_default_index"] = index
        self._show_frostbite_animation_record(asset, title, selected_info)

    def _step_animation_group(self, direction: int) -> None:
        index = self.animation_group_combo.current() + direction
        if not 0 <= index < len(self._animation_group_options):
            return
        self.animation_group_combo.current(index)
        self._on_animation_group_selected()

    def _matching_clip_indices(self) -> list[int]:
        terms = self.animation_clip_filter_var.get().casefold().split()
        if not terms:
            return []
        return [
            index for index, option in enumerate(self._animation_group_options)
            if all(term in str(option["clip"].name).casefold() for term in terms)
        ]

    def _update_clip_search_matches(self, _event=None) -> None:
        self._animation_clip_search_cursor = None
        matches = self._matching_clip_indices()
        enabled = "normal" if matches else "disabled"
        self.animation_clip_match_previous_button.configure(state=enabled)
        self.animation_clip_match_next_button.configure(state=enabled)
        if not self.animation_clip_filter_var.get().strip():
            self.animation_clip_match_var.set("")
        elif matches:
            self.animation_clip_match_var.set(f"{len(matches)} match(es)")
        else:
            self.animation_clip_match_var.set("No matches")

    def _step_clip_search(self, direction: int) -> str:
        matches = self._matching_clip_indices()
        if not matches:
            self._update_clip_search_matches()
            return "break"
        current = self.animation_group_combo.current()
        if self._animation_clip_search_cursor not in matches:
            if direction > 0:
                candidates = [index for index in matches if index > current]
                selected = candidates[0] if candidates else matches[0]
            else:
                candidates = [index for index in matches if index < current]
                selected = candidates[-1] if candidates else matches[-1]
        else:
            position = matches.index(self._animation_clip_search_cursor)
            selected = matches[(position + direction) % len(matches)]
        self._animation_clip_search_cursor = selected
        self.animation_group_combo.current(selected)
        self.animation_clip_match_var.set(
            f"{matches.index(selected) + 1} / {len(matches)} matches"
        )
        self._on_animation_group_selected()
        return "break"

    def _play_active_animation(self) -> None:
        animation = self._active_animation
        character = self._active_character
        if animation is None or character is None:
            self.status_var.set("This control unlocks when the selected Frostbite clip keyframes are decoded.")
            return
        mapping, channels = animation["mapping"], animation["channels"]
        filtering_note = ""
        transition_delta = False
        if self._prototype_running:
            from .interceptor_prototype import transition_base_clip_name
            transition_delta = transition_base_clip_name(
                str(animation.get("name", "")),
            ) is not None
            if transition_delta:
                bases = animation.get("transition_base_rotations") or []
                if len(bases) == len(channels):
                    from .frostbite_animation_playback import compose_transition_loop_channels
                    channels = compose_transition_loop_channels(channels, bases)
                else:
                    transition_delta = False
        if self.animation_filter_unhealthy_var.get() and animation.get("health"):
            from .frostbite_animation_playback import filter_healthy_channels
            mapping, channels = filter_healthy_channels(mapping, channels, animation["health"])
            skipped = len(animation["mapping"]) - len(mapping)
            if skipped:
                filtering_note = f" ({skipped} flagged channel(s) skipped)"
        vector_mapping = list(animation.get("vector_mapping", []))
        vector_channels = list(animation.get("vector_channels", []))
        if self._prototype_running:
            # World/controller motion is applied by the prototype physics.
            # Keep actual deforming-joint translations (Hips, props, etc.) but
            # do not move the centred render mesh through trajectory/camera
            # helper joints a second time.
            from .viewer import animation_helper_visible
            pairs = [
                (bone_index, channel)
                for bone_index, channel in zip(vector_mapping, vector_channels)
                if 0 <= bone_index < len(character["skeleton"].joints)
                and animation_helper_visible(character["skeleton"].joints[bone_index][0])
            ]
            vector_mapping = [pair[0] for pair in pairs]
            vector_channels = [pair[1] for pair in pairs]
        self._animation_generation += 1
        self._animation_skin_running = False
        self._animation_playback = {
            "skeleton": character["skeleton"],
            "bind_rotations": character.get("bind_rotations") or [],
            "mapping": mapping,
            "channels": channels,
            "vector_mapping": vector_mapping,
            "vector_channels": vector_channels,
            "rotation_mode": "absolute",
            "timeline_end": animation.get("timeline_end"),
            "loop_seconds": max(
                0.1,
                float(animation.get("timeline_end") or 1)
                / (float(animation.get("fps") or 30.0)
                   * float(animation.get("time_scale") or 1.0)),
            ),
        }
        from .frostbite_animation_playback import evaluate_pose_transforms
        self._animation_playback["bind_world"] = evaluate_pose_transforms(
            character["skeleton"], character.get("bind_rotations") or [], [], [], 0.0,
        )[1]
        self._animation_start_time = self._clock()
        self.animation_speed_var.set(f"{float(animation.get('time_scale') or 1.0):g}×")
        self.animation_play_button.configure(state="disabled")
        self.animation_stop_button.configure(state="normal")
        self.status_var.set(
            f"Playing {animation['name']}{filtering_note} · "
            f"{animation.get('mapping_kind', 'named DOF')} mapping; "
            + ("transition endpoint plus normalized loop motion."
               if transition_delta else "absolute local rotations.")
        )
        self._animation_tick()

    def _stop_active_animation(self) -> None:
        if self._animation_after_id is not None:
            self.after_cancel(self._animation_after_id)
            self._animation_after_id = None
        self._animation_start_time = None
        self._animation_generation += 1
        self._animation_skin_running = False
        self.animation_play_button.configure(state="normal" if self._active_animation else "disabled")
        self.animation_stop_button.configure(state="disabled")
        if not self._prototype_running:
            self._show_active_character()

    @staticmethod
    def _clock() -> float:
        import time
        return time.monotonic()

    def _animation_tick(self) -> None:
        playback = getattr(self, "_animation_playback", None)
        if playback is None or self._animation_start_time is None:
            return
        from .frostbite_animation_playback import evaluate_pose_transforms
        from .frostbite_skinning import skin_meshes
        elapsed = self._clock() - self._animation_start_time
        loop_seconds = playback["loop_seconds"]
        if not self.animation_loop_var.get() and elapsed >= loop_seconds:
            self._stop_active_animation()
            return
        phase = (elapsed % loop_seconds) / loop_seconds
        self._last_animation_phase = phase
        pose, world = evaluate_pose_transforms(
            playback["skeleton"], playback["bind_rotations"],
            playback["mapping"], playback["channels"], phase,
            rotation_mode=playback.get("rotation_mode", "absolute"),
            vector_mapping=playback.get("vector_mapping"),
            vector_channels=playback.get("vector_channels"),
            timeline_end=playback.get("timeline_end"),
        )
        if self.viewer.mesh_visible.get():
            if not self._animation_skin_running:
                self._animation_skin_running = True
                generation = self._animation_generation
                source_meshes = self._active_character["meshes"]

                def skin_worker() -> None:
                    try:
                        skinned = skin_meshes(
                            source_meshes, playback["skeleton"], pose,
                            playback["bind_world"], world,
                        )
                        self.after(
                            0, self._finish_animation_skin,
                            generation, skinned, pose, None,
                        )
                    except Exception as error:
                        self.after(
                            0, self._finish_animation_skin,
                            generation, None, pose, str(error),
                        )

                threading.Thread(target=skin_worker, daemon=True).start()
        else:
            self.viewer.set_mesh_pose(None, pose, realtime=False)
        if self._prototype_running:
            lod = int((self._active_character or {}).get("lod", 5))
            delay = {0: 300, 1: 240, 2: 180, 3: 140, 4: 110, 5: 83}.get(lod, 140)
        else:
            delay = 33
        self._animation_after_id = self.after(delay, self._animation_tick)

    def _finish_animation_skin(
        self, generation: int, skinned, pose, error: str | None,
    ) -> None:
        if generation != self._animation_generation:
            return
        self._animation_skin_running = False
        if error is not None:
            self.status_var.set(f"Animation skinning paused safely: {error}")
            return
        if self._animation_start_time is not None:
            # Bitmap rasterization is also asynchronous and coalesces obsolete
            # requests, keeping Tk's input/event loop responsive.
            self.viewer.set_mesh_pose(skinned, pose, realtime=False)

    def _update_prototype_button(self) -> None:
        if not hasattr(self, "prototype_button"):
            return
        ready = (
            self._active_character is not None
            and self._active_character.get("family") == "exf"
            and bool(self._animation_group_options)
        )
        self.prototype_button.configure(
            state="normal" if ready or self._prototype_running else "disabled",
            text="Stop prototype" if self._prototype_running else "Play prototype",
        )
        if hasattr(self, "prototype_export_button"):
            self.prototype_export_button.configure(
                state="normal" if ready and not self._standalone_export_running else "disabled",
            )

    def _collect_standalone_animations(self) -> dict[str, dict[str, object]]:
        """Bake the controller's state clips through the verified live mapper."""
        source = getattr(self, "_animation_group_source", None)
        if source is None:
            raise ValueError("Preview the Interceptor animation bank first.")
        from .frostbite_animation_playback import (
            compose_transition_loop_channels, filter_healthy_channels,
        )
        from .interceptor_prototype import select_clip_index, transition_base_clip_name

        asset, title, original_info = source
        names = [option["clip"].name for option in self._animation_group_options]
        baked: dict[str, dict[str, object]] = {}
        previous_running = self._prototype_running
        self._prototype_running = True  # Enables authored transition anchoring.
        try:
            for state in ("idle", "walk", "sprint", "jump", "fall", "land", "hover", "flight", "glide", "dash"):
                index = select_clip_index(names, state)
                if index is None:
                    continue
                selected_info = dict(original_info)
                selected_info["_animation_group_default_index"] = index
                self._show_frostbite_animation_record(asset, title, selected_info)
                if self._active_animation is None:
                    continue
                animation = dict(self._active_animation)
                channels = list(animation["channels"])
                mapping = list(animation["mapping"])
                if transition_base_clip_name(str(animation.get("name", ""))) is not None:
                    bases = animation.get("transition_base_rotations") or []
                    if len(bases) == len(channels):
                        channels = compose_transition_loop_channels(channels, bases)
                if animation.get("health"):
                    mapping, channels = filter_healthy_channels(
                        mapping, channels, animation["health"],
                    )
                animation["mapping"] = mapping
                animation["channels"] = channels
                animation["transition_base_rotations"] = []
                animation["health"] = None
                baked[state] = animation
        finally:
            self._prototype_running = previous_running
            self._update_prototype_button()
        if "idle" not in baked:
            raise ValueError("The decoded bank did not produce the required Interceptor idle clip.")
        return baked

    def _export_standalone_prototype(self) -> None:
        character = self._active_character
        if character is None or character.get("family") != "exf":
            messagebox.showinfo(
                "Interceptor required",
                "Preview the Interceptor base mesh and enable Use for animation first.",
            )
            return
        destination_parent = filedialog.askdirectory(
            title="Choose where to create the standalone Interceptor prototype",
        )
        if not destination_parent:
            return
        if self._prototype_running:
            self._stop_interceptor_prototype()
        try:
            animations = self._collect_standalone_animations()
        except Exception as error:
            messagebox.showerror("Cannot bake prototype animations", str(error))
            return
        import time
        destination = Path(destination_parent) / "Interceptor-Standalone-0.28.12"
        if destination.exists():
            destination = destination.with_name(
                destination.name + "-" + time.strftime("%Y%m%d-%H%M%S"),
            )
        self._standalone_export_running = True
        self._update_prototype_button()
        self.status_var.set(
            "Baking the decoded Interceptor LODs once for the standalone runtime…",
        )
        root = self.result.engine_root or self.result.root if self.result else None
        threading.Thread(
            target=self._standalone_export_worker,
            args=(destination, root, dict(character), animations), daemon=True,
        ).start()

    def _standalone_export_worker(
        self, destination: Path, root: Path | None, character: dict[str, object],
        animations: dict[str, dict[str, object]],
    ) -> None:
        try:
            if root is None:
                raise ValueError("The scanned Anthem installation is no longer available for the one-time bake.")
            from .frostbite import preview_frostbite_meshset_isolated
            from .standalone_export import export_standalone_folder

            lod_meshes: dict[int, object] = {}
            available = sorted({int(value) for value in character.get("available_lods", [])})
            for lod in available:
                self.after(0, self.status_var.set, f"Baking standalone LOD{lod}…")
                preview = preview_frostbite_meshset_isolated(
                    root, character["asset"], lod_index=lod,
                )
                if preview[0] != "unavailable":
                    lod_meshes[int(preview[2])] = preview[1]
            current_lod = int(character.get("lod", -1))
            if current_lod >= 0 and current_lod not in lod_meshes:
                lod_meshes[current_lod] = character["meshes"]
            if not lod_meshes:
                raise ValueError("No Interceptor geometry could be baked into the standalone package.")
            payload = {
                "name": character.get("name", "Interceptor"),
                "lod_meshes": lod_meshes,
                "skeleton": character["skeleton"],
                "bind_rotations": character.get("bind_rotations") or [],
                "animations": animations,
            }
            export_standalone_folder(
                destination, payload, Path(__file__).resolve().parent,
            )
            self.after(0, self._finish_standalone_export, destination, None)
        except Exception as error:
            self.after(0, self._finish_standalone_export, destination, str(error))

    def _finish_standalone_export(self, destination: Path, error: str | None) -> None:
        self._standalone_export_running = False
        self._update_prototype_button()
        if error is not None:
            self.status_var.set(f"Standalone export failed safely: {error}")
            messagebox.showerror("Cannot export standalone prototype", error)
            return
        launcher = destination / "Play Interceptor Prototype.bat"
        self.status_var.set(f"Standalone Interceptor prototype exported to {destination}.")
        messagebox.showinfo(
            "Standalone prototype ready",
            f"Created:\n{destination}\n\n"
            f"Double-click {launcher.name} to play. It starts at LOD5 and no longer "
            "scans, decrypts, or reads the Anthem installation.",
        )

    def _toggle_interceptor_prototype(self) -> None:
        if self._prototype_running:
            self._stop_interceptor_prototype()
        else:
            self._start_interceptor_prototype()

    def _start_interceptor_prototype(self) -> None:
        character = self._active_character
        if character is None or character.get("family") != "exf":
            messagebox.showinfo(
                "Interceptor required",
                "Preview the Interceptor base mesh and enable Use for animation first.",
            )
            return
        if not self._animation_group_options:
            messagebox.showinfo(
                "Animation bank required",
                "Preview the 584-clip Interceptor AntState RES first, then click Play prototype.",
            )
            return
        from .interceptor_prototype import InterceptorController
        self._stop_active_animation()
        self._prototype_running = True
        self._prototype_controller = InterceptorController()
        self._prototype_held.clear()
        self._prototype_pressed.clear()
        self._prototype_animation_cache.clear()
        self._prototype_clip_index = None
        self._prototype_last_time = self._clock()
        self._prototype_global_lod = 5
        self.viewer.bones_visible.set(False)
        self.viewer.zoom = max(self.viewer.zoom, 1.35)
        self.animation_loop_var.set(True)
        self._update_prototype_button()
        # LOD5 is intentionally requested before any higher-detail geometry.
        # If the active preview is already LOD5 this is a no-op.
        self._set_prototype_lod(5)
        self._select_prototype_animation("idle")
        self.status_var.set(
            "Interceptor prototype running at global LOD5. Click the viewport, then use "
            "WASD, Shift, Space, F, Ctrl and Q; numpad 0–5 changes global detail."
        )
        self.viewer.canvas.focus_set()
        self._prototype_tick()

    def _stop_interceptor_prototype(self) -> None:
        if self._prototype_after_id is not None:
            self.after_cancel(self._prototype_after_id)
            self._prototype_after_id = None
        self._prototype_running = False
        self._prototype_controller = None
        self._prototype_held.clear()
        self._prototype_pressed.clear()
        self.viewer.set_prototype_overlay(None)
        self._stop_active_animation()
        self._update_prototype_button()
        self.status_var.set("Interceptor prototype stopped; the active character remains loaded.")

    @staticmethod
    def _prototype_key_name(event) -> str | None:
        folded = str(getattr(event, "keysym", "")).casefold()
        aliases = {
            "shift_l": "shift", "shift_r": "shift",
            "control_l": "control", "control_r": "control",
        }
        return aliases.get(folded, folded if folded in {
            "w", "a", "s", "d", "shift", "control", "space", "f", "q", "escape",
        } else None)

    @staticmethod
    def _prototype_numpad_lod(event) -> int | None:
        folded = str(getattr(event, "keysym", "")).casefold()
        direct = {f"kp_{value}": value for value in range(6)}
        direct.update({
            "kp_insert": 0, "kp_end": 1, "kp_down": 2,
            "kp_next": 3, "kp_left": 4, "kp_begin": 5,
        })
        if folded in direct:
            return direct[folded]
        keycode = int(getattr(event, "keycode", -1))
        if 96 <= keycode <= 101:
            return keycode - 96
        return int(folded) if folded in {"0", "1", "2", "3", "4", "5"} else None

    def _prototype_key_press(self, event):
        if not self._prototype_running:
            return None
        if isinstance(getattr(event, "widget", None), (tk.Entry, ttk.Entry, ttk.Combobox)):
            return None
        lod = self._prototype_numpad_lod(event)
        if lod is not None:
            self._set_prototype_lod(lod)
            return "break"
        key = self._prototype_key_name(event)
        if key is None:
            return None
        if key == "escape":
            self._stop_interceptor_prototype()
            return "break"
        if key not in self._prototype_held:
            self._prototype_pressed.add(key)
        self._prototype_held.add(key)
        return "break"

    def _prototype_key_release(self, event):
        if not self._prototype_running:
            return None
        if isinstance(getattr(event, "widget", None), (tk.Entry, ttk.Entry, ttk.Combobox)):
            return None
        key = self._prototype_key_name(event)
        if key is not None:
            self._prototype_held.discard(key)
            return "break"
        return None

    def _set_prototype_lod(self, lod: int) -> None:
        character = self._active_character
        if character is None:
            return
        available = [int(value) for value in character.get("available_lods", [])]
        if lod not in available:
            self.status_var.set(
                f"Global LOD{lod} was requested, but this Interceptor MeshSet stores only "
                + ", ".join(f"LOD{value}" for value in sorted(available)) + "."
            )
            return
        self._prototype_global_lod = lod
        if int(character.get("lod", -1)) != lod:
            self._request_frostbite_lod(character["asset"], lod)

    def _select_prototype_animation(self, state: str) -> None:
        from .interceptor_prototype import select_clip_index
        names = [option["clip"].name for option in self._animation_group_options]
        index = select_clip_index(names, state)
        if index is None or index == self._prototype_clip_index:
            return
        self._prototype_clip_index = index
        if index in self._prototype_animation_cache:
            self._stop_active_animation()
            self._active_animation = dict(self._prototype_animation_cache[index])
            self.animation_group_combo.current(index)
            self.animation_clip_position_var.set(f"{index + 1} / {len(names)}")
            self.animation_clip_var.set(f"Animation: {self._active_animation['name']} · prototype")
            self._play_active_animation()
            return
        source = getattr(self, "_animation_group_source", None)
        if source is None:
            return
        asset, title, original_info = source
        selected_info = dict(original_info)
        selected_info["_animation_group_default_index"] = index
        self._show_frostbite_animation_record(asset, title, selected_info)
        if self._active_animation is not None:
            self._prototype_animation_cache[index] = dict(self._active_animation)
            self._play_active_animation()

    def _prototype_tick(self) -> None:
        if not self._prototype_running or self._prototype_controller is None:
            return
        from .interceptor_prototype import PrototypeInput
        now = self._clock()
        elapsed = now - (self._prototype_last_time or now)
        self._prototype_last_time = now
        held, pressed = self._prototype_held, self._prototype_pressed
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
        self._prototype_pressed.clear()
        state = self._prototype_controller.update(elapsed, controls, -self.viewer.yaw)
        animation_state = "glide" if state.mode == "flight" and controls.sprint else state.mode
        self._select_prototype_animation(animation_state)
        speed = (state.vx * state.vx + state.vy * state.vy + state.vz * state.vz) ** 0.5
        clip = str((self._active_animation or {}).get("name", "finding animation…"))
        self.viewer.set_prototype_overlay({
            "x": state.x, "y": state.y, "z": state.z,
            "speed": speed, "state": state.mode,
            "lod": self._prototype_global_lod, "clip": clip,
        }, redraw=self._animation_start_time is None)
        self._prototype_after_id = self.after(16, self._prototype_tick)

    def _save_animation_video(self) -> None:
        """Render a clip frame-by-frame, then store a dependency-free MJPEG AVI."""
        animation = self._active_animation
        character = self._active_character
        if animation is None or character is None or self._video_export_running:
            return
        destination = filedialog.asksaveasfilename(
            title="Save animation video", defaultextension=".avi",
            filetypes=(("Motion JPEG video", "*.avi"),),
            initialfile=re.sub(r"[^A-Za-z0-9._-]+", "_", animation["name"]).strip("_") + ".avi",
        )
        if not destination:
            return
        self._stop_active_animation()
        mapping, channels = animation["mapping"], animation["channels"]
        if self.animation_filter_unhealthy_var.get() and animation.get("health"):
            from .frostbite_animation_playback import filter_healthy_channels
            mapping, channels = filter_healthy_channels(mapping, channels, animation["health"])
        fps = 15
        loop_seconds = max(
            0.1, float(animation.get("timeline_end") or 1)
            / (float(animation.get("fps") or 30.0)
               * float(animation.get("time_scale") or 1.0)),
        )
        frame_count = max(2, round(loop_seconds * fps))
        # A fixed, shareable resolution keeps an LOD0 export bounded even when
        # the on-screen viewport is very large. Rendering is deliberately
        # offline: every frame completes before the next one is encoded.
        width, height = 960, 720
        center, extent = self.viewer._mesh_center, self.viewer._mesh_extent
        yaw, pitch, zoom = self.viewer.yaw, self.viewer.pitch, self.viewer.zoom
        wireframe = bool(self.viewer.wireframe.get())
        skeleton = character["skeleton"]
        bind_rotations = character.get("bind_rotations") or []
        source_meshes = character["meshes"]
        vector_mapping = animation.get("vector_mapping", [])
        vector_channels = animation.get("vector_channels", [])
        timeline_end = animation.get("timeline_end")
        self._video_export_running = True
        self.animation_video_button.configure(state="disabled")
        self.animation_play_button.configure(state="disabled")
        self.status_var.set(
            f"Offline-rendering {frame_count} frames at {width}×{height}; the app stays usable."
        )

        def worker() -> None:
            try:
                from .frostbite_animation_playback import evaluate_pose_transforms
                from .frostbite_skinning import skin_meshes
                from .geometry import MeshData
                from .video_export import write_mjpeg_avi
                from .viewer import render_mesh_raster
                bind_world = evaluate_pose_transforms(
                    skeleton, bind_rotations, [], [], 0.0,
                )[1]

                def frames():
                    for frame_index in range(frame_count):
                        phase = frame_index / frame_count
                        pose, world = evaluate_pose_transforms(
                            skeleton, bind_rotations, mapping, channels, phase,
                            rotation_mode="absolute", vector_mapping=vector_mapping,
                            vector_channels=vector_channels, timeline_end=timeline_end,
                        )
                        skinned = skin_meshes(
                            source_meshes, skeleton, pose, bind_world, world,
                        )
                        normalized = [MeshData(
                            mesh.name,
                            [tuple((vertex[i] - center[i]) / extent for i in range(3))
                             for vertex in mesh.vertices],
                            mesh.faces, mesh.skin_bones, mesh.skin_weights,
                        ) for mesh in skinned]
                        if frame_index % 5 == 0:
                            self.after(
                                0, self.status_var.set,
                                f"Offline-rendering frame {frame_index + 1:,} / {frame_count:,}…",
                            )
                        yield render_mesh_raster(
                            normalized, width, height, yaw, pitch, zoom, wireframe,
                        )

                written = write_mjpeg_avi(
                    Path(destination), frames(), width=width, height=height, fps=fps,
                )
                self.after(0, self._finish_animation_video, destination, written, None)
            except Exception as error:
                self.after(0, self._finish_animation_video, destination, 0, str(error))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_animation_video(
        self, destination: str, frame_count: int, error: str | None,
    ) -> None:
        self._video_export_running = False
        enabled = "normal" if self._active_animation is not None else "disabled"
        self.animation_video_button.configure(state=enabled)
        self.animation_play_button.configure(state=enabled)
        if error:
            self.status_var.set(f"Animation video export failed: {error}")
            messagebox.showerror("Cannot save animation video", error)
            return
        self.status_var.set(f"Saved {frame_count:,}-frame animation video to {destination}")
        messagebox.showinfo(
            "Animation video saved",
            f"Saved an offline-rendered {frame_count:,}-frame Motion JPEG video to:\n{destination}",
        )

    def _dump_animation_pose_debug(self) -> None:
        animation = self._active_animation
        character = self._active_character
        if animation is None or character is None:
            self.status_var.set("Nothing to dump yet -- select a compatible animation with a character loaded first.")
            return
        from datetime import datetime
        from pathlib import Path
        from .frostbite_animation_playback import describe_pose_debug, format_pose_debug
        phase = self._last_animation_phase
        # Prefer the mapping/channels actually used for the last Play (which
        # respects the "Skip flagged channels" toggle) so this reflects what
        # was really rendered, not always the full unfiltered set.
        playback = getattr(self, "_animation_playback", None)
        mapping = playback["mapping"] if playback else animation["mapping"]
        channels = playback["channels"] if playback else animation["channels"]
        records = describe_pose_debug(
            character["skeleton"], character.get("bind_rotations") or [],
            mapping, channels, phase,
            rotation_mode=(playback.get("rotation_mode", "absolute")
                           if playback else "absolute"),
            vector_mapping=(playback.get("vector_mapping") if playback
                            else animation.get("vector_mapping")),
            vector_channels=(playback.get("vector_channels") if playback
                             else animation.get("vector_channels")),
            timeline_end=(playback.get("timeline_end") if playback
                          else animation.get("timeline_end")),
        )
        text = format_pose_debug(records, phase)
        text = (
            f"Character: {character.get('name', '?')}\n"
            f"Animation: {animation.get('name', '?')}\n"
            f"Total joints: {len(character['skeleton'].joints)}, "
            f"rotation channels available: {len(channels)}, "
            f"translation channels applied: {len(animation.get('vector_channels', []))}"
            f"{' (filtered to healthy only)' if playback and len(channels) < len(animation['channels']) else ''}\n\n"
        ) + text
        try:
            out_dir = Path.home() / "Desktop"
            if not out_dir.is_dir():
                out_dir = Path.cwd()
            out_path = out_dir / f"pose_debug_{datetime.now():%Y%m%d_%H%M%S}.txt"
            out_path.write_text(text, encoding="utf-8")
        except OSError as error:
            self.status_var.set(f"Could not write pose debug file: {error}")
            return
        self.status_var.set(f"Wrote pose debug snapshot (phase={phase:.3f}) to {out_path}")

    def _dump_animation_channel_health(self) -> None:
        animation = self._active_animation
        if animation is None:
            self.status_var.set("Nothing to check yet -- select a compatible animation with a character loaded first.")
            return
        health = animation.get("health")
        if not health:
            self.status_var.set("No channel health data available for this clip.")
            return
        from datetime import datetime
        from pathlib import Path
        from .frostbite_animation_channels import format_channel_health_report, format_channel_health_summary
        text = f"Animation: {animation.get('name', '?')}\n\n" + format_channel_health_report(health)
        try:
            out_dir = Path.home() / "Desktop"
            if not out_dir.is_dir():
                out_dir = Path.cwd()
            out_path = out_dir / f"channel_health_{datetime.now():%Y%m%d_%H%M%S}.txt"
            out_path.write_text(text, encoding="utf-8")
        except OSError as error:
            self.status_var.set(f"Could not write channel health file: {error}")
            return
        self.status_var.set(f"Wrote channel health report ({format_channel_health_summary(health)}) to {out_path}")

    def start_scan(self) -> None:
        entered = self.path_var.get().strip()
        virtual = parse_virtual_location(entered)
        if virtual is not None:
            archive, prefix = virtual
            if not archive.is_file():
                messagebox.showerror(
                    "Invalid archive",
                    "The part before :: must be an existing Frostbite archive file.",
                )
                return
            self._virtual_scope = (archive.resolve(), prefix)
            self._folder_focus.clear()
            self._folder_focus_labels.clear()
            self.search_var.set("")
            if self.result and self.result.engine_root:
                try:
                    archive.resolve().relative_to(self.result.root.resolve())
                except ValueError:
                    pass
                else:
                    self._show_result(self.result)
                    return
            from .engines import parent_engine_root
            _engine, engine_root = parent_engine_root(archive.parent)
            if engine_root is None:
                messagebox.showerror(
                    "Frostbite layout not found",
                    "The archive exists, but no parent Frostbite installation layout was found.",
                )
                return
            path = engine_root
        else:
            path = Path(entered)
            self._virtual_scope = None
        if not path.is_dir():
            messagebox.showerror("Invalid folder", "Choose an existing game directory.")
            return
        self.scan_button.configure(state="disabled")
        self._archive_previews.clear()
        self._indexed_archives.clear()
        self._indexing_categories.clear()
        self._unity_indexed_categories.clear()
        self._frostbite_inventory_started = False
        self._frostbite_skeleton_cache.clear()
        self._frostbite_virtual_asset_cache.clear()
        self._preview_character = None
        self._active_character = None
        self._resolved_rigamate_asset = None
        self._resolved_rigamate_raw = None
        self.rigamate_extract_button.configure(state="disabled")
        self.use_for_animation_var.set(False)
        self.use_for_animation_check.configure(state="disabled")
        self.active_character_var.set("No active character")
        self.animation_clip_var.set("Animation: none selected")
        self.animation_state_var.set("Select an animation record to inspect it.")
        self._update_animation_bar()
        self._expanded_folders.clear()
        self._folder_focus.clear()
        self._folder_focus_labels.clear()
        self._folder_match_counts.clear()
        self.folder_scope.pack_forget()
        self.status_var.set("Scanning read-only…")
        for row in self.tree.get_children():
            self.tree.delete(row)
        threading.Thread(target=self._scan_worker, args=(path,), daemon=True).start()

    def _scan_worker(self, path: Path) -> None:
        try:
            result = scan(path, progress=lambda count, _: self.after(0, self.status_var.set, f"Scanning… {count:,} files"))
            self.after(0, self._show_result, result)
        except Exception as error:
            self.after(0, messagebox.showerror, "Scan failed", str(error))
            self.after(0, self.scan_button.configure, {"state": "normal"})

    def _show_result(self, result) -> None:
        selected = self.selected_bundle()
        selected_key = (
            (selected.mesh.path, selected.mesh.internal_path) if selected else None
        )
        self.result = result
        for row in self.tree.get_children():
            self.tree.delete(row)
        category = self.category_var.get()
        expanded = self._expanded_folders.setdefault(category, set())
        focused_folder = self._folder_focus.get(category)
        search_terms = tuple(
            term for term in self.search_var.get().casefold().split() if term
        )
        exclude_terms = tuple(
            term for term in self.exclude_search_var.get().casefold().split() if term
        )

        def search_matches(asset) -> bool:
            return self._asset_matches_search(asset, search_terms, exclude_terms)

        def in_virtual_scope(asset) -> bool:
            if self._virtual_scope is None:
                return True
            return asset_in_virtual_scope(asset, *self._virtual_scope)

        if category == self.CATEGORY_OPTIONS[0]:
            category_candidates = [
                bundle for bundle in result.bundles
                if category_for_asset(bundle.mesh) == category
                and in_virtual_scope(bundle.mesh)
            ]
            self.tree.heading("mesh", text="Character candidate / archive")
            eligible = [
                bundle for bundle in category_candidates
                if self.show_all_var.get() or bundle.score >= 20 or bundle.mesh.kind == "container"
            ]
            hidden = len(category_candidates) - len(eligible)
            if self.lod0_only_var.get():
                eligible = [
                    bundle for bundle in eligible
                    if bundle.mesh.metadata.get("lod0_available") is True
                ]
            if focused_folder is not None:
                eligible = items_in_folder(eligible, lambda bundle: bundle.mesh, focused_folder)
                expanded.add(focused_folder)
            self._displayed_bundles, self._folder_match_counts = folder_rows(
                eligible, lambda bundle: bundle.mesh,
                lambda bundle: search_matches(bundle.mesh), expanded,
            )
        else:
            if (category == "Animations" and self.animation_clip_search_var.get()
                    and (search_terms or exclude_terms) and self.result):
                self._start_animation_clip_index(result.assets)
            self.tree.heading("mesh", text="Asset / archive")
            # Non-character categories are an inventory view, so they include
            # low-confidence files by design; the character checkbox remains
            # dedicated to the default ranking view.
            matching = [
                asset for asset in result.assets
                if (category == "All" or category_for_asset(asset) == category)
                and in_virtual_scope(asset)
                and self._animation_res_visible(category, self.animation_res_only_var.get(), asset)
            ]
            # Show decoded/internal assets before their parent archive. Clicking
            # an .img row means "probe this container"; clicking its child WDR,
            # WFT, or WTD row previews the actual asset directly.
            matching.sort(key=lambda asset: (asset.kind == "container", asset.relative_path.lower()))
            category_bundles = [
                CharacterBundle(
                    name=asset.relative_path,
                    mesh=asset,
                    score=10.0,
                    reasons=[f"classified as {category.lower()}"],
                )
                for asset in matching
            ]
            if focused_folder is not None:
                category_bundles = items_in_folder(
                    category_bundles, lambda bundle: bundle.mesh, focused_folder,
                )
                expanded.add(focused_folder)
            self._displayed_bundles, self._folder_match_counts = folder_rows(
                category_bundles, lambda bundle: bundle.mesh,
                lambda bundle: search_matches(bundle.mesh), expanded,
            )
            hidden = 0
        for index, bundle in enumerate(self._displayed_bundles):
            unity_type = bundle.mesh.metadata.get("unity_type")
            if bundle.mesh.metadata.get("preview_unavailable_reason"):
                preview_state = "no geometry"
            elif unity_type == "Skeleton":
                preview_state = "joints"
            elif unity_type in {"Mesh", "Texture2D", "Sprite"}:
                preview_state = "ready" if unity_type == "Mesh" else "2D ready"
            elif bundle.mesh.metadata.get("reader") == "frostbite-animation-record":
                preview_state = self._animation_compatibility(bundle.mesh)[0]
            elif unity_type == "AnimationClip":
                preview_state = "indexed"
            elif bundle.mesh.metadata.get("reader") == "frostbite-skeleton-record":
                preview_state = "rig indexed"
            elif bundle.mesh.kind == "skeleton":
                preview_state = "joints"
            elif bundle.mesh.kind == "texture" and bundle.mesh.extension in IMAGE_EXTENSIONS:
                preview_state = "2D ready"
            elif bundle.mesh.kind == "texture" and bundle.mesh.extension == ".wtd":
                preview_state = "2D on demand"
            elif bundle.mesh.extension == ".meshset":
                preview_state = (
                    "LOD0 ready" if bundle.mesh.metadata.get("lod0_available") is True
                    else "ready"
                )
            elif bundle.mesh.kind != "container" and bundle.mesh.extension in EMBEDDED_PREVIEW_EXTENSIONS:
                preview_state = "ready"
            elif bundle.mesh.kind == "container":
                from .adapters import adapter_for
                capability = {
                    "Unity": "unity-read",
                    "Rockstar RAGE (GTA IV)": "rpf3-read",
                    "Frostbite": "frostbite-metadata-read",
                }.get(bundle.mesh.engine)
                preview_state = "on demand" if adapter_for(bundle.mesh, capability) else "unsupported"
            else:
                preview_state = "unsupported"
            self.tree.insert("", "end", iid=f"bundle-{index}", values=(
                f"{bundle.score:.1f}", bundle.mesh.relative_path,
                "yes" if bundle.count("skeleton") else "—", self._animation_display_count(bundle),
                bundle.count("texture"), preview_state,
            ))
        selected_row = None
        if selected_key is not None:
            selected_row = next((
                f"bundle-{index}" for index, bundle in enumerate(self._displayed_bundles)
                if (bundle.mesh.path, bundle.mesh.internal_path) == selected_key
            ), None)
        if self._displayed_bundles:
            selected_row = selected_row or "bundle-0"
            self.tree.selection_set(selected_row)
            self.tree.see(selected_row)
        engines = ", ".join(result.engine_hints) or "unknown/generic"
        warning_text = f" · {len(result.warnings)} notice(s)" if result.warnings else ""
        hidden_text = f" · {hidden:,} low-confidence asset(s) hidden" if hidden else ""
        lod0_text = " · LOD0 available only" if (
            category == self.CATEGORY_OPTIONS[0] and self.lod0_only_var.get()
        ) else ""
        expanded_visible = sum(key in expanded for key in self._folder_match_counts)
        if focused_folder is not None:
            folder_label = self._folder_focus_labels.get(category, "selected folder")
            self.folder_scope_var.set(
                f"Folder-only view: {folder_label} · {len(self._displayed_bundles):,} {category.lower()} file(s)"
            )
            if not self.folder_scope.winfo_manager():
                self.folder_scope.pack(fill="x", before=self.pane)
            folder_text = f" · folder-only view: {folder_label}"
        elif self._virtual_scope is not None:
            archive, prefix = self._virtual_scope
            virtual_label = f"{archive.name}::{prefix}"
            self.folder_scope_var.set(
                f"Virtual folder: {virtual_label} · {len(self._displayed_bundles):,} {category.lower()} file(s)"
            )
            if not self.folder_scope.winfo_manager():
                self.folder_scope.pack(fill="x", before=self.pane)
            folder_text = f" · virtual folder: {virtual_label}"
        else:
            self.folder_scope.pack_forget()
            folder_text = (
                f" · {len(self._folder_match_counts):,} folder(s)"
                f" · {expanded_visible:,} expanded" if self._folder_match_counts else ""
            )
        self.status_var.set(
            f"Scanned {result.files_seen:,} files · {len(self._displayed_bundles):,} likely candidates"
            f"{folder_text}{lod0_text}{hidden_text} · engine: {engines}{warning_text}"
        )
        self._update_animation_bar()
        self.scan_button.configure(state="normal")
        if category == "Skeletons" and not self._displayed_bundles and "Rockstar RAGE (GTA IV)" in result.engine_hints:
            self.viewer.clear(
                "No verified GTA IV skeleton hierarchy was decoded.\n\n"
                "WFT resources can contain rigged models, but a model or transform table is not itself a safe bone hierarchy."
            )
            self.status_var.set(
                "No verified GTA IV skeletons decoded · model meshes remain available under Characters / meshes"
            )
        self._maybe_index_category(category)

    def _refresh_result(self) -> None:
        if self.result:
            self._show_result(self.result)

    def _schedule_search_refresh(self, _event=None) -> None:
        """Debounce live filtering so large inventories remain responsive."""
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(180, self._apply_search_refresh)

    def _apply_search_refresh(self) -> None:
        self._search_after_id = None
        self._refresh_result()

    def _archive_targets_for(self, category: str):
        if not self.result:
            return []
        # Unity SerializedFiles and AssetBundles are inventories of typed
        # objects. Index them once, in the background, then let the generic
        # categories classify their real Mesh/Texture/Sprite/Animation names.
        unity = [
            asset for asset in self.result.assets
            if asset.kind == "container"
            and asset.engine == "Unity"
            and asset.path not in self._indexed_archives
        ]
        if unity and category != "Containers / archives" and category not in self._unity_indexed_categories:
            from .unity import select_unity_containers
            self._unity_indexed_categories.add(category)
            return select_unity_containers(unity, category, limit=12)
        if category != "Containers / archives" and not self._frostbite_inventory_started:
            # A single isolated installation pass follows all typed TOC/SB
            # MeshSet records into CAS. One representative target starts it;
            # the adapter resolves the full layout itself.
            frostbite_candidates = [
                asset for asset in self.result.assets
                if asset.engine == "Frostbite"
                and asset.path not in self._indexed_archives
            ]
            if (
                not frostbite_candidates
                and self.result.engine_root
                and "Frostbite" in self.result.engine_hints
            ):
                from .models import AssetRecord
                frostbite_candidates = [AssetRecord(
                    path=self.result.root,
                    relative_path=".",
                    kind="container",
                    extension="",
                    size=0,
                    engine="Frostbite",
                )]
            if frostbite_candidates:
                self._frostbite_inventory_started = True
                # Any Frostbite container can trigger the installation-level
                # reader. This matters when the chosen subfolder has only CAS
                # payloads and its TOC/SB metadata lives in an ancestor.
                return frostbite_candidates[:1]
        if category == self.CATEGORY_OPTIONS[0]:
            return []
        containers = [
            asset for asset in self.result.assets
            if asset.kind == "container"
            and asset.engine == "Rockstar RAGE (GTA IV)"
            and asset.path not in self._indexed_archives
        ]
        if category in {"Map parts", "Props", "Weapons & carryables"}:
            return [asset for asset in containers if category_for_asset(asset) == category]
        # WFT is a fragment/model, not a verified skeleton hierarchy. Do not
        # burn CPU indexing every GTA archive when the user selects Skeletons.
        return []

    def _maybe_index_category(self, category: str) -> None:
        # UnityPy can consume substantial memory while opening a bundle. Never
        # let rapid category changes run multiple archive passes concurrently.
        # _finish_archive_index refreshes the currently selected category, so
        # the newest request starts automatically after this pass finishes.
        if self._indexing_categories:
            self.status_var.set("Finishing the current safe archive pass before changing categories…")
            return
        targets = self._archive_targets_for(category)
        if not targets or category in self._indexing_categories:
            return
        self._indexing_categories.add(category)
        # Reserve targets before starting the worker so changing the category
        # cannot launch a second pass over the same large Unity files.
        self._indexed_archives.update(asset.path for asset in targets)
        self.status_var.set(f"Found {len(targets):,} relevant archive(s); indexing {category.lower()} in the background…")
        threading.Thread(target=self._index_archives_worker, args=(category, targets), daemon=True).start()

    def _index_archives_worker(self, category: str, targets) -> None:
        discovered = []
        warnings = []
        for index, asset in enumerate(targets, 1):
            self.after(
                0, self.status_var.set,
                f"Indexing {category.lower()}: archive {index:,}/{len(targets):,} · {asset.path.name}",
            )
            try:
                if asset.engine == "Unity":
                    from .adapters import ensure_adapter
                    ensure_adapter(asset, "unity-read", progress=lambda text: self.after(0, self.status_var.set, text))
                    from .unity import inspect_unity_container_isolated
                    found, notices = inspect_unity_container_isolated(self.result.root, asset, timeout=30)
                elif asset.engine == "Rockstar RAGE (GTA IV)":
                    from .rage import IMG3_MAGIC, inspect_gtaiv_archive
                    with asset.path.open("rb") as stream:
                        signature = stream.read(4)
                    if len(signature) != 4 or struct.unpack("<I", signature)[0] != IMG3_MAGIC:
                        from .adapters import ensure_adapter
                        ensure_adapter(asset, "rpf3-read", progress=lambda text: self.after(0, self.status_var.set, text))
                    found, notices = inspect_gtaiv_archive(self.result.root, asset)
                elif asset.engine == "Frostbite":
                    from .frostbite import inspect_frostbite_installation_isolated
                    found, notices = inspect_frostbite_installation_isolated(
                        self.result.engine_root or self.result.root, timeout=300,
                    )
                else:
                    found, notices = [], [
                        f"No metadata reader is registered for {asset.engine or asset.extension}."
                    ]
                discovered.extend(found)
                warnings.extend(notices)
            except Exception as error:
                warnings.append(f"Could not index {asset.relative_path}: {error}")
            self._indexed_archives.add(asset.path)
        engines = {asset.engine for asset in targets}
        self.after(0, self._finish_archive_index, category, discovered, warnings, engines)

    def _finish_archive_index(self, category: str, discovered, warnings, engines=None) -> None:
        self._indexing_categories.discard(category)
        if not self.result:
            return
        if engines == {"Frostbite"} and self.result.engine_root:
            discovered = [
                asset for asset in discovered if self._asset_in_selected_scope(asset)
            ]
        existing = {(asset.path, asset.internal_path) for asset in self.result.assets}
        self.result.assets.extend(
            asset for asset in discovered if (asset.path, asset.internal_path) not in existing
        )
        self.result.warnings.extend(warnings)
        references_only = discovered and all(
            asset.metadata.get("reader") == "frostbite-reference" for asset in discovered
        )
        if engines == {"Frostbite"} and references_only:
            # These are metadata references, not decoded assets. Running the
            # general relationship matcher across thousands of repeated path
            # tokens caused a huge all-to-all sort on the Tk thread. Add mesh
            # references as lightweight candidates and leave existing decoded
            # bundles untouched.
            existing_bundle_keys = {
                (bundle.mesh.path, bundle.mesh.internal_path) for bundle in self.result.bundles
            }
            self.result.bundles.extend(
                CharacterBundle(
                    name=asset.relative_path,
                    mesh=asset,
                    score=2.0,
                    reasons=["named Frostbite metadata reference; payload not decoded"],
                )
                for asset in discovered
                if asset.kind == "mesh"
                and (asset.path, asset.internal_path) not in existing_bundle_keys
            )
        else:
            # Decoded/internal assets participate in normal neutral grouping.
            from .grouping import build_bundles
            from .rage import known_archive_bundles
            self.result.bundles = build_bundles(self.result.assets)
            self.result.bundles.extend(known_archive_bundles(self.result.assets, self.result.bundles))
        self.result.bundles.sort(key=lambda bundle: (-bundle.score, bundle.mesh.relative_path.lower()))
        self._show_result(self.result)

    def _asset_in_selected_scope(self, asset) -> bool:
        """Keep full-layout discoveries inside the folder the user selected."""
        if not self.result or not self.result.engine_root:
            return True
        return asset_in_physical_scope(
            asset, self.result.root, self.result.engine_root,
        )

    def _show_folder_menu(self, event) -> None:
        """Offer expansion for the loose or virtual folder under the pointer."""
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        bundle = self.selected_bundle()
        if bundle is None:
            return
        category = self.category_var.get()
        key = asset_folder_key(bundle.mesh)
        count = self._folder_match_counts.get(key, 1)
        folder = asset_folder_label(bundle.mesh)
        if len(folder) > 64:
            folder = "…" + folder[-63:]

        menu = tk.Menu(self, tearoff=False)
        if bundle.mesh.internal_path:
            virtual_parent = str(Path(bundle.mesh.internal_path.replace("\\", "/")).parent).replace("\\", "/")
            if virtual_parent not in {"", "."}:
                short_virtual = virtual_parent if len(virtual_parent) <= 64 else "…" + virtual_parent[-63:]
                menu.add_command(
                    label=f"Open virtual folder {short_virtual}",
                    command=lambda: self._open_virtual_folder(bundle.mesh, virtual_parent),
                )
                menu.add_separator()
        if category in self._folder_focus:
            menu.add_command(
                label=f"Back to all {category.lower()} results",
                command=self._leave_folder_view,
            )
        elif count > 1:
            menu.add_command(
                label=f"Show only these {count:,} {category.lower()} files from {folder}",
                command=lambda: self._set_folder_focus(category, key, folder),
            )
        else:
            menu.add_command(
                label=f"Only matching {category.lower()} file in {folder}", state="disabled",
            )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_folder_focus(
        self, category: str, key: tuple[str, str], folder: str,
    ) -> None:
        self._folder_focus[category] = key
        self._folder_focus_labels[category] = folder
        self._expanded_folders.setdefault(category, set()).add(key)
        self.search_var.set("")
        self._refresh_result()

    def _open_virtual_folder(self, asset, prefix: str) -> None:
        archive = (asset.container_path or asset.path).resolve()
        self._virtual_scope = (archive, prefix)
        self.path_var.set(f"{archive}::{prefix}")
        self._folder_focus.clear()
        self._folder_focus_labels.clear()
        self.search_var.set("")
        self._refresh_result()

    def _leave_folder_view(self) -> None:
        category = self.category_var.get()
        key = self._folder_focus.pop(category, None)
        self._folder_focus_labels.pop(category, None)
        if key is not None:
            self._expanded_folders.setdefault(category, set()).discard(key)
        elif self._virtual_scope is not None:
            self._virtual_scope = None
            if self.result:
                self.path_var.set(str(self.result.root))
        self._refresh_result()

    def selected_bundle(self):
        if not self.result or not self.tree.selection():
            return None
        row = self.tree.selection()[0]
        try:
            return self._displayed_bundles[int(row.removeprefix("bundle-"))]
        except (ValueError, IndexError):
            return None

    def preview_selected(self) -> None:
        bundle = self.selected_bundle()
        if not bundle:
            messagebox.showinfo("Nothing selected", "Scan and select a candidate first.")
            return
        self.viewer.clear_detail_levels()
        self._assembly_source = None
        self.assemble_button.configure(state="disabled")
        self._rig_report = None
        self._rig_asset = None
        self.rig_button.configure(state="disabled")
        self.preview_button.configure(state="disabled")
        self.status_var.set(f"Preparing embedded preview for {bundle.name}…")
        # Tk variables must only be read on the Tk thread. Capture the current
        # filter before starting the decoder worker.
        category = self.category_var.get()
        threading.Thread(target=self._preview_worker, args=(bundle, category), daemon=True).start()

    def _preview_worker(self, bundle, category: str) -> None:
        try:
            asset = bundle.mesh
            meshes = None
            title = bundle.name
            if asset.metadata.get("reader") == "unity":
                from .adapters import ensure_adapter
                ensure_adapter(asset, "unity-read", progress=lambda text: self.after(0, self.status_var.set, text))
                from .unity import preview_unity_asset_isolated
                viewer_kind, payload = preview_unity_asset_isolated(asset)
                if viewer_kind == "mesh":
                    self.after(0, self.viewer.set_meshes, payload, title)
                    message = f"Previewing {title} inside Game Asset Explorer."
                elif viewer_kind == "image":
                    self.after(0, self.viewer.set_image, payload, title)
                    message = f"Showing {title} as a 2D image inside Game Asset Explorer."
                elif viewer_kind == "skeleton":
                    self.after(0, self.viewer.set_skeleton, payload, title)
                    message = f"Showing the verified {title} hierarchy inside Game Asset Explorer."
                else:
                    raise MeshFormatError(f"Unity preview returned an unknown type: {viewer_kind}")
                self.after(0, self.status_var.set, message)
                return
            if asset.metadata.get("reader") == "frostbite-reference":
                raise MeshFormatError(
                    f"{asset.internal_path} is a named Frostbite metadata reference, not a decoded payload. "
                    "Its EBX/RES/chunk and CAS data still require a matching Frostbite schema decoder."
                )
            if asset.metadata.get("reader") == "frostbite-animation-record":
                if not self.result:
                    raise ValueError("Scan data is no longer available.")
                from .frostbite import extract_frostbite_record_isolated
                raw = extract_frostbite_record_isolated(
                    self.result.engine_root or self.result.root, asset,
                )
                info: dict[str, object] = {
                    "record_kind": asset.metadata.get("record_kind", "record"),
                    "bytes": len(raw),
                }
                if asset.metadata.get("record_kind") == "ebx":
                    from .frostbite_ebx import inspect_anthem_ebx_record
                    try:
                        info.update(inspect_anthem_ebx_record(raw))
                    except MeshFormatError as error:
                        info["inspection_error"] = str(error)
                elif asset.metadata.get("record_kind") == "res":
                    from .frostbite_state import is_antstate_resource
                    if is_antstate_resource(raw):
                        from .frostbite_animation import (
                            decode_anthem_animation_stream, decode_bank_pointers,
                            decode_primary_rig_keys,
                        )
                        clips = decode_anthem_animation_stream(raw)
                        rig_keys = decode_primary_rig_keys(raw)
                        if len(rig_keys) == 1:
                            info["_primary_rig_key"] = rig_keys[0]
                        bank_pointers = decode_bank_pointers(raw)
                        clip_options = []
                        for clip_index, clip in enumerate(clips):
                            moving_rotations = sum(
                                len(channel.values) > 1
                                for channel in clip.quaternion_channels
                            )
                            constant_rotations = (
                                len(clip.quaternion_channels) - moving_rotations
                            )
                            clip_options.append({
                                "index": clip_index,
                                "clip": clip,
                                "label": (
                                    f"{clip.name} ({clip.frame_count} frames, "
                                    f"{moving_rotations} moving + "
                                    f"{constant_rotations} constant rotations)"
                                ),
                            })
                        if clip_options:
                            info["_animation_group_options"] = clip_options
                            info["_animation_group_default_index"] = 0
                            info["decoded_clip_count"] = len(clips)
                            info["quaternion_channel_count"] = len(clips[0].quaternion_channels)
                            info["animation_channel_count"] = clips[0].channel_count
                            info["animation_frame_count"] = clips[0].frame_count
                            info["dof_ids_decoded"] = clips[0].mapped
                            info["needs_rig_bank"] = True
                        rigamate = next((
                            pointer for pointer in bank_pointers
                            if "rigamate" in pointer.name.casefold()
                        ), None)
                        if rigamate is not None:
                            info["rigamate_pointer"] = rigamate.name
                            info["rigamate_key"] = rigamate.subject_key.hex()
                            info["rigamate_external"] = rigamate.external
                            if rigamate.external:
                                self.after(
                                    0, self.status_var.set,
                                    f"Resolving {rigamate.name} key "
                                    f"{rigamate.subject_key.hex()} across Anthem RES records…",
                                )
                                from .frostbite import resolve_frostbite_virtual_asset_key_isolated
                                try:
                                    engine_root = self.result.engine_root or self.result.root
                                    cache_key = (str(engine_root.resolve()), rigamate.subject_key)
                                    resolution = self._frostbite_virtual_asset_cache.get(cache_key)
                                    if resolution is None:
                                        resolution = resolve_frostbite_virtual_asset_key_isolated(
                                            engine_root, asset, rigamate.subject_key,
                                        )
                                        self._frostbite_virtual_asset_cache[cache_key] = resolution
                                    resolved, bank_raw, scanned, exhaustive = resolution
                                    info["rigamate_scanned_records"] = scanned
                                    info["rigamate_search_exhaustive"] = exhaustive
                                    if resolved is not None and bank_raw is not None:
                                        info["rigamate_resolved_asset"] = (
                                            resolved.internal_path or resolved.relative_path
                                        )
                                        info["rigamate_resolved_bytes"] = len(bank_raw)
                                        info["_rigamate_resolved_record"] = resolved
                                        info["_rigamate_resolved_raw"] = bank_raw
                                        if clip_options:
                                            from .frostbite_rigamate import inspect_rigamate_dof_types
                                            types = inspect_rigamate_dof_types(bank_raw, clips[0])
                                            if types is not None:
                                                info["rigamate_dof_types"] = (
                                                    f"{len(types.joint_names)} named joints; "
                                                    f"{len(types.quaternion_channel_ids)} rotation, "
                                                    f"{len(types.vector_channel_ids)} position and "
                                                    f"{len(types.float_channel_ids)} scalar DOF IDs verified"
                                                )
                                except MeshFormatError as error:
                                    info["rigamate_resolution_error"] = str(error)
                        if len(rig_keys) == 1 and clip_options:
                            # The family-neutral, named DOF table is owned by
                            # PrimaryRig.  EXM's smaller Rigamate resource also
                            # happened to contain it, but EXF points Rigamate at
                            # a curve bank with no joint names.  Resolve the
                            # authored PrimaryRig key explicitly for every
                            # Javelin family.
                            primary_key = rig_keys[0]
                            self.after(
                                0, self.status_var.set,
                                f"Resolving the authored PrimaryRig map {primary_key.hex()}…",
                            )
                            from .frostbite import resolve_frostbite_primary_rig_key_isolated
                            try:
                                engine_root = self.result.engine_root or self.result.root
                                cache_key = (
                                    str(engine_root.resolve()), primary_key, "primary-rig",
                                )
                                resolution = self._frostbite_virtual_asset_cache.get(cache_key)
                                if resolution is None:
                                    resolution = resolve_frostbite_primary_rig_key_isolated(
                                        engine_root, asset, primary_key,
                                    )
                                    self._frostbite_virtual_asset_cache[cache_key] = resolution
                                primary_asset, primary_raw, scanned, exhaustive = resolution
                                info["primary_rig_scanned_records"] = scanned
                                info["primary_rig_search_exhaustive"] = exhaustive
                                if primary_asset is not None and primary_raw is not None:
                                    info["primary_rig_resolved_asset"] = (
                                        primary_asset.internal_path or primary_asset.relative_path
                                    )
                                    info["_primary_rig_resolved_record"] = primary_asset
                                    info["_primary_rig_resolved_raw"] = primary_raw
                            except MeshFormatError as error:
                                info["primary_rig_resolution_error"] = str(error)
                self.after(0, self._show_frostbite_animation_record, asset, title, info)
                return
            if asset.metadata.get("reader") == "frostbite-skeleton-record":
                skeleton, _bind_rotations, _primary_rig_order = self._decode_frostbite_skeleton_asset(asset)
                self.after(0, self.viewer.set_skeleton, skeleton, title)
                self.after(
                    0, self.status_var.set,
                    f"Showing decoded Frostbite bind skeleton {title} · "
                    f"{len(skeleton.joints):,} bones.",
                )
                return
            if asset.metadata.get("reader") == "frostbite-meshset-cas":
                if not self.result:
                    raise ValueError("Scan data is no longer available.")
                from .frostbite import preview_frostbite_meshset_isolated
                preview = preview_frostbite_meshset_isolated(
                    self.result.engine_root or self.result.root, asset,
                )
                if preview[0] == "unavailable":
                    reason = str(preview[1])
                    self.after(0, self._mark_preview_unavailable, asset, reason)
                    return
                _kind, meshes, lod_index, decoded_name, available_lods, rig_diagnostic = preview
                skeleton = None
                bind_rotations = None
                primary_rig_order = None
                skeleton_error = None
                try:
                    self.after(0, self.status_var.set, "Resolving the matching Javelin bind skeleton…")
                    skeleton, bind_rotations, primary_rig_order = self._decode_matching_frostbite_skeleton(asset)
                except (MeshFormatError, OSError, RuntimeError, ValueError) as error:
                    skeleton_error = str(error)
                self.after(
                    0, self._show_frostbite_preview, asset, meshes, lod_index,
                    decoded_name, available_lods, rig_diagnostic, skeleton, skeleton_error, bind_rotations,
                    primary_rig_order,
                )
                return
            if asset.extension == ".meshset" and not asset.internal_path:
                from .frostbite_mesh import load_anthem_meshset
                meshes, lod_index, chunk_path = load_anthem_meshset(asset.path)
                self.after(0, self.viewer.set_meshes, meshes, f"{title} · LOD{lod_index}")
                self.after(
                    0, self.status_var.set,
                    f"Previewing {title} from {chunk_path.name} inside Game Asset Explorer.",
                )
                return
            if asset.kind == "skeleton":
                if asset.internal_path:
                    from .rage import read_internal_asset
                    raw = read_internal_asset(asset)
                else:
                    raw = asset.path.read_bytes()
                skeleton = load_skeleton_bytes(raw, asset.relative_path)
                self.after(0, self.viewer.set_skeleton, skeleton, title)
                self.after(0, self.status_var.set, f"Showing {title} as a joint graph inside Game Asset Explorer.")
                return
            if asset.kind == "texture" and asset.extension in IMAGE_EXTENSIONS:
                from .adapters import ensure_adapter
                ensure_adapter(asset, "image-read", progress=lambda text: self.after(0, self.status_var.set, text))
                from PIL import Image
                raw = None
                if asset.internal_path:
                    from .rage import read_internal_asset
                    raw = read_internal_asset(asset)
                else:
                    raw = asset.path.read_bytes()
                with Image.open(io.BytesIO(raw)) as decoded:
                    image = decoded.copy()
                self.after(0, self.viewer.set_image, image, title)
                self.after(0, self.status_var.set, f"Showing {title} as a 2D image inside Game Asset Explorer.")
                return
            if asset.kind == "texture" and asset.extension == ".wtd":
                if asset.internal_path:
                    from .rage import read_internal_asset
                    raw = read_internal_asset(asset)
                else:
                    raw = asset.path.read_bytes()
                image, texture_count = load_texture_bytes(raw, asset.relative_path)
                self.after(0, self.viewer.set_image, image, f"{title} · texture 1/{texture_count}")
                self.after(0, self.status_var.set, f"Showing {title} as a decoded GTA IV texture inside Game Asset Explorer.")
                return
            if asset.kind == "container" and not asset.internal_path:
                if asset.engine == "Unity":
                    if not self.result:
                        raise ValueError("Scan data is no longer available.")
                    from .adapters import ensure_adapter
                    ensure_adapter(asset, "unity-read", progress=lambda text: self.after(0, self.status_var.set, text))
                    from .unity import choose_unity_preview_asset, inspect_unity_container_isolated, preview_unity_asset_isolated
                    found, notices = inspect_unity_container_isolated(self.result.root, asset)
                    candidate = choose_unity_preview_asset(found, category)
                    self.after(0, self._merge_preview_discovery, found, notices)
                    if candidate is None:
                        kinds = sorted({item.kind for item in found})
                        detail = ", ".join(kinds) if kinds else "no supported Mesh, Texture2D, Sprite, AnimationClip, or rig objects"
                        raise MeshFormatError(
                            f"Unity opened {asset.relative_path}, but found {detail}. "
                            "This container may only hold localization, metadata, scripts, or other non-visual data."
                        )
                    viewer_kind, payload = preview_unity_asset_isolated(candidate)
                    title = candidate.internal_path or candidate.relative_path
                    if viewer_kind == "mesh":
                        self.after(0, self.viewer.set_meshes, payload, title)
                    elif viewer_kind == "image":
                        self.after(0, self.viewer.set_image, payload, title)
                    elif viewer_kind == "skeleton":
                        self.after(0, self.viewer.set_skeleton, payload, title)
                    else:
                        raise MeshFormatError(f"Unity preview returned an unknown type: {viewer_kind}")
                    self.after(0, self.status_var.set, f"Opened {asset.path.name} and previewing contained asset {title}.")
                    return
                if asset.engine == "Frostbite":
                    if not self.result:
                        raise ValueError("Scan data is no longer available.")
                    from .frostbite import frostbite_container_summary
                    summary = frostbite_container_summary(asset, self.result.assets)
                    self.after(0, self.viewer.clear, summary)
                    self.after(0, self.status_var.set, f"Showing Frostbite container information for {asset.relative_path}.")
                    return
                if asset.engine != "Rockstar RAGE (GTA IV)":
                    raise MeshFormatError(
                        f"{asset.engine or asset.extension} was detected, but no container preview is registered."
                    )
                cached = self._archive_previews.get(asset.path)
                if cached:
                    matched_bundle, meshes = cached
                else:
                    matched_bundle, meshes = self._probe_archive(asset)
                title = f"{asset.path.name} · {matched_bundle.name}"
                bundle = matched_bundle
            if meshes is None and asset.internal_path:
                if not self.result:
                    raise ValueError("Scan data is no longer available.")
                from .rage import read_internal_asset
                meshes = load_mesh_bytes(read_internal_asset(asset), asset.extension, asset.internal_path)
            elif meshes is None:
                meshes = load_mesh(asset.path)
            self.after(0, self.viewer.set_meshes, meshes, title)
            self.after(0, self.status_var.set, f"Previewing {title} inside Game Asset Explorer.")
        except MeshFormatError as error:
            self.after(0, self.viewer.clear, "Asset is not previewable")
            self.after(0, messagebox.showinfo, "No asset preview", str(error))
        except Exception as error:
            self.after(0, self.viewer.clear, "Preview unavailable")
            self.after(0, messagebox.showerror, "Cannot preview", str(error))
        finally:
            self.after(0, self.preview_button.configure, {"state": "normal"})

    def _show_frostbite_animation_record(self, asset, title: str, info: dict[str, object]) -> None:
        if "decoded_clip_count" in info:
            asset.metadata["decoded_clip_count"] = int(info["decoded_clip_count"])
            options = info.get("_animation_group_options") or []
            asset.metadata["clip_names"] = tuple(
                option["clip"].name for option in options
                if isinstance(option, dict) and option.get("clip") is not None
            )
            self._update_animation_count_cell(asset)
        self._animation_group_source = (asset, title, info)
        self._animation_playback = None
        active_family = (self._active_character or {}).get("family")
        asset_family = self._infer_character_family(asset.internal_path or asset.relative_path)
        cache_family = asset_family or active_family
        resolved_rigamate = info.get("_rigamate_resolved_record")
        resolved_rigamate_raw = info.get("_rigamate_resolved_raw")
        if resolved_rigamate is not None and isinstance(resolved_rigamate_raw, bytes):
            self._resolved_rigamate_asset = resolved_rigamate
            self._resolved_rigamate_raw = resolved_rigamate_raw
            self.rigamate_extract_button.configure(state="normal")
            if isinstance(cache_family, str):
                self._rigamate_bank_cache[cache_family] = (resolved_rigamate, resolved_rigamate_raw)
        else:
            self._resolved_rigamate_asset = None
            self._resolved_rigamate_raw = None
            self.rigamate_extract_button.configure(state="disabled")
        compatibility, compatible = self._animation_compatibility(asset)
        options = info.get("_animation_group_options") or []
        selected_index = int(info.get("_animation_group_default_index", 0))
        selected_clip = options[selected_index]["clip"] if 0 <= selected_index < len(options) else None
        if selected_clip is not None:
            for transient_key in (
                "_playback_mapping", "_playback_channels", "_playback_vector_mapping",
                "_playback_vector_channels", "_playback_skipped", "_playback_mapping_kind",
                "_playback_default_rotations",
                "_playback_transition_base_rotations",
                "_helper_constants_held", "_external_channel_map", "_invalid_constants_held",
                "_trajectory_translations_rebased", "_position_helpers_held",
            ):
                info.pop(transient_key, None)
        candidate_bank_raw = resolved_rigamate_raw
        if not isinstance(candidate_bank_raw, bytes) and isinstance(active_family, str):
            cached_bank = self._rigamate_bank_cache.get(active_family)
            if cached_bank is not None:
                candidate_bank_raw = cached_bank[1]
                info["_cached_rigamate_bank"] = True
        mapping_bank_raw = info.get("_primary_rig_resolved_raw")
        primary_rig_key = info.get("_primary_rig_key")
        if (isinstance(cache_family, str) and isinstance(mapping_bank_raw, bytes)
                and isinstance(primary_rig_key, bytes)):
            primary_asset = info.get("_primary_rig_resolved_record")
            self._primary_rig_bank_cache[cache_family] = (
                primary_asset, mapping_bank_raw, primary_rig_key,
            )
        elif isinstance(active_family, str):
            cached_primary = self._primary_rig_bank_cache.get(active_family)
            if cached_primary is not None:
                _primary_asset, mapping_bank_raw, primary_rig_key = cached_primary
                info["_cached_primary_rig_bank"] = True
        if not isinstance(mapping_bank_raw, bytes):
            mapping_bank_raw = candidate_bank_raw
        if (selected_clip is not None and not selected_clip.mapped
                and selected_clip.channel_to_dof_key
                and isinstance(candidate_bank_raw, bytes)):
            from .frostbite_animation import resolve_clip_channel_map
            selected_clip = resolve_clip_channel_map(selected_clip, candidate_bank_raw)
            if selected_clip.mapped:
                info["_external_channel_map"] = True
        resolved_mapping = None
        mapping_kind = ""
        if (compatible is not False and selected_clip is not None
                and isinstance(mapping_bank_raw, bytes) and self._active_character is not None
                and self._active_character.get("family") in {"exm", "exf", "exh", "exl"}):
            from dataclasses import replace
            from .frostbite_animation import channel_map_candidates
            from .frostbite_rigamate import decode_rigamate_bone_mapping
            if selected_clip.mapped:
                resolved_mapping = decode_rigamate_bone_mapping(
                    mapping_bank_raw, selected_clip, self._active_character["skeleton"],
                    expected_rig_key=primary_rig_key,
                )
                mapping_kind = "exact"
            elif isinstance(candidate_bank_raw, bytes):
                inferred = []
                for dof_ids in channel_map_candidates(candidate_bank_raw, selected_clip.channel_count):
                    candidate_clip = replace(selected_clip, dof_ids=dof_ids)
                    candidate_mapping = decode_rigamate_bone_mapping(
                        mapping_bank_raw, candidate_clip,
                        self._active_character["skeleton"], include_constants=True,
                        expected_rig_key=primary_rig_key,
                    )
                    if candidate_mapping is not None:
                        score = (
                            len(candidate_mapping.bone_indices)
                            + len(candidate_mapping.vector_bone_indices),
                            -len(candidate_mapping.skipped_names),
                        )
                        inferred.append((score, candidate_clip, candidate_mapping))
                if inferred:
                    inferred.sort(key=lambda item: item[0], reverse=True)
                    _score, selected_clip, resolved_mapping = inferred[0]
                    mapping_kind = "inferred bank"
            if resolved_mapping is not None:
                pose_mapping = []
                pose_channels = []
                pose_defaults = []
                helper_constants_held = 0
                invalid_constants_held = 0
                for channel_index, bone_index, channel_name, default_rotation in zip(
                    resolved_mapping.channel_indices,
                    resolved_mapping.bone_indices,
                    resolved_mapping.channel_names,
                    resolved_mapping.default_rotations,
                ):
                    channel = selected_clip.quaternion_channels[channel_index]
                    if not channel.decode_valid:
                        invalid_constants_held += 1
                        continue
                    is_bad_rear_toe = (
                        "toerear" in channel_name.casefold()
                        and len(channel.values) == 1
                        and abs(channel.values[0][3]) < 0.0001
                        and abs(channel.values[0][0]) > 0.999
                    )
                    if is_bad_rear_toe:
                        helper_constants_held += 1
                        continue
                    pose_mapping.append(bone_index)
                    pose_channels.append(channel)
                    pose_defaults.append(default_rotation)
                info["_playback_mapping"] = pose_mapping
                info["_playback_channels"] = pose_channels
                info["_playback_default_rotations"] = pose_defaults
                if self._prototype_running:
                    from .interceptor_prototype import transition_base_clip_name
                    base_name = transition_base_clip_name(selected_clip.name)
                    base_clip = next((
                        option["clip"] for option in options
                        if (base_name is not None
                            and option["clip"].name.casefold() == base_name.casefold())
                    ), None)
                    if base_clip is not None and base_clip.mapped:
                        base_mapping = decode_rigamate_bone_mapping(
                            mapping_bank_raw, base_clip,
                            self._active_character["skeleton"], include_constants=True,
                            expected_rig_key=primary_rig_key,
                        )
                        if base_mapping is not None:
                            base_by_bone = {
                                bone_index: base_clip.quaternion_channels[channel_index].values[-1]
                                for channel_index, bone_index in zip(
                                    base_mapping.channel_indices, base_mapping.bone_indices,
                                )
                                if (base_clip.quaternion_channels[channel_index].decode_valid
                                    and base_clip.quaternion_channels[channel_index].values)
                            }
                            info["_playback_transition_base_rotations"] = [
                                base_by_bone.get(bone_index) for bone_index in pose_mapping
                            ]
                from .frostbite_animation_playback import prepare_preview_translations
                (
                    preview_vector_mapping, preview_vector_channels,
                    trajectory_rebased, position_helpers_held,
                ) = prepare_preview_translations(
                    list(resolved_mapping.vector_bone_indices),
                    resolved_mapping.pose_vector_channels(selected_clip),
                    list(resolved_mapping.vector_channel_names),
                )
                info["_playback_vector_mapping"] = preview_vector_mapping
                info["_playback_vector_channels"] = preview_vector_channels
                info["_trajectory_translations_rebased"] = trajectory_rebased
                info["_position_helpers_held"] = position_helpers_held
                info["_playback_skipped"] = len(resolved_mapping.skipped_names)
                info["_playback_mapping_kind"] = mapping_kind
                info["_helper_constants_held"] = helper_constants_held
                info["_invalid_constants_held"] = invalid_constants_held
        else:
            info.pop("_playback_mapping", None)
            info.pop("_playback_channels", None)
            info.pop("_playback_missing_constants", None)
        # Mapless clips remain testable through the decoded primary-rig order.
        # This is a visible experimental fallback, never reported as exact.
        if (compatible is not False and selected_clip is not None
                and self._active_character is not None
                and not info.get("_playback_mapping")
                and self._active_character.get("family") == "exm"
                and selected_clip.quaternion_channels):
            from .frostbite_animation_playback import guess_bone_mapping
            guessed = guess_bone_mapping(
                self._active_character["skeleton"],
                len(selected_clip.quaternion_channels),
                self._active_character.get("primary_rig_order"),
            )
            count = min(len(guessed), len(selected_clip.quaternion_channels))
            if count:
                valid_pairs = [
                    (bone_index, channel)
                    for bone_index, channel in zip(
                        guessed[:count], selected_clip.quaternion_channels[:count],
                    )
                    if channel.decode_valid
                ]
                info["_playback_mapping"] = [pair[0] for pair in valid_pairs]
                info["_playback_channels"] = [pair[1] for pair in valid_pairs]
                info["_playback_vector_mapping"] = []
                info["_playback_vector_channels"] = []
                invalid_constants_held = count - len(valid_pairs)
                info["_playback_skipped"] = (
                    len(selected_clip.quaternion_channels) - count + invalid_constants_held
                )
                info["_invalid_constants_held"] = invalid_constants_held
                info["_playback_mapping_kind"] = "experimental primary-rig order"
        if self._active_character is not None:
            if not self._prototype_running:
                self._show_active_character()
            # During prototype state changes preserve the already displayed
            # mesh. Reloading it for every selected clip wastes work, while
            # clearing it here makes a valid active character disappear.
        else:
            self.viewer.clear(
                "Animation record loaded\n\n"
                "Preview a rigged Javelin mesh and enable Use for animation first."
            )
        display_name = (selected_clip.name if selected_clip is not None
                        else PurePosixPath(asset.internal_path or title).name)
        self.animation_clip_var.set(f"Animation: {display_name} · {compatibility}")
        kind = str(info.get("record_kind", "record")).upper()
        if "inspection_error" in info:
            structure = f"{kind} payload loaded; {info['inspection_error']}"
        elif kind == "EBX":
            structure = (
                f"EBX loaded: {int(info.get('imports', 0))} imports, "
                f"{int(info.get('arrays', 0))} arrays"
            )
        elif "decoded_clip_count" in info:
            structure = (
                f"{kind} payload loaded: {int(info['decoded_clip_count'])} clip(s), "
                f"{int(info.get('animation_channel_count', 0))} curves and "
                f"{int(info.get('quaternion_channel_count', 0))} rotation channels decoded"
            )
        else:
            structure = f"{kind} payload loaded ({int(info.get('bytes', 0)):,} bytes)"

        has_decoded_channels = "decoded_clip_count" in info
        has_exact_bone_mapping = bool(info.get("_playback_mapping"))
        playback_ready = (
            has_decoded_channels and has_exact_bone_mapping
            and self._active_character is not None and compatible is not False
        )
        if compatible is False:
            state = "Different Javelin family; playback remains disabled."
        elif self._active_character is None:
            state = "No active character; playback remains disabled."
        elif not has_decoded_channels:
            if "quaternion_channel_count" in info:
                state = "No active character bones matched this clip's channels; playback remains disabled."
            else:
                state = "This isn't a decodable AntState animation payload; playback remains disabled."
        elif not has_exact_bone_mapping:
            rigamate_key = str(info.get("rigamate_key", ""))
            resolved_bank = info.get("rigamate_resolved_asset")
            scanned = int(info.get("rigamate_scanned_records", 0))
            if resolved_bank:
                verified = info.get("rigamate_dof_types")
                state = (
                    f"Rigamate key {rigamate_key} resolved to {resolved_bank} after scanning "
                    f"{scanned:,} RES record(s). "
                    + (f"EXM bank: {verified}. " if verified else "The bank payload is loaded. ")
                    + "Channel types are confirmed, but the IDs still need a verified bone mapping before Play can be enabled."
                )
            elif info.get("rigamate_resolution_error"):
                state = (
                    f"Rigamate key {rigamate_key} was decoded, but its installation search failed safely: "
                    f"{info['rigamate_resolution_error']}"
                )
            elif rigamate_key:
                scope = "complete installation" if info.get("rigamate_search_exhaustive") else "bounded search"
                state = (
                    f"Rigamate key {rigamate_key} was decoded, but no defining RES was found in the "
                    f"{scope} ({scanned:,} candidate record(s)). Playback remains disabled."
                )
            elif selected_clip is not None and not selected_clip.mapped:
                state = (
                    "Clip curves decoded, but this RES has no ChannelToDof IDs or Rigamate pointer. "
                    "Bone mapping is needed before playback."
                )
            else:
                state = (
                    "Clip curves and ChannelToDof IDs decoded correctly. Playback is intentionally disabled until "
                    "the referenced Rigamate animation bank supplies the exact DOF-to-bone mapping."
                )
        else:
            self._active_animation = {
                "channels": info["_playback_channels"],
                "mapping": info["_playback_mapping"],
                "name": display_name,
                "health": info.get("_channel_health"),
                "vector_mapping": info.get("_playback_vector_mapping", []),
                "vector_channels": info.get("_playback_vector_channels", []),
                "timeline_end": selected_clip.frame_count if selected_clip is not None else None,
                "fps": selected_clip.fps if selected_clip is not None else 30.0,
                "time_scale": selected_clip.time_scale if selected_clip is not None else 1.0,
                "mapping_kind": info.get("_playback_mapping_kind", "exact"),
                "default_rotations": info.get("_playback_default_rotations", []),
                "transition_base_rotations": info.get(
                    "_playback_transition_base_rotations", [],
                ),
            }
            mapping_label = str(info.get("_playback_mapping_kind") or "exact")
            if mapping_label != "exact":
                state = (
                    f"{len(info['_playback_channels'])} rotations ready with {mapping_label} mapping; "
                    "the clip omitted its authored bone table, so verify the pose visually."
                )
            else:
                state = (
                    f"{len(info['_playback_channels'])} bank-mapped bone rotations ready; "
                    f"{info.get('_playback_skipped', 0)} helper channels absent from the mesh skeleton skipped. "
                    "Moving and constant rotations are decoded. "
                    + ("Channel map resolved from the external bank. "
                       if info.get("_external_channel_map") else "")
                    + f"{len(info.get('_playback_vector_channels', []))} named translations applied."
                )
                if info.get("_helper_constants_held"):
                    state += (
                        f" {int(info['_helper_constants_held'])} degenerate rear-toe helper "
                        "constant(s) held at bind pose."
                    )
                if info.get("_trajectory_translations_rebased"):
                    state += (
                        f" {int(info['_trajectory_translations_rebased'])} scene-space trajectory "
                        "translation(s) recentered at the clip's first frame."
                    )
                if info.get("_position_helpers_held"):
                    state += (
                        f" {int(info['_position_helpers_held'])} controller-plane position "
                        "helper(s) hidden from the preview skeleton."
                    )
        if info.get("_invalid_constants_held"):
            state += (
                f" {int(info['_invalid_constants_held'])} unverified packed constant(s) "
                "held at bind pose."
            )
        if not playback_ready:
            self._active_animation = None
        self.animation_state_var.set(state)
        self.animation_play_button.configure(state="normal" if playback_ready else "disabled")
        self.animation_stop_button.configure(state="disabled")
        self.animation_dump_button.configure(state="normal" if playback_ready else "disabled")
        self.animation_health_button.configure(state="normal" if playback_ready and info.get("_channel_health") else "disabled")
        self.animation_video_button.configure(
            state="normal" if playback_ready and not self._video_export_running else "disabled",
        )
        self.animation_loop_checkbox.configure(state="normal" if playback_ready else "disabled")
        self.animation_filter_checkbox.configure(state="normal" if playback_ready and info.get("_channel_health") else "disabled")
        group_options = info.get("_animation_group_options") or []
        if group_options:
            self._animation_group_options = group_options
            self.animation_group_combo.configure(state="readonly", values=[g["label"] for g in group_options])
            default_index = int(info.get("_animation_group_default_index", 0))
            self.animation_group_combo.current(default_index)
            self.animation_clip_position_var.set(f"{default_index + 1} / {len(group_options)}")
            self.animation_previous_button.configure(state="normal" if default_index > 0 else "disabled")
            self.animation_next_button.configure(
                state="normal" if default_index + 1 < len(group_options) else "disabled",
            )
            self._update_clip_search_matches()
        else:
            self._animation_group_options = []
            self.animation_group_combo.configure(state="disabled", values=[])
            self.animation_group_var.set("")
            self.animation_clip_position_var.set("0 / 0")
            self.animation_previous_button.configure(state="disabled")
            self.animation_next_button.configure(state="disabled")
            self._update_clip_search_matches()
        self._update_animation_bar()
        self._update_prototype_button()
        self.status_var.set(f"{structure}. {state}")

    def _extract_resolved_rigamate_bank(self) -> None:
        asset = self._resolved_rigamate_asset
        raw = self._resolved_rigamate_raw
        if asset is None or raw is None:
            messagebox.showinfo(
                "Rigamate bank unavailable",
                "Preview an AntState animation and let its Rigamate dependency resolve first.",
            )
            return
        destination = filedialog.askdirectory(title="Choose a folder for the resolved Rigamate bank")
        if not destination:
            return
        try:
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True)
            name = PurePosixPath(
                asset.internal_path or "resolved_rigamate_bank.res"
            ).name
            output = folder / name
            output.write_bytes(raw)
            descriptor = output.with_name(output.name + ".json")
            descriptor.write_text(
                json.dumps(asset.to_dict(), indent=2), encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as error:
            messagebox.showerror("Cannot extract Rigamate bank", str(error))
            return
        self.status_var.set(
            f"Extracted resolved Rigamate bank {output.name} ({len(raw):,} bytes)."
        )
        messagebox.showinfo(
            "Rigamate bank extracted",
            f"Decoded the resolved bank to:\n{output}\n\n"
            f"Also saved its archive descriptor:\n{descriptor.name}\n\n"
            "Send both files back so the DOF-to-bone table can be decoded. "
            "The original Anthem installation was not modified.",
        )

    def _show_frostbite_preview(
        self, asset, meshes, lod_index: int, decoded_name: str,
        available_lods: list[int], rig_diagnostic: dict[str, object] | str,
        skeleton=None, skeleton_error: str | None = None, bind_rotations=None,
        primary_rig_order=None,
    ) -> None:
        asset.metadata["available_lods"] = list(available_lods)
        asset.metadata["lod0_available"] = 0 in available_lods
        if isinstance(rig_diagnostic, str):
            report: dict[str, object] = {
                "summary": rig_diagnostic,
                "likely_skinned": rig_diagnostic.startswith("Likely skinned"),
                "weights_decoded": False,
                "sections": [],
            }
        else:
            report = rig_diagnostic
        summary = str(report.get("summary") or "Rigging evidence was not inspected.")
        likely_skinned = bool(report.get("likely_skinned"))
        weights_decoded = bool(report.get("weights_decoded"))
        rig_label = "skinned" if weights_decoded else "likely" if likely_skinned else "?"
        title_suffix = " · skin weights" if weights_decoded else " · likely skinned" if likely_skinned else ""
        self._rig_report = {**report, "lod": lod_index, "mesh_name": decoded_name}
        self._rig_asset = asset
        self.rig_button.configure(state="normal" if likely_skinned else "disabled")
        self.viewer.set_meshes(meshes, f"{decoded_name} · LOD{lod_index}{title_suffix}")
        if skeleton is not None:
            bone_ids = [int(value) for value in report.get("skeleton_bone_ids", [])]
            if bone_ids and max(bone_ids) >= len(skeleton.joints):
                skeleton_error = (
                    f"Mesh palette references bone {max(bone_ids)}, but the matching skeleton "
                    f"contains only {len(skeleton.joints)} bones."
                )
            else:
                self.viewer.set_mesh_skeleton(skeleton)
                self._rig_report.update({
                    "skeleton_decoded": True,
                    "skeleton_bones": len(skeleton.joints),
                    "skeleton_name": skeleton.name,
                })
                animation_ready = weights_decoded and any(
                    mesh.skin_bones is not None and mesh.skin_weights is not None
                    for mesh in meshes
                )
                if animation_ready:
                    candidate = {
                        "asset": asset,
                        "key": (str(asset.path), asset.internal_path),
                        "name": decoded_name,
                        "family": self._javelin_family(asset),
                        "meshes": meshes,
                        "skeleton": skeleton,
                        "bind_rotations": bind_rotations or [],
                        "primary_rig_order": primary_rig_order,
                        "lod": lod_index,
                        "available_lods": list(available_lods),
                        "rig_report": self._rig_report,
                    }
                    self._preview_character = candidate
                    self.use_for_animation_check.configure(state="normal")
                    if (
                        self._active_character is not None
                        and self._active_character.get("key") == candidate["key"]
                    ):
                        self._active_character = candidate
                        self.use_for_animation_var.set(True)
                    elif self._active_character is not None:
                        # Keep the previous active character until the user checks
                        # this new preview, but make the replacement action clear.
                        self.use_for_animation_var.set(False)
        selected = self.tree.selection()
        if selected:
            values = list(self.tree.item(selected[0], "values"))
            if len(values) >= 3:
                values[2] = rig_label
                self.tree.item(selected[0], values=values)
        self.viewer.set_detail_levels(
            available_lods, lod_index,
            lambda requested: self._request_frostbite_lod(asset, requested),
            declared_lods=[int(value) for value in asset.metadata.get("declared_lods", [])],
            related_assets=self._related_higher_detail_meshsets(asset, available_lods),
        )
        self.viewer.set_detail_loading(False)
        if len(available_lods) > 1:
            highest = available_lods[-1]
            detail_message = (
                "Move Detail toward the right for the real higher-detail LODs; "
                f"the highest geometry resolved for this asset is LOD{highest}."
            )
        else:
            detail_message = f"Only the LOD{lod_index} geometry chunk is available in this bundle."
        declared_lods = [int(value) for value in asset.metadata.get("declared_lods", [])]
        if 0 in declared_lods and 0 not in available_lods:
            present = ", ".join(f"LOD{value}" for value in sorted(available_lods))
            detail_message += (
                f" The MeshSet declares LOD0, but its chunk payload is absent from this "
                f"installation (present: {present or 'none'})."
            )
        skeleton_message = (
            f" Bind skeleton decoded: {len(skeleton.joints):,} bones; use Bones to toggle it."
            if skeleton is not None and skeleton_error is None
            else f" Bone overlay unavailable: {skeleton_error}" if skeleton_error else ""
        )
        parts, common_lods, assembly_stem = self._multipart_meshset_group(asset)
        if len(parts) >= 2 and common_lods:
            self._assembly_source = (asset, common_lods)
            self.assemble_button.configure(state="normal")
            detail_message += (
                f" Assemble cosmetics can combine the separate {assembly_stem} variant's "
                f"{len(parts)} weighted body pieces at "
                f"{', '.join(f'LOD{value}' for value in sorted(common_lods))}."
            )
        elif not asset.metadata.get("assembled_parts"):
            self._assembly_source = None
            self.assemble_button.configure(state="disabled")
        self.status_var.set(
            f"Previewing decoded Frostbite MeshSet {decoded_name} · LOD{lod_index}. "
            + detail_message + " " + summary + skeleton_message
        )
        if self._prototype_running:
            self.viewer.bones_visible.set(False)
        self._update_prototype_button()

    def _related_higher_detail_meshsets(self, asset, available_lods: list[int]) -> list[str]:
        """List installed sibling pieces that beat this MeshSet's best LOD.

        Anthem's playable Javelins are assembled from arms/legs/torso/helmet
        bitpacks. A monolithic preview MeshSet can therefore stop at LOD3 even
        though higher-detail family pieces are installed elsewhere.
        """
        if self.result is None or not available_lods:
            return []
        internal = (asset.internal_path or "").replace("\\", "/").casefold()
        match = re.search(r"(?:^|/)(exo/ex[a-z]_[^/]+)/", internal)
        if match is None:
            return []
        family_root = match.group(1) + "/"
        current_best = min(available_lods)
        related: list[tuple[int, int, str]] = []
        for candidate in self.result.assets:
            candidate_internal = (candidate.internal_path or "").replace("\\", "/")
            folded = candidate_internal.casefold()
            if (candidate is asset or candidate.extension.casefold() != ".meshset"
                    or family_root not in folded):
                continue
            candidate_lods = [int(value) for value in candidate.metadata.get("available_lods", [])]
            if not candidate_lods or min(candidate_lods) >= current_best:
                continue
            best = min(candidate_lods)
            base_priority = 0 if "/exm_lancer_base_" in ("/" + folded) else 1
            related.append((best, base_priority, f"LOD{best}: {candidate_internal}"))
        # Duplicate Data/Patch records have the same useful display name.
        return list(dict.fromkeys(item[2] for item in sorted(related)))[:12]

    @staticmethod
    def _assembly_identity(asset) -> tuple[str, str, str | None] | None:
        """Return (folder, variant stem, body part) for modular MeshSets."""
        internal = (asset.internal_path or "").replace("\\", "/").casefold()
        if not re.search(r"(?:^|/)exo/ex[a-z]_[^/]+/", internal):
            return None
        path = PurePosixPath(internal)
        name = path.name.removesuffix(".meshset")
        match = re.fullmatch(
            r"(?P<stem>.+?)(?:_(?P<part>arms|legs|torso|helm|helmv2|helmet|head))?_model_mesh",
            name,
        )
        if match is None:
            return None
        return str(path.parent), match.group("stem"), match.group("part")

    def _multipart_meshset_group(self, asset):
        """Find the best complete modular set in the selected Javelin folder.

        Prefer the exact selected variant. A monolithic ``base`` MeshSet may
        have no equally named pieces (notably Interceptor), so then choose the
        locally installed complete cosmetic variant with the highest shared
        detail. This reflects Anthem's runtime character construction: the
        playable render model is assembled from customization slots.
        """
        identity = App._assembly_identity(asset)
        if identity is None or self.result is None:
            return [], [], ""
        folder, selected_stem, _selected_part = identity
        grouped = {}
        for candidate in self.result.assets:
            if candidate.extension.casefold() != ".meshset":
                continue
            candidate_identity = App._assembly_identity(candidate)
            if candidate_identity is None:
                continue
            candidate_folder, candidate_stem, part = candidate_identity
            lods = {int(value) for value in candidate.metadata.get("available_lods", [])}
            if candidate_folder != folder or part is None or not lods:
                continue
            # Prefer patched records when Data and Patch expose the same path.
            priority = 0 if candidate.relative_path.casefold().startswith("patch") else 1
            by_part = grouped.setdefault(candidate_stem, {})
            existing = by_part.get(part)
            if existing is None or priority < existing[0]:
                by_part[part] = (priority, candidate, lods)
        choices = []
        for candidate_stem, by_part in grouped.items():
            if not {"legs", "torso", "arms"} <= by_part.keys():
                continue
            # Helmet variants are alternatives, not simultaneous layers.
            names = ["legs", "torso", "arms"]
            head = next((name for name in ("head", "helmet", "helm", "helmv2") if name in by_part), None)
            if head is not None:
                names.append(head)
            ordered_parts = [by_part[name] for name in names]
            common_lods = set.intersection(*(entry[2] for entry in ordered_parts))
            if not common_lods:
                continue
            exact_priority = 0 if candidate_stem == selected_stem else 1
            best_lod = min(common_lods)
            choices.append((
                exact_priority, best_lod, -len(ordered_parts), candidate_stem,
                [entry[1] for entry in ordered_parts], sorted(common_lods, reverse=True),
            ))
        if not choices:
            return [], [], selected_stem
        _exact, _best, _count, stem, parts, common_lods = min(choices, key=lambda item: item[:4])
        return parts, common_lods, stem

    def _assemble_high_detail(self) -> None:
        source = self._assembly_source
        if source is None:
            return
        asset, common_lods = source
        if not common_lods:
            messagebox.showinfo(
                "No common detail level",
                "The matching body pieces do not share a locally stored LOD.",
            )
            return
        self._request_frostbite_assembly(asset, min(common_lods))

    def _request_frostbite_assembly(self, asset, lod_index: int) -> None:
        if not self.result:
            return
        parts, common_lods, stem = self._multipart_meshset_group(asset)
        if len(parts) < 2 or lod_index not in common_lods:
            self._finish_frostbite_lod_error("No complete modular set exists at that LOD.")
            return
        self.preview_button.configure(state="disabled")
        self.assemble_button.configure(state="disabled")
        self.viewer.set_detail_loading(True)
        self.status_var.set(
            f"Assembling {len(parts)} matching body pieces at LOD{lod_index}…"
        )
        threading.Thread(
            target=self._frostbite_assembly_worker,
            args=(self.result.engine_root or self.result.root, asset, parts, common_lods, stem, lod_index),
            daemon=True,
        ).start()

    def _frostbite_assembly_worker(
        self, root: Path, source_asset, parts, common_lods: list[int], stem: str, lod_index: int,
    ) -> None:
        try:
            from dataclasses import replace
            from .frostbite import preview_frostbite_meshset_isolated
            meshes = []
            reports = []
            for part in parts:
                preview = preview_frostbite_meshset_isolated(root, part, lod_index=lod_index)
                if preview[0] == "unavailable":
                    raise MeshFormatError(str(preview[1]))
                _kind, part_meshes, _lod, _name, _available, report = preview
                meshes.extend(part_meshes)
                reports.append(report)
            skeleton, bind_rotations, primary_rig_order = self._decode_matching_frostbite_skeleton(source_asset)
            sections = [section for report in reports for section in report.get("sections", [])]
            bone_ids = sorted({
                int(value) for report in reports
                for value in report.get("skeleton_bone_ids", [])
            })
            weighted = sum(int(report.get("weighted_vertices", 0)) for report in reports)
            total = sum(int(report.get("total_skin_vertices", 0)) for report in reports)
            decoded_sections = sum(int(report.get("decoded_skin_sections", 0)) for report in reports)
            candidate_sections = sum(int(report.get("candidate_skin_sections", 0)) for report in reports)
            combined_report = {
                "summary": (
                    f"Assembled {len(parts)} modular pieces; {weighted:,}/{total:,} vertices weighted "
                    f"across {decoded_sections}/{candidate_sections} skin sections."
                ),
                "likely_skinned": candidate_sections > 0,
                "weights_decoded": decoded_sections > 0 and weighted > 0,
                "decoded_skin_sections": decoded_sections,
                "candidate_skin_sections": candidate_sections,
                "weighted_vertices": weighted,
                "total_skin_vertices": total,
                "skeleton_bone_ids": bone_ids,
                "sections": sections,
            }
            synthetic_path = f"{PurePosixPath(source_asset.internal_path or '').parent}/{stem}_assembled_model_mesh.meshset"
            assembled_asset = replace(
                source_asset, internal_path=synthetic_path,
                metadata={
                    **source_asset.metadata, "available_lods": common_lods,
                    "declared_lods": common_lods, "assembled_parts": len(parts),
                },
            )
            self.after(
                0, self._show_frostbite_assembly, source_asset, assembled_asset, meshes,
                lod_index, common_lods, combined_report, skeleton, bind_rotations,
                primary_rig_order, len(parts), stem,
            )
        except Exception as error:
            self.after(0, self._finish_frostbite_lod_error, str(error))
        finally:
            self.after(0, self.preview_button.configure, {"state": "normal"})

    def _show_frostbite_assembly(
        self, source_asset, assembled_asset, meshes, lod_index: int, common_lods: list[int],
        report, skeleton, bind_rotations, primary_rig_order, part_count: int, stem: str,
    ) -> None:
        self._show_frostbite_preview(
            assembled_asset, meshes, lod_index, f"{stem} assembled ({part_count} pieces)",
            common_lods, report, skeleton, None, bind_rotations, primary_rig_order,
        )
        self._assembly_source = (source_asset, common_lods)
        self.assemble_button.configure(state="normal")
        self.viewer.set_detail_levels(
            common_lods, lod_index,
            lambda requested: self._request_frostbite_assembly(source_asset, requested),
            declared_lods=common_lods,
        )
        self.status_var.set(
            f"Assembled {part_count} matching {stem} pieces at LOD{lod_index}. "
            "The Detail slider now switches the complete modular set."
        )

    def _infer_character_family(self, internal_path: str) -> str | None:
        """Return the short lowercase prefix (e.g. 'exm', 'ara', 'sox') that
        names this asset's character/creature family, if any.

        Originally only matched Javelin codes (ex[a-z]). Generalized after
        confirming, across real scanned data, that the SAME
        "{family}/{family}_master_skeleton.ebx" (or "{family}_skeleton.ebx")
        convention is used for dozens of non-Javelin families too (ara, dml,
        dsr, glm, sox, wrap, cla, and many more) -- it's a general Anthem
        convention, not something specific to Javelins.

        Only searches directory segments, not the filename itself, for the
        same reason as _javelin_family: a filename's own leading word could
        otherwise be mistaken for the family.
        """
        internal = internal_path.replace("\\", "/").lower()
        directory = (internal.rsplit("/", 1)[0] if "/" in internal else "") + "/"
        match = re.search(r"(?:^|/)([a-z]{2,8})_[^/]+(?:/|$)", directory)
        return match.group(1) if match else None

    @staticmethod
    def _candidate_skeleton_filenames(family: str) -> list[str]:
        """Both observed naming conventions, most specific first."""
        return [f"{family}_master_skeleton.ebx", f"{family}_skeleton.ebx"]

    def _matching_master_skeleton_asset(self, mesh_asset):
        internal = (mesh_asset.internal_path or "").replace("\\", "/").lower()
        family = self._infer_character_family(internal)
        if not family or not self.result:
            return None
        expected_names = self._candidate_skeleton_filenames(family)
        for expected in expected_names:
            match = next((
                candidate for candidate in self.result.assets
                if candidate.metadata.get("reader") == "frostbite-skeleton-record"
                and (candidate.internal_path or "").replace("\\", "/").lower().endswith("/" + expected)
            ), None)
            if match is not None:
                return match
        return None

    def _decode_frostbite_skeleton_asset(self, skeleton_asset):
        key = (skeleton_asset.internal_path or skeleton_asset.relative_path).casefold()
        cached = self._frostbite_skeleton_cache.get(key)
        if cached is not None:
            return cached
        if not self.result:
            raise MeshFormatError("Scan data is no longer available.")
        from .frostbite import extract_frostbite_record_dependencies_isolated
        from .frostbite_ebx import (
            decode_anthem_primary_rig_joint_order, decode_anthem_skeleton,
            decode_anthem_skeleton_bind_rotations,
        )
        primary, dependencies = extract_frostbite_record_dependencies_isolated(
            self.result.engine_root or self.result.root, skeleton_asset,
        )
        attempts = [(linked.internal_path or linked.relative_path, raw) for linked, raw in dependencies]
        attempts.append((skeleton_asset.internal_path or skeleton_asset.relative_path, primary))
        errors = []
        for candidate_name, raw in attempts:
            try:
                skeleton = decode_anthem_skeleton(raw, str(candidate_name))
                try:
                    bind_rotations = decode_anthem_skeleton_bind_rotations(raw)
                except MeshFormatError:
                    bind_rotations = []  # positions decoded fine; rotations unavailable, fall back to identity
                try:
                    primary_rig_order = decode_anthem_primary_rig_joint_order(raw)
                except MeshFormatError:
                    primary_rig_order = None
                result = (skeleton, bind_rotations, primary_rig_order)
                self._frostbite_skeleton_cache[key] = result
                return result
            except MeshFormatError as error:
                errors.append(str(error))
        raise MeshFormatError("The linked EBX was found but is not a decoded SkeletonAsset: " + errors[-1])

    def _decode_matching_frostbite_skeleton(self, mesh_asset):
        candidate = self._matching_master_skeleton_asset(mesh_asset)
        if candidate is None:
            raise MeshFormatError("No matching indexed master-skeleton entry was found.")
        return self._decode_frostbite_skeleton_asset(candidate)

    def _matching_master_skeleton(self, asset) -> str:
        """Return the indexed master-skeleton path suggested by the asset's
        inferred character/creature family (see _infer_character_family)."""
        internal = (asset.internal_path or "").replace("\\", "/").lower()
        family = self._infer_character_family(internal)
        if not family:
            return "No character/creature family could be inferred from this mesh name."
        expected_names = self._candidate_skeleton_filenames(family)
        if self.result:
            for expected in expected_names:
                for candidate in self.result.assets:
                    candidate_path = (candidate.internal_path or "").replace("\\", "/").lower()
                    if (
                        candidate.metadata.get("reader") == "frostbite-skeleton-record"
                        and candidate_path.endswith("/" + expected)
                    ):
                        return f"Indexed skeleton candidate: {candidate.internal_path}"
        return (
            f"Expected skeleton candidate: animation/{family}/{expected_names[0]} "
            "(not indexed in the current result set)"
        )

    def show_rig_inspector(self) -> None:
        """Show decoded skin inputs without claiming the skeleton is bound yet."""
        report, asset = self._rig_report, self._rig_asset
        if not report or asset is None:
            messagebox.showinfo("Rig Inspector", "Preview a Frostbite mesh with skin streams first.")
            return
        dialog = tk.Toplevel(self)
        dialog.title("Rig Inspector")
        dialog.geometry("840x470")
        dialog.minsize(680, 380)
        dialog.transient(self)

        header = ttk.Frame(dialog, padding=12)
        header.pack(fill="x")
        ttk.Label(
            header,
            text=f"{report.get('mesh_name', asset.internal_path or asset.path.name)} · LOD{report.get('lod', '?')}",
            font=("TkDefaultFont", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header, text=str(report.get("summary", "")), wraplength=790, justify="left",
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(
            header, text=self._matching_master_skeleton(asset), wraplength=790, justify="left",
        ).pack(anchor="w", pady=(5, 0))

        columns = ("section", "vertices", "streams", "weighted", "palette", "bones", "weight_sums", "state")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", height=10)
        for key, label, width in (
            ("section", "Section", 180), ("vertices", "Vertices", 75),
            ("streams", "Index / weight streams", 135), ("weighted", "Weighted", 90),
            ("palette", "Slots / palette", 95), ("bones", "Skeleton IDs", 85),
            ("weight_sums", "Weight sums", 90),
            ("state", "Decode", 90),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="w" if key == "section" else "center")
        tree.pack(fill="both", expand=True, padx=12)
        for index, section in enumerate(report.get("sections", [])):
            if not isinstance(section, dict):
                continue
            slots = section.get("palette_slots") or []
            bone_ids = section.get("skeleton_bone_ids") or []
            weight_min, weight_max = section.get("weight_sum_min"), section.get("weight_sum_max")
            sums = "—" if weight_min is None else f"{weight_min}–{weight_max}"
            state = "decoded" if section.get("values_decoded") else str(section.get("error") or "declared")
            tree.insert("", "end", iid=f"rig-section-{index}", values=(
                section.get("name", f"section_{index}"),
                f"{int(section.get('vertex_count', 0)):,}",
                f"{section.get('index_stream', '?')} / {section.get('weight_stream', '?')}",
                f"{int(section.get('weighted_vertices', 0)):,}",
                f"{len(slots)} / {int(section.get('palette_size', 0))}",
                len(bone_ids), sums, state,
            ))

        if report.get("skeleton_decoded"):
            note = (
                "Mesh palette mapping and the matching bind skeleton are decoded. "
                f"The viewport Bones toggle displays its {int(report.get('skeleton_bones', 0)):,} named "
                "bones, parent hierarchy, and model-space bind pose over the mesh. Animation clips are "
                "not decoded or playable yet."
            )
        else:
            note = (
                "MeshSet palette mapping is decoded: local skin slots resolve to skeleton bone IDs. "
                "The matching bind skeleton could not be attached to this preview."
            )
        ttk.Label(dialog, text=note, wraplength=790, justify="left", padding=12).pack(fill="x")
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=(0, 12))

    def _request_frostbite_lod(self, asset, lod_index: int) -> None:
        if not self.result:
            return
        self.preview_button.configure(state="disabled")
        self.viewer.set_detail_loading(True)
        self.status_var.set(f"Decoding the real Frostbite LOD{lod_index} geometry…")
        threading.Thread(
            target=self._frostbite_lod_worker,
            args=(self.result.engine_root or self.result.root, asset, lod_index), daemon=True,
        ).start()

    def _frostbite_lod_worker(self, root: Path, asset, lod_index: int) -> None:
        try:
            from .frostbite import preview_frostbite_meshset_isolated
            preview = preview_frostbite_meshset_isolated(
                root, asset, lod_index=lod_index,
            )
            if preview[0] == "unavailable":
                self.after(0, self._finish_frostbite_lod_error, str(preview[1]))
                return
            _kind, meshes, decoded_lod, decoded_name, available_lods, rig_diagnostic = preview
            skeleton = None
            bind_rotations = None
            primary_rig_order = None
            skeleton_error = None
            try:
                skeleton, bind_rotations, primary_rig_order = self._decode_matching_frostbite_skeleton(asset)
            except (MeshFormatError, OSError, RuntimeError, ValueError) as error:
                skeleton_error = str(error)
            self.after(
                0, self._show_frostbite_preview, asset, meshes, decoded_lod,
                decoded_name, available_lods, rig_diagnostic, skeleton, skeleton_error, bind_rotations,
                primary_rig_order,
            )
        except Exception as error:
            self.after(0, self._finish_frostbite_lod_error, str(error))
        finally:
            self.after(0, self.preview_button.configure, {"state": "normal"})

    def _finish_frostbite_lod_error(self, message: str) -> None:
        self.viewer.restore_detail_selection()
        self.viewer.set_detail_loading(False)
        self.status_var.set("That stored LOD could not be decoded; the previous preview is still displayed.")
        messagebox.showinfo("LOD unavailable", message)

    def _mark_preview_unavailable(self, asset, reason: str) -> None:
        """Remember a verified non-renderable MeshSet without another popup."""
        asset.metadata["preview_unavailable_reason"] = reason
        self.viewer.clear("MeshSet has no previewable geometry\n\n" + reason)
        self.status_var.set("This MeshSet is metadata, a wrapper, or has no available geometry chunk.")
        # Update only the selected row. Rebuilding a large filtered inventory
        # here would jump back to its first result just because one wrapper was
        # inspected.
        selection = self.tree.selection()
        if selection:
            self.tree.set(selection[0], "viewable", "no geometry")

    def _merge_preview_discovery(self, discovered, warnings) -> None:
        """Merge an on-demand container inventory without disturbing selection."""
        if not self.result:
            return
        existing = {(asset.path, asset.internal_path) for asset in self.result.assets}
        self.result.assets.extend(
            asset for asset in discovered if (asset.path, asset.internal_path) not in existing
        )
        for warning in warnings:
            if warning not in self.result.warnings:
                self.result.warnings.append(warning)

    def _probe_archive(self, asset):
        """Find and cache the first resource that actually decodes as geometry."""
        if not self.result:
            raise ValueError("Scan data is no longer available.")
        if asset.engine != "Rockstar RAGE (GTA IV)":
            raise MeshFormatError(
                f"{asset.engine or asset.extension} cannot be routed to the RPF/IMG reader."
            )
        # IMG3 is unencrypted and dependency-free. RPF2/RPF3 may need the
        # AES reader, which is resolved automatically only when required.
        from .rage import IMG3_MAGIC
        with asset.path.open("rb") as stream:
            signature = stream.read(4)
        if len(signature) != 4 or struct.unpack("<I", signature)[0] != IMG3_MAGIC:
            from .adapters import ensure_adapter
            ensure_adapter(
                asset,
                "rpf3-read",
                progress=lambda text: self.after(0, self.status_var.set, text),
            )
        self.after(0, self.status_var.set, f"Inspecting {asset.path.name} for previewable geometry…")
        from .grouping import build_bundles
        from .rage import inspect_gtaiv_archive, read_internal_asset
        discovered, warnings = inspect_gtaiv_archive(self.result.root, asset)
        internal_bundles = build_bundles(discovered)
        if warnings:
            self.result.warnings.extend(warnings)
        if not internal_bundles:
            detail = "\n".join(warnings) if warnings else "The archive table contained no readable resource records."
            raise MeshFormatError(f"No previewable resources could be read from {asset.relative_path}.\n\n{detail}")

        probe_errors: list[str] = []
        for index, candidate in enumerate(internal_bundles, 1):
            candidate_asset = candidate.mesh
            if index == 1 or index % 10 == 0:
                self.after(
                    0, self.status_var.set,
                    f"Testing resource {index:,}/{len(internal_bundles):,} in {asset.path.name}…",
                )
            try:
                raw = read_internal_asset(candidate_asset)
                meshes = load_mesh_bytes(raw, ".rsc", candidate_asset.internal_path or candidate.name)
                self._archive_previews[asset.path] = (candidate, meshes)
                return candidate, meshes
            except (MeshFormatError, OSError, ValueError, struct.error) as error:
                if len(probe_errors) < 3:
                    probe_errors.append(f"{candidate_asset.internal_path}: {error}")
        details = "\n".join(probe_errors)
        raise MeshFormatError(
            f"The archive is readable, but none of its {len(discovered):,} resources matched a supported "
            f"drawable structure.\n\nFirst probe results:\n{details}"
        )


    def export_report(self) -> None:
        if not self.result:
            messagebox.showinfo("No report", "Run a scan first.")
            return
        destination = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON report", "*.json")])
        if destination:
            Path(destination).write_text(json.dumps(self.result.to_dict(), indent=2), encoding="utf-8")
            self.status_var.set(f"Saved report to {destination}")

    def show_notices(self) -> None:
        if not self.result or not self.result.warnings:
            messagebox.showinfo("Scan notices", "No notices for the current scan.")
            return
        messagebox.showinfo("Scan notices", "\n\n".join(self.result.warnings[:20]))

    def extract_selected(self) -> None:
        bundle = self.selected_bundle()
        if not bundle or not self.result:
            messagebox.showinfo("Nothing selected", "Scan and select a candidate first.")
            return
        destination = filedialog.askdirectory(title="Choose a folder for extracted copies")
        if not destination:
            return
        try:
            if bundle.mesh.kind == "container" and bundle.mesh.path in self._archive_previews:
                bundle = self._archive_previews[bundle.mesh.path][0]
            if bundle.mesh.metadata.get("reader") in {
                "frostbite-animation-record", "frostbite-skeleton-record",
            }:
                self.status_var.set(f"Decoding {bundle.mesh.internal_path} from Frostbite CAS…")
                threading.Thread(
                    target=self._extract_frostbite_record_worker,
                    args=(bundle.mesh, Path(destination)), daemon=True,
                ).start()
                return
            from .rage import extract_bundle
            files = extract_bundle(bundle, self.result.root, Path(destination))
            self.status_var.set(f"Extracted {len(files)} file(s) to {destination}; the game archive was not modified.")
            messagebox.showinfo("Extraction complete", f"Copied {len(files)} file(s) to:\n{destination}\n\nThe original archive was not modified.")
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Cannot extract", str(error))

    def _extract_frostbite_record_worker(self, asset, destination: Path) -> None:
        try:
            if not self.result:
                raise ValueError("Scan data is no longer available.")
            engine_root = self.result.engine_root or self.result.root
            if asset.metadata.get("reader") == "frostbite-skeleton-record":
                from .frostbite import extract_frostbite_record_dependencies_isolated
                raw, dependencies = extract_frostbite_record_dependencies_isolated(
                    engine_root, asset,
                )
            else:
                from .frostbite import extract_frostbite_record_isolated
                raw = extract_frostbite_record_isolated(engine_root, asset)
                dependencies = []
            name = PurePosixPath(asset.internal_path or asset.path.name).name
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(raw)
            descriptor = output.with_name(output.name + ".json")
            descriptor.write_text(json.dumps(asset.to_dict(), indent=2), encoding="utf-8")
            linked_outputs: list[Path] = []
            for linked_asset, linked_raw in dependencies:
                linked_name = PurePosixPath(
                    linked_asset.internal_path or "linked_skeleton.ebx"
                ).name
                linked_output = destination / linked_name
                if linked_output == output:
                    linked_output = destination / f"linked_{linked_name}"
                linked_output.write_bytes(linked_raw)
                linked_descriptor = linked_output.with_name(linked_output.name + ".json")
                linked_descriptor.write_text(
                    json.dumps(linked_asset.to_dict(), indent=2), encoding="utf-8",
                )
                linked_outputs.extend((linked_output, linked_descriptor))
            self.after(
                0, self._finish_frostbite_record_extraction,
                output, descriptor, linked_outputs,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.after(0, messagebox.showerror, "Cannot extract Frostbite record", str(error))

    def _finish_frostbite_record_extraction(
        self, output: Path, descriptor: Path, linked_outputs: list[Path],
    ) -> None:
        self.status_var.set(f"Decoded {output.name} and saved its rigging descriptor to {output.parent}.")
        messagebox.showinfo(
            "Frostbite record extracted",
            f"Decoded the CAS payload to:\n{output}\n\n"
            f"Also saved:\n{descriptor.name}\n\n"
            + (
                "Resolved linked EBX payload(s):\n"
                + "\n".join(path.name for path in linked_outputs)
                + "\n\n"
                if linked_outputs else ""
            )
            + "The original game files were not modified.",
        )


def run_gui() -> None:
    App().mainloop()
