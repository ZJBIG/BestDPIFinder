"""Capture the app's own empty-state pages; no mouse injection or personal scores."""
from __future__ import annotations

import ctypes as ct
import struct
import sys
import zlib
from ctypes import wintypes as wt
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from native import bind, gdi32, user32
from win32_app import DesktopApp


class BitmapInfoHeader(ct.Structure):
    _fields_ = [("size", wt.DWORD), ("width", wt.LONG), ("height", wt.LONG),
                ("planes", wt.WORD), ("bit_count", wt.WORD), ("compression", wt.DWORD),
                ("image_size", wt.DWORD), ("x_ppm", wt.LONG), ("y_ppm", wt.LONG),
                ("colors_used", wt.DWORD), ("colors_important", wt.DWORD)]


bind(user32, "GetDC", wt.HDC, [wt.HWND])
bind(user32, "ReleaseDC", ct.c_int, [wt.HWND, wt.HDC])
bind(gdi32, "GetDIBits", ct.c_int, [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, wt.LPVOID, wt.LPVOID, wt.UINT])


def chunk(name: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)


def capture(hwnd: int, width: int, height: int, destination: Path) -> None:
    source = user32.GetDC(hwnd)
    memory = gdi32.CreateCompatibleDC(source)
    bitmap = gdi32.CreateCompatibleBitmap(source, width, height)
    previous = gdi32.SelectObject(memory, bitmap)
    try:
        if not gdi32.BitBlt(memory, 0, 0, width, height, source, 0, 0, 0x00CC0020):
            raise ct.WinError(ct.get_last_error())
        gdi32.SelectObject(memory, previous)
        info = BitmapInfoHeader(ct.sizeof(BitmapInfoHeader), width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
        buffer = ct.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ct.byref(info), 0):
            raise ct.WinError(ct.get_last_error())
        bgra: bytes = buffer.raw
        rows = bytearray()
        for y in range(height):
            rows.append(0)
            row: bytes = bgra[y * width * 4:(y + 1) * width * 4]
            rgb = bytearray(width * 3)
            rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
            rows.extend(rgb)
        image: bytes = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        image += chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")
        destination.write_bytes(image)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(hwnd, source)


class PreviewApp(DesktopApp):
    def run_smoke(self) -> None:
        try:
            for page in ("setup", "test", "results", "help"):
                self.page = page
                self.place_controls()
                self.invalidate()
                user32.UpdateWindow(self.hwnd)
                capture(self.hwnd, self.width, self.height, ROOT / "tests" / f"preview-{page}.png")
            self.smoke_result = {"preview_pages": 4, "personal_records": len(self.records)}
        finally:
            user32.PostMessageW(self.hwnd, 0x0010, 0, 0)


if __name__ == "__main__":
    import faulthandler
    faulthandler.dump_traceback_later(15, exit=True)
    raise SystemExit(PreviewApp(smoke_test=True).run())
