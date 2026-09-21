from __future__ import annotations

import unittest

from game_asset_explorer.frostbite import _kind_for_reference


class KindForReferenceTests(unittest.TestCase):
    def test_a_texture_inside_an_animations_folder_tree_is_still_a_texture(self) -> None:
        # Regression guard: confirmed on real Anthem data that a character's
        # head texture is bundled inside an "animations/antanimations/..."
        # folder tree alongside its facial animations. The word "animation"
        # matching earlier in that same path was previously winning over the
        # file's own much more specific texture signal ("textures/" as its
        # own folder, "pcatexture" and "diff" in the filename), causing it
        # to be misclassified as an animation and effectively hidden from
        # every texture-related search.
        path = (
            "animations/antanimations/humanoids/hmf_abbyneff/head/textures/"
            "hmf_abbyneff_pcatexture_diffflf_bundlegenbp_bundlegen_win32_antstate.ebx"
        )
        self.assertEqual(_kind_for_reference(path), "texture")

    def test_a_real_animation_clip_is_unaffected_by_the_texture_reorder(self) -> None:
        path = (
            "animations/antanimations/dynamiccontent/actionstations/bwtimelines/exm/"
            "ast_exm_sentinel_idle_1_bundlegenbp_bundlegen_win32_antstate.res"
        )
        self.assertEqual(_kind_for_reference(path), "animation")

    def test_abbreviated_diffuse_naming_is_recognized(self) -> None:
        # Real Anthem naming abbreviates "diffuse" (diffhf, diffflf, diff_hf)
        # rather than spelling it out -- the original "_diffuse" check never
        # matched real data.
        for suffix in ("diffhf", "diffflf", "diff_hf"):
            path = f"animations/antanimations/humanoids/hmf_x/head/textures/hmf_x_pcatexture_{suffix}_win32_antstate.ebx"
            self.assertEqual(_kind_for_reference(path), "texture", msg=suffix)

    def test_mesh_and_texture_paths_remain_correctly_classified(self) -> None:
        self.assertEqual(
            _kind_for_reference("exo/exm_lancer/exm_lancer_base_model_mesh.meshset"), "mesh",
        )
        self.assertEqual(
            _kind_for_reference("enemies/creatures/ara_worker/ara_worker_model_mesh.meshset"), "mesh",
        )

    def test_unmatched_reference_returns_none(self) -> None:
        self.assertIsNone(_kind_for_reference("some/totally/unrelated/config_data.json"))


if __name__ == "__main__":
    unittest.main()
