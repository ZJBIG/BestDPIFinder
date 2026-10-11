"""Small QA-only checks for seeded human-input behavior; no window or score mutation."""
from __future__ import annotations

import unittest

from autotest_model import HumanInputModel


class HumanInputModelTests(unittest.TestCase):
    def test_same_seed_repeats_behavior_parameters(self):
        a = HumanInputModel(20260928, .35, .3815)
        b = HumanInputModel(20260928, .35, .3815)
        self.assertEqual([a.reaction_ms(.35), a.movement_ms(200, .35), a.first_click_miss(.35)],
                         [b.reaction_ms(.35), b.movement_ms(200, .35), b.first_click_miss(.35)])

    def test_trajectory_is_smooth_and_reaches_endpoint(self):
        values = [HumanInputModel.minimum_jerk(index / 100) for index in range(101)]
        self.assertEqual(values[0], 0)
        self.assertEqual(values[-1], 1)
        self.assertTrue(all(left <= right for left, right in zip(values, values[1:])))

    def test_performance_penalty_increases_outside_preferred_gain(self):
        model = HumanInputModel(12, .35, .3815)
        self.assertLess(model.penalty(.3815), model.penalty(.28))
        self.assertLess(model.penalty(.3815), model.penalty(.4375))


if __name__ == "__main__":
    unittest.main(verbosity=2)
