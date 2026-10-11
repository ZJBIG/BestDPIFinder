"""Render only empty adaptive UI states; no synthetic scores or game input."""
from __future__ import annotations

import faulthandler
import sys
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adaptive_ui import AdaptiveApp
from native import user32
from capture_preview import capture


class AdaptivePreview(AdaptiveApp):
    def run_smoke(self) -> None:
        try:
            for page in ("setup", "test", "results", "help"):
                self.page = page
                self.place_controls()
                self.invalidate()
                user32.UpdateWindow(self.hwnd)
                capture(self.hwnd, self.width, self.height, ROOT / "tests" / f"adaptive-{page}.png")
            self.smoke_result = {"adaptive_empty_pages": 4, "personal_records": 0}
        finally:
            user32.PostMessageW(self.hwnd, 0x0010, 0, 0)


if __name__ == "__main__":
    faulthandler.dump_traceback_later(15, exit=True)
    raise SystemExit(AdaptivePreview(smoke_test=True).run())
