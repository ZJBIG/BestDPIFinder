"""Small typed Win32/GDI binding. Relative mouse input comes only from WM_INPUT."""
from __future__ import annotations

import ctypes as ct
import math
import struct
from ctypes import wintypes as wt
from typing import Callable

user32 = ct.WinDLL("user32", use_last_error=True)
gdi32 = ct.WinDLL("gdi32", use_last_error=True)
kernel32 = ct.WinDLL("kernel32", use_last_error=True)
comdlg32 = ct.WinDLL("comdlg32", use_last_error=True)
winmm = ct.WinDLL("winmm", use_last_error=True)
LRESULT = ct.c_ssize_t
WPARAM = ct.c_size_t
LPARAM = ct.c_ssize_t
WNDPROC = ct.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)


class WNDCLASS(ct.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ct.c_int),
                ("cbWndExtra", ct.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class PAINTSTRUCT(ct.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL), ("rgbReserved", wt.BYTE * 32)]


class RAWINPUTDEVICE(ct.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ct.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD), ("hDevice", wt.HANDLE), ("wParam", WPARAM)]


class BUTTONS(ct.Structure):
    _fields_ = [("usButtonFlags", wt.USHORT), ("usButtonData", wt.USHORT)]


class BUTTONUNION(ct.Union):
    _anonymous_ = ("buttons",)
    _fields_ = [("ulButtons", wt.ULONG), ("buttons", BUTTONS)]


class RAWMOUSE(ct.Structure):
    _anonymous_ = ("button_union",)
    _fields_ = [("usFlags", wt.USHORT), ("button_union", BUTTONUNION),
                ("ulRawButtons", wt.ULONG), ("lLastX", wt.LONG), ("lLastY", wt.LONG),
                ("ulExtraInformation", wt.ULONG)]


class RAWINPUT(ct.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


class OPENFILENAME(ct.Structure):
    _fields_ = [("lStructSize", wt.DWORD), ("hwndOwner", wt.HWND), ("hInstance", wt.HINSTANCE),
                ("lpstrFilter", wt.LPCWSTR), ("lpstrCustomFilter", wt.LPWSTR),
                ("nMaxCustFilter", wt.DWORD), ("nFilterIndex", wt.DWORD), ("lpstrFile", wt.LPWSTR),
                ("nMaxFile", wt.DWORD), ("lpstrFileTitle", wt.LPWSTR), ("nMaxFileTitle", wt.DWORD),
                ("lpstrInitialDir", wt.LPCWSTR), ("lpstrTitle", wt.LPCWSTR), ("Flags", wt.DWORD),
                ("nFileOffset", wt.WORD), ("nFileExtension", wt.WORD), ("lpstrDefExt", wt.LPCWSTR),
                ("lCustData", LPARAM), ("lpfnHook", wt.LPVOID), ("lpTemplateName", wt.LPCWSTR),
                ("pvReserved", wt.LPVOID), ("dwReserved", wt.DWORD), ("FlagsEx", wt.DWORD)]


def bind(dll: ct.WinDLL, name: str, returns: object, args: list[object]) -> object:
    function = getattr(dll, name)
    function.restype = returns
    function.argtypes = args
    return function


bind(kernel32, "GetModuleHandleW", wt.HMODULE, [wt.LPCWSTR])
bind(user32, "RegisterClassW", wt.ATOM, [ct.POINTER(WNDCLASS)])
bind(user32, "CreateWindowExW", wt.HWND, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID])
bind(user32, "DefWindowProcW", LRESULT, [wt.HWND, wt.UINT, WPARAM, LPARAM])
bind(user32, "DestroyWindow", wt.BOOL, [wt.HWND])
bind(user32, "ShowWindow", wt.BOOL, [wt.HWND, ct.c_int])
bind(user32, "UpdateWindow", wt.BOOL, [wt.HWND])
bind(user32, "GetMessageW", wt.BOOL, [ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT])
bind(user32, "PeekMessageW", wt.BOOL, [ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT])
bind(user32, "MsgWaitForMultipleObjectsEx", wt.DWORD,
     [wt.DWORD, ct.POINTER(wt.HANDLE), wt.DWORD, wt.DWORD, wt.DWORD])
bind(user32, "TranslateMessage", wt.BOOL, [ct.POINTER(wt.MSG)])
bind(user32, "DispatchMessageW", LRESULT, [ct.POINTER(wt.MSG)])
bind(user32, "GetDC", wt.HDC, [wt.HWND])
bind(user32, "ReleaseDC", ct.c_int, [wt.HWND, wt.HDC])
bind(user32, "ValidateRect", wt.BOOL, [wt.HWND, ct.POINTER(wt.RECT)])
bind(user32, "PostQuitMessage", None, [ct.c_int])
bind(user32, "PostMessageW", wt.BOOL, [wt.HWND, wt.UINT, WPARAM, LPARAM])
bind(user32, "SendMessageW", LRESULT, [wt.HWND, wt.UINT, WPARAM, LPARAM])
bind(user32, "SetTimer", ct.c_size_t, [wt.HWND, ct.c_size_t, wt.UINT, wt.LPVOID])
bind(user32, "KillTimer", wt.BOOL, [wt.HWND, ct.c_size_t])
bind(user32, "GetClientRect", wt.BOOL, [wt.HWND, ct.POINTER(wt.RECT)])
bind(user32, "ClientToScreen", wt.BOOL, [wt.HWND, ct.POINTER(wt.POINT)])
bind(user32, "GetWindowRect", wt.BOOL, [wt.HWND, ct.POINTER(wt.RECT)])
bind(user32, "MoveWindow", wt.BOOL, [wt.HWND, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.BOOL])
bind(user32, "InvalidateRect", wt.BOOL, [wt.HWND, ct.POINTER(wt.RECT), wt.BOOL])
bind(user32, "BeginPaint", wt.HDC, [wt.HWND, ct.POINTER(PAINTSTRUCT)])
bind(user32, "EndPaint", wt.BOOL, [wt.HWND, ct.POINTER(PAINTSTRUCT)])
bind(user32, "FillRect", ct.c_int, [wt.HDC, ct.POINTER(wt.RECT), wt.HBRUSH])
bind(user32, "DrawTextW", ct.c_int, [wt.HDC, wt.LPCWSTR, ct.c_int, ct.POINTER(wt.RECT), wt.UINT])
bind(user32, "LoadIconW", wt.HICON, [wt.HINSTANCE, wt.LPVOID])
bind(user32, "LoadCursorW", wt.HANDLE, [wt.HINSTANCE, wt.LPVOID])
bind(user32, "SetCursor", wt.HANDLE, [wt.HANDLE])
bind(user32, "ShowCursor", ct.c_int, [wt.BOOL])
bind(user32, "SetCapture", wt.HWND, [wt.HWND])
bind(user32, "ReleaseCapture", wt.BOOL, [])
bind(user32, "ClipCursor", wt.BOOL, [ct.POINTER(wt.RECT)])
bind(user32, "GetClipCursor", wt.BOOL, [ct.POINTER(wt.RECT)])
bind(user32, "GetCursorPos", wt.BOOL, [ct.POINTER(wt.POINT)])
bind(user32, "SetFocus", wt.HWND, [wt.HWND])
bind(user32, "SetForegroundWindow", wt.BOOL, [wt.HWND])
bind(user32, "GetForegroundWindow", wt.HWND, [])
bind(user32, "GetWindowTextW", ct.c_int, [wt.HWND, wt.LPWSTR, ct.c_int])
bind(user32, "SetWindowTextW", wt.BOOL, [wt.HWND, wt.LPCWSTR])
bind(user32, "EnableWindow", wt.BOOL, [wt.HWND, wt.BOOL])
bind(user32, "MessageBoxW", ct.c_int, [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT])
bind(user32, "RegisterRawInputDevices", wt.BOOL, [ct.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT])
bind(user32, "GetRawInputData", wt.UINT, [wt.HANDLE, wt.UINT, wt.LPVOID, ct.POINTER(wt.UINT), wt.UINT])
bind(user32, "GetSystemMetrics", ct.c_int, [ct.c_int])
bind(gdi32, "CreateSolidBrush", wt.HBRUSH, [wt.DWORD])
bind(gdi32, "CreatePen", wt.HANDLE, [ct.c_int, ct.c_int, wt.DWORD])
bind(gdi32, "DeleteObject", wt.BOOL, [wt.HANDLE])
bind(gdi32, "SelectObject", wt.HANDLE, [wt.HDC, wt.HANDLE])
bind(gdi32, "CreateCompatibleDC", wt.HDC, [wt.HDC])
bind(gdi32, "DeleteDC", wt.BOOL, [wt.HDC])
bind(gdi32, "CreateCompatibleBitmap", wt.HBITMAP, [wt.HDC, ct.c_int, ct.c_int])
bind(gdi32, "BitBlt", wt.BOOL, [wt.HDC, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.HDC, ct.c_int, ct.c_int, wt.DWORD])
bind(gdi32, "SetTextColor", wt.DWORD, [wt.HDC, wt.DWORD])
bind(gdi32, "SetBkMode", ct.c_int, [wt.HDC, ct.c_int])
bind(gdi32, "CreateFontW", wt.HFONT, [ct.c_int, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR])
bind(gdi32, "RoundRect", wt.BOOL, [wt.HDC, ct.c_int, ct.c_int, ct.c_int, ct.c_int, ct.c_int, ct.c_int])
bind(gdi32, "Ellipse", wt.BOOL, [wt.HDC, ct.c_int, ct.c_int, ct.c_int, ct.c_int])
bind(gdi32, "MoveToEx", wt.BOOL, [wt.HDC, ct.c_int, ct.c_int, ct.POINTER(wt.POINT)])
bind(gdi32, "LineTo", wt.BOOL, [wt.HDC, ct.c_int, ct.c_int])
bind(gdi32, "SaveDC", ct.c_int, [wt.HDC])
bind(gdi32, "RestoreDC", wt.BOOL, [wt.HDC, ct.c_int])
bind(gdi32, "IntersectClipRect", ct.c_int, [wt.HDC, ct.c_int, ct.c_int, ct.c_int, ct.c_int])
bind(comdlg32, "GetSaveFileNameW", wt.BOOL, [ct.POINTER(OPENFILENAME)])
bind(comdlg32, "GetOpenFileNameW", wt.BOOL, [ct.POINTER(OPENFILENAME)])
bind(comdlg32, "CommDlgExtendedError", wt.DWORD, [])
# SND_MEMORY 下 pszSound 是内存映像指针，与编码无关；缓冲必须常驻（SOUNDS 模块级引用）。
bind(winmm, "PlaySoundW", wt.BOOL, [ct.c_char_p, wt.HMODULE, wt.DWORD])
bind(winmm, "timeBeginPeriod", ct.c_uint, [ct.c_uint])
bind(winmm, "timeEndPeriod", ct.c_uint, [ct.c_uint])


# --- GDI+（系统自带 gdiplus.dll，ctypes 直调，零第三方依赖）：抗锯齿、渐变、半透明 -----------

class GpPointF(ct.Structure):
    _fields_ = [("X", ct.c_float), ("Y", ct.c_float)]


class GdiplusStartupInput(ct.Structure):
    _fields_ = [("GdiplusVersion", wt.UINT), ("DebugEventCallback", wt.LPVOID),
                ("SuppressBackgroundThread", wt.BOOL), ("SuppressExternalCodecs", wt.BOOL)]


gdiplus = ct.WinDLL("gdiplus")
bind(gdiplus, "GdiplusStartup", ct.c_int, [ct.POINTER(wt.ULONG), ct.POINTER(GdiplusStartupInput), wt.LPVOID])
bind(gdiplus, "GdiplusShutdown", None, [wt.ULONG])
bind(gdiplus, "GdipCreateFromHDC", ct.c_int, [wt.HDC, ct.POINTER(ct.c_void_p)])
bind(gdiplus, "GdipDeleteGraphics", ct.c_int, [ct.c_void_p])
bind(gdiplus, "GdipSetSmoothingMode", ct.c_int, [ct.c_void_p, ct.c_int])
bind(gdiplus, "GdipCreateSolidFill", ct.c_int, [wt.DWORD, ct.POINTER(ct.c_void_p)])
bind(gdiplus, "GdipCreatePen1", ct.c_int, [wt.DWORD, ct.c_float, ct.c_int, ct.POINTER(ct.c_void_p)])
bind(gdiplus, "GdipDeletePen", ct.c_int, [ct.c_void_p])
bind(gdiplus, "GdipDeleteBrush", ct.c_int, [ct.c_void_p])
bind(gdiplus, "GdipCreateLineBrush", ct.c_int, [ct.POINTER(GpPointF), ct.POINTER(GpPointF), wt.DWORD,
                                                wt.DWORD, ct.c_int, ct.POINTER(ct.c_void_p)])
bind(gdiplus, "GdipCreatePath", ct.c_int, [ct.c_int, ct.POINTER(ct.c_void_p)])
bind(gdiplus, "GdipAddPathArc", ct.c_int, [ct.c_void_p, ct.c_float, ct.c_float, ct.c_float,
                                           ct.c_float, ct.c_float, ct.c_float])
bind(gdiplus, "GdipClosePathFigure", ct.c_int, [ct.c_void_p])
bind(gdiplus, "GdipDeletePath", ct.c_int, [ct.c_void_p])
bind(gdiplus, "GdipFillPath", ct.c_int, [ct.c_void_p, ct.c_void_p, ct.c_void_p])
bind(gdiplus, "GdipDrawPath", ct.c_int, [ct.c_void_p, ct.c_void_p, ct.c_void_p])
bind(gdiplus, "GdipFillRectangle", ct.c_int, [ct.c_void_p, ct.c_void_p, ct.c_float, ct.c_float,
                                              ct.c_float, ct.c_float])
bind(gdiplus, "GdipDrawLine", ct.c_int, [ct.c_void_p, ct.c_void_p, ct.c_float, ct.c_float,
                                         ct.c_float, ct.c_float])
bind(gdiplus, "GdipFillEllipse", ct.c_int, [ct.c_void_p, ct.c_void_p, ct.c_float, ct.c_float,
                                            ct.c_float, ct.c_float])
bind(gdiplus, "GdipDrawEllipse", ct.c_int, [ct.c_void_p, ct.c_void_p, ct.c_float, ct.c_float,
                                            ct.c_float, ct.c_float])


def argb(hex_color: str) -> int:
    """"RRGGBB"（不透明）或 "AARRGGBB"（半透明光效）→ GDI+ 的 ARGB 整数。"""
    value: str = hex_color.lstrip("#")
    alpha: int = int(value[0:2], 16) if len(value) == 8 else 255
    red, green, blue = (int(value[-6:][index:index + 2], 16) for index in (0, 2, 4))
    return (alpha << 24) | (red << 16) | (green << 8) | blue


# --- 音效：内存合成的 WAV 经 winmm 异步播放，不阻塞输入与绘制路径 ----------------------------

_RATE: int = 22050


def _wav(samples: list[float]) -> bytes:
    """把 [-1,1] 浮点采样打包成 22050 Hz / 16 bit / 单声道的内存 WAV。"""
    frames: bytes = b"".join(struct.pack("<h", max(-32767, min(32767, int(sample * 32767))))
                             for sample in samples)
    return (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, _RATE, _RATE * 2, 2, 16)
            + b"data" + struct.pack("<I", len(frames)) + frames)


def _tone(start: float, end: float, seconds: float, amp: float, decay: float) -> list[float]:
    """一段正弦扫频采样，指数衰减包络（decay 为秒级时间常数）。"""
    count: int = int(_RATE * seconds)
    return [amp * math.sin(2 * math.pi * (start + (end - start) * position / count)
                           * position / _RATE) * math.exp(-position / _RATE / decay)
            for position in range(count)]


SOUNDS: dict[str, bytes] = {
    "hit": _wav(_tone(1500, 2100, 0.07, 0.5, 0.02)),
    "miss": _wav(_tone(190, 130, 0.12, 0.45, 0.045)),
    "start": _wav(_tone(380, 820, 0.16, 0.35, 0.07)),
    "block": _wav(_tone(659, 659, 0.1, 0.4, 0.04) + _tone(880, 880, 0.14, 0.4, 0.05)),
    "done": _wav(_tone(440, 440, 0.12, 0.38, 0.045) + _tone(554, 554, 0.12, 0.38, 0.045)
                 + _tone(659, 659, 0.18, 0.38, 0.07)),
}


def play_sound(name: str) -> None:
    """非阻塞播放内置音效；未知名称静默忽略，无音频设备时 winmm 自行静默处理。

    Python 的 winsound 拒绝 SND_ASYNC+SND_MEMORY 组合，所以这里直接调 winmm：
    C 层支持内存异步播放，前提是缓冲在播完前保持有效——SOUNDS 常驻模块级引用满足。
    """
    data: bytes | None = SOUNDS.get(name)
    if data:
        winmm.PlaySoundW(data, None, 0x0004 | 0x0001 | 0x0002)  # SND_MEMORY|SND_ASYNC|SND_NODEFAULT


def color(hex_color: str) -> int:
    # 兼容 8 位 "AARRGGBB"（GDI 无 alpha，取末 6 位按不透明处理）。
    value: str = hex_color.lstrip("#")[-6:]
    red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
    return red | (green << 8) | (blue << 16)


class RawMouse:
    """Register only while explicitly captured; release is safe and idempotent."""
    def __init__(self, hwnd: int, callback: Callable[[int, int, bool], None]) -> None:
        self.hwnd: int = hwnd
        self.callback = callback
        self.active: bool = False
        self.hidden_calls: int = 0
        self.relative_packets: int = 0
        self.absolute_packets: int = 0
        self.total_counts: int = 0
        self._packet_buffer = ct.create_string_buffer(ct.sizeof(RAWINPUT))

    def start(self, rect: wt.RECT) -> None:
        if self.active:
            return
        device = RAWINPUTDEVICE(1, 2, 0x30, self.hwnd)  # RIDEV_NOLEGACY, foreground only.
        if not user32.RegisterRawInputDevices(ct.byref(device), 1, ct.sizeof(device)):
            raise ct.WinError(ct.get_last_error())
        self.active = True
        user32.SetCapture(self.hwnd)
        if not user32.ClipCursor(ct.byref(rect)):
            self.stop()
            raise ct.WinError(ct.get_last_error())
        self.hidden_calls = 0
        while self.hidden_calls < 20:
            remaining: int = user32.ShowCursor(False)
            self.hidden_calls += 1
            if remaining < 0:
                break
        self.relative_packets = 0
        self.absolute_packets = 0
        self.total_counts = 0

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        # Remove this app's raw registration; no system mouse setting is modified.
        device = RAWINPUTDEVICE(1, 2, 0x01, None)  # RIDEV_REMOVE requires NULL target.
        user32.RegisterRawInputDevices(ct.byref(device), 1, ct.sizeof(device))
        user32.ClipCursor(None)
        user32.ReleaseCapture()
        for _ in range(self.hidden_calls):
            user32.ShowCursor(True)
        self.hidden_calls = 0

    def receive(self, handle: int) -> None:
        if not self.active:
            return
        size = wt.UINT(len(self._packet_buffer))
        header_size: int = ct.sizeof(RAWINPUTHEADER)
        result: int = user32.GetRawInputData(
            handle, 0x10000003, self._packet_buffer, ct.byref(size), header_size)
        if result == 0xFFFFFFFF or result < ct.sizeof(RAWINPUT) or size.value > len(self._packet_buffer):
            return
        packet: RAWINPUT = RAWINPUT.from_buffer(self._packet_buffer)
        if packet.header.dwType != 0:
            return
        if packet.mouse.usFlags & 1:  # MOUSE_MOVE_ABSOLUTE is not relative raw input.
            self.absolute_packets += 1
            return
        self.relative_packets += 1
        self.total_counts += abs(packet.mouse.lLastX) + abs(packet.mouse.lLastY)
        self.callback(packet.mouse.lLastX, packet.mouse.lLastY, bool(packet.mouse.usButtonFlags & 1))


def choose_json(hwnd: int, save: bool) -> str | None:
    buffer = ct.create_unicode_buffer("BestDPIFinder-result.json" if save else "", 32768)
    dialog = OPENFILENAME()
    dialog.lStructSize = ct.sizeof(OPENFILENAME)
    dialog.hwndOwner = hwnd
    dialog.lpstrFilter = "JSON 测评结果\0*.json\0\0"
    dialog.lpstrFile = ct.cast(buffer, wt.LPWSTR)
    dialog.nMaxFile = len(buffer)
    dialog.lpstrDefExt = "json"
    dialog.lpstrTitle = "导出测评结果" if save else "打开历史测评结果"
    dialog.Flags = 0x00080000 | 0x00000800 | (0x2 if save else 0x1000)
    success: bool = bool(comdlg32.GetSaveFileNameW(ct.byref(dialog)) if save else comdlg32.GetOpenFileNameW(ct.byref(dialog)))
    if not success:
        error: int = comdlg32.CommDlgExtendedError()
        if error:
            raise OSError(f"无法打开文件选择窗口（错误码 {error}）。")
        return None
    return buffer.value
