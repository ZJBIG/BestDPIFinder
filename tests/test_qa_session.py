"""独立 QA：使用受控时钟验证正式测评状态机；不创建原生窗口。"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import core
import win32_app


class TestQASession(unittest.TestCase):
    def test_render_cadence_leaves_input_headroom(self):
        """The benchmarked scene must not schedule a new paint before a 60 Hz frame ends."""
        self.assertEqual(win32_app.FRAME_INTERVAL_MS, 8)

    def setUp(self):
        self.app = win32_app.DesktopApp(smoke_test=True)
        self.app.hwnd = 1234
        self.app.plan = core.make_plan(self.app.settings, 31)
        self.app.raw = Mock()
        self.app.raw.total_counts = 100
        self.app.invalidate = Mock()
        self.app.place_controls = Mock()
        self.app.save_latest = Mock()
        self.api = Mock()
        self.api.GetForegroundWindow.return_value = self.app.hwnd
        self.api.DefWindowProcW.return_value = 4242
        self.api_patch = patch.object(win32_app, "user32", self.api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def test_raw_ignored_when_paused(self):
        self.app.on_raw(100, -100, True)
        self.assertEqual((self.app.view_x, self.app.view_y, self.app.hits), (0, 0, 0))

    def test_raw_movement_uses_sensitivity_yaw_not_dpi(self):
        self.app.active = True
        self.app.current_sensitivity = 0.4
        self.app.on_raw(100, -100, False)
        self.assertAlmostEqual(self.app.view_x, 2.8)
        self.assertAlmostEqual(self.app.view_y, -2.8)

    def test_countdown_click_does_not_count_hit_or_miss(self):
        self.app.active = True
        self.app.countdown_until = 101
        with patch("win32_app.time.perf_counter", return_value=100):
            self.app.on_raw(0, 0, True)
        self.assertEqual((self.app.hits, self.app.misses), (0, 0))

    def test_hits_require_target_and_respawn(self):
        self.app.active = True
        self.app.target_spawn = 99
        self.app.countdown_until = 99
        self.app.spawn_target = Mock()
        with patch("win32_app.time.perf_counter", return_value=100):
            self.app.on_raw(0, 0, True)
        self.assertEqual(self.app.hits, 1)
        self.assertEqual(self.app.latencies, [1])
        self.app.spawn_target.assert_called_once()

    def test_miss_and_tracking_click_are_not_hits(self):
        self.app.active = True
        self.app.target_x = 50
        self.app.countdown_until = 99
        with patch("win32_app.time.perf_counter", return_value=100):
            self.app.on_raw(0, 0, True)
            self.assertEqual((self.app.hits, self.app.misses), (0, 1))
            self.app.stage_index = 2
            self.app.on_raw(0, 0, True)
        self.assertEqual((self.app.hits, self.app.misses), (0, 1))

    def test_candidate_targets_are_fair_for_same_round(self):
        for stage_index in (0, 1, 2):
            sequences = []
            for index, trial in enumerate(self.app.plan):
                if trial.round_number != 1:
                    continue
                self.app.trial_index = index
                self.app.stage_index = stage_index
                self.app.start_stage()
                sequence = [(self.app.target_x, self.app.target_y, self.app.radius())]
                for _ in range(10):
                    self.app.spawn_target(reset_clock=False)
                    sequence.append((self.app.target_x, self.app.target_y, self.app.radius()))
                sequences.append(sequence)
            with self.subTest(stage=stage_index):
                self.assertEqual(sequences[0], sequences[1])
                self.assertEqual(sequences[1], sequences[2])

    def test_warmup_completion_never_records_or_advances(self):
        self.app.active = True
        self.app.warmup = True
        self.app.hits = 10
        self.app.finish_stage()
        self.assertTrue(self.app.warmed)
        self.assertFalse(self.app.active)
        self.assertEqual(self.app.records, [])
        self.assertEqual(self.app.trial_index, 0)
        self.app.save_latest.assert_not_called()
        self.app.raw.stop.assert_called_once()

    def test_abort_discards_all_partial_stages_and_preserves_slot(self):
        self.app.active = True
        self.app.stage_index = 1
        self.app.stage_records = [core.StageRecord(core.STAGES[0])]
        self.app.elapsed = 5
        self.app.abort("QA取消")
        self.app.abort("QA重复取消")
        self.assertEqual(self.app.stage_records, [])
        self.assertEqual(self.app.records, [])
        self.assertEqual(self.app.trial_index, 0)
        self.assertEqual(self.app.elapsed, 0)
        self.assertEqual(self.app.aborted, 1)
        self.app.raw.stop.assert_called_once()
        self.app.save_latest.assert_not_called()

    def test_paused_tick_does_not_advance_time(self):
        self.app.elapsed = 3
        with patch("win32_app.time.perf_counter", return_value=10000):
            self.app.tick()
        self.assertEqual(self.app.elapsed, 3)
        self.assertEqual(self.app.records, [])

    def test_long_frame_aborts_without_scoring(self):
        self.app.active = True
        self.app.last_tick = 100
        with patch("win32_app.time.perf_counter", return_value=100.3):
            self.app.tick()
        self.assertFalse(self.app.active)
        self.assertEqual(self.app.records, [])
        self.assertEqual(self.app.aborted, 1)

    def test_foreground_loss_aborts_on_timer(self):
        self.app.active = True
        self.api.GetForegroundWindow.return_value = 999
        self.app.tick()
        self.assertFalse(self.app.active)
        self.app.raw.stop.assert_called_once()

    def test_countdown_tick_does_not_score(self):
        self.app.active = True
        self.app.countdown_until = 101
        self.app.last_tick = 100
        with patch("win32_app.time.perf_counter", return_value=100.1):
            self.app.tick()
        self.assertEqual(self.app.elapsed, 0)
        self.assertEqual(self.app.tracking_on, 0)

    def test_timer_counts_only_post_countdown_interval(self):
        self.app.active = True
        self.app.countdown_until = 100.05
        self.app.last_tick = 100
        with patch("win32_app.time.perf_counter", return_value=100.1):
            self.app.tick()
        self.assertAlmostEqual(self.app.elapsed, 0.05)

    def test_complete_trial_has_all_three_stages_once(self):
        self.app.active = True
        self.app.warmup = False
        for expected_index in range(3):
            self.assertEqual(self.app.stage_index, expected_index)
            self.app.finish_stage()
        self.assertEqual(len(self.app.records), 1)
        self.assertEqual(tuple(stage.name for stage in self.app.records[0].stages), core.STAGES)
        self.assertEqual(self.app.trial_index, 1)
        self.app.save_latest.assert_called_once()

    def test_no_relative_movement_cannot_be_saved_as_personal_result(self):
        self.app.active = True
        self.app.raw.total_counts = 0
        for _ in range(3):
            self.app.finish_stage()
        self.assertEqual(self.app.records, [])
        self.assertEqual(self.app.trial_index, 0)
        self.assertEqual(self.app.aborted, 1)
        self.app.save_latest.assert_not_called()

    def test_wm_input_always_calls_default_cleanup(self):
        actual = self.app.window_proc(self.app.hwnd, 0x00FF, 0, 77)
        self.app.raw.receive.assert_called_once_with(77)
        self.api.DefWindowProcW.assert_called_once_with(self.app.hwnd, 0x00FF, 0, 77)
        self.assertEqual(actual, 4242)

    def test_escape_activation_focus_and_capture_loss_release(self):
        for message, wparam in ((0x100, 0x1B), (0x1C, 0), (0x06, 0),
                                (0x08, 0), (0x1F, 0), (0x215, 0)):
            self.app.raw.reset_mock()
            self.app.active = True
            with self.subTest(message=message):
                self.app.window_proc(self.app.hwnd, message, wparam, 0)
                self.assertFalse(self.app.active)
                self.app.raw.stop.assert_called_once()

    def test_close_and_destroy_release(self):
        for message in (0x10, 0x02):
            self.app.raw.reset_mock()
            self.app.active = True
            self.app.window_proc(self.app.hwnd, message, 0, 0)
            with self.subTest(message=message):
                self.assertFalse(self.app.active)
                self.app.raw.stop.assert_called_once()

    def test_unexpected_callback_exception_releases_and_closes_smoke(self):
        self.app.active = True
        self.app.raw.receive.side_effect = RuntimeError("QA模拟输入异常")
        self.app.window_proc(self.app.hwnd, 0xFF, 0, 77)
        self.assertFalse(self.app.active)
        self.assertIn("QA模拟输入异常", self.app.fatal_error)
        self.app.raw.stop.assert_called_once()
        self.api.PostMessageW.assert_called_once_with(self.app.hwnd, 0x10, 0, 0)

    def test_smoke_guard_never_writes_history(self):
        del self.app.save_latest
        self.app.records = [Mock()]
        with patch("win32_app.save_payload") as save:
            self.app.save_latest()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
