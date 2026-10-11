"""游戏式消息循环与 GDI 对象缓存的独立 QA；全部无原生窗口，受控时钟。"""
from __future__ import annotations

import unittest
from ctypes import wintypes as wt
from unittest.mock import Mock, patch

import win32_app


class LoopHarness:
    """Drives ``run_loop`` on a fake clock: no window, no real messages.

    ``PeekMessageW`` returns False until a configurable cutoff, then True with the
    pre-seeded message (WM_QUIT), which is the only way the loop stops.  Every
    ``MsgWaitForMultipleObjectsEx`` call advances the fake clock by the requested
    timeout, as a real sleep would.
    """

    def __init__(self, app: win32_app.DesktopApp, start: float, peeks: int,
                 early_wakes: bool = False) -> None:
        self.app = app
        self.clock = {"now": start}
        self.api = Mock()
        self.message = wt.MSG()
        self.message.message = 0x0012  # WM_QUIT；side_effect 只切换返回值
        self.remove_flags: list[int] = []
        state = {"peeks": 0, "waits": 0}
        self.early_wakes = early_wakes

        def peek(pointer, hwnd, lowest, highest, remove):
            state["peeks"] += 1
            self.remove_flags.append(remove)
            return 1 if state["peeks"] > peeks else 0

        def wait(count, handles, timeout, mask, flags):
            state["waits"] += 1
            # ``early_wakes`` simulates a raw-input packet waking the loop mid-frame:
            # every even wait advances only 4 ms, far short of the frame deadline.
            step = 0.004 if (self.early_wakes and state["waits"] % 2 == 0) else timeout / 1000.0
            self.clock["now"] += step
            return 0x102  # WAIT_TIMEOUT

        self.api.PeekMessageW.side_effect = peek
        self.api.MsgWaitForMultipleObjectsEx.side_effect = wait

    def run(self) -> None:
        with patch("win32_app.time.perf_counter", side_effect=lambda: self.clock["now"]), \
                patch.object(win32_app, "user32", self.api):
            self.app.run_loop(self.message)

    def timeouts(self) -> list[int]:
        return [call.args[2] for call in self.api.MsgWaitForMultipleObjectsEx.call_args_list]


def loop_app(active: bool) -> win32_app.DesktopApp:
    app = win32_app.DesktopApp(smoke_test=True)
    app.hwnd = 7
    app.active = active
    app.tick = Mock()
    app.present = Mock()
    return app


class TestGameLoopCadence(unittest.TestCase):
    def test_active_loop_paces_one_frame_per_interval_and_wakes_only_for_input(self):
        """活动状态每 16ms 预算最多呈现一帧；输入提前唤醒不得触发提前渲染。

        偶数次等待只前进 4ms，模拟高回报率鼠标在帧中途把循环唤醒。正确实现必须
        继续等到帧截止才渲染；每次唤醒都渲染的实现在约 40 次唤醒里会渲 ~40 帧，
        远超本断言的 ~21 帧上界——这就是本测试的反向验证。
        """
        app = loop_app(active=True)
        harness = LoopHarness(app, start=1000.0, peeks=40, early_wakes=True)
        harness.run()
        frames = app.present.call_count
        self.assertGreaterEqual(frames, 18, "8ms 截止必须持续呈现")
        self.assertLessEqual(frames, 22, "输入提前唤醒不得触发提前渲染")
        self.assertEqual(app.tick.call_count, frames, "活动状态下每帧先推进逻辑再呈现")
        timeouts = harness.timeouts()
        self.assertTrue(all(0 <= value <= win32_app.FRAME_INTERVAL_MS + 1 for value in timeouts),
                        f"活动状态的等待不得超过一个帧间隔: {timeouts}")
        calls = harness.api.MsgWaitForMultipleObjectsEx.call_args_list
        self.assertTrue(all(call.args[3] == 0x4FF for call in calls),
                        "唤醒掩码必须覆盖全部输入（含 raw input）")
        self.assertTrue(all(call.args[4] == 0x0002 for call in calls),
                        "必须用 MWMO_INPUTAVAILABLE 让已排队的输入立即唤醒")
        self.assertTrue(all(flag == 1 for flag in harness.remove_flags),
                        "PeekMessage 必须 PM_REMOVE，消息不得滞留队列")

    def test_idle_loop_renders_only_until_the_scene_is_clean(self):
        """空闲页只在场景变脏时呈现一帧，随后长睡——静态页面零持续开销。"""
        app = loop_app(active=False)
        app._dirty = True
        harness = LoopHarness(app, start=2000.0, peeks=20)
        harness.run()
        self.assertEqual(app.present.call_count, 1, "空闲页只在变脏后呈现一帧")
        app.tick.assert_not_called()
        timeouts = harness.timeouts()
        self.assertTrue(all(value > win32_app.FRAME_INTERVAL_MS for value in timeouts),
                        f"空闲等待应远大于帧间隔: {timeouts}")

    def test_loop_dispatches_pending_messages_before_frames(self):
        """排队消息先于渲染被 Translate/Dispatch，退出消息立即结束循环。"""
        app = loop_app(active=True)
        harness = LoopHarness(app, start=3000.0, peeks=99)  # 不会走到 cutoff
        state = {"peeks": 0}

        def peek(pointer, hwnd, lowest, highest, remove):
            state["peeks"] += 1
            if state["peeks"] == 1:
                pointer._obj.message = 0x0200  # WM_MOUSEMOVE
                return 1
            if state["peeks"] == 2:
                pointer._obj.message = 0x0012  # WM_QUIT
                return 1
            return 0

        harness.api.PeekMessageW.side_effect = peek
        harness.run()
        self.assertEqual(harness.api.TranslateMessage.call_count, 1)
        self.assertEqual(harness.api.DispatchMessageW.call_count, 1)
        self.assertEqual(app.present.call_count, 0, "退出前不应有时间渲染帧")

    def test_continuous_raw_input_cannot_starve_frame_deadlines(self):
        """队列持续非空时也必须按截止时间渲染，不能等到输入完全清空。"""
        app = loop_app(active=True)
        harness = LoopHarness(app, start=4000.0, peeks=9999)
        state = {"messages": 0}

        def peek(pointer, hwnd, lowest, highest, remove):
            state["messages"] += 1
            pointer._obj.message = 0x0200 if state["messages"] <= 1000 else 0x0012
            return 1

        def dispatch(pointer):
            harness.clock["now"] += 0.00025
            return 0

        harness.api.PeekMessageW.side_effect = peek
        harness.api.DispatchMessageW.side_effect = dispatch
        harness.run()
        self.assertGreaterEqual(app.present.call_count, 25,
                                "持续输入期间必须让出执行权给已到期的帧")

    def test_loop_resyncs_after_a_long_stall(self):
        """卡顿超过 4 个帧间隔后重新对齐帧截止，不得排队连发补偿帧。"""
        app = loop_app(active=True)
        harness = LoopHarness(app, start=5000.0, peeks=5)
        waits = {"count": 0}

        def wait_with_stall(count, handles, timeout, mask, flags):
            waits["count"] += 1
            stall = 0.5 if waits["count"] == 1 else 0.0  # 第一帧后注入 0.5s 停顿
            harness.clock["now"] += timeout / 1000.0 + stall
            return 0x102

        harness.api.MsgWaitForMultipleObjectsEx.side_effect = wait_with_stall
        harness.run()
        # 若不做重同步，循环会在停顿后连发 30+ 补偿帧。
        self.assertLessEqual(app.present.call_count, 6,
                             "长停顿后必须重对齐帧截止，而不是连发补偿帧")


class TestInvalidateRouting(unittest.TestCase):
    def test_invalidate_bumps_the_update_region_only_when_idle(self):
        app = win32_app.DesktopApp(smoke_test=True)
        app.hwnd = 5
        with patch.object(win32_app, "user32") as api:
            app.invalidate()
            self.assertTrue(app._dirty)
            api.InvalidateRect.assert_called_once()
            app._dirty = False
            app.active = True
            app.invalidate()
            self.assertTrue(app._dirty, "活动中仍然要标记脏帧")
            api.InvalidateRect.assert_called_once()  # 活动中由游戏循环呈现，不再追加系统失效


class TestPresent(unittest.TestCase):
    def test_present_renders_the_scene_and_validates_the_window(self):
        app = win32_app.DesktopApp(smoke_test=True)
        app.hwnd = 9
        app.paint_scene = Mock()
        with patch.object(win32_app, "user32") as api:
            api.GetDC.return_value = 321
            app.present()
        app.paint_scene.assert_called_once_with(321)
        api.ReleaseDC.assert_called_once_with(9, 321)
        api.ValidateRect.assert_called_once_with(9, None)


class TestGDIObjectCache(unittest.TestCase):
    def test_fast_path_pens_and_brushes_are_created_once_and_never_deleted(self):
        """掉帧修复回归锁：GDI 快路径对象按参数缓存，禁止每帧 Create/Delete。

        反向验证：去掉缓存（恢复每调用 Create+Delete），本测试必失败。
        """
        app = win32_app.DesktopApp(smoke_test=True)
        app._gfx = None  # 强制走 GDI 快路径
        with patch("win32_app.gdi32") as gdi:
            app.line(1, 0, 0, 40, 0, "FFFFFF", 1)
            app.line(1, 0, 10, 40, 10, "FFFFFF", 1)
            app.line(1, 0, 20, 40, 20, "EAF2F6", 1)
            self.assertEqual(gdi.CreatePen.call_count, 2, "同色同宽的画笔必须复用")
            app.box(1, 0, 0, 50, 50, "18232F", radius=0)
            app.box(1, 0, 60, 50, 50, "18232F", radius=0)
            self.assertEqual(gdi.CreateSolidBrush.call_count, 1, "同色画刷必须复用")
            gdi.DeleteObject.assert_not_called()  # 缓存的 GDI 对象不能在帧内删除


class TestPerfDiagnostics(unittest.TestCase):
    def test_summary_reports_frame_tail_latency_and_input_rate(self):
        probe = win32_app.PerfDiagnostics()
        probe.started_at = 10.0
        probe.raw_input_messages = 1000
        probe.message_batches.extend((1, 12, 128))
        probe.record_frame(10.000, 10.004, 0.000)
        probe.record_frame(10.008, 10.014, 0.002)
        with patch("win32_app.time.perf_counter", return_value=11.0):
            report = probe.summary()
        self.assertEqual(report["frames"], 2)
        self.assertAlmostEqual(report["frame_ms"]["max"], 6.0)
        self.assertAlmostEqual(report["frame_interval_ms"]["median"], 8.0)
        self.assertEqual(report["raw_input_per_second"], 1000.0)
        self.assertEqual(report["message_batch_max"], 128)


if __name__ == "__main__":
    unittest.main(verbosity=2)
