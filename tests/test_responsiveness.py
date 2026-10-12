"""Regression checks for slow disks, QA input cadence and frame diagnostics."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from adaptive_ui import AdaptiveApp
import win32_app
from test_qa_apploop import LoopHarness, loop_app


class TestResponsiveness(unittest.TestCase):
    def test_qa_input_advances_on_every_active_frame(self):
        app = AdaptiveApp(smoke_test=True, autotest=True)
        app.active = True
        app.last_tick = 100.0
        app.now = Mock(return_value=100.008)
        app.advance_session = Mock()
        app.invalidate = Mock()
        app.autotest_driver = Mock()
        app.tick()
        app.advance_session.assert_called_once_with(100.008)
        app.autotest_driver.tick.assert_called_once()

    def test_long_stall_is_reported_before_deadline_resync(self):
        app = loop_app(active=True)
        app.perf_diagnostics = win32_app.PerfDiagnostics()
        harness = LoopHarness(app, start=5000.0, peeks=5)
        waits = 0

        def wait(count, handles, timeout, mask, flags):
            nonlocal waits
            waits += 1
            harness.clock['now'] += timeout / 1000 + (0.5 if waits == 1 else 0)
            return 0x102

        harness.api.MsgWaitForMultipleObjectsEx.side_effect = wait
        harness.run()
        self.assertGreaterEqual(max(app.perf_diagnostics.deadline_late_ms), 490)
        self.assertLessEqual(app.present.call_count, 6)

    def test_active_save_does_not_wait_for_a_slow_disk_and_final_save_stays_last(self):
        app = AdaptiveApp()
        app.controller = Mock(records=[object()])
        app.active = True
        app.payload = Mock(side_effect=[{'step': 1}, {'step': 2}])
        entered = threading.Event()
        written = []

        def slow_save(path, payload):
            if payload['step'] == 1:
                entered.set()
                time.sleep(0.3)
            written.append(payload['step'])

        try:
            with patch('adaptive_ui.save_payload', side_effect=slow_save):
                started = time.perf_counter()
                app.save_latest()
                elapsed = time.perf_counter() - started
                self.assertTrue(entered.wait(1))
                app.active = False
                app.save_latest()
            self.assertLess(elapsed, 0.1, 'Disk I/O blocked the aiming thread')
            self.assertEqual(written, [1, 2], 'An older snapshot replaced the final record')
        finally:
            if getattr(app, '_save_pool', None):
                app._save_pool.shutdown(wait=True)

    def test_redrawing_after_idle_does_not_report_idle_time_as_a_stall(self):
        app = loop_app(active=False)
        app.perf_diagnostics = win32_app.PerfDiagnostics()
        harness = LoopHarness(app, start=5000.0, peeks=3)

        def wake(count, handles, timeout, mask, flags):
            harness.clock['now'] += timeout / 1000
            app._dirty = True
            return 0x102

        harness.api.MsgWaitForMultipleObjectsEx.side_effect = wake
        harness.run()
        self.assertEqual(app.perf_diagnostics.deadline_late_ms[0], 0)

    def test_failed_background_save_is_retried_with_the_final_snapshot(self):
        app = AdaptiveApp()
        app.controller = Mock(records=[object()])
        app.active = True
        app.payload = Mock(side_effect=[{'step': 1}, {'step': 2}])
        try:
            with patch('adaptive_ui.save_payload', side_effect=[OSError('disk unavailable'), None]) as save:
                app.save_latest()
                app.active = False
                app.save_latest()
            self.assertEqual(save.call_args_list[-1].args[1], {'step': 2})
            self.assertEqual(save.call_count, 2)
        finally:
            if getattr(app, '_save_pool', None):
                app._save_pool.shutdown(wait=True)

    def test_exception_releases_mouse_before_waiting_for_background_save(self):
        app = AdaptiveApp()
        app.active = True
        app.raw = Mock()
        app._save_pool = Mock()

        def waiting(**kwargs):
            app.raw.stop.assert_called_once()
            self.assertFalse(app.active)

        app._save_pool.shutdown.side_effect = waiting
        with patch('win32_app.DesktopApp.run_loop', side_effect=RuntimeError('render failure')):
            with self.assertRaisesRegex(RuntimeError, 'render failure'):
                app.run_loop(Mock())
        app._save_pool.shutdown.assert_called_once_with(wait=True)
