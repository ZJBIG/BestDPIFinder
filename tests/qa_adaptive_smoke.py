"""新版独立原生GUI探针：仅操作本应用，不产生个人测评分数。"""
from __future__ import annotations

import ctypes as ct
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qa_smoke import clip_rectangle, screenshot_own_window


def child():
    import faulthandler
    faulthandler.dump_traceback_later(10, exit=False)
    from adaptive_ui import AdaptiveApp
    from adaptive import BUDGET_SECONDS
    from native import user32

    class QAAdaptiveWindow(AdaptiveApp):
        def __init__(self, temporary):
            super().__init__(smoke_test=True)
            user32.SetProcessDPIAware()
            self.initial_clip = clip_rectangle(user32)
            self.history_path = Path(temporary) / "qa-only-never-personal.json"
            self.checks = []

        def check(self, label, condition):
            result = {"check": label, "passed": bool(condition)}
            self.checks.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if not condition:
                raise AssertionError(label)

        def click(self, name):
            print("QA own button: " + name, flush=True)
            self.invalidate()
            user32.UpdateWindow(self.hwnd)
            rectangle = next(bounds for bounds, action in reversed(self.buttons) if action == name)
            left, top, right, bottom = rectangle
            x, y = (left + right) // 2, (top + bottom) // 2
            user32.SendMessageW(self.hwnd, 0x0202, 0, (y << 16) | x)

        def run_smoke(self):
            try:
                self.check("新版AdaptiveApp且初始不捕获", self.controller is None and not self.active and not self.raw.active)
                self.check("初始Clip未改变", clip_rectangle(user32) == self.initial_clip)
                user32.SetWindowTextW(self.controls["dpi"], "800")
                user32.SetWindowTextW(self.controls["sens"], "0.4")
                self.click("prepare")
                self.check("真实开始按钮一次点击立即捕获", self.active and self.raw.active and self.warmup)
                self.check("新控制器已创建且换算正确", self.controller is not None and abs(self.settings.distance - 40.82142857142857) < 1e-8)
                self.check("鼠标限制在本应用", clip_rectangle(user32) != self.initial_clip)
                user32.SendMessageW(self.hwnd, 0x0100, 0x1B, 0)
                spent = self.spent
                user32.SendMessageW(self.hwnd, 0x0100, 0x1B, 0)
                self.check("Esc重复安全释放", not self.active and not self.raw.active and self.aborted == 1)
                self.check("Esc恢复系统Clip", clip_rectangle(user32) == self.initial_clip)
                self.click("capture")
                self.check("主动恢复不重置预算", self.active and self.spent == spent)
                user32.SendMessageW(self.hwnd, 0x001C, 0, 0)
                self.check("WM_ACTIVATEAPP失焦释放", not self.active and not self.raw.active)
                self.check("失焦恢复系统Clip", clip_rectangle(user32) == self.initial_clip)
                for page in ("setup", "test", "results", "help"):
                    self.click(page)
                    self.invalidate()
                    user32.UpdateWindow(self.hwnd)
                    self.check("新版页面绘制:" + page, self.page == page and not self.fatal_error)
                    screenshot_own_window(self.hwnd, ROOT / "tests" / ("qa_adaptive-" + page + ".png"))
                self.click("test")
                # 预算路径只在窗口为前台时才会走到“预算到点”，先显式取回前台，
                # 否则 tick() 会走失焦分支，检查会随桌面焦点随机失败。
                user32.SetForegroundWindow(self.hwnd)
                self.check("预算探针前窗口为前台", user32.GetForegroundWindow() == self.hwnd)
                self.click("capture")
                # 仅触发预算安全路径；不注入击中、不生成仿真个人成绩。
                self.spent = BUDGET_SECONDS
                self.tick()
                print(json.dumps({"budget_probe": {
                    "active": self.active, "raw_active": self.raw.active,
                    "controller_ended": self.controller.ended,
                    "foreground": user32.GetForegroundWindow() == self.hwnd,
                    "aborted": self.aborted}}, ensure_ascii=False), flush=True)
                self.check("预算耗尽安全截止并释放",
                           not self.active and not self.raw.active and self.controller.ended)
                self.check("截止恢复系统Clip", clip_rectangle(user32) == self.initial_clip)
                self.check("无完整成绩且推荐为未测得的原始值",
                           not self.controller.records
                           and self.controller.summary()["recommendation_source"] == "baseline_fallback"
                           and self.controller.summary()["recommendation_confidence"] == "none")
                self.check("临时历史未写入", not self.history_path.exists())
                user32.SendMessageW(self.hwnd, 0x0010, 0, 0)
                self.check("关闭后仍安全释放", not self.active and not self.raw.active)
            except Exception as error:
                self.fatal_error = repr(error)
                if self.raw:
                    self.raw.stop()
                user32.PostMessageW(self.hwnd, 0x0010, 0, 0)
            finally:
                self.smoke_result = {"qa_adaptive_probe": True, "checks": self.checks,
                                     "error": self.fatal_error, "personal_results": False}

    with tempfile.TemporaryDirectory(prefix="bestdpi-qa-adaptive-gui-") as temporary:
        result = QAAdaptiveWindow(temporary).run()
        print("QA AdaptiveApp.run returned: " + str(result), flush=True)
    print("QA temporary directory cleanup returned", flush=True)
    return result


def parent():
    history = ROOT / "results" / "adaptive-latest.json"
    before = history.read_bytes() if history.exists() else None
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child"],
                                cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=25,
                                env=os.environ | {"PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired as error:
        print("QA adaptive GUI timeout", flush=True)
        print(error.stdout, error.stderr, flush=True)
        return 2
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    unchanged = before == (history.read_bytes() if history.exists() else None)
    print(json.dumps({"child_exit_code": result.returncode, "adaptive_history_unchanged": unchanged}))
    return result.returncode or (0 if unchanged else 1)


if __name__ == "__main__":
    raise SystemExit(child() if "--child" in sys.argv else parent())
