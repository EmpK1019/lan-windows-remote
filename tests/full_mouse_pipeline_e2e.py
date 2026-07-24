from __future__ import annotations

import ctypes
import io
import json
import os
import statistics
import sys
import threading
import time
import uuid
from ctypes import wintypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from PIL import Image


TOKEN = "abcdefghijklmnop"
CONTROLLER_PORT = int(os.environ.get("LAN_REMOTE_CONTROLLER_PORT", "0"))
MJPEG_WEBVIEW_DISPLAY_FPS_FLOOR = 24
WM_CLOSE = 0x0010
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
PROCESS_SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
WAIT_TIMEOUT = 0x00000102
SW_RESTORE = 9
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002


class AuditState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.input_event = threading.Event()
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self.screen_requests = 0
        self.screen_request_times: list[float] = []
        self.screen_request_queries: list[str] = []
        self.cursor_request_times: list[float] = []

    def record(self, source: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.frames.append((source, payload))
            types = {str(item.get("type", "")) for _, item in self.frames}
            if {"mouse_down", "mouse_up", "mouse_wheel"}.issubset(types):
                self.input_event.set()


def jpeg_frame(red: int = 17) -> bytes:
    image = Image.new("RGB", (1280, 720), (red, 27, 42))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=82, optimize=False)
    return output.getvalue()


FRAME = jpeg_frame()
FRAME_SEQUENCE = tuple(jpeg_frame(32 + index * 12) for index in range(8))


class AuditHandler(BaseHTTPRequestHandler):
    server: "AuditServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def common_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Remote-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, CONNECT, OPTIONS")

    def json_response(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.common_headers("application/json; charset=utf-8", len(data))
        self.end_headers()
        self.wfile.write(data)

    def read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if not isinstance(payload, dict):
            raise ValueError("object required")
        return payload

    def read_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.rfile.read(length - len(chunks))
            if not chunk:
                raise ConnectionError("input stream ended early")
            chunks.extend(chunk)
        return bytes(chunks)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.common_headers("text/plain", 0)
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/monitors":
            self.json_response(
                {
                    "ok": True,
                    "monitors": [
                        {
                            "id": "all",
                            "label": "Audit display",
                            "width": 1280,
                            "height": 720,
                            "left": 0,
                            "top": 0,
                            "primary": True,
                        }
                    ],
                }
            )
            return
        if path == "/screen-stream":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=audit-frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    with self.server.state.lock:
                        frame_index = self.server.state.screen_requests
                        self.server.state.screen_requests += 1
                        self.server.state.screen_request_times.append(time.monotonic())
                        self.server.state.screen_request_queries.append(self.path)
                    frame = FRAME_SEQUENCE[frame_index % len(FRAME_SEQUENCE)]
                    self.wfile.write(
                        b"--audit-frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame)).encode("ascii")
                        + b"\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
                    self.wfile.flush()
                    time.sleep(1 / 60)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                self.close_connection = True
                return
        if path == "/screen":
            with self.server.state.lock:
                self.server.state.screen_requests += 1
                self.server.state.screen_request_times.append(time.monotonic())
                self.server.state.screen_request_queries.append(self.path)
            self.send_response(HTTPStatus.OK)
            self.common_headers("image/jpeg", len(FRAME))
            self.end_headers()
            self.wfile.write(FRAME)
            return
        if path == "/cursor-stream":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Remote-Cursor-FPS", "120")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                while True:
                    with self.server.state.lock:
                        self.server.state.cursor_request_times.append(time.monotonic())
                    payload = json.dumps(
                        {
                            "ok": True,
                            "visible": True,
                            "x": 640,
                            "y": 360,
                            "width": 1280,
                            "height": 720,
                            "input_source": "controller",
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.wfile.write(payload + b"\n")
                    self.wfile.flush()
                    time.sleep(1 / 120)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                self.close_connection = True
                return
        if path == "/cursor":
            self.json_response(
                {
                    "ok": True,
                    "visible": True,
                    "x": 640,
                    "y": 360,
                    "width": 1280,
                    "height": 720,
                    "input_source": "controller",
                }
            )
            return
        if path == "/clipboard":
            self.json_response(
                {"ok": True, "sequence": 1, "has_text": False, "text": ""}
            )
            return
        if path == "/api/session-status":
            self.json_response(
                {
                    "ok": True,
                    "secure_desktop_active": False,
                    "session_locked": False,
                }
            )
            return
        self.json_response({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        payload = self.read_payload()
        if path == "/input":
            self.server.state.record("http", payload)
            self.json_response({"ok": True})
            return
        if path == "/api/session/heartbeat":
            self.json_response({"ok": True})
            return
        if path == "/api/session/end":
            self.json_response({"ok": True})
            return
        if path == "/clipboard":
            self.json_response(
                {"ok": True, "sequence": 2, "has_text": bool(payload.get("text")), "text": payload.get("text", "")}
            )
            return
        self.json_response({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_CONNECT(self) -> None:
        if self.path != "/input-stream":
            self.json_response({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Connection", "keep-alive")
        self.send_header("X-LAN-Input-Protocol", "1")
        self.end_headers()
        self.wfile.flush()
        while True:
            try:
                header = self.read_exact(4)
            except ConnectionError:
                return
            length = int.from_bytes(header, "big")
            try:
                frame = self.read_exact(length)
            except ConnectionError:
                return
            try:
                payload = json.loads(frame.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("object required")
                self.server.state.record("stream", payload)
                self.wfile.write(b"\x00")
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                self.wfile.write(b"\x02")
            self.wfile.flush()


class AuditServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: AuditState) -> None:
        super().__init__(("127.0.0.1", 0), AuditHandler)
        self.state = state


def local_json(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    method = "GET"
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        method = "POST"
        headers["Content-Type"] = "application/json"
    request = Request(
        f"http://127.0.0.1:{CONTROLLER_PORT}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urlopen(request, timeout=5) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("invalid local response")
    return result


def start_embedded_controller() -> ThreadingHTTPServer:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import lan_remote

    lan_remote.configure_win32_signatures()
    settings = lan_remote.SettingsStore()
    registry = lan_remote.DiscoveryRegistry()
    state = lan_remote.ServerState(
        token="AUDT-CODE-0001",
        token_expires_at=time.time() + 3600,
        view_only=False,
        allow_non_lan=False,
        started_at=time.time(),
        device_id=f"audit{uuid.uuid4().hex[:7]}",
        device_name="Mouse pipeline controller",
        port=0,
        registry=registry,
        settings=settings,
    )
    controller = lan_remote.RemoteServer(
        ("127.0.0.1", 0),
        lan_remote.RemoteHandler,
        state,
    )
    state.port = int(controller.server_port)
    threading.Thread(target=controller.serve_forever, daemon=True).start()
    return controller


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.mouse_event.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_size_t,
]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetActiveWindow.argtypes = [wintypes.HWND]
user32.SetActiveWindow.restype = wintypes.HWND
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindowAsync.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.SwitchToThisWindow.restype = None
user32.keybd_event.argtypes = [
    wintypes.BYTE,
    wintypes.BYTE,
    wintypes.DWORD,
    ctypes.c_size_t,
]
user32.keybd_event.restype = None
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD


def window_for_process(process_id: int, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(window: int, _: int) -> bool:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value == process_id and user32.IsWindowVisible(window):
                found.append(int(window))
                return False
            return True

        user32.EnumWindows(callback, 0)
        if found:
            return found[0]
        time.sleep(0.1)
    raise TimeoutError("remote control window did not appear")


def window_diagnostics(window: int) -> str:
    foreground = int(user32.GetForegroundWindow())
    owner = wintypes.DWORD()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, ctypes.byref(owner))
    length = user32.GetWindowTextLengthW(foreground)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(foreground, title, len(title))
    target_owner = wintypes.DWORD()
    target_thread = user32.GetWindowThreadProcessId(window, ctypes.byref(target_owner))
    return (
        f"target={window} target_pid={target_owner.value} target_thread={target_thread} "
        f"foreground={foreground} foreground_pid={owner.value} "
        f"foreground_thread={foreground_thread} foreground_title={title.value!r}"
    )


def screen_pixel(point: wintypes.POINT) -> tuple[int, int, int]:
    desktop_dc = user32.GetDC(None)
    if not desktop_dc:
        raise ctypes.WinError()
    try:
        color = int(gdi32.GetPixel(desktop_dc, point.x, point.y))
    finally:
        user32.ReleaseDC(None, desktop_dc)
    if color == 0xFFFFFFFF:
        raise ctypes.WinError()
    return color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF


def force_foreground(window: int, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(window, None)
    while time.monotonic() < deadline:
        foreground = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        attached_threads: list[int] = []
        for thread_id in {int(foreground_thread), int(target_thread)}:
            if (
                thread_id
                and thread_id != current_thread
                and user32.AttachThreadInput(current_thread, thread_id, True)
            ):
                attached_threads.append(thread_id)
        try:
            user32.ShowWindowAsync(window, SW_RESTORE)
            user32.SetWindowPos(
                window,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            )
            user32.BringWindowToTop(window)
            user32.SetActiveWindow(window)
            user32.SetFocus(window)
            user32.keybd_event(VK_MENU, 0, 0, 0)
            try:
                user32.SetForegroundWindow(window)
                user32.SwitchToThisWindow(window, True)
            finally:
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        finally:
            for thread_id in reversed(attached_threads):
                user32.AttachThreadInput(current_thread, thread_id, False)
        if int(user32.GetForegroundWindow()) == window:
            return True
        time.sleep(0.03)
    return False


def close_process(window: int, process_id: int) -> None:
    user32.PostMessageW(window, WM_CLOSE, 0, 0)
    process = kernel32.OpenProcess(PROCESS_SYNCHRONIZE | PROCESS_TERMINATE, False, process_id)
    if not process:
        return
    try:
        if kernel32.WaitForSingleObject(process, 5000) == WAIT_TIMEOUT:
            kernel32.TerminateProcess(process, 1)
    finally:
        kernel32.CloseHandle(process)


def main() -> int:
    global CONTROLLER_PORT
    state = AuditState()
    server = AuditServer(state)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    controller: ThreadingHTTPServer | None = None
    if CONTROLLER_PORT == 0:
        controller = start_embedded_controller()
        CONTROLLER_PORT = int(controller.server_port)
    process_id = 0
    window = 0
    original_cursor = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(original_cursor))
    try:
        devices = local_json("/api/devices").get("devices", [])
        local_device = next(
            (device for device in devices if isinstance(device, dict) and device.get("is_self")),
            {},
        )
        controller_id = str(local_device.get("id", "audit-controller"))
        controller_name = str(local_device.get("name", "LAN Remote audit"))
        fake_id = f"mouse-audit-{uuid.uuid4().hex[:12]}"
        result = local_json(
            "/api/remote-window/open",
            {
                "device": {
                    "id": fake_id,
                    "ip": "127.0.0.1",
                    "port": server.server_port,
                    "name": "Mouse Pipeline Audit",
                    "os": "Windows",
                    "view_only": False,
                    "is_self": False,
                },
                "token": TOKEN,
                "viewOnly": False,
                "authMethod": "permanent",
                "credentialExpiresAt": None,
                "controllerSessionId": uuid.uuid4().hex,
                "controllerId": controller_id,
                "controllerName": controller_name,
            },
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "could not launch remote window"))
        process_id = int(result["process_id"])
        window = window_for_process(process_id)

        deadline = time.monotonic() + 10
        while state.screen_requests < 1 and time.monotonic() < deadline:
            time.sleep(0.1)
        if state.screen_requests < 1:
            raise TimeoutError("actual remote page never loaded a screen frame")

        if not force_foreground(window, timeout=5.0):
            raise RuntimeError(
                "actual remote control window could not become foreground: "
                + window_diagnostics(window)
            )

        rect = wintypes.RECT()
        if not user32.GetClientRect(window, ctypes.byref(rect)):
            raise ctypes.WinError()
        point = wintypes.POINT(
            max(1, (rect.right - rect.left) // 2),
            max(1, int((rect.bottom - rect.top) * 0.68)),
        )
        if not user32.ClientToScreen(window, ctypes.byref(point)):
            raise ctypes.WinError()
        sample_point = wintypes.POINT(
            max(1, (rect.right - rect.left) // 4),
            max(1, int((rect.bottom - rect.top) * 0.68)),
        )
        if not user32.ClientToScreen(window, ctypes.byref(sample_point)):
            raise ctypes.WinError()
        display_started = time.perf_counter()
        display_colors: list[tuple[int, int, int]] = []
        while time.perf_counter() - display_started < 1.25:
            color = screen_pixel(sample_point)
            if not display_colors or color != display_colors[-1]:
                display_colors.append(color)
            time.sleep(0.004)
        display_duration = time.perf_counter() - display_started
        displayed_fps = max(0, len(display_colors) - 1) / display_duration
        if displayed_fps < MJPEG_WEBVIEW_DISPLAY_FPS_FLOOR:
            raise RuntimeError(
                "MJPEG fallback displayed only "
                f"{displayed_fps:.1f} changing frames/s: {display_colors}"
            )
        for _ in range(4):
            if not user32.SetCursorPos(point.x, point.y):
                raise ctypes.WinError()
            time.sleep(0.12)
            if force_foreground(window):
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, 120, 0)
            if state.input_event.wait(1.5):
                break
        if not state.input_event.is_set():
            with state.lock:
                observed = list(state.frames)
            raise TimeoutError(f"actual page/native bridge did not forward click and wheel: {observed}")
        cadence_deadline = time.monotonic() + 2
        while time.monotonic() < cadence_deadline:
            with state.lock:
                cadence_ready = (
                    len(state.screen_request_times) >= 5
                    and len(state.cursor_request_times) >= 5
                )
            if cadence_ready:
                break
            time.sleep(0.04)
        with state.lock:
            observed = list(state.frames)
            screen_times = list(state.screen_request_times)
            screen_queries = list(state.screen_request_queries)
            cursor_times = list(state.cursor_request_times)
        if len(screen_times) < 5:
            raise TimeoutError(f"low-latency screen loop produced only {len(screen_times)} frames")
        if len(cursor_times) < 5:
            raise TimeoutError(f"remote cursor channel produced only {len(cursor_times)} updates")
        if not all("cursor=0" in query for query in screen_queries):
            raise RuntimeError(f"control frames still include a baked cursor: {screen_queries}")
        if not all("fps=60" in query for query in screen_queries):
            raise RuntimeError(f"control stream did not use the configured FPS ceiling: {screen_queries}")
        screen_intervals_ms = [
            (later - earlier) * 1000
            for earlier, later in zip(screen_times[-5:-1], screen_times[-4:])
        ]
        cursor_intervals_ms = [
            (later - earlier) * 1000
            for earlier, later in zip(cursor_times[-5:-1], cursor_times[-4:])
        ]
        if statistics.median(screen_intervals_ms) > 25:
            raise RuntimeError(f"screen cadence regressed: {screen_intervals_ms}")
        if statistics.median(cursor_intervals_ms) > 30:
            raise RuntimeError(f"cursor cadence regressed: {cursor_intervals_ms}")
        sources = sorted({source for source, _ in observed})
        non_move = [
            (source, payload)
            for source, payload in observed
            if payload.get("type") != "mouse_move"
        ]
        print(
            "FULL_MOUSE_PIPELINE_E2E_OK "
            f"pid={process_id} point={point.x},{point.y} "
            f"screen_ms={statistics.median(screen_intervals_ms):.1f} "
            f"cursor_ms={statistics.median(cursor_intervals_ms):.1f} "
            f"display_fps={displayed_fps:.1f} "
            f"sources={sources} frames={non_move}"
        )
        return 0
    finally:
        user32.SetCursorPos(original_cursor.x, original_cursor.y)
        if window and process_id:
            close_process(window, process_id)
        server.shutdown()
        server.server_close()
        if controller is not None:
            controller.shutdown()
            controller.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
