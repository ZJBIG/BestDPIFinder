"""独立 QA：真实 Windows ABI，加 mock 防止单元测试捕获用户鼠标。"""
from __future__ import annotations

import ctypes as ct
import unittest
from ctypes import wintypes as wt
from unittest.mock import Mock, patch

import native


class TestQANativeABI(unittest.TestCase):
    def test_process_pointer_width_and_message_types(self):
        self.assertEqual(ct.sizeof(native.WPARAM), ct.sizeof(ct.c_void_p))
        self.assertEqual(ct.sizeof(native.LPARAM), ct.sizeof(ct.c_void_p))
        self.assertEqual(ct.sizeof(native.LRESULT), ct.sizeof(ct.c_void_p))
        self.assertEqual(ct.sizeof(wt.HWND), ct.sizeof(ct.c_void_p))

    def test_rawmouse_windows_layout(self):
        self.assertEqual(ct.sizeof(native.RAWMOUSE), 24)
        self.assertEqual(native.RAWMOUSE.button_union.offset, 4)
        self.assertEqual(native.RAWMOUSE.lLastX.offset, 12)
        self.assertEqual(native.RAWMOUSE.lLastY.offset, 16)

    def test_rawinput_header_and_payload_layout(self):
        expected_header = 24 if ct.sizeof(ct.c_void_p) == 8 else 16
        self.assertEqual(ct.sizeof(native.RAWINPUTHEADER), expected_header)
        self.assertEqual(native.RAWINPUT.mouse.offset, expected_header)
        self.assertEqual(ct.sizeof(native.RAWINPUT), expected_header + 24)

    def test_pointer_sensitive_function_prototypes(self):
        self.assertIs(native.user32.CreateWindowExW.restype, wt.HWND)
        self.assertIs(native.user32.DefWindowProcW.restype, native.LRESULT)
        self.assertEqual(native.user32.DefWindowProcW.argtypes,
                         [wt.HWND, wt.UINT, native.WPARAM, native.LPARAM])
        self.assertEqual(native.user32.GetRawInputData.argtypes[-1], wt.UINT)
        self.assertIs(native.user32.LoadIconW.restype, wt.HICON)
        self.assertEqual(native.user32.LoadIconW.argtypes, [wt.HINSTANCE, wt.LPVOID])

    def test_gdi_color_channel_order(self):
        self.assertEqual(native.color("#123456"), 0x563412)


class TestQARawInputLifecycle(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.RegisterRawInputDevices.return_value = 1
        self.api.ClipCursor.return_value = 1
        self.api.ShowCursor.return_value = -1
        self.patch = patch.object(native, "user32", self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.callback = Mock()
        self.raw = native.RawMouse(1234, self.callback)
        self.rect = wt.RECT(0, 0, 800, 600)

    def packet_api(self, packet):
        def read_packet(handle, command, buffer, size, header_size):
            ct.cast(size, ct.POINTER(wt.UINT)).contents.value = ct.sizeof(packet)
            if buffer is None:
                return 0
            ct.memmove(buffer, ct.byref(packet), ct.sizeof(packet))
            return ct.sizeof(packet)
        self.api.GetRawInputData.side_effect = read_packet

    def test_start_registers_foreground_mouse_only(self):
        registrations = []

        def register(pointer, count, size):
            device = ct.cast(pointer, ct.POINTER(native.RAWINPUTDEVICE)).contents
            registrations.append((device.usUsagePage, device.usUsage,
                                  device.dwFlags, device.hwndTarget, count, size))
            return 1

        self.api.RegisterRawInputDevices.side_effect = register
        self.raw.start(self.rect)
        self.assertTrue(self.raw.active)
        self.assertEqual(registrations[0][:5], (1, 2, 0x30, 1234, 1))
        self.assertFalse(registrations[0][2] & 0x100, "不得注册后台 INPUTSINK")

    def test_start_is_idempotent(self):
        self.raw.start(self.rect)
        self.raw.start(self.rect)
        self.api.SetCapture.assert_called_once_with(1234)
        self.api.ShowCursor.assert_called_once_with(False)

    def test_stop_is_idempotent_and_balances_cursor(self):
        self.api.ShowCursor.side_effect = [1, 0, -1, 0, 1, 2]
        self.raw.start(self.rect)
        self.raw.stop()
        self.raw.stop()
        self.assertFalse(self.raw.active)
        self.assertEqual(self.raw.hidden_calls, 0)
        self.api.ReleaseCapture.assert_called_once()
        self.assertEqual(self.api.ShowCursor.call_args_list,
                         [((False,),)] * 3 + [((True,),)] * 3)
        self.assertEqual(self.api.ClipCursor.call_args_list[-1].args, (None,))

    def test_stop_unregisters_with_null_target(self):
        registrations = []

        def register(pointer, count, size):
            device = ct.cast(pointer, ct.POINTER(native.RAWINPUTDEVICE)).contents
            registrations.append((device.dwFlags, device.hwndTarget))
            return 1

        self.api.RegisterRawInputDevices.side_effect = register
        self.raw.start(self.rect)
        self.raw.stop()
        self.assertEqual(registrations[-1], (1, None))

    def test_registration_failure_does_not_capture(self):
        self.api.RegisterRawInputDevices.return_value = 0
        with self.assertRaises(OSError):
            self.raw.start(self.rect)
        self.assertFalse(self.raw.active)
        self.api.SetCapture.assert_not_called()
        self.api.ClipCursor.assert_not_called()

    def test_clip_failure_releases_capture(self):
        self.api.ClipCursor.return_value = 0
        with self.assertRaises(OSError):
            self.raw.start(self.rect)
        self.assertFalse(self.raw.active)
        self.api.ReleaseCapture.assert_called_once()
        self.assertEqual(self.api.ClipCursor.call_args_list[-1].args, (None,))

    def test_inactive_receive_does_nothing(self):
        self.raw.receive(7)
        self.api.GetRawInputData.assert_not_called()
        self.callback.assert_not_called()

    def test_relative_packet_preserves_negative_counts_and_click(self):
        packet = native.RAWINPUT()
        packet.header.dwType = 0
        packet.header.dwSize = ct.sizeof(packet)
        packet.mouse.lLastX = -123
        packet.mouse.lLastY = 456
        packet.mouse.usButtonFlags = 1
        self.packet_api(packet)
        self.raw.active = True
        self.raw.receive(7)
        self.callback.assert_called_once_with(-123, 456, True)
        self.assertEqual(self.raw.relative_packets, 1)
        self.assertEqual(self.raw.total_counts, 579)

    def test_absolute_packets_are_rejected_even_with_click(self):
        packet = native.RAWINPUT()
        packet.header.dwType = 0
        packet.mouse.usFlags = 1 | 2
        packet.mouse.usButtonFlags = 1
        packet.mouse.lLastX = 65535
        self.packet_api(packet)
        self.raw.active = True
        self.raw.receive(7)
        self.callback.assert_not_called()
        self.assertEqual(self.raw.absolute_packets, 1)
        self.assertEqual(self.raw.relative_packets, 0)
        self.assertEqual(self.raw.total_counts, 0)

    def test_nonmouse_packet_ignored(self):
        packet = native.RAWINPUT()
        packet.header.dwType = 1
        self.packet_api(packet)
        self.raw.active = True
        self.raw.receive(7)
        self.callback.assert_not_called()

    def test_invalid_packet_size_ignored(self):
        for size_value in (0, 23, ct.sizeof(native.RAWINPUT) - 1, 65537):
            def read_size(handle, command, buffer, size, header_size):
                ct.cast(size, ct.POINTER(wt.UINT)).contents.value = size_value
                return 0
            self.api.GetRawInputData.side_effect = read_size
            self.raw.active = True
            with self.subTest(size=size_value):
                self.raw.receive(7)
                self.callback.assert_not_called()

    def test_raw_api_failure_ignored(self):
        self.api.GetRawInputData.return_value = 0xFFFFFFFF
        self.raw.active = True
        self.raw.receive(7)
        self.callback.assert_not_called()


class TestQAFileDialog(unittest.TestCase):
    def test_cancel_returns_none_without_opening_real_dialog(self):
        api = Mock()
        api.GetSaveFileNameW.return_value = 0
        api.GetOpenFileNameW.return_value = 0
        api.CommDlgExtendedError.return_value = 0
        with patch.object(native, "comdlg32", api):
            self.assertIsNone(native.choose_json(0, True))
            self.assertIsNone(native.choose_json(0, False))

    def test_native_dialog_error_is_reported(self):
        api = Mock()
        api.GetSaveFileNameW.return_value = 0
        api.CommDlgExtendedError.return_value = 0x3002
        with patch.object(native, "comdlg32", api), self.assertRaises(OSError):
            native.choose_json(0, True)


class TestQASoundEffects(unittest.TestCase):
    """音效在导入时合成为内存 WAV；播放走 winsound 异步，不阻塞输入与绘制路径。"""

    def test_sound_buffers_are_well_formed(self):
        self.assertEqual({"hit", "miss", "start", "block", "done"}, set(native.SOUNDS))
        for name, data in native.SOUNDS.items():
            self.assertEqual(data[:4], b"RIFF", f"{name} 应为内存 WAV")
            self.assertLess(len(data), 100_000, f"{name} 应是一小段提示音")

    def test_play_sound_passes_buffer_to_winmm_async(self):
        with patch.object(native.winmm, "PlaySoundW") as play:
            native.play_sound("hit")
            self.assertEqual(play.call_count, 1)
            data, module, flags = play.call_args[0]
            self.assertIs(data, native.SOUNDS["hit"])
            self.assertEqual(flags, 0x0004 | 0x0001 | 0x0002)  # SND_MEMORY|SND_ASYNC|SND_NODEFAULT
            native.play_sound("no-such")
            self.assertEqual(play.call_count, 1, "未知音效应静默忽略")


if __name__ == "__main__":
    unittest.main(verbosity=2)
