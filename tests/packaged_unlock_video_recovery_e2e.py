from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from PIL import ImageChops, ImageGrab, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lan_remote
from packaged_native_video_host_e2e import find_window, window_rect


WINDOW_DEVICE_NAME = "Unlock recovery E2E"
WINDOW_TITLE = f"{WINDOW_DEVICE_NAME} · LAN Remote"
SESSION_ID = "unlock-recovery-session-0001"
ACCESS_TOKEN = "TEST-TEMP-CODE"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise locked native video recovery through the packaged WebView2 host"
    )
    parser.add_argument("--control-host", required=True, type=Path)
    parser.add_argument("--native-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    control_host = args.control_host.resolve()
    encoder = (args.native_dir / "WindowsLANRemoteVideoEncoder.exe").resolve()
    video_dll = (args.native_dir / "WindowsLANRemoteVideo.dll").resolve()
    if not control_host.is_file() or not encoder.is_file() or not video_dll.is_file():
        raise RuntimeError("packaged unlock recovery test inputs are incomplete")

    original_encoder_path = lan_remote.native_video_encoder_path
    original_start_encoder = lan_remote.start_native_video_encoder
    original_current_session_locked = lan_remote.current_session_locked
    original_secure_desktop_active = lan_remote.secure_desktop_active
    original_secure_video_required = lan_remote.secure_video_source_required
    original_secure_available = lan_remote.secure_native_video_available
    original_open_secure = lan_remote.open_secure_native_video_stream
    original_input_desktop_name = lan_remote.input_desktop_name
    original_foreground_process_name = lan_remote.foreground_process_name

    lock_state = {"locked": True, "unlock_at": 0.0}
    video_connections: list[float] = []
    normal_encoder_starts: list[float] = []
    native_status_reports: list[tuple[float, dict[str, object]]] = []
    secure_streams: list[StallingSecureStream] = []
    secure_stream_ready = threading.Event()
    unlock_requested = threading.Event()

    class FakeSocket:
        def __init__(self, stream: "StallingSecureStream") -> None:
            self.stream = stream

        def shutdown(self, _how: int) -> None:
            self.stream.aborted.set()

        def close(self) -> None:
            self.stream.aborted.set()

    class StallingSecureStream:
        def __init__(self, monitor: str, fps: int, generation: int) -> None:
            source = original_start_encoder(monitor, fps, generation)
            payload = bytearray()
            access_units = 0
            try:
                while access_units < 18:
                    if source.process.stdout is None:
                        raise RuntimeError("secure test encoder stdout is unavailable")
                    packet = lan_remote.read_native_video_packet(source.process.stdout)
                    if packet is None:
                        raise RuntimeError("secure test encoder ended before producing frames")
                    message, packed = packet
                    payload.extend(packed)
                    if message.message_type == lan_remote.NATIVE_VIDEO_MESSAGE_ACCESS_UNIT:
                        access_units += 1
            finally:
                lan_remote.stop_native_video_encoder(source)
            self.payload = payload
            self.payload_lock = threading.Lock()
            self.aborted = threading.Event()
            self._lan_remote_socket = FakeSocket(self)
            secure_stream_ready.set()

        def read(self, length: int) -> bytes:
            with self.payload_lock:
                if self.payload:
                    result = bytes(self.payload[:length])
                    del self.payload[:length]
                    return result
            self.aborted.wait(15)
            return b""

        def close(self) -> None:
            self.aborted.wait(15)

    class UnlockCredentialApi:
        def try_auto_unlock(
            self,
            _device_json: str,
            _access_password: str,
            force: bool = False,
        ) -> dict[str, object]:
            del force
            unlock_requested.set()
            if not secure_stream_ready.wait(8):
                return {"ok": False, "status": "transition_timeout"}
            # Leave enough time for the initial secure access units to reach
            # the real D3D child window before making the state transition.
            time.sleep(0.7)
            lock_state["locked"] = False
            lock_state["unlock_at"] = time.monotonic()
            return {"ok": True, "status": "submitted"}

    class RecordingRemoteHandler(lan_remote.RemoteHandler):
        def log_message(self, _format: str, *args: object) -> None:
            del args

        def do_GET(self) -> None:
            if urlparse(self.path).path == "/":
                diagnostic_script = """<script>
setInterval(async () => {
  try {
    if (!window.pywebview?.api?.native_video_status) return;
    const status = await window.pywebview.api.native_video_status();
    await fetch('/__test/native-status', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        status,
        native_video_active: Boolean(state.nativeVideoActive),
        native_video_revision: Number(state.nativeVideoRevision || 0),
        remote_session_locked: Boolean(state.remoteSessionLocked)
      })
    });
  } catch {}
}, 150);
</script>"""
                html = lan_remote.INDEX_HTML.replace("</body>", diagnostic_script + "</body>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
                return
            super().do_GET()

        def do_POST(self) -> None:
            if urlparse(self.path).path == "/__test/native-status":
                length = min(int(self.headers.get("Content-Length", "0")), 64 * 1024)
                try:
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    if isinstance(value, dict):
                        native_status_reports.append((time.monotonic(), value))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    pass
                self.send_response(204)
                self.end_headers()
                return
            super().do_POST()

        def do_CONNECT(self) -> None:
            if urlparse(self.path).path == "/video-stream":
                video_connections.append(time.monotonic())
            super().do_CONNECT()

    class QuietRemoteServer(lan_remote.RemoteServer):
        def handle_error(self, request: object, client_address: object) -> None:
            error = sys.exc_info()[1]
            if isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
                return
            super().handle_error(request, client_address)

    def open_secure(
        monitor: str,
        fps: int,
        generation: int,
        *,
        timeout: float = 4.0,
    ) -> StallingSecureStream:
        del timeout
        stream = StallingSecureStream(monitor, fps, generation)
        secure_streams.append(stream)
        return stream

    def start_normal(
        monitor: str,
        fps: int,
        generation: int,
    ) -> lan_remote.NativeVideoEncoderProcess:
        normal_encoder_starts.append(time.monotonic())
        return original_start_encoder(monitor, fps, generation)

    process: subprocess.Popen[bytes] | None = None
    server: lan_remote.RemoteServer | None = None
    server_thread: threading.Thread | None = None
    with tempfile.TemporaryDirectory() as appdata:
        previous_environment = {
            name: os.environ.get(name)
            for name in (
                "APPDATA",
                "LOCALAPPDATA",
                "LAN_REMOTE_NATIVE_VIDEO_TEST_PATTERN",
                "LAN_REMOTE_NATIVE_VIDEO_DEBUG",
            )
        }
        os.environ["APPDATA"] = appdata
        os.environ["LOCALAPPDATA"] = appdata
        os.environ["LAN_REMOTE_NATIVE_VIDEO_TEST_PATTERN"] = "1"
        os.environ["LAN_REMOTE_NATIVE_VIDEO_DEBUG"] = "1"
        try:
            lan_remote.native_video_encoder_path = lambda: encoder
            lan_remote.start_native_video_encoder = start_normal
            lan_remote.current_session_locked = lambda: bool(lock_state["locked"])
            lan_remote.secure_desktop_active = lambda: bool(lock_state["locked"])
            lan_remote.secure_video_source_required = lambda: bool(lock_state["locked"])
            lan_remote.secure_native_video_available = lambda: True
            lan_remote.open_secure_native_video_stream = open_secure
            lan_remote.input_desktop_name = lambda: "Winlogon" if lock_state["locked"] else "Default"
            lan_remote.foreground_process_name = lambda: "LockApp.exe" if lock_state["locked"] else "explorer.exe"

            settings = lan_remote.SettingsStore()
            state = lan_remote.ServerState(
                token=ACCESS_TOKEN,
                token_expires_at=time.time() + 600,
                view_only=False,
                allow_non_lan=False,
                started_at=time.time(),
                device_id="unlock-recovery-controller",
                device_name="Unlock recovery controller",
                port=0,
                registry=lan_remote.DiscoveryRegistry(),
                settings=settings,
            )
            state.credential_api = UnlockCredentialApi()
            server = QuietRemoteServer(("127.0.0.1", 0), RecordingRemoteHandler, state)
            state.port = int(server.server_address[1])
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()

            session = {
                "device": {
                    "id": "unlock-recovery-target",
                    "name": WINDOW_DEVICE_NAME,
                    "ip": "127.0.0.1",
                    "port": state.port,
                    "os": "Windows E2E",
                    "view_only": False,
                    "is_self": False,
                    "video_capabilities": [lan_remote.NATIVE_VIDEO_CAPABILITY],
                    "app_version": lan_remote.APP_VERSION,
                },
                "token": ACCESS_TOKEN,
                "viewOnly": False,
                "authMethod": "temporary",
                "credentialExpiresAt": int((time.time() + 600) * 1000),
                "controllerSessionId": SESSION_ID,
                "controllerId": "unlock-recovery-controller",
                "controllerName": "Unlock recovery controller",
            }
            handoff = state.create_remote_window_session(session)
            state.register_outgoing_session(session, os.getpid())
            url = f"http://127.0.0.1:{state.port}/?remote=1&handoff={handoff}"
            process = subprocess.Popen(
                [str(control_host), "--url", url],
                cwd=str(control_host.parent),
                env=os.environ.copy(),
            )

            deadline = time.monotonic() + 20
            window = 0
            while time.monotonic() < deadline:
                window = find_window(WINDOW_TITLE)
                if window and unlock_requested.is_set() and secure_stream_ready.is_set():
                    break
                if process.poll() is not None:
                    raise RuntimeError(f"control host exited before unlock (code={process.returncode})")
                time.sleep(0.05)
            else:
                raise RuntimeError(
                    "real control page did not render and request its initial automatic unlock "
                    f"(window={window}, secure_ready={secure_stream_ready.is_set()}, "
                    f"unlock_requested={unlock_requested.is_set()}, connections={len(video_connections)})"
                )

            transition_deadline = time.monotonic() + 8
            while time.monotonic() < transition_deadline:
                if (
                    not lock_state["locked"]
                    and len(video_connections) >= 2
                    and normal_encoder_starts
                    and secure_streams
                    and secure_streams[0].aborted.is_set()
                ):
                    break
                if process.poll() is not None:
                    raise RuntimeError(f"control host exited during recovery (code={process.returncode})")
                time.sleep(0.05)
            else:
                raise RuntimeError(
                    "locked-to-unlocked recovery did not rebuild the native video connection "
                    f"(locked={lock_state['locked']}, connections={len(video_connections)}, "
                    f"normal_starts={len(normal_encoder_starts)}, "
                    f"secure_aborted={bool(secure_streams and secure_streams[0].aborted.is_set())})"
                )

            recovery_seconds = video_connections[1] - float(lock_state["unlock_at"])
            if recovery_seconds > 4.0:
                raise RuntimeError(f"native video reconnect was too slow after unlock: {recovery_seconds:.3f}s")

            rendered_after_reconnect: dict[str, object] | None = None
            render_deadline = time.monotonic() + 10
            while time.monotonic() < render_deadline:
                for reported_at, report in reversed(native_status_reports):
                    status = report.get("status")
                    if (
                        reported_at >= video_connections[1]
                        and isinstance(status, dict)
                        and status.get("state") == "streaming"
                        and int(status.get("rendered_frames", 0)) >= 10
                        and report.get("native_video_active") is True
                        and report.get("remote_session_locked") is False
                    ):
                        rendered_after_reconnect = report
                        break
                if rendered_after_reconnect is not None:
                    break
                if process.poll() is not None:
                    raise RuntimeError(f"control host exited before rendering recovered video (code={process.returncode})")
                time.sleep(0.05)
            if rendered_after_reconnect is None:
                raise RuntimeError(
                    "the rebuilt native connection never rendered a Default desktop frame; "
                    f"recent_status={native_status_reports[-5:]}"
                )

            left, top, right, bottom = window_rect(window)
            ctypes.windll.user32.SetWindowPos(
                window, 0, 100, 100, right - left, bottom - top, 0x0040
            )
            ctypes.windll.user32.SetForegroundWindow(window)
            left, top, right, bottom = window_rect(window)
            time.sleep(0.25)
            visible_delta = 0.0
            visual_deadline = time.monotonic() + 8
            previous = ImageGrab.grab(bbox=(left, top, right, bottom)).convert("RGB")
            while time.monotonic() < visual_deadline:
                time.sleep(0.25)
                current = ImageGrab.grab(bbox=(left, top, right, bottom)).convert("RGB")
                difference = ImageChops.difference(previous, current)
                visible_delta = max(visible_delta, sum(ImageStat.Stat(difference).mean))
                if difference.getbbox() is not None and visible_delta >= 0.02:
                    break
                previous = current
            else:
                debug_path = Path(__file__).resolve().parents[1] / "build" / "unlock-recovery-e2e.png"
                previous.save(debug_path)
                raise RuntimeError(
                    "Default source connected but the real D3D control surface did not keep updating; "
                    f"screenshot={debug_path}"
                )

            print(
                "PACKAGED_UNLOCK_VIDEO_RECOVERY_E2E_OK "
                + json.dumps(
                    {
                        "connections": len(video_connections),
                        "normal_encoder_starts": len(normal_encoder_starts),
                        "recovery_seconds": round(recovery_seconds, 3),
                        "rendered_frames": int(
                            dict(rendered_after_reconnect.get("status", {})).get("rendered_frames", 0)
                        ),
                        "secure_stream_aborted": secure_streams[0].aborted.is_set(),
                        "visible_surface_delta": round(visible_delta, 3),
                    },
                    sort_keys=True,
                )
            )
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in secure_streams:
                stream.aborted.set()
            if server is not None:
                server.shutdown()
                server.server_close()
            if server_thread is not None:
                server_thread.join(timeout=3)
            lan_remote.native_video_encoder_path = original_encoder_path
            lan_remote.start_native_video_encoder = original_start_encoder
            lan_remote.current_session_locked = original_current_session_locked
            lan_remote.secure_desktop_active = original_secure_desktop_active
            lan_remote.secure_video_source_required = original_secure_video_required
            lan_remote.secure_native_video_available = original_secure_available
            lan_remote.open_secure_native_video_stream = original_open_secure
            lan_remote.input_desktop_name = original_input_desktop_name
            lan_remote.foreground_process_name = original_foreground_process_name
            for name, value in previous_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
