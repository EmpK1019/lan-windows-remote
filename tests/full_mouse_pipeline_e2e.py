from __future__ import annotations

import ctypes
import io
import json
import os
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
CONTROLLER_PORT = int(os.environ.get("LAN_REMOTE_CONTROLLER_PORT", "8765"))
WM_CLOSE = 0x0010
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
PROCESS_SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
WAIT_TIMEOUT = 0x00000102


class AuditState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.input_event = threading.Event()
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self.screen_requests = 0

    def record(self, source: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.frames.append((source, payload))
            types = {str(item.get("type", "")) for _, item in self.frames}
            if {"mouse_down", "mouse_up", "mouse_wheel"}.issubset(types):
                self.input_event.set()


def png_frame() -> bytes:
    image = Image.new("RGB", (1280, 720), (17, 27, 42))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


FRAME = png_frame()


class AuditHandler(BaseHTTPRequestHandler):
    server: "AuditServer"

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
        if path == "/screen":
            with self.server.state.lock:
                self.server.state.screen_requests += 1
            self.send_response(HTTPStatus.OK)
            self.common_headers("image/png", len(FRAME))
            self.end_headers()
            self.wfile.write(FRAME)
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
kernel32 = ctypes.windll.kernel32

user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
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


def force_foreground(window: int) -> None:
    foreground = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
    attached = bool(
        foreground_thread
        and foreground_thread != current_thread
        and user32.AttachThreadInput(current_thread, foreground_thread, True)
    )
    try:
        user32.BringWindowToTop(window)
        user32.SetForegroundWindow(window)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


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

        force_foreground(window)
        time.sleep(0.4)
        if int(user32.GetForegroundWindow()) != window:
            raise RuntimeError("actual remote control window could not become foreground")

        rect = wintypes.RECT()
        if not user32.GetClientRect(window, ctypes.byref(rect)):
            raise ctypes.WinError()
        point = wintypes.POINT(
            max(1, (rect.right - rect.left) // 2),
            max(1, int((rect.bottom - rect.top) * 0.68)),
        )
        if not user32.ClientToScreen(window, ctypes.byref(point)):
            raise ctypes.WinError()
        if not user32.SetCursorPos(point.x, point.y):
            raise ctypes.WinError()
        time.sleep(0.25)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, 120, 0)

        if not state.input_event.wait(6):
            with state.lock:
                observed = list(state.frames)
            raise TimeoutError(f"actual page/native bridge did not forward click and wheel: {observed}")
        with state.lock:
            observed = list(state.frames)
        sources = sorted({source for source, _ in observed})
        non_move = [
            (source, payload)
            for source, payload in observed
            if payload.get("type") != "mouse_move"
        ]
        print(
            "FULL_MOUSE_PIPELINE_E2E_OK "
            f"pid={process_id} point={point.x},{point.y} "
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
