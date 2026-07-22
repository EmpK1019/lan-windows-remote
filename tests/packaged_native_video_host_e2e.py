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
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import ImageChops, ImageGrab, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lan_remote


WINDOW_TITLE = "LAN Remote Native Host E2E"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exercise native H.264 through the packaged WebView2 host")
    parser.add_argument("--control-host", required=True, type=Path)
    parser.add_argument("--native-dir", required=True, type=Path)
    return parser.parse_args()


def find_window(title: str) -> int:
    result = ctypes.c_void_p()
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(window: int, _: int) -> bool:
        length = ctypes.windll.user32.GetWindowTextLengthW(window)
        if length <= 0:
            return True
        text = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(window, text, len(text))
        if text.value == title and ctypes.windll.user32.IsWindowVisible(window):
            result.value = window
            return False
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return int(result.value or 0)


def child_window_texts(parent: int) -> list[str]:
    results: list[str] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(window: int, _: int) -> bool:
        length = ctypes.windll.user32.GetWindowTextLengthW(window)
        if length > 0:
            text = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(window, text, len(text))
            if text.value:
                results.append(text.value)
        return True

    ctypes.windll.user32.EnumChildWindows(parent, callback, 0)
    return results


def process_windows(process_id: int) -> list[tuple[int, str, bool, list[str]]]:
    results: list[tuple[int, str, bool, list[str]]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(window: int, _: int) -> bool:
        owner = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if int(owner.value) != process_id:
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(window)
        text = ctypes.create_unicode_buffer(max(1, length + 1))
        ctypes.windll.user32.GetWindowTextW(window, text, len(text))
        results.append(
            (
                int(window),
                text.value,
                bool(ctypes.windll.user32.IsWindowVisible(window)),
                child_window_texts(window),
            )
        )
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return results


def window_rect(window: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(window, ctypes.byref(rect)):
        raise RuntimeError("could not read the packaged control window bounds")
    return rect.left, rect.top, rect.right, rect.bottom


def visible_owned_windows(owner: int) -> list[tuple[int, int, int, int]]:
    results: list[tuple[int, int, int, int]] = []
    get_window = ctypes.windll.user32.GetWindow
    get_window.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    get_window.restype = ctypes.c_void_p
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(window: int, _: int) -> bool:
        if get_window(window, 4) != owner:
            return True
        if not ctypes.windll.user32.IsWindowVisible(window):
            return True
        results.append(window_rect(window))
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return results


def main() -> int:
    args = parse_args()
    control_host = args.control_host.resolve()
    encoder = (args.native_dir / "WindowsLANRemoteVideoEncoder.exe").resolve()
    video_dll = (args.native_dir / "WindowsLANRemoteVideo.dll").resolve()
    if not control_host.is_file() or not encoder.is_file() or not video_dll.is_file():
        raise RuntimeError("packaged native host test inputs are incomplete")

    lan_remote.native_video_encoder_path = lambda: encoder
    with tempfile.TemporaryDirectory() as appdata:
        previous_appdata = os.environ.get("APPDATA")
        previous_localappdata = os.environ.get("LOCALAPPDATA")
        previous_pattern = os.environ.get("LAN_REMOTE_NATIVE_VIDEO_TEST_PATTERN")
        os.environ["APPDATA"] = appdata
        os.environ["LOCALAPPDATA"] = appdata
        os.environ["LAN_REMOTE_NATIVE_VIDEO_TEST_PATTERN"] = "1"
        settings = lan_remote.SettingsStore()
        state = lan_remote.ServerState(
            token="TEST-TEMP-CODE",
            token_expires_at=time.time() + 600,
            view_only=False,
            allow_non_lan=False,
            started_at=time.time(),
            device_id="packaged-native-host-e2e",
            device_name="Packaged native host E2E",
            port=0,
            registry=lan_remote.DiscoveryRegistry(),
            settings=settings,
        )
        video_server = lan_remote.RemoteServer(("127.0.0.1", 0), lan_remote.RemoteHandler, state)
        state.port = int(video_server.server_address[1])
        video_thread = threading.Thread(target=video_server.serve_forever, daemon=True)
        video_thread.start()

        report_event = threading.Event()
        page_event = threading.Event()
        report: dict[str, object] = {}

        class PageHandler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                return

            def do_GET(self) -> None:
                page_event.set()
                config = {
                    "enabled": True,
                    "endpoint": f"http://127.0.0.1:{state.port}/video-stream",
                    "token": "TEST-TEMP-CODE",
                    "monitor": "all",
                    "fps_limit": 60,
                    "scale_mode": "fill",
                    "surface_left": 40,
                    "surface_top": 80,
                    "surface_width": 800,
                    "surface_height": 450,
                }
                html = f"""<!doctype html><meta charset=utf-8><title>{WINDOW_TITLE}</title>
<style>html,body{{margin:0;background:#101116;color:white;font-family:Segoe UI}}#stage{{position:absolute;left:40px;top:80px;width:800px;height:450px;background:#ff0033}}</style>
<div id=stage></div><script>
async function startNativeHostTest() {{
 try {{
  await window.pywebview.api.set_window_title({json.dumps(WINDOW_TITLE)});
  const config = {json.dumps(config)};
  const started = await window.pywebview.api.configure_native_video(config);
  let surfaced = false;
  let hiddenRenderedFrames = -1;
  for (let attempt=0; attempt<300; attempt++) {{
    const status = await window.pywebview.api.native_video_status();
    if (!started || status.state === 'failed') {{
      await fetch('/result', {{method:'POST', body:JSON.stringify(status)}});
      return;
    }}
    if (!surfaced && Number(status.decoded_frames || 0) > 0) {{
      hiddenRenderedFrames = Number(status.rendered_frames || 0);
      if (hiddenRenderedFrames !== 0) {{
        await fetch('/result', {{method:'POST', body:JSON.stringify({{
          ...status, state:'failed', error:'hidden native surface was presented',
          hidden_rendered_frames:hiddenRenderedFrames
        }})}});
        return;
      }}
      await window.pywebview.api.set_native_video_layout({{...config, visible:true}});
      await window.pywebview.api.set_native_overlay_state({{
        visible:true, collapsed:false, unlock_visible:false, view_only:false,
        fps:60, scale_mode:'fill', keyboard:true, clipboard:true, fullscreen:false,
        status_error:false, monitors:[{{id:'all',label:'全部显示器'}}]
      }});
      surfaced = true;
    }}
    if (status.state === 'streaming' && Number(status.rendered_frames || 0) >= 30) {{
      await fetch('/result', {{method:'POST', body:JSON.stringify({{
        ...status, hidden_rendered_frames:hiddenRenderedFrames
      }})}});
      return;
    }}
    await new Promise(resolve => setTimeout(resolve, 50));
  }}
  await fetch('/result', {{method:'POST', body:JSON.stringify({{state:'failed',error:'host timeout'}})}});
 }} catch (error) {{
   await fetch('/result', {{method:'POST', body:JSON.stringify({{state:'failed',error:String(error && error.message || error)}})}});
 }}
}}
if (window.pywebview && window.pywebview.api) startNativeHostTest();
else window.addEventListener('pywebviewready', startNativeHostTest, {{once:true}});
</script>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)

            def do_POST(self) -> None:
                if self.path != "/result":
                    self.send_error(404)
                    return
                length = min(int(self.headers.get("Content-Length", "0")), 16 * 1024)
                try:
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    if isinstance(value, dict):
                        report.update(value)
                finally:
                    report_event.set()
                self.send_response(204)
                self.end_headers()

        page_server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
        page_thread = threading.Thread(target=page_server.serve_forever, daemon=True)
        page_thread.start()
        url = (
            f"http://127.0.0.1:{page_server.server_port}/?remote=1&"
            "handoff=0123456789abcdef"
        )
        environment = os.environ.copy()
        process = subprocess.Popen(
            [str(control_host), "--url", url],
            cwd=str(control_host.parent),
            env=environment,
        )
        try:
            if not report_event.wait(30):
                raise RuntimeError(
                    "packaged native control host did not report video readiness "
                    f"(page_requested={page_event.is_set()}, process_exit={process.poll()}, "
                    f"window={find_window(WINDOW_TITLE)}, "
                    f"process_windows={process_windows(process.pid)})"
                )
            if report.get("state") != "streaming" or int(report.get("rendered_frames", 0)) < 30:
                raise RuntimeError(f"packaged native control host failed: {report}")
            if int(report.get("hidden_rendered_frames", -1)) != 0:
                raise RuntimeError(f"hidden native surface was presented: {report}")
            window = find_window(WINDOW_TITLE)
            if not window:
                raise RuntimeError("packaged native control host window was not visible")
            left, top, right, bottom = window_rect(window)
            if right - left < 900 or bottom - top < 550:
                raise RuntimeError("packaged native control host window is unexpectedly small")
            ctypes.windll.user32.SetWindowPos(
                window, 0, 100, 100, right - left, bottom - top, 0x0040
            )
            ctypes.windll.user32.SetForegroundWindow(window)
            left, top, right, bottom = window_rect(window)
            time.sleep(0.25)
            owned = visible_owned_windows(window)
            if not any(
                owned_right - owned_left >= 300 and 30 <= owned_bottom - owned_top <= 120
                for owned_left, owned_top, owned_right, owned_bottom in owned
            ):
                raise RuntimeError(f"native glass toolbar was not visible above the D3D surface: {owned}")
            first = ImageGrab.grab(bbox=(left, top, right, bottom)).convert("RGB")
            preview_path = Path(__file__).resolve().parents[1] / "build" / "native-glass-toolbar-e2e.png"
            first.save(preview_path)
            time.sleep(0.35)
            second = ImageGrab.grab(bbox=(left, top, right, bottom)).convert("RGB")
            difference = ImageChops.difference(first, second)
            delta = sum(ImageStat.Stat(difference).mean)
            if difference.getbbox() is None or delta < 0.01:
                debug_path = Path(__file__).resolve().parents[1] / "build" / "packaged-native-host-debug.png"
                second.save(debug_path)
                raise RuntimeError(
                    "packaged native video surface did not visibly update; "
                    f"rect={(left, top, right, bottom)}, screenshot={debug_path}"
                )
            print(json.dumps({**report, "visible_surface_delta": round(delta, 3)}, sort_keys=True))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            page_server.shutdown()
            page_server.server_close()
            page_thread.join(timeout=3)
            video_server.shutdown()
            video_server.server_close()
            video_thread.join(timeout=3)
            for name, value in (
                ("APPDATA", previous_appdata),
                ("LOCALAPPDATA", previous_localappdata),
                ("LAN_REMOTE_NATIVE_VIDEO_TEST_PATTERN", previous_pattern),
            ):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
