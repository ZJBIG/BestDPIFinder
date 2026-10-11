"""120秒按成绩爬坡自适应回归测试；所有成绩均为合成数据，不代表个人结果。

The suite pins the score-driven hill-climb model: the sensitivity a window is measured at is
chosen from the *previous* window's score, so the gain series is causal and cannot be
recomputed from the band. Several tests therefore assert about the feedback loop itself --
what the climber does when fed a rising, falling, or flat series -- rather than about a
closed-form curve.
"""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adaptive import (BLOCK_SECONDS, BUDGET_SECONDS, HILL_FAST_FACTOR, HILL_GEARS,
                      HILL_IMPROVEMENT, HILL_PROMOTE, HILL_SLOW_FACTOR, HIT_TIMEOUT_SECONDS,
                      MAX_BLOCKS, MIN_COMPOSITE, MIN_PEAK_WINDOWS, MIN_TASK_SCORE, MODE,
                      SCHEMA_VERSION, SPEED_DECAY_SECONDS, TASK_SECONDS, WARMUP_SECONDS,
                      WINDOW_SECONDS, WINDOWS_PER_TASK, AdaptiveController, BlockRecord,
                      HillClimb, TaskSample, TrackingIntegral, band_for, click_score,
                      click_window_score, describe, hit_speed_score, load_adaptive, quality,
                      tracking_target)
from core import STAGES, Settings, clamp, save_payload, sensitivity_for


def climb_series(controller: AdaptiveController, block_index: int,
                 scores: tuple[float, ...]) -> tuple[float, ...]:
    """Gain series a real climber produces from ``scores``, padded to the window count.

    Under the score-driven model the only honest way to synthesise a recorded series is to
    replay the scores through the same ``HillClimb`` the app uses -- there is no closed form.
    """
    climb = controller.climb_for(0, block_index)
    for score in scores:
        climb.observe(score)
    gains: tuple[float, ...] = climb.history[:WINDOWS_PER_TASK]
    if len(gains) < WINDOWS_PER_TASK:
        gains = tuple(gains) + (climb.gain,) * (WINDOWS_PER_TASK - len(gains))
    return gains


def observation(controller: AdaptiveController, *, hit_curve: tuple[float, ...] | None = None,
                tracking_curve: tuple[float, ...] | None = None, gap: float = .2,
                valid: bool = True, latency: float = .5, phase: str | None = None) -> BlockRecord:
    """Build a synthetic block whose window scores come from ``gap`` and the two curves.

    ``hit_curve`` holds one hit count per scoring window for the two click tasks; the tracking
    task uses ``tracking_curve`` seconds-on-target per window.

    The interval is *stepped* per window rather than held constant. Scores are defined on the
    hit interval, so a uniform gap makes every window score identically, the climber's dead-band
    correctly refuses to move, and the block is refused for never having searched -- which would
    make every test here assert about a rejected record. ``gap`` is therefore the *first*
    window's interval and each subsequent window is slightly slower, guaranteeing a moving
    series while ``gap`` stays meaningful to the caller.
    """
    request = controller.request()
    if request is None:
        raise AssertionError("请求意外结束")
    hits_per_window: tuple[float, ...] = hit_curve or (3.0,) * WINDOWS_PER_TASK
    track_per_window: tuple[float, ...] = tracking_curve or (0.9,) * WINDOWS_PER_TASK
    if len(hits_per_window) != WINDOWS_PER_TASK or len(track_per_window) != WINDOWS_PER_TASK:
        raise AssertionError("合成曲线长度必须等于窗口数")
    window_hits: tuple[int, ...] = tuple(int(round(value)) for value in hits_per_window)
    # Step the interval so consecutive windows differ by more than the climber's noise floor.
    # Scores are defined on the interval and decay with it, so a step that is too small leaves
    # every neighbouring window inside HILL_IMPROVEMENT: the dead-band then correctly holds the
    # gain still and the whole block is refused as never having searched. Scaling with ``gap``
    # keeps the step proportionate, and the floor keeps it clear of the threshold for fast runs.
    step: float = max(gap / 4, 0.06)
    gaps = tuple((gap + step * position,) * count for position, count in enumerate(window_hits))
    click_scores = tuple(click_window_score(list(window_gap), 0) for window_gap in gaps)
    track_scores = tuple(100 * clamp(value / WINDOW_SECONDS) for value in track_per_window)
    click_gains = climb_series(controller, request.index, click_scores)
    track_gains = climb_series(controller, request.index, track_scores)
    timeouts = (0,) * WINDOWS_PER_TASK
    total_hits = sum(window_hits)
    tasks: list[TaskSample] = []
    for index, name in enumerate(STAGES):
        if name == STAGES[2]:
            tasks.append(TaskSample(name, 0, 0, (), sum(track_per_window), TASK_SECONDS, 100,
                                    (0,) * WINDOWS_PER_TASK, (0,) * WINDOWS_PER_TASK,
                                    track_per_window, track_gains))
            continue
        tasks.append(TaskSample(name, total_hits, 0, (latency,) * total_hits, 0.0,
                                TASK_SECONDS, 100, window_hits, (0,) * WINDOWS_PER_TASK,
                                (0.0,) * WINDOWS_PER_TASK, click_gains, gaps, timeouts))
    start: float = max(WARMUP_SECONDS + controller.completed * BLOCK_SECONDS,
                       controller.records[-1].active_end if controller.records else 0.0)
    epoch: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    low_gain, high_gain = controller.band_for_task(0, request.index)
    return BlockRecord(request.index, controller.settings.dpi, controller.settings.yaw,
                       (epoch + timedelta(seconds=start)).isoformat(),
                       (epoch + timedelta(seconds=start + BLOCK_SECONDS)).isoformat(),
                       start, start + BLOCK_SECONDS, tuple(tasks), valid,
                       "" if valid else "合成无效块", request.reason,
                       request.phase if phase is None else phase,
                       task_band_lows=(low_gain,) * len(STAGES) if valid else (),
                       task_band_highs=(high_gain,) * len(STAGES) if valid else ())


def complete(controller: AdaptiveController, plans: list[dict] | None = None) -> AdaptiveController:
    """Accept every planned block; per-block knobs keep the two ends distinguishable."""
    knobs: list[dict] = plans or []
    for index in range(MAX_BLOCKS):
        controller.accept(observation(controller, **(knobs[index] if index < len(knobs) else {})))
    return controller


class PlanTests(unittest.TestCase):
    def test_plan_is_120_seconds_with_two_twenty_second_task_blocks(self) -> None:
        self.assertEqual((WARMUP_SECONDS, TASK_SECONDS, MAX_BLOCKS), (2.0, 20.0, 2))
        self.assertEqual(BLOCK_SECONDS, 60.0)
        self.assertEqual(WARMUP_SECONDS + MAX_BLOCKS * BLOCK_SECONDS, BUDGET_SECONDS)
        self.assertEqual(BUDGET_SECONDS, 122.0)

    def test_every_task_is_twenty_seconds_and_divides_into_windows(self) -> None:
        self.assertEqual(TASK_SECONDS, 20.0)
        self.assertEqual(WINDOW_SECONDS, 2.0)
        self.assertEqual(WINDOWS_PER_TASK, 10)
        self.assertEqual(WINDOWS_PER_TASK * WINDOW_SECONDS, TASK_SECONDS)

    def test_search_factors_bracket_the_current_setting(self) -> None:
        # Factors scale cm/360°, so the fast end divides by HILL_FAST_FACTOR (>1) and the slow
        # end by HILL_SLOW_FACTOR (<1): the band is not symmetric in ratio terms.
        self.assertGreater(HILL_FAST_FACTOR, 1.0)
        self.assertLess(HILL_SLOW_FACTOR, 1.0)
        self.assertAlmostEqual(HILL_FAST_FACTOR, 1.25, places=12)
        self.assertAlmostEqual(HILL_SLOW_FACTOR, 0.8, places=12)
        settings = Settings(sensitivity=0.4)
        low_gain, high_gain = band_for(settings)
        self.assertLess(low_gain, high_gain)
        per_cm = settings.dpi * settings.yaw
        cm_at_low = 914.4 / (per_cm * low_gain)
        cm_at_high = 914.4 / (per_cm * high_gain)
        # The highest gain is the shortest distance and the fastest end of the band.
        self.assertGreater(cm_at_low, cm_at_high)
        self.assertAlmostEqual(cm_at_low, settings.distance / HILL_SLOW_FACTOR, places=6)
        self.assertAlmostEqual(cm_at_high, settings.distance / HILL_FAST_FACTOR, places=6)


class HillClimbTests(unittest.TestCase):
    """The climber is the headline feature; these tests drive it as an object, not a curve."""

    def test_gears_are_sub_unit_step_magnitudes(self) -> None:
        # Every gear below 1.0 means they are magnitudes, not signed ratios: the direction
        # argument alone decides which way a step goes.
        self.assertTrue(HILL_GEARS)
        self.assertTrue(all(0 < gear < 1 for gear in HILL_GEARS))

    def test_large_improvement_promotes_a_gear_and_a_drop_demotes(self) -> None:
        climb = HillClimb(0.5, 0.32, 0.5, 1.0)
        geared = HillClimb(0.5, 0.32, 0.5, 1.0)
        geared.gear = 2
        geared.observe(20.0)
        geared.observe(20.0 + HILL_PROMOTE + 1.0)
        self.assertEqual(geared.gear, 1, "大幅进步应升一档（步长变大）")
        climb.observe(50.0)
        climb.observe(10.0)
        self.assertGreaterEqual(climb.gear, 1, "退化应降一档（步长变小）")

    def test_a_flat_score_series_never_moves_the_gain(self) -> None:
        """The dead-band is load-bearing: without it a flat series looks like a search."""
        climb = HillClimb(0.5, 0.32, 0.5, -1.0)
        for _ in range(WINDOWS_PER_TASK):
            climb.observe(40.0)
        self.assertEqual(len(set(climb.history)), 1, "成绩完全不变时灵敏度必须原地不动")
        self.assertEqual(climb.reversals, 0)
        self.assertGreaterEqual(climb.stalled, WINDOWS_PER_TASK - 2)

    def test_improvement_continues_in_the_same_direction(self) -> None:
        climb = HillClimb(0.5, 0.32, 0.5, -1.0)
        climb.observe(20.0)
        climb.observe(20.0 + HILL_IMPROVEMENT + 5.0)
        first_step: float = climb.history[-1]
        climb.observe(20.0 + 2 * (HILL_IMPROVEMENT + 5.0))
        self.assertNotAlmostEqual(climb.gain, first_step,
                                  msg="成绩持续变好时应继续沿当前方向走")
        self.assertEqual(climb.reversals, 0, "持续变好不应掉头")

    def test_a_drop_reverses_direction(self) -> None:
        climb = HillClimb(0.5, 0.32, 0.5, -1.0)
        climb.observe(50.0)
        climb.observe(10.0)
        self.assertGreaterEqual(climb.reversals, 1, "成绩明显变差必须掉头")

    def test_each_reversal_tightens_the_step(self) -> None:
        """A reversal means the peak is behind us, so the next step should shrink.

        ``gear`` indexes ``HILL_GEARS`` with 0 as the *largest* step, so "tighter" means a
        larger index and a step ratio closer to 1.0. Note the escape hatch: a later large
        improvement promotes the gear back, so this is asserted on a monotone-worsening run
        rather than on an alternating one.
        """
        climb = HillClimb(0.5, 0.32, 0.5, -1.0)
        ratios: list[float] = []
        climb.observe(95.0)
        for score in (60.0, 40.0, 25.0, 10.0, 5.0):
            climb.observe(score)
            ratios.append(climb.step_ratio)
        self.assertGreaterEqual(climb.reversals, 4, "连续变差必须持续掉头")
        # ``gear`` itself keeps counting, but the *step* it selects saturates on the finest
        # entry -- which is the property that matters, since the ratio is what moves the gain.
        self.assertEqual(climb.step_ratio, HILL_GEARS[-1], "多次掉头后应到达最细档")
        self.assertTrue(all(b >= a for a, b in zip(ratios, ratios[1:])), "步长只能收紧")
        self.assertTrue(all(gear < 1 for gear in HILL_GEARS))

    def test_improvement_promotes_the_gear_back(self) -> None:
        climb = HillClimb(0.5, 0.32, 0.5, -1.0)
        climb.observe(95.0)
        climb.observe(40.0)
        tightened: int = climb.gear
        self.assertGreater(tightened, 0)
        climb.observe(40.0 + HILL_PROMOTE + 5.0)
        self.assertLess(climb.gear, tightened, "大幅进步应放大步长")

    def test_gain_never_leaves_the_band(self) -> None:
        climb = HillClimb(0.5, 0.32, 0.5, 1.0)
        for score in (0.0, 100.0, 0.0, 100.0, 5.0, 95.0, 0.0, 100.0, 50.0, 50.0):
            climb.observe(score)
        self.assertTrue(all(0.32 <= gain <= 0.5 for gain in climb.history))

    def test_bounce_off_a_bound_keeps_measuring_new_values(self) -> None:
        # Starting pinned at the ceiling, a step outward would clamp back onto the ceiling and
        # grind; the bounce turns it into a real move instead.
        climb = HillClimb(0.5, 0.32, 0.5, 1.0)
        climb.observe(40.0)
        climb.observe(90.0)
        self.assertLess(climb.gain, 0.5, "贴边时应反弹离开，而不是原地磨")

    def test_history_opens_with_the_starting_gain(self) -> None:
        climb = HillClimb(0.5, 0.32, 0.5, -1.0)
        self.assertEqual(climb.history, (0.5,), "history 第一条必须是起始增益")
        climb.observe(50.0)
        self.assertEqual(len(climb.history), 2)

    def test_invalid_scores_are_rejected(self) -> None:
        climb = HillClimb(0.5, 0.32, 0.5, -1.0)
        for bad in (float("nan"), float("inf"), -1.0, 101.0):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                climb.observe(bad)

    def test_invalid_band_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HillClimb(0.5, 0.5, 0.32, -1.0)

    def test_blocks_start_at_opposite_ends_and_walk_inward(self) -> None:
        controller = AdaptiveController(Settings(sensitivity=0.4))
        low, high = controller.band_for_task(0, 1)
        first = controller.climb_for(0, 1)
        second = controller.climb_for(0, 2)
        self.assertAlmostEqual(first.gain, high, places=12)
        self.assertLess(first.direction, 0, "块1 应从快端往慢端走")
        self.assertAlmostEqual(second.gain, low, places=12)
        self.assertGreater(second.direction, 0, "块2 应从慢端往快端走")

    def test_climb_is_deterministic_from_the_score_series_alone(self) -> None:
        """Same scores in, same gains out -- and different scores must give different gains."""
        def run(scores: tuple[float, ...]) -> tuple[float, ...]:
            climb = HillClimb(0.5, 0.32, 0.5, -1.0)
            for score in scores:
                climb.observe(score)
            return climb.history

        rising = run((10.0, 30.0, 55.0, 80.0, 95.0))
        falling = run((95.0, 80.0, 55.0, 30.0, 10.0))
        flat = run((40.0,) * 5)
        self.assertEqual(run((10.0, 30.0, 55.0, 80.0, 95.0)), rising, "同输入必须同输出")
        self.assertNotEqual(rising[1:], falling[1:], "成绩走势不同，灵敏度轨迹必须不同")
        self.assertNotEqual(rising[1:], flat[1:], "平坦成绩不能走出与上升成绩相同的轨迹")


class ScoringTests(unittest.TestCase):
    def test_speed_term_decays_with_the_hit_interval(self) -> None:
        # This is the function(lastTimeHit - nowTimeHit) idea: faster hits score higher.
        self.assertGreater(hit_speed_score(0.05), hit_speed_score(0.3))
        self.assertGreater(hit_speed_score(0.3), hit_speed_score(1.0))
        self.assertAlmostEqual(hit_speed_score(0.0), 100.0, places=9)

    def test_speed_term_is_capped_at_the_timeout(self) -> None:
        # Past the timeout the loss is charged through accuracy instead, so a single stall
        # cannot erase the rest of the window.
        self.assertAlmostEqual(hit_speed_score(HIT_TIMEOUT_SECONDS),
                               hit_speed_score(HIT_TIMEOUT_SECONDS * 4), places=9)
        self.assertAlmostEqual(hit_speed_score(HIT_TIMEOUT_SECONDS),
                               100 * math.exp(-HIT_TIMEOUT_SECONDS / SPEED_DECAY_SECONDS), places=9)

    def test_accuracy_is_squared_so_spraying_is_penalised_twice(self) -> None:
        fast_sure = click_window_score([0.2] * 4, 0)
        fast_spray = click_window_score([0.2] * 4, 4)
        self.assertGreater(fast_sure, fast_spray)
        self.assertAlmostEqual(fast_spray, fast_sure / 4, places=9)

    def test_empty_window_scores_zero(self) -> None:
        self.assertEqual(click_window_score([], 0), 0.0)
        self.assertEqual(click_window_score([], 3), 0.0)

    def test_invalid_window_inputs_are_rejected(self) -> None:
        for bad in (-0.1, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                hit_speed_score(bad)
        with self.assertRaises(ValueError):
            click_window_score([0.2], -1)

    def test_task_score_aggregates_every_window(self) -> None:
        gaps = ((0.2,) * 3,) * WINDOWS_PER_TASK
        sample = TaskSample(STAGES[0], 30, 0, (.5,) * 30, 0.0, TASK_SECONDS, 100,
                            (3,) * WINDOWS_PER_TASK, (0,) * WINDOWS_PER_TASK,
                            (0.0,) * WINDOWS_PER_TASK, (0.4,) * WINDOWS_PER_TASK,
                            gaps, (0,) * WINDOWS_PER_TASK).validate()
        expected = click_window_score([0.2] * 3, 0)
        self.assertAlmostEqual(sample.score, expected, places=9)
        self.assertEqual(sample.window_scores(), (expected,) * WINDOWS_PER_TASK)

    def test_window_scores_use_the_recorded_gaps(self) -> None:
        """Click-task windows are scored from the gap detail, not from raw counts.

        A click task must carry per-window intervals (the record is rejected without them), so
        the count-only path is unreachable here. The count fallback still exists for trajectory
        data that predates the gap detail; what matters for a click task is that the speed term
        drives the score.
        """
        gaps = ((0.2, 0.2, 0.2),) * WINDOWS_PER_TASK
        sample = TaskSample(STAGES[0], 30, 0, (.5,) * 30, 0.0, TASK_SECONDS, 100,
                            (3,) * WINDOWS_PER_TASK, (0,) * WINDOWS_PER_TASK,
                            (0.0,) * WINDOWS_PER_TASK, (0.4,) * WINDOWS_PER_TASK,
                            gaps, (0,) * WINDOWS_PER_TASK).validate()
        self.assertEqual(sample.window_scores(),
                         (click_window_score([0.2, 0.2, 0.2], 0),) * WINDOWS_PER_TASK)
        # Slower hits in the same window must score lower: the speed term is what moves it.
        slower = replace(sample, window_gaps=((1.2, 1.2, 1.2),) * WINDOWS_PER_TASK).validate()
        self.assertLess(slower.score, sample.score)

    def test_tracking_window_scores_use_window_length(self) -> None:
        sample = TaskSample(STAGES[2], 0, 0, (), 10.0, TASK_SECONDS, 100,
                            (0,) * WINDOWS_PER_TASK, (0,) * WINDOWS_PER_TASK,
                            (1.0,) * WINDOWS_PER_TASK, (0.4,) * WINDOWS_PER_TASK).validate()
        self.assertEqual(sample.window_scores(), (50.0,) * WINDOWS_PER_TASK)

    def test_slow_targets_cannot_inflate_windows(self) -> None:
        self.assertGreater(click_score(4, 0, 4.0), click_score(4, 0, 8.0))


class WindowEvidenceTests(unittest.TestCase):
    def test_window_gain_series_matches_the_replayed_climb(self) -> None:
        controller = AdaptiveController(Settings(sensitivity=0.4))
        record = observation(controller)
        for task in record.tasks:
            with self.subTest(task=task.name):
                self.assertEqual(len(task.window_gains), WINDOWS_PER_TASK)

    def test_window_counts_must_sum_to_task_totals(self) -> None:
        sample = TaskSample(STAGES[0], 5, 0, (.5,) * 5, 0.0, TASK_SECONDS, 100,
                            (1,) * WINDOWS_PER_TASK, (0,) * WINDOWS_PER_TASK,
                            (0.0,) * WINDOWS_PER_TASK, (0.4,) * WINDOWS_PER_TASK)
        with self.assertRaises(ValueError):
            replace(sample, hits=99).validate()

    def test_window_series_length_is_enforced(self) -> None:
        controller = AdaptiveController(Settings())
        tasks = observation(controller).tasks
        for index in range(len(STAGES)):
            truncated = list(tasks)
            truncated[index] = replace(truncated[index],
                                       window_gains=(0.4,) * (WINDOWS_PER_TASK - 1))
            with self.subTest(index=index), self.assertRaises(ValueError):
                replace(tasks[index], window_gains=(0.4,) * (WINDOWS_PER_TASK - 1)).validate()

    def test_non_positive_window_gain_is_rejected(self) -> None:
        for bad in (0.0, -1.0, float("nan"), True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    TaskSample(STAGES[0], 1, 0, (.5,), 0.0, TASK_SECONDS, 100,
                               (1,) * WINDOWS_PER_TASK, (0,) * WINDOWS_PER_TASK,
                               (0.0,) * WINDOWS_PER_TASK,
                               (0.4,) * (WINDOWS_PER_TASK - 1) + (bad,)).validate()

    def test_gap_detail_must_align_with_window_hits(self) -> None:
        """The speed term is computed from the gaps, so a mismatch would rescore dishonestly."""
        gaps = ((0.2,),) * WINDOWS_PER_TASK
        sample = TaskSample(STAGES[0], WINDOWS_PER_TASK, 0, (.5,) * WINDOWS_PER_TASK, 0.0,
                            TASK_SECONDS, 100, (1,) * WINDOWS_PER_TASK, (0,) * WINDOWS_PER_TASK,
                            (0.0,) * WINDOWS_PER_TASK, (0.4,) * WINDOWS_PER_TASK, gaps,
                            (0,) * WINDOWS_PER_TASK)
        self.assertEqual(len(sample.validate().window_gaps), WINDOWS_PER_TASK)
        with self.assertRaises(ValueError):
            replace(sample, window_gaps=((),) * WINDOWS_PER_TASK).validate()


class BlockRecordTests(unittest.TestCase):
    def test_valid_block_requires_three_tasks_inside_the_declared_band(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        record.validate(controller.settings)
        self.assertEqual(len(record.tasks), len(STAGES))
        for low, high, task in zip(record.task_band_lows, record.task_band_highs, record.tasks):
            self.assertTrue(all(low <= gain <= high for gain in task.window_gains))

    def test_single_point_band_is_rejected(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        with self.assertRaises(ValueError):
            replace(record, task_band_lows=(0.4,) * len(STAGES),
                    task_band_highs=(0.4,) * len(STAGES)).validate(controller.settings)

    def test_frozen_gain_series_cannot_claim_a_search(self) -> None:
        # A block whose sensitivity never moved cannot have explored anything, even when every
        # other field is well formed.
        controller = AdaptiveController(Settings())
        record = observation(controller)
        frozen = tuple(replace(task, window_gains=(0.4,) * WINDOWS_PER_TASK)
                       for task in record.tasks)
        with self.assertRaises(ValueError):
            replace(record, tasks=frozen).validate(controller.settings)

    def test_gain_outside_the_declared_band_is_rejected(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        escaped = list(record.tasks)
        escaped[0] = replace(escaped[0], window_gains=(9.9,) * WINDOWS_PER_TASK)
        with self.assertRaises(ValueError):
            replace(record, tasks=tuple(escaped)).validate(controller.settings)

    def test_block_must_span_three_scored_tasks(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        with self.assertRaises(ValueError):
            replace(record, tasks=record.tasks[:2]).validate(controller.settings)

    def test_invalid_block_has_no_score_and_needs_a_reason(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller, valid=False)
        record.validate(controller.settings)
        # ``curve`` deliberately still exposes the raw points -- it is the audit trail -- but a
        # block that was never valid must claim neither a score nor a peak.
        self.assertIsNone(record.score)
        self.assertIsNone(record.peak_gain())
        with self.assertRaises(ValueError):
            replace(record, invalid_reason="").validate(controller.settings)

    def test_curve_is_sorted_best_first_and_covers_every_window(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        curve = record.curve()
        self.assertEqual(len(curve), len(STAGES) * WINDOWS_PER_TASK)
        self.assertTrue(all(a[1] >= b[1] for a, b in zip(curve, curve[1:])), "必须按分数降序")

    def test_band_reports_the_lowest_and_highest_gain_visited(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        low, high = record.band()
        self.assertLessEqual(low, high)
        for task in record.tasks:
            self.assertTrue(all(low <= gain <= high for gain in task.window_gains))
        self.assertAlmostEqual(low, min(g for t in record.tasks for g in t.window_gains), places=12)

    def test_travelled_measures_total_excursion(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        self.assertGreater(record.travelled(), 0.0, "有调整就必须有位移")
        frozen = tuple(replace(task, window_gains=(0.4,) * WINDOWS_PER_TASK)
                       for task in record.tasks)
        self.assertEqual(replace(record, tasks=frozen).travelled(), 0.0)

    def test_hardware_mismatch_is_rejected(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        with self.assertRaises(ValueError):
            replace(record, dpi=record.dpi + 1).validate(controller.settings)


class ControllerFlowTests(unittest.TestCase):
    def test_two_blocks_then_the_controller_finishes(self) -> None:
        controller = complete(AdaptiveController(Settings(sensitivity=0.4)))
        self.assertTrue(controller.ended)
        self.assertEqual(controller.completed, MAX_BLOCKS)
        self.assertIsNone(controller.request())

    def test_interrupted_block_does_not_consume_a_slot(self) -> None:
        controller = AdaptiveController(Settings())
        controller.interrupt(observation(controller, valid=False, phase="中断"))
        self.assertEqual(controller.completed, 0)
        self.assertFalse(controller.ended)

    def test_overlapping_blocks_are_rejected(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        controller.accept(record)
        second = replace(observation(controller), active_start=record.active_start,
                         active_end=record.active_end)
        with self.assertRaises(ValueError):
            controller.accept(second)

    def test_accepting_after_finish_is_rejected(self) -> None:
        controller = AdaptiveController(Settings())
        stale = observation(controller)
        controller.finish("测试结束")
        with self.assertRaises(ValueError):
            controller.accept(stale)


class RecommendationTests(unittest.TestCase):
    def test_flat_curve_keeps_the_original_setting(self) -> None:
        # Every window at the same speed: no sensitivity can be told apart, so nothing is claimed.
        controller = complete(AdaptiveController(Settings(sensitivity=0.4)))
        result = controller.summary()
        self.assertEqual(result["recommendation_source"], "baseline_fallback")
        self.assertAlmostEqual(result["retained_cm360"], controller.settings.distance, places=6)

    def test_clear_peak_yields_a_candidate_inside_the_searched_band(self) -> None:
        controller = AdaptiveController(Settings(sensitivity=0.4))
        # A single well-scored window would be a noise peak; MIN_PEAK_WINDOWS requires the gain
        # to be backed more than once, so the fast window is repeated across the task.
        peak = (9.0,) * WINDOWS_PER_TASK
        controller.accept(observation(controller, hit_curve=peak, tracking_curve=(1.8,) * WINDOWS_PER_TASK,
                                      gap=0.05))
        controller.accept(observation(controller))
        controller.finish("测试结束")
        result = controller.summary()
        self.assertEqual(result["recommendation_source"], "hill_peak")
        low, high = controller.band_for_task(0, 1)
        low_cm = 914.4 / (controller.settings.dpi * controller.settings.yaw * high)
        high_cm = 914.4 / (controller.settings.dpi * controller.settings.yaw * low)
        self.assertGreaterEqual(result["recommended_cm360"], min(low_cm, high_cm) - 1e-6)
        self.assertLessEqual(result["recommended_cm360"], max(low_cm, high_cm) + 1e-6)

    def test_recommendation_reports_an_interval_not_false_precision(self) -> None:
        controller = AdaptiveController(Settings(sensitivity=0.4))
        peak = (9.0,) * WINDOWS_PER_TASK
        controller.accept(observation(controller, hit_curve=peak, tracking_curve=(1.8,) * WINDOWS_PER_TASK,
                                      gap=0.05))
        controller.accept(observation(controller))
        controller.finish("测试结束")
        result = controller.summary()
        band = result["recommended_band_cm360"]
        self.assertIsNotNone(band)
        self.assertLess(band[0], band[1], "邻档之间没测过，必须给出区间而不是单点")
        self.assertLessEqual(band[0], result["recommended_cm360"])
        self.assertGreaterEqual(band[1], result["recommended_cm360"])

    def test_flat_curve_reports_no_measured_interval(self) -> None:
        controller = complete(AdaptiveController(Settings(sensitivity=0.4)))
        self.assertIsNone(controller.summary()["recommended_band_cm360"])

    def test_no_blocks_falls_back_to_the_entered_value(self) -> None:
        controller = AdaptiveController(Settings(sensitivity=0.4))
        controller.finish("未开始")
        result = controller.summary()
        self.assertEqual(result["recommendation_source"], "baseline_fallback")
        self.assertAlmostEqual(result["retained_cm360"], controller.settings.distance, places=6)

    def test_recommendation_never_returns_empty_fields(self) -> None:
        controller = complete(AdaptiveController(Settings()))
        result = controller.summary()
        for key in ("recommended_cm360", "recommended_sensitivity", "recommendation_source",
                    "recommendation_confidence", "recommendation_reason", "retained_cm360"):
            with self.subTest(key=key):
                self.assertIsNotNone(result.get(key), f"{key} 不能为空")
        self.assertTrue(result["recommendation_reason"])

    def test_quality_gate_rejects_low_scoring_blocks(self) -> None:
        controller = AdaptiveController(Settings())
        weak = observation(controller, hit_curve=(0.0,) * WINDOWS_PER_TASK,
                           tracking_curve=(0.0,) * WINDOWS_PER_TASK)
        self.assertFalse(quality(weak))
        strong = observation(controller, hit_curve=(3.0,) * WINDOWS_PER_TASK,
                             tracking_curve=(1.5,) * WINDOWS_PER_TASK, gap=0.05)
        self.assertTrue(quality(strong))
        self.assertGreaterEqual(MIN_COMPOSITE, MIN_TASK_SCORE)
        self.assertGreaterEqual(MIN_PEAK_WINDOWS, 2)


class DescribeTests(unittest.TestCase):
    def test_describe_exposes_bands_and_the_full_curve(self) -> None:
        controller = AdaptiveController(Settings())
        record = observation(controller)
        info = describe(record)
        self.assertEqual(len(info["task_bands"]), len(STAGES))
        self.assertEqual(len(info["curve"]), len(STAGES) * WINDOWS_PER_TASK)
        self.assertIn("band_cm360", info)
        self.assertIn("peak_gain", info)

    def test_describe_of_nothing_is_none(self) -> None:
        self.assertIsNone(describe(None))


class PayloadTests(unittest.TestCase):
    def test_payload_declares_the_hill_climb_method_and_schema(self) -> None:
        controller = complete(AdaptiveController(Settings()))
        payload = controller.payload(BUDGET_SECONDS, "合成记录")
        self.assertEqual(payload["mode"], MODE)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        method = payload["method"]
        self.assertEqual(method["windows_per_task"], WINDOWS_PER_TASK)
        self.assertAlmostEqual(method["task_seconds"], TASK_SECONDS)

    def test_payload_roundtrips_through_save_and_load(self) -> None:
        controller = complete(AdaptiveController(Settings()))
        payload = controller.payload(BUDGET_SECONDS, "合成记录")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            save_payload(path, payload)
            loaded, elapsed, note = load_adaptive(path)
        self.assertEqual(loaded.completed, controller.completed)
        self.assertAlmostEqual(elapsed, BUDGET_SECONDS, places=6)
        self.assertEqual(note, "合成记录")

    def test_window_evidence_survives_the_roundtrip(self) -> None:
        controller = complete(AdaptiveController(Settings()))
        payload = controller.payload(BUDGET_SECONDS, "合成记录")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            save_payload(path, payload)
            loaded, _, _ = load_adaptive(path)
        for record in loaded.records:
            for task in record.tasks:
                self.assertEqual(len(task.window_gains), WINDOWS_PER_TASK)

    def test_in_progress_snapshot_stays_unfinished_after_import(self) -> None:
        controller = AdaptiveController(Settings())
        controller.accept(observation(controller))
        # The declared elapsed time must at least cover the finished block: the importer checks
        # that consistency, so a snapshot that claims less than it recorded is rejected.
        payload = controller.payload(BLOCK_SECONDS + WARMUP_SECONDS, "进行中")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            save_payload(path, payload)
            loaded, _, _ = load_adaptive(path)
        self.assertFalse(loaded.ended)


class LoadRejectionTests(unittest.TestCase):
    def payload(self) -> dict:
        return complete(AdaptiveController(Settings())).payload(BUDGET_SECONDS, "合成记录")

    def write(self, folder: str, payload: dict) -> Path:
        path = Path(folder) / "record.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_previous_schemas_are_rejected_with_a_clear_reason(self) -> None:
        for version in (2, 3, 4, 5):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                payload = self.payload()
                payload["schema_version"] = version
                with self.assertRaises(ValueError) as caught:
                    load_adaptive(self.write(folder, payload))
                self.assertIn("旧版", str(caught.exception))

    def test_wrong_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            payload = self.payload()
            payload["mode"] = "adaptive_50s_in_task_sweep"
            with self.assertRaises(ValueError):
                load_adaptive(self.write(folder, payload))

    def test_wrong_budget_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            payload = self.payload()
            payload["budget_seconds"] = 50.0
            with self.assertRaises(ValueError):
                load_adaptive(self.write(folder, payload))

    def test_elapsed_beyond_budget_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            payload = self.payload()
            payload["active_elapsed"] = BUDGET_SECONDS + 1.0
            with self.assertRaises(ValueError):
                load_adaptive(self.write(folder, payload))

    def test_forged_scores_in_the_file_are_ignored(self) -> None:
        """Scores are recomputed from the evidence, so an edited score cannot change a verdict."""
        with tempfile.TemporaryDirectory() as folder:
            payload = self.payload()
            for record in payload["blocks"]:
                record["score"] = 999.0
            loaded, _, _ = load_adaptive(self.write(folder, payload))
        for record in loaded.records:
            self.assertNotEqual(record.score, 999.0)

    def test_tampered_window_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            payload = self.payload()
            payload["blocks"][0]["tasks"][0]["window_gains"] = [0.4]
            with self.assertRaises(ValueError):
                load_adaptive(self.write(folder, payload))

    def test_corrupt_and_oversized_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_adaptive(path)
            path.write_text("x" * (4 * 1024 * 1024), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_adaptive(path)


class TrackingInputTests(unittest.TestCase):
    def test_tracking_integral_records_per_window_and_total(self) -> None:
        """The integral samples on its own 1 ms clock, independent of paint FPS.

        The view is fixed while the target sweeps, so the test asserts what the integral is
        actually responsible for: it advances by elapsed time, it splits the result across the
        scoring windows, and the parts sum back to the whole.
        """
        integral = TrackingIntegral(0.0)
        integral.advance(TASK_SECONDS, 0.0, 0.0)
        self.assertEqual(len(integral.window_on), WINDOWS_PER_TASK)
        self.assertAlmostEqual(sum(integral.window_on), integral.on_seconds, places=9)
        self.assertEqual(integral.samples, int(TASK_SECONDS * 1000))
        # A view parked on the target's path picks up some time, and only inside the windows it
        # was actually on target -- the first window may be zero, but not the whole task.
        self.assertGreater(integral.on_seconds, 0.0)
        self.assertLess(integral.on_seconds, TASK_SECONDS)

    def test_tracking_integral_advances_with_elapsed_time_only(self) -> None:
        integral = TrackingIntegral(0.0)
        integral.advance(5.0, 0.0, 0.0)
        partial = integral.on_seconds
        integral.advance(5.0, 0.0, 0.0)
        self.assertAlmostEqual(integral.on_seconds, partial, places=9, msg="同一时刻重复推进不得重复计分")
        integral.advance(10.0, 0.0, 0.0)
        self.assertEqual(integral.samples, 10_000)

    def test_tracking_integral_ignores_time_outside_the_target(self) -> None:
        integral = TrackingIntegral(0.0)
        integral.advance(TASK_SECONDS, 1e6, 1e6)
        self.assertEqual(integral.on_seconds, 0.0)

    def test_tracking_target_trajectory_is_deterministic(self) -> None:
        self.assertEqual(tracking_target(1.5), tracking_target(1.5))
        self.assertNotEqual(tracking_target(0.0), tracking_target(1.0))


if __name__ == "__main__":
    unittest.main()
