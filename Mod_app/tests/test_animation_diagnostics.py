from __future__ import annotations

import unittest
from types import SimpleNamespace

from game_asset_explorer.animation_diagnostics import (
    infer_missing_w_signs, invalid_component_counts,
)
from game_asset_explorer.frostbite_animation import QuaternionChannel


class AnimationDiagnosticsTests(unittest.TestCase):
    def test_raw_keys_reject_direct_xyz_even_if_w_sign_changes(self):
        clip = SimpleNamespace(quaternion_channels=(QuaternionChannel(
            times=(0, 1, 2),
            values=((0, 0, 0, 1),) * 3,
            packed_words=((32768, 32768, 32768),
                          (65535, 65535, 32768),
                          (32768, 65535, 32768)),
        ),))
        self.assertEqual(invalid_component_counts(clip), (1, 3))

    def test_sign_heuristic_uses_bank_default_without_mutating_values(self):
        values = ((0.6, 0.0, 0.0, 0.8), (0.6, 0.0, 0.0, 0.8))
        result = infer_missing_w_signs(values, (0.6, 0.0, 0.0, -0.8))
        self.assertEqual(result, ((0.6, 0.0, 0.0, -0.8),) * 2)
        self.assertEqual(values[0][3], 0.8)


if __name__ == "__main__":
    unittest.main()
