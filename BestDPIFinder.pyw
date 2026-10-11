"""Double-click launcher when Python .pyw file association is installed."""
from __future__ import annotations

import ctypes
import traceback

from main import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        ctypes.windll.user32.MessageBoxW(None, traceback.format_exc(), "BestDPI Finder 启动失败", 0x10)
