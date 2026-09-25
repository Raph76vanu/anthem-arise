from __future__ import annotations

import unittest
from types import SimpleNamespace

from game_asset_explorer.gui import App


def asset(path: str, lods: list[int]):
    return SimpleNamespace(
        internal_path=path, relative_path="Patch/" + path, extension=".meshset",
        metadata={"available_lods": lods},
    )


class RelatedLodTests(unittest.TestCase):
    def test_finds_higher_detail_multipart_siblings(self) -> None:
        current = asset("exo/exm_lancer/exm_lancer_base_model_mesh.meshset", [3, 4, 5])
        arms = asset("exo/exm_lancer/exm_lancer_base_arms_model_mesh.meshset", [1, 2, 3])
        variant = asset("exo/exm_lancer/exm_lancer_bitpack_spacemarine_model_mesh.meshset", [0])
        other_rig = asset("exo/exh_colossus/exh_colossus_base_model_mesh.meshset", [0])
        owner = SimpleNamespace(result=SimpleNamespace(
            assets=[current, arms, variant, other_rig],
        ))

        result = App._related_higher_detail_meshsets(owner, current, [3, 4, 5])

        self.assertEqual(result[0], "LOD0: exo/exm_lancer/exm_lancer_bitpack_spacemarine_model_mesh.meshset")
        self.assertIn("LOD1: exo/exm_lancer/exm_lancer_base_arms_model_mesh.meshset", result)
        self.assertFalse(any("colossus" in item for item in result))

    def test_builds_variant_group_from_body_parts_at_common_lods(self) -> None:
        base = asset("exo/exm_lancer/exm_lancer_base_model_mesh.meshset", [3, 4, 5])
        arms = asset("exo/exm_lancer/exm_lancer_base_arms_model_mesh.meshset", [1, 2, 3, 4, 5])
        legs = asset("exo/exm_lancer/exm_lancer_base_legs_model_mesh.meshset", [1, 2, 3, 4, 5])
        torso = asset("exo/exm_lancer/exm_lancer_base_torso_model_mesh.meshset", [1, 2, 3, 4, 5])
        other = asset("exo/exm_lancer/exm_lancer_bitpack_space_arms_model_mesh.meshset", [0, 1])
        owner = SimpleNamespace(result=SimpleNamespace(assets=[base, arms, legs, torso, other]))

        parts, lods, stem = App._multipart_meshset_group(owner, base)

        self.assertEqual([part.internal_path for part in parts], [
            legs.internal_path, torso.internal_path, arms.internal_path,
        ])
        self.assertEqual(lods, [5, 4, 3, 2, 1])
        self.assertEqual(stem, "exm_lancer_base")

    def test_base_without_parts_chooses_complete_highest_detail_variant(self) -> None:
        base = asset("exo/exf_interceptor/exf_interceptor_base_model_mesh.meshset", [3, 4, 5])
        assets = [base]
        for part in ("arms", "legs", "torso", "helm"):
            assets.append(asset(
                f"exo/exf_interceptor/exf_interceptor_bitpack_garroter_{part}_model_mesh.meshset",
                [1, 2, 3, 4, 5],
            ))
        # A nominally related but incomplete set must never win.
        for part in ("arms", "legs", "helm"):
            assets.append(asset(
                f"exo/exf_interceptor/exf_interceptor_bitpack_incomplete_{part}_model_mesh.meshset",
                [0, 1, 2],
            ))
        owner = SimpleNamespace(result=SimpleNamespace(assets=assets))

        parts, lods, stem = App._multipart_meshset_group(owner, base)

        self.assertEqual(stem, "exf_interceptor_bitpack_garroter")
        self.assertEqual(lods, [5, 4, 3, 2, 1])
        self.assertEqual(len(parts), 4)


if __name__ == "__main__":
    unittest.main()
