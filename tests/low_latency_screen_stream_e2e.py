#!/usr/bin/env python3
"""Measure the real DXGI/JPEG/HTTP stream and enforce the 33 FPS floor."""

from __future__ import annotations

import socket
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lan_remote
from full_mouse_pipeline_e2e import start_embedded_controller


FRAME_COUNT = 150
WARMUP_FRAMES = 10
MAX_P95_FRAME_INTERVAL_MS = 1000 / 33


def read_headers(stream: object) -> tuple[bytes, dict[str, str]]:
    status = stream.readline().strip()
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line in (b"", b"\r\n"):
            return status, headers
        key, value = line.decode("latin1").split(":", 1)
        headers[key.lower()] = value.strip()


def main() -> int:
    lan_remote.set_dpi_awareness()
    lan_remote.configure_win32_signatures()
    server = start_embedded_controller()
    token = server.state.create_session_token({"auth_method": "temporary"})
    connection = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    connection.settimeout(5)
    stream = connection.makefile("rb")
    try:
        connection.sendall(
            (
                "GET /screen-stream?monitor=all&cursor=0&token="
                f"{token} HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        status, headers = read_headers(stream)
        if b" 200 " not in status:
            raise RuntimeError(f"screen stream failed: {status!r}")
        if not headers.get("content-type", "").startswith("multipart/x-mixed-replace"):
            raise RuntimeError(f"unexpected stream type: {headers.get('content-type')}")

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
        if p95_interval > MAX_P95_FRAME_INTERVAL_MS:
            raise RuntimeError(
                f"screen stream missed the 33 FPS floor: median={median_interval:.1f} ms "
                f"p95={p95_interval:.1f} ms"
            )
        print(
            "LOW_LATENCY_SCREEN_STREAM_OK "
            f"frames={len(arrivals)} median_fps={1000 / median_interval:.1f} "
            f"p95_fps={1000 / p95_interval:.1f} "
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
