"""Two-minute native assessment whose sensitivity follows your own scores.

Plan: 2 s warm-up + 2 blocks x (three 20 s tasks). Every task is cut into ten two-second
scoring windows. At each window boundary the score you just earned is fed to a hill climber,
which decides where to move the sensitivity next: keep going while you improve, turn around
and shorten the stride when you get worse. The sensitivity therefore drifts up and down on
its own while you click, hunting for the setting you aim best at.

Block 1 starts at the fast end of the band and block 2 at the slow end, so the two blocks
approach the peak from opposite directions and single-run noise is damped by pooling both.
"""
from __future__ import annotations

import ctypes as ct
import math
import random
import time
from ctypes import wintypes as wt
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from adaptive import (BLOCK_SECONDS, BUDGET_SECONDS, HILL_FAST_FACTOR, HILL_IMPROVEMENT,
                      HILL_SLOW_FACTOR, HIT_TIMEOUT_SECONDS, MAX_BLOCKS, MIN_COMPOSITE, PEAK_MARGIN,
                      TASK_SECONDS, TASK_SEED, WARMUP_SECONDS, WINDOW_SECONDS, WINDOWS_PER_TASK,
                      ZERO_WINDOW_CURVE, AdaptiveController, BlockRecord, BlockRequest, HillClimb,
                      TaskSample, TrackingIntegral, click_window_score, load_adaptive,
                      tracking_target)
from core import STAGES, Settings, clamp, raw_angle, save_payload
from native import SOUNDS, choose_json, gdi32, play_sound, user32
from win32_app import BG, BORDER, MUTED, PANEL, PANEL2, TEAL, WHITE, YELLOW, DesktopApp


def band_label() -> str:
    """The cm/360 band the climber may roam, expressed for the user."""
    return (f"灵敏度会在 cm/360° ×{HILL_SLOW_FACTOR:g}–×{HILL_FAST_FACTOR:g}"
            f"（−{(1 - HILL_SLOW_FACTOR) * 100:.0f}% 至 +{(HILL_FAST_FACTOR - 1) * 100:.0f}%）之间"
            f"按你的成绩自动上下调整，每 {WINDOW_SECONDS:g} 秒重新判断一次方向")


class AdaptiveApp(DesktopApp):
    """Score-driven sensitivity climb; a single capture spans the whole run."""
    def __init__(self, smoke_test: bool = False, perf_report: Path | None = None,
                 autotest: bool = False, autotest_seed: int = 20260928,
                 autotest_output: Path | None = None, autotest_fast: bool = False) -> None:
        super().__init__(smoke_test, perf_report)
        self.autotest: bool = bool(autotest)
        self.autotest_seed: int = int(autotest_seed)
        self.autotest_output: Path = Path(autotest_output or "autotest_output")
        self.autotest_fast: bool = bool(autotest_fast)
        self.autotest_driver: Any | None = None
        self._autotest_real_origin = time.perf_counter()
        self._autotest_clock_origin = self._autotest_real_origin
        self.controller: AdaptiveController | None = None
        self.current_request: BlockRequest | None = None
        self.task_samples: list[TaskSample] = []
        self.spent: float = 0.0
        self.segment_started: float = 0.0
        self.block_started: float = 0.0
        # Task-local clock, stored in *active-budget* seconds (same units as
        # ``active_elapsed``), never in raw perf_counter seconds.
        self.task_start: float = 0.0
        self.block_timestamp: str = ""
        self.task_raw_counts: int = 0
        self.climb: HillClimb | None = None
        self.block_climbs: list[HillClimb] = []
        self.tracking_integral: TrackingIntegral | None = None
        self.window_hits: list[int] = []
        self.window_misses: list[int] = []
        # Per-window click detail, so the speed term can be re-derived from a stored record.
        self.window_gaps: list[list[float]] = []
        self.window_timeouts: list[int] = []
        # The score the climber was actually fed per window, kept for the audit trail: it is
        # the input half of the feedback loop, and without it a stored record could not show
        # *why* the sensitivity moved the way it did.
        self.window_scores_observed: list[float] = []
        # Interval from the previous successful hit, and how long the current target has been
        # on screen without being hit. Both are what the speed score is built from.
        self.last_hit_time: float | None = None
        self.window_start_time: float = 0.0
        # Raw mice can report 1,000–8,000 packets per second.  The scoring
        # clock needs 1 ms precision, not a complete session walk for every
        # packet inside the same millisecond.
        self.last_session_advance: float = 0.0
        # Absolute anchor of the current stage's window grid; see ``task_local``.
        self.window_grid_start: float = 0.0
        self.history_path = self.base_dir / "results" / "adaptive-latest.json"
        self.capture_status = "点击一次开始：灵敏度会跟着你的成绩自动上下调整，你只管瞄准。"
        self.sound_on: bool = True
        # 最近一次命中（视角坐标与时刻）：绘制命中扩散光圈用，纯装饰不参与计分。
        self.hit_mark: tuple[float, float, float] | None = None
        self.band_note: str = band_label()

    def now(self) -> float:
        """Production perf-counter clock; QA fast mode scales only the isolated test clock."""
        real_now = time.perf_counter()
        if self.autotest and self.autotest_fast:
            return self._autotest_clock_origin + (real_now - self._autotest_real_origin) * 10.0
        return real_now

    def run_loop(self, message: wt.MSG) -> None:
        """Enable the isolated QA driver only on the explicit command-line path."""
        try:
            if self.autotest:
                from autotest_driver import AutotestDriver
                from autotest_capture import WindowCapture
                self.autotest_output.mkdir(parents=True, exist_ok=True)
                runtime = self.autotest_output / ".runtime"
                if runtime.exists():
                    import shutil
                    shutil.rmtree(runtime)
                self.history_path = runtime / "adaptive-latest.json"
                for name in ("frames", "keyframes", "result", "logs"):
                    path = self.autotest_output / name
                    if path.exists():
                        import shutil
                        shutil.rmtree(path)
                for name in ("autotest_events.jsonl", "autotest_summary.json",
                             "autotest_manifest.json", "README_AUTOTEST.txt", "autotest_seed.json"):
                    path = self.autotest_output / name
                    if path.exists():
                        path.unlink()
                user32.ShowWindow(self.hwnd, 9)
                # Keep the real window visible above the workspace without stealing keyboard
                # focus or moving the physical cursor. QA relative input is injected internally.
                from native import bind
                bind(user32, "SetWindowPos", wt.BOOL,
                     [wt.HWND, wt.HWND, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.UINT])
                if not user32.SetWindowPos(self.hwnd, wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010):
                    raise ct.WinError(ct.get_last_error())
                self.autotest_capture = WindowCapture(self.hwnd, self.width, self.height,
                                                       self.autotest_output, fps=12)
                self.autotest_capture.start()
                self.autotest_capture.keyframe("01_settings")
                self.invalidate()
                user32.UpdateWindow(self.hwnd)
                user32.SetWindowTextW(self.controls["dpi"], "800")
                user32.SetWindowTextW(self.controls["sens"], "0.35")
                user32.SendMessageW(self.controls["game"], 0x14E, 0, 0)
                self.autotest_driver = AutotestDriver(self, self.autotest_seed,
                                                       self.autotest_output,
                                                       self.autotest_capture,
                                                       fast=self.autotest_fast)
                self.autotest_driver.prepare()
                if not user32.SetTimer(self.hwnd, 3, 100, None):
                    raise ct.WinError(ct.get_last_error())
            super().run_loop(message)
        finally:
            if self.autotest:
                user32.KillTimer(self.hwnd, 3)
            if self.autotest_driver:
                self.autotest_driver.close()
            capture = getattr(self, "autotest_capture", None)
            if capture:
                capture.stop()

    def inject_qa_relative(self, dx: int, dy: int, left_down: bool = False) -> None:
        """QA-only relative input hook; decoded by the same production scoring function."""
        if not self.autotest:
            raise RuntimeError("QA relative input is available only with --autotest")
        self.on_raw(dx, dy, left_down)

    def tick(self) -> None:
        super().tick()
        if self.autotest_driver:
            self.autotest_driver.tick()

    def active_elapsed(self, now: float | None = None) -> float:
        current: float = self.now() if now is None else now
        return min(BUDGET_SECONDS, self.spent + (max(0.0, current - self.segment_started) if self.active else 0.0))

    def window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if self.autotest and message in (0x0006, 0x0008, 0x001C, 0x001F, 0x0215):
            # QA uses internal relative injection without ClipCursor or cursor hiding, so
            # external app focus/capture notifications must not abort the isolated run.
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)
        if message == 0x0113 and wparam == 3 and self.autotest_driver:
            try:
                self.autotest_driver.tick()
            except Exception:
                self.fatal_error = __import__("traceback").format_exc()
                self.autotest_driver.log_line("QA timer failure: " + self.fatal_error)
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
            return 0
        if message == 0x0010 and getattr(self, "active", False):
            self.abort("窗口关闭，未完成块作废")
            if self.controller:
                self.controller.finish("用户关闭窗口")
                self.save_latest()
        return super().window_proc(hwnd, message, wparam, lparam)

    def action(self, action: str) -> None:
        try:
            if action in ("prepare", "restart"):
                if self.controller and self.controller.records and user32.MessageBoxW(
                        self.hwnd, "重新测试会清空界面记录。需要保留请先导出 JSON。继续吗？", "重新测评", 0x24) != 6:
                    return
                self.settings = self.read_settings()
                self.controller = AdaptiveController(self.settings)
                self.band_note = band_label()
                self.current_request = None
                self.records = []
                self.loaded = False
                self.climb = None
                self.current_sensitivity = self.settings.sensitivity
                self.warmed = False
                self.spent = 0.0
                self.aborted = 0
                self.page = "test"
                user32.SetWindowTextW(self.controls["comfort"], "")
                self.place_controls()
                self.start_capture()
            elif action == "capture":
                self.start_capture()
            elif action == "end":
                self.end_session("用户提前结束；未完成最终确认")
            elif action == "export":
                if not self.controller or not self.controller.records:
                    raise ValueError("尚无测试块，不能导出个人结果。")
                path = choose_json(self.hwnd, True)
                if path:
                    save_payload(Path(path), self.payload())
                    self.status = f"已导出：{path}"
            elif action == "import":
                path = choose_json(self.hwnd, False)
                if path:
                    # ``load_adaptive`` is the single validation entry point: it enforces the
                    # 2 MB cap and rejects every older schema with an accurate message, so the
                    # UI does not keep a second, easily-stale copy of those rules.
                    self.controller, self.spent, comfort = load_adaptive(Path(path))
                    self.settings = self.controller.settings
                    self.status = "已读取自适应历史并重新计算证据；历史不能继续计分。"
                    self.loaded = True
                    self.current_request = None
                    self.apply_settings()
                    user32.SetWindowTextW(self.controls["comfort"], comfort)
                    self.page = "results"
            elif action == "sound":
                self.sound_on = not self.sound_on
            elif action in ("setup", "test", "results", "help"):
                self.page = action
                if action == "setup":
                    self.band_note = band_label()
            self.place_controls()
            self.invalidate()
        except (ValueError, OSError) as error:
            if self.smoke_test:
                raise
            user32.MessageBoxW(self.hwnd, str(error), "请检查", 0x30)

    def start_capture(self) -> None:
        if self.active or self.raw is None or self.controller is None or self.controller.ended or self.loaded:
            return
        if self.spent >= BUDGET_SECONDS:
            self.end_session("活动预算已用完")
            return
        # An explicit resume is always honoured; the remaining budget is shown to the user,
        # and only the automatic advance into the next block enforces the budget guard.
        short: bool = BUDGET_SECONDS - self.spent + 0.001 < BLOCK_SECONDS
        self.page = "test"
        self.place_controls()
        if not self.autotest:
            user32.SetFocus(self.hwnd)
            left, top, right, bottom = self.viewport()
            a, b = wt.POINT(left + 2, top + 2), wt.POINT(right - 2, bottom - 2)
            user32.ClientToScreen(self.hwnd, ct.byref(a))
            user32.ClientToScreen(self.hwnd, ct.byref(b))
            self.raw.start(wt.RECT(a.x, a.y, b.x, b.y))
        self.segment_started = self.now()
        self.segment_wall = datetime.now(timezone.utc)
        self.active = True
        self.warmup = not self.warmed
        self.task_samples = []
        if self.warmup:
            self.stage_index = 0
            self.climb = None
            self.current_sensitivity = self.settings.sensitivity
            self.start_stage(started_at=self.segment_started)
        else:
            self.begin_block(self.segment_started, guard=False)
        self.status = ("剩余预算不足一个完整块；本块可能被到点截断，建议重新开始整轮。"
                       if short else
                       "灵敏度按成绩自动上下调整 · 仅作用于本测试场 · Esc/切走窗口释放并暂停 · 不更改真实游戏/DPI")
        if self.sound_on:
            play_sound("start")
        self.invalidate()

    def update_sensitivity(self, now: float) -> float:
        """Apply whatever gain the climber currently holds.

        The climber is advanced only at *window boundaries* (see ``finish_window``), never
        here: the gain has to stay fixed for a whole window so that window's score can be
        attributed to one known sensitivity.
        """
        if self.climb is not None:
            self.current_sensitivity = self.climb.gain
        return self.current_sensitivity

    def wall_timestamp(self, now: float) -> str:
        return (self.segment_wall + timedelta(seconds=now - self.segment_started)).isoformat()

    def begin_block(self, now: float | None = None, guard: bool = True) -> None:
        if not self.controller:
            return
        now = self.now() if now is None else now
        if guard and BUDGET_SECONDS - self.active_elapsed(now) + 0.001 < BLOCK_SECONDS:
            self.end_session("剩余活动预算不足以完成下一个测评块；本轮证据不完整，请重新开始整轮。")
            return
        self.current_request = self.controller.request()
        if self.current_request is None:
            self.end_session(f"{MAX_BLOCKS} 个测评块已完成")
            return
        self.block_started = self.active_elapsed(now)
        self.block_timestamp = self.wall_timestamp(now)
        self.task_samples = []
        self.block_climbs = []
        self.stage_index = 0
        self.start_stage(prepare=True, started_at=now)

    def start_stage(self, prepare: bool = False, started_at: float | None = None) -> None:
        self.view_x = self.view_y = self.elapsed = self.tracking_on = 0.0
        self.hits = self.misses = self.task_raw_counts = 0
        self.latencies = []
        self.hit_mark = None
        self.random = random.Random(TASK_SEED + self.stage_index)
        now: float = self.now() if started_at is None else started_at
        self.last_tick = now
        self.last_session_advance = now
        # ``countdown_until`` schedules absolute boundaries (perf_counter domain); ``task_start``
        # records the same moment in the consumed-budget domain for the run summary. They are
        # deliberately different clocks, so every window-grid calculation below uses
        # ``countdown_until`` only and never subtracts one domain from the other.
        self.task_start = self.active_elapsed(now)
        self.countdown_until = now
        self.window_grid_start = now
        self.target_spawn = now
        self.window_hits = [0] * WINDOWS_PER_TASK
        self.window_misses = [0] * WINDOWS_PER_TASK
        self.window_gaps = [[] for _ in range(WINDOWS_PER_TASK)]
        self.window_timeouts = [0] * WINDOWS_PER_TASK
        self.window_scores_observed = [0.0] * WINDOWS_PER_TASK
        self.last_hit_time = None
        self.window_start_time = now
        # The warm-up has no climber (it is just a feel-out pass); scored tasks climb.
        if self.warmup:
            self.climb = None
            self.current_sensitivity = self.settings.sensitivity
        else:
            request: BlockRequest | None = self.current_request
            block_index: int = request.index if request else 1
            # Continue from the last sensitivity that was actually used for a complete scoring
            # window.  The climber also calculates a history[10] at the final boundary, but that
            # value never controlled input and must not leak into the next task.
            inherited_gain: float | None = None
            if self.stage_index > 0 and self.task_samples:
                inherited_gain = self.task_samples[-1].window_gains[-1]
            self.climb = (self.controller.climb_for(self.stage_index, block_index, inherited_gain)
                          if self.controller else None)
            if self.climb is not None:
                self.block_climbs.append(self.climb)
            self.update_sensitivity(now)
        self.spawn_target(reset_clock=False)
        self.tracking_integral = TrackingIntegral(now) if self.stage_index == 2 else None
        if self.stage_index == 2:
            self.target_x, self.target_y = tracking_target(0.0)

    def spawn_target(self, reset_clock: bool = True) -> None:
        super().spawn_target(reset_clock)
        if self.autotest:
            # DesktopApp's legacy target clock is a real perf-counter timestamp. QA fast mode
            # scales its isolated active clock, so keep target reaction timing in that domain.
            self.target_spawn = self.now()
        if self.stage_index >= 2:
            return  # 跟踪任务的靶由 tracking_target 路径给出，不走出靶。
        left, top, right, bottom = self.viewport()
        scale: float = (right - left) / 72
        low_x, high_x, low_y, high_y = self.field_span(self.radius() * scale + 6.0)
        self.target_x = clamp(self.target_x, low_x, high_x)
        self.target_y = clamp(self.target_y, low_y, high_y)

    def integrate_tracking(self, now: float) -> None:
        if self.tracking_integral is not None:
            self.tracking_on = self.tracking_integral.advance(now, self.view_x, self.view_y, self.radius())

    def advance_session(self, now: float) -> None:
        """Walk every absolute boundary crossed since the last call.

        Two levels of boundary exist and both must be honoured in order:
        * scoring-window boundaries (every ``WINDOW_SECONDS``) close the current window, score
          it, and let the climber pick the sensitivity for the next one;
        * stage boundaries end the task.
        Timer latency never extends the plan: boundaries are absolute, so a late tick simply
        crosses several of them at once.
        """
        deadline: float = self.segment_started + BUDGET_SECONDS - self.spent
        until: float = min(now, deadline)
        while self.active:
            duration: float = WARMUP_SECONDS if self.warmup else TASK_SECONDS
            stage_end: float = self.countdown_until + duration
            if self.warmup or self.climb is None:
                # The warm-up is one uninterrupted feel-out pass with no scoring windows.
                boundary = stage_end
            else:
                # The next window to close is the one *after* the last one closed, which is
                # ``window_start_time``; deriving it from the target time instead would already
                # name the final window and collapse the whole task into a single step, so ten
                # windows' worth of input would be charged to the last one and thrown away.
                boundary = self.window_start_time + WINDOW_SECONDS
            sample_time: float = min(until, stage_end, boundary)
            self.integrate_tracking(sample_time)
            self.elapsed = max(0.0, min(duration, sample_time - self.countdown_until))
            if self.stage_index == 2:
                self.target_x, self.target_y = tracking_target(self.elapsed)
            # Close the window only once its *own* two seconds have actually elapsed. Comparing
            # the raw sample time against ``window_start_time`` looked equivalent but is not: a
            # window is ``WINDOW_SECONDS`` wide, so a tick a few milliseconds into a freshly
            # started stage already satisfies it. That closed window 0 two seconds early on the
            # very first tick, scored it empty, and pushed every subsequent click into a window
            # that had already been judged -- a flat zero score series for the whole task.
            # ``>=`` on the boundary would instead re-close a window a late tick has already
            # crossed, so each window is closed exactly when its boundary is reached.
            if self.climb is not None and sample_time + 1e-9 >= self.window_start_time + WINDOW_SECONDS:
                self.finish_window(self.window_start_time + WINDOW_SECONDS)
            if sample_time < stage_end:
                break
            self.finish_stage(stage_end)
        if self.active:
            self.update_sensitivity(until)
            self.last_session_advance = until
            if now >= deadline:
                self.end_session("活动预算到点；未完成的尾部不计分")

    def task_local(self, now: float) -> float:
        """Seconds this task has run at ``now``, measured on the task's own grid.

        The grid anchor is captured once per stage in :attr:`window_grid_start` and must not be
        re-read from ``countdown_until`` while the stage runs. ``countdown_until`` is re-pinned
        every time a stage begins, so during the inner walk it already points one stage ahead
        and every comparison against it lands at the *end* of the task.

        This is also why the two clock domains stay separate: ``active_elapsed`` totals consumed
        budget, while this grid counts wall seconds since the stage started. Inside one task
        nothing pauses -- a pause aborts the block -- so the two advance in lockstep, but they
        are still different numbers and subtracting them produced a whole task's worth of local
        time inside a single tick.
        """
        return max(0.0, now - self.window_grid_start)

    def window_index_at(self, now: float) -> int:
        """Scoring window the given moment belongs to, clamped to the last window."""
        return max(0, min(WINDOWS_PER_TASK - 1,
                          int(self.task_local(now) / WINDOW_SECONDS)))

    def window_boundary(self, index: int) -> float:
        """Absolute time at which scoring window ``index`` ends."""
        return self.window_grid_start + (index + 1) * WINDOW_SECONDS

    def finished_window_index(self, boundary: float) -> int:
        """Index of the window that *ended* at ``boundary``.

        Not the same question as :meth:`window_index_at`: a boundary sits exactly on the next
        window's start, so asking "which window is this moment in" names a window that has not
        run yet. The just-ended window is the one before that point.
        """
        position: int = int(math.ceil(self.task_local(boundary) / WINDOW_SECONDS)) - 1
        return max(0, min(WINDOWS_PER_TASK - 1, position))

    def finish_window(self, boundary: float) -> None:
        """Score the window that just ended and let the climber choose the next sensitivity.

        This is the feedback loop. It runs *before* the next window's input is decoded, so the
        gain a window is scored at is decided by the score of the window before it -- which is
        exactly why the recorded gain series cannot be recomputed from the band.
        """
        if self.climb is None:
            return
        index: int = self.finished_window_index(boundary)
        if self.stage_index == 2:
            window_score: float = 100 * clamp(
                self.tracking_integral.window_on[index] / WINDOW_SECONDS
            ) if self.tracking_integral is not None else 0.0
        else:
            window_score = click_window_score(list(self.window_gaps[index]),
                                              self.window_timeouts[index])
        self.window_scores_observed[index] = window_score
        self.climb.observe(window_score)
        # ``window_start_time`` lives in the absolute ``perf_counter`` domain because the tick
        # loop compares it against raw sample times; only the index lookup above converts.
        self.window_start_time = boundary
        self.update_sensitivity(boundary)

    def on_raw(self, dx: int, dy: int, left_down: bool) -> None:
        if not self.active:
            return
        now: float = self.now()
        # Integrate the previous held view before applying this event, but no
        # more often than the integral's 1 ms sampling grid.  At high polling
        # rates this removes thousands of redundant Python clock walks per
        # second without weakening tracking evidence.
        if now - self.last_session_advance >= 0.001:
            self.advance_session(now)
        if not self.active:
            return
        self.update_sensitivity(now)
        self.view_x += raw_angle(dx, self.current_sensitivity, self.settings.yaw)
        self.view_y += raw_angle(dy, self.current_sensitivity, self.settings.yaw)
        if self.stage_index < 2:
            # 固定相机：可视场就是测试场，准星像普通光标一样到屏幕边缘即停，绝不甩出屏幕。
            # 夹取的是绘制用的视角坐标；raw counts 照常累计，命中判定仍用视角差。
            low_x, high_x, low_y, high_y = self.field_span(12.0)
            self.view_x = clamp(self.view_x, low_x, high_x)
            self.view_y = clamp(self.view_y, low_y, high_y)
        if self.warmup:
            return  # Warm-up is usable but never scored.
        self.task_raw_counts += abs(dx) + abs(dy)
        if left_down and self.stage_index < 2:
            # Attribute the click to the scoring window it happened in, so the score can be
            # mapped onto the sensitivity that was actually in effect at that moment.
            bucket: int = self.window_index_at(now)
            if self.on_target():
                # The speed term needs the gap since the *previous hit*, not since this target
                # spawned: that is the `lastTimeHit - nowTimeHit` interval the user asked for.
                gap: float = (max(0.0, now - self.last_hit_time) if self.last_hit_time is not None
                              else max(0.0, now - self.target_spawn))
                self.hits += 1
                self.window_hits[bucket] += 1
                self.window_gaps[bucket].append(gap)
                self.last_hit_time = now
                self.latencies.append(max(0.0, now - self.target_spawn))
                self.hit_flash = now + .1
                self.hit_mark = (self.target_x, self.target_y, now)
                if self.sound_on:
                    play_sound("hit")
                self.spawn_target()
            else:
                self.misses += 1
                self.window_misses[bucket] += 1
                if self.sound_on:
                    play_sound("miss")

    def sweep_timeouts(self, now: float) -> None:
        """Charge a missed-by-timeout interval to the current window.

        A window with no click at all would otherwise score 0 through the empty-gaps path while
        also leaving no evidence of *why*. Recording explicit timeouts keeps the accuracy
        denominator honest and makes "you never clicked" distinguishable from "you clicked and
        missed" in the exported record.
        """
        if self.climb is None or self.stage_index == 2 or self.warmup:
            return
        index: int = self.window_index_at(now)
        reference: float = self.last_hit_time if self.last_hit_time is not None else self.window_start_time
        while now - reference >= HIT_TIMEOUT_SECONDS:
            reference += HIT_TIMEOUT_SECONDS
            self.window_timeouts[index] = self.window_timeouts[index] + 1

    def tick(self) -> None:
        if not self.active:
            return
        now: float = self.now()
        if not self.autotest and user32.GetForegroundWindow() != self.hwnd:
            self.abort("窗口失焦，已释放鼠标；主动点击后重测当前块，继续使用剩余预算。")
            return
        if now - self.last_tick > .25:
            if self.active_elapsed(now) >= BUDGET_SECONDS:
                self.end_session("活动预算到点；卡顿期间的未完成块作废")
            else:
                self.abort("卡顿超过250 ms；当前块作废，鼠标已释放。")
            return
        self.last_tick = now
        self.advance_session(now)
        self.invalidate()

    def make_block(self, valid: bool, reason: str = "", interrupted: bool = False,
                   now: float | None = None) -> BlockRecord:
        if self.current_request is None:
            raise ValueError("当前没有自动测试块。")
        now = self.now() if now is None else now
        request: BlockRequest = self.current_request
        lows: tuple[float, ...] = ()
        highs: tuple[float, ...] = ()
        if valid and self.controller:
            bands: list[tuple[float, float]] = [
                self.controller.band_for_task(index, request.index) for index in range(len(STAGES))]
            lows = tuple(low for low, _ in bands)
            highs = tuple(high for _, high in bands)
        return BlockRecord(request.index, self.settings.dpi, self.settings.yaw, self.block_timestamp,
                           self.wall_timestamp(now), self.block_started, self.active_elapsed(now),
                           tuple(self.task_samples), valid, reason, request.reason,
                           "中断" if interrupted else request.phase,
                           task_band_lows=lows, task_band_highs=highs)

    def finish_stage(self, now: float | None = None) -> None:
        now = self.now() if now is None else now
        if self.warmup:
            self.warmed = True
            self.warmup = False
            self.climb = None
            self.begin_block(now)
            return
        # The task itself has just ended, so score whatever the final, partial window holds
        # before recording: with WINDOW_SECONDS dividing TASK_SECONDS exactly this is a
        # boundary-finish rather than an early cut, but the guard keeps the two in step if the
        # timer ever lands slightly short.
        #
        # The extra guard is "has this window already been closed?" rather than "is its boundary
        # behind us?": on a normal run the walker closes the tenth window itself, and asking only
        # about the boundary would then score that same, empty window a second time -- a
        # duplicate zero dragging the task's mean score down.
        if self.climb is not None and self.window_start_time + WINDOW_SECONDS <= now + 1e-6:
            self.finish_window(self.window_start_time + WINDOW_SECONDS)
        # Every task carries a full window curve: click/settle tasks use the shared zero
        # tracking curve so the four window tuples always line up.
        window_tracking: tuple[float, ...] = ZERO_WINDOW_CURVE
        if self.tracking_integral is not None:
            window_tracking = tuple(self.tracking_integral.window_on)
        # The gain series is what the climber *decided*, in order. It cannot be recomputed
        # from the band because each entry after the first depends on the user's score in the
        # window before it.
        #
        # ``history`` carries one entry more than there are windows: it opens with the starting
        # gain (the value window 0 was measured at) and then appends the value chosen at each
        # of the ten window boundaries. The applied series is therefore the first ten entries --
        # taking the tail instead would drop window 0 and shift every gain by one, silently
        # pairing each score with the sensitivity of the window after it.
        gains: tuple[float, ...] = ()
        if self.climb is not None:
            history: tuple[float, ...] = self.climb.history or (self.climb.gain,)
            gains = (history[:WINDOWS_PER_TASK] if len(history) >= WINDOWS_PER_TASK
                     else tuple(history) + (self.climb.gain,) * (WINDOWS_PER_TASK - len(history)))
        if len(gains) != WINDOWS_PER_TASK:
            raise ValueError("爬坡记录与计分窗口数量不一致。")
        self.task_samples.append(TaskSample(STAGES[self.stage_index], self.hits, self.misses,
                                            tuple(self.latencies), self.tracking_on, TASK_SECONDS,
                                            self.task_raw_counts, tuple(self.window_hits),
                                            tuple(self.window_misses), window_tracking, gains,
                                            tuple(tuple(gaps) for gaps in self.window_gaps),
                                            tuple(self.window_timeouts)).validate())
        if self.stage_index < 2:
            self.stage_index += 1
            self.start_stage(started_at=now)
            return
        valid: bool = all(sample.raw_counts > 0 for sample in self.task_samples)
        record: BlockRecord = self.make_block(valid, "" if valid else "至少一个任务未收到真实相对移动", now=now)
        if self.controller:
            self.controller.accept(record)
        if valid and self.sound_on:
            play_sound("block")
        self.current_request = None
        self.task_samples = []
        if self.controller and self.controller.ended:
            self.end_session(self.controller.end_reason)
        else:
            # Start the next absolute schedule before saving. I/O never extends the budget.
            self.begin_block(now)
            self.save_latest()

    def abort(self, reason: str) -> None:
        if not self.active:
            return
        now: float = self.now()
        self.update_sensitivity(now)
        used: float = self.active_elapsed(now)
        self.climb = None  # Freeze the applied gain; the aborted block is audit-only anyway.
        self.active = False
        self.spent = used
        if self.raw:
            self.raw.stop()
        # Safety takes precedence over record validation and filesystem work.
        if self.controller and self.current_request and not self.warmup:
            self.controller.interrupt(self.make_block(False, reason, interrupted=True))
        self.task_samples = []
        self.stage_records = []
        self.elapsed = 0.0
        self.aborted += 1
        self.capture_status = reason
        self.status = "安全暂停 · 当前不完整块不计分 · 暂停时间不计预算，恢复必须主动点击"
        self.save_latest()
        self.invalidate()

    def end_session(self, reason: str) -> None:
        if self.active:
            self.abort(reason)
        if self.controller:
            self.controller.finish(reason)
        if self.sound_on:
            play_sound("done")
        self.page = "results"
        self.save_latest()
        self.place_controls()
        self.invalidate()

    def payload(self) -> dict[str, Any]:
        if self.controller is None:
            raise ValueError("没有自适应会话。")
        return self.controller.payload(self.active_elapsed(), self.get_text("comfort"))

    def save_latest(self) -> None:
        if self.smoke_test or self.loaded or not self.controller or not self.controller.records:
            return
        try:
            save_payload(self.history_path, self.payload())
            self.status = "每块已本地保存 · 灵敏度按成绩自动调整 · Esc暂停 / 失焦释放"
        except (ValueError, OSError):
            # No modal dialog while captured; never interrupt the deadline safety path.
            self.status = "自动保存失败；记录保留在内存，结束后请导出到可写目录。"

    def draw_setup(self, dc: int) -> None:
        super().draw_setup(dc)
        self.box(dc, 380, 338, self.width - 414, self.height - 383, BG, radius=0)
        self.button(dc, f"捕获并开始自动测评 · {BUDGET_SECONDS:g}秒", "prepare", 54, self.height - 110, 280, True)
        self.button(dc, f"音效：{'开' if self.sound_on else '关'}", "sound",
                    self.width - 300, 126, 266, height=40)
        self.text(dc, "灵敏度跟着你的成绩自动上下调整", 380, 349, self.width - 414, 40, 23, TEAL, True)
        tips: list[tuple[str, str]] = [
            (f"01  {BUDGET_SECONDS:g} 秒自动测评 / 到点自动停止",
             f"2秒热身 + {MAX_BLOCKS}块×三项各{TASK_SECONDS:g}秒 = "
             f"{WARMUP_SECONDS:g}+{MAX_BLOCKS}×{BLOCK_SECONDS:g}={BUDGET_SECONDS:g}秒。暂停不计时。"),
            ("02  灵敏度按你的分数动态变化（关键变化）",
             f"{self.band_note}。打得比上一段好就继续同方向走，变差就掉头并缩小步长，"
             f"所以你不需要自己调参数。"),
            ("03  两块从两端出发，减少单侧偏差",
             "块1 从更快一侧起步、块2 从更慢一侧起步；"
             f"成绩好的一段会被记为待验证候选，各档成绩拉不开差距就保留原设置。")]
        for index, (title, body) in enumerate(tips):
            y: int = 403 + index * 72
            self.text(dc, title, 380, y, self.width - 420, 28, 17, WHITE, True)
            self.text(dc, body, 380, y + 30, self.width - 420, 44, 14, MUTED)
        self.text(dc, "不是后台自动瞄准：你仍需亲自完成任务。单窗口数据量小，最好的一档可能是噪声；"
                      "成绩接近时不会锁定最优，结论最多是待验证候选。\nEsc/失焦即时释放；仅普通腰射角度平面近似，"
                      "不保证FOV/开镜/拉伸手感。",
                  380, self.height - 105, self.width - 420, 64, 13, YELLOW)

    def viewport(self) -> tuple[int, int, int, int]:
        if self.active:
            # Keep the application header visible; the training HUD is painted
            # inside the field.  The bottom trend strip is omitted while
            # active, reclaiming its height without hiding timing information.
            original_height: int = max(1, self.height - 410)
            target_height: int = min(int(original_height * 1.5), max(1, self.height - 40))
            top: int = max(34, self.height - 40 - target_height)
            return (34, top, self.width - 34, self.height - 20)
        return (34, 266, self.width - 34, self.height - 144)

    def field_span(self, pad_px: float) -> tuple[float, float, float, float]:
        """固定相机可视场在视角坐标里的范围 ``(low_x, high_x, low_y, high_y)``。

        场的横向跨度就是绘制用的 72°，纵向随窗口高度换算；``pad_px`` 是边缘留白像素，
        让准星刻线或整个靶心在贴边时仍然完整可见。
        """
        left, top, right, bottom = self.viewport()
        scale: float = (right - left) / 72
        half_x: float = (right - left) / 2 - pad_px
        half_y: float = (bottom - top) / 2 - pad_px
        return (-half_x / scale, half_x / scale, -half_y / scale, half_y / scale)

    def draw_test(self, dc: int) -> None:
        now = self.now()
        if self.active:
            self.update_sensitivity(now)
        remaining: float = max(0.0, BUDGET_SECONDS - self.active_elapsed(now))
        request: BlockRequest | None = self.current_request
        if not self.warmed:
            label: str = "热身 · 不计分"
        elif request is not None:
            label = f"{request.phase} · 灵敏度按成绩自动调整"
        else:
            label = "动态调整测评"
        cm_now: float = 914.4 / (self.settings.dpi * self.current_sensitivity * self.settings.yaw)
        if not self.active:
            self.text(dc, label, 34, 129, 560, 43, 28, WHITE, True)
            self.text(dc, f"剩余 {int(remaining)//60:02d}:{int(remaining)%60:02d}  /  "
                          f"活动预算{int(BUDGET_SECONDS)//60:02d}:{int(BUDGET_SECONDS)%60:02d}",
                      self.width - 420, 139, 386, 30, 19, TEAL, True)
            detail: str = (request.reason if request else
                           f"一次捕获后自动完成 {MAX_BLOCKS} 个测评块；灵敏度按你的成绩自动调整，无需逐档改参数。")
            self.text(dc, detail, 34, 183, self.width - 68, 47, 15, MUTED)
            self.text(dc, f"当前 sens  {self.current_sensitivity:.5f}    |    {cm_now:.2f} cm/360°    |    "
                      + (f"块 {request.index}/{MAX_BLOCKS} · {request.phase}" if request else "先热身，再自动进入测评"),
                      34, 234, self.width - 68, 28, 16, TEAL, True)
        left, top, right, bottom = self.viewport()
        # The active field is repainted at the input cadence.  A newly-sized
        # GDI+ gradient can stall the first captured frame (~90 ms on the
        # benchmark machine), so use the equivalent solid GDI fast path while
        # training; gradients remain on the setup/results pages.
        if self.active:
            self.box(dc, left, top, right-left, bottom-top, "0C1520", outline=BORDER, radius=0)
        else:
            self.gradient_panel(dc, left, top, right-left, bottom-top, "0C1520", "16242F", outline=BORDER)
        cx, cy = (left + right) / 2, (top + bottom) / 2
        scale: float = (right - left) / 72
        if self.active:
            gdi32.SaveDC(dc)
            gdi32.IntersectClipRect(dc, left + 2, top + 2, right - 2, bottom - 2)
            # 射击任务用固定相机：可视场就是测试场（横向 72°），场景静止、准星像光标一样
            # 在屏幕上移动，出靶后不重置（on_raw 已把视角夹在场内，到边即停）。
            # 跟踪任务保持移动相机（准星居中、场景随视角滑动），因为跟踪本身就是转视角。
            # 两种画法只差参考系，命中判定始终用视角差，计分不受影响。
            if self.stage_index < 2:
                hx: float = cx + self.view_x * scale
                hy: float = cy + self.view_y * scale
                tx: float = cx + self.target_x * scale
                ty: float = cy + self.target_y * scale
                gx = gy = 0.0
            else:
                hx, hy = cx, cy
                tx = cx + (self.target_x - self.view_x) * scale
                ty = cy + (self.target_y - self.view_y) * scale
                gx, gy = self.view_x % 6, self.view_y % 6
            for number in range(-8, 9):
                x: float = cx + (number * 6 - gx) * scale
                y: float = cy + (number * 6 - gy) * scale
                self.line(dc, x, top, x, bottom, "8C1A2B3B")
                self.line(dc, left, y, right, y, "8C1A2B3B")
            # 目标出场弹性缩放 + 半透明光晕，命中后一圈扩散光波；均为装饰，不影响判定。
            grow: float = max(0.0, min(1.0, (now - self.target_spawn) / 0.12))
            target_radius: float = self.radius() * scale * (0.35 + 0.65 * grow)
            for factor, alpha in ((1.62, "204FB697"), (1.32, "384FB697")):
                self.circle(dc, tx, ty, target_radius * factor, alpha, alpha)
            self.circle(dc, tx, ty, target_radius, "4FB697", TEAL)
            if self.hit_mark and 0 <= now - self.hit_mark[2] < 0.25:
                wave: float = (now - self.hit_mark[2]) / 0.25
                self.circle(dc, cx + self.hit_mark[0] * scale, cy + self.hit_mark[1] * scale,
                            self.radius() * scale * (1 + 2.5 * wave), "004FB697",
                            f"{int(150 * (1 - wave)):02X}63E1C0")
            if not left < tx < right or not top < ty < bottom:
                self.circle(dc, min(right-15, max(left+15, tx)), min(bottom-15, max(top+15, ty)), 6, YELLOW, YELLOW)
            for a, b, c, d in ((hx-11,hy,hx-4,hy),(hx+4,hy,hx+11,hy),(hx,hy-11,hx,hy-4),(hx,hy+4,hx,hy+11)):
                self.line(dc,a,b,c,d,WHITE,2)
            self.circle(dc, hx, hy, 2.0, TEAL, TEAL)
            duration: float = WARMUP_SECONDS if self.warmup else TASK_SECONDS
            hud_top: int = max(top + 12, 120)
            overall_seconds: int = math.ceil(remaining)
            overall_clock: str = f"{overall_seconds // 60:02d}:{overall_seconds % 60:02d}"
            block_label: str = (f"块 {request.index}/{MAX_BLOCKS}" if request is not None
                                else "热身")
            self.text(dc, f"{block_label}   |   {label}   |   本项剩余 "
                          f"{max(0.0, duration - self.elapsed):.1f}s   |   整轮约剩 {overall_clock}",
                      left + 20, hud_top, right - left - 40, 28, 18, WHITE, True)
            self.text(dc, f"当前 sens {self.current_sensitivity:.5f}   |   {cm_now:.2f} cm/360°",
                      left + 20, hud_top + 31, right - left - 40, 25, 14, TEAL, True)
            task_label: str = "热身 / 不计分" if self.warmup else STAGES[self.stage_index]
            instruction: str = ("持续把目标保持在准星内，灵敏度按成绩调整"
                               if self.stage_index == 2 else "把准星移到目标上点击，灵敏度按成绩调整")
            self.text(dc, f"{task_label}  ·  {instruction}",
                      left+20, hud_top+64, right-left-40, 31, 17, WHITE, True)
            climb_note: str = (f"本窗口灵敏度 {self.current_sensitivity:.5f} → {cm_now:.2f} cm/360°"
                               f"  ·  第 {min(WINDOWS_PER_TASK, int(self.elapsed / WINDOW_SECONDS) + 1)}"
                               f"/{WINDOWS_PER_TASK} 个计分窗口"
                               if not self.warmup else "热身阶段不改变也不记录灵敏度")
            self.text(dc, f"{climb_note}  ·  原生相对输入 {self.task_raw_counts} counts  ·  Esc立即释放",
                      left+20, bottom-34, right-left-40, 26, 13, MUTED)
            gdi32.RestoreDC(dc, -1)
        else:
            self.text(dc, "已暂停，鼠标自由" if self.controller and not self.controller.ended else "准备开始自动测评",
                      left, top+45, right-left, 42, 29, WHITE, True, True)
            self.text(dc, self.capture_status, left+50, top+104, right-left-100, 66, 16, MUTED, center=True)
            if self.controller and not self.controller.ended and not self.loaded:
                self.button(dc,"主动恢复捕获 · 重试当前块","capture",int(cx)-230,bottom-80,285,True)
                self.button(dc,"结束并看结果","end",int(cx)+70,bottom-80,165)
                self.button(dc, f"音效：{'开' if self.sound_on else '关'}", "sound", int(cx)+251, bottom-80, 150)
            else:
                self.button(dc,"返回设置开始新测评","setup",int(cx)-145,bottom-80,290,True)
        if not self.active:
            self.draw_trend(dc, 34, self.height-125, self.width-68, 74, compact=True)

    def draw_trend(self, dc: int, x: int, y: int, width: int, height: int, compact: bool = False) -> None:
        self.box(dc,x,y,width,height,PANEL)
        records: list[BlockRecord] = self.controller.records if self.controller else []
        valid: list[BlockRecord] = [item for item in records if item.valid]
        self.text(dc,"灵敏度—成绩图：横轴灵敏度区间 / 纵轴该窗口得分"
                  f"（{WINDOW_SECONDS:g}秒窗口）" if not compact
                  else "灵敏度—成绩图 · 单次运行仅供观察趋势，不据此判断稳定",
                  x+12,y+7,width-24,23,12,MUTED)
        if not valid:
            self.text(dc,"尚无完整有效块，不展示模拟个人成绩",x+12,y+32,width-24,25,14,MUTED)
            return
        plot_y, plot_h = y+34, max(12,height-47)
        # Merge every window across valid blocks into gain-bucketed averages.
        buckets: dict[float, list[float]] = {}
        for record in valid:
            for task in record.tasks:
                for gain, score in zip(task.window_gains, task.window_scores()):
                    buckets.setdefault(round(gain, 10), []).append(score)
        points: list[tuple[float, float]] = sorted(
            ((gain, sum(values) / len(values)) for gain, values in buckets.items()))
        if not points:
            self.text(dc,"尚无窗口级数据",x+12,y+32,width-24,25,14,MUTED)
            return
        min_g: float = points[0][0]
        max_g: float = points[-1][0]
        span: float = max(1e-12, max_g - min_g)
        prev: tuple[float, float] | None = None
        for gain, score in points:
            px: float = x + 20 + (width - 40) * (gain - min_g) / span
            py: float = plot_y + plot_h * (1 - score / 100)
            if prev:
                self.line(dc, prev[0], prev[1], px, py, TEAL, 2)
            self.circle(dc, px, py, 3, TEAL, TEAL)
            prev = (px, py)
        best: tuple[float, float] = max(points, key=lambda item: item[1])
        bx: float = x + 20 + (width - 40) * (best[0] - min_g) / span
        by: float = plot_y + plot_h * (1 - best[1] / 100)
        self.circle(dc, bx, by, 5, YELLOW, YELLOW)
        # The x axis runs left-to-right with rising gain, i.e. falling cm/360°. A larger
        # cm/360° means a *slower* turn, so it is the lower sensitivity, and vice versa.
        slow_cm: float = 914.4 / (self.settings.dpi * self.settings.yaw * min_g)
        fast_cm: float = 914.4 / (self.settings.dpi * self.settings.yaw * max_g)
        self.text(dc, f"{slow_cm:.2f} cm/360°（更低灵敏度）", x+12, y+height-2, 200, 16, 12, MUTED)
        self.text(dc, f"{fast_cm:.2f} cm/360°（更高灵敏度）", x+width-212, y+height-2, 200, 16, 12, MUTED)

    def draw_results(self, dc: int) -> None:
        summary: dict[str, Any] = self.controller.summary() if self.controller else {}
        title = f"{BUDGET_SECONDS:g}秒动态调整结果 / 只给待验证候选"
        self.text(dc,title,34,130,self.width-68,42,28,WHITE,True)
        self.box(dc,34,189,self.width-68,160,PANEL2)
        conclusion: str = str(summary.get("conclusion", "insufficient"))
        candidate: float | None = summary.get("candidate_cm360")
        headings: dict[str, str] = {"candidate": "自动调整后出现更好的一档 · 待验证",
                                    "keep": "曲线平坦或未达标 · 建议保留原设置",
                                    "insufficient": "证据不足 · 不宣称最佳"}
        heading: str = headings.get(conclusion, headings["insufficient"])
        if candidate:
            heading += f"   {candidate:.2f} cm/360°"
        self.text(dc,heading,54,205,self.width-108,39,25,TEAL if conclusion == "candidate" else YELLOW,True)
        # Always-present single recommendation; colour encodes confidence, never blank.
        recommended_sens = summary.get("recommended_sensitivity")
        recommended_cm360 = summary.get("recommended_cm360")
        confidence: str = str(summary.get("recommendation_confidence", "none"))
        conf_colors: dict[str, int] = {"high": TEAL, "low": YELLOW, "none": MUTED}
        conf_labels: dict[str, str] = {"high": "置信度 高", "low": "置信度 低", "none": "未测得（原始值）"}
        if recommended_sens is not None and recommended_cm360 is not None:
            recommend_line: str = (f"推荐灵敏度 sens {float(recommended_sens):.5f}"
                                   f"（{float(recommended_cm360):.2f} cm/360°）"
                                   f"   ·   {conf_labels.get(confidence, conf_labels['none'])}")
        else:
            recommend_line = "推荐灵敏度：暂缺（尚无可用数据）"
        self.text(dc,recommend_line,54,246,self.width-108,26,18,conf_colors.get(confidence,MUTED),True)
        self.text(dc,summary.get("message","尚无个人测评记录。"),54,276,self.width-108,26,14,MUTED)
        band = summary.get("visited_band_cm360")
        peak_score = summary.get("recommended_window_score")
        tie_band = summary.get("recommended_band_cm360")
        if band:
            compare: str = (f"实际走到 {band[0]:.2f}–{band[1]:.2f} cm/360°  ·  最佳窗口 "
                            + (f"{float(peak_score):.1f} 分" if peak_score is not None else "未达标"))
            if tie_band:
                # Adjacent buckets sit only a couple of cm/360 apart and one window holds only a
                # few clicks, so a single "best" value would claim precision we do not have.
                compare += f"  ·  与最好成绩不可区分 {tie_band[0]:.2f}–{tie_band[1]:.2f}"
            else:
                compare += f"  ·  每 {WINDOW_SECONDS:g} 秒按成绩重新决定方向"
        else:
            compare = "需要至少一个完整有效的测评块才能画出图；未完成时不宣称任何结果。"
        self.text(dc,compare,54,306,self.width-108,26,14,WHITE)
        self.draw_trend(dc,34,362,self.width-68,104)
        retained = summary.get("retained_sensitivity", self.settings.sensitivity)
        retained_cm360 = summary.get("retained_cm360", self.settings.distance)
        self.text(dc,f"有效块 {summary.get('valid_blocks',0)} / 完成块 {summary.get('completed_blocks',0)}  ·  "
                  f"共 {MAX_BLOCKS} 块 × 三项各 {TASK_SECONDS:g} 秒  ·  "
                  f"推荐 sens {retained:.5f}（{retained_cm360:.2f} cm/360°）",
                  34,486,self.width-68,29,14,WHITE)
        latest: BlockRecord | None = next((item for item in reversed(self.controller.records) if item.valid), None) if self.controller else None
        breakdown: str = (f"最近有效块：定位 {latest.tasks[0].score:.1f} / 微调 {latest.tasks[1].score:.1f} / 跟踪 {latest.tasks[2].score:.1f} / 综合 {latest.score:.1f}；"
                          f"命中 {sum(item.hits for item in latest.tasks)}，误击 {sum(item.misses for item in latest.tasks)}。" if latest else "完整命中、误击、跟踪、分项、时间及调参原因可导出JSON。")
        self.text(dc,f"候选门槛：最佳一档需≥{MIN_COMPOSITE:g}分且领先次佳≥{PEAK_MARGIN:g}分；"
                  f"单窗口仅{WINDOW_SECONDS:g}秒、数据量小，"
                  "最好的一档可能是噪声，因此不宣称稳定。\n" + breakdown,
                  34,524,self.width-68,52,13,MUTED)
        self.text(dc,"舒适度备注（不改变分数）",52,self.height-169,self.width-104,26,14,MUTED)
        self.button(dc,"重新自动测评","restart",34,self.height-89,172,True)
        self.button(dc,"导出 JSON","export",222,self.height-89,150)
        self.button(dc,"打开历史 JSON","import",388,self.height-89,180)
        self.text(dc,"自动调整仅限本软件；没有改动系统/真实游戏。",592,self.height-84,self.width-626,40,13,MUTED)

    def draw_help(self, dc: int) -> None:
        self.text(dc,"自动调参方法 · 边界与证据",34,131,self.width-68,44,28,WHITE,True)
        sections: list[tuple[str,str]] = [
            (f"01 / {BUDGET_SECONDS:g}秒计划与跟着成绩走的灵敏度",
             f"2秒热身 + {MAX_BLOCKS}块×三项各{TASK_SECONDS:g}秒 = "
             f"{WARMUP_SECONDS:g}+{MAX_BLOCKS}×{BLOCK_SECONDS:g}={BUDGET_SECONDS:g}秒；暂停不计时，已用预算不重置。\n"
             f"关键：灵敏度不是单向扫过去，而是每 {WINDOW_SECONDS:g} 秒按你上一窗的成绩改一次方向——"
             f"分数变好就沿当前方向继续走，变差就掉头并把步子收窄（cm/360° 始终在 ×{HILL_SLOW_FACTOR:g}–×{HILL_FAST_FACTOR:g} 内）。"
             "所以曲线会上下波动，波峰就是候选。"),
            ("02 / 每2秒单独计分，结果是一条曲线",
             f"每项{TASK_SECONDS:g}秒切成{WINDOWS_PER_TASK}个{WINDOW_SECONDS:g}秒窗口，各自独立计分，"
             "横轴是当时生效的灵敏度、纵轴是该窗口得分。\n"
             "块1 从快端出发、块2 从慢端出发，两次起点相反以削弱顺序偏差；曲线取两块平均后的峰值。"),
            ("03 / 评分与候选门槛",
             f"点击={WINDOW_SECONDS:g}秒内平均 100×exp(−间隔/0.45s) 乘准确率²（超时算未命中，防乱点）；"
             f"跟踪=100×覆盖秒数/{WINDOW_SECONDS:g}；综合=.75×几何均值+.25×最弱项。\n"
             f"峰值被报为候选需：≥{MIN_COMPOSITE:g}分且领先次佳≥{PEAK_MARGIN:g}分；否则视为曲线平坦并建议保留原设置。"),
            ("04 / 原生输入和真实游戏边界",
             "仅WM_INPUT相对counts；角度=count×sens×yaw；cm360=914.4/(DPI×sens×yaw)。DPI手填，不自动猜测。\n"
             "只在本测试场改变sens，不读写/注入真实游戏、不自动瞄准、不改DPI/系统鼠标。普通腰射角度平面近似。"),
            ("05 / 局限与本地数据",
             "启发式不是学术最佳；单窗口仅2秒、样本量小，峰值可能来自噪声，所以最多给待验证候选，建议不同日期复测。"
             "数据本地 results/adaptive-latest.json，可导出；旧版 schema 2/3/4/5 一律拒收。\n"
             "Yaw：CS2 .022（totalcsgo.com/commands/myaw）；VAL .07第三方常见值（sensconverter.org/game/valorant/）。\n"
             "微软Raw Input：learn.microsoft.com/en-us/windows/win32/dxtecharts/taking-advantage-of-high-dpi-mouse-movement")]
        for index,(title,body) in enumerate(sections):
            y: int = 200+index*97
            self.text(dc,title,34,y,self.width-68,28,17,TEAL,True)
            self.text(dc,body,34,y+31,self.width-68,66,14,MUTED)

    def run_smoke(self) -> None:
        """Real Win32 lifecycle checks without fake performance or saved history."""
        self.controller = AdaptiveController(self.settings)
        self.smoke_result["gdiplus_started"] = bool(self.gp_token)
        self.smoke_result["timer_resolution_1ms"] = bool(self.timer_period_set)
        self.smoke_result["sounds_ready"] = bool(SOUNDS)
        self.smoke_result["budget_is_122_seconds"] = BUDGET_SECONDS == 122
        self.smoke_result["plan_fits_budget"] = math.isclose(
            WARMUP_SECONDS + MAX_BLOCKS * BLOCK_SECONDS, BUDGET_SECONDS)
        self.smoke_result["each_task_is_20_seconds"] = TASK_SECONDS == 20
        self.smoke_result["two_blocks_only"] = MAX_BLOCKS == 2
        self.smoke_result["windows_divide_task"] = (
            WINDOWS_PER_TASK * WINDOW_SECONDS == TASK_SECONDS and WINDOWS_PER_TASK >= 2)
        first, second = self.controller.climb_for(0, 1), self.controller.climb_for(0, 2)
        band = self.controller.band_for_task(0, 1)
        # Block 1 starts at the fast end and walks down, block 2 starts at the slow end and
        # walks up, so the two blocks actually bracket the band instead of retracing it.
        self.smoke_result["blocks_start_at_opposite_ends"] = (
            math.isclose(first.gain, band[1]) and math.isclose(second.gain, band[0])
            and first.direction < 0 < second.direction)
        self.smoke_result["band_has_room_to_move"] = (
            band[0] < band[1] and self.controller.band_for_task(0, 2) == band)
        # The heart of the new model: the next gain must come from the score just fed in, not
        # from a clock. Feed the same instance a rise and a fall and require it to react.
        probe = self.controller.climb_for(0, 1)
        probe.observe(20.0)
        after_flat: float = probe.gain
        probe.observe(60.0)          # a clear improvement => keep going, no reversal
        improved_gain: float = probe.gain
        probe.observe(5.0)           # a clear drop => reverse and shorten the stride
        self.smoke_result["gain_follows_score"] = bool(
            improved_gain != after_flat and probe.reversals >= 1
            and math.isclose(probe.gain, probe.history[-1]))
        self.smoke_result["climb_starts_inside_band"] = bool(
            all(band[0] <= gain <= band[1] for gain in probe.history))
        self.start_capture()
        self.smoke_result["single_start_captures"] = bool(self.active and self.raw and self.raw.active)
        self.finish_stage()  # warm-up only, no scored observation.
        self.smoke_result["warmup_auto_enters_block"] = bool(self.active and self.current_request and not self.warmup)
        self.smoke_result["scored_task_owns_a_climb"] = self.climb is not None
        self.smoke_result["window_boundaries_are_armed"] = bool(
            self.climb is not None and self.window_start_time > 0)
        user32.SendMessageW(self.hwnd,0x0100,0x1B,0)
        self.smoke_result["escape_releases"] = bool(not self.active and self.raw and not self.raw.active)
        self.start_capture()
        user32.SendMessageW(self.hwnd,0x001C,0,0)
        self.smoke_result["focus_releases"] = bool(not self.active and self.raw and not self.raw.active)
        for page in ("setup","test","results","help"):
            self.page=page
            self.place_controls()
            self.invalidate()
            user32.UpdateWindow(self.hwnd)
        self.smoke_result["all_pages_rendered"] = not bool(self.fatal_error)
        self.smoke_result["no_valid_scores"] = not any(item.valid for item in self.controller.records)
        self.smoke_result["history_written"] = False
        self.start_capture()
        # The deadline path only runs while this window owns the foreground (otherwise the tick
        # releases capture as a focus loss instead). Windows may refuse SetForegroundWindow when
        # another application currently owns the foreground, and this probe cannot fix that:
        # there is no foreground lock timeout to raise, because ``SPI_GETFOREGROUNDLOCKTIMEOUT``
        # answers 0 on a system where no lock was configured, and Windows grants the request only
        # to a process the user just interacted with. Retrying briefly covers the case where the
        # window is already coming forward; a False here is recorded and never mistaken for a
        # broken budget guard. ``deadline_releases`` is therefore only a hard requirement in the
        # frozen exe, which the user double-clicks and which is genuinely foreground while it runs.
        for _ in range(20):
            user32.SetForegroundWindow(self.hwnd)
            if user32.GetForegroundWindow() == self.hwnd:
                break
            time.sleep(.02)
        self.smoke_result["deadline_window_is_foreground"] = bool(
            user32.GetForegroundWindow() == self.hwnd)
        self.spent=BUDGET_SECONDS
        self.tick()
        self.smoke_result["deadline_releases"] = bool(not self.active and self.controller.ended and self.raw and not self.raw.active)
        user32.SendMessageW(self.hwnd,0x0010,0,0)
