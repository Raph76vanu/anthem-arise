from __future__ import annotations

import hashlib
import struct
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from game_asset_explorer.frostbite_index import (
    BundleFile,
    BundleInfo,
    CasLocation,
    LayoutMap,
    MESHSET_RESOURCE_TYPE,
    _annotate_meshset_lod_availability,
    find_bundle_chunks,
    frostbite_rig_record_kind,
    is_animation_record,
    load_chunk_index,
    parse_bundle,
    save_chunk_index,
)
from game_asset_explorer.models import AssetRecord


def _cas_record(payload: bytes) -> bytes:
    return struct.pack(">IHH", len(payload), 0x70, len(payload)) + payload


class FrostbiteIndexTests(unittest.TestCase):
    def test_animation_record_does_not_mislabel_meshset_in_animation_folder(self) -> None:
        location = CasLocation(1, Path("cas_01.cas"), 0, 8, 8, b"x" * 20)
        mesh = BundleFile(
            "res", "animation/xzcr_right/curtain_model_mesh",
            MESHSET_RESOURCE_TYPE, b"", location,
        )
        clip_candidate = BundleFile(
            "ebx", "animation/javelin/locomotion_run", 0, b"", location,
        )
        self.assertFalse(is_animation_record(mesh))
        self.assertTrue(is_animation_record(clip_candidate))

    def test_master_skeleton_inside_animation_folder_is_a_skeleton(self) -> None:
        location = CasLocation(1, Path("cas_01.cas"), 0, 8, 8, b"x" * 20)
        skeleton = BundleFile(
            "ebx", "animation/exh/exh_master_skeleton", 0, b"", location,
        )
        self.assertEqual(frostbite_rig_record_kind(skeleton), "skeleton")
        self.assertFalse(is_animation_record(skeleton))

    def test_texture_inside_an_animations_folder_tree_is_a_texture_not_animation(self) -> None:
        # Regression guard: confirmed on real Anthem data that a character's
        # head texture is bundled inside an "animations/antanimations/..."
        # folder tree alongside its facial animations. Before this fix,
        # frostbite_rig_record_kind had no texture branch at all, so this
        # kind of file either got misclassified as "animation" (its folder
        # matched that check) or, outside an animations/ folder, silently
        # dropped from the scan entirely (rig_kind was None and it isn't a
        # meshset, so the caller skipped it) -- textures were invisible in
        # the live app either way, regardless of the (separately fixed, but
        # never actually called) frostbite.py heuristic.
        location = CasLocation(1, Path("cas_01.cas"), 0, 8, 8, b"x" * 20)
        texture = BundleFile(
            "ebx",
            "animations/antanimations/humanoids/hmf_abbyneff/head/textures/"
            "hmf_abbyneff_pcatexture_diffflf_bundlegenbp_bundlegen_win32_antstate",
            0, b"", location,
        )
        self.assertEqual(frostbite_rig_record_kind(texture), "texture")
        self.assertFalse(is_animation_record(texture))

    def test_real_animation_clip_is_unaffected_by_the_texture_branch(self) -> None:
        location = CasLocation(1, Path("cas_01.cas"), 0, 8, 8, b"x" * 20)
        clip = BundleFile(
            "res",
            "animations/antanimations/dynamiccontent/actionstations/bwtimelines/exm/"
            "ast_exm_sentinel_idle_1_bundlegenbp_bundlegen_win32_antstate",
            0, b"", location,
        )
        self.assertEqual(frostbite_rig_record_kind(clip), "animation")
        self.assertTrue(is_animation_record(clip))

    def test_abbreviated_diffuse_naming_is_recognized_as_texture(self) -> None:
        # Real Anthem naming abbreviates "diffuse" (diffhf, diffflf) rather
        # than spelling it out.
        location = CasLocation(1, Path("cas_01.cas"), 0, 8, 8, b"x" * 20)
        for suffix in ("diffhf", "diffflf", "diff_hf"):
            texture = BundleFile(
                "ebx", f"animations/humanoids/hmf_x/head/textures/hmf_x_pcatexture_{suffix}",
                0, b"", location,
            )
            self.assertEqual(frostbite_rig_record_kind(texture), "texture", msg=suffix)

    def test_typed_meshset_and_chunk_locations_are_read_from_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cas_path = root / "cas_01.cas"
            mesh_record = _cas_record(b"meshset")
            chunk_record = _cas_record(b"geometry")
            cas = bytearray(256)
            cas[64:64 + len(mesh_record)] = mesh_record
            cas[160:160 + len(chunk_record)] = chunk_record
            cas_path.write_bytes(cas)

            name = b"characters/hero/body_model_mesh\0"
            uid = bytes.fromhex("00112233445566778899aabbccddeeff")
            mesh_hash = hashlib.sha1(mesh_record).digest()
            chunk_hash = hashlib.sha1(chunk_record).digest()
            metadata = bytearray()
            metadata.extend(struct.pack(">8I", 0x9D798ED6, 2, 0, 1, 1, 132, 0, 0))
            metadata.extend(mesh_hash + chunk_hash)
            metadata.extend(struct.pack(">II", 0, 7))
            metadata.extend(struct.pack(">I", MESHSET_RESOURCE_TYPE))
            metadata.extend(b"\0" * 16)
            metadata.extend(struct.pack(">Q", 123))
            metadata.extend(uid + struct.pack(">HHI", 0, 0, 0))
            metadata.extend(name)
            payload = struct.pack(">IIIIII", 0x501, 64, len(mesh_record), 160, len(chunk_record), 0)
            # The trailing value is outside bundle_length and therefore ignored.
            bundle_length = 36 + len(metadata) + len(payload) - 4
            header = struct.pack(">IIIII", 0x20, 0, bundle_length, 2, 0) + b"\0" * 12
            sb_path = root / "default.sb"
            sb_path.write_bytes(header + struct.pack(">I", len(metadata)) + metadata + payload[:-4])

            layout = LayoutMap(root, root, None, {(False, 5): root}, {0x501: cas_path})
            bundle = parse_bundle(layout, sb_path, 0, "test")

        self.assertEqual(len(bundle.files), 2)
        mesh, chunk = bundle.files
        self.assertEqual(mesh.resource_type, MESHSET_RESOURCE_TYPE)
        self.assertEqual(mesh.name, "characters/hero/body_model_mesh")
        self.assertEqual(mesh.location.offset, 64)
        self.assertEqual(chunk.uid, uid)
        self.assertEqual(chunk.location.offset, 160)

    def test_matching_base_and_patch_bundles_supply_all_lod_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "Data"
            patch_root = root / "Patch"
            data.mkdir()
            patch_root.mkdir()
            base_sb = data / "forttarsis.sb"
            patch_sb = patch_root / "forttarsis.sb"
            base_sb.write_bytes(b"base")
            patch_sb.write_bytes(b"patch")
            cas = root / "cas_01.cas"
            cas.write_bytes(b"")
            layout = LayoutMap(root, data, patch_root, {}, {})
            high_uid = bytes.fromhex("00112233445566778899aabbccddeeff")
            shared_uid = bytes.fromhex("ffeeddccbbaa99887766554433221100")

            def chunk(uid: bytes, offset: int) -> BundleFile:
                location = CasLocation(1, cas, offset, 8, 8, b"x" * 20)
                return BundleFile("chunk", "chunk", 0, uid, location)

            base = BundleInfo("exo", base_sb, 100, (chunk(high_uid, 10), chunk(shared_uid, 20)))
            current_patch = BundleInfo("exo", patch_sb, 200, (chunk(shared_uid, 30),))

            refs = {
                data / "forttarsis.toc": [("exo", base_sb, 100)],
                patch_root / "forttarsis.toc": [("exo", patch_sb, 200)],
            }
            with patch(
                "game_asset_explorer.frostbite_index._toc_paths",
                return_value=list(refs),
            ), patch(
                "game_asset_explorer.frostbite_index.iter_toc_bundle_refs",
                side_effect=lambda _layout, toc: iter(refs[toc]),
            ), patch(
                "game_asset_explorer.frostbite_index.parse_bundle",
                return_value=base,
            ):
                chunks = find_bundle_chunks(layout, "exo", current_patch)

        self.assertEqual(chunks[high_uid].location.offset, 10)
        self.assertEqual(chunks[shared_uid].location.offset, 30)

    def test_installation_chunk_cache_loads_only_requested_uids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cas = root / "cas_01.cas"
            cas.write_bytes(b"x" * 64)
            wanted = bytes.fromhex("00112233445566778899aabbccddeeff")
            other = bytes.fromhex("ffeeddccbbaa99887766554433221100")

            def chunk(uid: bytes, offset: int) -> BundleFile:
                location = CasLocation(1, cas, offset, 8, 8, b"x" * 20)
                return BundleFile("chunk", "chunk", 0, uid, location)

            save_chunk_index(root, {wanted: chunk(wanted, 10), other: chunk(other, 20)})
            loaded = load_chunk_index(root, {wanted})

        self.assertEqual(set(loaded), {wanted})
        self.assertEqual(loaded[wanted].location.offset, 10)

    def test_chunk_cache_batches_more_than_sqlite_variable_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cas = root / "cas_01.cas"
            cas.write_bytes(b"x" * 64)
            chunks = {}
            for index in range(1_100):
                uid = index.to_bytes(16, "big")
                location = CasLocation(1, cas, index, 8, 8, b"x" * 20)
                chunks[uid] = BundleFile("chunk", "chunk", 0, uid, location)
            save_chunk_index(root, chunks)
            loaded = load_chunk_index(root, set(chunks))

        self.assertEqual(len(loaded), 1_100)

    def test_lod0_availability_is_based_on_resolved_chunk_uid(self) -> None:
        lod0 = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
        lod1 = uuid.UUID("ffeeddcc-bbaa-9988-7766-554433221100")
        asset = AssetRecord(
            path=Path("Data/default.sb"),
            relative_path="Data/default.sb::exo/ranger.meshset",
            kind="mesh", extension=".meshset", size=100, engine="Frostbite",
            internal_path="exo/ranger.meshset",
            metadata={
                "cas_id": 1, "cas_path": "cas_01.cas", "cas_offset": 0,
                "packed_size": 8, "original_size": 8, "sha1": "00" * 20,
            },
        )
        warnings = []
        lod0_record = BundleFile(
            "chunk", "lod0", 0, lod0.bytes,
            CasLocation(1, Path("Anthem/cas_01.cas"), 42, 8, 8, b"x" * 20),
        )
        with patch(
            "game_asset_explorer.frostbite_index.find_oodle_runtime", return_value=Path("oo2core.dll"),
        ), patch(
            "game_asset_explorer.frostbite_index.OodleDecoder", return_value=object(),
        ), patch(
            "game_asset_explorer.frostbite_index._read_cas", return_value=b"record",
        ), patch(
            "game_asset_explorer.frostbite_index.decode_cas_record", return_value=b"meshset",
        ), patch(
            "game_asset_explorer.frostbite_index.inspect_anthem_meshset",
            return_value=SimpleNamespace(chunk_ids=[str(lod0), str(lod1)]),
        ), patch(
            "game_asset_explorer.frostbite_index.load_chunk_index",
            return_value={lod0.bytes: lod0_record},
        ):
            _annotate_meshset_lod_availability(Path("Anthem"), [asset], warnings)

        self.assertEqual(asset.metadata["available_lods"], [0])
        self.assertIs(asset.metadata["lod0_available"], True)
        self.assertEqual(asset.metadata["lod0_cas_offset"], 42)
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
