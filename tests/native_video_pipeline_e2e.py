from __future__ import annotations

import ctypes
import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import ImageChops, ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lan_remote


def display_refresh_hz() -> int:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
    desktop_dc = user32.GetDC(None)
    try:
        return int(gdi32.GetDeviceCaps(desktop_dc, 116)) if desktop_dc else 0
    finally:
        if desktop_dc:
            user32.ReleaseDC(None, desktop_dc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exercise the real Windows native H.264 pipeline")
    parser.add_argument("--fps", type=int, choices=(30, 60, 120), default=60)
    parser.add_argument("--measure-seconds", type=float, default=2.0)
    parser.add_argument("--native-dir", type=Path)
    parser.add_argument("--enforce-performance", action="store_true")
    parser.add_argument("--exercise-secure-transition", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root_path = Path(__file__).resolve().parents[1]
    native_dir = args.native_dir or (root_path / "build" / "native")
    dll_path = native_dir / "WindowsLANRemoteVideo.dll"
    encoder_path = native_dir / "WindowsLANRemoteVideoEncoder.exe"
    if not dll_path.is_file() or not encoder_path.is_file():
        raise RuntimeError("build native video components before running the E2E test")
    lan_remote.native_video_encoder_path = lambda: encoder_path
    original_secure_desktop_active = lan_remote.secure_desktop_active
    secure_state = {"active": False}
    if args.exercise_secure_transition:
        lan_remote.secure_desktop_active = lambda: secure_state["active"]

    with tempfile.TemporaryDirectory() as appdata:
        previous_appdata = os.environ.get("APPDATA")
        previous_video_debug = os.environ.get("LAN_REMOTE_NATIVE_VIDEO_DEBUG")
        previous_test_pattern = os.environ.get("LAN_REMOTE_NATIVE_VIDEO_TEST_PATTERN")
        os.environ["APPDATA"] = appdata
        os.environ["LAN_REMOTE_NATIVE_VIDEO_DEBUG"] = "1"
        os.environ["LAN_REMOTE_NATIVE_VIDEO_TEST_PATTERN"] = "1"
        settings = lan_remote.SettingsStore()
        state = lan_remote.ServerState(
            token="TEST-TEMP-CODE",
            token_expires_at=time.time() + 600,
            view_only=False,
            allow_non_lan=False,
            started_at=time.time(),
            device_id="native-video-e2e",
            device_name="Native video E2E",
            port=0,
            registry=lan_remote.DiscoveryRegistry(),
            settings=settings,
        )
        server = lan_remote.RemoteServer(("127.0.0.1", 0), lan_remote.RemoteHandler, state)
        state.port = int(server.server_address[1])
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        window = tk.Tk()
        window.title("LAN Remote native video E2E")
        window.geometry("900x560+80+80")
        window.configure(bg="#991f2f")
        window.attributes("-topmost", True)
        activity = tk.Label(window, text="FRAME 0", bg="#ff264f", fg="white", font=("Segoe UI", 16, "bold"))
        activity.place(x=0, y=0, width=900, height=42)
        window.update()

        dll = ctypes.WinDLL(str(dll_path))
        dll.LANRemoteVideoCreate.argtypes = [ctypes.c_void_p]
        dll.LANRemoteVideoCreate.restype = ctypes.c_void_p
        dll.LANRemoteVideoGetLastError.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
        dll.LANRemoteVideoGetLastError.restype = ctypes.c_int
        dll.LANRemoteVideoConfigure.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        dll.LANRemoteVideoConfigure.restype = ctypes.c_int
        dll.LANRemoteVideoSetLayout.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        dll.LANRemoteVideoSetScaleMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        dll.LANRemoteVideoGetStatus.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        dll.LANRemoteVideoGetStatus.restype = ctypes.c_int
        dll.LANRemoteVideoDestroy.argtypes = [ctypes.c_void_p]

        handle = dll.LANRemoteVideoCreate(window.winfo_id())
        if not handle:
            error = ctypes.create_unicode_buffer(2048)
            dll.LANRemoteVideoGetLastError(error, len(error))
            raise RuntimeError(f"native video DLL did not create a child surface: {error.value}")
        try:
            configure_started = time.monotonic()
            started = dll.LANRemoteVideoConfigure(
                handle,
                "127.0.0.1",
                state.port,
                "TEST-TEMP-CODE",
                "all",
                args.fps,
                40,
                50,
                820,
                460,
            )
            if not started:
                raise RuntimeError("native video DLL rejected the E2E configuration")
            dll.LANRemoteVideoSetLayout(handle, 40, 50, 820, 460, 1)
            dll.LANRemoteVideoSetScaleMode(handle, 1)

            deadline = time.monotonic() + 20
            first_image = None
            first_rendered = 0
            first_render_ms: float | None = None
            measurement_started = 0.0
            measurement_start_rendered = 0
            measurement_start_decoded = 0
            final_status: dict[str, object] = {}
            color_changed = False
            next_color_change = time.monotonic() + 0.35
            color_toggle = False
            metric_samples: list[dict[str, float]] = []
            while time.monotonic() < deadline:
                window.update()
                if time.monotonic() >= next_color_change:
                    color_toggle = not color_toggle
                    window.configure(bg="#1f4599" if color_toggle else "#991f2f")
                    activity.configure(
                        text=f"FRAME {time.monotonic_ns()}",
                        bg="#246dff" if color_toggle else "#ff264f",
                    )
                    window.geometry(f"900x560+{82 if color_toggle else 80}+80")
                    color_changed = True
                    next_color_change = time.monotonic() + 0.35
                buffer = ctypes.create_unicode_buffer(4096)
                dll.LANRemoteVideoGetStatus(handle, buffer, len(buffer))
                final_status = json.loads(buffer.value or "{}")
                if final_status.get("state") == "failed":
                    raise RuntimeError(str(final_status.get("error") or "native video failed"))
                rendered = int(final_status.get("rendered_frames", 0))
                if rendered > 0 and first_render_ms is None:
                    first_render_ms = (time.monotonic() - configure_started) * 1000
                if measurement_started:
                    metric_samples.append({
                        key: float(final_status.get(key, 0) or 0)
                        for key in (
                            "actual_capture_fps",
                            "actual_encode_fps",
                            "actual_send_fps",
                            "actual_decode_fps",
                            "actual_render_fps",
                        )
                    })
                if rendered >= 8 and first_image is None:
                    window.update()
                    time.sleep(0.2)
                    first_image = ImageGrab.grab(
                        bbox=(
                            window.winfo_rootx() + 40,
                            window.winfo_rooty() + 50,
                            window.winfo_rootx() + 860,
                            window.winfo_rooty() + 510,
                        )
                    )
                    first_rendered = rendered
                    measurement_started = time.monotonic()
                    measurement_start_rendered = rendered
                    measurement_start_decoded = int(final_status.get("decoded_frames", 0))
                if (
                    first_image is not None
                    and time.monotonic() - measurement_started >= args.measure_seconds
                ):
                    second_image = ImageGrab.grab(
                        bbox=(
                            window.winfo_rootx() + 40,
                            window.winfo_rooty() + 50,
                            window.winfo_rootx() + 860,
                            window.winfo_rooty() + 510,
                        )
                    )
                    if ImageChops.difference(first_image, second_image).getbbox() is not None:
                        break
                time.sleep(0.02)
            else:
                raise RuntimeError(f"native video E2E timed out: {final_status}")

            if not color_changed or int(final_status.get("rendered_frames", 0)) < 20:
                raise RuntimeError(f"too few native video frames were rendered: {final_status}")
            if first_render_ms is None or first_render_ms > 2500:
                raise RuntimeError(
                    "native first frame exceeded the 2500 ms startup gate: "
                    f"first_render_ms={first_render_ms}, status={final_status}"
                )
            final_status["first_render_ms"] = round(first_render_ms, 2)
            if final_status.get("transport") != "native_h264_v1":
                raise RuntimeError(f"unexpected video transport: {final_status}")
            if final_status.get("scale_mode") != "fill":
                raise RuntimeError(f"native fill scale mode was not applied: {final_status}")
            measured_seconds = max(0.001, time.monotonic() - measurement_started)
            final_status["measured_seconds"] = round(measured_seconds, 3)
            final_status["measured_decode_fps"] = round(
                (int(final_status.get("decoded_frames", 0)) - measurement_start_decoded) /
                measured_seconds,
                2,
            )
            final_status["measured_render_fps"] = round(
                (int(final_status.get("rendered_frames", 0)) - measurement_start_rendered) /
                measured_seconds,
                2,
            )
            for key in (
                "actual_capture_fps",
                "actual_encode_fps",
                "actual_send_fps",
                "actual_decode_fps",
                "actual_render_fps",
            ):
                values = [sample[key] for sample in metric_samples if sample[key] > 0]
                if values:
                    final_status[f"median_{key}"] = round(statistics.median(values), 2)
            if args.enforce_performance:
                refresh_hz = display_refresh_hz()
                final_status["display_refresh_hz"] = refresh_hz
                sender_floor = {30: 29.0, 60: 55.0, 120: 90.0}[args.fps]
                render_floor = sender_floor
                if args.fps == 120 and refresh_hz > 1:
                    sender_floor = min(sender_floor, refresh_hz * 0.94)
                    render_floor = min(render_floor, refresh_hz * 0.94)
                encode_fps = float(final_status.get("median_actual_encode_fps", 0))
                render_fps = float(final_status.get("measured_render_fps", 0))
                if encode_fps < sender_floor or render_fps < render_floor:
                    raise RuntimeError(
                        f"native video performance gate failed: encode={encode_fps:.2f}/"
                        f"{sender_floor:.2f}, render={render_fps:.2f}/{render_floor:.2f}, "
                        f"status={final_status}"
                    )
                latency_samples = int(final_status.get("presentation_latency_samples", 0))
                latency_p50 = float(final_status.get("presentation_latency_p50_ms", 0))
                latency_p95 = float(final_status.get("presentation_latency_p95_ms", 0))
                if latency_samples < 30 or latency_p50 > 50.0 or latency_p95 > 100.0:
                    raise RuntimeError(
                        "native video latency gate failed: "
                        f"samples={latency_samples}, p50={latency_p50:.2f}/50.00ms, "
                        f"p95={latency_p95:.2f}/100.00ms, status={final_status}"
                    )
            if args.exercise_secure_transition:
                initial_generation = int(final_status.get("generation", 0))
                secure_state["active"] = True
                transition_deadline = time.monotonic() + 5
                while time.monotonic() < transition_deadline:
                    window.update()
                    buffer = ctypes.create_unicode_buffer(4096)
                    dll.LANRemoteVideoGetStatus(handle, buffer, len(buffer))
                    transition_status = json.loads(buffer.value or "{}")
                    if transition_status.get("state") == "failed":
                        if "secure desktop" not in str(transition_status.get("error", "")).lower():
                            raise RuntimeError(f"unexpected secure transition failure: {transition_status}")
                        break
                    time.sleep(0.02)
                else:
                    raise RuntimeError("native stream did not enter secure-desktop fallback")
                secure_state["active"] = False
                if not dll.LANRemoteVideoConfigure(
                    handle, "127.0.0.1", state.port, "TEST-TEMP-CODE", "all",
                    args.fps, 40, 50, 820, 460,
                ):
                    raise RuntimeError("native stream could not restart after secure desktop")
                dll.LANRemoteVideoSetLayout(handle, 40, 50, 820, 460, 1)
                recovery_deadline = time.monotonic() + 10
                while time.monotonic() < recovery_deadline:
                    window.update()
                    buffer = ctypes.create_unicode_buffer(4096)
                    dll.LANRemoteVideoGetStatus(handle, buffer, len(buffer))
                    recovered = json.loads(buffer.value or "{}")
                    if (
                        recovered.get("state") == "streaming"
                        and int(recovered.get("generation", 0)) != initial_generation
                        and int(recovered.get("rendered_frames", 0)) > 0
                    ):
                        final_status["secure_transition_recovered"] = True
                        break
                    if recovered.get("state") == "failed":
                        raise RuntimeError(f"native secure-desktop recovery failed: {recovered}")
                    time.sleep(0.02)
                else:
                    raise RuntimeError("native stream did not recover after secure desktop")
            print(json.dumps(final_status, ensure_ascii=False, sort_keys=True))
        finally:
            dll.LANRemoteVideoDestroy(handle)
            window.destroy()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)
            if previous_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = previous_appdata
            if previous_video_debug is None:
                os.environ.pop("LAN_REMOTE_NATIVE_VIDEO_DEBUG", None)
            else:
                os.environ["LAN_REMOTE_NATIVE_VIDEO_DEBUG"] = previous_video_debug
            if previous_test_pattern is None:
                os.environ.pop("LAN_REMOTE_NATIVE_VIDEO_TEST_PATTERN", None)
            else:
                os.environ["LAN_REMOTE_NATIVE_VIDEO_TEST_PATTERN"] = previous_test_pattern
            lan_remote.secure_desktop_active = original_secure_desktop_active
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
