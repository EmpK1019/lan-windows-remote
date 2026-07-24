from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call, patch

import lan_remote


class FakeResponse:
    def __init__(self, body: bytes, url: str, headers: dict[str, str] | None = None) -> None:
        self._stream = io.BytesIO(body)
        self._url = url
        self.headers = headers or {"Content-Length": str(len(body))}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url


def make_state(settings: lan_remote.SettingsStore) -> lan_remote.ServerState:
    return lan_remote.ServerState(
        token="TEST-TEMP-CODE",
        token_expires_at=time.time() + 600,
        view_only=False,
        allow_non_lan=False,
        started_at=time.time(),
        device_id="test-device",
        device_name="Test device",
        port=lan_remote.DEFAULT_PORT,
        registry=lan_remote.DiscoveryRegistry(),
        settings=settings,
    )


class CoreFunctionTests(unittest.TestCase):
    def test_service_session_state_is_fresh_known_and_session_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "session-state.json"

            def publish(**changes: object) -> None:
                payload: dict[str, object] = {
                    "version": 1,
                    "session_id": 7,
                    "locked": True,
                    "known": True,
                    "updated_at_ms": 1_000_000,
                }
                payload.update(changes)
                state_path.write_text(json.dumps(payload), encoding="utf-8")

            publish()
            self.assertIs(lan_remote.service_session_locked(state_path, now_ms=1_001_000, session_id=7), True)
            publish(locked=False)
            self.assertIs(lan_remote.service_session_locked(state_path, now_ms=1_001_000, session_id=7), False)
            publish(known=False)
            self.assertIsNone(lan_remote.service_session_locked(state_path, now_ms=1_001_000, session_id=7))
            publish(session_id=8)
            self.assertIsNone(lan_remote.service_session_locked(state_path, now_ms=1_001_000, session_id=7))
            publish(updated_at_ms=990_000)
            self.assertIsNone(lan_remote.service_session_locked(state_path, now_ms=1_001_000, session_id=7))

    def test_locked_default_desktop_uses_normal_input_but_native_video_falls_back(self) -> None:
        with (
            patch.object(lan_remote, "current_session_locked", return_value=True),
            patch.object(lan_remote, "input_desktop_name", return_value="Default"),
        ):
            self.assertFalse(lan_remote.secure_desktop_active())
            self.assertTrue(lan_remote.native_video_requires_compatibility())

    def test_session_ui_state_covers_unlock_transitions_without_guessing(self) -> None:
        self.assertEqual(lan_remote.session_ui_state(False, "Default"), "unlocked")
        self.assertEqual(lan_remote.session_ui_state(False, "Winlogon"), "secure_prompt")
        self.assertEqual(lan_remote.session_ui_state(True, "Default", "LockApp.exe"), "lock_screen")
        self.assertEqual(lan_remote.session_ui_state(True, "Winlogon", "LockApp.exe"), "lock_screen")
        self.assertEqual(lan_remote.session_ui_state(True, "Winlogon", "LogonUI.exe"), "credential_ui")
        self.assertEqual(lan_remote.session_ui_state(True, "Winlogon", None), "locked_transition")
        self.assertEqual(lan_remote.session_ui_state(True, "Disconnect"), "locked_transition")
        self.assertEqual(lan_remote.session_ui_state(True, None), "unknown")

    def test_locked_default_desktop_wake_input_is_not_sent_to_winlogon_helper(self) -> None:
        state = Mock()
        state.settings.values = {"secure_desktop_enabled": True}
        payload = {"type": "key_press", "key": "Enter", "code": "Enter"}
        with (
            patch.object(lan_remote, "current_session_locked", return_value=True),
            patch.object(lan_remote, "input_desktop_name", return_value="Default"),
            patch.object(lan_remote, "try_send_elevated_input", return_value=True) as elevated,
            patch.object(lan_remote, "send_secure_input") as secure,
        ):
            lan_remote.dispatch_remote_input(state, payload)
        elevated.assert_called_once_with(payload)
        secure.assert_not_called()

    def test_lock_workstation_confirms_and_falls_back_to_active_session_disconnect(self) -> None:
        with (
            patch.object(lan_remote.platform, "system", return_value="Windows"),
            patch.object(lan_remote, "active_console_session_id", return_value=7),
            patch.object(lan_remote, "request_workstation_lock", return_value=True) as request_lock,
            patch.object(lan_remote, "wait_for_windows_session_lock", side_effect=[False, True]) as wait_locked,
            patch.object(lan_remote, "disconnect_windows_session", return_value=True) as disconnect,
        ):
            lan_remote.lock_remote_workstation()
        request_lock.assert_called_once_with()
        disconnect.assert_called_once_with(7)
        self.assertEqual(wait_locked.call_args_list, [call(7, 1.0), call(7)])

    def test_lock_events_and_first_frame_gate_are_wired_into_native_paths(self) -> None:
        root = Path(__file__).resolve().parents[1]
        service = (root / "packaging" / "SecureDesktopService.cs").read_text(encoding="utf-8")
        native = (root / "native" / "WindowsLANRemoteVideo.cpp").read_text(encoding="utf-8")
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("SessionChangeReason.SessionLock", service)
        self.assertIn("SessionChangeReason.SessionUnlock", service)
        self.assertIn('Path.Combine(directory, "session-state.json")', service)
        self.assertIn("helpersNeedRefresh = false;", service)
        self.assertIn('"decoding"', native)
        self.assertIn("rendered_frames_.load() > first_frame_rendered_count", native)
        self.assertIn("next_first_frame_keyframe", native)
        self.assertIn("function startNativeVideoPreview(session)", html)
        self.assertIn("Number(status.rendered_frames || 0) > 0", html)

    def test_native_video_protocol_round_trip_and_validation(self) -> None:
        message = lan_remote.NativeVideoMessage(
            message_type=lan_remote.NATIVE_VIDEO_MESSAGE_ACCESS_UNIT,
            flags=lan_remote.NATIVE_VIDEO_FLAG_KEYFRAME | lan_remote.NATIVE_VIDEO_FLAG_CODEC_CONFIG,
            generation=3,
            sequence=19,
            timestamp_us=123456789,
            width=1920,
            height=1080,
            fps_limit=120,
            payload=b"\x00\x00\x00\x01\x65encoded",
        )
        packed = lan_remote.pack_native_video_message(message)
        self.assertEqual(len(packed), lan_remote.NATIVE_VIDEO_HEADER.size + len(message.payload))
        self.assertEqual(lan_remote.unpack_native_video_message(packed), message)

        with self.assertRaisesRegex(ValueError, "unsupported"):
            lan_remote.unpack_native_video_message(b"BAD!" + packed[4:])
        with self.assertRaisesRegex(ValueError, "does not match"):
            lan_remote.unpack_native_video_message(packed[:-1])
        with self.assertRaisesRegex(ValueError, "FPS ceiling"):
            lan_remote.pack_native_video_message(
                lan_remote.NativeVideoMessage(
                    message_type=lan_remote.NATIVE_VIDEO_MESSAGE_STREAM_CONFIG,
                    flags=0,
                    generation=1,
                    sequence=0,
                    timestamp_us=0,
                    width=1920,
                    height=1080,
                    fps_limit=144,
                    payload=b"{}",
                )
            )

    def test_native_video_protocol_rejects_oversized_payload_before_allocation(self) -> None:
        header = lan_remote.NATIVE_VIDEO_HEADER.pack(
            lan_remote.NATIVE_VIDEO_PROTOCOL_MAGIC,
            lan_remote.NATIVE_VIDEO_PROTOCOL_VERSION,
            lan_remote.NATIVE_VIDEO_MESSAGE_STREAM_CONFIG,
            0,
            1,
            0,
            0,
            lan_remote.NATIVE_VIDEO_MAX_CONFIG_BYTES + 1,
            1920,
            1080,
            60,
            0,
        )
        with self.assertRaisesRegex(ValueError, "too large"):
            lan_remote.unpack_native_video_message(header)

    def test_remote_session_status_is_exclusive_and_expires(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            state = make_state(lan_remote.SettingsStore())
            accepted, status = state.touch_remote_session(
                "session-0000000000000001",
                "controller-one",
                "Office controller",
                False,
                "192.168.1.20",
            )
            self.assertTrue(accepted)
            self.assertTrue(status["active"])
            self.assertEqual(status["controller_name"], "Office controller")
            self.assertEqual(status["mode"], "control")

            accepted, occupied = state.touch_remote_session(
                "session-0000000000000002",
                "controller-two",
                "Second controller",
                True,
                "192.168.1.21",
            )
            self.assertFalse(accepted)
            self.assertEqual(occupied["controller_id"], "controller-one")
            self.assertFalse(state.end_remote_session("session-0000000000000002"))
            self.assertTrue(state.end_remote_session("session-0000000000000001"))
            self.assertEqual(state.remote_session_status(), {"active": False})

            state.touch_remote_session(
                "session-0000000000000003",
                "controller-three",
                "Third controller",
                True,
                "192.168.1.22",
            )
            with state.active_remote_session_lock:
                state.active_remote_session["last_seen"] = time.time() - lan_remote.ACTIVE_REMOTE_SESSION_TTL_SECONDS - 1
            self.assertEqual(state.remote_session_status(), {"active": False})

    def test_remote_window_payload_carries_controller_identity(self) -> None:
        normalized = lan_remote.normalize_remote_window_payload(
            {
                "device": {"id": "remote-device", "ip": "192.168.1.30", "port": 8765, "name": "Remote"},
                "token": "session-token",
                "viewOnly": False,
                "authMethod": "permanent",
                "controllerSessionId": "session-0000000000000001",
                "controllerId": "local-device",
                "controllerName": "Local controller",
            }
        )
        self.assertEqual(normalized["controllerSessionId"], "session-0000000000000001")
        self.assertEqual(normalized["controllerId"], "local-device")
        self.assertEqual(normalized["controllerName"], "Local controller")

    def test_outgoing_control_session_can_be_listed_cancelled_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            state = make_state(lan_remote.SettingsStore())
            payload = {
                "device": {"id": "remote-device", "name": "Remote", "ip": "192.168.1.30", "port": 8765},
                "token": "session-token",
                "viewOnly": False,
                "controllerSessionId": "session-0000000000000001",
            }
            state.register_outgoing_session(payload, 1234)
            self.assertEqual(
                state.public_outgoing_sessions()[0],
                {
                    "session_id": "session-0000000000000001",
                    "device_id": "remote-device",
                    "device_name": "Remote",
                    "mode": "control",
                    "started_at": unittest.mock.ANY,
                },
            )
            self.assertTrue(state.touch_outgoing_session("session-0000000000000001"))
            cancelled = state.cancel_outgoing_session("session-0000000000000001")
            self.assertIsNotNone(cancelled)
            self.assertFalse(state.touch_outgoing_session("session-0000000000000001"))
            self.assertEqual(state.public_outgoing_sessions(), [])

    def test_remote_window_payload_rejects_local_device(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能远程控制本机"):
            lan_remote.normalize_remote_window_payload(
                {
                    "device": {"id": "local-device", "ip": "127.0.0.1", "port": 8765, "name": "Local"},
                    "token": "session-token",
                    "viewOnly": False,
                    "authMethod": "temporary",
                    "controllerSessionId": "session-0000000000000001",
                    "controllerId": "local-device",
                    "controllerName": "Local controller",
                }
            )

    def test_frontend_has_graphical_busy_state_and_authenticated_wallpaper_preview(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn(".app.settings-mode .control-occupation { display: none; }", html)
        for marker in (
            'id="controlOccupation"',
            'id="controlledDuration"',
            'id="detailPreviewDesktop"',
            'id="detailPreviewImage"',
            'id="detailPreviewStatus"',
            'id="detailPreviewState">在线</span>',
            'id="deviceFileButton"',
            '<span>远程文件</span>',
            "被控端原始桌面背景图片",
            "/desktop-background",
            'headers["X-Remote-Token"] = token',
            "clearLegacyDevicePreviews",
            "/api/session/heartbeat",
            "本机正在被远程控制",
            'id="stopOutgoingControl"',
            "/api/outgoing-session/cancel",
        ):
            self.assertIn(marker, html)
        preview_markup = html.split('<div class="preview" id="detailPreview">', 1)[1].split(
            '<div class="control-grid">', 1
        )[0]
        self.assertNotIn("preview-desktop-icons", preview_markup)
        self.assertNotIn("preview-taskbar", preview_markup)
        self.assertNotIn("preview-shade", preview_markup)
        self.assertNotIn("preview-footer", preview_markup)
        control_markup = html.split('<div class="control-grid">', 1)[1].split(
            '<div class="info-card">', 1
        )[0]
        self.assertEqual(control_markup.count('class="control-card'), 4)
        self.assertNotIn("wide", control_markup)
        self.assertIn('openPair("files")', html)
        for forbidden in (
            "saveDevicePreview",
            "canvas.toDataURL",
            "上次控制画面",
            "detailPreviewCaption",
            "detailPreviewTime",
        ):
            self.assertNotIn(forbidden, html)

    def test_desktop_background_reads_the_wallpaper_file_not_the_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallpaper.png"
            output = io.BytesIO()
            lan_remote.Image.new("RGB", (8, 5), (12, 34, 56)).save(output, format="PNG")
            expected = output.getvalue()
            path.write_bytes(expected)
            with patch.object(lan_remote, "current_desktop_wallpaper_path", return_value=path):
                data, content_type = lan_remote.desktop_background_image()
        self.assertEqual(data, expected)
        self.assertEqual(content_type, "image/png")

    def test_frontend_blocks_remote_control_of_local_device(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("device.is_self || device.busy || state.localStatus?.active", html)
        self.assertIn("不能控制或观看本机", html)
        self.assertIn("本机不可远程连接", html)

    def test_clipboard_defaults_on_and_auto_unlock_only_runs_at_session_start(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("enableDefaultClipboardSync();", html)
        self.assertIn('$("clipboardSend").checked = true;', html)
        self.assertIn('$("clipboardReceive").checked = true;', html)
        self.assertIn("initialAutoUnlockPending: !viewOnly", html)
        self.assertIn("session.initialAutoUnlockPending = false;", html)
        self.assertNotIn('id="unlockRemoteButton"', html)
        self.assertIn('state.remoteSessionLocked ? "#i-lock" : "#i-unlock"', html)
        self.assertIn("if (state.remoteSessionLocked) requestRemoteUnlock(true);", html)
        self.assertIn("startRemoteLockMonitoring(state.session);", html)
        self.assertIn("if (locked) await requestRemoteUnlock(false);", html)
        self.assertIn('requestRemoteUnlock(true)', html)
        self.assertNotIn("autoUnlockLastCheck", html)

    def test_remote_window_chrome_is_separate_from_control_toolbar(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        host = (root / "packaging" / "ControlWindowHost.cs").read_text(encoding="utf-8")
        titlebar = html.split('<header class="remote-titlebar"', 1)[1].split("</header>", 1)[0]
        toolbar = html.split('<div class="remote-toolbar">', 1)[1].split("</div>", 1)[0]
        for control_id in ("remoteWindowMinimize", "remoteWindowMaximize", "closeSession"):
            self.assertIn(f'id="{control_id}"', titlebar)
            self.assertNotIn(f'id="{control_id}"', toolbar)
        self.assertIn('id="keyboardButton"', toolbar)
        self.assertIn('id="fullscreenButton"', toolbar)
        self.assertIn("event.clientY <= 6", html)
        self.assertIn("event.clientY > 40", html)
        self.assertIn("remote-titlebar-visible", html)
        self.assertIn("LANRemoteVideoSetExclusions", host)
        native = (root / "native" / "WindowsLANRemoteVideo.cpp").read_text(encoding="utf-8")
        self.assertIn("ApplyExclusionRegion", native)

    def test_remote_window_is_foregrounded_without_blocking_main_webview(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        host = (root / "packaging" / "ControlWindowHost.cs").read_text(encoding="utf-8")
        self.assertIn("activate_remote_window(remoteWindowResult.process_id)", html)
        self.assertIn('case "activate_remote_window":', host)
        self.assertIn("await Task.Delay(50);", host)
        self.assertIn('remoteWindow ? "ControlHostWebView2-Remote" : "ControlHostWebView2"', host)
        self.assertIn("PromoteInitialRemoteWindow();", host)
        self.assertIn("DwmWindowCornerPreference = 33", host)
        self.assertIn("DwmCornerRound = 2", host)
        self.assertIn("ApplyWindowCornerPreference();", host)

    def test_native_glass_toolbar_and_fill_mode_do_not_cut_video_holes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        host = (root / "packaging" / "ControlWindowHost.cs").read_text(encoding="utf-8")
        native = (root / "native" / "WindowsLANRemoteVideo.cpp").read_text(encoding="utf-8")
        encoder = (root / "native" / "WindowsLANRemoteVideoEncoder.cpp").read_text(encoding="utf-8")
        self.assertIn("class NativeGlassToolbar", host)
        self.assertIn('case "set_native_overlay_state":', host)
        self.assertIn("LANRemoteVideoSetExclusions(nativeVideoHandle, new int[0], 0);", host)
        self.assertIn("LANRemoteVideoSetScaleMode", host)
        self.assertIn('#include "VideoQualityPolicy.hpp"', encoder)
        self.assertIn("QualityProfileForFps(fps)", encoder)
        self.assertIn("eAVEncCommonRateControlMode_LowDelayVBR", encoder)
        self.assertIn("AdaptiveBitrateForFps(", encoder)
        self.assertIn(
            "fps_ >= 120 ? eAVEncH264VProfile_Main : eAVEncH264VProfile_High",
            encoder,
        )
        self.assertIn("CODECAPI_AVScenarioInfo", encoder)
        self.assertIn("eAVScenarioInfo_DisplayRemoting", encoder)
        self.assertIn("EncoderQualityVsSpeedForFps(fps_)", encoder)
        self.assertIn("CODECAPI_AVEncCommonQualityVsSpeed", encoder)
        self.assertIn("CODECAPI_AVEncH264CABACEnable", encoder)
        self.assertIn("D3D11_VIDEO_USAGE_OPTIMAL_QUALITY", native)
        self.assertIn("D3D11_VIDEO_PROCESSOR_FILTER_EDGE_ENHANCEMENT", native)
        self.assertIn("description.InputFrameRate.Numerator = requested_fps_;", native)
        self.assertIn("description.OutputFrameRate.Numerator = requested_fps_;", native)
        self.assertIn("FillMode", host)
        self.assertIn("window.__lanNativeOverlayAction", html)
        self.assertIn("set_native_overlay_state", html)
        self.assertIn("html.remote-window-root .remote-session.show", html)
        self.assertNotIn("html.remote-window-root .remote-stage { grid-row: 2; }", html)
        self.assertIn('id="remoteToolbarGrip" title="拖动工具栏"', html)
        self.assertIn("toolbarDockedAtTop", html)
        self.assertIn('action === "toolbar_docked"', html)
        self.assertIn('$("fpsLimitMenu").classList.contains("show") ? visibleElementRect($("fpsLimitMenu")) : null', html)
        self.assertIn("PositionForOwner", host)
        self.assertIn('fps.Caption = ReadInteger(values, "fps", 60).ToString() + " FPS";', host)
        self.assertIn("TextRenderingHint.AntiAliasGridFit", host)
        self.assertNotIn("TextRenderer.DrawText(", host)
        self.assertIn("private ContextMenuStrip activeMenu;", host)
        self.assertIn("PositionActiveMenu();", host)
        self.assertIn("return collapsed ? ownerBounds.Top : ownerBounds.Top + 8;", host)
        self.assertIn("int minimumY = ownerBounds.Top;", host)
        self.assertIn("state.toolbarDockedAtTop = rect.top <= 14;", html)
        self.assertIn('remoteToolbar.style.top = "";', html)
        self.assertNotIn("const titlebarBottom = 40;", html)
        self.assertIn("Opacity = 0.40;", host)
        self.assertNotIn('id="scaleModeButton"', html)
        self.assertIn('scale_mode: sessionScaleMode()', html)
        self.assertIn("#remoteScreen.scale-fill { object-fit: fill; }", html)
        self.assertNotIn("{left: 0, top: 0, width: window.innerWidth, height: 7}", html)
        self.assertIn("VideoProcessorSetStreamSourceRect", native)
        self.assertIn("if (!fill_mode_)", native)
        self.assertIn('"fill" : "fit"', native)
        self.assertIn("function positionOpenToolbarPopovers()", html)
        self.assertIn("positionOpenToolbarPopovers();", html)
        self.assertIn("function trackOpenToolbarPopovers(duration = 260)", html)
        self.assertIn("trackOpenToolbarPopovers();", html)
        self.assertIn("background: linear-gradient(180deg, rgba(122, 126, 133, .18), rgba(38, 41, 47, .10));", html)
        self.assertIn("background: linear-gradient(180deg, rgba(122, 126, 133, .38), rgba(38, 41, 47, .28));", html)
        self.assertIn("backdrop-filter: blur(4px) saturate(116%) brightness(1.02)", html)
        stop_control = html.split(".preview-stop-control {", 1)[1].split("}", 1)[0]
        self.assertIn("inset: 0;", stop_control)
        self.assertIn("width: 100%;", stop_control)
        self.assertIn("height: 100%;", stop_control)
        self.assertIn("color: #f7f8fa;", stop_control)
        self.assertIn('font-family: "PingFang SC", "Noto Sans SC", "Segoe UI Variable Text", "Microsoft YaHei UI", sans-serif;', stop_control)
        self.assertIn("font-size: 20px;", stop_control)
        self.assertIn("font-weight: 400;", stop_control)
        self.assertNotIn("translate(-50%, -50%)", stop_control)
        self.assertNotIn("busy-tag", html)
        self.assertIn('device.busy_mode === "view" ? "观看中" : "控制中"', html)
        self.assertNotIn('device.busy_mode === "view" ? "观看中" : "会话中"', html)
        self.assertIn("background: linear-gradient(180deg, rgba(50, 53, 60, .62), rgba(24, 27, 32, .52));", html)
        stop_markup = html.split('id="stopOutgoingControl"', 1)[1].split("</button>", 1)[0]
        self.assertNotIn("<svg", stop_markup)

    def test_access_code_has_expected_entropy_friendly_format(self) -> None:
        values = {lan_remote.generate_access_code() for _ in range(64)}
        self.assertEqual(len(values), 64)
        for value in values:
            self.assertRegex(value, re.compile(r"^[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){2}$"))

    def test_version_comparison_is_numeric(self) -> None:
        self.assertGreater(lan_remote.version_key("v0.10.0"), lan_remote.version_key("0.9.9"))
        self.assertEqual(lan_remote.version_key("v0.6.4"), (0, 6, 4))
        self.assertEqual(lan_remote.version_key(" 1.2 "), (1, 2, 0))

    def test_lan_and_local_origin_boundaries(self) -> None:
        self.assertTrue(lan_remote.is_allowed_client("192.168.1.20", False))
        self.assertTrue(lan_remote.is_allowed_client("127.0.0.1", False))
        self.assertFalse(lan_remote.is_allowed_client("8.8.8.8", False))
        self.assertTrue(lan_remote.is_trusted_local_origin("http://127.0.0.1:8765", 8765))
        self.assertTrue(lan_remote.is_trusted_local_origin("http://localhost:8765", 8765))
        self.assertTrue(lan_remote.is_trusted_local_origin("http://[::1]:8765", 8765))
        self.assertFalse(lan_remote.is_trusted_local_origin("https://127.0.0.1:8765", 8765))
        self.assertFalse(lan_remote.is_trusted_local_origin("http://127.0.0.1:9999", 8765))
        self.assertFalse(lan_remote.is_trusted_local_origin("http://127.0.0.1.evil.test:8765", 8765))

    def test_github_download_url_allowlist(self) -> None:
        accepted = [
            "https://github.com/EmpK1019/lan-windows-remote/releases/download/v1/setup.exe",
            "https://objects.githubusercontent.com/github-production-release-asset/setup.exe",
        ]
        rejected = [
            "http://github.com/owner/repo/setup.exe",
            "https://github.com.evil.test/setup.exe",
            "https://example.com/setup.exe",
            "file:///C:/setup.exe",
        ]
        for value in accepted:
            self.assertTrue(lan_remote.trusted_github_download_url(value), value)
        for value in rejected:
            self.assertFalse(lan_remote.trusted_github_download_url(value), value)

    def test_file_name_and_path_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolved = lan_remote.local_file_path(directory)
            self.assertEqual(resolved, Path(directory).resolve())
            child = lan_remote.local_file_path(str(Path(directory) / "new.txt"), must_exist=False)
            self.assertEqual(child.parent, Path(directory).resolve())
        for value in ("", ".", "..", "a/b", "a\\b", "bad?.txt", "bad:name"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    lan_remote.validate_file_name(value)
        with self.assertRaises(ValueError):
            lan_remote.local_file_path(r"\\server\share\file.txt", must_exist=False)
        with patch.object(lan_remote, "local_drive_type", return_value=4):
            with self.assertRaises(ValueError):
                lan_remote.local_file_path(r"Z:\mapped-share\file.txt", must_exist=False)

    def test_remote_input_payload_validation(self) -> None:
        valid = [
            {"type": "mouse_move", "x": 12, "y": 18, "monitor": "all"},
            {"type": "mouse_down", "x": 12, "y": 18, "button": 0},
            {"type": "mouse_down", "x": 12, "y": 18, "button": 3},
            {"type": "mouse_up", "x": 12, "y": 18, "button": 4},
            {"type": "mouse_wheel", "x": 12, "y": 18, "delta": -120},
            {"type": "mouse_hwheel", "x": 12, "y": 18, "delta": 120},
            {"type": "key_down", "key": "a", "code": "KeyA"},
            {"type": "key_up", "key": "Shift", "code": "ShiftLeft"},
            {"type": "native_key_down", "scan_code": 30, "extended": False},
            {"type": "native_key_up", "scan_code": 29, "extended": True},
            {"type": "text", "text": "Hello 中文"},
            {"type": "text_sequence", "text": "Lock 密码"},
        ]
        for payload in valid:
            lan_remote.validate_remote_input_payload(payload)

        invalid = [
            {"type": "mouse_unknown", "x": 0, "y": 0},
            {"type": "mouse_down", "x": 0, "y": 0, "button": 9},
            {"type": "mouse_move", "x": "left", "y": 0},
            {"type": "key_down", "key": "", "code": ""},
            {"type": "native_key_down", "scan_code": 0, "extended": False},
            {"type": "native_key_up", "scan_code": 256, "extended": False},
            {"type": "native_key_down", "scan_code": 30, "extended": "yes"},
            {"type": "text", "text": ""},
            {"type": "text", "text": "x" * 257},
            {"type": "text_sequence", "text": "x" * 129},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                lan_remote.validate_remote_input_payload(payload)

    def test_physical_key_codes_use_the_remote_keyboard_layout(self) -> None:
        self.assertEqual(lan_remote.key_to_vk("a", "KeyQ"), ord("Q"))
        self.assertEqual(lan_remote.key_to_vk("!", "Digit1"), ord("1"))
        self.assertEqual(lan_remote.key_to_vk("Control", "ControlRight"), 0xA3)
        self.assertEqual(lan_remote.key_to_vk("AltGraph", "AltRight"), 0xA5)
        self.assertIn("ControlRight", lan_remote.EXTENDED_KEY_CODES)
        self.assertIn("NumpadEnter", lan_remote.EXTENDED_KEY_CODES)

    def test_elevated_input_helper_is_preferred_and_temporarily_cached_when_unavailable(self) -> None:
        previous_retry = lan_remote.ELEVATED_INPUT_HELPER_RETRY_AFTER
        payload = {"type": "key_down", "key": "Escape", "code": "Escape"}
        try:
            lan_remote.ELEVATED_INPUT_HELPER_RETRY_AFTER = 0.0
            with patch.object(lan_remote, "elevated_input_helper_request") as request:
                self.assertTrue(lan_remote.try_send_elevated_input(payload))
            request.assert_called_once_with(payload, timeout=1.0)

            lan_remote.ELEVATED_INPUT_HELPER_RETRY_AFTER = time.monotonic() + 60
            with patch.object(lan_remote, "elevated_input_helper_request") as request:
                self.assertFalse(lan_remote.try_send_elevated_input(payload))
            request.assert_not_called()
        finally:
            lan_remote.ELEVATED_INPUT_HELPER_RETRY_AFTER = previous_retry

    def test_mouse_and_keyboard_buttons_use_checked_send_input(self) -> None:
        user32 = Mock()
        user32.SetCursorPos.return_value = True
        user32.MapVirtualKeyW.return_value = 1
        user32.SendInput.return_value = 1
        with (
            patch.object(lan_remote.ctypes.windll, "user32", user32),
            patch.object(lan_remote, "screen_rect", return_value=(0, 0, 1920, 1080)),
        ):
            lan_remote.send_mouse_event(
                {"type": "mouse_down", "x": 100, "y": 200, "button": 0, "monitor": "all"}
            )
            lan_remote.send_keyboard_event({"type": "key_down", "key": "Escape", "code": "Escape"})
        self.assertEqual(user32.SendInput.call_count, 2)
        user32.mouse_event.assert_not_called()
        user32.keybd_event.assert_not_called()

    def test_extended_mouse_buttons_and_horizontal_wheel_use_native_reports(self) -> None:
        user32 = Mock()
        user32.SetCursorPos.return_value = True
        user32.SendInput.return_value = 1
        with (
            patch.object(lan_remote.ctypes.windll, "user32", user32),
            patch.object(lan_remote, "screen_rect", return_value=(0, 0, 1920, 1080)),
        ):
            lan_remote.send_mouse_event(
                {"type": "mouse_down", "x": 100, "y": 200, "button": 3, "monitor": "all"}
            )
            lan_remote.send_mouse_event(
                {"type": "mouse_up", "x": 100, "y": 200, "button": 4, "monitor": "all"}
            )
            lan_remote.send_mouse_event(
                {"type": "mouse_hwheel", "x": 100, "y": 200, "delta": -240, "monitor": "all"}
            )
            lan_remote.send_mouse_event(
                {"type": "mouse_wheel", "x": 100, "y": 200, "delta": 1, "monitor": "all"}
            )

        reports = [entry.args[1]._obj.mi for entry in user32.SendInput.call_args_list]
        self.assertEqual(
            [(report.dwFlags, report.mouseData) for report in reports],
            [
                (0x0080, 0x0001),
                (0x0100, 0x0002),
                (lan_remote.MOUSEEVENTF_HWHEEL, (-120) & 0xFFFFFFFF),
                (lan_remote.MOUSEEVENTF_WHEEL, (-120) & 0xFFFFFFFF),
            ],
        )
        self.assertTrue(all(report.dwExtraInfo == 0 for report in reports))

    def test_cursor_is_composited_into_desktop_capture(self) -> None:
        user32 = Mock()
        gdi32 = Mock()

        def populate_cursor(pointer: Any) -> bool:
            cursor = pointer._obj
            cursor.flags = 1
            cursor.hCursor = 99
            cursor.ptScreenPos.x = 320
            cursor.ptScreenPos.y = 240
            return True

        def populate_icon(pointer_handle: Any, pointer: Any) -> bool:
            icon = pointer._obj
            icon.xHotspot = 4
            icon.yHotspot = 6
            icon.hbmMask = 101
            icon.hbmColor = 102
            return True

        user32.GetCursorInfo.side_effect = populate_cursor
        user32.GetIconInfo.side_effect = populate_icon
        with (
            patch.object(lan_remote.ctypes.windll, "user32", user32),
            patch.object(lan_remote.ctypes.windll, "gdi32", gdi32),
        ):
            lan_remote.draw_desktop_cursor(7, 100, 50)

        user32.DrawIconEx.assert_called_once_with(7, 216, 184, 99, 0, 0, 0, None, 3)
        self.assertEqual([call(101), call(102)], gdi32.DeleteObject.call_args_list)

    def test_desktop_cursor_payload_is_relative_to_selected_monitor(self) -> None:
        user32 = Mock()

        def populate_cursor(pointer: Any) -> bool:
            cursor = pointer._obj
            cursor.flags = 1
            cursor.hCursor = 99
            cursor.ptScreenPos.x = 320
            cursor.ptScreenPos.y = 240
            return True

        user32.GetCursorInfo.side_effect = populate_cursor
        with (
            patch.object(lan_remote.ctypes.windll, "user32", user32),
            patch.object(lan_remote, "screen_rect", return_value=(100, 50, 800, 600)),
        ):
            payload = lan_remote.desktop_cursor_payload("monitor-1")

        self.assertEqual(
            payload,
            {"visible": True, "x": 220, "y": 190, "width": 800, "height": 600},
        )

    def test_remote_pointer_source_distinguishes_controller_echo_from_controlled_mouse(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            state = make_state(lan_remote.SettingsStore())
            self.assertEqual(state.remote_pointer_source("all", 400, 300), "controlled")
            state.note_remote_pointer({"type": "mouse_move", "monitor": "all", "x": 400, "y": 300})
            self.assertEqual(state.remote_pointer_source("all", 401, 298), "controller")
            self.assertEqual(state.remote_pointer_source("all", 430, 300), "controlled")
            self.assertEqual(state.remote_pointer_source("monitor-1", 400, 300), "controlled")

    def test_remote_frontend_serializes_input_and_suppresses_local_shortcuts(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("state.inputQueue = state.inputQueue.catch", html)
        self.assertIn("state.pendingMouseMove = entry", html)
        self.assertIn("state.mouseMoveQueued", html)
        self.assertIn("INPUT_REQUEST_TIMEOUT_MS", html)
        self.assertIn("state.inputAbortController.abort()", html)
        self.assertIn("resetInputTransport()", html)
        self.assertIn("LONG_INPUT_REQUEST_TIMEOUT_MS", html)
        self.assertIn('id="lockRemoteButton"', html)
        self.assertIn('title="帧率上限"', html)
        self.assertIn("font-size: 15px;", html)
        self.assertIn("font-weight: 400;", html)
        self.assertIn('font-family: "PingFang SC", "Noto Sans SC", "Segoe UI Variable Text", sans-serif;', html)
        self.assertIn("waitForRemoteLockState(true, 1800)", html)
        self.assertIn("被控端未进入锁屏，请更新被控端后重试", html)
        self.assertIn('id="settingLockRemoteOnDisconnect"', html)
        self.assertIn('navigator.sendBeacon(endpoint("/lock")', html)
        self.assertIn('type: "text/plain;charset=UTF-8"', html)
        self.assertIn("event.stopImmediatePropagation()", html)
        self.assertIn('{capture: true}', html)
        self.assertNotIn('event.key === "Escape"', html)
        self.assertIn("window.__lanForwardNativeKey", html)
        self.assertIn("setNativeKeyboardCapture(false)", html)
        self.assertIn("shouldCaptureNativeKeyboard()", html)
        self.assertIn("configure_native_input", html)
        self.assertIn("release_native_input", html)
        self.assertIn("state.nativeInputActive", html)
        self.assertIn(
            '$("remoteStage").addEventListener("pointermove", (event) => {\n'
            '      if (state.session?.connected && !state.session.viewOnly) setRemoteCursorOwner("controller");\n'
            "      if (state.nativeInputActive) return;",
            html,
        )
        self.assertIn(
            '$("remoteStage").addEventListener("wheel", (event) => {\n'
            "      if (state.nativeInputActive) {",
            html,
        )
        self.assertIn("state.remoteFrameWidth = Number(monitor?.width) || image.naturalWidth", html)
        self.assertIn("CONTROL_STREAM_FPS_OPTIONS = new Set([30, 60, 120])", html)
        self.assertIn('id="settingControlFps"', html)
        self.assertIn('id="fpsLimitMenu"', html)
        self.assertIn('class="remote-fps-option"', html)
        self.assertIn("实际帧率会随网络与设备性能自动降低", html)
        self.assertIn("session.fpsLimit = fpsLimit", html)
        self.assertIn('endpoint(\n        `/screen-stream?monitor=', html)
        self.assertIn("REMOTE_CURSOR_STREAM_FPS = 120", html)
        self.assertIn("REMOTE_CURSOR_RECONNECT_MS = 80", html)
        self.assertIn("桌面控制与桌面观看统一使用 30 / 60 / 120 FPS 上限", html)
        self.assertNotIn('id="settingFrameDelay"', html)
        self.assertNotIn("refreshPollingScreen", html)
        self.assertNotIn('endpoint(`/screen?monitor=${encodeURIComponent(monitorId)}&cursor=1', html)
        self.assertIn('endpoint(`/cursor-stream?monitor=', html)
        self.assertIn('if (state.session?.viewOnly) {\n        setRemoteCursorOwner("remote");', html)
        self.assertIn('result.input_source === "controller"', html)
        self.assertIn(".remote-stage.controlling.remote-cursor-active #remoteScreen { cursor: none !important; }", html)
        self.assertIn('id="remotePointer"', html)
        self.assertIn(
            "const remoteWidth = state.remoteFrameWidth || image.naturalWidth || 0",
            html,
        )
        host = (Path(__file__).resolve().parents[1] / "packaging" / "ControlWindowHost.cs").read_text(encoding="utf-8")
        self.assertIn("AreBrowserAcceleratorKeysEnabled = false", host)
        self.assertIn("SetWindowsHookEx(WhKeyboardLl", host)
        self.assertIn("SetWindowsMouseHookEx(WhMouseLl", host)
        self.assertIn(
            "InitializeKeyboardHook();\n"
            "                        InitializeMouseHook();\n"
            "                        StartNativeInputWorker();",
            host,
        )
        self.assertIn(
            "return mouseCaptureReady && "
            "(!session.KeyboardEnabled || keyboardHook != IntPtr.Zero);",
            host,
        )
        self.assertIn("session.RemoteBounds.Width >= 1", host)
        self.assertIn("WmMouseHWheel", host)
        self.assertIn('MousePayload("mouse_hwheel"', host)
        self.assertIn("RemoteInputExtraInfo", host)
        self.assertIn("CONNECT /input-stream HTTP/1.1", host)
        self.assertIn('case "configure_native_input"', host)
        self.assertIn("GetForegroundWindow() == Handle", host)
        self.assertIn('case "set_keyboard_capture"', host)
        service = (Path(__file__).resolve().parents[1] / "packaging" / "SecureDesktopService.cs").read_text(
            encoding="utf-8"
        )
        self.assertIn("InteractiveHelperPort = 8768", service)
        self.assertIn(r'@"winsta0\Default"', service)
        self.assertIn("interactiveHelper = LaunchHelper(", service)

    def test_lock_password_text_is_typed_sequentially(self) -> None:
        with (
            patch.object(lan_remote, "send_unicode_text") as send_character,
            patch.object(lan_remote.time, "sleep") as pause,
        ):
            lan_remote.handle_remote_input({"type": "text_sequence", "text": "P@密"})
        self.assertEqual(send_character.call_args_list, [call("P"), call("@"), call("密")])
        self.assertEqual(
            pause.call_args_list,
            [call(lan_remote.UNLOCK_CHARACTER_DELAY_SECONDS), call(lan_remote.UNLOCK_CHARACTER_DELAY_SECONDS)],
        )


class SettingsAndAuthenticationTests(unittest.TestCase):
    def test_auto_unlock_types_directly_when_credential_ui_is_already_ready(self) -> None:
        api = lan_remote.DesktopApi()
        api.vault = Mock()
        api.vault.get_secret.return_value = "Lock 密码"
        device = {"id": "remote-device", "ip": "192.168.1.25", "port": 8765}
        remote = Mock(
            side_effect=[
                {"session_locked": True, "session_ui_state": "credential_ui"},
                {"ok": True},
                {"ok": True},
                {"session_locked": False, "session_ui_state": "unlocked"},
            ]
        )
        with (
            patch.object(api, "_validated_device", return_value=device),
            patch.object(api, "_remote_json", remote),
            patch.object(lan_remote.time, "sleep") as pause,
            patch.object(lan_remote, "UNLOCK_RESULT_POLLS", 1),
        ):
            result = api.try_auto_unlock("{}", "access-token")

        self.assertEqual(result, {"ok": True, "status": "unlocked"})
        self.assertEqual(
            remote.call_args_list,
            [
                call(device, "/api/session-status", "access-token"),
                call(
                    device,
                    "/input",
                    "access-token",
                    {"type": "text_sequence", "text": "Lock 密码"},
                    timeout=lan_remote.UNLOCK_SEQUENCE_TIMEOUT_SECONDS,
                ),
                call(device, "/input", "access-token", {"type": "key_press", "key": "Enter", "code": "Enter"}),
                call(device, "/api/session-status", "access-token"),
            ],
        )
        self.assertEqual(
            pause.call_args_list,
            [call(lan_remote.UNLOCK_SUBMIT_DELAY_SECONDS), call(lan_remote.UNLOCK_RESULT_POLL_SECONDS)],
        )

    def test_auto_unlock_retries_an_unresponsive_lock_screen_before_typing(self) -> None:
        api = lan_remote.DesktopApi()
        api.vault = Mock()
        api.vault.get_secret.return_value = "Lock password"
        device = {"id": "remote-device", "ip": "192.168.1.25", "port": 8765}
        remote = Mock(
            side_effect=[
                {"session_locked": True, "session_ui_state": "lock_screen"},
                {"ok": True},
                {"session_locked": True, "session_ui_state": "lock_screen"},
                {"session_locked": True, "session_ui_state": "lock_screen"},
                {"ok": True},
                {"session_locked": True, "session_ui_state": "credential_ui"},
                {"ok": True},
                {"ok": True},
                {"session_locked": False, "session_ui_state": "unlocked"},
            ]
        )
        with (
            patch.object(api, "_validated_device", return_value=device),
            patch.object(api, "_remote_json", remote),
            patch.object(lan_remote.time, "sleep"),
            patch.object(lan_remote, "UNLOCK_WAKE_POLLS_PER_ATTEMPT", 2),
            patch.object(lan_remote, "UNLOCK_RESULT_POLLS", 1),
        ):
            result = api.try_auto_unlock("{}", "access-token")

        self.assertEqual(result, {"ok": True, "status": "unlocked"})
        wake = call(device, "/input", "access-token", {"type": "key_press", "key": "Enter", "code": "Enter"})
        self.assertEqual(remote.call_args_list.count(wake), 3)
        # Two wake presses plus the final credential submission. Password is
        # sent only after the status endpoint reports credential_ui.
        password_call = call(
            device,
            "/input",
            "access-token",
            {"type": "text_sequence", "text": "Lock password"},
            timeout=lan_remote.UNLOCK_SEQUENCE_TIMEOUT_SECONDS,
        )
        self.assertLess(remote.call_args_list.index(password_call), len(remote.call_args_list) - 1)

    def test_auto_unlock_waits_out_a_transition_without_sending_a_wake_key(self) -> None:
        api = lan_remote.DesktopApi()
        api.vault = Mock()
        api.vault.get_secret.return_value = "Lock password"
        device = {"id": "remote-device", "ip": "192.168.1.25", "port": 8765}
        remote = Mock(
            side_effect=[
                {"session_locked": True, "session_ui_state": "locked_transition"},
                {"session_locked": True, "session_ui_state": "credential_ui"},
                {"ok": True},
                {"ok": True},
                {"session_locked": False, "session_ui_state": "unlocked"},
            ]
        )
        with (
            patch.object(api, "_validated_device", return_value=device),
            patch.object(api, "_remote_json", remote),
            patch.object(lan_remote.time, "sleep"),
            patch.object(lan_remote, "UNLOCK_WAKE_POLLS_PER_ATTEMPT", 1),
            patch.object(lan_remote, "UNLOCK_RESULT_POLLS", 1),
        ):
            result = api.try_auto_unlock("{}", "access-token")

        self.assertEqual(result, {"ok": True, "status": "unlocked"})
        key_calls = [
            entry for entry in remote.call_args_list
            if len(entry.args) >= 4 and entry.args[1] == "/input" and entry.args[3].get("type") == "key_press"
        ]
        self.assertEqual(len(key_calls), 1)

    def test_manual_unlock_can_retry_after_the_initial_attempt(self) -> None:
        api = lan_remote.DesktopApi()
        api.vault = Mock()
        api.vault.get_secret.return_value = "Lock password"
        api._unlock_attempts.add("remote-device")
        device = {"id": "remote-device", "ip": "192.168.1.25", "port": 8765}
        remote = Mock(
            side_effect=[
                {"session_locked": True, "session_ui_state": "credential_ui"},
                {"ok": True},
                {"ok": True},
                {"session_locked": True, "session_ui_state": "credential_ui"},
            ]
        )
        with (
            patch.object(api, "_validated_device", return_value=device),
            patch.object(api, "_remote_json", remote),
            patch.object(lan_remote.time, "sleep"),
            patch.object(lan_remote, "UNLOCK_RESULT_POLLS", 1),
        ):
            result = api.try_auto_unlock("{}", "access-token", force=True)

        self.assertEqual(result, {"ok": False, "status": "still_locked"})
        self.assertEqual(remote.call_count, 4)

    def test_auto_unlock_never_types_when_remote_ui_state_is_unavailable(self) -> None:
        api = lan_remote.DesktopApi()
        api.vault = Mock()
        api.vault.get_secret.return_value = "must-not-be-sent"
        device = {"id": "remote-device", "ip": "192.168.1.25", "port": 8765}
        remote = Mock(return_value={"session_locked": True})
        with (
            patch.object(api, "_validated_device", return_value=device),
            patch.object(api, "_remote_json", remote),
        ):
            result = api.try_auto_unlock("{}", "access-token")
        self.assertEqual(result, {"ok": False, "status": "state_unavailable"})
        self.assertEqual(remote.call_count, 1)

    def test_saved_settings_and_permanent_password_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            settings = lan_remote.SettingsStore()
            settings.values["device_name"] = "Persistent device"
            settings.values["view_only"] = True
            settings.values["auto_install_updates"] = False
            settings.values["close_to_tray"] = False
            settings.values["lock_remote_on_disconnect"] = True
            settings.set_permanent_password("persistent password value")
            settings.save()

            reloaded = lan_remote.SettingsStore()
            self.assertEqual(reloaded.values["device_name"], "Persistent device")
            self.assertIs(reloaded.values["view_only"], True)
            self.assertIs(reloaded.values["auto_install_updates"], False)
            self.assertIs(reloaded.values["close_to_tray"], False)
            self.assertIs(reloaded.values["lock_remote_on_disconnect"], True)
            self.assertTrue(reloaded.permanent_password_is_set())
            self.assertTrue(reloaded.verify_permanent_password("persistent password value"))

    def test_dpapi_credential_vault_round_trip_and_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            vault = lan_remote.CredentialVault()
            vault.set_secret("access", "remote-device", "permanent access secret", "Remote")
            vault.set_secret("lock", "remote-device", "Windows lock secret", "Remote")
            stored = vault.path.read_text(encoding="utf-8")
            self.assertNotIn("permanent access secret", stored)
            self.assertNotIn("Windows lock secret", stored)
            self.assertEqual(vault.get_secret("access", "remote-device"), "permanent access secret")
            self.assertEqual(vault.get_secret("lock", "remote-device"), "Windows lock secret")
            vault.remove_secret("access", "remote-device")
            self.assertFalse(vault.has_secret("access", "remote-device"))
            self.assertTrue(vault.has_secret("lock", "remote-device"))

    def test_corrupt_settings_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            settings_path = Path(directory) / "LAN Remote" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "device_name": ["not", "a", "string"],
                        "view_only": "yes",
                        "frame_delay_ms": 999,
                        "control_fps_limit": 144,
                        "auto_check_updates": 1,
                        "auto_install_updates": "yes",
                        "close_to_tray": "yes",
                        "permanent_password_salt": [],
                        "permanent_password_hash": "partial",
                    }
                ),
                encoding="utf-8",
            )
            settings = lan_remote.SettingsStore()
            self.assertEqual(settings.values["device_name"], "")
            self.assertIs(settings.values["view_only"], False)
            self.assertEqual(settings.values["frame_delay_ms"], 80)
            self.assertEqual(settings.values["control_fps_limit"], 60)
            self.assertIs(settings.values["auto_check_updates"], True)
            self.assertIs(settings.values["auto_install_updates"], True)
            self.assertIs(settings.values["close_to_tray"], True)
            self.assertFalse(settings.permanent_password_is_set())

    def test_device_id_survives_device_rename_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            first_settings = lan_remote.SettingsStore()
            first_id = lan_remote.persistent_device_id(first_settings, "Original name")
            self.assertRegex(first_id, re.compile(r"^[0-9a-f]{12}$"))

            second_settings = lan_remote.SettingsStore()
            second_id = lan_remote.persistent_device_id(second_settings, "Renamed device")
            self.assertEqual(second_id, first_id)

    def test_temporary_and_permanent_session_lifetimes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            settings = lan_remote.SettingsStore()
            settings.set_permanent_password("correct horse battery staple")
            state = make_state(settings)

            temporary = state.authenticate("TEST-TEMP-CODE")
            self.assertEqual(temporary["auth_method"], "temporary")
            temporary_session = state.create_session_token(temporary)
            self.assertEqual(state.authenticate(temporary_session)["auth_method"], "temporary")

            permanent = state.authenticate("correct horse battery staple")
            self.assertEqual(permanent["auth_method"], "permanent")
            permanent_session = state.create_session_token(permanent)

            state.rotate_temporary_access_code()
            self.assertIsNone(state.authenticate(temporary_session))
            self.assertEqual(state.authenticate(permanent_session)["auth_method"], "permanent")

            settings.set_permanent_password("a different permanent password")
            self.assertIsNone(state.authenticate(permanent_session))

    def test_remote_window_handoff_is_one_time_and_expires(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            state = make_state(lan_remote.SettingsStore())
            handoff = state.create_remote_window_session({"device": {"id": "one"}})
            self.assertEqual(state.consume_remote_window_session(handoff), {"device": {"id": "one"}})
            self.assertIsNone(state.consume_remote_window_session(handoff))

            expired = state.create_remote_window_session({"expired": True})
            payload, _ = state.remote_window_sessions[expired]
            state.remote_window_sessions[expired] = (payload, time.time() - 1)
            self.assertIsNone(state.consume_remote_window_session(expired))

    def test_authentication_and_handoff_caches_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            state = make_state(lan_remote.SettingsStore())
            for _ in range(lan_remote.MAX_SESSION_TOKENS + 50):
                state.create_session_token({"auth_method": "permanent"})
            self.assertEqual(len(state.session_tokens), lan_remote.MAX_SESSION_TOKENS)

            for index in range(lan_remote.MAX_REMOTE_WINDOW_HANDOFFS + 20):
                state.create_remote_window_session({"index": index})
            self.assertEqual(len(state.remote_window_sessions), lan_remote.MAX_REMOTE_WINDOW_HANDOFFS)

    def test_wrong_permanent_password_attempts_are_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            settings = lan_remote.SettingsStore()
            settings.values["permanent_password_salt"] = "marker-salt"
            settings.values["permanent_password_hash"] = "marker-hash"
            settings.verify_permanent_password = Mock(return_value=False)
            state = make_state(settings)
            for _ in range(lan_remote.AUTH_FAILURE_LIMIT + 4):
                self.assertIsNone(state.authenticate("wrong-password", "192.168.1.25"))
            self.assertEqual(settings.verify_permanent_password.call_count, lan_remote.AUTH_FAILURE_LIMIT)
            self.assertEqual(state.authenticate("TEST-TEMP-CODE", "192.168.1.25")["auth_method"], "temporary")


class UpdateTests(unittest.TestCase):
    def test_latest_release_selects_installer_and_digest(self) -> None:
        payload = {
            "tag_name": "v9.8.7",
            "html_url": "https://github.com/EmpK1019/lan-windows-remote/releases/tag/v9.8.7",
            "body": "notes",
            "assets": [
                {
                    "name": "WindowsLANRemoteSetup-9.8.7.exe",
                    "browser_download_url": "https://github.com/EmpK1019/lan-windows-remote/releases/download/v9.8.7/setup.exe",
                    "digest": "sha256:abcd",
                    "size": 123456,
                }
            ],
        }
        response = FakeResponse(json.dumps(payload).encode("utf-8"), lan_remote.GITHUB_LATEST_RELEASE_API)
        with patch.object(lan_remote, "urlopen", return_value=response):
            result = lan_remote.latest_release()
        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "9.8.7")
        self.assertEqual(result["installer_digest"], "sha256:abcd")
        self.assertEqual(result["installer_size"], 123456)

    def test_update_download_checks_hash_size_and_launches(self) -> None:
        content = (b"LAN-REMOTE-SETUP" * 5000)[:70000]
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        url = "https://github.com/EmpK1019/lan-windows-remote/releases/download/v9.8.7/setup.exe"
        with tempfile.TemporaryDirectory() as directory:
            response = FakeResponse(content, url)
            process = Mock()
            with (
                patch.object(lan_remote, "urlopen", return_value=response),
                patch.object(lan_remote.tempfile, "gettempdir", return_value=directory),
                patch.object(lan_remote.subprocess, "Popen", return_value=process) as popen,
            ):
                destination = lan_remote.download_and_launch_update(
                    {
                        "installer_url": url,
                        "latest_version": "9.8.7",
                        "installer_digest": digest,
                        "installer_size": len(content),
                    }
                )
            self.assertEqual(destination.read_bytes(), content)
            popen.assert_called_once_with([str(destination), "--from-update"], close_fds=True)

    def test_update_download_retries_network_failure_and_reuses_verified_cache(self) -> None:
        content = (b"LAN-REMOTE-RETRY" * 5000)[:70000]
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        url = "https://github.com/EmpK1019/lan-windows-remote/releases/download/v9.8.7/setup.exe"
        release = {
            "installer_url": url,
            "latest_version": "9.8.7",
            "installer_digest": digest,
            "installer_size": len(content),
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(lan_remote, "urlopen", side_effect=[OSError("temporary"), FakeResponse(content, url)]) as open_url,
                patch.object(lan_remote.tempfile, "gettempdir", return_value=directory),
                patch.object(lan_remote.time, "sleep"),
                patch.object(lan_remote.subprocess, "Popen") as popen,
            ):
                destination = lan_remote.download_and_launch_update(release)
            self.assertEqual(open_url.call_count, 2)
            self.assertEqual(destination.read_bytes(), content)
            popen.assert_called_once()

            with (
                patch.object(lan_remote, "urlopen") as cached_open,
                patch.object(lan_remote.tempfile, "gettempdir", return_value=directory),
                patch.object(lan_remote.subprocess, "Popen") as cached_popen,
            ):
                cached_destination = lan_remote.download_and_launch_update(release)
            self.assertEqual(cached_destination, destination)
            cached_open.assert_not_called()
            cached_popen.assert_called_once()

    def test_update_download_removes_hash_mismatch(self) -> None:
        content = b"X" * 70000
        url = "https://github.com/EmpK1019/lan-windows-remote/releases/download/v9.8.7/setup.exe"
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(lan_remote, "urlopen", return_value=FakeResponse(content, url)),
                patch.object(lan_remote.tempfile, "gettempdir", return_value=directory),
                patch.object(lan_remote.subprocess, "Popen") as popen,
            ):
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    lan_remote.download_and_launch_update(
                        {
                            "installer_url": url,
                            "latest_version": "9.8.7",
                            "installer_digest": "sha256:" + ("0" * 64),
                            "installer_size": len(content),
                        }
                    )
            self.assertFalse((Path(directory) / "WindowsLANRemoteSetup-9.8.7.exe").exists())
            popen.assert_not_called()

    def test_update_download_requires_published_sha256(self) -> None:
        url = "https://github.com/EmpK1019/lan-windows-remote/releases/download/v9.8.7/setup.exe"
        with patch.object(lan_remote, "urlopen") as urlopen_mock:
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                lan_remote.download_and_launch_update(
                    {
                        "installer_url": url,
                        "latest_version": "9.8.7",
                        "installer_digest": "",
                        "installer_size": 70000,
                    }
                )
        urlopen_mock.assert_not_called()

    def test_update_download_rejects_unsafe_release_version(self) -> None:
        url = "https://github.com/EmpK1019/lan-windows-remote/releases/download/v9/setup.exe"
        with patch.object(lan_remote, "urlopen") as urlopen_mock:
            with self.assertRaisesRegex(RuntimeError, "版本号"):
                lan_remote.download_and_launch_update(
                    {
                        "installer_url": url,
                        "latest_version": "../outside",
                        "installer_digest": "sha256:" + ("0" * 64),
                        "installer_size": 70000,
                    }
                )
        urlopen_mock.assert_not_called()


class HttpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"APPDATA": self.temp_directory.name})
        self.environment.start()
        self.settings = lan_remote.SettingsStore()
        self.state = make_state(self.settings)
        self.server = lan_remote.RemoteServer(("127.0.0.1", 0), lan_remote.RemoteHandler, self.state)
        self.state.port = int(self.server.server_address[1])
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)
        self.environment.stop()
        self.temp_directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.state.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        result = response.status, {key.lower(): value for key, value in response.getheaders()}, data
        connection.close()
        return result

    def test_local_api_rejects_cross_site_origin(self) -> None:
        status, headers, _ = self.request("GET", "/api/devices")
        self.assertEqual(status, 200)
        self.assertNotIn("access-control-allow-origin", headers)

        same_origin = f"http://127.0.0.1:{self.state.port}"
        status, _, _ = self.request("GET", "/api/devices", headers={"Origin": same_origin})
        self.assertEqual(status, 200)

        status, _, _ = self.request(
            "GET",
            "/api/devices",
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)

        status, headers, _ = self.request(
            "OPTIONS",
            "/api/settings",
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", headers)

    def test_native_video_connect_rejects_unauthenticated_and_invalid_negotiation(self) -> None:
        status, _, _ = self.request(
            "CONNECT",
            "/video-stream?monitor=all&fps=60",
            headers={"X-Remote-Token": "wrong", "X-LAN-Video-Protocol": "1"},
        )
        self.assertEqual(status, 401)

        status, _, data = self.request(
            "CONNECT",
            "/video-stream?monitor=all&fps=60",
            headers={"X-Remote-Token": "TEST-TEMP-CODE", "X-LAN-Video-Protocol": "2"},
        )
        self.assertEqual(status, 426)
        self.assertIn(b"unsupported video protocol", data)

        status, _, _ = self.request(
            "CONNECT",
            "/video-stream?monitor=all&fps=144",
            headers={"X-Remote-Token": "TEST-TEMP-CODE", "X-LAN-Video-Protocol": "1"},
        )
        self.assertEqual(status, 400)

        with (
            patch.object(lan_remote, "screen_rect", return_value=(0, 0, 100, 80)),
            patch.object(lan_remote, "native_video_requires_compatibility", return_value=True),
        ):
            status, _, data = self.request(
                "CONNECT",
                "/video-stream?monitor=all&fps=60",
                headers={"X-Remote-Token": "TEST-TEMP-CODE", "X-LAN-Video-Protocol": "1"},
            )
        self.assertEqual(status, 423)
        self.assertIn(b"MJPEG", data)

    def test_session_status_reports_wts_lock_even_when_input_desktop_is_default(self) -> None:
        with (
            patch.object(lan_remote, "current_session_locked", return_value=True),
            patch.object(lan_remote, "input_desktop_name", return_value="Default"),
            patch.object(lan_remote, "foreground_process_name", return_value="LockApp.exe"),
        ):
            status, _, data = self.request(
                "GET",
                "/api/session-status",
                headers={"X-Remote-Token": "TEST-TEMP-CODE"},
            )
        self.assertEqual(status, 200, data)
        payload = json.loads(data)
        self.assertTrue(payload["session_locked"])
        self.assertFalse(payload["secure_desktop_active"])
        self.assertEqual(payload["session_ui_state"], "lock_screen")
        self.assertFalse(payload["credential_ui_ready"])

    def test_authentication_session_input_and_monitor_endpoints(self) -> None:
        status, _, _ = self.request("GET", "/monitors")
        self.assertEqual(status, 401)

        status, _, data = self.request(
            "POST",
            "/api/verify",
            headers={"X-Remote-Token": "TEST-TEMP-CODE", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        session_token = json.loads(data)["session_token"]

        monitors = [{"id": "all", "label": "All", "width": 100, "height": 80}]
        with patch.object(lan_remote, "monitor_payload", return_value=monitors):
            status, headers, data = self.request("GET", "/monitors", headers={"X-Remote-Token": session_token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["monitors"], monitors)
        self.assertEqual(headers.get("access-control-allow-origin"), "*")

        cursor = {"visible": True, "x": 44, "y": 55, "width": 100, "height": 80}
        with (
            patch.object(lan_remote, "secure_desktop_active", return_value=False),
            patch.object(lan_remote, "desktop_cursor_payload", return_value=cursor),
        ):
            status, _, data = self.request(
                "GET",
                "/cursor?monitor=all",
                headers={"X-Remote-Token": session_token},
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True, **cursor, "input_source": "controlled"})

        received: list[dict[str, object]] = []
        with (
            patch.object(lan_remote, "secure_desktop_active", return_value=False),
            patch.object(lan_remote, "try_send_elevated_input", return_value=False),
            patch.object(lan_remote, "handle_remote_input", side_effect=received.append),
        ):
            payload = json.dumps({"type": "key_down", "key": "a", "code": "KeyA"}).encode("utf-8")
            status, _, _ = self.request(
                "POST",
                "/input",
                body=payload,
                headers={"X-Remote-Token": session_token, "Content-Type": "application/json"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(received[0]["code"], "KeyA")

        with (
            patch.object(lan_remote, "secure_desktop_active", return_value=False),
            patch.object(lan_remote, "try_send_elevated_input", return_value=True) as elevated_input,
            patch.object(lan_remote, "handle_remote_input") as local_input,
        ):
            status, _, data = self.request(
                "POST",
                "/input",
                body=json.dumps({"type": "key_up", "key": "a", "code": "KeyA"}).encode("utf-8"),
                headers={"X-Remote-Token": session_token, "Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, data)
        elevated_input.assert_called_once()
        local_input.assert_not_called()

    def test_remote_session_heartbeat_is_visible_and_exclusive(self) -> None:
        status, _, data = self.request(
            "POST",
            "/api/verify",
            headers={"X-Remote-Token": "TEST-TEMP-CODE", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        session_token = json.loads(data)["session_token"]
        heartbeat = {
            "token": session_token,
            "session_id": "session-0000000000000001",
            "controller_id": "controller-one",
            "controller_name": "Controller one",
            "view_only": False,
        }
        status, _, data = self.request(
            "POST",
            "/api/session/heartbeat",
            body=json.dumps(heartbeat).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(json.loads(data)["session_status"]["active"])

        status, _, data = self.request("GET", "/api/devices")
        self.assertEqual(status, 200)
        devices_payload = json.loads(data)
        self.assertTrue(devices_payload["local_status"]["active"])
        self.assertTrue(devices_payload["devices"][0]["busy"])
        self.assertTrue(devices_payload["devices"][0]["online"])

        occupied = {
            **heartbeat,
            "session_id": "session-0000000000000002",
            "controller_id": "controller-two",
        }
        status, _, _ = self.request(
            "POST",
            "/api/session/heartbeat",
            body=json.dumps(occupied).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 409)

        status, _, data = self.request(
            "POST",
            "/api/session/end",
            body=json.dumps({"token": session_token, "session_id": heartbeat["session_id"]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(json.loads(data)["ended"])

        with (
            patch.object(lan_remote, "secure_desktop_active", return_value=False),
            patch.object(lan_remote, "lock_remote_workstation") as lock_workstation,
        ):
            status, _, data = self.request(
                "POST",
                "/lock",
                body=b"{}",
                headers={"X-Remote-Token": session_token, "Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["status"], "locked")
        lock_workstation.assert_called_once_with()

    def test_main_window_can_cancel_its_outgoing_control_session(self) -> None:
        session_id = "session-0000000000000001"
        self.state.register_outgoing_session(
            {
                "device": {"id": "remote-device", "name": "Remote", "ip": "192.168.1.25", "port": 8765},
                "token": "remote-session-token",
                "viewOnly": False,
                "controllerSessionId": session_id,
            },
            1234,
        )
        status, _, data = self.request("GET", "/api/devices")
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["outgoing_sessions"][0]["session_id"], session_id)

        with patch.object(lan_remote, "end_outgoing_remote_session", return_value=True) as end_remote:
            status, _, data = self.request(
                "POST",
                "/api/outgoing-session/cancel",
                body=json.dumps({"session_id": session_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, data)
        self.assertTrue(json.loads(data)["remote_ended"])
        end_remote.assert_called_once()
        self.assertFalse(self.state.touch_outgoing_session(session_id))

    def test_native_input_stream_is_authenticated_and_ordered(self) -> None:
        received: list[dict[str, object]] = []
        connection = socket.create_connection(("127.0.0.1", self.state.port), timeout=3)
        connection.settimeout(3)
        try:
            connection.sendall(
                b"CONNECT /input-stream HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"X-Remote-Token: TEST-TEMP-CODE\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            response = b""
            while b"\r\n\r\n" not in response:
                response += connection.recv(1024)
            self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])

            payloads = [
                {"type": "native_key_down", "scan_code": 59, "extended": False},
                {"type": "native_key_up", "scan_code": 59, "extended": False},
                {"type": "mouse_down", "x": 25, "y": 35, "button": 0, "monitor": "all"},
                {"type": "mouse_up", "x": 25, "y": 35, "button": 0, "monitor": "all"},
            ]
            with (
                patch.object(lan_remote, "secure_desktop_active", return_value=False),
                patch.object(lan_remote, "try_send_elevated_input", return_value=False),
                patch.object(lan_remote, "handle_remote_input", side_effect=received.append),
            ):
                for payload in payloads:
                    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                    connection.sendall(len(raw).to_bytes(4, "big") + raw)
                    self.assertEqual(connection.recv(1), b"\x00")
            self.assertEqual(received, payloads)
        finally:
            connection.close()

        rejected = socket.create_connection(("127.0.0.1", self.state.port), timeout=3)
        rejected.settimeout(3)
        try:
            rejected.sendall(
                b"CONNECT /input-stream HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"X-Remote-Token: wrong-token\r\n\r\n"
            )
            response = rejected.recv(4096)
            self.assertIn(b" 401 ", response.split(b"\r\n", 1)[0])
        finally:
            rejected.close()

    def test_invalid_input_monitor_and_view_only_are_rejected(self) -> None:
        headers = {"X-Remote-Token": "TEST-TEMP-CODE", "Content-Type": "application/json"}
        status, _, _ = self.request(
            "POST",
            "/input",
            body=json.dumps({"type": "mouse_unknown", "x": 0, "y": 0}).encode("utf-8"),
            headers=headers,
        )
        self.assertEqual(status, 400)

        capture = Mock(return_value=(b"jpeg", "image/jpeg"))
        with (
            patch.object(lan_remote, "secure_desktop_active", return_value=False),
            patch.object(lan_remote, "capture_screen_image", capture),
        ):
            status, _, data = self.request("GET", "/screen?monitor=all&cursor=0", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(data, b"jpeg")
        capture.assert_called_once_with("all", False)

        with (
            patch.object(lan_remote, "secure_desktop_active", return_value=False),
            patch.object(lan_remote, "capture_screen_image", side_effect=ValueError("invalid monitor")),
        ):
            status, _, _ = self.request("GET", "/screen?monitor=missing", headers=headers)
        self.assertEqual(status, 400)

        self.state.view_only = True
        status, _, _ = self.request(
            "POST",
            "/input",
            body=json.dumps({"type": "key_down", "key": "a", "code": "KeyA"}).encode("utf-8"),
            headers=headers,
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/lock", body=b"{}", headers=headers)
        self.assertEqual(status, 403)

    def test_low_latency_screen_stream_is_multipart_and_authenticated(self) -> None:
        capture = Mock(return_value=(b"jpeg-frame", "image/jpeg", 1920, 1080))
        connection = socket.create_connection(("127.0.0.1", self.state.port), timeout=3)
        connection.settimeout(3)
        try:
            with (
                patch.object(lan_remote, "secure_desktop_active", return_value=False),
                patch.object(lan_remote, "capture_low_latency_screen", capture),
            ):
                connection.sendall(
                    b"GET /screen-stream?monitor=all&cursor=0&fps=120&token=TEST-TEMP-CODE HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
                )
                response = bytearray()
                while b"jpeg-frame" not in response:
                    response.extend(connection.recv(4096))
        finally:
            connection.close()
        self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b"multipart/x-mixed-replace; boundary=lan-remote-frame", response)
        self.assertIn(b"X-Remote-FPS: 120", response)
        self.assertIn(b"X-Remote-FPS-Limit: 120", response)
        self.assertIn(b"Content-Length: 10", response)
        capture.assert_called_with("all", 120)

        status, _, _ = self.request(
            "GET",
            "/screen-stream?monitor=all&fps=144",
            headers={"X-Remote-Token": "TEST-TEMP-CODE"},
        )
        self.assertEqual(status, 400)

    def test_cursor_stream_is_persistent_authenticated_and_source_aware(self) -> None:
        cursor = {"visible": True, "x": 44, "y": 55, "width": 100, "height": 80}
        connection = socket.create_connection(("127.0.0.1", self.state.port), timeout=3)
        connection.settimeout(3)
        try:
            with (
                patch.object(lan_remote, "secure_desktop_active", return_value=False),
                patch.object(lan_remote, "desktop_cursor_payload", return_value=cursor),
            ):
                connection.sendall(
                    b"GET /cursor-stream?monitor=all&token=TEST-TEMP-CODE HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
                )
                response = bytearray()
                while response.count(b'"input_source":"controlled"') < 2:
                    response.extend(connection.recv(4096))
        finally:
            connection.close()
        self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b"application/x-ndjson", response)
        self.assertIn(b"X-Remote-Cursor-FPS: 120", response)
        self.assertGreaterEqual(response.count(b'"ok":true'), 2)

    def test_desktop_background_is_authenticated_and_contains_only_wallpaper_bytes(self) -> None:
        status, _, _ = self.request("GET", "/desktop-background")
        self.assertEqual(status, 401)

        with patch.object(lan_remote, "desktop_background_image", return_value=(b"wallpaper", "image/png")):
            status, headers, data = self.request(
                "GET",
                "/desktop-background",
                headers={"X-Remote-Token": "TEST-TEMP-CODE"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertEqual(data, b"wallpaper")

    def test_concurrent_upload_to_same_destination_is_rejected(self) -> None:
        root = Path(self.temp_directory.name) / "files"
        root.mkdir()
        destination = (root / "busy.bin").resolve()
        query_root = lan_remote.quote(str(root), safe="")
        with lan_remote.FILE_UPLOAD_LOCK:
            lan_remote.ACTIVE_FILE_UPLOADS.add(destination)
        try:
            status, _, data = self.request(
                "POST",
                f"/files/upload?path={query_root}&name=busy.bin",
                body=b"blocked",
                headers={"X-Remote-Token": "TEST-TEMP-CODE", "Content-Type": "application/octet-stream"},
            )
        finally:
            with lan_remote.FILE_UPLOAD_LOCK:
                lan_remote.ACTIVE_FILE_UPLOADS.discard(destination)
        self.assertEqual(status, 409, data)
        self.assertFalse(destination.exists())

    def test_partial_request_body_times_out_without_hanging_server(self) -> None:
        with patch.object(lan_remote, "CLIENT_SOCKET_TIMEOUT_SECONDS", 0.2):
            connection = socket.create_connection(("127.0.0.1", self.state.port), timeout=2)
            try:
                connection.sendall(
                    b"POST /input HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"X-Remote-Token: TEST-TEMP-CODE\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 100\r\n\r\n{"
                )
                connection.settimeout(3)
                response = b""
                while b"request body timed out" not in response:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            finally:
                connection.close()
        self.assertIn(b"408 Request Timeout", response)
        self.assertIn(b"request body timed out", response)

        status, _, data = self.request("GET", "/health")
        self.assertEqual(status, 200, data)

    def test_file_upload_list_and_download_round_trip(self) -> None:
        root = Path(self.temp_directory.name) / "files"
        root.mkdir()
        token_headers = {"X-Remote-Token": "TEST-TEMP-CODE"}
        upload = b"remote-file-content\x00\xff"
        query_root = lan_remote.quote(str(root), safe="")
        status, _, data = self.request(
            "POST",
            f"/files/upload?path={query_root}&name=sample.bin",
            body=upload,
            headers={**token_headers, "Content-Type": "application/octet-stream"},
        )
        self.assertEqual(status, 200, data)

        status, _, data = self.request("GET", f"/files?path={query_root}", headers=token_headers)
        self.assertEqual(status, 200)
        listing = json.loads(data)
        self.assertIn("sample.bin", [entry["name"] for entry in listing["entries"]])

        file_path = lan_remote.quote(str(root / "sample.bin"), safe="")
        status, headers, data = self.request("GET", f"/files/download?path={file_path}", headers=token_headers)
        self.assertEqual(status, 200)
        self.assertEqual(data, upload)
        self.assertEqual(int(headers["content-length"]), len(upload))

    def test_settings_endpoint_is_same_origin_and_validated(self) -> None:
        same_origin = f"http://127.0.0.1:{self.state.port}"
        payload = json.dumps(
            {
                "device_name": "Renamed",
                "frame_delay_ms": 80,
                "control_fps_limit": 120,
                "auto_install_updates": False,
                "close_to_tray": False,
                "lock_remote_on_disconnect": True,
            }
        ).encode("utf-8")
        with (
            patch.object(lan_remote, "startup_enabled", return_value=False),
            patch.object(lan_remote, "set_startup_enabled") as set_startup,
        ):
            status, _, data = self.request(
                "POST",
                "/api/settings",
                body=payload,
                headers={"Origin": same_origin, "Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, data)
        self.assertEqual(self.state.device_name, "Renamed")
        self.assertIs(self.settings.values["auto_install_updates"], False)
        self.assertIs(self.settings.values["close_to_tray"], False)
        self.assertIs(self.settings.values["lock_remote_on_disconnect"], True)
        self.assertEqual(self.settings.values["control_fps_limit"], 120)
        set_startup.assert_called_once_with(False)

        invalid_fps = json.dumps({"device_name": "Rejected", "control_fps_limit": 144}).encode("utf-8")
        status, _, _ = self.request(
            "POST",
            "/api/settings",
            body=invalid_fps,
            headers={"Origin": same_origin, "Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.settings.values["control_fps_limit"], 120)

        bad_payload = json.dumps({"device_name": "Rejected"}).encode("utf-8")
        status, _, _ = self.request(
            "POST",
            "/api/settings",
            body=bad_payload,
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.state.device_name, "Renamed")

    def test_native_credential_bridge_is_local_encrypted_and_complete(self) -> None:
        same_origin = f"http://127.0.0.1:{self.state.port}"
        headers = {"Origin": same_origin, "Content-Type": "application/json"}

        def credential(action: str, **values: object) -> object:
            body = json.dumps({"action": action, "device_id": "bridge-device", **values}).encode("utf-8")
            status, _, data = self.request("POST", "/api/native/credentials", body=body, headers=headers)
            self.assertEqual(status, 200, data)
            return json.loads(data)["result"]

        self.assertTrue(credential("save_access", password="bridge access secret", device_name="Bridge"))
        self.assertEqual(credential("load_access"), "bridge access secret")
        self.assertTrue(credential("save_lock", password="bridge lock secret", device_name="Bridge"))
        self.assertEqual(credential("status"), {"access_saved": True, "lock_saved": True})
        credential_file = Path(self.temp_directory.name) / "LAN Remote" / "credentials.json"
        stored = credential_file.read_text(encoding="utf-8")
        self.assertNotIn("bridge access secret", stored)
        self.assertNotIn("bridge lock secret", stored)
        self.assertTrue(credential("clear_access"))
        self.assertTrue(credential("clear_lock"))

        body = json.dumps({"action": "status", "device_id": "bridge-device"}).encode("utf-8")
        status, _, _ = self.request(
            "POST",
            "/api/native/credentials",
            body=body,
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)

    def test_update_install_is_single_flight(self) -> None:
        same_origin = f"http://127.0.0.1:{self.state.port}"
        headers = {"Origin": same_origin, "Content-Type": "application/json"}
        release = {
            "update_available": True,
            "installer_url": "https://github.com/owner/repo/setup.exe",
            "latest_version": "9.8.7",
        }
        with (
            patch.object(lan_remote, "latest_release", return_value=release),
            patch.object(lan_remote, "download_and_launch_update", return_value=Path("setup.exe")) as launch,
        ):
            first, _, _ = self.request("POST", "/api/update/install", headers=headers)
            second, _, _ = self.request("POST", "/api/update/install", headers=headers)
            self.state.update_install_started_at -= lan_remote.UPDATE_INSTALL_RETRY_SECONDS + 1
            retry, _, _ = self.request("POST", "/api/update/install", headers=headers)
        self.assertEqual(first, 200)
        self.assertEqual(second, 409)
        self.assertEqual(retry, 200)
        self.assertEqual(launch.call_count, 2)

    def test_failed_update_can_be_retried(self) -> None:
        same_origin = f"http://127.0.0.1:{self.state.port}"
        headers = {"Origin": same_origin, "Content-Type": "application/json"}
        release = {"update_available": True, "latest_version": "9.8.7"}
        with (
            patch.object(lan_remote, "latest_release", return_value=release),
            patch.object(lan_remote, "download_and_launch_update", side_effect=RuntimeError("download failed")) as launch,
        ):
            first, _, _ = self.request("POST", "/api/update/install", headers=headers)
            second, _, _ = self.request("POST", "/api/update/install", headers=headers)
        self.assertEqual(first, 502)
        self.assertEqual(second, 502)
        self.assertEqual(launch.call_count, 2)
        self.assertFalse(self.state.update_install_started)

    def test_failed_settings_save_rolls_back_runtime_and_startup(self) -> None:
        same_origin = f"http://127.0.0.1:{self.state.port}"
        payload = json.dumps(
            {"device_name": "Should roll back", "frame_delay_ms": 80, "launch_at_login": True}
        ).encode("utf-8")
        original_values = dict(self.settings.values)
        original_name = self.state.device_name
        with (
            patch.object(lan_remote, "startup_enabled", return_value=False),
            patch.object(lan_remote, "set_startup_enabled") as set_startup,
            patch.object(self.settings, "save", side_effect=OSError("disk full")),
        ):
            status, _, _ = self.request(
                "POST",
                "/api/settings",
                body=payload,
                headers={"Origin": same_origin, "Content-Type": "application/json"},
            )
        self.assertEqual(status, 500)
        self.assertEqual(self.state.device_name, original_name)
        self.assertEqual(self.settings.values, original_values)
        self.assertEqual(set_startup.call_args_list[0].args, (True,))
        self.assertEqual(set_startup.call_args_list[1].args, (False,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
