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
        self.category_var = tk.StringVar(value=self.CATEGORY_OPTIONS[0])
        self.search_var = tk.StringVar()
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
        self._preview_character: dict[str, object] | None = None
        self._active_character: dict[str, object] | None = None
        self.use_for_animation_var = tk.BooleanVar(value=False)
        self.active_character_var = tk.StringVar(value="No active character")
        self.animation_clip_var = tk.StringVar(value="Animation: none selected")
        self.animation_state_var = tk.StringVar(value="Select an animation record to inspect it.")
        self.animation_loop_var = tk.BooleanVar(value=True)
        self.animation_speed_var = tk.StringVar(value="1.0×")
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
        ttk.Label(filter_bar, text="Search:").pack(side="left", padx=(18, 0))
        search_entry = ttk.Entry(filter_bar, textvariable=self.search_var, width=34)
        search_entry.pack(side="left", padx=(8, 0), fill="x", expand=True)
        search_entry.bind("<KeyRelease>", self._schedule_search_refresh)

        self.folder_scope = ttk.Frame(self, padding=(12, 0, 12, 8))
        self.folder_scope_var = tk.StringVar()
        ttk.Label(self.folder_scope, textvariable=self.folder_scope_var).pack(
            side="left", fill="x", expand=True,
        )
        ttk.Button(
            self.folder_scope, text="Back to all results", command=self._leave_folder_view,
        ).pack(side="right")

        self.animation_bar = ttk.Frame(self, padding=(12, 0, 12, 8))
        ttk.Label(
            self.animation_bar, textvariable=self.active_character_var,
            foreground="#176b3a",
        ).pack(side="left")
        ttk.Separator(self.animation_bar, orient="vertical").pack(
            side="left", fill="y", padx=10,
        )
        ttk.Label(self.animation_bar, textvariable=self.animation_clip_var).pack(side="left")
        self.animation_play_button = ttk.Button(
            self.animation_bar, text="Play", command=self._play_active_animation,
            state="disabled",
        )
        self.animation_play_button.pack(side="left", padx=(12, 4))
        self.animation_stop_button = ttk.Button(
            self.animation_bar, text="Stop", command=self._stop_active_animation,
            state="disabled",
        )
        self.animation_stop_button.pack(side="left", padx=4)
        ttk.Checkbutton(
            self.animation_bar, text="Loop", variable=self.animation_loop_var,
            state="disabled",
        ).pack(side="left", padx=4)
        ttk.Label(self.animation_bar, textvariable=self.animation_speed_var).pack(
            side="left", padx=4,
        )
        ttk.Label(
            self.animation_bar, textvariable=self.animation_state_var,
            foreground="#6b7280",
        ).pack(side="right")

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
        self.use_for_animation_check = ttk.Checkbutton(
            bottom, text="Use for animation", variable=self.use_for_animation_var,
            command=self._toggle_active_character, state="disabled",
        )
        self.use_for_animation_check.grid(row=0, column=2, padx=(0, 8))
        self.rig_button = ttk.Button(
            bottom, text="Inspect rig…", command=self.show_rig_inspector, state="disabled",
        )
        self.rig_button.grid(row=0, column=3, padx=(0, 8))
        ttk.Button(
            bottom, text="Extract selected…", command=self.extract_selected,
        ).grid(row=0, column=4, padx=(0, 8))
        ttk.Button(
            bottom, text="Export report…", command=self.export_report,
        ).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(bottom, text="Notices…", command=self.show_notices).grid(
            row=0, column=6,
        )

    def choose(self) -> None:
        chosen = filedialog.askdirectory(title="Choose game installation")
        if chosen:
            self.path_var.set(chosen)

    @staticmethod
    def _javelin_family(asset) -> str | None:
        value = (asset.internal_path or asset.relative_path).replace("\\", "/").casefold()
        matches = re.findall(r"(?:^|/)(ex[a-z])(?=_|/)", value)
        specific = [family for family in matches if family != "exo"]
        return specific[-1] if specific else None

    def _category_changed(self, _event=None) -> None:
        self._refresh_result()
        self._update_animation_bar()

    def _update_animation_bar(self) -> None:
        visible = self._active_character is not None or self.category_var.get() == "Animations"
        if visible:
            if not self.animation_bar.winfo_manager():
                self.animation_bar.pack(fill="x", before=self.pane)
        else:
            self.animation_bar.pack_forget()

    def _toggle_active_character(self) -> None:
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
        self._update_animation_bar()
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

    def _play_active_animation(self) -> None:
        self.status_var.set("This control unlocks when the selected Frostbite clip keyframes are decoded.")

    def _stop_active_animation(self) -> None:
        self._show_active_character()

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
        self._preview_character = None
        self._active_character = None
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
            term for term in self.search_var.get().lower().split() if term
        )

        def search_matches(asset) -> bool:
            if not search_terms:
                return True
            haystack = " ".join((
                asset.relative_path,
                asset.internal_path or "",
                str(asset.metadata.get("bundle_name", "")),
            )).lower()
            return all(term in haystack for term in search_terms)

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
            self.tree.heading("mesh", text="Asset / archive")
            # Non-character categories are an inventory view, so they include
            # low-confidence files by design; the character checkbox remains
            # dedicated to the default ranking view.
            matching = [
                asset for asset in result.assets
                if category_for_asset(asset) == category
                and in_virtual_scope(asset)
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
                "yes" if bundle.count("skeleton") else "—", bundle.count("animation"),
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
                self.after(0, self._show_frostbite_animation_record, asset, title, info)
                return
            if asset.metadata.get("reader") == "frostbite-skeleton-record":
                skeleton = self._decode_frostbite_skeleton_asset(asset)
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
                skeleton_error = None
                try:
                    self.after(0, self.status_var.set, "Resolving the matching Javelin bind skeleton…")
                    skeleton = self._decode_matching_frostbite_skeleton(asset)
                except (MeshFormatError, OSError, RuntimeError, ValueError) as error:
                    skeleton_error = str(error)
                self.after(
                    0, self._show_frostbite_preview, asset, meshes, lod_index,
                    decoded_name, available_lods, rig_diagnostic, skeleton, skeleton_error,
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
        compatibility, compatible = self._animation_compatibility(asset)
        if self._active_character is not None:
            self._show_active_character()
        else:
            self.viewer.clear(
                "Animation record loaded\n\n"
                "Preview a rigged Javelin mesh and enable Use for animation first."
            )
        display_name = PurePosixPath(asset.internal_path or title).name
        self.animation_clip_var.set(f"Animation: {display_name} · {compatibility}")
        kind = str(info.get("record_kind", "record")).upper()
        if "inspection_error" in info:
            structure = f"{kind} payload loaded; {info['inspection_error']}"
        elif kind == "EBX":
            structure = (
                f"EBX loaded: {int(info.get('imports', 0))} imports, "
                f"{int(info.get('arrays', 0))} arrays"
            )
        else:
            structure = f"{kind} payload loaded ({int(info.get('bytes', 0)):,} bytes)"
        if compatible is False:
            state = "Different Javelin family; playback remains disabled."
        elif self._active_character is None:
            state = "No active character; playback remains disabled."
        else:
            state = "Record inspected; compressed animation keyframes are not decoded yet."
        self.animation_state_var.set(state)
        self.animation_play_button.configure(state="disabled")
        self.animation_stop_button.configure(state="disabled")
        self._update_animation_bar()
        self.status_var.set(f"{structure}. {state}")

    def _show_frostbite_preview(
        self, asset, meshes, lod_index: int, decoded_name: str,
        available_lods: list[int], rig_diagnostic: dict[str, object] | str,
        skeleton=None, skeleton_error: str | None = None,
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
        skeleton_message = (
            f" Bind skeleton decoded: {len(skeleton.joints):,} bones; use Bones to toggle it."
            if skeleton is not None and skeleton_error is None
            else f" Bone overlay unavailable: {skeleton_error}" if skeleton_error else ""
        )
        self.status_var.set(
            f"Previewing decoded Frostbite MeshSet {decoded_name} · LOD{lod_index}. "
            + detail_message + " " + summary + skeleton_message
        )

    def _matching_master_skeleton_asset(self, mesh_asset):
        internal = (mesh_asset.internal_path or "").replace("\\", "/").lower()
        match = re.search(r"(?:^|/)(ex[a-z])_[^/]+(?:/|$)", internal)
        if not match or not self.result:
            return None
        expected = f"{match.group(1)}_master_skeleton.ebx"
        return next((
            candidate for candidate in self.result.assets
            if candidate.metadata.get("reader") == "frostbite-skeleton-record"
            and (candidate.internal_path or "").replace("\\", "/").lower().endswith("/" + expected)
        ), None)

    def _decode_frostbite_skeleton_asset(self, skeleton_asset):
        key = (skeleton_asset.internal_path or skeleton_asset.relative_path).casefold()
        cached = self._frostbite_skeleton_cache.get(key)
        if cached is not None:
            return cached
        if not self.result:
            raise MeshFormatError("Scan data is no longer available.")
        from .frostbite import extract_frostbite_record_dependencies_isolated
        from .frostbite_ebx import decode_anthem_skeleton
        primary, dependencies = extract_frostbite_record_dependencies_isolated(
            self.result.engine_root or self.result.root, skeleton_asset,
        )
        attempts = [(linked.internal_path or linked.relative_path, raw) for linked, raw in dependencies]
        attempts.append((skeleton_asset.internal_path or skeleton_asset.relative_path, primary))
        errors = []
        for candidate_name, raw in attempts:
            try:
                skeleton = decode_anthem_skeleton(raw, str(candidate_name))
                self._frostbite_skeleton_cache[key] = skeleton
                return skeleton
            except MeshFormatError as error:
                errors.append(str(error))
        raise MeshFormatError("The linked EBX was found but is not a decoded SkeletonAsset: " + errors[-1])

    def _decode_matching_frostbite_skeleton(self, mesh_asset):
        candidate = self._matching_master_skeleton_asset(mesh_asset)
        if candidate is None:
            raise MeshFormatError("No matching indexed master-skeleton entry was found.")
        return self._decode_frostbite_skeleton_asset(candidate)

    def _matching_master_skeleton(self, asset) -> str:
        """Return the indexed master-skeleton path suggested by a Javelin prefix."""
        internal = (asset.internal_path or "").replace("\\", "/").lower()
        match = re.search(r"(?:^|/)(ex[a-z])_[^/]+(?:/|$)", internal)
        if not match:
            return "No Javelin skeleton family could be inferred from this mesh name."
        family = match.group(1)
        expected = f"{family}_master_skeleton.ebx"
        if self.result:
            for candidate in self.result.assets:
                candidate_path = (candidate.internal_path or "").replace("\\", "/").lower()
                if (
                    candidate.metadata.get("reader") == "frostbite-skeleton-record"
                    and candidate_path.endswith("/" + expected)
                ):
                    return f"Indexed skeleton candidate: {candidate.internal_path}"
        return f"Expected skeleton candidate: animation/{family}/{expected} (not indexed in the current result set)"

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
            skeleton_error = None
            try:
                skeleton = self._decode_matching_frostbite_skeleton(asset)
            except (MeshFormatError, OSError, RuntimeError, ValueError) as error:
                skeleton_error = str(error)
            self.after(
                0, self._show_frostbite_preview, asset, meshes, decoded_lod,
                decoded_name, available_lods, rig_diagnostic, skeleton, skeleton_error,
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
