"""独立原生窗口冒烟：仅向本子进程创建的 HWND 发消息，不模拟个人测评。"""
from __future__ import annotations

import ctypes as ct
import json
import struct
import subprocess
import sys
import tempfile
from ctypes import wintypes as wt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def clip_rectangle(user32):
    rectangle = wt.RECT()
    if not user32.GetClipCursor(ct.byref(rectangle)):
        raise ct.WinError(ct.get_last_error())
    return (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)


def screenshot_own_window(hwnd, path):
    """仅截取传入的本应用客户区，编码标准 PNG；不读取其他窗口。"""
    import zlib
    from native import bind, gdi32, user32

    class BitmapInfoHeader(ct.Structure):
        _fields_ = [("size", wt.DWORD), ("width", wt.LONG), ("height", wt.LONG),
                    ("planes", wt.WORD), ("bits", wt.WORD), ("compression", wt.DWORD),
                    ("image_size", wt.DWORD), ("xppm", wt.LONG), ("yppm", wt.LONG),
                    ("colors", wt.DWORD), ("important", wt.DWORD)]

    bind(user32, "GetDC", wt.HDC, [wt.HWND])
    bind(user32, "ReleaseDC", ct.c_int, [wt.HWND, wt.HDC])
    bind(gdi32, "GetDIBits", ct.c_int,
         [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, wt.LPVOID, wt.LPVOID, wt.UINT])
    rectangle = wt.RECT()
    user32.GetClientRect(hwnd, ct.byref(rectangle))
    width, height = rectangle.right, rectangle.bottom
    source = user32.GetDC(hwnd)
    memory = gdi32.CreateCompatibleDC(source)
    bitmap = gdi32.CreateCompatibleBitmap(source, width, height)
    old = gdi32.SelectObject(memory, bitmap)
    try:
        if not gdi32.BitBlt(memory, 0, 0, width, height, source, 0, 0, 0x00CC0020):
            raise ct.WinError(ct.get_last_error())
        gdi32.SelectObject(memory, old)
        info = BitmapInfoHeader()
        info.size, info.width, info.height = ct.sizeof(info), width, -height
        info.planes, info.bits = 1, 32
        pixels = ct.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(source, bitmap, 0, height, pixels, ct.byref(info), 0) != height:
            raise ct.WinError(ct.get_last_error())
        bgra = pixels.raw
        rgb = bytearray(width * height * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
        rows = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3]
                        for y in range(height))

        def chunk(kind, payload):
            return (struct.pack(">I", len(payload)) + kind + payload
                    + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
        Path(path).write_bytes(png)
    finally:
        gdi32.SelectObject(memory, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(hwnd, source)


def child_probe():
    import faulthandler
    faulthandler.dump_traceback_later(10, exit=False)
    print("QA child: entering real DesktopApp startup", flush=True)
    from native import user32
    from win32_app import DesktopApp

    class QAWindow(DesktopApp):
        def __init__(self, directory):
            super().__init__(smoke_test=True)
            self.history_path = Path(directory) / "QA-NOT-PERSONAL.json"
            self.qa_checks = []
            # 与 DesktopApp.run 使用一致的 DPI 上下文后再比较 ClipCursor 坐标。
            user32.SetProcessDPIAware()
            self.initial_clip = clip_rectangle(user32)

        def check(self, name, condition):
            print(json.dumps({"qa_step": name, "passed": bool(condition)}, ensure_ascii=False), flush=True)
            self.qa_checks.append({"check": name, "passed": bool(condition)})
            if not condition:
                raise AssertionError(name)

        def click_own_button(self, action):
            print(json.dumps({"qa_action": action}, ensure_ascii=False), flush=True)
            self.invalidate()
            user32.UpdateWindow(self.hwnd)
            bounds = next(bounds for bounds, name in self.buttons if name == action)
            left, top, right, bottom = bounds
            x, y = (left + right) // 2, (top + bottom) // 2
            user32.SendMessageW(self.hwnd, 0x0202, 0, (y << 16) | x)

        def run_smoke(self):
            try:
                self.check("启动未自动捕获", not self.active and not self.raw.active)
                self.check("启动未限制鼠标", clip_rectangle(user32) == self.initial_clip)
                screenshot_own_window(self.hwnd, ROOT / "tests" / "qa_setup.png")
                user32.SetWindowTextW(self.controls["dpi"], "800")
                user32.SetWindowTextW(self.controls["sens"], "0.4")
                self.click_own_button("prepare")
                self.check("真实设置控件换算", abs(self.settings.distance - 40.82142857142857) < 1e-10)
                self.check("准备生成六组两轮但未捕获", len(self.plan) == 6 and not self.active)
                self.click_own_button("capture")
                self.check("真实鼠标注册与捕获", self.active and self.raw.active)
                self.check("真实ClipCursor限制在本窗口", clip_rectangle(user32) != self.initial_clip)
                user32.SendMessageW(self.hwnd, 0x0100, 0x1B, 0)
                user32.SendMessageW(self.hwnd, 0x0100, 0x1B, 0)
                self.check("Esc幂等释放", not self.active and not self.raw.active and self.aborted == 1)
                self.check("Esc真实恢复ClipCursor", clip_rectangle(user32) == self.initial_clip)
                self.click_own_button("capture")
                user32.SendMessageW(self.hwnd, 0x001C, 0, 0)
                self.check("WM_ACTIVATEAPP失焦释放", not self.active and not self.raw.active)
                self.check("失焦真实恢复ClipCursor", clip_rectangle(user32) == self.initial_clip)
                for page in ("setup", "test", "results", "help"):
                    self.click_own_button(page)
                    self.check("原生页面绘制:" + page, self.page == page and not self.fatal_error)
                    self.invalidate()
                    user32.UpdateWindow(self.hwnd)
                    screenshot_own_window(self.hwnd, ROOT / "tests" / ("qa_" + page + ".png"))
                self.click_own_button("test")
                self.click_own_button("capture")
                self.check("关闭前处于捕获", self.active and self.raw.active)
                user32.SendMessageW(self.hwnd, 0x0010, 0, 0)
                self.check("关闭释放", not self.active and not self.raw.active)
                self.check("关闭真实恢复ClipCursor", clip_rectangle(user32) == self.initial_clip)
                self.check("未生成正式成绩", not self.records and not self.stage_records and self.trial_index == 0)
                self.check("未写测试历史", not self.history_path.exists())
            except Exception as error:
                self.fatal_error = repr(error)
                self.raw.stop()
                user32.PostMessageW(self.hwnd, 0x0010, 0, 0)
            finally:
                self.smoke_result = {"qa_synthetic_probe": True, "checks": self.qa_checks,
                                     "error": self.fatal_error, "personal_result_generated": False}
                # Close the window through WM_CLOSE rather than letting the probe return, because
                # this override never reaches DesktopApp.run's post-loop `emit`. WM_CLOSE ->
                # DestroyWindow -> WM_DESTROY -> PostQuitMessage ends the message loop, and
                # run() then prints smoke_result to stdout for the parent to parse. Returning
                # early leaves the window alive, GetMessageW blocks forever, and the parent
                # kills the child on its 25 s timeout -- after every check has already passed.
                user32.PostMessageW(self.hwnd, 0x0010, 0, 0)

    with tempfile.TemporaryDirectory(prefix="bestdpi-qa-gui-") as directory:
        return QAWindow(directory).run()


def parent_probe():
    history = ROOT / "results" / "latest.json"
    before = history.read_bytes() if history.exists() else None
    try:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=25,
            env=__import__("os").environ | {"PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired as error:
        print("QA GUI TIMEOUT", flush=True)
        for stream in (error.stdout, error.stderr):
            if stream:
                print(stream.decode("utf-8", "replace") if isinstance(stream, bytes) else stream, flush=True)
        return 2
    print(process.stdout, end="")
    if process.stderr:
        print(process.stderr, file=sys.stderr, end="")
    after = history.read_bytes() if history.exists() else None
    unchanged = before == after
    print(json.dumps({"child_exit_code": process.returncode,
                      "real_history_unchanged": unchanged}, ensure_ascii=False))
    return process.returncode if process.returncode else (0 if unchanged else 1)


if __name__ == "__main__":
    raise SystemExit(child_probe() if "--child" in sys.argv else parent_probe())
