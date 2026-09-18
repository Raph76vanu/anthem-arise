import tempfile
import unittest
import struct
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

from game_asset_explorer.geometry import MeshFormatError, load_mesh, load_mesh_bytes, load_skeleton_bytes
from game_asset_explorer.adapters import adapter_for
from game_asset_explorer.engines import engine_for, parent_engine_root
from game_asset_explorer.grouping import category_for_asset
from game_asset_explorer.models import AssetRecord
from game_asset_explorer.scanner import scan
from game_asset_explorer.rage import (
    IMG3_MAGIC, inspect_gtaiv_archive, parse_archive_entries, read_internal_asset,
)
from game_asset_explorer.frostbite import inspect_frostbite_metadata, select_metadata_containers
from game_asset_explorer.unity import (
    choose_unity_preview_asset, inspect_unity_container_isolated,
    select_unity_containers,
)


def _asset(root: Path, name: str, kind: str, extension: str | None = None) -> AssetRecord:
    path = root / name
    return AssetRecord(path, name, kind, extension or path.suffix.lower(), 100)


class ScannerTests(unittest.TestCase):
    def test_nested_frostbite_folder_keeps_scope_and_finds_parent_layout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            installation = Path(raw) / "Anthem"
            data = installation / "Data"
            selected = data / "Win32" / "levels"
            selected.mkdir(parents=True)
            (data / "layout.toc").write_bytes(b"layout")
            (selected / "javelin.meshset").write_bytes(b"mesh")

            engine, engine_root = parent_engine_root(selected)
            result = scan(selected)

        self.assertEqual(engine, "Frostbite")
        self.assertEqual(engine_root, installation)
        self.assertEqual(result.root, selected)
        self.assertEqual(result.engine_root, installation)
        self.assertIn("Frostbite", result.engine_hints)
        self.assertEqual(result.assets[0].relative_path, "javelin.meshset")

    def test_asset_categories_are_capability_based(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual(category_for_asset(_asset(root, "idle.anim", "animation")), "Animations")
            self.assertEqual(category_for_asset(_asset(root, "hero.skel", "skeleton")), "Skeletons")
            self.assertEqual(category_for_asset(_asset(root, "street_building.fbx", "mesh")), "Map parts")
            self.assertEqual(category_for_asset(_asset(root, "pistol.fbx", "mesh")), "Weapons & carryables")
            self.assertEqual(category_for_asset(_asset(root, "garden_plant.fbx", "mesh")), "Props")
            self.assertEqual(category_for_asset(_asset(root, "preview.png", "texture", ".png")), "Images")
            self.assertEqual(category_for_asset(_asset(root, "normal.dds", "texture", ".dds")), "Textures")
            self.assertEqual(category_for_asset(_asset(root, "weapons.img", "container", ".img")), "Containers / archives")
            self.assertEqual(category_for_asset(_asset(root, "map_buildings.img", "container", ".img")), "Containers / archives")
            self.assertEqual(category_for_asset(_asset(root, "pedprops.img", "container", ".img")), "Containers / archives")
            self.assertEqual(category_for_asset(_asset(root, "head.wft", "mesh", ".wft")), "Characters / meshes")
            self.assertEqual(category_for_asset(_asset(root, "pc/data/maps/borough/block.wdr", "mesh", ".wdr")), "Map parts")

    def test_common_game_animation_extensions_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "walk.ifp").write_bytes(b"anim")
            (root / "run.ycd").write_bytes(b"anim")
            result = scan(root)
            self.assertEqual({asset.extension for asset in result.assets}, {".ifp", ".ycd"})
            self.assertTrue(all(asset.kind == "animation" for asset in result.assets))

    def test_tune_skeleton_exposes_joint_graph(self) -> None:
        raw = b"root\0pelvis\0spine\0neck\0head\0upperarm_l\0forearm_l\0hand_l\0"
        skeleton = load_skeleton_bytes(raw, "test.tune")
        self.assertGreaterEqual(len(skeleton.joints), 5)
        self.assertEqual(skeleton.joints[0][1], -1)
        self.assertTrue(any(parent >= 0 for _name, parent, *_coords in skeleton.joints))

    def test_tune_objects_are_not_classified_as_skeletons(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "bm_parking_meter02.tune").write_bytes(b"configuration")
            (root / "cj_ld_skel_1.tune").write_bytes(b"configuration")
            result = scan(root)
            self.assertFalse(any(asset.extension == ".tune" for asset in result.assets))

    def test_generic_transform_matrices_are_not_guessed_as_skeletons(self) -> None:
        raw = struct.pack("<16f", *([1.0, 0.0, 0.0, 0.0] * 4)) * 20
        with self.assertRaisesRegex(MeshFormatError, "verified skeleton hierarchy"):
            load_skeleton_bytes(raw, "vehicle.wft")

    def test_extensionless_unityfs_bundle_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "StreamingAssets" / "Cache" / "bundles" / "sge_exoskeletons_v3" / "hash_without_extension"
            bundle.parent.mkdir(parents=True)
            bundle.write_bytes(b"UnityFS\0" + b"\0" * 64)
            result = scan(root)
            asset = next(item for item in result.assets if item.path == bundle)
            self.assertEqual(asset.kind, "container")
            self.assertEqual(asset.engine, "Unity")
            self.assertEqual(adapter_for(asset, "unity-read").capability, "unity-read")

    def test_unity_resource_stream_is_not_listed_as_an_asset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "sharedassets0.assets").write_bytes(b"serialized")
            (root / "sharedassets0.assets.resS").write_bytes(b"stream")
            result = scan(root)
            self.assertEqual([asset.path.name for asset in result.assets], ["sharedassets0.assets"])

    def test_unity_probe_selection_is_bounded_and_skips_localization(self) -> None:
        root = Path("game")
        containers = [
            AssetRecord(
                root / f"bundles/object_{index}", f"bundles/object_{index}",
                "container", "", 1_000 + index, engine="Unity",
            )
            for index in range(80)
        ]
        containers.append(AssetRecord(
            root / "localization-string-tables-fr",
            "localization-string-tables-fr", "container", "", 5_000_000,
            engine="Unity",
        ))
        chosen = select_unity_containers(containers, "Props", limit=15)
        self.assertEqual(len(chosen), 15)
        self.assertFalse(any("localization" in item.relative_path for item in chosen))

    def test_unity_probe_does_not_prioritize_unhinted_multigigabyte_files(self) -> None:
        root = Path("game")
        huge = AssetRecord(root / "sharedassets", "sharedassets", "container", ".assets", 4 * 1024**3, engine="Unity")
        normal = AssetRecord(root / "props_bundle", "props_bundle", "container", ".bundle", 8 * 1024**2, engine="Unity")
        self.assertEqual(select_unity_containers([huge, normal], "Props", limit=2), [normal])

    def test_unity_worker_timeout_is_a_bounded_format_error(self) -> None:
        asset = AssetRecord(Path("bundle"), "bundle", "container", ".bundle", 100, engine="Unity")
        with patch("game_asset_explorer.unity.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 1)):
            with self.assertRaisesRegex(MeshFormatError, "safety limit"):
                inspect_unity_container_isolated(Path("."), asset, timeout=1)

    def test_unity_worker_failure_does_not_escape_as_process_error(self) -> None:
        asset = AssetRecord(Path("broken.bundle"), "broken.bundle", "container", ".bundle", 100, engine="Unity")
        failed = types.SimpleNamespace(returncode=9, stdout="", stderr="decoder crashed\n")
        with patch("game_asset_explorer.unity.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(MeshFormatError, "isolated reader: decoder crashed"):
                inspect_unity_container_isolated(Path("."), asset)

    def test_unity_preview_choice_uses_real_typed_child(self) -> None:
        root = Path("game")
        container = root / "bundle"
        texture = AssetRecord(
            container, "bundle::crate_diffuse#2", "texture", ".unitytexture", 10,
            engine="Unity", directly_viewable=True, internal_path="crate_diffuse#2",
            metadata={"reader": "unity", "unity_type": "Texture2D"},
        )
        mesh = AssetRecord(
            container, "bundle::crate#1", "mesh", ".unitymesh", 10,
            engine="Unity", directly_viewable=True, internal_path="crate#1",
            metadata={"reader": "unity", "unity_type": "Mesh"},
        )
        self.assertIs(choose_unity_preview_asset([texture, mesh], "Props"), mesh)
        self.assertIs(choose_unity_preview_asset([texture, mesh], "Textures"), texture)

    def test_unity_container_publishes_typed_internal_assets(self) -> None:
        class Pointer:
            def __init__(self, path_id):
                self.path_id = path_id

            def deref(self):
                return readers_by_id[self.path_id]

        class Reader:
            def __init__(self, path_id, object_type, name, parsed=None):
                self.path_id = path_id
                self.type = types.SimpleNamespace(name=object_type)
                self.assets_file = types.SimpleNamespace(name="sharedassets0.assets")
                self.byte_size = 100
                self._name = name
                self._parsed = parsed

            def peek_name(self):
                return self._name

            def parse_as_object(self):
                return self._parsed

        mesh = Reader(10, "Mesh", "Hero Body")
        texture = Reader(11, "Texture2D", "Hero Diffuse")
        animation = Reader(12, "AnimationClip", "Hero Idle")
        bone_a = Reader(20, "Transform", "Pelvis")
        bone_b = Reader(21, "Transform", "Spine")
        game_object = Reader(30, "GameObject", "Hero Renderer")
        renderer_data = types.SimpleNamespace(
            m_Mesh=Pointer(10), m_Bones=[Pointer(20), Pointer(21)], m_GameObject=Pointer(30),
        )
        renderer = Reader(13, "SkinnedMeshRenderer", "", renderer_data)
        readers = [mesh, texture, animation, renderer, bone_a, bone_b, game_object]
        readers_by_id = {reader.path_id: reader for reader in readers}
        fake_unity = types.SimpleNamespace(load=lambda _path: types.SimpleNamespace(objects=readers))

        with tempfile.TemporaryDirectory() as raw, patch.dict(sys.modules, {"UnityPy": fake_unity}):
            root = Path(raw)
            path = root / "sharedassets0.assets"
            path.write_bytes(b"data")
            container = AssetRecord(path, path.name, "container", ".assets", 4, engine="Unity")
            from game_asset_explorer.unity import inspect_unity_container
            found, warnings = inspect_unity_container(root, container)

        self.assertFalse(warnings)
        self.assertEqual({asset.kind for asset in found}, {"mesh", "texture", "animation", "skeleton"})
        self.assertTrue(next(asset for asset in found if asset.kind == "mesh").metadata["skinned"])
        skeleton = next(asset for asset in found if asset.kind == "skeleton")
        self.assertEqual(skeleton.metadata["unity_bone_path_ids"], [20, 21])

    def test_gtaiv_map_img_path_is_recognized_as_rage(self) -> None:
        path = Path("game") / "pc" / "data" / "maps" / "east" / "queens.img"
        self.assertEqual(engine_for(path), "Rockstar RAGE (GTA IV)")

    def test_character_bundle_is_ranked_first(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            char = root / "characters" / "ranger"
            props = root / "props"
            char.mkdir(parents=True)
            props.mkdir()
            (char / "ranger_body.fbx").write_bytes(b"x" * 60_000)
            (char / "ranger_skeleton.rig").write_bytes(b"rig")
            (char / "ranger_idle.anim").write_bytes(b"anim")
            (char / "ranger_diffuse.png").write_bytes(b"png")
            (props / "crate.obj").write_bytes(b"obj")

            result = scan(root)
            self.assertEqual(result.bundles[0].mesh.path.name, "ranger_body.fbx")
            self.assertEqual(result.bundles[0].count("skeleton"), 1)
            self.assertEqual(result.bundles[0].count("animation"), 1)

    def test_frostbite_is_detected_without_claiming_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "layout.toc").write_bytes(b"toc")
            (root / "Data.cas").write_bytes(b"cas")
            result = scan(root)
            self.assertIn("Frostbite", result.engine_hints)
            self.assertFalse(result.bundles)

    def test_frostbite_metadata_inventory_is_bounded_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "default.sb"
            path.write_bytes(
                b"header\0characters/javelin/ranger_mesh\0"
                b"animations/javelin/ranger_idle.anim\0"
                b"textures/javelin/ranger_diffuse.dds\0"
                + b"x" * 256
            )
            container = AssetRecord(
                path, path.name, "container", ".sb", path.stat().st_size,
                engine="Frostbite",
            )
            found, notices = inspect_frostbite_metadata(root, container, max_bytes=160)

        self.assertEqual({item.kind for item in found}, {"mesh", "animation", "texture"})
        self.assertTrue(all(item.metadata["reader"] == "frostbite-reference" for item in found))
        self.assertTrue(notices)

    def test_frostbite_container_has_its_own_capability(self) -> None:
        asset = AssetRecord(
            Path("default.sb"), "default.sb", "container", ".sb", 10,
            engine="Frostbite",
        )
        self.assertIsNone(adapter_for(asset, "rpf3-read"))
        self.assertEqual(
            adapter_for(asset, "frostbite-metadata-read").capability,
            "frostbite-metadata-read",
        )

    def test_frostbite_inventory_selects_one_bounded_entry_per_pair(self) -> None:
        root = Path("game")
        assets = []
        for index in range(20):
            for suffix in (".toc", ".sb"):
                path = root / f"bundle_{index}{suffix}"
                assets.append(AssetRecord(
                    path, str(path), "container", suffix, 100 + index,
                    engine="Frostbite",
                ))
        selected = select_metadata_containers(assets, "Animations", limit=7)
        self.assertEqual(len(selected), 7)
        self.assertTrue(all(asset.extension == ".sb" for asset in selected))
        self.assertEqual(len({asset.path.with_suffix("") for asset in selected}), 7)

    def test_gtaiv_character_archive_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_dir = root / "pc" / "models" / "cdimages"
            archive_dir.mkdir(parents=True)
            (root / "GTAIV.exe").write_bytes(b"not a real executable")
            (archive_dir / "componentpeds.img").write_bytes(b"RPF3")
            result = scan(root)
            self.assertIn("Rockstar RAGE (GTA IV)", result.engine_hints)
            self.assertTrue(result.bundles)
            self.assertEqual(result.bundles[0].mesh.path.name, "componentpeds.img")
            self.assertFalse(result.bundles[0].mesh.directly_viewable)
            self.assertEqual(adapter_for(result.bundles[0].mesh, "rpf3-read").capability, "rpf3-read")

    def test_gtaiv_prop_archive_is_not_presented_as_a_character(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_dir = root / "pc" / "models" / "cdimages"
            archive_dir.mkdir(parents=True)
            (archive_dir / "pedprops.img").write_bytes(b"RPF3")
            result = scan(root)
            self.assertFalse(any(bundle.mesh.path.name == "pedprops.img" for bundle in result.bundles))

    def test_obj_geometry_for_embedded_viewer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "character.obj"
            path.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
            meshes = load_mesh(path)
            self.assertEqual(len(meshes), 1)
            self.assertEqual(len(meshes[0].vertices), 3)
            self.assertEqual(meshes[0].faces, [(0, 1, 2)])

    def test_hashed_rpf3_resource_is_discovered_by_flags(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_dir = root / "pc" / "models" / "cdimages"
            archive_dir.mkdir(parents=True)
            archive = archive_dir / "playerped.rpf"
            resource = b"RSC\x05" + struct.pack("<II", 110, 0) + b"binary-resource-body"
            data_offset = 0x900
            toc_entry = struct.pack(
                "<IIII", 0x1234ABCD, len(resource), data_offset | 0x6E,
                0x80000000,
            )
            header = b"RPF3" + struct.pack("<IIII", len(toc_entry), 1, 0, 0)
            archive.write_bytes(
                header.ljust(0x800, b"\0") + toc_entry.ljust(data_offset - 0x800, b"\0") + resource
            )
            result = scan(root)
            placeholder = next(bundle.mesh for bundle in result.bundles if bundle.mesh.path == archive)
            discovered, warnings = inspect_gtaiv_archive(root, placeholder)
            self.assertFalse(warnings)
            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].internal_path, "1234ABCD.rsc")
            self.assertEqual(discovered[0].metadata["resource_type"], 0x6E)
            self.assertEqual(read_internal_asset(discovered[0]), resource)

    def test_img3_resource_is_discovered_by_signature_and_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_dir = root / "pc" / "models" / "cdimages"
            archive_dir.mkdir(parents=True)
            archive = archive_dir / "componentpeds.img"
            payload = bytearray(0x340)
            struct.pack_into("<I", payload, 0x40, 0x80)
            struct.pack_into("<I", payload, 0x80, 0x90)
            struct.pack_into("<I", payload, 0x90, 0xA0)
            struct.pack_into("<I", payload, 0xA4, 0xC0)
            struct.pack_into("<H", payload, 0xAA, 1)
            struct.pack_into("<I", payload, 0xC0, 0xD0)
            struct.pack_into("<I", payload, 0xDC, 0x140)
            struct.pack_into("<I", payload, 0xEC, 0x180)
            struct.pack_into("<I", payload, 0xFC, 3)
            struct.pack_into("<H", payload, 0x104, 3)
            struct.pack_into("<I", payload, 0x148, 0x200)
            struct.pack_into("<I", payload, 0x150, 0x1C0)
            struct.pack_into("<H", payload, 0x1C4, 28)
            struct.pack_into("<I", payload, 0x188, 0x300)
            for index, vertex in enumerate(((0., 0., 0.), (1., 0., 0.), (0., 1., 0.))):
                struct.pack_into("<3f", payload, 0x200 + index * 28, *vertex)
            struct.pack_into("<3H", payload, 0x300, 0, 1, 2)
            resource = b"RSC\x05" + struct.pack("<II", 110, 0) + payload
            sector_offset = 1
            sector_count = 1
            padding = 2048 - len(resource)
            entry = struct.pack("<IIIHH", 0, 110, sector_offset, sector_count, 0x2000 | padding)
            names = b"m_y_test.wdr\0"
            table = entry + names
            header = struct.pack("<IIIIHH", IMG3_MAGIC, 3, 1, len(table), 16, 0)
            archive.write_bytes((header + table).ljust(2048, b"\0") + resource)

            result = scan(root)
            placeholder = next(bundle.mesh for bundle in result.bundles if bundle.mesh.path == archive)
            discovered, warnings = inspect_gtaiv_archive(root, placeholder)
            self.assertFalse(warnings)
            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].internal_path, "m_y_test.wdr")
            self.assertEqual(discovered[0].extension, ".wdr")
            self.assertEqual(discovered[0].metadata["archive_format"], "IMG3")
            self.assertEqual(read_internal_asset(discovered[0]), resource)
            meshes = load_mesh_bytes(read_internal_asset(discovered[0]), ".rsc", "m_y_test.wdr")
            self.assertEqual(meshes[0].faces, [(0, 1, 2)])

    def test_encrypted_img3_header_and_table_are_auto_detected(self) -> None:
        try:
            from Crypto.Cipher import AES
        except ImportError:
            self.skipTest("optional pycryptodome adapter is not installed")

        def encrypt_blocks(data: bytes, key: bytes) -> bytes:
            aligned = len(data) & ~0x0F
            encrypted = data[:aligned]
            for _ in range(16):
                encrypted = AES.new(key, AES.MODE_ECB).encrypt(encrypted)
            return encrypted + data[aligned:]

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "componentpeds.img"
            executable = root / "GTAIV.exe"
            executable.write_bytes(b"test executable")
            key = bytes(range(32))
            resource = b"RSC\x05" + struct.pack("<II", 110, 0) + b"test"
            padding = 2048 - len(resource)
            table = struct.pack("<IIIHH", 0, 110, 1, 1, 0x2000 | padding) + b"ped.wdr\0"
            header = struct.pack("<IIIIHH", IMG3_MAGIC, 3, 1, len(table), 16, 0)
            archive.write_bytes(
                (encrypt_blocks(header, key) + encrypt_blocks(table, key)).ljust(2048, b"\0") + resource
            )
            with patch("game_asset_explorer.rage._aes_key", return_value=key):
                archive_format, entries = parse_archive_entries(archive, executable)
            self.assertEqual(archive_format, "IMG3-AES")
            self.assertEqual(entries[0].name, "ped.wdr")
            self.assertEqual(entries[0].file_offset, 2048)

    def test_unnamed_rsc_is_probed_as_drawable(self) -> None:
        payload = bytearray(0x340)
        struct.pack_into("<I", payload, 0x40, 0x80)   # model collection
        struct.pack_into("<I", payload, 0x80, 0x90)   # model pointer array
        struct.pack_into("<I", payload, 0x90, 0xA0)   # model
        struct.pack_into("<I", payload, 0xA4, 0xC0)   # geometry pointer array
        struct.pack_into("<H", payload, 0xAA, 1)      # geometry count
        struct.pack_into("<I", payload, 0xC0, 0xD0)   # geometry
        struct.pack_into("<I", payload, 0xDC, 0x140)  # vertex buffer
        struct.pack_into("<I", payload, 0xEC, 0x180)  # index buffer
        struct.pack_into("<I", payload, 0xFC, 3)      # index count
        struct.pack_into("<H", payload, 0x104, 3)     # vertex count
        struct.pack_into("<I", payload, 0x148, 0x200) # vertex data
        struct.pack_into("<I", payload, 0x150, 0x1C0) # declaration
        struct.pack_into("<H", payload, 0x1C4, 28)    # stride
        struct.pack_into("<I", payload, 0x188, 0x300) # index data
        for index, vertex in enumerate(((0., 0., 0.), (1., 0., 0.), (0., 1., 0.))):
            struct.pack_into("<3f", payload, 0x200 + index * 28, *vertex)
        struct.pack_into("<3H", payload, 0x300, 0, 1, 2)
        raw = b"RSC\x05" + struct.pack("<II", 110, 0) + payload
        meshes = load_mesh_bytes(raw, ".rsc", "hashed")
        self.assertEqual(len(meshes), 1)
        self.assertEqual(meshes[0].faces, [(0, 1, 2)])

    def test_bad_wdd_has_a_bounded_format_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "radar.wdd"
            path.write_bytes(b"RSC\x05" + struct.pack("<II", 110, 0) + b"\0" * 32)
            with self.assertRaisesRegex(MeshFormatError, "WDD|drawable geometry"):
                load_mesh(path)


if __name__ == "__main__":
    unittest.main()
