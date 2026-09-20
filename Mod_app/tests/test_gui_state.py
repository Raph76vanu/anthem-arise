from __future__ import annotations

from pathlib import Path
import unittest

from game_asset_explorer.gui import App
from game_asset_explorer.models import AssetRecord


class AnimationStateTests(unittest.TestCase):
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
    app = App()
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
            "record_kind": "res", "bytes": 100, "quaternion_channel_count": 1,
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
