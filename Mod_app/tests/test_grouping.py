from pathlib import Path
import unittest

from game_asset_explorer.grouping import (
    asset_folder_key, asset_in_physical_scope, asset_in_virtual_scope,
    category_for_asset, folder_rows, items_in_folder, parse_virtual_location,
)
from game_asset_explorer.models import AssetRecord, CharacterBundle


class CategoryForAssetTests(unittest.TestCase):
    def _asset(self, kind: str, extension: str = ".res") -> AssetRecord:
        return AssetRecord(
            path=Path("Anthem/Data/default.sb"),
            relative_path=f"Data/default.sb::some/path/file{extension}",
            kind=kind,
            extension=extension,
            size=100,
            engine="Frostbite",
            directly_viewable=False,
        )

    def test_other_kind_routes_to_its_own_category_not_meshes(self) -> None:
        # Regression guard: references that don't match any known naming
        # pattern (e.g. a likely-texture .res file with no naming hint --
        # confirmed on real Anthem data that textures aren't named
        # helpfully, same as meshes/animations/skeletons before them) used
        # to be silently dropped entirely. Now they get kind="other" and
        # must land in a dedicated category, not get mixed into
        # "Characters / meshes" via the generic fallback.
        asset = self._asset("other")
        self.assertEqual(category_for_asset(asset), "Other / unclassified")

    def test_known_kinds_are_unaffected_by_the_other_routing(self) -> None:
        self.assertEqual(category_for_asset(self._asset("animation")), "Animations")
        self.assertEqual(category_for_asset(self._asset("skeleton")), "Skeletons")
        self.assertEqual(category_for_asset(self._asset("texture")), "Textures")


class FolderRowsTests(unittest.TestCase):
    def _bundle(self, container: Path, internal: str) -> CharacterBundle:
        asset = AssetRecord(
            path=container,
            relative_path=f"Data/default.sb::{internal}",
            kind="mesh",
            extension=".meshset",
            size=100,
            engine="Frostbite",
            directly_viewable=True,
            container_path=container,
            internal_path=internal,
        )
        return CharacterBundle(internal, asset, score=26.0)

    def test_search_selects_first_matching_file_per_virtual_folder(self) -> None:
        container = Path("Anthem/Data/default.sb")
        alpha = self._bundle(container, "exo/ranger/alpha.meshset")
        beta = self._bundle(container, "exo/ranger/beta.meshset")
        storm = self._bundle(container, "exo/storm/storm.meshset")

        rows, counts = folder_rows(
            [alpha, beta, storm], lambda bundle: bundle.mesh,
            lambda bundle: "beta" in bundle.mesh.internal_path, set(),
        )

        self.assertEqual(rows, [beta])
        self.assertEqual(counts[asset_folder_key(beta.mesh)], 2)

    def test_expansion_reveals_category_siblings_which_do_not_match_search(self) -> None:
        container = Path("Anthem/Data/default.sb")
        alpha = self._bundle(container, "exo/ranger/alpha.meshset")
        beta = self._bundle(container, "exo/ranger/beta.meshset")
        storm = self._bundle(container, "exo/storm/storm.meshset")
        expanded = {asset_folder_key(beta.mesh)}

        rows, _ = folder_rows(
            [alpha, beta, storm], lambda bundle: bundle.mesh,
            lambda bundle: "beta" in bundle.mesh.internal_path, expanded,
        )

        self.assertEqual(rows, [alpha, beta])

    def test_folder_only_view_excludes_every_other_folder(self) -> None:
        container = Path("Anthem/Data/default.sb")
        alpha = self._bundle(container, "exo/ranger/alpha.meshset")
        beta = self._bundle(container, "exo/ranger/beta.meshset")
        storm = self._bundle(container, "exo/storm/storm.meshset")

        isolated = items_in_folder(
            [alpha, beta, storm], lambda bundle: bundle.mesh,
            asset_folder_key(alpha.mesh),
        )

        self.assertEqual(isolated, [alpha, beta])

    def test_virtual_asset_can_match_selected_cas_subfolder(self) -> None:
        engine_root = Path("/games/Anthem")
        selected = engine_root / "Data/Win32/installpackage"
        asset = AssetRecord(
            path=engine_root / "Data/Win32/default.sb",
            relative_path="Data/Win32/default.sb::exo/ranger.meshset",
            kind="mesh", extension=".meshset", size=100, engine="Frostbite",
            internal_path="exo/ranger.meshset",
            metadata={
                "cas_path": str(selected / "cas_01.cas"),
                "available_lod_cas_paths": {"0": "Data/Win32/installpackage/cas_02.cas"},
            },
        )

        self.assertTrue(asset_in_physical_scope(asset, selected, engine_root))
        self.assertFalse(asset_in_physical_scope(
            asset, engine_root / "Patch/Win32/other", engine_root,
        ))

    def test_virtual_location_and_scope_use_archive_plus_internal_prefix(self) -> None:
        archive = Path("/games/Anthem/Data/Win32/forttarsis.sb")
        parsed = parse_virtual_location(f"{archive}::exo/exh_colossus")
        self.assertEqual(parsed, (archive, "exo/exh_colossus"))
        colossus = self._bundle(archive, "exo/exh_colossus/base.meshset").mesh
        ranger = self._bundle(archive, "exo/exm_lancer/base.meshset").mesh
        self.assertTrue(asset_in_virtual_scope(colossus, archive, "exo"))
        self.assertTrue(asset_in_virtual_scope(colossus, archive, "exo/exh_colossus"))
        self.assertFalse(asset_in_virtual_scope(ranger, archive, "exo/exh_colossus"))


if __name__ == "__main__":
    unittest.main()
