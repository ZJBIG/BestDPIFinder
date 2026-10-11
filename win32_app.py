"""Chinese native Windows desktop frontend and angular-plane aim assessment."""
from __future__ import annotations

import ctypes as ct
import gc
import json
import math
import os
import random
import sys
import time
import traceback
from ctypes import wintypes as wt
from pathlib import Path
from typing import Any

from core import (GAME_YAWS, STAGES, STAGE_SECONDS, Candidate, Settings, StageRecord,
                  Trial, TrialRecord, candidates, load_payload, make_payload,
                  make_plan, raw_angle, save_payload, sensitivity_for, summarize)
from native import (GdiplusStartupInput, GpPointF, PAINTSTRUCT, WNDCLASS, WNDPROC, RawMouse,
                    argb, choose_json, color, gdi32, gdiplus, kernel32, user32, winmm)

BG: str = "101720"
PANEL: str = "18232F"
PANEL2: str = "1D2B39"
WHITE: str = "EAF2F6"
MUTED: str = "94A8BC"
TEAL: str = "63E1C0"
YELLOW: str = "F0CA80"
BORDER: str = "2A3B4D"
# The optimized active scene measures about 5.3 ms at p90.  Keep the visual
# cadence near 125 Hz; 60 Hz was visibly choppy on high-refresh displays.
FRAME_INTERVAL_MS: int = 8
MESSAGE_BATCH_LIMIT: int = 128


def percentile(samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile used by the opt-in performance report."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(math.ceil(len(ordered) * fraction)) - 1))
    return ordered[index]


class PerfDiagnostics:
    """In-memory frame/input probe; it performs no I/O until the app exits."""
    def __init__(self) -> None:
        self.started_at: float = time.perf_counter()
        self.last_frame_at: float | None = None
        self.frame_ms: list[float] = []
        self.frame_interval_ms: list[float] = []
        self.deadline_late_ms: list[float] = []
        self.message_batches: list[int] = []
        self.message_drain_ms: list[float] = []
        self.raw_input_ms: list[float] = []
        self.blit_ms: list[float] = []
        self.raw_input_messages: int = 0

    def record_frame(self, started: float, ended: float, late_seconds: float) -> None:
        self.frame_ms.append(max(0.0, ended - started) * 1000)
        if self.last_frame_at is not None:
            self.frame_interval_ms.append(max(0.0, started - self.last_frame_at) * 1000)
        self.last_frame_at = started
        self.deadline_late_ms.append(max(0.0, late_seconds) * 1000)

    def summary(self) -> dict[str, Any]:
        duration: float = max(1e-9, time.perf_counter() - self.started_at)
        active_seconds: float = sum(self.frame_interval_ms) / 1000
        effective_fps: float = ((len(self.frame_interval_ms) + 1) / active_seconds
                                if active_seconds > 0 else 0.0)

        def distribution(values: list[float]) -> dict[str, float]:
            return {"median": percentile(values, .5), "p95": percentile(values, .95),
                    "p99": percentile(values, .99), "max": max(values, default=0.0)}

        return {
            "target_frame_ms": FRAME_INTERVAL_MS,
            "frames": len(self.frame_ms),
            "effective_fps": effective_fps,
            "frame_ms": distribution(self.frame_ms),
            "frame_interval_ms": distribution(self.frame_interval_ms),
            "deadline_late_ms": distribution(self.deadline_late_ms),
            "over_budget_frames": sum(value > FRAME_INTERVAL_MS for value in self.frame_ms),
            "missed_frame_slots": sum(value >= FRAME_INTERVAL_MS for value in self.deadline_late_ms),
            "raw_input_messages": self.raw_input_messages,
            "raw_input_per_second": self.raw_input_messages / duration,
            "message_batch_max": max(self.message_batches, default=0),
            "full_message_batches": sum(value >= MESSAGE_BATCH_LIMIT for value in self.message_batches),
            "message_drain_ms": distribution(self.message_drain_ms),
            "raw_input_ms": distribution(self.raw_input_ms),
            "screen_blt_ms": distribution(self.blit_ms),
        }


def emit(text: str, stream: Any = None) -> None:
    """Write a diagnostic line only when a console actually exists.

    The packaged app is built with ``--windowed``, so ``sys.stdout``/``sys.stderr`` are None.
    An unguarded ``print`` there raises, and the windowed bootloader answers an uncaught
    exception with a modal error box that blocks until someone clicks it -- which looks
    exactly like a hung program. Evidence for packaged runs travels through --smoke-report.
    """
    handle = sys.stdout if stream is None else stream
    if handle is None:
        return
    try:
        print(text, file=handle)
    except (OSError, ValueError):
        pass


class DesktopApp:
    """Own all UI and input state on the single native Windows message thread."""
    def __init__(self, smoke_test: bool = False, perf_report: Path | None = None) -> None:
        self.smoke_test: bool = smoke_test
        self.smoke_result: dict[str, Any] = {}
        self.hwnd: int = 0
        self.width: int = 1180
        self.height: int = 746
        self.page: str = "setup"
        self.settings: Settings = Settings()
        self.plan: list[Trial] = []
        self.records: list[TrialRecord] = []
        self.trial_index: int = 0
        self.stage_index: int = 0
        self.stage_records: list[StageRecord] = []
        self.aborted: int = 0
        self.loaded: bool = False
        self.warmed: bool = False
        self.active: bool = False
        self.warmup: bool = False
        self.countdown_until: float = 0.0
        self.last_tick: float = 0.0
        self.elapsed: float = 0.0
        self.tracking_on: float = 0.0
        self.hits: int = 0
        self.misses: int = 0
        self.latencies: list[float] = []
        self.target_spawn: float = 0.0
        self.view_x: float = 0.0
        self.view_y: float = 0.0
        self.target_x: float = 0.0
        self.target_y: float = 0.0
        self.hit_flash: float = 0.0
        self.random: random.Random = random.Random(0)
        self.current_sensitivity: float = self.settings.sensitivity
        self.status: str = "本地运行 · 不读取游戏 · 不更改系统鼠标设置"
        self.capture_status: str = "等待你的明确点击，不会自动捕获鼠标。"
        self.buttons: list[tuple[tuple[int, int, int, int], str]] = []
        self.controls: dict[str, int] = {}
        self.fonts: dict[tuple[int, bool], int] = {}
        self.raw: RawMouse | None = None
        self.proc = WNDPROC(self.window_proc)
        self.gp_token: int = 0
        self.timer_period_set: bool = False
        # GDI+ 会话：token=0 表示未启动（单测/启动失败时退回纯 GDI 绘制，行为不变）。
        self._gfx: int | None = None
        self._gp_pens: dict[tuple[int, float], int] = {}
        self._gp_brushes: dict[int, int] = {}
        self._panel_cache: dict[tuple, tuple[int, int]] = {}
        # Keep the full-window double buffer across paints.  Creating and
        # destroying a screen-sized bitmap for every frame causes occasional
        # allocation stalls that are especially noticeable while aiming.
        self._backbuffer: tuple[int, int, int, int, int] | None = None
        # Game-loop render state: ``_dirty`` marks a frame the loop must present;
        # cached GDI pens/brushes stop per-frame CreatePen/CreateSolidBrush churn.
        self._dirty: bool = True
        self._pen_cache: dict[tuple[int, int], int] = {}
        self._brush_cache: dict[int, int] = {}
        self.perf_report_path: Path | None = perf_report
        self.perf_diagnostics: PerfDiagnostics | None = PerfDiagnostics() if perf_report else None
        self.base_dir: Path = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
        self.history_path: Path = self.base_dir / "results" / "latest.json"
        self.fatal_error: str = ""

    def run(self) -> int:
        """Create the native window and enter its message loop."""
        user32.SetProcessDPIAware()
        # Raise the timer resolution so the 16 ms render cadence stays stable.
        self.timer_period_set = winmm.timeBeginPeriod(1) == 0
        startup = GdiplusStartupInput(1, None, False, False)
        token = wt.ULONG(0)
        if gdiplus.GdiplusStartup(ct.byref(token), ct.byref(startup), None) == 0:
            self.gp_token = token.value  # 失败则保持 0，全部绘制退回纯 GDI，功能不受影响。
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASS()
        window_class.lpfnWndProc = self.proc
        window_class.hInstance = instance
        # PyInstaller embeds the application icon as resource 1.  Loading it from the module
        # keeps the title bar/taskbar icon aligned with the executable; source runs fall back
        # to Windows' standard application icon because they have no embedded resource.
        window_class.hIcon = (user32.LoadIconW(instance, ct.c_void_p(1))
                              or user32.LoadIconW(None, ct.c_void_p(32512)))
        window_class.hCursor = user32.LoadCursorW(None, ct.c_void_p(32512))
        window_class.lpszClassName = "BestDPIFinder.NativeDesktop.v1"
        if not user32.RegisterClassW(ct.byref(window_class)):
            raise ct.WinError(ct.get_last_error())
        screen_width: int = user32.GetSystemMetrics(0)
        screen_height: int = user32.GetSystemMetrics(1)
        outer_width: int = min(1220, screen_width - 40)
        outer_height: int = min(820, screen_height - 64)
        # 窗口化全屏：标准窗口（保留标题栏/任务栏）创建后立即最大化铺满工作区。
        self.hwnd = user32.CreateWindowExW(0, window_class.lpszClassName,
                                           "BestDPI Finder — FPS 灵敏度实验室", 0x02CF0000,
                                           max(0, (screen_width - outer_width) // 2),
                                           max(0, (screen_height - outer_height) // 2),
                                           outer_width, outer_height, None, None, instance, None)
        if not self.hwnd:
            raise ct.WinError(ct.get_last_error())
        user32.ShowWindow(self.hwnd, 3)  # SW_MAXIMIZE：窗口化全屏，铺满工作区（任务栏保留）。
        rectangle = wt.RECT()
        user32.GetClientRect(self.hwnd, ct.byref(rectangle))
        self.width, self.height = rectangle.right, rectangle.bottom
        self.raw = RawMouse(self.hwnd, self.on_raw)
        self.create_controls()
        if self.smoke_test and not user32.SetTimer(self.hwnd, 2, 150, None):
            raise ct.WinError(ct.get_last_error())
        # Startup objects are permanent. Freezing them out of the collector's way and
        # making full collections rare removes GC pauses from the frame budget --
        # a mid-aim gen2 collection is a visible hitch even though nothing else changed.
        gc.collect()
        gc.freeze()
        gc.set_threshold(2000, 25, 100)
        user32.UpdateWindow(self.hwnd)
        message = wt.MSG()
        try:
            self.run_loop(message)
        finally:
            if self.raw:
                self.raw.stop()
            for font in self.fonts.values():
                gdi32.DeleteObject(font)
            for pen in self._pen_cache.values():
                gdi32.DeleteObject(pen)
            for brush in self._brush_cache.values():
                gdi32.DeleteObject(brush)
            if self.gp_token:
                for pen in self._gp_pens.values():
                    gdiplus.GdipDeletePen(pen)
                for brush in self._gp_brushes.values():
                    gdiplus.GdipDeleteBrush(brush)
                for old_bitmap, old_dc in self._panel_cache.values():
                    gdi32.DeleteDC(old_dc)
                    gdi32.DeleteObject(old_bitmap)
                gdiplus.GdiplusShutdown(self.gp_token)
            self._discard_backbuffer()
            if self.timer_period_set:
                winmm.timeEndPeriod(1)
            if self.perf_report_path and self.perf_diagnostics:
                try:
                    self.perf_report_path.parent.mkdir(parents=True, exist_ok=True)
                    self.perf_report_path.write_text(
                        json.dumps(self.perf_diagnostics.summary(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
                except OSError as error:
                    emit(f"performance report failed: {error}", sys.stderr)
        if self.smoke_test:
            emit(json.dumps(self.smoke_result, ensure_ascii=False))
        return 1 if self.fatal_error else 0

    def run_loop(self, message: wt.MSG) -> None:
        """Game-style loop: drain every pending input message, then present one
        frame per ``FRAME_INTERVAL_MS`` deadline.

        WM_TIMER and WM_PAINT are the two lowest-priority messages Windows
        generates: both are synthesized only when the queue is otherwise empty,
        so a burst of raw input (1,000-8,000 packets/s) starved the render timer
        and produced visible hitches while aiming.  Owning the cadence here
        fixes that: input is consumed the moment it arrives and the frame
        deadline is enforced by our own clock.  While idle (nothing active, no
        dirty scene) the loop sleeps in 100 ms slices, so static pages cost
        nothing.
        """
        interval: float = FRAME_INTERVAL_MS / 1000.0
        next_frame: float = time.perf_counter() + interval
        while True:
            drain_started: float = time.perf_counter()
            batch: int = 0
            while user32.PeekMessageW(ct.byref(message), None, 0, 0, 1):  # PM_REMOVE
                if message.message == 0x0012:  # WM_QUIT
                    return
                user32.TranslateMessage(ct.byref(message))
                user32.DispatchMessageW(ct.byref(message))
                batch += 1
                # A high-polling mouse can keep the queue non-empty forever.
                # Stop draining at the frame deadline (with a count backstop),
                # present, then continue with the remaining input immediately.
                if batch >= MESSAGE_BATCH_LIMIT or (self.active and time.perf_counter() >= next_frame):
                    break
            if self.perf_diagnostics:
                self.perf_diagnostics.message_batches.append(batch)
                self.perf_diagnostics.message_drain_ms.append(
                    max(0.0, time.perf_counter() - drain_started) * 1000)
            now: float = time.perf_counter()
            if now > next_frame + 4 * interval:
                next_frame = now  # First frame, or resync after a long stall.
            if self.active or self._dirty:
                if now >= next_frame:
                    frame_started: float = now
                    late: float = max(0.0, now - next_frame)
                    if self.active:
                        self.tick()
                    self.present()
                    if self.perf_diagnostics:
                        self.perf_diagnostics.record_frame(
                            frame_started, time.perf_counter(), late)
                    self._dirty = False
                    frame_ended: float = time.perf_counter()
                    next_frame = (frame_ended + interval
                                  if frame_ended >= next_frame + interval
                                  else next_frame + interval)
            else:
                next_frame = 0.0  # Idle: the first dirty frame renders immediately.
            if self.active or self._dirty:
                timeout_ms = max(0, int((next_frame - time.perf_counter()) * 1000) + 1)
            else:
                timeout_ms = 100
            # QS_ALLINPUT (0x4FF) covers raw input; MWMO_INPUTAVAILABLE (2) wakes
            # even when the input was already queued before the wait began.
            user32.MsgWaitForMultipleObjectsEx(0, None, timeout_ms, 0x4FF, 0x0002)

    def window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            if message == 0x000F:  # WM_PAINT
                self.paint(hwnd)
                return 0
            if message == 0x0014:  # WM_ERASEBKGND: double buffered.
                return 1
            if message == 0x00FF:  # WM_INPUT; never WM_MOUSEMOVE for aiming.
                raw_started: float = time.perf_counter() if self.perf_diagnostics else 0.0
                if self.perf_diagnostics:
                    self.perf_diagnostics.raw_input_messages += 1
                if self.raw:
                    self.raw.receive(lparam)
                if self.perf_diagnostics:
                    self.perf_diagnostics.raw_input_ms.append(
                        max(0.0, time.perf_counter() - raw_started) * 1000)
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)
            if message == 0x0113:
                if wparam == 2 and self.smoke_test:
                    user32.KillTimer(hwnd, 2)
                    self.run_smoke()
                else:
                    self.tick()
                return 0
            if message == 0x0100 and wparam == 0x1B:  # Escape always frees capture.
                self.abort("已按 Esc 暂停。当前未完成候选不计分；可重新开始。")
                return 0
            if message in (0x0008, 0x001F, 0x0215):  # focus/cancel/capture loss.
                self.abort("窗口失去焦点或捕获已取消；当前未完成候选不计分。")
            if message == 0x001C and not wparam:
                self.abort("已切换到其他应用。鼠标已释放，当前候选需要重测。")
            if message == 0x0006 and (wparam & 0xFFFF) == 0:
                self.abort("窗口未激活。鼠标已释放，当前候选需要重测。")
            if message == 0x0202 and not self.active:
                x: int = ct.c_short(lparam & 0xFFFF).value
                y: int = ct.c_short((lparam >> 16) & 0xFFFF).value
                for (left, top, right, bottom), action in reversed(self.buttons):
                    if left <= x <= right and top <= y <= bottom:
                        self.action(action)
                        break
                return 0
            if message == 0x0111:
                identifier: int = wparam & 0xFFFF
                notification: int = (wparam >> 16) & 0xFFFF
                if identifier == 101 and notification == 1:
                    selection: int = user32.SendMessageW(self.controls["game"], 0x147, 0, 0)
                    user32.SetWindowTextW(self.controls["yaw"], "0.07" if selection == 0 else "0.022")
                    user32.EnableWindow(self.controls["yaw"], selection != 0)
                self.invalidate()
                return 0
            if message == 0x0005 and self.hwnd:
                if self.active:
                    self.abort("窗口尺寸改变，鼠标已释放；请重试当前候选。")
                self.width = lparam & 0xFFFF
                self.height = (lparam >> 16) & 0xFFFF
                self._discard_backbuffer()
                self.place_controls()
                self.invalidate()
                return 0
            if message == 0x0010:
                self.active = False
                if self.raw:
                    self.raw.stop()
                user32.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:
                self.active = False
                if self.raw:
                    self.raw.stop()
                user32.KillTimer(hwnd, 1)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception:
            self.fatal_error = traceback.format_exc()
            self.active = False
            if self.raw:
                self.raw.stop()
            if self.smoke_test:
                self.smoke_result["error"] = self.fatal_error
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
            else:
                self.status = "出现异常，鼠标已释放。请重新打开应用。"
                emit(self.fatal_error, sys.stderr)
                user32.MessageBoxW(hwnd, self.status + "\n\n" + self.fatal_error[-1000:], "程序错误", 0x10)
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def font(self, size: int = 16, bold: bool = False) -> int:
        key: tuple[int, bool] = (size, bold)
        if key not in self.fonts:
            self.fonts[key] = gdi32.CreateFontW(-size, 0, 0, 0, 600 if bold else 400,
                                               0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei UI")
        return self.fonts[key]

    def create_controls(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        specs: list[tuple[str, str, str, int, int]] = [
            ("game", "COMBOBOX", "", 0x00200003, 101),
            ("dpi", "EDIT", "800", 0x00800080, 102),
            ("sens", "EDIT", "0.35", 0x00800080, 103),
            ("yaw", "EDIT", "0.07", 0x00800080, 104),
            ("comfort", "EDIT", "", 0x00800080, 105)]
        for name, class_name, text, style, identifier in specs:
            handle: int = user32.CreateWindowExW(0, class_name, text, 0x50010000 | style,
                                                 0, 0, 280, 34, self.hwnd, identifier, instance, None)
            if not handle:
                raise ct.WinError(ct.get_last_error())
            self.controls[name] = handle
            user32.SendMessageW(handle, 0x0030, self.font(17), 1)
        for game in GAME_YAWS:
            value = ct.create_unicode_buffer(game)
            user32.SendMessageW(self.controls["game"], 0x143, 0, ct.cast(value, ct.c_void_p).value)
        user32.SendMessageW(self.controls["game"], 0x14E, 0, 0)
        user32.EnableWindow(self.controls["yaw"], False)
        user32.SendMessageW(self.controls["comfort"], 0x00C5, 500, 0)
        self.place_controls()

    def place_controls(self) -> None:
        for name, y in (("game", 229), ("dpi", 309), ("sens", 389), ("yaw", 469)):
            if name in self.controls:
                user32.MoveWindow(self.controls[name], 54, y, 280, 180 if name == "game" else 33, True)
                user32.ShowWindow(self.controls[name], 5 if self.page == "setup" else 0)
        if "comfort" in self.controls:
            user32.MoveWindow(self.controls["comfort"], 52, self.height - 140, self.width - 104, 31, True)
            user32.ShowWindow(self.controls["comfort"], 5 if self.page == "results" else 0)

    def get_text(self, name: str) -> str:
        buffer = ct.create_unicode_buffer(1200)
        user32.GetWindowTextW(self.controls[name], buffer, len(buffer))
        return buffer.value.strip()

    def read_settings(self) -> Settings:
        try:
            index: int = user32.SendMessageW(self.controls["game"], 0x147, 0, 0)
            return Settings(list(GAME_YAWS)[max(0, index)], float(self.get_text("dpi")),
                            float(self.get_text("sens")), float(self.get_text("yaw"))).validate()
        except (ValueError, OverflowError) as error:
            if "could not convert" in str(error):
                raise ValueError("请为 DPI、灵敏度和 Yaw 填写有效数字，不要输入单位。") from error
            raise

    def invalidate(self) -> None:
        # The game loop owns the frame cadence; while active it presents the next
        # frame anyway, so only the idle pages need the system update region bumped
        # (that also keeps the synchronous InvalidateRect+UpdateWindow render path
        # real for the QA probes, which click buttons right after painting).
        self._dirty = True
        if self.hwnd and not self.active:
            user32.InvalidateRect(self.hwnd, None, False)

    def _gdi_pen(self, color_ref: int, width: int = 1) -> int:
        """Cached GDI pen: per-frame CreatePen/DeleteObject churn shows up in frame time."""
        key: tuple[int, int] = (width, color_ref)
        pen: int | None = self._pen_cache.get(key)
        if pen is None:
            pen = gdi32.CreatePen(0, width, color_ref)
            self._pen_cache[key] = pen
        return pen

    def _gdi_brush(self, color_ref: int) -> int:
        brush: int | None = self._brush_cache.get(color_ref)
        if brush is None:
            brush = gdi32.CreateSolidBrush(color_ref)
            self._brush_cache[color_ref] = brush
        return brush

    def _gp_pen(self, argb_value: int, width: float) -> int:
        key = (argb_value, round(width, 1))
        pen: int | None = self._gp_pens.get(key)
        if not pen:
            handle = ct.c_void_p()
            if gdiplus.GdipCreatePen1(argb_value, width, 2, ct.byref(handle)) == 0:  # 2=UnitPixel
                pen = handle.value
                self._gp_pens[key] = pen
        return pen

    def _gp_solid(self, argb_value: int) -> int:
        brush: int | None = self._gp_brushes.get(argb_value)
        if not brush:
            handle = ct.c_void_p()
            if gdiplus.GdipCreateSolidFill(argb_value, ct.byref(handle)) == 0:
                brush = handle.value
                self._gp_brushes[argb_value] = brush
        return brush

    def _gp_rounded_path(self, x: int, y: int, width: int, height: int, radius: int) -> int | None:
        """圆角矩形路径 = 四段 90° 圆弧首尾相连；创建失败返回 None。"""
        path = ct.c_void_p()
        if gdiplus.GdipCreatePath(0, ct.byref(path)) == 0 and path.value:
            d: float = float(radius)
            for px, py, begin in ((x, y, 180.0), (x + width - d, y, 270.0),
                                  (x + width - d, y + height - d, 0.0), (x, y + height - d, 90.0)):
                gdiplus.GdipAddPathArc(path.value, float(px), float(py), d, d, begin, 90.0)
            gdiplus.GdipClosePathFigure(path.value)
            return path.value
        return None

    def _gp_box(self, x: int, y: int, width: int, height: int, fill: str,
                outline: str | None, radius: int) -> None:
        brush = self._gp_solid(argb(fill))
        if not brush:
            return
        rounded = self._gp_rounded_path(x, y, width, height, radius) if radius > 0 else None
        if rounded:
            gdiplus.GdipFillPath(self._gfx, brush, rounded)
            if outline:
                gdiplus.GdipDrawPath(self._gfx, self._gp_pen(argb(outline), 1.0), rounded)
            gdiplus.GdipDeletePath(rounded)
        else:
            gdiplus.GdipFillRectangle(self._gfx, brush, float(x), float(y), float(width), float(height))
            if outline:
                pen = self._gp_pen(argb(outline), 1.0)
                corners = ((x, y, x + width, y), (x + width, y, x + width, y + height),
                           (x + width, y + height, x, y + height), (x, y + height, x, y))
                for px1, py1, px2, py2 in corners:
                    gdiplus.GdipDrawLine(self._gfx, pen, float(px1), float(py1), float(px2), float(py2))

    def gradient_panel(self, dc: int, x: int, y: int, width: int, height: int, top_color: str,
                       bottom_color: str, outline: str | None = None, radius: int = 18) -> None:
        """渐变圆角面板：GDI+ 渐变填充大面板每帧约 10ms，是掉帧主因，因此只渲染一次进
        位图缓存，之后每帧 BitBlt。几何或颜色变化（例如窗口尺寸改变）时重建并丢弃旧位图。"""
        # Unit tests and a failed GDI+ startup deliberately run with no GDI+
        # session.  Keep that documented pure-GDI fallback real instead of
        # calling GDI+ with a headless DC.
        if not self.gp_token:
            self.box(dc, x, y, width, height, top_color, outline, radius)
            return
        key = (x, y, width, height, top_color, bottom_color, outline, radius)
        if key not in self._panel_cache:
            for old_bitmap, old_dc in self._panel_cache.values():
                gdi32.DeleteDC(old_dc)
                gdi32.DeleteObject(old_bitmap)
            self._panel_cache.clear()
            mem_dc = gdi32.CreateCompatibleDC(dc)
            mem_bitmap = gdi32.CreateCompatibleBitmap(dc, width, height)
            gdi32.SelectObject(mem_dc, mem_bitmap)
            graphics = ct.c_void_p()
            gdiplus.GdipCreateFromHDC(mem_dc, ct.byref(graphics))
            previous_gfx = self._gfx
            self._gfx = graphics.value
            try:
                # 圆角外先铺页面底色，整块贴回时四角才不露黑。
                self.box(mem_dc, 0, 0, width, height, BG, radius=0)
                start, end = GpPointF(0.0, 0.0), GpPointF(0.0, float(height))
                brush = ct.c_void_p()
                # 线性渐变刷只接受 WrapMode 0–3（Clamp 会 InvalidParameter）；首尾正好贴合
                # 矩形边界，平铺模式不可见。
                if gdiplus.GdipCreateLineBrush(ct.byref(start), ct.byref(end), argb(top_color),
                                               argb(bottom_color), 0, ct.byref(brush)) == 0 and brush.value:
                    rounded = self._gp_rounded_path(0, 0, width, height, radius) if radius > 0 else None
                    if rounded:
                        gdiplus.GdipFillPath(graphics.value, brush.value, rounded)
                        if outline:
                            gdiplus.GdipDrawPath(graphics.value, self._gp_pen(argb(outline), 1.0), rounded)
                        gdiplus.GdipDeletePath(rounded)
                    else:
                        gdiplus.GdipFillRectangle(graphics.value, brush.value,
                                                  0.0, 0.0, float(width), float(height))
                    gdiplus.GdipDeleteBrush(brush.value)
            finally:
                gdiplus.GdipDeleteGraphics(graphics.value)
                self._gfx = previous_gfx
            self._panel_cache[key] = (mem_bitmap, mem_dc)
        mem_dc = self._panel_cache[key][1]
        gdi32.BitBlt(dc, x, y, width, height, mem_dc, 0, 0, 0x00CC0020)

    def box(self, dc: int, x: int, y: int, width: int, height: int, fill: str = PANEL,
            outline: str | None = None, radius: int = 18) -> None:
        # 圆角面板走 GDI+（抗锯齿圆角肉眼可见）；实心直角矩形走 GDI 快路径——GDI+ 软件填充
        # 整屏背景是掉帧主因，实心底没有任何抗锯齿收益。
        if self._gfx and radius > 0:
            self._gp_box(x, y, width, height, fill, outline, radius)
            return
        brush = self._gdi_brush(color(fill))
        pen = self._gdi_pen(color(outline or fill))
        previous_brush = gdi32.SelectObject(dc, brush)
        previous_pen = gdi32.SelectObject(dc, pen)
        gdi32.RoundRect(dc, int(x), int(y), int(x + width), int(y + height), radius, radius)
        gdi32.SelectObject(dc, previous_brush)
        gdi32.SelectObject(dc, previous_pen)

    def text(self, dc: int, text: str, x: int, y: int, width: int, height: int = 28,
             size: int = 16, fill: str = WHITE, bold: bool = False, center: bool = False) -> None:
        old_font = gdi32.SelectObject(dc, self.font(size, bold))
        gdi32.SetTextColor(dc, color(fill))
        gdi32.SetBkMode(dc, 1)
        rect = wt.RECT(int(x), int(y), int(x + width), int(y + height))
        user32.DrawTextW(dc, text, -1, ct.byref(rect), 0x800 | 0x10 | (1 if center else 0))
        gdi32.SelectObject(dc, old_font)

    def line(self, dc: int, x1: float, y1: float, x2: float, y2: float, fill: str, width: int = 1) -> None:
        # 1px 直线（网格/分隔线）走 GDI 快路径：1px 上抗锯齿不可见，纯开销。
        if self._gfx and width > 1:
            gdiplus.GdipDrawLine(self._gfx, self._gp_pen(argb(fill), float(width)),
                                 float(x1), float(y1), float(x2), float(y2))
            return
        pen = self._gdi_pen(color(fill), width)
        previous = gdi32.SelectObject(dc, pen)
        gdi32.MoveToEx(dc, int(x1), int(y1), None)
        gdi32.LineTo(dc, int(x2), int(y2))
        gdi32.SelectObject(dc, previous)

    def circle(self, dc: int, x: float, y: float, radius: float, fill: str, outline: str) -> None:
        if self._gfx:
            gdiplus.GdipFillEllipse(self._gfx, self._gp_solid(argb(fill)),
                                    x - radius, y - radius, radius * 2, radius * 2)
            gdiplus.GdipDrawEllipse(self._gfx, self._gp_pen(argb(outline), 2.0),
                                    x - radius, y - radius, radius * 2, radius * 2)
            return
        brush = self._gdi_brush(color(fill))
        pen = self._gdi_pen(color(outline), 2)
        previous_brush = gdi32.SelectObject(dc, brush)
        previous_pen = gdi32.SelectObject(dc, pen)
        gdi32.Ellipse(dc, int(x - radius), int(y - radius), int(x + radius), int(y + radius))
        gdi32.SelectObject(dc, previous_brush)
        gdi32.SelectObject(dc, previous_pen)

    def button(self, dc: int, label: str, action: str, x: int, y: int, width: int,
               primary: bool = False, height: int = 44) -> None:
        self.box(dc, x, y, width, height, TEAL if primary else PANEL2, None if primary else BORDER, 12)
        self.text(dc, label, x + 8, y + (height - 25) // 2, width - 16, 28, 15,
                  BG if primary else WHITE, True, True)
        self.buttons.append(((x, y, x + width, y + height), action))

    def _discard_backbuffer(self) -> None:
        if self._backbuffer is None:
            return
        bitmap, dc, old_bitmap, _, _ = self._backbuffer
        gdi32.SelectObject(dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
        self._backbuffer = None

    def _backbuffer_for(self, front: int, width: int, height: int) -> int:
        if self._backbuffer is None or self._backbuffer[3:] != (width, height):
            self._discard_backbuffer()
            dc = gdi32.CreateCompatibleDC(front)
            bitmap = gdi32.CreateCompatibleBitmap(front, width, height)
            old_bitmap = gdi32.SelectObject(dc, bitmap)
            self._backbuffer = (bitmap, dc, old_bitmap, width, height)
        return self._backbuffer[1]

    def paint(self, hwnd: int) -> None:
        """WM_PAINT path: system-initiated exposure (resize, occlusion) and the
        synchronous InvalidateRect+UpdateWindow renders the QA probes rely on."""
        paint = PAINTSTRUCT()
        front = user32.BeginPaint(hwnd, ct.byref(paint))
        try:
            self.paint_scene(front)
        finally:
            user32.EndPaint(hwnd, ct.byref(paint))

    def present(self) -> None:
        """Render one frame from the game loop straight to the window."""
        if not self.hwnd:
            return
        front = user32.GetDC(self.hwnd)
        try:
            self.paint_scene(front)
        finally:
            user32.ReleaseDC(self.hwnd, front)
        user32.ValidateRect(self.hwnd, None)

    def paint_scene(self, front: int) -> None:
        width: int = max(self.width, 1)
        height: int = max(self.height, 1)
        dc = self._backbuffer_for(front, width, height)
        try:
            if self.gp_token:
                graphics = ct.c_void_p()
                if gdiplus.GdipCreateFromHDC(dc, ct.byref(graphics)) == 0:
                    gdiplus.GdipSetSmoothingMode(graphics, 4)  # SmoothingModeAntiAlias
                    self._gfx = graphics.value
            self.box(dc, 0, 0, width + 1, height + 1, BG, radius=0)
            self.buttons = []
            training_page: bool = self.active and self.page == "test"
            self.text(dc, "BESTDPI", 34, 22, 230, 34, 25, TEAL, True)
            self.text(dc, "FINDER  /  灵敏度实验室", 179, 28, 340, 28, 16, MUTED)
            self.text(dc, "把手感变成可比较的练习记录，而不是一个神奇数字。", 34, 65, 730, 28, 15, MUTED)
            if not self.active:
                for index, (label, action, page) in enumerate((("01  设置", "setup", "setup"),
                        ("02  测评", "test", "test"), ("03  结果", "results", "results"),
                        ("帮助", "help", "help"))):
                    self.button(dc, label, action, self.width - 480 + index * 112, 26, 102,
                                self.page == page, 37)
            self.line(dc, 34, 108, width - 34, 108, BORDER)
            if self.page == "setup":
                self.draw_setup(dc)
            elif self.page == "test":
                self.draw_test(dc)
            elif self.page == "results":
                self.draw_results(dc)
            else:
                self.draw_help(dc)
            if training_page:
                self.text(dc, "BESTDPI", 34, 22, 230, 34, 25, TEAL, True)
                self.text(dc, "FINDER  /  灵敏度实验室", 179, 28, 340, 28, 16, MUTED)
                self.text(dc, "把手感变成可比较的练习记录，而不是一个神奇数字。", 34, 65, 730, 28, 15, MUTED)
                self.line(dc, 34, 108, width - 34, 108, BORDER)
            if not training_page:
                self.text(dc, self.status, 34, height - 29, width - 68, 24, 13, MUTED)
            blit_started: float = time.perf_counter() if self.perf_diagnostics else 0.0
            gdi32.BitBlt(front, 0, 0, width, height, dc, 0, 0, 0x00CC0020)
            if self.perf_diagnostics:
                self.perf_diagnostics.blit_ms.append(
                    max(0.0, time.perf_counter() - blit_started) * 1000)
        finally:
            if self._gfx:
                gdiplus.GdipDeleteGraphics(self._gfx)
                self._gfx = None

    def draw_setup(self, dc: int) -> None:
        self.text(dc, "先建立你的基准", 34, 132, 600, 42, 28, WHITE, True)
        self.box(dc, 34, 189, 320, self.height - 244)
        for label, y in (("游戏 / 普通腰射", 201), ("鼠标 DPI · 手动填写", 281),
                         ("当前游戏灵敏度", 361), ("水平 Yaw · CS2 可自定义", 441)):
            self.text(dc, label, 54, y, 282, 25, 14, MUTED)
        self.text(dc, "DPI 请从鼠标驱动或机身档位确认。\n本工具不会声称自动读取硬件 DPI。", 54, 521, 280, 58, 14, MUTED)
        self.button(dc, "保存基准，进入热身", "prepare", 54, self.height - 110, 280, True)
        right: int = 380
        available: int = self.width - right - 34
        self.box(dc, right, 189, available, 140, PANEL2)
        try:
            setting: Settings = self.read_settings()
            distance: float = setting.distance
            self.text(dc, f"{distance:.2f}", right + 24, 209, 320, 65, 48, TEAL, True)
            self.text(dc, "cm / 360°", right + 24, 276, 300, 28, 17, MUTED)
            self.text(dc, "转身一周的物理距离", right + 320, 217, available - 338, 28, 18, WHITE, True)
            self.text(dc, f"更短 = 更快    更长 = 更慢\n每个 raw count：{setting.sensitivity * setting.yaw:.6g}°", right + 320, 254, available - 338, 60, 14, MUTED)
        except ValueError as error:
            self.text(dc, "请检查输入", right + 24, 211, available - 48, 38, 26, YELLOW, True)
            self.text(dc, str(error), right + 24, 261, available - 48, 52, 15, YELLOW)
        self.text(dc, "约 3 分半钟 · 3 档候选 × 2 轮重复", right, 349, available, 34, 20, WHITE, True)
        tips: list[tuple[str, str]] = [
            ("01  保持条件稳定", "固定鼠标 DPI、握姿、坐姿与鼠标垫；在桌面独立测试，不启动游戏。"),
            ("02  先热身，再比较", "围绕基准比较 0.8 / 1.0 / 1.25 倍 cm/360°，每轮顺序随机。"),
            ("03  你始终控制鼠标", "只有点击“捕获并开始”后接收 Raw Input。Esc 或切走窗口立即释放。")]
        for index, (title, body) in enumerate(tips):
            y: int = 400 + index * 73
            self.text(dc, title, right, y, available, 26, 17, TEAL, True)
            self.text(dc, body, right, y + 29, available, 42, 14, MUTED)
        self.text(dc, "简化角度平面测评 ≠ 游戏模拟器。VALORANT Yaw 0.07 为常见第三方参数。\n不保证开镜、FOV、拉伸分辨率或两款游戏的整体手感一致。", right, self.height - 109, available, 63, 13, YELLOW)

    def viewport(self) -> tuple[int, int, int, int]:
        return (34, 227, self.width - 34, self.height - 72)

    def draw_test(self, dc: int) -> None:
        complete: bool = bool(self.plan) and self.trial_index >= len(self.plan)
        trial: Trial | None = self.plan[self.trial_index] if self.plan and not complete else None
        title: str = "热身 · 不计分" if not self.warmed else "正式测评"
        self.text(dc, title, 34, 131, 390, 45, 28, WHITE, True)
        self.text(dc, f"已完成 {len(self.records)} / 6 组  ·  每组 3 × 10 秒  ·  Esc 随时退出捕获",
                  420, 141, self.width - 454, 32, 15, MUTED)
        if trial and self.warmed:
            subtitle: str = f"第 {trial.round_number} / 2 轮  ·  候选顺序 {self.trial_index % 3 + 1} / 3  ·  参数暂时隐藏，减少期待偏差"
        else:
            subtitle = "固定中心准星；移动鼠标，让目标进入准星。定位/微调时左键点击；跟踪时无需按键。"
        self.text(dc, subtitle, 34, 188, self.width - 68, 28, 16, TEAL)
        left, top, right, bottom = self.viewport()
        self.box(dc, left, top, right - left, bottom - top, "111D29", BORDER)
        center_x: float = (left + right) / 2
        center_y: float = (top + bottom) / 2
        scale: float = (right - left) / 72.0  # Explicit angular plane, NOT game FOV simulation.
        gdi32.SaveDC(dc)
        gdi32.IntersectClipRect(dc, left + 2, top + 2, right - 2, bottom - 2)
        for delta in range(-8, 9):
            x: float = center_x + (delta * 6 - self.view_x % 6) * scale
            y: float = center_y + (delta * 6 - self.view_y % 6) * scale
            self.line(dc, x, top, x, bottom, "1A2B3B")
            self.line(dc, left, y, right, y, "1A2B3B")
        if self.active:
            tx: float = center_x + (self.target_x - self.view_x) * scale
            ty: float = center_y + (self.target_y - self.view_y) * scale
            radius: float = self.radius() * scale
            self.circle(dc, tx, ty, radius + 5, "18372F", "2D6556")
            self.circle(dc, tx, ty, radius, "4FB697", TEAL)
            if tx < left or tx > right or ty < top or ty > bottom:
                arrow_x: float = min(right - 22, max(left + 22, tx))
                arrow_y: float = min(bottom - 22, max(top + 22, ty))
                self.circle(dc, arrow_x, arrow_y, 7, YELLOW, YELLOW)
                self.text(dc, "目标在边缘方向", left + 20, bottom - 35, 240, 26, 14, YELLOW)
            cross_color: str = TEAL if time.perf_counter() < self.hit_flash else WHITE
            self.line(dc, center_x - 11, center_y, center_x - 4, center_y, cross_color, 2)
            self.line(dc, center_x + 4, center_y, center_x + 11, center_y, cross_color, 2)
            self.line(dc, center_x, center_y - 11, center_x, center_y - 4, cross_color, 2)
            self.line(dc, center_x, center_y + 4, center_x, center_y + 11, cross_color, 2)
            self.circle(dc, center_x, center_y, 1, cross_color, cross_color)
            now: float = time.perf_counter()
            if now < self.countdown_until:
                self.text(dc, f"准备  {math.ceil(self.countdown_until - now)}", left, top + 42,
                          right - left, 48, 32, YELLOW, True, True)
            duration: float = 8.0 if self.warmup else STAGE_SECONDS
            self.text(dc, f"{STAGES[self.stage_index]}   |   {max(0.0, duration - self.elapsed):04.1f} s",
                      left + 22, top + 20, 580, 32, 20, WHITE, True)
            packets: int = self.raw.relative_packets if self.raw else 0
            counts: int = self.raw.total_counts if self.raw else 0
            self.text(dc, f"RAW  {packets} 包 / {counts} counts", right - 370, top + 22, 348, 28, 14, TEAL)
            self.box(dc, left + 20, bottom - 15, max(1, int((right - left - 40) * self.elapsed / duration)), 4, TEAL, radius=2)
        else:
            self.text(dc, "测评结束" if complete else "准备好后，再捕获鼠标", left, top + 59, right - left, 52, 32, WHITE, True, True)
            self.text(dc, self.capture_status, left + 90, top + 133, right - left - 180, 66, 17, MUTED, center=True)
            if complete:
                self.button(dc, "查看本次结果", "results", int(center_x) - 120, int(center_y) + 70, 240, True)
            elif not self.plan:
                self.button(dc, "先设置基准", "setup", int(center_x) - 120, int(center_y) + 70, 240, True)
            else:
                label: str = "捕获并开始热身（8 秒）" if not self.warmed else "捕获并开始 / 重试当前组"
                self.button(dc, label, "capture", int(center_x) - 180, int(center_y) + 70, 360, True)
            self.text(dc, "WM_INPUT 原生相对输入  ·  无注入 / 无自动瞄准  ·  角度平面近似",
                      left + 40, bottom - 43, right - left - 80, 28, 14, MUTED, center=True)
        gdi32.RestoreDC(dc, -1)

    def draw_results(self, dc: int) -> None:
        result: dict[str, Any] = summarize(self.settings, self.records)
        self.text(dc, "你的灵敏度对照", 34, 129, 600, 45, 28, WHITE, True)
        self.text(dc, "导入的历史记录" if self.loaded else f"已完成 {len(self.records)} / 6 组正式测评 · 热身与中断不计分",
                  34, 181, self.width - 68, 27, 15, MUTED)
        recommended: list[float] | None = result["recommended_range"]
        self.box(dc, 34, 222, self.width - 68, 130, PANEL2)
        heading: str = "还没有个人测评结论"
        detail: str = result["message"]
        if recommended:
            lower, upper = recommended
            heading = f"建议验证  {lower:.2f}" + (f" – {upper:.2f}" if upper != lower else "") + " cm/360°"
            valorant: str = f"{sensitivity_for(upper, self.settings.dpi, 0.07):.4f} – {sensitivity_for(lower, self.settings.dpi, 0.07):.4f}"
            cs2: str = f"{sensitivity_for(upper, self.settings.dpi, 0.022):.4f} – {sensitivity_for(lower, self.settings.dpi, 0.022):.4f}"
            detail += f"\n在 {self.settings.dpi:g} DPI 下，VALORANT：{valorant}   |   CS2（默认 .022）：{cs2}"
        elif result["leader"]:
            heading = f"暂时领先：候选 {result['leader']}  ·  不是确定最优"
        self.text(dc, heading, 56, 241, self.width - 112, 45, 27, TEAL, True)
        self.text(dc, detail, 56, 293, self.width - 112, 54, 14, MUTED)
        widths: list[int] = [0, 165, 305, 420, 535, 650, 765, 880]
        labels: list[str] = ["候选 / cm360", "本游戏 sens", "定位", "微调", "跟踪", "综合", "命中/误击", "重复差 / 组数"]
        for x, label in zip(widths, labels):
            self.text(dc, label, 54 + x, 374, 175, 27, 14, MUTED)
        for index, row in enumerate(result["rows"]):
            y: int = 410 + index * 46
            self.box(dc, 34, y - 4, self.width - 68, 41, PANEL if index % 2 == 0 else BG, radius=8)
            values: list[str] = [f"{row['code']}   {row['distance_cm']:.2f}", f"{row['sensitivity']:.5f}"]
            values.extend([f"{value:.1f}" for value in row["stage_scores"]] if row["stage_scores"] else ["—", "—", "—"])
            values.extend([f"{row['score']:.1f}" if row["score"] is not None else "—",
                           f"{row['hits']} / {row['misses']}" if row["completed"] else "—",
                           (f"{row['spread']:.1f}" if row["spread"] is not None else "—") + f" / {row['completed']}"])
            for x, value in zip(widths, values):
                self.text(dc, value, 54 + x, y + 1, 170, 27, 16, TEAL if x == 650 else WHITE)
        self.text(dc, "0–100 启发式评分，三阶段等权；差 ≤4 分保留范围，重复差 >12 分扩大范围。仅两轮，不是统计显著性。",
                  34, 555, self.width - 68, 31, 13, YELLOW)
        self.text(dc, "舒适度备注（可选，不改变客观分数）", 52, self.height - 169, self.width - 104, 26, 14, MUTED)
        self.button(dc, "重新测评", "restart", 34, self.height - 89, 158, True)
        self.button(dc, "导出 JSON", "export", 208, self.height - 89, 158)
        self.button(dc, "打开历史 JSON", "import", 382, self.height - 89, 180)
        self.text(dc, "距离匹配只保证水平转角/物理距离；不保证两款游戏 FOV 或开镜手感。",
                  584, self.height - 84, self.width - 618, 45, 13, MUTED)

    def draw_help(self, dc: int) -> None:
        self.text(dc, "方法透明，结论保持克制", 34, 130, self.width - 68, 45, 28, WHITE, True)
        self.box(dc, 34, 195, self.width - 68, self.height - 251)
        sections: list[tuple[str, str]] = [
            ("01 / 真实相对输入", "仅 WM_INPUT 的相对 raw counts 控制角度；绝对设备包不计入。固定准星，目标按角度平面投影。\n转角 = raw count × sens × yaw；DPI 不改变已填写 sens 的每 count 增益。此处不是游戏透视/FOV模拟器。"),
            ("02 / 换算与测评", "cm/360° = 914.4 ÷ (DPI × sens × yaw)。CS2 默认 yaw .022；VALORANT .07 为第三方常用值，非官方保证。\n8 秒热身不计分；3 档距离 × 2 轮随机顺序；每组定位、微调、跟踪各 10 秒。卡顿、失焦、Esc 均作废当前组。"),
            ("03 / 分数不是科学证明的最优值", "点击分 = 100 × min(命中/6, 1) × 准确率 × [0.55 + 0.45 × max(0, 1−命中耗时中位数/2秒)]。\n跟踪分 = 准星在目标内的有效时间占比 ×100；三阶段等权。0 次命中不得分；未完成两轮不给确定推荐。"),
            ("04 / 本地隐私与适用范围", "不读取/写入游戏，不注入，不模拟射击，不自动瞄准，不更改鼠标系统设置。只测普通腰射的简化任务。\n结果仅在本地 results/latest.json 保存，也可导出 JSON；没有后台上传。舒适度仅作为备注。"),
            ("05 / 研究依据 · 可核查链接", "微软 Raw Input：learn.microsoft.com/en-us/windows/win32/dxtecharts/taking-advantage-of-high-dpi-mouse-movement\n距离换算：gamingsmart.com/mouse-sensitivity-converter/   |   CS2 yaw：totalcsgo.com/commands/myaw\nVALORANT yaw（第三方）：sensconverter.org/game/valorant/。距离相同不代表 FOV / ADS / 拉伸 / 后坐力相同。")]
        for index, (title, body) in enumerate(sections):
            y: int = 211 + index * 96
            self.text(dc, title, 54, y, self.width - 108, 29, 17, TEAL, True)
            self.text(dc, body, 54, y + 33, self.width - 108, 58, 14, MUTED)

    def action(self, action: str) -> None:
        try:
            if action in ("setup", "test", "results", "help"):
                self.page = action
            elif action in ("prepare", "restart"):
                if self.records and user32.MessageBoxW(self.hwnd, "重新测评会清除界面中的本次记录。需要保留请先导出 JSON。继续吗？", "重新测评", 0x24) != 6:
                    return
                self.settings = self.read_settings()
                self.plan = make_plan(self.settings)
                self.records = []
                self.stage_records = []
                self.trial_index = 0
                self.aborted = 0
                self.warmed = False
                self.loaded = False
                self.page = "test"
                self.capture_status = "先进行 8 秒不计分热身。移动鼠标瞄准圆点并左键点击。Esc 可以随时退出。"
                user32.SetWindowTextW(self.controls["comfort"], "")
            elif action == "capture":
                self.start_capture()
            elif action == "export":
                if not self.records:
                    raise ValueError("还没有完成的正式测评记录，不能导出个人结果。")
                path: str | None = choose_json(self.hwnd, True)
                if path:
                    save_payload(Path(path), self.payload())
                    self.status = f"已导出：{path}"
            elif action == "import":
                path = choose_json(self.hwnd, False)
                if path:
                    settings, records, comfort = load_payload(Path(path))
                    self.settings, self.records = settings, records
                    self.plan = []
                    self.loaded = True
                    self.warmed = False
                    self.apply_settings()
                    user32.SetWindowTextW(self.controls["comfort"], comfort)
                    self.status = "已打开历史结果；所有汇总均由原始记录重新计算。历史不能接续测评。"
                    self.page = "results"
            self.place_controls()
            self.invalidate()
        except (ValueError, OSError) as error:
            if self.smoke_test:
                raise
            user32.MessageBoxW(self.hwnd, str(error), "请检查", 0x30)

    def apply_settings(self) -> None:
        user32.SendMessageW(self.controls["game"], 0x14E, list(GAME_YAWS).index(self.settings.game), 0)
        for name, value in (("dpi", self.settings.dpi), ("sens", self.settings.sensitivity), ("yaw", self.settings.yaw)):
            user32.SetWindowTextW(self.controls[name], f"{value:g}")
        user32.EnableWindow(self.controls["yaw"], self.settings.game == "Counter-Strike 2")

    def start_capture(self) -> None:
        """The only regular capture entrypoint: the user's explicit start click."""
        if self.active or not self.plan or self.trial_index >= len(self.plan) or self.raw is None:
            return
        user32.SetFocus(self.hwnd)
        self.warmup = not self.warmed
        self.current_sensitivity = self.settings.sensitivity if self.warmup else self.plan[self.trial_index].candidate.sensitivity
        left, top, right, bottom = self.viewport()
        top_left = wt.POINT(left + 2, top + 2)
        bottom_right = wt.POINT(right - 2, bottom - 2)
        user32.ClientToScreen(self.hwnd, ct.byref(top_left))
        user32.ClientToScreen(self.hwnd, ct.byref(bottom_right))
        rect = wt.RECT(top_left.x, top_left.y, bottom_right.x, bottom_right.y)
        self.raw.start(rect)
        self.stage_records = []
        self.stage_index = 0
        self.active = True
        self.start_stage()
        self.status = "鼠标已捕获 · Esc / 切换应用即释放 · 没有系统设置变更"
        self.invalidate()

    def start_stage(self) -> None:
        self.view_x = 0.0
        self.view_y = 0.0
        self.elapsed = 0.0
        self.tracking_on = 0.0
        self.hits = 0
        self.misses = 0
        self.latencies = []
        round_number: int = self.plan[self.trial_index].round_number if self.plan else 1
        self.random = random.Random(round_number * 101 + self.stage_index)
        now: float = time.perf_counter()
        self.last_tick = now
        self.countdown_until = now + 1.0
        self.target_spawn = self.countdown_until
        self.spawn_target(reset_clock=False)

    def spawn_target(self, reset_clock: bool = True) -> None:
        amplitude: float = 10.0 if self.stage_index == 0 else 3.0
        angle: float = self.random.uniform(0.0, 2 * math.pi)
        self.target_x = self.view_x + math.cos(angle) * amplitude
        self.target_y = self.view_y + math.sin(angle) * amplitude * 0.50
        if reset_clock:
            self.target_spawn = time.perf_counter()

    def radius(self) -> float:
        return (1.0, 0.42, 1.2)[self.stage_index]

    def on_target(self) -> bool:
        return math.hypot(self.target_x - self.view_x, self.target_y - self.view_y) <= self.radius()

    def on_raw(self, dx: int, dy: int, left_down: bool) -> None:
        if not self.active:
            return
        self.view_x += raw_angle(dx, self.current_sensitivity, self.settings.yaw)
        self.view_y += raw_angle(dy, self.current_sensitivity, self.settings.yaw)
        now: float = time.perf_counter()
        duration: float = 8.0 if self.warmup else STAGE_SECONDS
        if now < self.countdown_until or now >= self.countdown_until + duration:
            return
        if left_down and self.stage_index < 2:
            if self.on_target():
                self.hits += 1
                self.latencies.append(max(0.0, now - self.target_spawn))
                self.hit_flash = now + 0.1
                self.spawn_target()
            else:
                self.misses += 1

    def tick(self) -> None:
        if not self.active:
            return
        if user32.GetForegroundWindow() != self.hwnd:
            self.abort("检测到窗口失焦，已释放鼠标；当前候选作废。")
            return
        now: float = time.perf_counter()
        dt: float = now - self.last_tick
        self.last_tick = now
        if dt > 0.25:
            self.abort("检测到明显卡顿（>250 ms），本组已作废，以避免污染计分。")
            return
        if now < self.countdown_until:
            self.invalidate()
            return
        duration: float = 8.0 if self.warmup else STAGE_SECONDS
        dt = min(dt, now - self.countdown_until, duration - self.elapsed)
        self.elapsed += max(0.0, dt)
        if self.stage_index == 2:
            self.target_x = 12.0 * math.sin(self.elapsed * 0.8)
            self.target_y = 4.0 * math.sin(self.elapsed * 1.3 + 0.6)
            if self.on_target():
                self.tracking_on += max(0.0, dt)
        if self.elapsed >= duration - 1e-8:
            self.finish_stage()
        self.invalidate()

    def finish_stage(self) -> None:
        if self.warmup:
            self.active = False
            if self.raw:
                self.raw.stop()
            self.warmed = True
            self.capture_status = "热身完成，不计入任何分数。接下来每组依次完成：定位点击 → 小目标微调 → 连续跟踪。"
            self.status = "已释放鼠标 · 可休息后点击开始正式测评"
            return
        self.stage_records.append(StageRecord(STAGES[self.stage_index], self.hits, self.misses,
                                              tuple(self.latencies), self.tracking_on, STAGE_SECONDS))
        if self.stage_index < 2:
            self.stage_index += 1
            self.start_stage()
            return
        self.active = False
        counts: int = self.raw.total_counts if self.raw else 0
        if self.raw:
            self.raw.stop()
        if counts == 0:
            self.stage_records = []
            self.capture_status = "本组未收到真实相对鼠标移动，不保存结果。请使用物理鼠标，检查远程桌面/触控板输入后重试。"
            self.aborted += 1
            return
        trial: Trial = self.plan[self.trial_index]
        record: TrialRecord = TrialRecord(trial.candidate.code, trial.round_number,
                                          trial.candidate.distance_cm, trial.candidate.sensitivity,
                                          tuple(self.stage_records)).validate()
        self.records.append(record)
        self.trial_index += 1
        self.stage_records = []
        self.capture_status = "本组已完成并记录。放松手腕；准备好后再开始下一组，候选顺序已随机。"
        self.status = "已释放鼠标 · 当前结果只包含完整完成的正式测评组"
        self.save_latest()
        if self.trial_index >= len(self.plan):
            self.page = "results"
            self.place_controls()

    def abort(self, reason: str) -> None:
        """Discard every stage of the interrupted trial without advancing its slot."""
        if not self.active:
            return
        self.active = False
        if self.raw:
            self.raw.stop()
        self.stage_records = []
        self.elapsed = 0.0
        self.aborted += 1
        self.capture_status = reason
        self.status = "已安全释放鼠标 · 中断的当前组没有进入结果"
        self.invalidate()

    def payload(self) -> dict[str, Any]:
        return make_payload(self.settings, self.records, self.get_text("comfort"), self.aborted)

    def save_latest(self) -> None:
        if self.smoke_test or not self.records:
            return
        try:
            save_payload(self.history_path, self.payload())
            self.status = f"本地自动保存：{self.history_path}"
        except (OSError, ValueError) as error:
            self.status = "自动保存失败；记录仍在内存，请从结果页导出 JSON 到可写位置。"
            user32.MessageBoxW(self.hwnd, self.status + "\n\n" + str(error), "保存提醒", 0x30)

    def run_smoke(self) -> None:
        """Native integration probe: no injection, no scoring, no history writes."""
        self.plan = make_plan(self.settings, 7)
        self.page = "test"
        self.place_controls()
        self.start_capture()
        self.smoke_result["raw_registration"] = bool(self.raw and self.raw.active)
        user32.SendMessageW(self.hwnd, 0x0100, 0x1B, 0)
        self.smoke_result["escape_release"] = not self.active and bool(self.raw and not self.raw.active)
        self.start_capture()
        user32.SendMessageW(self.hwnd, 0x001C, 0, 0)
        self.smoke_result["focus_release"] = not self.active and bool(self.raw and not self.raw.active)
        self.smoke_result["no_scored_records"] = not self.records
        self.smoke_result["same_trial_after_abort"] = self.trial_index == 0 and not self.stage_records
        for page in ("setup", "test", "results", "help"):
            self.page = page
            self.place_controls()
            self.invalidate()
            user32.UpdateWindow(self.hwnd)
        self.smoke_result["all_pages_rendered"] = not bool(self.fatal_error)
        self.start_capture()
        user32.SendMessageW(self.hwnd, 0x0010, 0, 0)
        self.smoke_result["close_release"] = not self.active and bool(self.raw and not self.raw.active)
        self.smoke_result["history_written"] = False
