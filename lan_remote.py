#!/usr/bin/env python3
"""
LAN-only Windows remote control MVP.

Run this on the Windows PC that you own or are explicitly allowed to control.
Open the printed LAN URL from another device on the same network and enter the
one-time access code shown in this console.
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import hmac
import html
import ipaddress
import json
import os
import platform
import secrets
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

if platform.system() == "Windows":
    from ctypes import wintypes


APP_NAME = "Windows LAN Remote"
DEFAULT_PORT = 8765
MAX_POST_BYTES = 16 * 1024
SCREEN_LOCK = threading.Lock()
GDIPLUS_LOCK = threading.Lock()
GDIPLUS_TOKEN = ctypes.c_void_p()
GDIPLUS_STARTED = False


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Windows LAN Remote</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #121417;
      --panel: #1d2229;
      --line: #343b45;
      --text: #eef2f6;
      --muted: #a8b0bb;
      --accent: #35c286;
      --danger: #ff6b6b;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--text);
      overflow: hidden;
    }

    .toolbar {
      min-height: 58px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      flex-wrap: wrap;
    }

    .brand {
      font-weight: 700;
      margin-right: 8px;
      white-space: nowrap;
    }

    input {
      width: min(280px, 52vw);
      min-height: 36px;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: #11161c;
      color: var(--text);
      padding: 8px 10px;
      outline: none;
    }

    button {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #242b34;
      color: var(--text);
      padding: 7px 12px;
      cursor: pointer;
    }

    button:hover { border-color: #596270; }
    button.primary { background: #1f6f52; border-color: #2d9b72; }
    button.danger { background: #703038; border-color: #9b4650; }
    button.active { outline: 2px solid var(--accent); }

    .status {
      margin-left: auto;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .stage {
      height: calc(100vh - 58px);
      display: grid;
      place-items: center;
      background: #080a0d;
      position: relative;
    }

    #screen {
      width: 100%;
      height: 100%;
      object-fit: contain;
      user-select: none;
      -webkit-user-drag: none;
      cursor: crosshair;
      image-rendering: auto;
    }

    .empty {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      padding: 20px;
      color: var(--muted);
      text-align: center;
      pointer-events: none;
    }

    .empty.hidden { display: none; }

    @media (max-width: 720px) {
      body { overflow: auto; }
      .toolbar { min-height: 112px; align-content: center; }
      .status { width: 100%; margin-left: 0; }
      .stage { height: calc(100vh - 112px); }
      button { padding-inline: 10px; }
    }
  </style>
</head>
<body>
  <div class="toolbar">
    <div class="brand">Windows LAN Remote</div>
    <input id="token" autocomplete="off" spellcheck="false" placeholder="输入控制端显示的访问码" />
    <button id="connect" class="primary">连接</button>
    <button id="keyboard">键盘</button>
    <button id="viewOnly">仅观看</button>
    <button id="fullscreen">全屏</button>
    <button id="disconnect" class="danger">断开</button>
    <div id="status" class="status">未连接</div>
  </div>

  <main class="stage" id="stage">
    <img id="screen" alt="远程屏幕" draggable="false" />
    <div id="empty" class="empty">在被控制电脑上启动程序，然后输入访问码。</div>
  </main>

  <script>
    const screen = document.getElementById("screen");
    const stage = document.getElementById("stage");
    const empty = document.getElementById("empty");
    const tokenInput = document.getElementById("token");
    const statusText = document.getElementById("status");
    const keyboardButton = document.getElementById("keyboard");
    const viewOnlyButton = document.getElementById("viewOnly");

    let token = "";
    let connected = false;
    let keyboardEnabled = false;
    let viewOnly = false;
    let loadingFrame = false;
    let lastMoveAt = 0;
    let lastPointer = null;
    let frameDelayMs = 160;

    function setStatus(text, danger = false) {
      statusText.textContent = text;
      statusText.style.color = danger ? "var(--danger)" : "var(--muted)";
    }

    function endpoint(path) {
      const separator = path.includes("?") ? "&" : "?";
      return `${path}${separator}token=${encodeURIComponent(token)}`;
    }

    function connect() {
      token = tokenInput.value.trim();
      if (!token) {
        setStatus("请输入访问码", true);
        return;
      }
      connected = true;
      empty.classList.add("hidden");
      setStatus("正在连接...");
      refreshScreen();
    }

    function disconnect() {
      connected = false;
      keyboardEnabled = false;
      keyboardButton.classList.remove("active");
      screen.removeAttribute("src");
      empty.classList.remove("hidden");
      setStatus("已断开");
    }

    function refreshScreen() {
      if (!connected || loadingFrame) return;
      loadingFrame = true;
      const next = new Image();
      next.onload = () => {
        screen.src = next.src;
        loadingFrame = false;
        setStatus(`已连接 · ${screen.naturalWidth || next.width}×${screen.naturalHeight || next.height}`);
        if (connected) window.setTimeout(refreshScreen, frameDelayMs);
      };
      next.onerror = () => {
        loadingFrame = false;
        setStatus("连接失败或访问码不正确", true);
        if (connected) window.setTimeout(refreshScreen, 900);
      };
      next.src = endpoint(`/screen?t=${Date.now()}`);
    }

    function remotePoint(event) {
      if (!screen.naturalWidth || !screen.naturalHeight) return null;
      const rect = screen.getBoundingClientRect();
      const scale = Math.min(rect.width / screen.naturalWidth, rect.height / screen.naturalHeight);
      const renderedWidth = screen.naturalWidth * scale;
      const renderedHeight = screen.naturalHeight * scale;
      const offsetX = (rect.width - renderedWidth) / 2;
      const offsetY = (rect.height - renderedHeight) / 2;
      const x = (event.clientX - rect.left - offsetX) / scale;
      const y = (event.clientY - rect.top - offsetY) / scale;
      if (x < 0 || y < 0 || x > screen.naturalWidth || y > screen.naturalHeight) return null;
      return { x: Math.round(x), y: Math.round(y) };
    }

    async function sendInput(payload) {
      if (!connected || viewOnly) return;
      try {
        await fetch(endpoint("/input"), {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Remote-Token": token },
          body: JSON.stringify(payload),
          cache: "no-store"
        });
      } catch {
        setStatus("输入发送失败", true);
      }
    }

    function pointerPayload(event, type) {
      const point = remotePoint(event);
      if (!point) return null;
      lastPointer = point;
      return {
        type,
        x: point.x,
        y: point.y,
        button: event.button,
        delta: event.deltaY || 0
      };
    }

    stage.addEventListener("contextmenu", (event) => event.preventDefault());

    stage.addEventListener("pointermove", (event) => {
      const now = performance.now();
      if (now - lastMoveAt < 24) return;
      lastMoveAt = now;
      const payload = pointerPayload(event, "mouse_move");
      if (payload) sendInput(payload);
    });

    stage.addEventListener("pointerdown", (event) => {
      stage.setPointerCapture(event.pointerId);
      const payload = pointerPayload(event, "mouse_down");
      if (payload) sendInput(payload);
      event.preventDefault();
    });

    stage.addEventListener("pointerup", (event) => {
      const payload = pointerPayload(event, "mouse_up");
      if (payload) sendInput(payload);
      event.preventDefault();
    });

    stage.addEventListener("wheel", (event) => {
      const payload = pointerPayload(event, "mouse_wheel");
      if (payload) sendInput(payload);
      event.preventDefault();
    }, { passive: false });

    window.addEventListener("keydown", (event) => {
      if (!connected || viewOnly || !keyboardEnabled) return;
      if (event.key === "Escape") {
        keyboardEnabled = false;
        keyboardButton.classList.remove("active");
        return;
      }
      sendInput({ type: "key_down", key: event.key, code: event.code });
      event.preventDefault();
    });

    window.addEventListener("keyup", (event) => {
      if (!connected || viewOnly || !keyboardEnabled) return;
      sendInput({ type: "key_up", key: event.key, code: event.code });
      event.preventDefault();
    });

    document.getElementById("connect").addEventListener("click", connect);
    document.getElementById("disconnect").addEventListener("click", disconnect);
    tokenInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") connect();
    });

    keyboardButton.addEventListener("click", () => {
      keyboardEnabled = !keyboardEnabled;
      keyboardButton.classList.toggle("active", keyboardEnabled);
      setStatus(keyboardEnabled ? "键盘控制已开启，按 Esc 关闭" : "键盘控制已关闭");
    });

    viewOnlyButton.addEventListener("click", () => {
      viewOnly = !viewOnly;
      viewOnlyButton.classList.toggle("active", viewOnly);
      setStatus(viewOnly ? "仅观看模式" : "控制模式");
    });

    document.getElementById("fullscreen").addEventListener("click", () => {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        stage.requestFullscreen?.();
      }
    });
  </script>
</body>
</html>
"""


@dataclass(frozen=True)
class ServerState:
    token: str
    view_only: bool
    allow_non_lan: bool
    started_at: float


def require_windows() -> None:
    if platform.system() != "Windows":
        raise SystemExit("This MVP controls Windows PCs and must be run on Windows.")


def set_dpi_awareness() -> None:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def get_lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            ips.add(sock.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    private_ips = []
    other_ips = []
    for ip in sorted(ips):
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if address.is_private:
            private_ips.append(ip)
        else:
            other_ips.append(ip)
    return private_ips + other_ips


def is_allowed_client(raw_ip: str, allow_non_lan: bool) -> bool:
    if allow_non_lan:
        return True
    try:
        address = ipaddress.ip_address(raw_ip)
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        return address.is_private or address.is_loopback or address.is_link_local
    except ValueError:
        return False


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def text_response(handler: BaseHTTPRequestHandler, status: int, payload: str, content_type: str) -> None:
    data = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class GDIPLUS_STARTUP_INPUT(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", wintypes.UINT),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


JPEG_CLSID = GUID(
    0x557CF401,
    0x1A04,
    0x11D3,
    (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E),
)


def configure_win32_signatures() -> None:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    gdiplus = ctypes.windll.gdiplus
    ole32 = ctypes.windll.ole32
    kernel32 = ctypes.windll.kernel32

    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_ulong]
    user32.mouse_event.restype = None
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_ulong]
    user32.keybd_event.restype = None
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.MapVirtualKeyW.restype = wintypes.UINT

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    gdiplus.GdiplusStartup.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(GDIPLUS_STARTUP_INPUT),
        ctypes.c_void_p,
    ]
    gdiplus.GdiplusStartup.restype = ctypes.c_uint
    gdiplus.GdiplusShutdown.argtypes = [ctypes.c_void_p]
    gdiplus.GdiplusShutdown.restype = None
    gdiplus.GdipCreateBitmapFromHBITMAP.argtypes = [wintypes.HBITMAP, wintypes.HPALETTE, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateBitmapFromHBITMAP.restype = ctypes.c_uint
    gdiplus.GdipSaveImageToStream.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
    ]
    gdiplus.GdipSaveImageToStream.restype = ctypes.c_uint
    gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDisposeImage.restype = ctypes.c_uint

    ole32.CreateStreamOnHGlobal.argtypes = [wintypes.HGLOBAL, wintypes.BOOL, ctypes.POINTER(ctypes.c_void_p)]
    ole32.CreateStreamOnHGlobal.restype = wintypes.HRESULT
    ole32.GetHGlobalFromStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.HGLOBAL)]
    ole32.GetHGlobalFromStream.restype = wintypes.HRESULT

    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL


def virtual_screen_rect() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(76)
    top = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    if width <= 0 or height <= 0:
        left = top = 0
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
    return left, top, width, height


def capture_screen_bmp() -> bytes:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    left, top, width, height = virtual_screen_rect()
    data_size = width * height * 4

    with SCREEN_LOCK:
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise ctypes.WinError()

        mem_dc = None
        bitmap = None
        previous = None
        try:
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            if not mem_dc:
                raise ctypes.WinError()

            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not bitmap:
                raise ctypes.WinError()

            previous = gdi32.SelectObject(mem_dc, bitmap)
            if not previous:
                raise ctypes.WinError()

            if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top, 0x00CC0020 | 0x40000000):
                raise ctypes.WinError()

            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0
            info.bmiHeader.biSizeImage = data_size

            pixels = ctypes.create_string_buffer(data_size)
            rows = gdi32.GetDIBits(mem_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0)
            if rows != height:
                raise ctypes.WinError()
        finally:
            if previous and mem_dc:
                gdi32.SelectObject(mem_dc, previous)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)

    file_header_size = 14
    dib_header_size = 40
    pixel_offset = file_header_size + dib_header_size
    file_size = pixel_offset + data_size
    file_header = b"BM" + struct.pack("<IHHI", file_size, 0, 0, pixel_offset)
    dib_header = struct.pack("<IiiHHIIiiII", dib_header_size, width, -height, 1, 32, 0, data_size, 0, 0, 0, 0)
    return file_header + dib_header + pixels.raw


def ensure_gdiplus_started() -> None:
    global GDIPLUS_STARTED
    if GDIPLUS_STARTED:
        return
    with GDIPLUS_LOCK:
        if GDIPLUS_STARTED:
            return
        startup_input = GDIPLUS_STARTUP_INPUT(1, None, False, False)
        status = ctypes.windll.gdiplus.GdiplusStartup(ctypes.byref(GDIPLUS_TOKEN), ctypes.byref(startup_input), None)
        if status != 0:
            raise RuntimeError(f"GDI+ startup failed with status {status}")
        GDIPLUS_STARTED = True
        atexit.register(lambda: ctypes.windll.gdiplus.GdiplusShutdown(GDIPLUS_TOKEN))


def release_com_object(ptr: ctypes.c_void_p) -> None:
    if not ptr:
        return
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    release(ptr)


def hresult_failed(hr: int) -> bool:
    return ctypes.c_long(hr).value < 0


def encode_hbitmap_jpeg(bitmap: int) -> bytes:
    ensure_gdiplus_started()
    gdiplus = ctypes.windll.gdiplus
    ole32 = ctypes.windll.ole32
    kernel32 = ctypes.windll.kernel32

    image = ctypes.c_void_p()
    status = gdiplus.GdipCreateBitmapFromHBITMAP(bitmap, None, ctypes.byref(image))
    if status != 0:
        raise RuntimeError(f"GDI+ bitmap creation failed with status {status}")

    stream = ctypes.c_void_p()
    try:
        hr = ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream))
        if hresult_failed(hr):
            raise OSError(f"CreateStreamOnHGlobal failed with HRESULT 0x{hr & 0xFFFFFFFF:08x}")

        status = gdiplus.GdipSaveImageToStream(image, stream, ctypes.byref(JPEG_CLSID), None)
        if status != 0:
            raise RuntimeError(f"GDI+ JPEG encode failed with status {status}")

        hglobal = wintypes.HGLOBAL()
        hr = ole32.GetHGlobalFromStream(stream, ctypes.byref(hglobal))
        if hresult_failed(hr):
            raise OSError(f"GetHGlobalFromStream failed with HRESULT 0x{hr & 0xFFFFFFFF:08x}")

        size = kernel32.GlobalSize(hglobal)
        data_ptr = kernel32.GlobalLock(hglobal)
        if not data_ptr or size <= 0:
            raise OSError("GlobalLock failed for encoded image")
        try:
            return ctypes.string_at(data_ptr, size)
        finally:
            kernel32.GlobalUnlock(hglobal)
    finally:
        if image:
            gdiplus.GdipDisposeImage(image)
        if stream:
            release_com_object(stream)


def capture_screen_jpeg() -> bytes:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    left, top, width, height = virtual_screen_rect()

    with SCREEN_LOCK:
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise ctypes.WinError()

        mem_dc = None
        bitmap = None
        previous = None
        try:
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            if not mem_dc:
                raise ctypes.WinError()

            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not bitmap:
                raise ctypes.WinError()

            previous = gdi32.SelectObject(mem_dc, bitmap)
            if not previous:
                raise ctypes.WinError()

            if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top, 0x00CC0020 | 0x40000000):
                raise ctypes.WinError()

            return encode_hbitmap_jpeg(bitmap)
        finally:
            if previous and mem_dc:
                gdi32.SelectObject(mem_dc, previous)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)


def capture_screen_image() -> tuple[bytes, str]:
    try:
        return capture_screen_jpeg(), "image/jpeg"
    except Exception:
        return capture_screen_bmp(), "image/bmp"


MOUSE_FLAGS = {
    ("down", 0): 0x0002,
    ("up", 0): 0x0004,
    ("down", 1): 0x0020,
    ("up", 1): 0x0040,
    ("down", 2): 0x0008,
    ("up", 2): 0x0010,
}

KEY_MAP = {
    "Backspace": 0x08,
    "Tab": 0x09,
    "Enter": 0x0D,
    "Shift": 0x10,
    "Control": 0x11,
    "Alt": 0x12,
    "Pause": 0x13,
    "CapsLock": 0x14,
    "Escape": 0x1B,
    " ": 0x20,
    "PageUp": 0x21,
    "PageDown": 0x22,
    "End": 0x23,
    "Home": 0x24,
    "ArrowLeft": 0x25,
    "ArrowUp": 0x26,
    "ArrowRight": 0x27,
    "ArrowDown": 0x28,
    "PrintScreen": 0x2C,
    "Insert": 0x2D,
    "Delete": 0x2E,
    "Meta": 0x5B,
    "ContextMenu": 0x5D,
    "NumLock": 0x90,
    "ScrollLock": 0x91,
}

for number in range(10):
    KEY_MAP[str(number)] = ord(str(number))
for codepoint in range(ord("A"), ord("Z") + 1):
    KEY_MAP[chr(codepoint).lower()] = codepoint
    KEY_MAP[chr(codepoint)] = codepoint
for index in range(1, 25):
    KEY_MAP[f"F{index}"] = 0x6F + index

CODE_MAP = {
    "Numpad0": 0x60,
    "Numpad1": 0x61,
    "Numpad2": 0x62,
    "Numpad3": 0x63,
    "Numpad4": 0x64,
    "Numpad5": 0x65,
    "Numpad6": 0x66,
    "Numpad7": 0x67,
    "Numpad8": 0x68,
    "Numpad9": 0x69,
    "NumpadMultiply": 0x6A,
    "NumpadAdd": 0x6B,
    "NumpadSubtract": 0x6D,
    "NumpadDecimal": 0x6E,
    "NumpadDivide": 0x6F,
    "Semicolon": 0xBA,
    "Equal": 0xBB,
    "Comma": 0xBC,
    "Minus": 0xBD,
    "Period": 0xBE,
    "Slash": 0xBF,
    "Backquote": 0xC0,
    "BracketLeft": 0xDB,
    "Backslash": 0xDC,
    "BracketRight": 0xDD,
    "Quote": 0xDE,
}


def key_to_vk(key: str, code: str) -> int | None:
    if code in CODE_MAP:
        return CODE_MAP[code]
    if key in KEY_MAP:
        return KEY_MAP[key]
    if len(key) == 1:
        value = ctypes.windll.user32.VkKeyScanW(ord(key))
        if value != -1:
            return value & 0xFF
    return None


def send_mouse_event(payload: dict[str, Any]) -> None:
    user32 = ctypes.windll.user32
    left, top, _, _ = virtual_screen_rect()
    x = int(payload.get("x", 0)) + left
    y = int(payload.get("y", 0)) + top
    user32.SetCursorPos(x, y)

    event_type = str(payload.get("type", ""))
    if event_type == "mouse_move":
        return
    if event_type == "mouse_wheel":
        delta = int(payload.get("delta", 0))
        wheel_delta = -120 if delta > 0 else 120
        user32.mouse_event(0x0800, 0, 0, wheel_delta, 0)
        return

    direction = "down" if event_type == "mouse_down" else "up"
    button = int(payload.get("button", 0))
    flag = MOUSE_FLAGS.get((direction, button))
    if flag:
        user32.mouse_event(flag, 0, 0, 0, 0)


def send_keyboard_event(payload: dict[str, Any]) -> None:
    user32 = ctypes.windll.user32
    event_type = str(payload.get("type", ""))
    key = str(payload.get("key", ""))
    code = str(payload.get("code", ""))
    vk = key_to_vk(key, code)
    if vk is None:
        return
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = 0x0002 if event_type == "key_up" else 0
    user32.keybd_event(vk, scan, flags, 0)


def handle_remote_input(payload: dict[str, Any]) -> None:
    input_type = str(payload.get("type", ""))
    if input_type.startswith("mouse_"):
        send_mouse_event(payload)
    elif input_type in {"key_down", "key_up"}:
        send_keyboard_event(payload)


class RemoteServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler], state: ServerState):
        super().__init__(server_address, handler)
        self.state = state


class RemoteHandler(BaseHTTPRequestHandler):
    server: RemoteServer

    def log_message(self, format: str, *args: Any) -> None:
        message = format % args
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {message}", flush=True)

    def do_GET(self) -> None:
        if not self.check_client_allowed():
            return

        parsed = urlparse(self.path)
        if parsed.path == "/":
            text_response(self, HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/screen":
            if not self.check_token(parsed):
                return
            try:
                data, content_type = capture_screen_image()
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/health":
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "app": APP_NAME,
                    "view_only": self.server.state.view_only,
                    "uptime_seconds": int(time.time() - self.server.state.started_at),
                },
            )
            return
        json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self.check_client_allowed():
            return

        parsed = urlparse(self.path)
        if parsed.path != "/input":
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_POST_BYTES:
            json_response(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "payload too large"})
            return
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid json"})
            return

        if not self.check_token(parsed, payload):
            return
        if self.server.state.view_only:
            json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "server is view-only"})
            return

        try:
            handle_remote_input(payload)
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        json_response(self, HTTPStatus.OK, {"ok": True})

    def check_client_allowed(self) -> bool:
        if is_allowed_client(self.client_address[0], self.server.state.allow_non_lan):
            return True
        json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "LAN clients only"})
        return False

    def check_token(self, parsed: Any, payload: dict[str, Any] | None = None) -> bool:
        query_token = parse_qs(parsed.query).get("token", [""])[0]
        header_token = self.headers.get("X-Remote-Token", "")
        body_token = str((payload or {}).get("token", ""))
        supplied = query_token or header_token or body_token
        if hmac.compare_digest(supplied, self.server.state.token):
            return True
        json_response(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "bad token"})
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LAN-only Windows remote control MVP")
    parser.add_argument("--host", default="0.0.0.0", help="listening host; default opens on LAN interfaces")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"listening port, default {DEFAULT_PORT}")
    parser.add_argument("--token", default="", help="custom access code; defaults to a random one-time code")
    parser.add_argument("--view-only", action="store_true", help="serve screen view without accepting input")
    parser.add_argument(
        "--allow-non-lan",
        action="store_true",
        help="allow non-private client IPs; leave off unless you understand the risk",
    )
    return parser


def print_banner(host: str, port: int, token: str, view_only: bool) -> None:
    print()
    print("=" * 64)
    print(f"{APP_NAME} is running")
    print("=" * 64)
    print("Access code:")
    print(f"  {token}")
    print()
    print("Open one of these addresses from a device on the same LAN:")
    print(f"  http://localhost:{port}")
    for ip in get_lan_ips():
        print(f"  http://{ip}:{port}")
    if host not in {"0.0.0.0", "::"}:
        print(f"  http://{host}:{port}")
    print()
    if view_only:
        print("Mode: view-only. Remote input is disabled.")
    else:
        print("Mode: control enabled after the access code is entered.")
    print("Press Ctrl+C in this window to stop the server.")
    print("=" * 64)
    print()


def main(argv: list[str] | None = None) -> int:
    require_windows()
    set_dpi_awareness()
    configure_win32_signatures()

    args = build_parser().parse_args(argv)
    token = args.token.strip() or secrets.token_urlsafe(12)
    state = ServerState(
        token=token,
        view_only=bool(args.view_only),
        allow_non_lan=bool(args.allow_non_lan),
        started_at=time.time(),
    )

    server = RemoteServer((args.host, args.port), RemoteHandler, state)
    print_banner(args.host, args.port, token, state.view_only)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
