from __future__ import annotations

import hashlib
import io
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
    iter_toc_chunks,
    load_chunk_index,
    parse_bundle,
    resolve_frostbite_virtual_asset_key,
    save_chunk_index,
)
from game_asset_explorer.models import AssetRecord


def _cas_record(payload: bytes) -> bytes:
    return struct.pack(">IHH", len(payload), 0x70, len(payload)) + payload


class FrostbiteIndexTests(unittest.TestCase):
    def test_anthem_toc_level_streamed_chunk_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cas = root / "cas_01.cas"
            cas.write_bytes(b"x" * 256)
            uid = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
            guid_offset = 64
            location_offset = 96
            payload = bytearray(128)
            struct.pack_into(
                ">12I", payload, 0,
                0x30, 0, 0, 48, guid_offset, 1, location_offset, 0, 0, 0, 0, 0,
            )
            raw_guid = uid.bytes_le[::-1]
            payload[guid_offset:guid_offset + 16] = raw_guid
            struct.pack_into(">I", payload, guid_offset + 16, 0)
            payload[location_offset:location_offset + 4] = bytes((0, 0, 5, 1))
            struct.pack_into(">II", payload, location_offset + 4, 32, 64)
            layout = LayoutMap(root, root, None, {(False, 5): root}, {0x501: cas})

            with patch(
                "game_asset_explorer.frostbite_index._toc_payload",
                return_value=io.BytesIO(payload),
            ):
                chunks = list(iter_toc_chunks(layout, root / "default.toc"))

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].uid, uid.bytes)
        self.assertEqual(chunks[0].location.cas_id, 0x501)
        self.assertEqual(chunks[0].location.offset, 32)
        self.assertEqual(chunks[0].location.packed_size, 64)

    def test_virtual_asset_resolver_finds_key_in_unclassified_res(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            sb = root / "default.sb"
            sb.write_bytes(b"bundle")
            cas = root / "cas_01.cas"
            cas.write_bytes(b"cas")
            key = bytes.fromhex("cdb55c0a6ebc15cd")

            source_location = CasLocation(1, cas, 1, 8, 8, b"s" * 20)
            target_location = CasLocation(1, cas, 2, 8, 8, b"t" * 20)
            source_item = BundleFile("res", "animation/exm/source", 1, b"", source_location)
            target_item = BundleFile("res", "hidden/subject_bank", 2, b"", target_location)
            bundle = BundleInfo("animation/exm/test", sb, 12, (source_item, target_item))
            source = AssetRecord(
                path=sb, relative_path="default.sb::animation/exm/source.res",
                kind="animation", extension=".res", size=8, engine="Frostbite",
                internal_path="animation/exm/source.res",
                metadata={
                    "reader": "frostbite-animation-record", "record_kind": "res",
                    "bundle_name": bundle.name, "bundle_offset": 12,
                    "sha1": source_location.sha1.hex(), "cas_id": 1,
                    "cas_path": str(cas), "cas_offset": 1, "packed_size": 8,
                    "original_size": 8,
                },
            )
            target_raw = bytearray(128)
            target_raw[:8] = b"GD.DATAl"
            struct.pack_into("<I", target_raw, 8, len(target_raw))
            target_raw[72:80] = key
            target_raw[96:103] = b"GD.REFL"

            layout = LayoutMap(root, root, None, {}, {1: cas})
            with patch(
                "game_asset_explorer.frostbite_index.load_layout_map", return_value=layout,
            ), patch(
                "game_asset_explorer.frostbite_index.find_oodle_runtime", return_value=Path("oo2core.dll"),
            ), patch(
                "game_asset_explorer.frostbite_index.OodleDecoder", return_value=object(),
            ), patch(
                "game_asset_explorer.frostbite_index._toc_paths", return_value=[],
            ), patch(
                "game_asset_explorer.frostbite_index.parse_bundle", return_value=bundle,
            ), patch(
                "game_asset_explorer.frostbite_index._read_cas", return_value=b"packed",
            ), patch(
                "game_asset_explorer.frostbite_index.decode_cas_record", return_value=bytes(target_raw),
            ):
                resolved, decoded, scanned, exhaustive = resolve_frostbite_virtual_asset_key(
                    root, source, key,
                )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.internal_path, "hidden/subject_bank.res")
        self.assertEqual(decoded, bytes(target_raw))
        self.assertEqual(scanned, 1)
        self.assertTrue(exhaustive)

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
