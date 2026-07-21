#!/usr/bin/env python3
"""Measure the real DXGI/JPEG/HTTP stream and enforce the 33 FPS floor."""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lan_remote
from full_mouse_pipeline_e2e import start_embedded_controller


FRAME_COUNT = 150
WARMUP_FRAMES = 10
CURSOR_SAMPLE_COUNT = 180


def read_headers(stream: object) -> tuple[bytes, dict[str, str]]:
    status = stream.readline().strip()
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line in (b"", b"\r\n"):
            return status, headers
        key, value = line.decode("latin1").split(":", 1)
        headers[key.lower()] = value.strip()


def measure_cursor_stream(port: int, token: str, arrivals: list[float], errors: list[BaseException]) -> None:
    connection: socket.socket | None = None
    stream: object | None = None
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=5)
        connection.settimeout(5)
        stream = connection.makefile("rb")
        connection.sendall(
            (
                f"GET /cursor-stream?monitor=all&token={token} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
        )
        status, headers = read_headers(stream)
        if b" 200 " not in status or headers.get("x-remote-cursor-fps") != "120":
            raise RuntimeError(f"cursor stream failed: {status!r} {headers!r}")
        for _ in range(CURSOR_SAMPLE_COUNT):
            payload = json.loads(stream.readline().decode("utf-8"))
            if not payload.get("ok") or payload.get("input_source") not in {"controller", "controlled"}:
                raise RuntimeError(f"invalid cursor payload: {payload!r}")
            arrivals.append(time.perf_counter())
    except BaseException as exc:
        errors.append(exc)
    finally:
        if stream is not None:
            stream.close()
        if connection is not None:
            connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fps", type=int, choices=(30, 60, 120), default=60)
    parser.add_argument("--minimum-fps", type=float, default=33)
    args = parser.parse_args()
    max_p95_frame_interval_ms = 1000 / args.minimum_fps
    lan_remote.set_dpi_awareness()
    lan_remote.configure_win32_signatures()
    server = start_embedded_controller()
    token = server.state.create_session_token({"auth_method": "temporary"})
    cursor_arrivals: list[float] = []
    cursor_errors: list[BaseException] = []
    cursor_thread = threading.Thread(
        target=measure_cursor_stream,
        args=(server.server_port, token, cursor_arrivals, cursor_errors),
        daemon=True,
    )
    cursor_thread.start()
    connection = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    connection.settimeout(5)
    stream = connection.makefile("rb")
    try:
        connection.sendall(
            (
                "GET /screen-stream?monitor=all&cursor=0&token="
                f"{token}&fps={args.fps} HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        status, headers = read_headers(stream)
        if b" 200 " not in status:
            raise RuntimeError(f"screen stream failed: {status!r}")
        if not headers.get("content-type", "").startswith("multipart/x-mixed-replace"):
            raise RuntimeError(f"unexpected stream type: {headers.get('content-type')}")
        if headers.get("x-remote-fps-limit") != str(args.fps):
            raise RuntimeError(f"unexpected FPS ceiling: {headers.get('x-remote-fps-limit')}")

        arrivals: list[float] = []
        sizes: list[int] = []
        for _ in range(FRAME_COUNT):
            boundary = stream.readline().strip()
            if not boundary:
                boundary = stream.readline().strip()
            if boundary != b"--lan-remote-frame":
                raise RuntimeError(f"unexpected frame boundary: {boundary!r}")
            _, part_headers = read_headers(stream)
            size = int(part_headers.get("content-length", "0"))
            frame = stream.read(size)
            ending = stream.read(2)
            if len(frame) != size or ending != b"\r\n":
                raise RuntimeError("screen stream frame was truncated")
            arrivals.append(time.perf_counter())
            sizes.append(size)

        intervals = [
            (later - earlier) * 1000
            for earlier, later in zip(
                arrivals[WARMUP_FRAMES:-1],
                arrivals[WARMUP_FRAMES + 1 :],
            )
        ]
        median_interval = statistics.median(intervals)
        p95_interval = sorted(intervals)[int(len(intervals) * 0.95) - 1]
        if p95_interval > max_p95_frame_interval_ms:
            raise RuntimeError(
                f"screen stream missed the {args.minimum_fps:g} FPS floor: median={median_interval:.1f} ms "
                f"p95={p95_interval:.1f} ms"
            )
        cursor_thread.join(timeout=5)
        if cursor_thread.is_alive():
            raise RuntimeError("cursor stream did not complete while the screen stream was active")
        if cursor_errors:
            raise RuntimeError(f"cursor stream failed: {cursor_errors[0]}")
        cursor_intervals = [
            (later - earlier) * 1000
            for earlier, later in zip(
                cursor_arrivals[WARMUP_FRAMES:-1],
                cursor_arrivals[WARMUP_FRAMES + 1 :],
            )
        ]
        cursor_median_interval = statistics.median(cursor_intervals)
        cursor_p95_interval = sorted(cursor_intervals)[int(len(cursor_intervals) * 0.95) - 1]
        if cursor_p95_interval > 25:
            raise RuntimeError(
                f"cursor stream cadence regressed: median={cursor_median_interval:.1f} ms "
                f"p95={cursor_p95_interval:.1f} ms"
            )
        print(
            "LOW_LATENCY_SCREEN_STREAM_OK "
            f"limit={args.fps} "
            f"frames={len(arrivals)} median_fps={1000 / median_interval:.1f} "
            f"p95_fps={1000 / p95_interval:.1f} "
            f"cursor_median_hz={1000 / cursor_median_interval:.1f} "
            f"cursor_p95_hz={1000 / cursor_p95_interval:.1f} "
            f"median_kb={statistics.median(sizes) / 1024:.1f}"
        )
        return 0
    finally:
        stream.close()
        connection.close()
        server.shutdown()
        server.server_close()
        lan_remote.LOW_LATENCY_CAPTURE.close()


if __name__ == "__main__":
    raise SystemExit(main())
