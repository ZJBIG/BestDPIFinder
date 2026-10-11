"""QA-only autonomous input for the production AdaptiveApp scoring pipeline."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adaptive import BUDGET_SECONDS, TASK_SECONDS, WINDOW_SECONDS, WINDOWS_PER_TASK
from autotest_model import HumanInputModel
from core import STAGES, raw_angle


class AutotestDriver:
    def __init__(self, app: Any, seed: int, output: Path, capture: Any, fast: bool = False) -> None:
        self.app, self.seed, self.output, self.capture = app, int(seed), Path(output), capture
        self.fast = bool(fast)
        self.base_sens = 0.35
        self.preferred_sens = self.base_sens * 1.09
        self.model = HumanInputModel(self.seed, self.base_sens, self.preferred_sens)
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started_clock = self.app.now()
        self.events_path = self.output / "autotest_events.jsonl"
        self.log_path = self.output / "logs" / "autotest.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("w", encoding="utf-8", buffering=1)
        self.events = self.events_path.open("w", encoding="utf-8", buffering=1)
        self.stage_key: tuple[Any, ...] | None = None
        self.target_key: tuple[float, float, float] | None = None
        self.action_state = "idle"
        self.reaction_until = 0.0
        self.move_started = 0.0
        self.move_duration = 0.0
        self.move_origin = (0.0, 0.0)
        self.move_destination = (0.0, 0.0)
        self.move_kind = ""
        self.planned_miss = False
        self.clicks_before = 0
        self.correction_due = 0.0
        self.count_residual = [0.0, 0.0]
        self.last_climb_id = 0
        self.last_history_len = 0
        self.last_gain = None
        self.last_direction = None
        self.last_reversals = 0
        self.climb_states: dict[int, dict[str, Any]] = {}
        self.tracked_climbs: list[Any] = []
        self.gain_changes = 0
        self.direction_reverses = 0
        self.gear_shrinks = 0
        self.misses = 0
        self.corrections = 0
        self.tracking_lost = 0
        self._tracking_drop_until = 0.0
        self._tracking_lag = 110.0
        self._last_tracking_lag_pick = 0.0
        self.finalize_at = None
        self.finalized = False
        self.failure_reported = False
        self.initial_gain_values: list[float] = []
        self.log_line(f"seed={seed}; speed={'fast' if fast else '1.0'}; preferred_sens={self.preferred_sens:.8f}")

    def log_line(self, message: str) -> None:
        self.log.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")

    def prepare(self) -> None:
        self.capture.keyframe("01_settings")
        self.app.action("prepare")
        if not self.app.active or not self.app.warmup:
            raise RuntimeError("QA start failed: prepare did not start the real warmup capture")
        self.log_line("settings page captured; production action('prepare') started warmup")

    def _context(self) -> dict[str, Any]:
        app = self.app
        req = app.current_request
        elapsed = max(0.0, float(getattr(app, "elapsed", 0.0)))
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task": ("warmup" if app.warmup else STAGES[app.stage_index]),
            "block": req.index if req else None,
            "window_index": (app.window_index_at(time.perf_counter()) if app.active and not app.warmup else None),
            "cursor_x": round(app.view_x, 5), "cursor_y": round(app.view_y, 5),
            "target_x": round(app.target_x, 5), "target_y": round(app.target_y, 5),
            "current_gain": app.current_sensitivity,
            "current_sens": app.current_sensitivity,
            "reaction_delay_ms": None, "movement_duration_ms": None,
            "overshoot_px": None, "undershoot_px": None, "jitter_px": None,
            "click_attempt": None, "hit_or_miss": None,
            "correction_delay_ms": None, "tracking_lag_ms": None,
            "window_score": None, "hill_decision": None, "hill_direction": None,
            "hill_gear": None, "next_gain": None, "elapsed_seconds": round(elapsed, 4),
        }

    def event(self, name: str, **fields: Any) -> None:
        entry = {"event": name, **self._context(), **fields}
        self.events.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")

    def _stage_identity(self) -> tuple[Any, ...]:
        app = self.app
        request = app.current_request
        return (request.index if request else None, app.stage_index, bool(app.warmup))

    def _begin_stage(self, now: float) -> None:
        app = self.app
        self.action_state = "idle"
        self.target_key = None
        self.stage_key = self._stage_identity()
        if app.warmup:
            return
        if app.current_request and app.current_request.index == 1 and app.stage_index == 0:
            self.capture.keyframe("02_test_start")
            self.initial_gain_values.append(app.current_sensitivity)
        if app.current_request and app.current_request.index == 2 and app.stage_index == 0:
            self.capture.keyframe("10_block2_start")
            self.initial_gain_values.append(app.current_sensitivity)
            self.event("block_started", hill_direction=app.climb.direction if app.climb else None,
                       hill_gear=app.climb.gear if app.climb else None,
                       next_gain=app.current_sensitivity)
        if app.stage_index == 1:
            self.capture.keyframe("08_micro_adjust")
        elif app.stage_index == 2:
            self.capture.keyframe("09_tracking")
        if app.climb:
            if id(app.climb) not in self.climb_states:
                self.tracked_climbs.append(app.climb)
            self.climb_states[id(app.climb)] = {
                "climb": app.climb, "history_len": len(app.climb.history),
                "gain": app.climb.gain, "direction": app.climb.direction,
                "gear": app.climb.gear, "reversals": app.climb.reversals,
                "block": app.current_request.index if app.current_request else None,
                "task": STAGES[app.stage_index], "windows_seen": 0,
            }
        self.log_line(f"task started: block={app.current_request.index if app.current_request else None}, stage={app.stage_index}")

    def _key_target(self) -> tuple[float, float, float]:
        app = self.app
        return (round(app.target_x, 5), round(app.target_y, 5), round(app.target_spawn, 4))

    def _start_target(self, now: float) -> None:
        app = self.app
        self.target_key = self._key_target()
        if app.stage_index == 0 and app.elapsed >= 7.0 and not getattr(self, "positioning_keyframe", False):
            self.capture.keyframe("03_positioning")
            self.positioning_keyframe = True
        self.event("target_appeared")
        delay = self.model.reaction_ms(app.current_sensitivity)
        self.reaction_until = now + delay / 1000.0
        self.event("reaction_started", reaction_delay_ms=round(delay, 2))
        self.action_state = "reaction"

    def _current_target_destination(self, correction: bool = False) -> tuple[float, float]:
        app = self.app
        width = max(1, app.viewport()[2] - app.viewport()[0])
        scale = width / 72.0
        tx, ty = app.target_x, app.target_y
        micro = app.stage_index == 1
        if correction:
            radius_px = self.model.correction_offset_px()
            angle = self.model.rng.random() * math.tau
            offset = radius_px / scale
            # Correction lands within the genuine target radius after a short, small adjustment.
            return tx + math.cos(angle) * min(offset, app.radius() * .5), ty + math.sin(angle) * min(offset, app.radius() * .5)
        distance = math.hypot(tx - app.view_x, ty - app.view_y) * scale
        noise = self.model.motor_noise_px(app.current_sensitivity) / scale
        end_x, end_y = tx + noise, ty - noise * 0.55
        if self.model.first_click_miss(app.current_sensitivity):
            angle = self.model.rng.random() * math.tau
            offset_px = max(app.radius() * scale + 5, self.model.rng.uniform(5, 40))
            offset = offset_px / scale
            end_x += math.cos(angle) * offset
            end_y += math.sin(angle) * offset
            self.planned_miss = True
        else:
            self.planned_miss = False
            if micro and self.model.rng.random() < 0.24:
                # Small near-target undershoot/overshoot; the final correction is distinct.
                angle = math.atan2(ty - app.view_y, tx - app.view_x)
                end_x -= math.cos(angle) * self.model.rng.uniform(0.05, 0.25)
                end_y -= math.sin(angle) * self.model.rng.uniform(0.05, 0.25)
        if micro and not self.planned_miss:
            distance = math.hypot(end_x - app.view_x, end_y - app.view_y)
            if distance > 1e-6:
                final_gap = min(self.model.rng.uniform(10.0, 40.0) / scale, distance * 0.45)
                end_x -= (end_x - app.view_x) / distance * final_gap
                end_y -= (end_y - app.view_y) / distance * final_gap
        return end_x, end_y

    def _start_move(self, now: float, correction: bool = False, micro_final: bool = False) -> None:
        app = self.app
        dest = ((app.target_x + self.model.rng.gauss(0, 0.035),
                 app.target_y + self.model.rng.gauss(0, 0.035)) if micro_final else
                self._current_target_destination(correction))
        width = max(1, app.viewport()[2] - app.viewport()[0])
        scale = width / 72.0
        distance_px = math.hypot(dest[0] - app.view_x, dest[1] - app.view_y) * scale
        duration_ms = (self.model.rng.uniform(80, 150) if micro_final else
                       max(70, self.model.correction_delay_ms()) if correction else
                       self.model.movement_ms(distance_px, app.current_sensitivity, app.stage_index == 1))
        self.move_started = now
        self.move_duration = duration_ms / 1000.0
        self.move_origin = (app.view_x, app.view_y)
        self.move_destination = dest
        self.move_kind = "correction" if correction else ("micro_final" if micro_final else "primary")
        self.action_state = "moving"
        if correction:
            self.corrections += 1
            self.capture.keyframe("04_first_miss")
            self.event("correction_started", correction_delay_ms=round(self.correction_due_ms, 2),
                       movement_duration_ms=round(duration_ms, 2), click_attempt=2)
        else:
            self.event("movement_started", movement_duration_ms=round(duration_ms, 2),
                       overshoot_px=round(max(0.0, distance_px - math.hypot(app.target_x-app.view_x, app.target_y-app.view_y)*scale), 2),
                       undershoot_px=(round(math.hypot(app.target_x-dest[0], app.target_y-dest[1])*scale, 2)
                                      if app.stage_index == 1 and not self.planned_miss else None),
                       jitter_px=round(abs(self.model.motor_noise_px(app.current_sensitivity)), 2), click_attempt=1)

    def _move_step(self, now: float) -> None:
        p = min(1.0, max(0.0, (now - self.move_started) / max(0.001, self.move_duration)))
        eased = self.model.minimum_jerk(p)
        x = self.move_origin[0] + (self.move_destination[0] - self.move_origin[0]) * eased
        y = self.move_origin[1] + (self.move_destination[1] - self.move_origin[1]) * eased
        self._move_cursor(x, y)
        if p >= 1.0:
            if self.app.stage_index == 1 and self.move_kind == "primary" and not self.planned_miss:
                self._start_move(now, micro_final=True)
            else:
                self.action_state = "click"

    def _move_cursor(self, x: float, y: float) -> None:
        app = self.app
        gain = max(1e-9, raw_angle(1, app.current_sensitivity, app.settings.yaw))
        desired = ((x - app.view_x) / gain + self.count_residual[0],
                   (y - app.view_y) / gain + self.count_residual[1])
        dx, dy = int(round(desired[0])), int(round(desired[1]))
        self.count_residual = [desired[0] - dx, desired[1] - dy]
        if dx or dy:
            app.inject_qa_relative(dx, dy, False)

    def _click(self, now: float, attempt: int) -> None:
        app = self.app
        before_hit, before_miss = app.hits, app.misses
        app.inject_qa_relative(0, 0, True)
        hit = app.hits > before_hit
        miss = app.misses > before_miss
        self.clicks_before = app.hits + app.misses
        self.event("click_hit" if hit else "click_miss", click_attempt=attempt,
                   hit_or_miss="hit" if hit else ("miss" if miss else "ignored"),
                   overshoot_px=round(math.hypot(app.target_x - app.view_x,
                                                  app.target_y - app.view_y) *
                                      max(1, app.viewport()[2]-app.viewport()[0]) / 72, 2))
        if miss:
            self.misses += 1
            if attempt == 1:
                self.correction_due_ms = self.model.correction_delay_ms()
                self.correction_due = now + self.correction_due_ms / 1000
                self.action_state = "correction_wait"
                self.capture.keyframe("04_first_miss")
            else:
                self.action_state = "idle"
        elif hit:
            self.action_state = "idle"

    def _tick_clicking(self, now: float) -> None:
        app = self.app
        key = self._key_target()
        if key != self.target_key and self.action_state not in ("correction_wait", "moving"):
            self._start_target(now)
        if self.action_state == "reaction" and now >= self.reaction_until:
            self._start_move(now)
        elif self.action_state == "moving":
            self._move_step(now)
        elif self.action_state == "click":
            self._click(now, 2 if self.move_kind == "correction" else 1)
        elif self.action_state == "correction_wait" and now >= self.correction_due:
            self._start_move(now, correction=True)

    def _tick_tracking(self, now: float) -> None:
        app = self.app
        if now - self._last_tracking_lag_pick > 0.8:
            self._tracking_lag = self.model.tracking_lag_ms(app.current_sensitivity)
            self._last_tracking_lag_pick = now
            if self.model.tracking_drop():
                self._tracking_drop_until = now + self.model.rng.uniform(0.10, 0.35)
                self.tracking_lost += 1
                self.event("tracking_lost", tracking_lag_ms=round(self._tracking_lag, 2))
        task_time = max(0.0, app.elapsed - self._tracking_lag / 1000)
        from adaptive import tracking_target
        tx, ty = tracking_target(task_time)
        if now >= self._tracking_drop_until:
            if getattr(self, "_was_tracking_lost", False):
                self.event("tracking_recovered", tracking_lag_ms=round(self._tracking_lag, 2))
            self._was_tracking_lost = False
            noise = self.model.rng.gauss(0, 0.16 + 0.12 * self.model.penalty(app.current_sensitivity))
            self._move_cursor(app.view_x + (tx - app.view_x) * 0.24 + noise,
                              app.view_y + (ty - app.view_y) * 0.24 - noise * 0.6)
        else:
            self._was_tracking_lost = True

    def _observe_hill(self) -> None:
        app = self.app
        climbs = list(self.tracked_climbs)
        if app.climb and app.climb not in climbs:
            climbs.append(app.climb)
        for climb in climbs:
            state = self.climb_states.get(id(climb))
            if state is None:
                continue
            while len(climb.history) > state["history_len"]:
                previous_gain = state["gain"]
                next_gain = climb.history[state["history_len"]]
                state["history_len"] += 1
                score = climb.last_score
                decision = "move" if not math.isclose(previous_gain, next_gain, rel_tol=1e-10) else "hold"
                fields = {"task": state["task"], "block": state["block"],
                          "window_index": state["windows_seen"],
                          "window_score": round(score, 4), "hill_decision": decision,
                          "hill_direction": climb.direction, "hill_gear": climb.gear,
                          "next_gain": next_gain}
                self.event("window_finished", **fields)
                if decision == "move":
                    self.gain_changes += 1
                    self.event("gain_changed", **fields)
                    if self.gain_changes == 1: self.capture.keyframe("05_sens_changed")
                if climb.reversals > state["reversals"]:
                    self.direction_reverses += climb.reversals - state["reversals"]
                    self.event("direction_reversed", **fields)
                    if self.direction_reverses == 1: self.capture.keyframe("06_direction_reverse")
                if climb.gear > state["gear"]:
                    self.gear_shrinks += climb.gear - state["gear"]
                    self.event("gear_shrunk", **fields)
                    if self.gear_shrinks == 1: self.capture.keyframe("07_step_shrink")
                state["gain"] = next_gain
                state["direction"] = climb.direction
                state["gear"] = climb.gear
                state["reversals"] = climb.reversals
                state["windows_seen"] += 1

    def tick(self) -> None:
        if self.finalized:
            return
        if not self.app.active:
            if self.app.controller and self.app.controller.ended and self.app.page == "results":
                if self.finalize_at is None:
                    self.finalize_at = time.monotonic() + (6.0 if not self.fast else 0.2)
                    self.capture.keyframe("11_candidate")
                    self.capture.keyframe("12_final_result")
                    self.log_line("real result page reached; holding it for capture")
                elif time.monotonic() >= self.finalize_at:
                    self.finalize()
                    from native import user32
                    user32.PostMessageW(self.app.hwnd, 0x0010, 0, 0)
            elif self.app.controller and not self.failure_reported:
                self.failure_reported = True
                self.fail("application became inactive before the controller reached its final results page")
            return
        now = self.app.now()
        identity = self._stage_identity()
        if identity != self.stage_key:
            self._begin_stage(now)
        self._observe_hill()
        if self.app.warmup:
            return
        if self.app.stage_index < 2:
            self._tick_clicking(now)
        else:
            self._tick_tracking(now)

    def close(self) -> None:
        if self.events and not self.events.closed:
            self.events.flush()
            self.events.close()
        if self.log and not self.log.closed:
            self.log.flush()
            self.log.close()

    def fail(self, reason: str) -> None:
        self.log_line("FAILED: " + reason)
        (self.output / "logs" / "failure.txt").write_text(reason + "\n", encoding="utf-8")
        self.capture.stop()
        self.close()
        from native import user32
        user32.PostMessageW(self.app.hwnd, 0x0010, 0, 0)

    def finalize(self) -> None:
        if self.finalized:
            return
        self.finalized = True
        self.capture.stop()
        end = datetime.now(timezone.utc).isoformat()
        result_source = Path(self.app.history_path)
        result_copy = self.output / "result" / "adaptive-latest.json"
        result_copy.parent.mkdir(parents=True, exist_ok=True)
        if not result_source.exists():
            raise RuntimeError(f"formal result file missing: {result_source}")
        shutil.copy2(result_source, result_copy)
        runtime_dir = self.output / ".runtime"
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        seed_report = Path(__file__).parent / "autotest_seed.json"
        if seed_report.exists():
            shutil.copy2(seed_report, self.output / "autotest_seed.json")
        payload = json.loads(result_copy.read_text(encoding="utf-8"))
        summary = payload["summary"]
        blocks = payload.get("blocks", [])
        summary_blocks = summary.get("blocks", [])
        task_scores: dict[str, list[float]] = {}
        all_gains: list[float] = []
        score_windows = 0
        for block in blocks:
            for index, task in enumerate(block.get("tasks", [])):
                window_scores = block.get("window_scores", [])[index]
                task_scores.setdefault(task["name"], []).append(
                    sum(float(score) for score in window_scores) / max(1, len(window_scores)))
                all_gains.extend(float(g) for g in task.get("window_gains", []))
                score_windows += len(task.get("window_gains", []))
        gains_range = [min(all_gains), max(all_gains)] if all_gains else None
        report = {
            "seed": self.seed, "base_dpi": 800, "base_sens": self.base_sens,
            "preferred_sens": self.preferred_sens, "test_started_at": self.started_at,
            "test_ended_at": end, "actual_active_seconds": payload.get("active_elapsed"),
            "block1_score": blocks[0].get("score") if blocks else None,
            "block2_score": blocks[1].get("score") if len(blocks) > 1 else None,
            "task_scores": {key: values for key, values in task_scores.items()},
            "candidate_sens": summary.get("candidate_sensitivity"),
            "candidate_cm360": summary.get("candidate_cm360"),
            "candidate_produced": summary.get("candidate_sensitivity") is not None,
            "tested_sensitivity_range": gains_range, "gain_change_count": self.gain_changes,
            "direction_reverse_count": self.direction_reverses,
            "gear_shrink_count": self.gear_shrinks, "miss_count": self.misses,
            "correction_count": self.corrections, "tracking_lost_count": self.tracking_lost,
            "scoring_window_count": score_windows, "capture_fps": self.capture.fps,
            "frame_count": self.capture.count,
        }
        for block in blocks:
            self.event("block_finished", block=block.get("index"),
                       hill_decision="complete", window_score=block.get("score"))
        self.event("candidate_generated", current_sens=summary.get("candidate_sensitivity"),
                   current_gain=summary.get("candidate_sensitivity"),
                   next_gain=summary.get("candidate_sensitivity"),
                   hill_decision=summary.get("recommendation_source"),
                   window_score=summary.get("recommended_window_score"))
        (self.output / "autotest_summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest = self._manifest(end, result_copy)
        (self.output / "autotest_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.output / "logs" / "environment.txt").write_text(
            f"Python: {sys.version}\nPlatform: {platform.platform()}\n"
            f"Dependencies: mss, Pillow\nCapture: {self.capture.fps} FPS JPEG quality 90\n",
            encoding="utf-8")
        self._validate(payload, report)
        self._readme()
        zip_path = self.output.parent / "BestDPIFinder_Autotest_Result.zip"
        self.log_line(f"complete: zip={zip_path}; frames={self.capture.count}; fps={self.capture.fps}")
        self.close()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(self.output.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(self.output.parent))

    def _manifest(self, ended_at: str, result_copy: Path) -> dict[str, Any]:
        from native import user32
        import ctypes as ct
        from ctypes import wintypes as wt
        rect = wt.RECT()
        user32.GetWindowRect(self.app.hwnd, ct.byref(rect))
        def sha(path: Path) -> str:
            h = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()
        source_files = [Path(__file__).parent / item for item in
                        ("adaptive.py", "adaptive_ui.py", "core.py", "native.py", "win32_app.py", "main.py",
                         "BestDPIFinder.pyw")]
        qa_files = [Path(__file__).parent / item for item in
                    ("autotest_driver.py", "autotest_model.py", "autotest_capture.py",
                     "autotest_seed_search.py", "requirements-autotest.txt",
                     "setup_autotest.ps1", "run_autotest.ps1", "autotest_seed.json")]
        return {
            "project_version": "BestDPI Finder adaptive schema 6",
            "schema_version": 1, "windows_version": platform.platform(),
            "screen_resolution": f"{user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}",
            "app_window_rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom},
            "capture_resolution": f"{self.capture.width}x{self.capture.height}",
            "capture_fps": self.capture.fps, "capture_image_format": "JPEG quality 90",
            "python_version": sys.version, "autotest_dependencies": ["mss", "Pillow"],
            "seed": self.seed,
            "human_model_parameters": {"base_reaction_ms": [180, 320], "extra_hesitation_probability": 0.09,
                "movement_ms": [100, 330], "miss_base_probability": 0.055,
                "tracking_lag_ms": [70, 240], "tracking_short_loss_ms": [100, 350]},
            "base_dpi": 800, "base_sens": self.base_sens,
            "preferred_sens": self.preferred_sens, "yaw": 0.07,
            "test_speed": "fast-dev" if self.fast else 1.0,
            "test_started_at": self.started_at, "test_ended_at": ended_at,
            "git_commit": self._git_commit(),
            "formal_source_hashes": {str(p.name): sha(p) for p in source_files if p.exists()},
            "autotest_file_hashes": {str(p.name): sha(p) for p in qa_files if p.exists()},
            "formal_result_sha256": sha(result_copy),
            "frame_count": self.capture.count, "capture_error": self.capture.error,
        }

    @staticmethod
    def _git_commit() -> str | None:
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                    text=True, timeout=2, check=True)
            return result.stdout.strip()
        except Exception:
            return None

    def _validate(self, payload: dict[str, Any], report: dict[str, Any]) -> None:
        blocks = payload.get("blocks", [])
        summary_blocks = payload.get("summary", {}).get("blocks", [])
        tasks = [task for block in blocks for task in block.get("tasks", [])]
        errors = []
        if len(blocks) != 2 or any(not block.get("valid") for block in blocks): errors.append("both valid blocks not completed")
        if len(tasks) != 6: errors.append("expected six tasks")
        if any(abs(float(task.get("valid_seconds", 0)) - TASK_SECONDS) > 0.01 for task in tasks): errors.append("task duration mismatch")
        if any(len(task.get("window_gains", [])) != WINDOWS_PER_TASK or len(task.get("window_gaps", [])) != WINDOWS_PER_TASK for task in tasks): errors.append("10 scoring windows missing")
        if report["scoring_window_count"] != 60: errors.append("expected 60 scoring windows")
        if not 1 <= report["miss_count"] <= 12: errors.append("miss count outside 1..12")
        if report["correction_count"] < 1: errors.append("no corrective movement")
        if report["direction_reverse_count"] < 1: errors.append("no hill direction reversal")
        if report["gear_shrink_count"] < 1: errors.append("no hill gear shrink")
        if report["gain_change_count"] < 2: errors.append("too few gain changes")
        if not self.fast and any(not item.get("quality_sufficient") for item in summary_blocks):
            errors.append("one or more blocks did not meet the production quality gate")
        required = ["01_settings.png", "02_test_start.png", "03_positioning.png", "04_first_miss.png",
                    "05_sens_changed.png", "06_direction_reverse.png", "07_step_shrink.png",
                    "08_micro_adjust.png", "09_tracking.png", "10_block2_start.png",
                    "11_candidate.png", "12_final_result.png"]
        absent = [name for name in required if not (self.output / "keyframes" / name).exists()]
        if absent: errors.append("missing keyframes: " + ", ".join(absent))
        if self.capture.error: errors.append("capture error: " + self.capture.error)
        try:
            from PIL import Image, ImageStat
            frame_paths = sorted((self.output / "frames").glob("frame_*.jpg"))
            if (not frame_paths or frame_paths[0].name != "frame_000001.jpg"
                    or frame_paths[-1].name != f"frame_{len(frame_paths):06d}.jpg"):
                errors.append("frame sequence has a missing first/last frame or numbering gap")
            dimensions: set[tuple[int, int]] = set()
            black_frames = []
            samples = (frame_paths if len(frame_paths) <= 20 else
                       [frame_paths[round(i * (len(frame_paths) - 1) / 19)] for i in range(20)])
            for path in samples:
                with Image.open(path) as image:
                    dimensions.add(image.size)
                    if sum(ImageStat.Stat(image.convert("RGB")).mean) < 12:
                        black_frames.append(path.name)
            if dimensions != {(self.capture.width, self.capture.height)}:
                errors.append(f"frame dimensions inconsistent: {sorted(dimensions)}")
            if black_frames: errors.append("black frame(s): " + ", ".join(black_frames))
            if self.capture.count != len(frame_paths): errors.append("captured frame count differs from saved sequence")
            elapsed_wall = time.monotonic() - self.started_clock
            if len(frame_paths) < int(elapsed_wall * 8): errors.append("frame rate fell below 8 FPS over the capture run")
            for name in required:
                with Image.open(self.output / "keyframes" / name) as image:
                    if image.size != (self.capture.width, self.capture.height):
                        errors.append(f"keyframe dimensions mismatch: {name}")
                    if sum(ImageStat.Stat(image.convert("RGB")).mean) < 12:
                        errors.append(f"black keyframe: {name}")
        except Exception as error:
            errors.append(f"frame validation failed: {type(error).__name__}: {error}")
        if errors:
            message = "VALIDATION FAILED\n" + "\n".join("- " + item for item in errors)
            (self.output / "logs" / "validation.txt").write_text(message + "\n", encoding="utf-8")
            raise RuntimeError(message)
        (self.output / "logs" / "validation.txt").write_text(
            f"PASS: 2 blocks, 6 tasks, 60 windows; {self.capture.count} frames at {self.capture.fps} FPS; "
            f"{report['miss_count']} misses, {report['correction_count']} corrections, "
            f"{report['direction_reverse_count']} reversals, {report['gear_shrink_count']} gear shrinks.\n",
            encoding="utf-8")

    def _readme(self) -> None:
        (self.output / "README_AUTOTEST.txt").write_text(
            "BestDPI Finder QA-only autonomous run\n\n"
            "Frames are unedited client-area JPEG images captured from the real Windows application. "
            "The scoring, timing, hit testing, hill climb and candidate were produced by the normal "
            "AdaptiveApp code. QA relative input enters its on_raw path. No video was generated.\n"
            "Seed search and its fast synthetic screening record are included as autotest_seed.json. "
            "Seed and model parameters are recorded in autotest_manifest.json. This is a deterministic "
            "QA demonstration run, not a claim about all human players.\n",
            encoding="utf-8")
