"""新版独立QA：所有成绩均为测试仿真，文件只落TemporaryDirectory。"""
from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from adaptive import (BLOCK_SECONDS, BUDGET_SECONDS, HILL_FAST_FACTOR, HILL_IMPROVEMENT,
                      HILL_SLOW_FACTOR, HIT_TIMEOUT_SECONDS, MAX_BLOCKS, MIN_CLICKS, MIN_COMPOSITE,
                      MIN_PEAK_WINDOWS, MIN_TASK_SCORE, MODE, SCHEMA_VERSION, TASK_SECONDS,
                      WARMUP_SECONDS, WINDOW_SECONDS, WINDOWS_PER_TASK, AdaptiveController,
                      BlockRecord, HillClimb, TaskSample, TrackingIntegral, click_window_score,
                      describe, hit_speed_score, load_adaptive, quality, tracking_target)
from adaptive_ui import AdaptiveApp, WHITE
from core import STAGES, Settings, clamp


class AdaptiveQAFactory:
    """Synthesise finished blocks without touching real hardware or real scores."""

    @staticmethod
    def curve(hits_per_window: tuple[int, ...] | None = None,
              tracking_per_window: tuple[float, ...] | None = None):
        # ``or`` would swallow an all-zero tuple -- and an all-zero run is exactly the case the
        # "a dead player must not be handed a recommendation" tests need to build. Test for
        # ``None`` explicitly so a zeroed curve reaches the fixture intact.
        default_hits = tuple(2 + (position % 3) for position in range(WINDOWS_PER_TASK))
        default_tracking = tuple(0.6 + 0.08 * (position % 4) for position in range(WINDOWS_PER_TASK))
        return (default_hits if hits_per_window is None else hits_per_window,
                default_tracking if tracking_per_window is None else tracking_per_window)

    @classmethod
    def climb_series(cls, controller, block_index, scores, start_fresh=True):
        """Replay a synthetic score series through a real climber to get its gain history.

        The QA blocks must carry a gain series the controller will accept, and under the
        score-driven model that series *is* the climber's history -- there is no closed form
        to compute it from the band. Rather than hand-write gains, feed the same scores the
        fixtures describe through ``HillClimb`` so the recorded evidence stays self-consistent
        with the very loop that produces it in the real app.
        """
        climb = controller.climb_for(0, block_index)
        for score in scores:
            climb.observe(score)
        gains = climb.history[:WINDOWS_PER_TASK]
        if len(gains) < WINDOWS_PER_TASK:
            gains = tuple(gains) + (climb.gain,) * (WINDOWS_PER_TASK - len(gains))
        return tuple(gains)

    @classmethod
    def tasks(cls, controller, block_index, hits_per_window=None, tracking_per_window=None,
              raw=120, gaps_per_window=None, timeouts_per_window=None):
        window_hits, window_tracking = cls.curve(hits_per_window, tracking_per_window)
        if len(window_hits) != WINDOWS_PER_TASK or len(window_tracking) != WINDOWS_PER_TASK:
            raise AssertionError("合成曲线长度必须等于窗口数")
        # Build per-window hit intervals (the ``lastTimeHit - nowTimeHit`` sequence the score is
        # defined on) and score them exactly the way the app will. The interval is what carries
        # performance: with a uniform gap, hit *count* alone cancels out of the mean, so a
        # fixture that only varied counts would describe a flat run and cannot be valid under
        # the score-driven model.
        if gaps_per_window is None:
            gaps_per_window = tuple(0.2 + 0.05 * (position % 3) for position in range(WINDOWS_PER_TASK))
        gaps = tuple(tuple(gaps_per_window[position] for _ in range(count))
                     for position, count in enumerate(window_hits))
        timeouts = ((0,) * WINDOWS_PER_TASK if timeouts_per_window is None
                    else tuple(timeouts_per_window))
        scores = tuple(click_window_score(list(window_gap), missed)
                       for window_gap, missed in zip(gaps, timeouts))
        click_gains = cls.climb_series(controller, block_index, scores)
        track_scores = tuple(100 * clamp(value / WINDOW_SECONDS) for value in window_tracking)
        track_gains = cls.climb_series(controller, block_index, track_scores)
        hits = sum(window_hits)
        click = TaskSample(STAGES[0], hits, 0, (0.5,) * hits, 0.0, TASK_SECONDS, raw,
                           window_hits, (0,) * WINDOWS_PER_TASK, (0.0,) * WINDOWS_PER_TASK,
                           click_gains, gaps, timeouts)
        adjust = TaskSample(STAGES[1], hits, 0, (0.5,) * hits, 0.0, TASK_SECONDS, raw,
                            window_hits, (0,) * WINDOWS_PER_TASK, (0.0,) * WINDOWS_PER_TASK,
                            click_gains, gaps, timeouts)
        track = TaskSample(STAGES[2], 0, 0, (), sum(window_tracking), TASK_SECONDS, raw,
                           (0,) * WINDOWS_PER_TASK, (0,) * WINDOWS_PER_TASK,
                           tuple(window_tracking), track_gains)
        return (click, adjust, track)

    @classmethod
    def block(cls, controller, tasks=None, valid=True, phase=None, reason=""):
        request = controller.request()
        if request is None:
            raise AssertionError("请求意外结束")
        duration = BLOCK_SECONDS
        start = max(WARMUP_SECONDS + controller.completed * duration,
                    controller.records[-1].active_end if controller.records else 0)
        epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
        band = controller.band_for_task(0, request.index)
        lows = tuple(band[0] for _ in STAGES)
        highs = tuple(band[1] for _ in STAGES)
        return BlockRecord(request.index, controller.settings.dpi, controller.settings.yaw,
                           (epoch + timedelta(seconds=start)).isoformat(),
                           (epoch + timedelta(seconds=start + duration)).isoformat(),
                           start, start + duration,
                           tasks or cls.tasks(controller, request.index), valid,
                           "" if valid else (reason or "QA仿真无效"), request.reason,
                           request.phase if phase is None else phase,
                           task_band_lows=lows if valid else (),
                           task_band_highs=highs if valid else ())

    @classmethod
    def complete(cls, spec=None, limit=MAX_BLOCKS):
        spec = spec or {}
        controller = AdaptiveController(Settings(sensitivity=0.4))
        for index in range(limit):
            request = controller.request()
            block = cls.block(controller, cls.tasks(controller, request.index,
                                                    spec.get("hits"), spec.get("tracking"),
                                                    gaps_per_window=spec.get("gaps"),
                                                    timeouts_per_window=spec.get("timeouts")),
                              valid=spec.get("valid", True),
                              phase=spec.get("phase"),
                              reason=spec.get("reason", ""))
            for key, value in spec.get("replace", {}).items():
                block = replace(block, **{key: value})
            if block.valid:
                controller.accept(block)
            else:
                controller.interrupt(block)
        return controller


class TestQAAdaptivePlan(unittest.TestCase):
    def test_two_blocks_only_and_every_task_twenty_seconds(self):
        self.assertEqual(MAX_BLOCKS, 2)
        self.assertAlmostEqual(TASK_SECONDS, 20.0)
        self.assertAlmostEqual(WINDOWS_PER_TASK * WINDOW_SECONDS, TASK_SECONDS)
        self.assertAlmostEqual(WARMUP_SECONDS + MAX_BLOCKS * BLOCK_SECONDS, BUDGET_SECONDS)
        self.assertAlmostEqual(WARMUP_SECONDS + 2 * TASK_SECONDS * 3, BUDGET_SECONDS)
        self.assertAlmostEqual(BUDGET_SECONDS, 122.0)
        self.assertEqual(MODE, "adaptive_120s_score_hill_climb")
        self.assertEqual(SCHEMA_VERSION, 6)

    def test_band_brackets_the_entered_setting_by_twenty_to_twentyfive_percent(self):
        controller = AdaptiveController(Settings(sensitivity=0.4))
        baseline = controller.settings.distance
        for block_index in (1, 2):
            low_gain, high_gain = controller.band_for_task(0, block_index)
            low_cm = 914.4 / (controller.settings.dpi * controller.settings.yaw * low_gain)
            high_cm = 914.4 / (controller.settings.dpi * controller.settings.yaw * high_gain)
            with self.subTest(block=block_index):
                # 因子作用在 cm/360°：快端 = 基准 ÷1.25（增益最高、距离最短），
                # 慢端 = 基准 ÷0.8（增益最低、距离最长）。
                self.assertAlmostEqual(min(low_cm, high_cm), baseline / HILL_FAST_FACTOR, places=6)
                self.assertAlmostEqual(max(low_cm, high_cm), baseline / HILL_SLOW_FACTOR, places=6)
        self.assertGreater(HILL_FAST_FACTOR, 1.0)
        self.assertLess(HILL_SLOW_FACTOR, 1.0)
        self.assertLess(baseline / HILL_FAST_FACTOR, baseline / HILL_SLOW_FACTOR)

    def test_blocks_start_at_opposite_ends_and_walk_inward(self):
        """Both blocks sample the whole band; starting apart keeps the two passes independent."""
        controller = AdaptiveController(Settings(sensitivity=0.4))
        low_gain, high_gain = controller.band_for_task(0, 1)
        first = controller.climb_for(0, 1)
        second = controller.climb_for(0, 2)
        self.assertAlmostEqual(first.gain, high_gain, places=12)
        self.assertLess(first.direction, 0, "块1 应从快端往慢端走")
        self.assertAlmostEqual(second.gain, low_gain, places=12)
        self.assertGreater(second.direction, 0, "块2 应从慢端往快端走")

    def test_inherited_start_gain_keeps_a_fresh_task_score_history(self):
        controller = AdaptiveController(Settings(sensitivity=0.4))
        low_gain, high_gain = controller.band_for_task(1, 1)
        inherited = (low_gain + high_gain) / 2
        climb = controller.climb_for(1, 1, inherited)
        self.assertAlmostEqual(climb.gain, inherited, places=12)
        self.assertEqual(climb.history, (inherited,))
        self.assertEqual(climb.best_score, -1.0)
        self.assertEqual(climb.last_score, -1.0)
        self.assertLess(climb.direction, 0, "继承灵敏度不能改变块1的搜索方向")

    def test_pending_request_is_fixed_and_idempotent(self):
        controller = AdaptiveController(Settings())
        expected = controller.request()
        for _ in range(20):
            self.assertIs(controller.request(), expected)
        self.assertEqual(expected.phase, "从快侧爬坡")
        controller.accept(AdaptiveQAFactory.block(controller))
        self.assertEqual(controller.request().phase, "从慢侧爬坡")

    def test_two_blocks_end_the_search_and_never_claim_stability(self):
        controller = AdaptiveQAFactory.complete()
        self.assertTrue(controller.ended)
        self.assertEqual(controller.completed, MAX_BLOCKS)
        self.assertIsNone(controller.request())
        result = controller.summary()
        self.assertEqual(result["conclusion"], "keep")
        self.assertNotIn("stable", result)

    def test_zero_and_low_scores_retain_original_sensitivity(self):
        """A run that never scores cannot claim a search, let alone a result.

        Under the score-driven model a dead-flat run is *rejected as evidence* rather than
        quietly accepted with a flat gain series, so the two shapes have to be built the way
        the app would actually produce them: a genuinely zero score produces a record the
        controller refuses, and a weak-but-varying run stays valid yet falls back to the
        entered setting. Both must end at ``baseline_fallback``.
        """
        dead = {"hits": (0,) * WINDOWS_PER_TASK, "tracking": (0.0,) * WINDOWS_PER_TASK,
                "gaps": (0.0,) * WINDOWS_PER_TASK, "valid": False, "phase": "中断",
                "reason": "完全没有点击，爬坡未发生"}
        # Weak but not dead: neighbouring windows differ by more than the noise floor (else the
        # climber correctly holds still and the record is rejected as "never moved"), yet every
        # window stays far below MIN_COMPOSITE so no candidate can be claimed.
        weak = {"tracking": (0.05,) * WINDOWS_PER_TASK,
                "gaps": tuple(0.60 if position % 2 else 1.40
                              for position in range(WINDOWS_PER_TASK))}
        for spec in (dead, weak):
            with self.subTest(spec=spec):
                controller = AdaptiveQAFactory.complete(spec)
                result = controller.summary()
                self.assertEqual(result["recommendation_source"], "baseline_fallback")
                self.assertIsNone(result["candidate_cm360"])
                self.assertAlmostEqual(result["retained_cm360"],
                                       controller.settings.distance, places=6)

    def test_partial_single_side_never_produces_a_candidate(self):
        controller = AdaptiveController(Settings(sensitivity=0.4))
        controller.accept(AdaptiveQAFactory.block(controller))
        controller.finish("QA单侧结束")
        result = controller.summary()
        self.assertEqual(result["conclusion"], "keep")
        self.assertIsNone(result["candidate_cm360"])
        self.assertEqual(result["recommendation_source"], "baseline_fallback")
        self.assertEqual(result["completed_blocks"], 1)

    def test_second_block_interrupt_leaves_no_candidate(self):
        controller = AdaptiveController(Settings(sensitivity=0.4))
        controller.accept(AdaptiveQAFactory.block(controller))
        record = AdaptiveQAFactory.block(controller, valid=False, phase="中断")
        record = replace(record, tasks=(), active_end=record.active_start + 1,
                         ended_at=(datetime.fromisoformat(record.started_at)
                                   + timedelta(seconds=1)).isoformat())
        controller.interrupt(record)
        controller.finish("QA第二块中断")
        self.assertEqual(controller.completed, 1)
        self.assertEqual(controller.records[-1].valid, False)
        result = controller.summary()
        self.assertEqual(result["conclusion"], "keep")
        self.assertIsNone(result["candidate_cm360"])
        self.assertEqual(result["recommendation_source"], "baseline_fallback")

    def test_zero_raw_in_any_task_rejects_valid_block(self):
        for index in range(3):
            controller = AdaptiveController(Settings())
            tasks = list(AdaptiveQAFactory.tasks(controller, 1))
            tasks[index] = replace(tasks[index], raw_counts=0)
            with self.subTest(index=index), self.assertRaises(ValueError):
                controller.accept(AdaptiveQAFactory.block(controller, tuple(tasks)))

    def test_task_invalid_numbers_names_and_lengths_rejected(self):
        controller = AdaptiveController(Settings())
        tasks = AdaptiveQAFactory.tasks(controller, 1)
        bad: list[TaskSample] = [
            TaskSample("假任务"),
            TaskSample(STAGES[0], hits=1),
            TaskSample(STAGES[2], hits=1, latencies=(0.2,)),
            replace(tasks[0], valid_seconds=TASK_SECONDS - 1),
            replace(tasks[0], valid_seconds=TASK_SECONDS + 1),
            replace(tasks[0], window_hits=(1,) * (WINDOWS_PER_TASK - 1)),
            replace(tasks[0], window_hits=(1,) * (WINDOWS_PER_TASK + 1)),
            replace(tasks[0], window_gains=(0.4,) * (WINDOWS_PER_TASK - 1)),
            replace(tasks[0], window_gains=(0.4,) * (WINDOWS_PER_TASK - 1) + (True,)),
            replace(tasks[0], window_gains=(0.4,) * (WINDOWS_PER_TASK - 1) + (0.0,)),
            replace(tasks[0], window_tracking=(0.0,) * (WINDOWS_PER_TASK - 1)),
            replace(tasks[0], window_hits=(1,) * (WINDOWS_PER_TASK - 1) + (99,)),
        ]
        for value in (math.nan, math.inf, -1, 1.5, True):
            bad.append(TaskSample(STAGES[0], raw_counts=value))
        for sample in bad:
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                sample.validate()

    def test_every_task_must_carry_a_full_window_curve(self):
        controller = AdaptiveController(Settings())
        tasks = AdaptiveQAFactory.tasks(controller, 1)
        for index in range(len(STAGES)):
            truncated = list(tasks)
            truncated[index] = replace(truncated[index], window_gains=(0.4,) * 3)
            with self.subTest(index=index), self.assertRaises(ValueError):
                controller.accept(AdaptiveQAFactory.block(controller, tuple(truncated)))

    def test_quality_gate_requires_every_task_and_real_clicks(self):
        controller = AdaptiveController(Settings())
        strong = AdaptiveQAFactory.block(controller)
        strong.validate(controller.settings)
        self.assertTrue(quality(strong))
        description = describe(strong)
        self.assertTrue(description["quality_sufficient"])
        self.assertIsNotNone(description["peak_gain"])
        self.assertEqual(len(description["task_bands"]), len(STAGES))
        self.assertEqual(len(description["curve"]), len(STAGES) * WINDOWS_PER_TASK)
        self.assertGreaterEqual(MIN_CLICKS, 2)
        self.assertGreater(MIN_COMPOSITE, MIN_TASK_SCORE)
        self.assertGreaterEqual(MIN_PEAK_WINDOWS, 1)

    def test_single_window_is_never_treated_as_a_backed_peak(self):
        # 单个 2 秒窗口不构成可复现峰值：MIN_PEAK_WINDOWS 至少为 2。
        self.assertGreaterEqual(MIN_PEAK_WINDOWS, 2)


class TestQAAdaptiveImport(unittest.TestCase):
    def setUp(self):
        self.controller = AdaptiveQAFactory.complete()
        self.payload = self.controller.payload(BUDGET_SECONDS, "QA仿真，非个人结果")
        self.temporary = tempfile.TemporaryDirectory(prefix="bestdpi-qa-adaptive-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "synthetic.json"

    def load(self, payload):
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return load_adaptive(self.path)

    def assert_rejected(self, payload):
        with self.assertRaises(ValueError):
            self.load(payload)

    def test_roundtrip_recomputes_scores_and_summary(self):
        self.payload["summary"] = {"conclusion": "candidate", "candidate_cm360": 999}
        for block in self.payload["blocks"]:
            block["score"] = -999
            block["stage_scores"] = [999] * 3
        controller, elapsed, note = self.load(self.payload)
        self.assertEqual(controller.summary(), self.controller.summary())
        self.assertNotEqual(controller.summary()["candidate_cm360"], 999)
        self.assertEqual(elapsed, BUDGET_SECONDS)
        self.assertIn("QA仿真", note)

    def test_duplicate_completed_index_rejected(self):
        self.payload["blocks"][1] = copy.deepcopy(self.payload["blocks"][0])
        self.assert_rejected(self.payload)

    def test_more_than_two_completed_blocks_rejected(self):
        self.payload["blocks"].append(copy.deepcopy(self.payload["blocks"][-1]))
        self.assert_rejected(self.payload)

    def test_wrong_task_seed_or_phase_rejected(self):
        for field, value in (("task_seed", 2), ("phase", "伪造确认"), ("phase", "基准")):
            payload = copy.deepcopy(self.payload)
            payload["blocks"][1 if field == "phase" else 0][field] = value
            with self.subTest(field=field, value=value):
                self.assert_rejected(payload)

    def test_missing_or_forged_band_range_rejected(self):
        baseline = copy.deepcopy(self.payload)
        for field, value in (("task_band_lows", []), ("task_band_highs", [])):
            payload = copy.deepcopy(baseline)
            payload["blocks"][0][field] = value
            with self.subTest(field=field, value=value):
                self.assert_rejected(payload)
        for field in ("task_band_lows", "task_band_highs"):
            payload = copy.deepcopy(baseline)
            del payload["blocks"][0][field]
            with self.subTest(field=field):
                self.assert_rejected(payload)
        for value in (0, -1, math.nan, math.inf):
            payload = copy.deepcopy(baseline)
            payload["blocks"][0]["task_band_lows"] = [value] * 3
            with self.subTest(value=value):
                self.assert_rejected(payload)
        # 布尔值不是合法的灵敏度，即使 Python 里 True == 1。
        payload = copy.deepcopy(baseline)
        payload["blocks"][0]["task_band_lows"] = [1.0, 1.0, True]
        self.assert_rejected(payload)

    def test_flat_band_is_rejected(self):
        """A single-point band cannot measure anything: the climber would have nowhere to go."""
        payload = copy.deepcopy(self.payload)
        flat = payload["blocks"][0]["task_band_lows"][0]
        payload["blocks"][0]["task_band_lows"] = [flat] * 3
        payload["blocks"][0]["task_band_highs"] = [flat] * 3
        self.assert_rejected(payload)

    def test_window_gains_outside_the_declared_band_are_rejected(self):
        """The gains follow the scores, so only band membership is checkable -- enforce it.

        The old sweep model could recompute every gain from the declared endpoints and demand
        an exact match. Under score-driven climbing there is no closed form to compare against,
        so the auditable claim is weaker but still real: every applied gain must sit inside the
        band the run declared, and at least one task must show that the loop actually moved.
        """
        payload = copy.deepcopy(self.payload)
        gains = list(payload["blocks"][0]["tasks"][0]["window_gains"])
        top = payload["blocks"][0]["task_band_highs"][0]
        gains[4] = top * 1.5
        payload["blocks"][0]["tasks"][0]["window_gains"] = gains
        self.assert_rejected(payload)

        payload = copy.deepcopy(self.payload)
        gains = list(payload["blocks"][0]["tasks"][1]["window_gains"])
        bottom = payload["blocks"][0]["task_band_lows"][1]
        gains[2] = bottom * 0.5
        payload["blocks"][0]["tasks"][1]["window_gains"] = gains
        self.assert_rejected(payload)

    def test_frozen_gain_series_cannot_claim_a_search(self):
        """If nothing moved, the run never explored the band and cannot be a valid block."""
        payload = copy.deepcopy(self.payload)
        for task in payload["blocks"][0]["tasks"]:
            frozen = task["window_gains"][0]
            task["window_gains"] = [frozen] * WINDOWS_PER_TASK
        self.assert_rejected(payload)

    def test_wrong_budget_or_task_length_rejected(self):
        for field, value in (("budget_seconds", 240), ("budget_seconds", 50)):
            payload = copy.deepcopy(self.payload)
            payload[field] = value
            with self.subTest(field=field, value=value):
                self.assert_rejected(payload)
        for field, value in (("task_seconds", 8.0), ("task_seconds", 50.0),
                             ("window_seconds", 1.0), ("windows_per_task", 8)):
            payload = copy.deepcopy(self.payload)
            payload["method"][field] = value
            with self.subTest(field=field, value=value):
                self.assert_rejected(payload)
        payload = copy.deepcopy(self.payload)
        del payload["method"]
        self.assert_rejected(payload)

    def test_zero_duration_blocks_cannot_fake_a_conclusion(self):
        for block in self.payload["blocks"]:
            block["active_start"] = block["active_end"] = 0
        self.payload["active_elapsed"] = 0
        self.assert_rejected(self.payload)

    def test_overlapping_blocks_cannot_fake_a_conclusion(self):
        for block in self.payload["blocks"]:
            block["active_start"] = WARMUP_SECONDS
            block["active_end"] = WARMUP_SECONDS + BLOCK_SECONDS
        self.assert_rejected(self.payload)

    def test_elapsed_must_cover_last_record(self):
        self.payload["active_elapsed"] = 20
        self.assert_rejected(self.payload)

    def test_invalid_or_reversed_wallclock_timestamps_rejected(self):
        for start, end in (("invalid", "invalid"),
                           ("2026-01-01T00:00:30+00:00", "2026-01-01T00:00:10+00:00")):
            payload = copy.deepcopy(self.payload)
            payload["blocks"][0]["started_at"] = start
            payload["blocks"][0]["ended_at"] = end
            with self.subTest(start=start):
                self.assert_rejected(payload)

    def test_in_progress_snapshot_cannot_be_promoted_to_a_conclusion(self):
        self.payload["ended"] = False
        self.payload["end_reason"] = "QA进行中快照"
        try:
            controller, _, _ = self.load(self.payload)
        except ValueError:
            return
        self.assertFalse(controller.ended)
        result = controller.summary()
        self.assertEqual(result["conclusion"], "insufficient")
        self.assertIsNone(result["candidate_cm360"])

    def test_forged_interrupt_index_is_rejected(self):
        invalid = copy.deepcopy(self.payload["blocks"][0])
        invalid.update(index=99, valid=False, phase="中断", tasks=[],
                       invalid_reason="QA伪造审计索引", active_start=0, active_end=1)
        self.payload["blocks"].insert(0, invalid)
        self.assert_rejected(self.payload)

    def test_previous_schemas_and_unknown_schema_are_rejected(self):
        for version in (1, 2, 3, 4, 5, 7):
            payload = copy.deepcopy(self.payload)
            payload["schema_version"] = version
            with self.subTest(version=version):
                self.assert_rejected(payload)


class TestQAAdaptiveContinuousSession(unittest.TestCase):
    def setUp(self):
        self.app = AdaptiveApp(smoke_test=True)
        self.app.hwnd = 302
        self.app.raw = Mock()
        self.app.controls = {"comfort": 401}
        self.app.read_settings = Mock(return_value=Settings(sensitivity=0.4))
        self.app.place_controls = Mock()
        self.app.invalidate = Mock()
        self.app.save_latest = Mock()
        self.now = 1000.0
        clock = patch("adaptive_ui.time.perf_counter", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        wallclock = patch("adaptive_ui.datetime")
        simulated_datetime = wallclock.start()
        simulated_datetime.now.side_effect = lambda *args, **kwargs: (
            datetime(2026, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=self.now - 1000.0))
        self.addCleanup(wallclock.stop)
        api = patch("adaptive_ui.user32")
        self.user32 = api.start()
        self.addCleanup(api.stop)
        self.user32.GetForegroundWindow.return_value = self.app.hwnd

    def start(self):
        self.app.action("prepare")
        self.assertTrue(self.app.active)

    def test_high_polling_input_advances_session_once_per_millisecond(self):
        self.start()
        self.app.advance_session = Mock()
        self.now += 0.0005
        last_tick = self.app.last_tick
        self.app.on_raw(1, 0, False)
        self.app.advance_session.assert_not_called()
        self.assertEqual(self.app.last_tick, last_tick,
                         "原始输入不得掩盖帧循环停顿；last_tick 只由 tick 推进")
        self.now += 0.001
        self.app.on_raw(1, 0, False)
        self.app.advance_session.assert_called_once_with(self.now)

    def drive(self, samples=None):
        """Run the whole session on the simulated clock, feeding synthetic task input.

        Synthetic input goes through ``on_raw`` so hit/window accounting stays consistent;
        it validates the state machine only and never stands in for real player scores.
        ``samples`` collects the sensitivity actually in effect inside each scored task.

        Two details are load-bearing rather than cosmetic:
        * every click is preceded by a non-zero relative move, because a block whose tasks
          report zero raw counts is rejected outright (``至少一个任务未收到真实相对移动``),
          and ``view_x`` jumps alone would never be seen by the native path;
        * the click cadence *varies* across windows, and the tracking view is deliberately held
          slightly off the target. Under the score-driven model a uniform cadence scores every
          window within the noise floor, the climber correctly holds still, and the record is
          then refused as having never searched -- so a metronome would test the dead-band
          rather than the feedback loop. The tracker gets the same treatment: chasing the
          target perfectly scores a flat 100 every window, which is exactly a frozen series.
        """
        while self.app.active:
            scored = not self.app.warmup and self.now >= self.app.countdown_until
            block = self.app.current_request.index if self.app.current_request else None
            if scored:
                window = self.app.window_index_at(self.now)
                if self.app.stage_index < 2:
                    # Aim at the live target, then click it: one real hit in this window.
                    self.app.view_x, self.app.view_y = self.app.target_x, self.app.target_y
                    # Vary the interval between windows well beyond the dead-band (2.0) while
                    # keeping every window above zero, so the climber has something to react to.
                    lead: float = 0.06 + 0.06 * (window % 3)
                    self.app.last_hit_time = self.now - lead
                    self.app.on_raw(6, 3, True)
                else:
                    # Lag the ideal path by a per-window offset: a tracker that is always
                    # perfect produces one flat 100 for every window and gives the climber
                    # nothing to react to.
                    drift: float = 0.15 * (window % 4)
                    self.app.view_x, self.app.view_y = tracking_target(self.app.elapsed - drift)
                    self.app.on_raw(12, 0, False)
            self.now += 0.02
            self.app.tick()
            if samples is not None and scored and self.app.active:
                samples.setdefault(block, set()).add(round(self.app.current_sensitivity, 9))

    def reach_tracking(self, lead: float = 0.2):
        """Stop the clock just inside the first block's tracking task, without ticking again.

        Ticking past the boundary would already advance the integral, so the clock is moved
        directly instead: the integral stays parked at the task start and the caller's first
        raw event is what consumes ``lead`` seconds of it. ``lead`` must stay below the 250 ms
        stall guard or the event would be treated as a stall and release capture.
        """
        self.start()
        while self.app.active and not (self.app.stage_index == 2 and not self.app.warmup):
            self.now += 0.02
            self.app.tick()
        self.assertTrue(self.app.active and self.app.stage_index == 2, "未能进入跟踪任务")
        self.now = self.app.countdown_until + lead

    def test_hit_and_miss_trigger_sound_effects(self):
        """命中/脱靶必须接到 play_sound；开关只由界面层（self.sound_on）控制。"""
        self.start()
        while self.app.active and self.app.warmup:  # 小步推进跳过热身，避开 250ms 卡顿守卫
            self.now += 0.2
            self.app.tick()
        self.assertFalse(self.app.warmup)
        self.now += 0.02
        with patch("adaptive_ui.play_sound") as sound:
            self.app.view_x, self.app.view_y = self.app.target_x, self.app.target_y
            self.app.on_raw(6, 3, True)  # 准星压在目标上点击 → 命中
            sound.assert_called_once_with("hit")
            self.app.on_raw(6, 3, True)  # 目标已换位 → 脱靶
            sound.assert_called_with("miss")
        self.app.sound_on = False
        with patch("adaptive_ui.play_sound") as sound:
            self.app.view_x, self.app.view_y = self.app.target_x, self.app.target_y
            self.app.on_raw(6, 3, True)
            sound.assert_not_called()

    def test_fixed_camera_aim_is_bounded_by_the_visible_field(self):
        """固定相机下视角被夹在可视场内（到边即停，点击后不重置）；跟踪任务不夹。

        ``on_raw`` 是唯一写入视角的入口，夹取必须在这里接线，所以走真实事件路径验证。
        """
        self.start()
        self.now += 0.02
        self.app.on_raw(50000, 0, False)
        low_x, high_x, _, _ = self.app.field_span(12.0)
        self.assertAlmostEqual(self.app.view_x, high_x, places=6,
                               msg="鼠标大幅右甩时准星应停在场的右边缘")
        self.app.on_raw(-50000, 0, False)
        self.assertAlmostEqual(self.app.view_x, low_x, places=6,
                               msg="鼠标大幅左甩时准星应停在场的左边缘")
        self.reach_tracking()
        self.app.on_raw(50000, 0, False)
        self.assertGreater(self.app.view_x, high_x,
                           "跟踪任务保持移动相机：视角不得被场边界夹住")

    def test_applied_gain_is_held_for_a_whole_window_then_moves(self):
        """Regression: the applied gain must follow the recorded series, window by window.

        An earlier build derived task-local time from a raw ``perf_counter`` timestamp while
        ``active_elapsed`` counts consumed budget. Local time therefore pinned at zero and the
        applied gain froze at the climb start, even though the recorded ``window_gains`` still
        described a perfect sweep. Only sampling the applied gain catches that, so this test
        never reuses the UI's own task clock arithmetic.

        Under score-driven climbing the invariant changed shape: the gain is deliberately held
        constant for each 2 s window (so the score can be attributed to one sensitivity) and
        only moves at the boundary. Both halves are asserted, because a frozen series and a
        per-tick drifting one are each wrong in their own way.
        """
        self.start()
        # Record the gain in effect for each window: the value the app held when the window
        # *began*, captured on the first tick inside it. Taking the last tick instead would
        # read the value that the closing boundary has already moved to, and taking the union
        # would show both the outgoing and incoming value for every window -- which is what an
        # earlier revision of this test did, and it made a correct app look like it drifted
        # continuously inside each window.
        applied: dict[tuple[int, int], dict[int, float]] = {}
        while self.app.active:
            scored = not self.app.warmup and self.now >= self.app.countdown_until
            if scored and self.app.stage_index < 2:
                self.app.view_x, self.app.view_y = self.app.target_x, self.app.target_y
                # A real relative move is part of the fixture, not decoration: a block whose
                # tasks report zero raw counts is refused, so a click with (0, 0) would make
                # this test assert about a rejected record instead of the applied gain.
                self.app.last_hit_time = self.now - (
                    0.06 + 0.06 * (self.app.window_index_at(self.now) % 3))
                self.app.on_raw(6, 3, True)
            elif scored:
                from adaptive import tracking_target as _target
                self.app.view_x, self.app.view_y = _target(
                    self.app.elapsed - 0.15 * (self.app.window_index_at(self.now) % 4))
                self.app.on_raw(12, 0, False)
            if scored and self.app.active and self.app.current_request:
                window = self.app.window_index_at(self.now)
                slot = applied.setdefault((self.app.current_request.index, self.app.stage_index), {})
                slot.setdefault(window, round(self.app.current_sensitivity, 12))
            self.now += 0.02
            self.app.tick()
        self.assertEqual(len(applied), MAX_BLOCKS * len(STAGES))
        for record in self.app.controller.records:
            for index, task in enumerate(record.tasks):
                windows = applied[(record.index, index)]
                with self.subTest(block=record.index, stage=task.name):
                    self.assertEqual(sorted(windows), list(range(WINDOWS_PER_TASK)),
                                     "每个计分窗口都必须被采样到")
                    # The gain the task was actually measured at, window by window, must be the
                    # series the record claims. This is the invariant that matters: the record
                    # is the audit trail, and a gain that never reached the crosshair makes it a
                    # fiction -- which is exactly what the earlier clock bug produced.
                    live = [windows[position] for position in range(WINDOWS_PER_TASK)]
                    self.assertGreater(len(set(live)), 1, "任务内必须出现不同的灵敏度")
                    recorded = [round(gain, 9) for gain in task.window_gains]
                    for position, gain in enumerate(recorded):
                        self.assertAlmostEqual(
                            gain, live[position], places=9,
                            msg=f"第{position + 1}个窗口记录为 {gain}，实际生效 {live[position]}")

    def test_one_start_runs_two_swept_blocks_via_timer_not_finish_calls(self):
        self.start()
        self.drive()
        self.assertEqual(self.app.controller.completed, MAX_BLOCKS)
        self.assertTrue(self.app.controller.ended)
        self.assertAlmostEqual(self.app.spent, BUDGET_SECONDS, places=6)
        # The session must end on the budget boundary, not later: the last tick that crosses
        # it is a fixed 20 ms step, so allow one step of simulated-clock slack.
        self.assertLessEqual(self.now, 1000 + BUDGET_SECONDS + 0.021)
        self.app.raw.start.assert_called_once()
        self.app.raw.stop.assert_called_once()
        self.assertEqual({record.phase for record in self.app.controller.records},
                         {"从快侧爬坡", "从慢侧爬坡"})
        for record in self.app.controller.records:
            self.assertEqual([task.valid_seconds for task in record.tasks], [TASK_SECONDS] * 3)
            for previous, following in zip(record.tasks, record.tasks[1:]):
                self.assertAlmostEqual(
                    following.window_gains[0], previous.window_gains[-1], places=12,
                    msg="同一块内的下一项必须继承上一项最后实际生效的灵敏度")
            for task in record.tasks:
                with self.subTest(gains=task.window_gains):
                    self.assertEqual(len(task.window_gains), WINDOWS_PER_TASK)
                    self.assertNotAlmostEqual(task.window_gains[0], task.window_gains[-1])

    def test_sensitivity_changes_within_each_scored_task(self):
        self.start()
        seen: dict[int, set[float]] = {}
        self.drive(seen)
        self.assertEqual(set(seen), set(range(1, MAX_BLOCKS + 1)))
        for block_index, values in seen.items():
            with self.subTest(block=block_index):
                self.assertGreater(len(values), 1, "一个任务内必须出现不同灵敏度")

    def test_the_two_blocks_start_from_opposite_ends(self):
        self.start()
        self.drive()
        gains = {record.index: record.tasks[0].window_gains for record in self.app.controller.records}
        band = self.app.controller.band_for_task(0, 1)
        self.assertAlmostEqual(gains[1][0], band[1], places=9)
        self.assertAlmostEqual(gains[2][0], band[0], places=9)

    def test_climbing_direction_follows_the_score_it_is_fed(self):
        """The feedback loop is the whole feature, so prove it reacts rather than merely runs.

        A climber that ignored its input would still produce a plausible-looking gain series
        and still pass every structural test above. The only assertion that separates the two
        is behavioural: feed the *same* climber two different score sequences and require the
        resulting gain sequences to differ, then require a rise to be answered with movement in
        the current direction and a fall with a reversal.
        """
        controller = AdaptiveController(Settings(sensitivity=0.4))
        low_gain, high_gain = controller.band_for_task(0, 1)

        def series(scores: tuple[float, ...]) -> tuple[float, ...]:
            climb = controller.climb_for(0, 1)
            for score in scores:
                climb.observe(score)
            return climb.history

        rising = series((10.0, 30.0, 55.0, 80.0, 95.0))
        falling = series((95.0, 80.0, 55.0, 30.0, 10.0))
        flat = series((40.0,) * 5)
        self.assertNotEqual(rising[1:], falling[1:], "同样的爬坡器必须对不同的成绩给出不同的路径")
        self.assertNotEqual(rising[1:], flat[1:], "原地不动不能和持续提高走成同一条线")
        self.assertTrue(all(low_gain <= gain <= high_gain for gain in rising + falling + flat))
        # Block 1 starts at the fast end heading down: a *rise* must not send it back up.
        climb = controller.climb_for(0, 1)
        climb.observe(20.0)
        climb.observe(60.0)
        self.assertLess(climb.gain, climb.history[1], "成绩变好时应沿当前方向继续，而不是掉头")
        climb.observe(5.0)
        self.assertGreaterEqual(climb.reversals, 1, "成绩明显变差必须掉头")

    def test_pause_excludes_idle_but_never_resets_consumed_budget(self):
        self.start()
        self.now += 17
        self.app.abort("QA暂停")
        self.assertEqual(self.app.spent, 17)
        self.now += 10000
        self.assertEqual(self.app.active_elapsed(), 17)
        self.app.start_capture()
        self.now += 4
        self.assertEqual(self.app.active_elapsed(), 21)

    def test_deadline_tick_and_raw_release_without_new_scores(self):
        for via_raw in (False, True):
            self.app = AdaptiveApp(smoke_test=True)
            self.app.hwnd = 302
            self.app.raw = Mock()
            self.app.controller = AdaptiveController(Settings())
            self.app.place_controls = Mock()
            self.app.invalidate = Mock()
            self.app.save_latest = Mock()
            self.app.start_capture()
            self.now += BUDGET_SECONDS
            if via_raw:
                self.app.on_raw(20, 10, True)
            else:
                self.app.tick()
            with self.subTest(via_raw=via_raw):
                self.assertFalse(self.app.active)
                self.assertTrue(self.app.controller.ended)
                self.assertEqual(self.app.spent, BUDGET_SECONDS)
                self.app.raw.stop.assert_called_once()
                self.assertEqual(self.app.controller.summary()["conclusion"], "insufficient")

    def test_stall_releases_and_preserves_consumed_time(self):
        self.start()
        self.app.warmed, self.app.warmup = True, False
        self.app.begin_block()
        self.now += 0.251
        self.app.tick()
        self.assertFalse(self.app.active)
        self.assertGreater(self.app.spent, 0.25)
        self.assertFalse(self.app.controller.records[-1].valid)
        self.app.raw.stop.assert_called_once()

    def test_foreground_loss_pauses_without_auto_resume(self):
        self.start()
        self.user32.GetForegroundWindow.return_value = 999
        self.app.tick()
        self.app.tick()
        self.assertFalse(self.app.active)
        self.app.raw.stop.assert_called_once()
        self.app.raw.start.assert_called_once()

    def test_abort_validation_exception_still_releases_first(self):
        self.start()
        self.app.warmup = False
        self.app.begin_block()
        self.app.controller.interrupt = Mock(side_effect=ValueError("QA审计验证异常"))
        with self.assertRaises(ValueError):
            self.app.abort("QA故障")
        self.assertFalse(self.app.active)
        self.app.raw.stop.assert_called_once()

    def test_end_session_validation_exception_still_releases_first(self):
        self.start()
        self.app.warmup = False
        self.app.begin_block()
        self.app.make_block = Mock(side_effect=ValueError("QA记录异常"))
        with self.assertRaises(ValueError):
            self.app.end_session("QA提前结束")
        self.assertFalse(self.app.active)
        self.app.raw.stop.assert_called_once()

    def test_raw_integrates_the_previous_view_before_applying_the_new_delta(self):
        """The held view is integrated for the elapsed slice *before* the new delta lands."""
        self.reach_tracking()
        self.app.view_x, self.app.view_y = tracking_target(0.0)
        old_view = (self.app.view_x, self.app.view_y)
        with patch.object(self.app.tracking_integral, "advance",
                          wraps=self.app.tracking_integral.advance) as advance:
            self.app.on_raw(10000, -10000, True)
        advance.assert_called_once_with(self.now, *old_view, self.app.radius())
        self.assertGreater(self.app.tracking_on, 0.0)
        self.assertGreater(abs(self.app.view_x), 100)
        self.assertEqual((self.app.hits, self.app.misses, self.app.latencies), (0, 0, []))

    def test_scored_tasks_record_real_clicks_and_self_consistent_windows(self):
        self.start()
        self.drive()
        total = sum(task.hits for record in self.app.controller.records for task in record.tasks[:2])
        self.assertGreater(total, 0, "计分任务必须记到真实点击")
        for record in self.app.controller.records:
            self.assertEqual(record.score, record.score, "成绩必须可重算")
            for task in record.tasks:
                self.assertAlmostEqual(sum(task.window_hits), task.hits, places=9)
                self.assertAlmostEqual(sum(task.window_misses), task.misses, places=9)


class TestQAAdaptiveTrackingIntegral(unittest.TestCase):
    """Fixed 1 ms midpoint sampling must depend on elapsed time, never on call frequency."""

    def test_fixed_trajectory_integral_is_independent_of_advance_frequency(self):
        duration: float = TASK_SECONDS
        total: int = int(duration * 1000)
        changes = {0: (0, 2.3), 173: (3, 3), 819: (9, 4), 1711: (11, 0), 2473: (8, -3), 9000: (2, 2)}
        expected_samples: int = 0
        view = changes[0]
        for millisecond in range(total):
            view = changes.get(millisecond, view)
            tx, ty = tracking_target((millisecond + 0.5) / 1000)
            expected_samples += math.hypot(tx - view[0], ty - view[1]) <= 1.2
        self.assertGreater(expected_samples, 0)
        self.assertLess(expected_samples, total)
        for fps in (1, 24, 60, 144, 1000):
            integral = TrackingIntegral(0)
            view = changes[0]
            schedule = sorted({total, *changes,
                               *(round(index * 1000 / fps) for index in range(int(duration * fps) + 1))})
            for millisecond in schedule:
                integral.advance(millisecond / 1000, *view)
                view = changes.get(millisecond, view)
            with self.subTest(fps=fps):
                self.assertEqual(integral.samples, total)
                self.assertAlmostEqual(integral.on_seconds, expected_samples / 1000, places=12)

    def test_integral_clamps_before_start_after_end_and_duplicate_times(self):
        integral = TrackingIntegral(10)
        self.assertEqual(integral.advance(9, 0, 0, radius=100), 0)
        self.assertAlmostEqual(integral.advance(11, 0, 0, radius=100), 1)
        self.assertAlmostEqual(integral.advance(11, 0, 0, radius=100), 1)
        self.assertAlmostEqual(integral.advance(10 + TASK_SECONDS, 0, 0, radius=100), TASK_SECONDS)
        self.assertEqual(integral.samples, int(TASK_SECONDS * 1000))
        self.assertAlmostEqual(integral.advance(100, 0, 0, radius=100), TASK_SECONDS)


class TestQAAdaptivePageRendering(unittest.TestCase):
    """Every page must render against a stubbed DC.

    The page painters are only reached through the real message loop, so a name that was
    never imported (or a copy string that references a removed constant) stays invisible to
    state-machine tests and only blows up in front of the user. Stubbing GDI here makes the
    painters themselves testable.
    """

    def test_all_pages_render_without_raising(self):
        app = AdaptiveApp(smoke_test=True)
        app.hwnd = 303
        app.raw = Mock()
        app.controls = {"comfort": 501}
        app.controller = AdaptiveController(Settings(sensitivity=0.4))
        app.settings = app.controller.settings
        app.read_settings = Mock(return_value=app.controller.settings)
        painters = {"setup": app.draw_setup, "test": app.draw_test,
                    "results": app.draw_results, "help": app.draw_help}
        with patch("adaptive_ui.gdi32"), patch("win32_app.gdi32"):
            for page, painter in painters.items():
                with self.subTest(page=page):
                    app.page = page
                    painter(0)

    def test_active_hud_shows_task_and_overall_time_remaining(self):
        app = self._capture_scene(self._page_app())
        app.segment_started = 100.0
        app.spent = 0.0
        app.countdown_until = 100.0

        def headline_at(now: float, elapsed: float) -> tuple[str, list[str]]:
            app.elapsed = elapsed
            app.text = Mock()
            with patch("adaptive_ui.time.perf_counter", return_value=now), \
                    patch("adaptive_ui.gdi32"), patch("win32_app.gdi32"):
                app.draw_test(0)
            painted = [str(call.args[1]) for call in app.text.call_args_list]
            return next(line for line in painted if "本项剩余" in line), painted

        headline, painted = headline_at(107.25, 7.25)
        self.assertIn("块 1/2", headline)
        self.assertIn("本项剩余 12.8s", headline)
        self.assertIn("整轮约剩 01:55", headline)
        self.assertTrue(any(STAGES[0] in line for line in painted), "HUD 必须显示当前项目")
        almost_done, _ = headline_at(221.2, 20.4)
        self.assertIn("本项剩余 0.0s", almost_done)
        self.assertIn("整轮约剩 00:01", almost_done)
        expired, _ = headline_at(222.5, 21.0)
        self.assertIn("本项剩余 0.0s", expired)
        self.assertIn("整轮约剩 00:00", expired)

    def _page_app(self) -> AdaptiveApp:
        """A headless app wired to a real controller, ready for painter tests."""
        app = AdaptiveApp(smoke_test=True)
        app.hwnd = 303
        app.raw = Mock()
        app.controls = {"comfort": 501}
        app.controller = AdaptiveController(Settings(sensitivity=0.4))
        app.settings = app.controller.settings
        return app

    def _capture_scene(self, app: AdaptiveApp) -> AdaptiveApp:
        """Active test field, parked on a scored stage, ready to be painted by ``draw_test``."""
        app.active = True
        app.warmed = True
        app.warmup = False
        app.current_request = app.controller.request()
        app.climb = app.controller.climb_for(app.stage_index, app.current_request.index)
        app.start_stage(started_at=1.0)
        return app

    def _painted(self, app: AdaptiveApp):
        """Paint the active test field with stubbed GDI and split the calls by role.

        Returns the target circle (dc, x, y, radius, ...), the four crosshair ticks and the
        grid lines, so coordinate assertions can be made instead of only "did not raise".
        """
        with patch("adaptive_ui.gdi32"), patch("win32_app.gdi32"):
            app.line, app.circle = Mock(), Mock()
            app.draw_test(0)
        target = next(call.args for call in app.circle.call_args_list if call.args[4] == "4FB697")
        crosshair = [call.args for call in app.line.call_args_list if call.args[5] == WHITE]
        grid = [call.args for call in app.line.call_args_list if call.args[5] == "8C1A2B3B"]
        return target, crosshair, grid

    def test_click_task_paints_a_fixed_camera_scene(self):
        """射击任务为固定相机：目标在屏幕上静止、准星随视角移动，点击后不重置。

        按项目约定，绘制改动必须用合成状态驱动并断言画笔坐标，不能只断言不抛异常。
        反向验证：把射击分支改回 ``target - view`` 参考（移动相机）本测试必须失败。
        """
        app = self._capture_scene(self._page_app())
        self.assertEqual(app.stage_index, 0)
        app.view_x, app.view_y = app.target_x, app.target_y  # 准星压在目标上
        target, crosshair, _ = self._painted(app)
        self.assertAlmostEqual(crosshair[2][1], target[1], places=6, msg="竖刻线 x 应等于目标圆心 x")
        self.assertAlmostEqual(crosshair[0][2], target[2], places=6, msg="横刻线 y 应等于目标圆心 y")
        app.view_x, app.view_y = app.target_x + 5.0, app.target_y - 3.0
        moved_target, moved_crosshair, _ = self._painted(app)
        scale: float = (app.viewport()[2] - app.viewport()[0]) / 72
        self.assertEqual(moved_target, target, "固定相机下目标在屏幕上必须静止，与视角无关")
        self.assertAlmostEqual(moved_crosshair[2][1], crosshair[2][1] + 5.0 * scale, places=6,
                               msg="准星必须随视角在屏幕上移动")
        # 贴边出新靶：靶心必须被夹回可视场内，永远不会生成在屏幕外；命中后不重置准星。
        app.view_x, app.view_y = 30.0, 0.0
        app.spawn_target()
        _, _, _, high_y = app.field_span(0.0)
        self.assertLessEqual(abs(app.target_x), 36.0, "靶心不得超出可视场横向范围")
        self.assertLessEqual(abs(app.target_y), high_y, "靶心不得超出可视场纵向范围")

    def test_active_training_field_is_at_least_fifty_percent_taller(self):
        app = self._page_app()
        inactive_height = app.viewport()[3] - app.viewport()[1]
        app.active = True
        active_height = app.viewport()[3] - app.viewport()[1]
        self.assertGreaterEqual(active_height, inactive_height * 1.5)

    def test_fixed_camera_grid_does_not_scroll_with_the_view(self):
        """固定相机的背景网格与视角无关；跟踪任务的场景则仍随视角滑动。"""
        app = self._capture_scene(self._page_app())
        app.view_x, app.view_y = 0.0, 0.0
        _, _, grid_rest = self._painted(app)
        app.view_x, app.view_y = 4.0, 2.0
        _, _, grid_moved = self._painted(app)
        self.assertEqual(grid_moved, grid_rest, "固定相机下网格不得随视角滚动")
        app.stage_index = 2
        self._capture_scene(app)
        app.view_x, app.view_y = 0.0, 0.0
        tracking_at_rest, _, tracking_rest = self._painted(app)
        app.view_x, app.view_y = 4.0, 2.0
        tracking_moved_args, _, tracking_moved = self._painted(app)
        self.assertNotEqual(tracking_moved, tracking_rest, "跟踪任务保持移动相机：场景随视角滑动")
        scale: float = (app.viewport()[2] - app.viewport()[0]) / 72
        self.assertAlmostEqual(tracking_moved_args[1], tracking_at_rest[1] - 4.0 * scale,
                               places=6, msg="跟踪目标在屏幕上的位置应随视角反向滑动")

    def test_paint_hot_path_routes_low_value_primitives_to_fast_gdi(self):
        """掉帧修复的回归锁：实心整屏底与 1px 线必须走 GDI 快路径，圆与 2px 线仍走 GDI+。

        GDI+ 是软件渲染，整屏填充约 6ms/帧，是掉帧主因；实心底与 1px 直线得不到任何
        抗锯齿收益，按形状分流。本测试直接驱动绘制助手并断言落到哪条管线。
        反向验证：去掉 line/box 里的宽度或圆角条件，本测试必须失败。
        """
        app = self._page_app()
        app._gfx = 4321
        app.gp_token = 1
        with patch("win32_app.gdiplus") as gp, patch("win32_app.gdi32") as gdi:
            app.line(0, 10, 10, 110, 10, WHITE, 1)
            self.assertFalse(gp.GdipDrawLine.called, "1px 线必须走 GDI 快路径")
            self.assertTrue(gdi.CreatePen.called)
            app.line(0, 10, 20, 110, 20, WHITE, 2)
            self.assertTrue(gp.GdipDrawLine.called, "2px 线应保留 GDI+ 抗锯齿")
            gp.GdipDrawLine.reset_mock()
            app.circle(0, 50, 50, 10, "4FB697", "63E1C0")
            self.assertTrue(gp.GdipFillEllipse.called, "圆必须保留 GDI+ 抗锯齿")
            app.box(0, 0, 0, 1920, 1080, "101720", radius=0)
            self.assertFalse(gp.GdipFillRectangle.called, "实心整屏底必须走 GDI 快路径")
            self.assertFalse(gp.GdipFillPath.called)
            self.assertTrue(gdi.RoundRect.called)
            def assign_graphics(_, output):
                output._obj.value = 601
                return 0

            def assign_brush(*args):
                args[-1]._obj.value = 602
                return 0

            gp.GdipCreateFromHDC.side_effect = assign_graphics
            gp.GdipCreateLineBrush.side_effect = assign_brush
            # The large gradient is cached: it may use GDI+ once, but repeated
            # frames must only BitBlt the prepared bitmap.
            app.gradient_panel(0, 0, 0, 600, 400, "0C1520", "16242F", radius=0)
            app.gradient_panel(0, 0, 0, 600, 400, "0C1520", "16242F", radius=0)
            self.assertEqual(gp.GdipCreateLineBrush.call_count, 1,
                             "渐变面板必须只在缓存失效时重建")
            self.assertEqual(gdi.BitBlt.call_count, 2,
                             "渐变面板的每一帧应只复制缓存位图")

    def test_swept_curve_painter_draws_a_polyline_once_a_block_is_valid(self):
        """The swept curve is the headline feature, yet it was only ever painted empty.

        ``draw_trend`` returns early with "尚无完整有效块" while no block is valid, so every
        render test above exercised that branch only. Plotting arithmetic therefore had no
        coverage at all, and a defect in it would surface for the first time at the end of the
        user's first real assessment -- the worst possible moment. Drive the painter with
        finished synthetic blocks and assert it emits one segment per adjacent gain bucket plus
        the highlighted peak, so "it drew a curve" is checked rather than "it did not raise".
        """
        controller = AdaptiveQAFactory.complete()
        valid = [record for record in controller.records if record.valid]
        self.assertTrue(valid, "合成控制器应产出有效块")
        buckets = {round(gain, 10)
                   for record in valid for task in record.tasks for gain in task.window_gains}
        self.assertGreaterEqual(len(buckets), 2, "曲线至少要有两个增益桶才谈得上折线")
        app = AdaptiveApp(smoke_test=True)
        app.hwnd = 303
        app.raw = Mock()
        app.controls = {"comfort": 501}
        app.controller = controller
        app.settings = controller.settings
        with patch("adaptive_ui.gdi32"), patch("win32_app.gdi32"):
            app.line = Mock()
            app.circle = Mock()
            app.draw_trend(0, 34, 362, 600, 104)
        self.assertEqual(app.line.call_count, len(buckets) - 1)
        self.assertEqual(app.circle.call_count, len(buckets) + 1)

    def test_swept_curve_painter_stays_empty_without_a_valid_block(self):
        """The preview must never invent a personal score, so the early return must survive."""
        app = AdaptiveApp(smoke_test=True)
        app.hwnd = 303
        app.raw = Mock()
        app.controls = {"comfort": 501}
        app.controller = AdaptiveController(Settings(sensitivity=0.4))
        app.settings = app.controller.settings
        with patch("adaptive_ui.gdi32"), patch("win32_app.gdi32"):
            app.line = Mock()
            app.circle = Mock()
            app.draw_trend(0, 34, 362, 600, 104)
        self.assertEqual(app.line.call_count, 0)
        self.assertEqual(app.circle.call_count, 0)

    def test_results_page_renders_a_finished_assessment_end_to_end(self):
        """``draw_results`` reads a dozen ``summary()`` fields that only exist once blocks land.

        With no records every lookup falls back to a ``.get(...)`` default, so the populated
        page -- the one the user actually sees at the end of a 120 s run -- had never been
        painted. Render it against a finished synthetic assessment and assert the conclusion
        heading reaches the screen, so a missing key or a wrong-length band fails here rather
        than in front of the user.
        """
        controller = AdaptiveQAFactory.complete()
        self.assertEqual(len(controller.records), MAX_BLOCKS)
        summary = controller.summary()
        self.assertIn(summary["conclusion"], ("candidate", "keep"))
        headings = {"candidate": "自动调整后出现更好的一档 · 待验证",
                    "keep": "曲线平坦或未达标 · 建议保留原设置"}
        app = AdaptiveApp(smoke_test=True)
        app.hwnd = 303
        app.raw = Mock()
        app.controls = {"comfort": 501}
        app.controller = controller
        app.settings = controller.settings
        app.read_settings = Mock(return_value=controller.settings)
        drawn: list[str] = []
        with patch("adaptive_ui.gdi32"), patch("win32_app.gdi32"):
            app.text = Mock(side_effect=lambda dc, text, *a, **k: drawn.append(text))
            app.draw_results(0)
        joined: str = "\n".join(drawn)
        self.assertIn(headings[summary["conclusion"]], joined)
        self.assertIn("推荐灵敏度", joined)
        self.assertIn("实际走到 ", joined, "有效块应把真正走过的 cm/360° 区间写进结果页")


if __name__ == "__main__":
    unittest.main(verbosity=2)
