"""Unit tests for equations, balanced plans, honest scoring and safe storage."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import (STAGES, Settings, StageRecord, TrialRecord, candidates, cm360,
                  load_payload, make_payload, make_plan, raw_angle,
                  save_payload, sensitivity_for, summarize)


def finished(settings: Settings, score_variant: int = 0) -> list[TrialRecord]:
    result: list[TrialRecord] = []
    for trial in make_plan(settings, seed=123):
        latency: float = 0.5 + score_variant * 0.05
        stages: tuple[StageRecord, ...] = (StageRecord(STAGES[0], 8, 1, (latency,) * 8),
                                           StageRecord(STAGES[1], 7, 1, (latency,) * 7),
                                           StageRecord(STAGES[2], tracking_on=7.0))
        candidate = trial.candidate
        result.append(TrialRecord(candidate.code, trial.round_number, candidate.distance_cm,
                                  candidate.sensitivity, stages))
    return result


class EquationTests(unittest.TestCase):
    def test_known_distance(self) -> None:
        self.assertAlmostEqual(cm360(800, 0.35, 0.07), 46.6530612244898)
        self.assertAlmostEqual(cm360(800, 1.0, 0.022), 51.95454545454545)

    def test_round_trip(self) -> None:
        for dpi in (400, 800, 1600, 6400):
            for yaw in (0.07, 0.022, 0.0165):
                for sensitivity in (0.01, 0.35, 1.0, 10.0):
                    distance: float = cm360(dpi, sensitivity, yaw)
                    self.assertAlmostEqual(sensitivity_for(distance, dpi, yaw), sensitivity)

    def test_invalid_equation_inputs(self) -> None:
        for value in (0, -1, math.nan, math.inf):
            for arguments in ((value, 0.35, 0.07), (800, value, 0.07), (800, 0.35, value)):
                with self.assertRaises(ValueError):
                    cm360(*arguments)
            with self.assertRaises(ValueError):
                sensitivity_for(value, 800, 0.07)

    def test_raw_gain_has_no_dpi(self) -> None:
        self.assertAlmostEqual(raw_angle(100, 0.35, 0.07), 2.45)
        self.assertAlmostEqual(raw_angle(-100, 0.35, 0.07), -2.45)
        self.assertEqual(raw_angle(0, 0.35, 0.07), 0)

    def test_settings_validation(self) -> None:
        for settings in (Settings(dpi=0), Settings(dpi=64001), Settings(sensitivity=-1),
                         Settings(yaw=0.08), Settings(game="invalid"), Settings(yaw=math.nan)):
            with self.assertRaises(ValueError):
                settings.validate()
        self.assertEqual(Settings(game="Counter-Strike 2", yaw=0.0165).validate().yaw, 0.0165)


class PlanTests(unittest.TestCase):
    def test_balanced_randomized_rounds(self) -> None:
        settings = Settings()
        plan = make_plan(settings, seed=42)
        self.assertEqual(len(plan), 6)
        for round_number in (1, 2):
            self.assertEqual({trial.candidate.code for trial in plan if trial.round_number == round_number}, {"A", "B", "C"})
        self.assertEqual(plan, make_plan(settings, seed=42))
        self.assertNotEqual(make_plan(settings, seed=42), make_plan(settings, seed=99))

    def test_candidate_physical_distances(self) -> None:
        settings = Settings()
        for candidate, multiplier in zip(candidates(settings), (0.8, 1.0, 1.25)):
            self.assertAlmostEqual(candidate.distance_cm, settings.distance * multiplier)
            self.assertAlmostEqual(cm360(settings.dpi, candidate.sensitivity, settings.yaw), candidate.distance_cm)


class ScoreTests(unittest.TestCase):
    def test_no_clicks_never_scores(self) -> None:
        self.assertEqual(StageRecord(STAGES[0]).score, 0)
        self.assertEqual(StageRecord(STAGES[1]).score, 0)
        self.assertEqual(StageRecord(STAGES[2]).score, 0)

    def test_tracking_is_valid_time_ratio(self) -> None:
        self.assertEqual(StageRecord(STAGES[2], tracking_on=4.0).score, 40)

    def test_faster_accurate_clicks_are_better(self) -> None:
        accurate = StageRecord(STAGES[0], 8, 0, (0.3,) * 8)
        slow = StageRecord(STAGES[0], 8, 0, (1.5,) * 8)
        misses = StageRecord(STAGES[0], 8, 8, (0.3,) * 8)
        self.assertGreater(accurate.score, slow.score)
        self.assertGreater(accurate.score, misses.score)
        self.assertLessEqual(accurate.score, 100)

    def test_empty_and_partial_results_no_recommendation(self) -> None:
        settings = Settings()
        self.assertIsNone(summarize(settings, [])["leader"])
        result = summarize(settings, finished(settings)[:3])
        self.assertFalse(result["complete"])
        self.assertIsNone(result["recommended_range"])
        self.assertIsNotNone(result["leader"])

    def test_close_scores_give_range(self) -> None:
        settings = Settings()
        summary = summarize(settings, finished(settings))
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["recommended_range"], [settings.distance * .8, settings.distance * 1.25])

    def test_zero_performance_no_recommendation(self) -> None:
        settings = Settings()
        records = [TrialRecord(trial.candidate.code, trial.round_number, trial.candidate.distance_cm,
                               trial.candidate.sensitivity, tuple(StageRecord(name) for name in STAGES))
                   for trial in make_plan(settings, 7)]
        self.assertIsNone(summarize(settings, records)["recommended_range"])

    def test_incomplete_trial_invalid(self) -> None:
        with self.assertRaises(ValueError):
            TrialRecord("A", 1, 40, .4, (StageRecord(STAGES[0]),)).validate()
        with self.assertRaises(ValueError):
            StageRecord(STAGES[0], valid_seconds=4).validate()
        with self.assertRaises(ValueError):
            StageRecord(STAGES[2], tracking_on=12).validate()


class PersistenceTests(unittest.TestCase):
    def test_json_round_trip(self) -> None:
        settings = Settings()
        records = finished(settings)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            save_payload(path, make_payload(settings, records, "手腕舒适", 2))
            restored_settings, restored_records, comfort = load_payload(path)
            self.assertEqual(settings, restored_settings)
            self.assertEqual(records, restored_records)
            self.assertEqual(comfort, "手腕舒适")

    def test_corrupted_json_handled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            for body in ("bad json", "{}", "[]", '{"schema_version": 999}'):
                path.write_text(body, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_payload(path)

    def test_duplicate_and_mismatched_records_rejected(self) -> None:
        settings = Settings()
        records = finished(settings)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            payload = make_payload(settings, records[:1])
            payload["records"] = payload["records"] * 2
            save_payload(path, payload)
            with self.assertRaises(ValueError):
                load_payload(path)
            payload = make_payload(settings, records[:1])
            payload["records"][0]["distance_cm"] = 123.0
            save_payload(path, payload)
            with self.assertRaises(ValueError):
                load_payload(path)

    def test_atomic_write_failure_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_text("original", encoding="utf-8")
            with mock.patch("core.os.replace", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    save_payload(path, {"new": True})
            self.assertEqual(path.read_text(encoding="utf-8"), "original")
            self.assertEqual(len(list(Path(directory).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
