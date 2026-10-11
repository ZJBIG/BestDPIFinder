"""独立 QA：纯逻辑、低证据和 JSON 边界；仿真数据只写临时目录。"""
from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core


class QADataFactory:
    """生成明确为测试用途的可复用数据，不接触用户历史。"""

    @staticmethod
    def stages(hits=6, misses=0, latency=0.5, tracking=6.0):
        return (
            core.StageRecord(core.STAGES[0], hits, misses, (latency,) * hits),
            core.StageRecord(core.STAGES[1], hits, misses, (latency,) * hits),
            core.StageRecord(core.STAGES[2], tracking_on=tracking),
        )

    @classmethod
    def records(cls, settings=None, stage_sets=None):
        settings = settings or core.Settings(sensitivity=0.4)
        return [
            core.TrialRecord(
                trial.candidate.code, trial.round_number,
                trial.candidate.distance_cm, trial.candidate.sensitivity,
                (stage_sets or {}).get(trial.candidate.code, cls.stages()),
            )
            for trial in core.make_plan(settings, seed=3701)
        ]


class TestQAMeasurement(unittest.TestCase):
    def setUp(self):
        self.settings = core.Settings(sensitivity=0.4)

    def test_documented_valorant_cm360(self):
        self.assertAlmostEqual(core.cm360(800, 0.4, 0.07), 40.82142857142857)

    def test_documented_cs2_conversion(self):
        distance = core.cm360(800, 0.4, 0.07)
        self.assertAlmostEqual(core.sensitivity_for(distance, 800, 0.022), 1.2727272727272727)

    def test_roundtrip_across_realistic_inputs(self):
        for dpi in (50, 400, 800, 1600, 64000):
            for sensitivity in (0.0001, 0.4, 1.25, 100):
                for yaw in (0.022, 0.07):
                    with self.subTest(dpi=dpi, sensitivity=sensitivity, yaw=yaw):
                        actual = core.sensitivity_for(core.cm360(dpi, sensitivity, yaw), dpi, yaw)
                        self.assertTrue(math.isclose(actual, sensitivity, rel_tol=1e-12))

    def test_positive_rejects_invalid_values(self):
        for value in (0, -1, math.nan, math.inf, -math.inf, "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.positive(value, "QA")

    def test_conversion_rejects_each_invalid_argument(self):
        for function in (core.cm360, core.sensitivity_for):
            for index in range(3):
                for value in (0, -1, math.nan, math.inf, -math.inf):
                    args = [800.0, 0.4, 0.07]
                    args[index] = value
                    with self.subTest(function=function.__name__, args=args):
                        with self.assertRaises(ValueError):
                            function(*args)

    def test_conversion_rejects_overflow_and_underflow(self):
        for args in ((1e308, 1e308, 0.07), (1e-300, 1e-300, 0.07)):
            for function in (core.cm360, core.sensitivity_for):
                with self.subTest(function=function.__name__, args=args):
                    with self.assertRaises(ValueError):
                        function(*args)

    def test_raw_angle_is_signed_count_mapping_without_dpi(self):
        self.assertAlmostEqual(core.raw_angle(100, 0.4, 0.07), 2.8)
        self.assertAlmostEqual(core.raw_angle(-100, 0.4, 0.07), -2.8)
        self.assertEqual(core.raw_angle(0, 0.4, 0.07), 0.0)

    def test_raw_angle_rejects_invalid_sensitivity_and_yaw(self):
        for value in (0, -1, math.nan, math.inf):
            for args in ((10, value, 0.07), (10, 0.4, value)):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    core.raw_angle(*args)

    def test_settings_reject_unsupported_game_and_invalid_yaw(self):
        for settings in (core.Settings(game="Unknown"), core.Settings(yaw=0.022)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate()

    def test_settings_reject_numeric_boundaries(self):
        for field in ("dpi", "sensitivity", "yaw"):
            for value in (0, -1, math.nan, math.inf):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    core.Settings(**{field: value}).validate()
        for settings in (core.Settings(dpi=49), core.Settings(dpi=64001),
                         core.Settings(sensitivity=0.00009), core.Settings(sensitivity=101)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate()

    def test_cs2_supports_explicit_custom_yaw(self):
        settings = core.Settings(game="Counter-Strike 2", sensitivity=1.0, yaw=0.03)
        self.assertIs(settings.validate(), settings)

    def test_candidate_distance_and_speed_directions(self):
        fast, base, slow = core.candidates(self.settings)
        self.assertEqual([fast.code, base.code, slow.code], ["A", "B", "C"])
        self.assertAlmostEqual(fast.distance_cm / base.distance_cm, 0.8)
        self.assertAlmostEqual(slow.distance_cm / base.distance_cm, 1.25)
        self.assertAlmostEqual(fast.sensitivity / base.sensitivity, 1.25)
        self.assertAlmostEqual(slow.sensitivity / base.sensitivity, 0.8)
        self.assertAlmostEqual(base.sensitivity, self.settings.sensitivity)

    def test_plan_contains_two_distinct_rounds_and_eighteen_stages(self):
        plan = core.make_plan(self.settings, seed=7)
        self.assertEqual(len(plan) * len(core.STAGES), 18)
        for round_number in (1, 2):
            self.assertCountEqual(
                [trial.candidate.code for trial in plan if trial.round_number == round_number],
                ["A", "B", "C"],
            )

    def test_plan_seed_reproducibility_and_variety(self):
        self.assertEqual(core.make_plan(self.settings, 31), core.make_plan(self.settings, 31))
        orders = {tuple(item.candidate.code for item in core.make_plan(self.settings, seed))
                  for seed in range(20)}
        self.assertGreater(len(orders), 1)

    def test_round_boundary_avoids_immediate_candidate_repeat(self):
        for seed in range(100):
            plan = core.make_plan(self.settings, seed)
            with self.subTest(seed=seed):
                self.assertNotEqual(plan[2].candidate.code, plan[3].candidate.code)


class TestQAScoringAndEvidence(unittest.TestCase):
    def setUp(self):
        self.settings = core.Settings(sensitivity=0.4)

    def test_no_click_and_no_tracking_scores_zero(self):
        for stage in QADataFactory.stages(hits=0, tracking=0):
            self.assertEqual(stage.validate().score, 0)
            self.assertEqual(stage.accuracy, 0)
            self.assertIsNone(stage.median_latency)

    def test_misses_only_do_not_score(self):
        for name in core.STAGES[:2]:
            self.assertEqual(core.StageRecord(name, misses=1000).score, 0)

    def test_more_misses_never_improve_same_hit_performance(self):
        accurate = core.StageRecord(core.STAGES[0], 6, 0, (0.5,) * 6)
        inaccurate = core.StageRecord(core.STAGES[0], 6, 100, (0.5,) * 6)
        self.assertGreater(accurate.score, inaccurate.score)

    def test_rapid_low_accuracy_clicking_cannot_beat_careful_hits(self):
        random_clicking = core.StageRecord(core.STAGES[0], 6, 100, (0.05,) * 6)
        careful = core.StageRecord(core.STAGES[0], 3, 0, (1.0,) * 3)
        self.assertLess(random_clicking.validate().score, careful.validate().score)

    def test_tracking_score_uses_valid_time_fraction(self):
        self.assertEqual(core.StageRecord(core.STAGES[2], tracking_on=2.5).score, 25)

    def test_incomplete_and_invalid_stage_rejected(self):
        stages = [core.StageRecord("unknown"), core.StageRecord(core.STAGES[0], hits=-1),
                  core.StageRecord(core.STAGES[0], misses=-1),
                  core.StageRecord(core.STAGES[0], hits=1, latencies=()),
                  core.StageRecord(core.STAGES[0], valid_seconds=5)]
        for value in (math.nan, math.inf, -1, 11):
            stages.append(core.StageRecord(core.STAGES[2], tracking_on=value))
            stages.append(core.StageRecord(core.STAGES[0], hits=1, latencies=(value,)))
        for stage in stages:
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                stage.validate()

    def test_nonfinite_or_fractional_click_counts_rejected(self):
        for value in (math.nan, math.inf, 1.5, True):
            for field in ("hits", "misses"):
                stage = core.StageRecord(core.STAGES[2], **{field: value})
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    stage.validate()

    def test_trial_requires_three_stages_in_order(self):
        candidate = core.candidates(self.settings)[0]
        for stages in ((), QADataFactory.stages()[:2], QADataFactory.stages()[::-1]):
            with self.subTest(stages=stages), self.assertRaises(ValueError):
                core.TrialRecord(candidate.code, 1, candidate.distance_cm,
                                 candidate.sensitivity, stages).validate()

    def test_empty_summary_has_no_recommendation(self):
        result = core.summarize(self.settings, [])
        self.assertFalse(result["complete"])
        self.assertIsNone(result["leader"])
        self.assertIsNone(result["recommended_range"])

    def test_single_round_does_not_get_final_recommendation(self):
        records = [record for record in QADataFactory.records() if record.round_number == 1]
        result = core.summarize(self.settings, records)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["recommended_range"])
        self.assertIn("不足", result["message"])

    def test_duplicate_round_is_not_complete_evidence(self):
        one_round = [record for record in QADataFactory.records() if record.round_number == 1]
        try:
            result = core.summarize(self.settings, one_round * 2)
        except ValueError:
            return
        self.assertFalse(result["complete"], "重复第一轮不能冒充两轮完整测评")
        self.assertIsNone(result["recommended_range"])

    def test_all_zero_results_refuse_recommendation(self):
        empty = QADataFactory.stages(hits=0, tracking=0)
        result = core.summarize(self.settings, QADataFactory.records(stage_sets=dict.fromkeys("ABC", empty)))
        self.assertTrue(result["complete"])
        self.assertIsNone(result["recommended_range"])
        self.assertIn("不足", result["message"])

    def test_equal_results_preserve_whole_candidate_range(self):
        result = core.summarize(self.settings, QADataFactory.records())
        options = core.candidates(self.settings)
        self.assertEqual(result["recommended_range"], [options[0].distance_cm, options[2].distance_cm])
        self.assertIn("分差", result["message"])

    def test_clear_leader_is_only_simplified_measurement_not_optimum(self):
        stages = {"A": QADataFactory.stages(6, 0, 0.2, 9),
                  "B": QADataFactory.stages(3, 0, 1.0, 3),
                  "C": QADataFactory.stages(2, 0, 1.2, 2)}
        result = core.summarize(self.settings, QADataFactory.records(stage_sets=stages))
        self.assertEqual(result["leader"], "A")
        self.assertIn("再次确认", result["message"])
        self.assertNotIn("最优", result["message"])

    def test_noisy_rounds_preserve_whole_range(self):
        records = QADataFactory.records()
        first = records[0]
        records[0] = core.TrialRecord(first.candidate_code, first.round_number,
                                     first.distance_cm, first.sensitivity,
                                     QADataFactory.stages(hits=0, tracking=0))
        result = core.summarize(self.settings, records)
        options = core.candidates(self.settings)
        self.assertIn("波动", result["message"])
        self.assertEqual(result["recommended_range"], [options[0].distance_cm, options[2].distance_cm])


class TestQAPersistence(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="bestdpi-qa-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "qa-synthetic.json"
        self.settings = core.Settings(sensitivity=0.4)
        self.records = QADataFactory.records()
        self.payload = core.make_payload(self.settings, self.records, "QA仿真；非个人成绩", 2)

    def test_json_roundtrip_keeps_measurements_and_unicode(self):
        core.save_payload(self.path, self.payload)
        settings, records, comfort = core.load_payload(self.path)
        self.assertEqual(settings, self.settings)
        self.assertEqual(records, self.records)
        self.assertEqual(comfort, "QA仿真；非个人成绩")

    def test_payload_has_disclaimer_and_bounded_metadata(self):
        payload = core.make_payload(self.settings, [], "长" * 1100, -2)
        self.assertEqual(len(payload["comfort_note"]), 1000)
        self.assertEqual(payload["aborted_trials"], 0)
        self.assertIn("不是", payload["limitations"])

    def test_import_does_not_trust_stored_summary(self):
        self.payload["summary"] = {"leader": "FAKE", "message": "个人最优"}
        core.save_payload(self.path, self.payload)
        settings, records, _ = core.load_payload(self.path)
        self.assertNotEqual(core.summarize(settings, records)["leader"], "FAKE")

    def test_duplicate_import_record_rejected(self):
        self.payload["records"][1] = self.payload["records"][0]
        core.save_payload(self.path, self.payload)
        with self.assertRaises(ValueError):
            core.load_payload(self.path)

    def test_wrong_candidate_or_schema_rejected(self):
        for mutation in ("schema", "candidate", "count"):
            data = copy.deepcopy(self.payload)
            if mutation == "schema":
                data["schema_version"] = 999
            elif mutation == "candidate":
                data["records"][0]["distance_cm"] *= 2
            else:
                data["records"] *= 2
            core.save_payload(self.path, data)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                core.load_payload(self.path)

    def test_malformed_json_and_structure_rejected(self):
        for text in ("{broken", "null", "[]", '{"schema_version": 1}'):
            self.path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                core.load_payload(self.path)

    def test_oversized_import_rejected(self):
        self.path.write_bytes(b" " * 2_000_001)
        with self.assertRaises(ValueError):
            core.load_payload(self.path)

    def test_nonfinite_save_preserves_old_file_and_removes_temporary(self):
        core.save_payload(self.path, self.payload)
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            core.save_payload(self.path, {"bad": math.nan})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".bestdpi-*.tmp")), [])

    def test_atomic_replace_failure_preserves_old_file(self):
        core.save_payload(self.path, self.payload)
        original = self.path.read_bytes()
        with patch("core.os.replace", side_effect=PermissionError("QA模拟拒绝")):
            with self.assertRaises(PermissionError):
                core.save_payload(self.path, {"new": True})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".bestdpi-*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
