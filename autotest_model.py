"""Seeded, deliberately imperfect mouse behaviour used only by QA runs."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class HumanInputModel:
    seed: int
    base_sens: float
    preferred_sens: float

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def penalty(self, current_sens: float) -> float:
        return abs(math.log(max(1e-9, current_sens) / self.preferred_sens))

    def reaction_ms(self, current_sens: float) -> float:
        extra = 150 if self.rng.random() < 0.09 else 0
        return min(650.0, max(180.0, self.rng.gauss(245 + 100 * self.penalty(current_sens), 38))
                   + extra)

    def movement_ms(self, distance_px: float, current_sens: float, micro: bool = False) -> float:
        base = 105 + min(225, distance_px * 0.38) + 42 * self.penalty(current_sens)
        if micro:
            base += 25
        return min(330.0, max(100.0, self.rng.gauss(base, 20)))

    def motor_noise_px(self, current_sens: float) -> float:
        return self.rng.gauss(0.0, 1.1 + 2.2 * self.penalty(current_sens))

    def first_click_miss(self, current_sens: float) -> bool:
        p = min(0.17, 0.055 + 0.10 * self.penalty(current_sens))
        return self.rng.random() < p

    def correction_delay_ms(self) -> float:
        return self.rng.uniform(60.0, 180.0)

    def correction_offset_px(self) -> float:
        return self.rng.uniform(5.0, 40.0)

    def tracking_lag_ms(self, current_sens: float) -> float:
        return min(240.0, max(70.0, self.rng.gauss(112 + 55 * self.penalty(current_sens), 18)))

    def tracking_drop(self) -> bool:
        return self.rng.random() < 0.012

    @staticmethod
    def minimum_jerk(progress: float) -> float:
        t = min(1.0, max(0.0, progress))
        return 10 * t**3 - 15 * t**4 + 6 * t**5
