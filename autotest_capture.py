"""Non-blocking capture of the BestDPI client area for QA evidence."""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any


class WindowCapture:
    def __init__(self, hwnd: int, width: int, height: int, output: Path, fps: int = 12) -> None:
        self.hwnd, self.width, self.height = int(hwnd), int(width), int(height)
        self.output = Path(output)
        self.fps = min(12, max(8, int(fps)))
        self.frames = self.output / "frames"
        self.keyframes = self.output / "keyframes"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.keyframes.mkdir(parents=True, exist_ok=True)
        self._keys: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.count = 0
        self.error: str | None = None
        self.rect: dict[str, int] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="BestDPI-QA-capture", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self.count and not self.error and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.error:
            raise RuntimeError("Window capture failed: " + self.error)
        if not self.count:
            raise RuntimeError("Window capture did not produce the first frame within 10 seconds")

    def keyframe(self, name: str) -> None:
        self._keys.put(name)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.error = self.error or "capture thread did not stop within 5 seconds"

    def _run(self) -> None:
        try:
            import mss
            from PIL import Image
            import ctypes as ct
            from ctypes import wintypes as wt
            from native import user32

            def region() -> dict[str, int]:
                rect = wt.RECT()
                point = wt.POINT(0, 0)
                if not user32.GetClientRect(self.hwnd, ct.byref(rect)):
                    raise ct.WinError(ct.get_last_error())
                if not user32.ClientToScreen(self.hwnd, ct.byref(point)):
                    raise ct.WinError(ct.get_last_error())
                current = {"left": point.x, "top": point.y,
                           "width": rect.right, "height": rect.bottom}
                if current["width"] != self.width or current["height"] != self.height:
                    raise RuntimeError(f"client size changed: {current['width']}x{current['height']}")
                return current

            target = 1.0 / self.fps
            with mss.mss() as sct:
                while not self._stop.is_set():
                    started = time.monotonic()
                    self.rect = region()
                    image = Image.frombytes("RGB", (self.width, self.height),
                                            sct.grab(self.rect).rgb)
                    self.count += 1
                    image.save(self.frames / f"frame_{self.count:06d}.jpg",
                               "JPEG", quality=90, optimize=False, subsampling=0)
                    while True:
                        try:
                            name = self._keys.get_nowait()
                        except queue.Empty:
                            break
                        image.save(self.keyframes / f"{name}.png", "PNG")
                    remaining = target - (time.monotonic() - started)
                    if remaining > 0:
                        self._stop.wait(remaining)
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"

