from __future__ import annotations

from pathlib import Path
import unittest

from game_asset_explorer.gui import App
from game_asset_explorer.models import AssetRecord, CharacterBundle


class AnimationStateTests(unittest.TestCase):
    def test_animation_column_uses_decoded_clip_count(self) -> None:
        asset = AssetRecord(
            Path("default.sb"), "animation.res", "animation", ".res", 1,
            metadata={"reader": "frostbite-animation-record", "decoded_clip_count": 3},
        )
        self.assertEqual(
            App._animation_display_count(CharacterBundle("animation", asset)), 3,
        )

    def test_uninspected_animation_container_is_not_reported_as_one_clip(self) -> None:
        asset = AssetRecord(
            Path("default.sb"), "animation.res", "animation", ".res", 1,
            metadata={"reader": "frostbite-animation-record"},
        )
        self.assertEqual(
            App._animation_display_count(CharacterBundle("animation", asset)), "…",
        )

    def test_exclude_search_hides_matching_asset_paths_and_clip_names(self) -> None:
        enemy = AssetRecord(
            Path("default.sb"), "Enemy/EXM_sharedbundle.res", "animation", ".res", 1,
            internal_path="animations/enemies_bgc/exm_sharedbundle.res",
        )
        friendly = AssetRecord(
            Path("default.sb"), "Exoplayer/EXM_sharedbundle.res", "animation", ".res", 1,
            internal_path="animations/exoplayer_bgc/exm_sharedbundle.res",
            metadata={"clip_names": ("EXM_Flying_Hover", "EXM_Walk")},
        )
        self.assertFalse(App._asset_matches_search(enemy, ("exm",), ("enemy",)))
        self.assertTrue(App._asset_matches_search(friendly, ("exm",), ("enemy",)))
        self.assertFalse(App._asset_matches_search(friendly, ("exm",), ("hover",)))
        self.assertFalse(App._asset_matches_search(friendly, (), ("fly", "walk")))
        self.assertTrue(App._asset_matches_search(friendly, ("exm", "fly"), ("enemy",)))
        self.assertTrue(App._asset_matches_search(enemy, (), ()))

    def test_res_filter_only_applies_in_animations_category(self) -> None:
        ebx = AssetRecord(Path("default.sb"), "animation", "animation", ".ebx", 1)
        res = AssetRecord(Path("default.sb"), "animation", "animation", ".res", 1)
        self.assertFalse(App._animation_res_visible("Animations", True, ebx))
        self.assertTrue(App._animation_res_visible("Animations", True, res))
        self.assertTrue(App._animation_res_visible("Animations", False, ebx))
        self.assertTrue(App._animation_res_visible("All", True, ebx))

    def test_equipment_folder_does_not_override_exm_family(self) -> None:
        clip = AssetRecord(
            Path("default.sb"), "animation", "animation", ".res", 1,
            internal_path=("animations/antanimations/equipment/gear/lancer/wrist/"
                           "projectilelauncher/beam/lancer_projectilelauncher_beam_gear_antstate.res"),
        )
        self.assertEqual(App._javelin_family(clip), "exm")
        unknown = AssetRecord(
            Path("default.sb"), "animation", "animation", ".res", 1,
            internal_path="animations/antanimations/equipment/gear/wrist/beam/beam_idle.res",
        )
        self.assertIsNone(App._javelin_family(unknown))

    def test_javelin_family_is_shared_by_mesh_and_animation_paths(self) -> None:
        mesh = AssetRecord(
            Path("forttarsis.sb"), "mesh", "mesh", ".meshset", 1,
            internal_path="exo/exh_colossus/exh_colossus_base_model_mesh.meshset",
        )
        animation = AssetRecord(
            Path("forttarsis.sb"), "animation", "animation", ".ebx", 1,
            internal_path="animation/exh/exh_locomotion.ebx",
        )

        self.assertEqual(App._javelin_family(mesh), "exh")
        self.assertEqual(App._javelin_family(animation), "exh")

    def test_javelin_family_generalizes_to_non_javelin_creature_families(self) -> None:
        # Confirmed on real scanned data: the same family/skeleton naming
        # convention is used for many non-Javelin creatures (ara, dml, sox,
        # glm, wrap, and others), not just Javelins -- despite the method's
        # name, kept for compatibility.
        mesh = AssetRecord(
            Path("default.sb"), "mesh", "mesh", ".meshset", 1,
            internal_path="enemies/creatures/ara_worker/ara_worker_model_mesh.meshset",
        )
        self.assertEqual(App._javelin_family(mesh), "ara")

    def test_javelin_family_not_fooled_by_the_filename_itself(self) -> None:
        # Regression guard: a long animation filename's own leading word
        # (e.g. "ast" in "ast_exm_sentinel_idle_1_...") must not be picked
        # over the real family folder ("exm") just because it appears later
        # in the string -- findall+last-match found the filename's word
        # first without this fix.
        animation = AssetRecord(
            Path("default.sb"), "animation", "animation", ".res", 1,
            internal_path=(
                "animations/antanimations/dynamiccontent/actionstations/bwtimelines/"
                "exm/ast_exm_sentinel_idle_1_bundlegenbp_bundlegen_win32_antstate.res"
            ),
        )
        self.assertEqual(App._javelin_family(animation), "exm")

    def test_javelin_family_handles_a_family_descriptor_folder(self) -> None:
        # e.g. "exl_storm" -- the family ("exl") is followed by an
        # underscore and a descriptor within the same folder segment, not
        # a bare family-only folder.
        animation = AssetRecord(
            Path("default.sb"), "animation", "animation", ".res", 1,
            internal_path="animations/antanimations/exo/exl_storm/exlstorm_template_bundlegenbp_bundlegen_win32_antstate.res",
        )
        self.assertEqual(App._javelin_family(animation), "exl")

    def test_playerpreview_file_identifies_lancer_inside_generic_exo_folder(self) -> None:
        animation = AssetRecord(
            Path("default.sb"), "animation", "animation", ".res", 1,
            internal_path=("animations/antanimations/exo/playerpreview/"
                           "exm_lancer_playerpreview_tmpl_bundlegenbp_bundlegen_win32_antstate.res"),
        )
        self.assertEqual(App._javelin_family(animation), "exm")
        mismatch = AssetRecord(
            Path("default.sb"), "animation", "animation", ".res", 1,
            internal_path=("animations/antanimations/exo/playerpreview/"
                           "exm_colossus_playerpreview_tmpl_bundlegenbp_bundlegen_win32_antstate.res"),
        )
        self.assertIsNone(App._javelin_family(mismatch))

    def test_infer_character_family_on_a_creature_mesh_path(self) -> None:
        app = _make_app()
        try:
            mesh = AssetRecord(
                Path("default.sb"), "mesh", "mesh", ".meshset", 1,
                internal_path="enemies/creatures/ara_worker/ara_worker_model_mesh.meshset",
            )
            family = app._infer_character_family(mesh.internal_path)
            self.assertEqual(family, "ara")
        finally:
            app.destroy()


def _make_app():
    try:
        app = App()
    except Exception as error:
        # Tk requires a display server. Decoder and state-free GUI helpers
        # still run in headless CI; widget-state tests run on Windows/Xvfb.
        if error.__class__.__name__ == "TclError" and "display" in str(error).lower():
            raise unittest.SkipTest("Tk display is unavailable") from error
        raise
    app.withdraw()
    return app


def _dummy_mesh():
    from game_asset_explorer.geometry import MeshData
    return MeshData(name="dummy", vertices=[(0, 0, 0), (1, 0, 0), (0, 1, 0)], faces=[(0, 1, 2)])


def _dummy_skeleton():
    from game_asset_explorer.geometry import SkeletonData
    return SkeletonData("dummy", [("Root", -1, 0.0, 0.0, 0.0), ("Child", 0, 0.0, 1.0, 0.0)])


def _dummy_quaternion_channel():
    from game_asset_explorer.frostbite_animation_channels import QuaternionChannel
    return QuaternionChannel(0, 2, (0, 100), ((0, 0, 0), (500, -500, 200)))


class AnimationPlaybackWiringTests(unittest.TestCase):
    """These exercise the GUI methods directly (no display interaction needed
    beyond a withdrawn Tk root) because none of them are covered by decoding
    a real file -- they're state-machine logic that a bad edit could silently
    break without any decoder-level test catching it."""

    def setUp(self) -> None:
        self.app = _make_app()
        self.asset = AssetRecord(
            Path("default.sb"), "animation", "animation", ".res", 1,
            internal_path="animations/antanimations/exm/ast_exm_test.res",
        )
        self.channels = [_dummy_quaternion_channel()]
        self.mapping = [1]
        self.playback_info = {
            "record_kind": "res", "bytes": 100, "decoded_clip_count": 1,
            "animation_channel_count": 1, "quaternion_channel_count": 1,
            "_playback_channels": self.channels, "_playback_mapping": self.mapping,
        }

    def tearDown(self) -> None:
        self.app.destroy()

    def test_play_button_enables_only_when_character_and_channels_and_family_all_agree(self) -> None:
        self.app._active_character = None
        self.app._show_frostbite_animation_record(self.asset, "t", self.playback_info)
        self.assertEqual(str(self.app.animation_play_button["state"]), "disabled")

        self.app._active_character = {
            "meshes": [_dummy_mesh()], "skeleton": _dummy_skeleton(), "name": "Char", "family": "exm",
        }
        self.app._show_frostbite_animation_record(self.asset, "t", self.playback_info)
        self.assertEqual(str(self.app.animation_play_button["state"]), "normal")
        self.assertIsNotNone(self.app._active_animation)

    def test_play_button_stays_disabled_on_family_mismatch_even_with_decoded_channels(self) -> None:
        # Regression test: a prior version enabled Play whenever the info
        # dict carried decoded channels, without checking family
        # compatibility at all.
        self.app._active_character = {
            "meshes": [_dummy_mesh()], "skeleton": _dummy_skeleton(), "name": "Char", "family": "exh",
        }
        self.app._show_frostbite_animation_record(self.asset, "t", self.playback_info)
        self.assertEqual(str(self.app.animation_play_button["state"]), "disabled")
        self.assertIsNone(self.app._active_animation)

    def test_decoded_curves_without_exact_dof_mapping_do_not_enable_playback(self) -> None:
        self.app._active_character = {
            "meshes": [_dummy_mesh()], "skeleton": _dummy_skeleton(), "name": "Char", "family": "exm",
        }
        info = {
            "record_kind": "res", "bytes": 100, "decoded_clip_count": 1,
            "animation_channel_count": 120, "quaternion_channel_count": 83,
            "dof_ids_decoded": True, "needs_rig_bank": True,
        }
        self.app._show_frostbite_animation_record(self.asset, "t", info)
        self.assertEqual(str(self.app.animation_play_button["state"]), "disabled")
        self.assertIsNone(self.app._active_animation)
        self.assertIn("Rigamate", self.app.animation_state_var.get())

    def test_play_then_stop_does_not_raise_and_resets_button_states(self) -> None:
        self.app._active_character = {
            "meshes": [_dummy_mesh()], "skeleton": _dummy_skeleton(), "name": "Char", "family": "exm",
        }
        self.app._active_animation = {"channels": self.channels, "mapping": self.mapping, "name": "clip"}

        self.app._play_active_animation()
        self.assertEqual(str(self.app.animation_stop_button["state"]), "normal")
        self.assertEqual(str(self.app.animation_play_button["state"]), "disabled")

        for _ in range(3):
            self.app._animation_tick()

        self.app._stop_active_animation()
        self.assertEqual(str(self.app.animation_stop_button["state"]), "disabled")
        self.assertEqual(str(self.app.animation_play_button["state"]), "normal")
        self.assertIsNone(self.app._animation_after_id)

    def test_play_without_active_animation_does_nothing_harmful(self) -> None:
        self.app._active_character = None
        self.app._active_animation = None
        self.app._play_active_animation()  # should not raise
        self.assertEqual(str(self.app.animation_stop_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
