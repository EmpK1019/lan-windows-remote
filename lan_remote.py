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
import base64
import ctypes
import hashlib
import hmac
import io
import ipaddress
import json
import os
import platform
import queue
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageTk
import webview

if platform.system() == "Windows":
    from ctypes import wintypes
    import winreg


APP_NAME = "Windows LAN Remote"
APP_VERSION = "0.6.3"
GITHUB_REPOSITORY = "EmpK1019/lan-windows-remote"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
DEFAULT_PORT = 8765
DEFAULT_DISCOVERY_PORT = 8766
SECURE_HELPER_PORT = 8767
DISCOVERY_MAGIC = "windows-lan-remote-v1"
DISCOVERY_TTL_SECONDS = 9
TEMPORARY_ACCESS_CODE_TTL_SECONDS = 30 * 60
PERMANENT_PASSWORD_ITERATIONS = 240_000
PERMANENT_PASSWORD_MIN_LENGTH = 8
ACCESS_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_POST_BYTES = 16 * 1024
REMOTE_WINDOW_HANDOFF_TTL_SECONDS = 60
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
      cursor: default !important;
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


def application_path(*parts: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root.joinpath(*parts)


def load_embedded_interface() -> str:
    path = application_path("web", "index.html")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Desktop interface is missing: {path}") from exc


INDEX_HTML = load_embedded_interface()


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi_protect(value: str) -> str:
    data = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(data)
    data_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    data_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
        ctypes.byref(data_in),
        "LAN Remote credential",
        None,
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(data_out),
    ):
        raise ctypes.WinError()
    try:
        protected = ctypes.string_at(data_out.pbData, data_out.cbData)
        return base64.b64encode(protected).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


def dpapi_unprotect(value: str) -> str:
    protected = base64.b64decode(value, validate=True)
    buffer = ctypes.create_string_buffer(protected)
    data_in = DATA_BLOB(len(protected), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    data_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(data_in),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(data_out),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


class CredentialVault:
    """Per-user DPAPI-protected credentials used only by the WebView shell."""

    def __init__(self) -> None:
        self.path = Path(os.environ.get("APPDATA", Path.home())) / "LAN Remote" / "credentials.json"
        self._lock = threading.Lock()

    @staticmethod
    def _key(kind: str, device_id: str) -> str:
        safe_id = "".join(character for character in device_id if character.isalnum() or character in "-_")[:64]
        if kind not in {"access", "lock"} or not safe_id:
            raise ValueError("invalid credential key")
        return f"{kind}:{safe_id}"

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def has_secret(self, kind: str, device_id: str) -> bool:
        key = self._key(kind, device_id)
        with self._lock:
            item = self._read().get(key)
        return isinstance(item, dict) and bool(item.get("protected"))

    def get_secret(self, kind: str, device_id: str) -> str:
        key = self._key(kind, device_id)
        with self._lock:
            item = self._read().get(key)
        if not isinstance(item, dict) or not item.get("protected"):
            return ""
        try:
            return dpapi_unprotect(str(item["protected"]))
        except (OSError, ValueError, UnicodeError):
            return ""

    def set_secret(self, kind: str, device_id: str, secret: str, device_name: str = "") -> None:
        if not secret:
            raise ValueError("secret is empty")
        key = self._key(kind, device_id)
        protected = dpapi_protect(secret)
        with self._lock:
            payload = self._read()
            payload[key] = {
                "protected": protected,
                "device_name": device_name[:128],
                "updated_at": int(time.time()),
            }
            self._write(payload)

    def remove_secret(self, kind: str, device_id: str) -> None:
        key = self._key(kind, device_id)
        with self._lock:
            payload = self._read()
            if key in payload:
                payload.pop(key, None)
                self._write(payload)


class DiscoveryRegistry:
    """Thread-safe cache of agents recently seen on the local network."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, dict[str, Any]] = {}

    def update(self, payload: dict[str, Any], source_ip: str) -> None:
        device_id = str(payload.get("id", ""))[:64]
        name = str(payload.get("name", ""))[:128]
        try:
            port = int(payload.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            return
        if not device_id or not name or not (1 <= port <= 65535):
            return
        device = {
            "id": device_id,
            "name": name,
            "ip": source_ip,
            "port": port,
            "os": str(payload.get("os", "Windows"))[:80],
            "view_only": bool(payload.get("view_only", False)),
            "last_seen": time.monotonic(),
            "is_self": False,
        }
        with self._lock:
            self._devices[device_id] = device

    def online_devices(self) -> list[dict[str, Any]]:
        cutoff = time.monotonic() - DISCOVERY_TTL_SECONDS
        with self._lock:
            expired = [key for key, value in self._devices.items() if value["last_seen"] < cutoff]
            for key in expired:
                self._devices.pop(key, None)
            devices = []
            for value in self._devices.values():
                item = dict(value)
                item.pop("last_seen", None)
                devices.append(item)
        return sorted(devices, key=lambda item: item["name"].casefold())


class SettingsStore:
    DEFAULTS: dict[str, Any] = {
        "device_name": "",
        "view_only": False,
        "discovery_enabled": True,
        "frame_delay_ms": 120,
        "remember_codes": True,
        "launch_at_login": False,
        "start_maximized": False,
        "reduce_motion": False,
        "auto_check_updates": True,
        "secure_desktop_enabled": True,
        "permanent_password_salt": "",
        "permanent_password_hash": "",
    }

    def __init__(self) -> None:
        base = Path(os.environ.get("APPDATA", Path.home())) / "LAN Remote"
        self.path = base / "settings.json"
        self.values = dict(self.DEFAULTS)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                for key in self.DEFAULTS:
                    if key in payload:
                        self.values[key] = payload[key]
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def permanent_password_is_set(self) -> bool:
        return bool(self.values.get("permanent_password_salt") and self.values.get("permanent_password_hash"))

    def set_permanent_password(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PERMANENT_PASSWORD_ITERATIONS,
        )
        self.values["permanent_password_salt"] = base64.b64encode(salt).decode("ascii")
        self.values["permanent_password_hash"] = base64.b64encode(digest).decode("ascii")

    def clear_permanent_password(self) -> None:
        self.values["permanent_password_salt"] = ""
        self.values["permanent_password_hash"] = ""

    def verify_permanent_password(self, password: str) -> bool:
        if not password or not self.permanent_password_is_set():
            return False
        try:
            salt = base64.b64decode(str(self.values["permanent_password_salt"]), validate=True)
            expected = base64.b64decode(str(self.values["permanent_password_hash"]), validate=True)
        except (ValueError, TypeError):
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PERMANENT_PASSWORD_ITERATIONS,
        )
        return hmac.compare_digest(actual, expected)

    def public_values(self, state: "ServerState") -> dict[str, Any]:
        access_code, expires_at = state.temporary_access_code()
        return {
            "device_name": state.device_name,
            "access_code": access_code,
            "access_code_expires_at": int(expires_at * 1000),
            "permanent_password_set": self.permanent_password_is_set(),
            "view_only": state.view_only,
            "discovery_enabled": bool(self.values["discovery_enabled"]),
            "frame_delay_ms": int(self.values["frame_delay_ms"]),
            "remember_codes": bool(self.values["remember_codes"]),
            "launch_at_login": startup_enabled(),
            "start_maximized": bool(self.values["start_maximized"]),
            "reduce_motion": bool(self.values["reduce_motion"]),
            "auto_check_updates": bool(self.values["auto_check_updates"]),
            "secure_desktop_enabled": bool(self.values["secure_desktop_enabled"]),
            "secure_desktop_available": secure_helper_available(),
            "app_version": APP_VERSION,
        }


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _ = winreg.QueryValueEx(key, "LAN Remote")
            return bool(value)
    except OSError:
        return False


def set_startup_enabled(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, "LAN Remote", 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, "LAN Remote")
            except FileNotFoundError:
                pass


@dataclass
class ServerState:
    token: str
    token_expires_at: float
    view_only: bool
    allow_non_lan: bool
    started_at: float
    device_id: str
    device_name: str
    port: int
    registry: DiscoveryRegistry
    settings: SettingsStore
    token_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    permanent_auth_cache: dict[str, tuple[str, float]] = field(default_factory=dict, repr=False)
    permanent_auth_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    permanent_auth_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    session_tokens: dict[str, tuple[str, float, str]] = field(default_factory=dict, repr=False)
    session_token_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    remote_window_sessions: dict[str, tuple[dict[str, Any], float]] = field(default_factory=dict, repr=False)
    remote_window_session_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def temporary_access_code(self) -> tuple[str, float]:
        with self.token_lock:
            if time.time() >= self.token_expires_at:
                self.token = generate_access_code()
                self.token_expires_at = time.time() + TEMPORARY_ACCESS_CODE_TTL_SECONDS
            return self.token, self.token_expires_at

    def rotate_temporary_access_code(self) -> tuple[str, float]:
        with self.token_lock:
            self.token = generate_access_code()
            self.token_expires_at = time.time() + TEMPORARY_ACCESS_CODE_TTL_SECONDS
            return self.token, self.token_expires_at

    def authenticate(self, supplied: str) -> dict[str, Any] | None:
        temporary_code, expires_at = self.temporary_access_code()
        now = time.time()
        password_marker = str(self.settings.values.get("permanent_password_hash", ""))
        if supplied:
            with self.session_token_lock:
                session = self.session_tokens.get(supplied)
                if session:
                    method, session_expires_at, marker = session
                    marker_valid = marker == (temporary_code if method == "temporary" else password_marker)
                    if session_expires_at > now and marker_valid:
                        credential_expiry = int(session_expires_at * 1000) if method == "temporary" else None
                        return {"auth_method": method, "credential_expires_at": credential_expiry}
                    self.session_tokens.pop(supplied, None)
        if supplied and hmac.compare_digest(supplied, temporary_code):
            return {"auth_method": "temporary", "credential_expires_at": int(expires_at * 1000)}
        if not supplied:
            return None
        cache_key = hmac.new(self.permanent_auth_secret, supplied.encode("utf-8"), hashlib.sha256).hexdigest()
        with self.permanent_auth_lock:
            cached = self.permanent_auth_cache.get(cache_key)
            if cached and cached[0] == password_marker and cached[1] > now:
                return {"auth_method": "permanent", "credential_expires_at": None}
        if self.settings.verify_permanent_password(supplied):
            with self.permanent_auth_lock:
                if len(self.permanent_auth_cache) >= 16:
                    self.permanent_auth_cache.clear()
                self.permanent_auth_cache[cache_key] = (password_marker, now + 12 * 60 * 60)
            return {"auth_method": "permanent", "credential_expires_at": None}
        return None

    def create_session_token(self, authentication: dict[str, Any]) -> str:
        method = str(authentication.get("auth_method", ""))
        if method == "temporary":
            with self.token_lock:
                marker = self.token
                expires_at = self.token_expires_at
        elif method == "permanent":
            marker = str(self.settings.values.get("permanent_password_hash", ""))
            expires_at = float("inf")
        else:
            raise ValueError("invalid authentication method")
        session_token = secrets.token_urlsafe(32)
        with self.session_token_lock:
            now = time.time()
            self.session_tokens = {
                key: value
                for key, value in self.session_tokens.items()
                if value[1] > now
            }
            self.session_tokens[session_token] = (method, expires_at, marker)
        return session_token

    def create_remote_window_session(self, payload: dict[str, Any]) -> str:
        handoff_id = secrets.token_urlsafe(24)
        now = time.time()
        with self.remote_window_session_lock:
            self.remote_window_sessions = {
                key: value
                for key, value in self.remote_window_sessions.items()
                if value[1] > now
            }
            self.remote_window_sessions[handoff_id] = (
                payload,
                now + REMOTE_WINDOW_HANDOFF_TTL_SECONDS,
            )
        return handoff_id

    def consume_remote_window_session(self, handoff_id: str) -> dict[str, Any] | None:
        if not 16 <= len(handoff_id) <= 64:
            return None
        with self.remote_window_session_lock:
            item = self.remote_window_sessions.pop(handoff_id, None)
        if item is None or item[1] <= time.time():
            return None
        return item[0]


def require_windows() -> None:
    if platform.system() != "Windows":
        raise SystemExit("This MVP controls Windows PCs and must be run on Windows.")


def set_dpi_awareness() -> None:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def get_lan_ips() -> list[str]:
    ips: list[str] = []

    def add_ip(value: str) -> None:
        if value and not value.startswith("127.") and value not in ips:
            ips.append(value)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            add_ip(sock.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add_ip(info[4][0])
    except OSError:
        pass

    private_ips = []
    other_ips = []
    for ip in ips:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if address.is_private:
            private_ips.append(ip)
        else:
            other_ips.append(ip)
    return private_ips + other_ips


def stable_device_id(device_name: str) -> str:
    seed = f"{device_name}|{uuid.getnode():012x}".encode("utf-8", "replace")
    return hashlib.sha256(seed).hexdigest()[:12]


def generate_access_code() -> str:
    """Generate a strong code that is still practical to read and type."""
    groups = ["".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "-".join(groups)


def version_key(value: str) -> tuple[int, ...]:
    """Return the numeric portion of a release tag for stable comparisons."""
    cleaned = value.strip().lower().removeprefix("v")
    numbers: list[int] = []
    for part in cleaned.split("."):
        digits = "".join(character for character in part if character.isdigit())
        numbers.append(int(digits) if digits else 0)
    return tuple((numbers + [0, 0, 0])[:3])


def latest_release() -> dict[str, Any]:
    request = Request(
        GITHUB_LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Windows-LAN-Remote/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    except HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            return {
                "ok": True,
                "current_version": APP_VERSION,
                "latest_version": APP_VERSION,
                "update_available": False,
                "installer_url": "",
                "html_url": f"https://github.com/{GITHUB_REPOSITORY}/releases",
                "message": "远端仓库暂未发布可用版本",
            }
        raise RuntimeError(f"GitHub 返回错误 {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError("暂时无法连接 GitHub，请检查网络后重试") from exc

    tag_name = str(payload.get("tag_name", APP_VERSION))
    latest_version = tag_name.removeprefix("v")
    assets = payload.get("assets", []) if isinstance(payload.get("assets"), list) else []
    installer_url = ""
    installer_digest = ""
    installer_size = 0
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        if name.lower().startswith("windowslanremotesetup-") and name.lower().endswith(".exe"):
            installer_url = str(asset.get("browser_download_url", ""))
            installer_digest = str(asset.get("digest", ""))
            try:
                installer_size = int(asset.get("size", 0) or 0)
            except (TypeError, ValueError):
                installer_size = 0
            break
    return {
        "ok": True,
        "current_version": APP_VERSION,
        "latest_version": latest_version,
        "update_available": version_key(latest_version) > version_key(APP_VERSION),
        "installer_url": installer_url,
        "installer_digest": installer_digest,
        "installer_size": installer_size,
        "html_url": str(payload.get("html_url", f"https://github.com/{GITHUB_REPOSITORY}/releases")),
        "release_notes": str(payload.get("body", ""))[:4000],
        "message": "发现新版本" if version_key(latest_version) > version_key(APP_VERSION) else "当前已是最新版本",
    }


def trusted_github_download_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "github.com" or host.endswith(".github.com") or host.endswith(".githubusercontent.com")
    )


def download_and_launch_update(release: dict[str, Any]) -> Path:
    installer_url = str(release.get("installer_url", ""))
    latest_version = str(release.get("latest_version", "update"))
    if not installer_url or not trusted_github_download_url(installer_url):
        raise RuntimeError("此版本没有可用的 Windows 安装包")

    destination = Path(tempfile.gettempdir()) / f"WindowsLANRemoteSetup-{latest_version}.exe"
    expected_digest = str(release.get("installer_digest", "")).strip().lower()
    try:
        asset_size = int(release.get("installer_size", 0) or 0)
    except (TypeError, ValueError):
        asset_size = 0
    if asset_size > 250 * 1024 * 1024:
        raise RuntimeError("安装包大小异常")
    request = Request(
        installer_url,
        headers={"User-Agent": f"Windows-LAN-Remote/{APP_VERSION}", "Accept": "application/octet-stream"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            if not trusted_github_download_url(final_url):
                raise RuntimeError("GitHub 下载跳转到了不受信任的地址")
            expected = int(response.headers.get("Content-Length", "0") or "0")
            if expected > 250 * 1024 * 1024:
                raise RuntimeError("安装包大小异常")
            total = 0
            digest = hashlib.sha256()
            with destination.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 250 * 1024 * 1024:
                        raise RuntimeError("安装包大小异常")
                    digest.update(chunk)
                    stream.write(chunk)
            if total < 64 * 1024 or (expected and total != expected):
                raise RuntimeError("安装包下载不完整")
            if asset_size and total != asset_size:
                raise RuntimeError("安装包大小与 GitHub 发布信息不一致")
            if expected_digest.startswith("sha256:") and not hmac.compare_digest(
                f"sha256:{digest.hexdigest()}",
                expected_digest,
            ):
                raise RuntimeError("安装包 SHA-256 校验失败")
    except RuntimeError:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("安装包下载失败，请稍后重试") from exc

    try:
        # The bootstrapper starts as a normal process and requests UAC itself.
        # This lets the HTTP handler return before the installer stops the old app.
        subprocess.Popen([str(destination), "--from-update"], close_fds=True)
    except OSError as exc:
        raise RuntimeError("安装程序无法启动，请从下载目录手动运行安装包") from exc
    return destination


class DiscoveryService:
    """Advertise this agent and listen for other agents over UDP broadcast."""

    def __init__(
        self,
        registry: DiscoveryRegistry,
        device_id: str,
        device_name: str,
        service_port: int,
        discovery_port: int,
        view_only: bool,
        enabled: bool = True,
    ) -> None:
        self.registry = registry
        self.device_id = device_id
        self.device_name = device_name
        self.service_port = service_port
        self.discovery_port = discovery_port
        self.view_only = view_only
        self.enabled = enabled
        self.state_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        for name, target in (("listener", self._listen), ("advertiser", self._advertise)):
            thread = threading.Thread(target=target, name=f"lan-remote-discovery-{name}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        self.stop_event.set()

    def set_enabled(self, enabled: bool) -> None:
        with self.state_lock:
            self.enabled = enabled

    def update_identity(self, device_name: str, view_only: bool) -> None:
        with self.state_lock:
            self.device_name = device_name
            self.view_only = view_only

    def _listen(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", self.discovery_port))
            sock.settimeout(1.0)
            while not self.stop_event.is_set():
                try:
                    raw, address = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict) or payload.get("magic") != DISCOVERY_MAGIC:
                    continue
                if payload.get("id") == self.device_id:
                    continue
                with self.state_lock:
                    enabled = self.enabled
                if enabled and is_allowed_client(address[0], allow_non_lan=False):
                    self.registry.update(payload, address[0])
        except OSError as exc:
            print(f"Discovery listener unavailable on UDP {self.discovery_port}: {exc}", flush=True)
        finally:
            sock.close()

    def _advertise(self) -> None:
        targets = {"255.255.255.255"}
        for ip in get_lan_ips():
            parts = ip.split(".")
            if len(parts) == 4:
                targets.add(".".join((*parts[:3], "255")))

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while not self.stop_event.is_set():
                with self.state_lock:
                    enabled = self.enabled
                    device_name = self.device_name
                    view_only = self.view_only
                if enabled:
                    payload = json.dumps(
                        {
                            "magic": DISCOVERY_MAGIC,
                            "id": self.device_id,
                            "name": device_name,
                            "port": self.service_port,
                            "os": f"Windows {platform.release()}",
                            "view_only": view_only,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    for target in targets:
                        try:
                            sock.sendto(payload, (target, self.discovery_port))
                        except OSError:
                            continue
                self.stop_event.wait(2.0)
        finally:
            sock.close()


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


def is_local_machine_client(raw_ip: str) -> bool:
    try:
        address = ipaddress.ip_address(raw_ip)
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        if address.is_loopback:
            return True
        return str(address) in get_lan_ips()
    except ValueError:
        return False


def send_common_headers(handler: BaseHTTPRequestHandler, allow_cross_origin: bool = True) -> None:
    if allow_cross_origin:
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Remote-Token")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")


def json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: dict[str, Any],
    allow_cross_origin: bool = True,
) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    send_common_headers(handler, allow_cross_origin=allow_cross_origin)
    handler.end_headers()
    handler.wfile.write(data)


def text_response(handler: BaseHTTPRequestHandler, status: int, payload: str, content_type: str) -> None:
    data = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    send_common_headers(handler)
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


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", INPUT_UNION)]


class WTSINFOEX_LEVEL1(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * 33),
        ("UserName", wintypes.WCHAR * 21),
        ("DomainName", wintypes.WCHAR * 18),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    ]


class WTSINFOEX_LEVEL(ctypes.Union):
    _fields_ = [("Level1", WTSINFOEX_LEVEL1)]


class WTSINFOEX(ctypes.Structure):
    _fields_ = [("Level", wintypes.DWORD), ("Data", WTSINFOEX_LEVEL)]


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
    wtsapi32 = ctypes.windll.wtsapi32

    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
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
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetUserObjectInformationW.restype = wintypes.BOOL
    user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user32.GetThreadDesktop.restype = wintypes.HANDLE
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    wtsapi32.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wtsapi32.WTSFreeMemory.restype = None

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


def service_secret_path() -> Path:
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return program_data / "Windows LAN Remote" / "service-token.txt"


def read_service_secret(path: str | Path | None = None) -> str:
    target = Path(path) if path else service_secret_path()
    try:
        return target.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def secure_desktop_active() -> bool:
    """Return True when the input desktop is Winlogon/UAC rather than Default."""
    user32 = ctypes.windll.user32
    desktop = user32.OpenInputDesktop(0, False, 0x0001)
    if not desktop:
        return True
    try:
        required = wintypes.DWORD()
        user32.GetUserObjectInformationW(desktop, 2, None, 0, ctypes.byref(required))
        if required.value <= 2:
            return False
        buffer = ctypes.create_unicode_buffer(required.value // ctypes.sizeof(wintypes.WCHAR) + 1)
        if not user32.GetUserObjectInformationW(
            desktop,
            2,
            buffer,
            ctypes.sizeof(buffer),
            ctypes.byref(required),
        ):
            return True
        return buffer.value.casefold() != "default"
    finally:
        user32.CloseDesktop(desktop)


def current_thread_desktop_name() -> str:
    user32 = ctypes.windll.user32
    thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    desktop = user32.GetThreadDesktop(thread_id)
    if not desktop:
        return "unknown"
    required = wintypes.DWORD()
    user32.GetUserObjectInformationW(desktop, 2, None, 0, ctypes.byref(required))
    if required.value <= 2:
        return "unknown"
    buffer = ctypes.create_unicode_buffer(required.value // ctypes.sizeof(wintypes.WCHAR) + 1)
    if not user32.GetUserObjectInformationW(desktop, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(required)):
        return "unknown"
    return buffer.value


def current_session_locked() -> bool:
    """Return the lock state of the current interactive Windows session."""
    kernel32 = ctypes.windll.kernel32
    wtsapi32 = ctypes.windll.wtsapi32
    session_id = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session_id)):
        return False
    buffer = ctypes.c_void_p()
    returned = wintypes.DWORD()
    # WTSSessionInfoEx = 25. Its level-1 SessionFlags value is 0 when locked
    # and 1 when unlocked on supported Windows 10/11 systems.
    if not wtsapi32.WTSQuerySessionInformationW(
        None,
        session_id.value,
        25,
        ctypes.byref(buffer),
        ctypes.byref(returned),
    ):
        return False
    try:
        if returned.value < ctypes.sizeof(WTSINFOEX):
            return False
        info = ctypes.cast(buffer, ctypes.POINTER(WTSINFOEX)).contents
        return info.Level == 1 and info.Data.Level1.SessionFlags == 0
    finally:
        wtsapi32.WTSFreeMemory(buffer)


def secure_helper_request(
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 4.0,
) -> tuple[bytes, str]:
    secret = read_service_secret()
    if not secret:
        raise RuntimeError("安全桌面服务尚未安装")
    data = None
    headers = {"X-Secure-Token": secret}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = Request(
        f"http://127.0.0.1:{SECURE_HELPER_PORT}{path}",
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read(32 * 1024 * 1024), response.headers.get("Content-Type", "application/octet-stream")
    except HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error", "安全桌面服务拒绝请求")
        except Exception:
            message = "安全桌面服务拒绝请求"
        raise RuntimeError(str(message)) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("安全桌面服务未就绪，请修复或重新安装软件") from exc


def secure_helper_available() -> bool:
    try:
        data, _ = secure_helper_request("/secure/health", timeout=0.8)
        return bool(json.loads(data.decode("utf-8")).get("ok"))
    except (RuntimeError, ValueError, UnicodeDecodeError):
        return False


def capture_secure_desktop() -> tuple[bytes, str]:
    return secure_helper_request("/secure/screen")


def send_secure_input(payload: dict[str, Any]) -> None:
    secure_helper_request("/secure/input", payload=payload, timeout=2.0)


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


def send_key_press(payload: dict[str, Any]) -> None:
    key_payload = {
        "key": str(payload.get("key", "")),
        "code": str(payload.get("code", "")),
    }
    send_keyboard_event({**key_payload, "type": "key_down"})
    time.sleep(0.035)
    send_keyboard_event({**key_payload, "type": "key_up"})


def send_unicode_text(text: str) -> None:
    if not text or len(text) > 256:
        raise ValueError("text input length is invalid")
    encoded = text.encode("utf-16-le")
    code_units = [int.from_bytes(encoded[index:index + 2], "little") for index in range(0, len(encoded), 2)]
    inputs: list[INPUT] = []
    for code_unit in code_units:
        inputs.append(INPUT(type=1, ki=KEYBDINPUT(0, code_unit, 0x0004, 0, 0)))
        inputs.append(INPUT(type=1, ki=KEYBDINPUT(0, code_unit, 0x0004 | 0x0002, 0, 0)))
    array_type = INPUT * len(inputs)
    sent = ctypes.windll.user32.SendInput(len(inputs), array_type(*inputs), ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise ctypes.WinError()


def handle_remote_input(payload: dict[str, Any]) -> None:
    input_type = str(payload.get("type", ""))
    if input_type.startswith("mouse_"):
        send_mouse_event(payload)
    elif input_type in {"key_down", "key_up"}:
        send_keyboard_event(payload)
    elif input_type == "key_press":
        send_key_press(payload)
    elif input_type == "text":
        send_unicode_text(str(payload.get("text", "")))


@dataclass(frozen=True)
class SecureDesktopState:
    secret: str


class SecureDesktopServer(HTTPServer):

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        state: SecureDesktopState,
    ) -> None:
        super().__init__(server_address, handler)
        self.state = state


class SecureDesktopHandler(BaseHTTPRequestHandler):
    server: SecureDesktopServer
    server_version = "LANRemoteSecure/0.5"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def authorized(self) -> bool:
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "loopback only"}, False)
            return False
        supplied = self.headers.get("X-Secure-Token", "")
        if not hmac.compare_digest(supplied, self.server.state.secret):
            json_response(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "bad service token"}, False)
            return False
        return True

    def do_GET(self) -> None:
        if not self.authorized():
            return
        path = urlparse(self.path).path
        if path == "/secure/health":
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "desktop": current_thread_desktop_name(),
                    "secure_input_active": secure_desktop_active(),
                },
                False,
            )
            return
        if path != "/secure/screen":
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"}, False)
            return
        try:
            data, content_type = capture_screen_image()
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)}, False)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if not self.authorized():
            return
        if urlparse(self.path).path != "/secure/input":
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"}, False)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_POST_BYTES:
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid payload"}, False)
            return
        try:
            payload = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("object required")
            handle_remote_input(payload)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}, False)
            return
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)}, False)
            return
        json_response(self, HTTPStatus.OK, {"ok": True}, False)


def run_secure_desktop_helper(port: int, secret_file: str) -> int:
    secret = read_service_secret(secret_file)
    if len(secret) < 32:
        return 2
    try:
        server = SecureDesktopServer(("127.0.0.1", port), SecureDesktopHandler, SecureDesktopState(secret))
    except OSError:
        return 3
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0


class RemoteServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler], state: ServerState):
        super().__init__(server_address, handler)
        self.state = state
        self.discovery_service: DiscoveryService | None = None


class DesktopApi:
    """Native window and DPAPI credential bridge for the WebView shell."""

    def __init__(
        self,
        maximized: bool = False,
        remote_window: bool = False,
    ) -> None:
        self.window: Any = None
        self.fullscreen = False
        self.maximized = maximized
        self.remote_window = remote_window
        self.vault = CredentialVault()
        self._unlock_attempts: set[str] = set()
        self._unlock_lock = threading.Lock()

    def _defer_window_action(self, action: Any) -> None:
        """Run native window calls after the current JS bridge call returns.

        Calling a WinForms Window method synchronously from a pywebview JS API
        handler can deadlock: WebView2's UI thread waits for the API result while
        the Python handler uses Control.Invoke to wait for that same UI thread.
        """
        window = self.window
        if window is None:
            return

        def invoke() -> None:
            time.sleep(0.05)
            try:
                action(window)
            except Exception:
                pass

        threading.Thread(
            target=invoke,
            name="lan-remote-window-action",
            daemon=True,
        ).start()

    def toggle_fullscreen(self) -> bool:
        if self.window is None:
            return self.fullscreen
        self.fullscreen = not self.fullscreen
        self._defer_window_action(lambda window: window.toggle_fullscreen())
        return self.fullscreen

    def minimize_window(self) -> bool:
        self._defer_window_action(lambda window: window.minimize())
        return True

    def toggle_maximize_window(self) -> bool:
        if self.window is None:
            return self.maximized
        if self.maximized:
            self._defer_window_action(lambda window: window.restore())
        else:
            self._defer_window_action(lambda window: window.maximize())
        self.maximized = not self.maximized
        return self.maximized

    def close_window(self) -> bool:
        self._defer_window_action(lambda window: window.destroy())
        return True

    def window_state(self) -> dict[str, bool]:
        return {
            "maximized": self.maximized,
            "fullscreen": self.fullscreen,
            "remote_window": self.remote_window,
        }

    def set_window_title(self, title: str) -> bool:
        safe_title = str(title).strip()[:160] or "LAN Remote"
        self._defer_window_action(lambda window: window.set_title(safe_title))
        return True

    def credential_status(self, device_id: str) -> dict[str, bool]:
        return {
            "access_saved": self.vault.has_secret("access", device_id),
            "lock_saved": self.vault.has_secret("lock", device_id),
        }

    def load_access_password(self, device_id: str) -> str:
        return self.vault.get_secret("access", device_id)

    def save_access_password(self, device_id: str, password: str, device_name: str = "") -> bool:
        self.vault.set_secret("access", device_id, password, device_name)
        return True

    def clear_access_password(self, device_id: str) -> bool:
        self.vault.remove_secret("access", device_id)
        return True

    def save_lock_password(self, device_id: str, password: str, device_name: str = "") -> bool:
        if not 1 <= len(password) <= 128:
            raise ValueError("锁屏密码长度无效")
        self.vault.set_secret("lock", device_id, password, device_name)
        with self._unlock_lock:
            self._unlock_attempts.discard(device_id)
        return True

    def clear_lock_password(self, device_id: str) -> bool:
        self.vault.remove_secret("lock", device_id)
        with self._unlock_lock:
            self._unlock_attempts.discard(device_id)
        return True

    @staticmethod
    def _validated_device(device_json: str) -> dict[str, Any]:
        device = json.loads(device_json)
        if not isinstance(device, dict):
            raise ValueError("设备信息无效")
        device_id = str(device.get("id", ""))[:64]
        host = str(device.get("ip", ""))
        port = int(device.get("port", 0))
        if not device_id or not is_allowed_client(host, False) or not 1 <= port <= 65535:
            raise ValueError("设备地址不在受信任的局域网范围内")
        return {"id": device_id, "ip": host, "port": port}

    @staticmethod
    def _remote_json(
        device: dict[str, Any],
        path: str,
        access_password: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        method = "GET"
        headers = {"X-Remote-Token": access_password, "User-Agent": f"Windows-LAN-Remote/{APP_VERSION}"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = Request(target_url(device, path), data=data, method=method, headers=headers)
        with urlopen(request, timeout=4) as response:
            result = json.loads(response.read(256 * 1024).decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("远端响应无效")
        return result

    def try_auto_unlock(self, device_json: str, access_password: str) -> dict[str, Any]:
        try:
            device = self._validated_device(device_json)
            status = self._remote_json(device, "/api/session-status", access_password)
            device_id = str(device["id"])
            if not status.get("session_locked"):
                with self._unlock_lock:
                    self._unlock_attempts.discard(device_id)
                return {"ok": True, "status": "not_locked"}
            lock_password = self.vault.get_secret("lock", device_id)
            if not lock_password:
                return {"ok": True, "status": "no_credential"}
            with self._unlock_lock:
                if device_id in self._unlock_attempts:
                    return {"ok": True, "status": "already_attempted"}
                self._unlock_attempts.add(device_id)
            self._remote_json(device, "/input", access_password, {"type": "key_press", "key": "Enter", "code": "Enter"})
            time.sleep(0.65)
            self._remote_json(device, "/input", access_password, {"type": "text", "text": lock_password})
            time.sleep(0.08)
            self._remote_json(device, "/input", access_password, {"type": "key_press", "key": "Enter", "code": "Enter"})
            return {"ok": True, "status": "submitted"}
        except HTTPError as exc:
            return {"ok": False, "status": "access_denied" if exc.code == 401 else "remote_error", "error": str(exc)}
        except (OSError, ValueError, URLError, TimeoutError) as exc:
            return {"ok": False, "status": "error", "error": str(exc)}


def run_webview_shell(
    url: str,
    maximized: bool,
    remote_window: bool = False,
) -> int:
    """Run WebView2 outside the HTTP/control process.

    Python.NET's Windows message loop can hold the interpreter lock on some
    runtime combinations. Keeping WebView in a separate process ensures LAN
    discovery, screen streaming and the local API remain responsive.
    """
    desktop_api = DesktopApi(
        maximized=maximized,
        remote_window=remote_window,
    )
    window = webview.create_window(
        "LAN Remote · 远程控制" if remote_window else "LAN Remote",
        url,
        js_api=desktop_api,
        width=1280 if remote_window else 1200,
        height=800 if remote_window else 760,
        min_size=(720, 480) if remote_window else (920, 600),
        resizable=True,
        background_color="#0f1014",
        text_select=False,
        zoomable=False,
        maximized=maximized,
        frameless=True,
        easy_drag=False,
        shadow=True,
    )
    desktop_api.window = window
    try:
        start_options: dict[str, Any] = {
            "gui": "edgechromium",
            "debug": False,
            "private_mode": True,
            "icon": str(application_path("assets", "lan-remote-icon.ico")),
        }
        if remote_window:
            # A bundled control window is much quicker to initialize with a
            # reusable WebView2 cache. Keep it isolated from the main host:
            # sharing one folder across separate Python.NET processes can lock
            # both native message loops. Remote credentials never enter browser
            # storage; they remain protected by Windows DPAPI.
            start_options["private_mode"] = False
            start_options["storage_path"] = str(
                Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LAN Remote" / "WebView2-remote"
            )
        webview.start(**start_options)
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"无法启动软件界面。请确认 Microsoft Edge WebView2 Runtime 已安装。\n\n{exc}",
            "LAN Remote 无法启动",
            0x10,
        )
        return 1
    return 0


def webview_shell_command(
    url: str,
    maximized: bool,
    remote_window: bool = False,
) -> list[str]:
    arguments = ["--ui-shell", "--ui-url", url]
    if maximized:
        arguments.append("--ui-maximized")
    if remote_window:
        arguments.append("--ui-remote")
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    return [sys.executable, str(Path(__file__).resolve()), *arguments]


def normalize_remote_window_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_device = payload.get("device")
    if not isinstance(raw_device, dict):
        raise ValueError("设备信息无效")
    validated = DesktopApi._validated_device(json.dumps(raw_device, ensure_ascii=False))
    token = str(payload.get("token", ""))
    if not 1 <= len(token) <= 512:
        raise ValueError("远程会话令牌无效")
    auth_method = str(payload.get("authMethod", "temporary"))
    if auth_method not in {"temporary", "permanent"}:
        raise ValueError("远程认证方式无效")
    expires_at = payload.get("credentialExpiresAt")
    if expires_at is not None:
        try:
            expires_at = int(expires_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("远程凭据到期时间无效") from exc
    device = {
        **validated,
        "name": str(raw_device.get("name", "远程桌面"))[:128],
        "os": str(raw_device.get("os", "Windows"))[:80],
        "view_only": bool(raw_device.get("view_only", False)),
        "is_self": bool(raw_device.get("is_self", False)),
    }
    return {
        "device": device,
        "token": token,
        "viewOnly": bool(payload.get("viewOnly", False)),
        "authMethod": auth_method,
        "credentialExpiresAt": expires_at,
    }


def launch_remote_window(local_port: int, handoff_id: str) -> int:
    process = subprocess.Popen(
        webview_shell_command(
            f"http://127.0.0.1:{local_port}/?remote=1&handoff={quote(handoff_id)}&v={quote(APP_VERSION)}",
            False,
            remote_window=True,
        ),
        cwd=str(application_path()),
        close_fds=True,
    )
    return int(process.pid)


class RemoteHandler(BaseHTTPRequestHandler):
    server: RemoteServer
    server_version = "LANRemote/0.2"

    def log_message(self, format: str, *args: Any) -> None:
        # Screen polling and input are both high-frequency. Screen URLs also
        # carry the one-time token, so never echo those request paths.
        if urlparse(self.path).path in {"/screen", "/input"}:
            return
        # A PyInstaller windowed application deliberately has no stdout.
        # Attempting to print here raises from send_response() and leaves the
        # desktop UI waiting for headers that are never sent.
        if sys.stdout is not None:
            message = format % args
            print(f"[{self.log_date_time_string()}] {self.client_address[0]} {message}", flush=True)

    def do_OPTIONS(self) -> None:
        if not self.check_client_allowed():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        send_common_headers(self)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self.check_client_allowed():
            return

        parsed = urlparse(self.path)
        if parsed.path == "/":
            if not is_local_machine_client(self.client_address[0]):
                json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "local desktop only"})
                return
            text_response(self, HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/app-icon.png":
            if not is_local_machine_client(self.client_address[0]):
                json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "local desktop only"})
                return
            try:
                icon_data = application_path("assets", "lan-remote-icon.png").read_bytes()
            except OSError:
                json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "icon not found"})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(icon_data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(icon_data)
            return
        if parsed.path == "/api/devices":
            ips = get_lan_ips()
            local_device = {
                "id": self.server.state.device_id,
                "name": self.server.state.device_name,
                "ip": ips[0] if ips else "127.0.0.1",
                "port": self.server.state.port,
                "os": f"Windows {platform.release()}",
                "view_only": self.server.state.view_only,
                "is_self": True,
            }
            devices = [local_device, *self.server.state.registry.online_devices()]
            json_response(self, HTTPStatus.OK, {"ok": True, "devices": devices})
            return
        if parsed.path == "/api/local-access-code":
            if not is_local_machine_client(self.client_address[0]):
                json_response(
                    self,
                    HTTPStatus.FORBIDDEN,
                    {"ok": False, "error": "access code is only available on this computer"},
                    allow_cross_origin=False,
                )
                return
            access_code, expires_at = self.server.state.temporary_access_code()
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "name": self.server.state.device_name,
                    "access_code": access_code,
                    "access_code_expires_at": int(expires_at * 1000),
                },
                allow_cross_origin=False,
            )
            return
        if parsed.path == "/api/remote-window/session":
            if not self.check_local_desktop():
                return
            handoff_id = parse_qs(parsed.query).get("id", [""])[0]
            payload = self.server.state.consume_remote_window_session(handoff_id)
            if payload is None:
                json_response(
                    self,
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "远程窗口会话已失效"},
                    allow_cross_origin=False,
                )
                return
            json_response(
                self,
                HTTPStatus.OK,
                {"ok": True, "session": payload},
                allow_cross_origin=False,
            )
            return
        if parsed.path == "/api/settings":
            if not self.check_local_desktop():
                return
            json_response(
                self,
                HTTPStatus.OK,
                {"ok": True, "settings": self.server.state.settings.public_values(self.server.state)},
                allow_cross_origin=False,
            )
            return
        if parsed.path == "/api/update":
            if not self.check_local_desktop():
                return
            try:
                result = latest_release()
            except RuntimeError as exc:
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"ok": False, "error": str(exc), "current_version": APP_VERSION},
                    allow_cross_origin=False,
                )
                return
            json_response(self, HTTPStatus.OK, result, allow_cross_origin=False)
            return
        if parsed.path == "/api/session-status":
            authentication = self.authenticate_request(parsed)
            if authentication is None:
                return
            secure_active = secure_desktop_active()
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "secure_desktop_active": secure_active,
                    "session_locked": bool(secure_active and current_session_locked()),
                    **authentication,
                },
            )
            return
        if parsed.path in {"/api/info", "/health"}:
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "app": APP_NAME,
                    "version": APP_VERSION,
                    "id": self.server.state.device_id,
                    "name": self.server.state.device_name,
                    "port": self.server.state.port,
                    "os": f"Windows {platform.release()}",
                    "view_only": self.server.state.view_only,
                    "uptime_seconds": int(time.time() - self.server.state.started_at),
                },
            )
            return
        if parsed.path == "/screen":
            if self.authenticate_request(parsed) is None:
                return
            try:
                if secure_desktop_active():
                    if not self.server.state.settings.values["secure_desktop_enabled"]:
                        json_response(self, HTTPStatus.LOCKED, {"ok": False, "error": "secure desktop control disabled"})
                        return
                    data, content_type = capture_secure_desktop()
                else:
                    data, content_type = capture_screen_image()
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            send_common_headers(self)
            self.end_headers()
            self.wfile.write(data)
            return
        json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self.check_client_allowed():
            return

        parsed = urlparse(self.path)
        if parsed.path == "/api/remote-window/open":
            if not self.check_local_desktop():
                return
            payload = self.read_json_payload()
            if payload is None:
                return
            handoff_id = ""
            try:
                normalized = normalize_remote_window_payload(payload)
                handoff_id = self.server.state.create_remote_window_session(normalized)
                process_id = launch_remote_window(self.server.state.port, handoff_id)
            except (OSError, ValueError) as exc:
                if handoff_id:
                    self.server.state.consume_remote_window_session(handoff_id)
                json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": str(exc)},
                    allow_cross_origin=False,
                )
                return
            json_response(
                self,
                HTTPStatus.OK,
                {"ok": True, "process_id": process_id},
                allow_cross_origin=False,
            )
            return
        if parsed.path == "/api/settings":
            if not self.check_local_desktop():
                return
            payload = self.read_json_payload()
            if payload is None:
                return
            self.update_settings(payload)
            return
        if parsed.path == "/api/update/install":
            if not self.check_local_desktop():
                return
            try:
                release = latest_release()
                if not release.get("update_available"):
                    json_response(
                        self,
                        HTTPStatus.CONFLICT,
                        {"ok": False, "error": "当前已是最新版本"},
                        allow_cross_origin=False,
                    )
                    return
                download_and_launch_update(release)
            except RuntimeError as exc:
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"ok": False, "error": str(exc)},
                    allow_cross_origin=False,
                )
                return
            json_response(
                self,
                HTTPStatus.OK,
                {"ok": True, "message": "安装包已校验，安装程序已启动，请在 UAC 窗口选择“是”"},
                allow_cross_origin=False,
            )
            return
        if parsed.path == "/api/verify":
            authentication = self.authenticate_request(parsed)
            if authentication is None:
                return
            session_token = self.server.state.create_session_token(authentication)
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "view_only": self.server.state.view_only,
                    "session_token": session_token,
                    **authentication,
                },
            )
            return
        if parsed.path != "/input":
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return

        payload = self.read_json_payload()
        if payload is None:
            return

        if self.authenticate_request(parsed, payload) is None:
            return
        if self.server.state.view_only:
            json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "server is view-only"})
            return

        try:
            if secure_desktop_active():
                if not self.server.state.settings.values["secure_desktop_enabled"]:
                    json_response(self, HTTPStatus.LOCKED, {"ok": False, "error": "secure desktop control disabled"})
                    return
                send_secure_input(payload)
            else:
                handle_remote_input(payload)
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        json_response(self, HTTPStatus.OK, {"ok": True})

    def read_json_payload(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid content length"})
            return None
        if length < 0 or length > MAX_POST_BYTES:
            json_response(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "payload too large"})
            return None
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid json"})
            return None
        if not isinstance(payload, dict):
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "json object required"})
            return None
        return payload

    def update_settings(self, payload: dict[str, Any]) -> None:
        state = self.server.state
        values = state.settings.values
        device_name = str(payload.get("device_name", state.device_name)).strip()
        if not 1 <= len(device_name) <= 64:
            json_response(
                self,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "设备名必须为 1 到 64 个字符"},
                allow_cross_origin=False,
            )
            return

        frame_delay = payload.get("frame_delay_ms", values["frame_delay_ms"])
        try:
            frame_delay = int(frame_delay)
        except (TypeError, ValueError):
            frame_delay = -1
        if frame_delay not in {80, 120, 220}:
            json_response(
                self,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "画面刷新速度无效"},
                allow_cross_origin=False,
            )
            return

        boolean_keys = {
            "view_only",
            "discovery_enabled",
            "remember_codes",
            "launch_at_login",
            "start_maximized",
            "reduce_motion",
            "auto_check_updates",
            "secure_desktop_enabled",
        }
        for key in boolean_keys:
            if key in payload and not isinstance(payload[key], bool):
                json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": f"设置项 {key} 的值无效"},
                    allow_cross_origin=False,
                )
                return

        permanent_password = payload.get("permanent_password")
        if permanent_password is not None:
            if not isinstance(permanent_password, str) or not PERMANENT_PASSWORD_MIN_LENGTH <= len(permanent_password) <= 128:
                json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": f"永久访问密码必须为 {PERMANENT_PASSWORD_MIN_LENGTH} 到 128 个字符"},
                    allow_cross_origin=False,
                )
                return
        if payload.get("clear_permanent_password") not in {None, False, True}:
            json_response(
                self,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "清除永久密码参数无效"},
                allow_cross_origin=False,
            )
            return
        if permanent_password is not None and payload.get("clear_permanent_password") is True:
            json_response(
                self,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "不能同时设置和清除永久密码"},
                allow_cross_origin=False,
            )
            return

        state.device_name = device_name
        state.view_only = bool(payload.get("view_only", state.view_only))
        values["device_name"] = device_name
        values["view_only"] = state.view_only
        values["frame_delay_ms"] = frame_delay
        for key in boolean_keys - {"view_only", "launch_at_login"}:
            if key in payload:
                values[key] = payload[key]
        launch_at_login = bool(payload.get("launch_at_login", startup_enabled()))
        try:
            set_startup_enabled(launch_at_login)
            values["launch_at_login"] = launch_at_login
            if payload.get("regenerate_access_code") is True:
                state.rotate_temporary_access_code()
            if permanent_password is not None:
                state.settings.set_permanent_password(permanent_password)
            elif payload.get("clear_permanent_password") is True:
                state.settings.clear_permanent_password()
            if self.server.discovery_service:
                self.server.discovery_service.update_identity(state.device_name, state.view_only)
                self.server.discovery_service.set_enabled(bool(values["discovery_enabled"]))
            state.settings.save()
        except OSError as exc:
            json_response(
                self,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"保存设置失败：{exc}"},
                allow_cross_origin=False,
            )
            return
        json_response(
            self,
            HTTPStatus.OK,
            {"ok": True, "settings": state.settings.public_values(state)},
            allow_cross_origin=False,
        )

    def check_local_desktop(self) -> bool:
        if is_local_machine_client(self.client_address[0]):
            return True
        json_response(
            self,
            HTTPStatus.FORBIDDEN,
            {"ok": False, "error": "local desktop only"},
            allow_cross_origin=False,
        )
        return False

    def check_client_allowed(self) -> bool:
        if is_allowed_client(self.client_address[0], self.server.state.allow_non_lan):
            return True
        json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "LAN clients only"})
        return False

    def authenticate_request(self, parsed: Any, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        query_token = parse_qs(parsed.query).get("token", [""])[0]
        header_token = self.headers.get("X-Remote-Token", "")
        body_token = str((payload or {}).get("token", ""))
        supplied = query_token or header_token or body_token
        authentication = self.server.state.authenticate(supplied)
        if authentication is not None:
            return authentication
        json_response(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "bad token"})
        return None


def target_url(device: dict[str, Any], path: str) -> str:
    host = str(device["ip"])
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{int(device['port'])}{path}"


def verify_remote_device(device: dict[str, Any], token: str) -> dict[str, Any]:
    request = Request(
        target_url(device, "/api/verify"),
        data=b"",
        method="POST",
        headers={"X-Remote-Token": token},
    )
    try:
        with urlopen(request, timeout=4) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == HTTPStatus.UNAUTHORIZED:
            raise ValueError("访问码不正确") from exc
        if exc.code == HTTPStatus.FORBIDDEN:
            raise ValueError("目标电脑拒绝了本次连接") from exc
        raise ValueError(f"目标电脑返回错误：{exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError("无法连接目标电脑，请检查防火墙和网络") from exc


def enable_dark_title_bar(window: tk.Misc) -> None:
    """Ask Windows 10/11 to render the native title bar in dark mode."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                break
    except Exception:
        pass


class RemoteSessionWindow(tk.Toplevel):
    KEY_NAMES = {
        "BackSpace": "Backspace",
        "Return": "Enter",
        "Shift_L": "Shift",
        "Shift_R": "Shift",
        "Control_L": "Control",
        "Control_R": "Control",
        "Alt_L": "Alt",
        "Alt_R": "Alt",
        "Win_L": "Meta",
        "Win_R": "Meta",
        "Escape": "Escape",
        "space": " ",
        "Prior": "PageUp",
        "Next": "PageDown",
        "Left": "ArrowLeft",
        "Right": "ArrowRight",
        "Up": "ArrowUp",
        "Down": "ArrowDown",
        "Print": "PrintScreen",
        "Menu": "ContextMenu",
    }

    def __init__(self, owner: "RemoteDesktopApp", device: dict[str, Any], token: str, view_only: bool) -> None:
        super().__init__(owner.root)
        self.owner = owner
        self.device = dict(device)
        self.token = token
        self.view_only = view_only
        self.active = True
        self.keyboard_enabled = False
        self.fullscreen_enabled = False
        self.last_move_at = 0.0
        self.latest_lock = threading.Lock()
        self.latest_image: Image.Image | None = None
        self.last_rendered_image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.render_box = (0, 0, 0, 0)
        self.screen_size = (0, 0)
        self.render_dirty = True
        self.input_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=256)

        self.title(f"{device['name']} - LAN Remote")
        self.geometry("1180x760")
        self.minsize(760, 500)
        self.configure(bg="#08090c")
        self.protocol("WM_DELETE_WINDOW", self.close)

        toolbar = tk.Frame(self, bg="#181a20", height=54)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        tk.Label(
            toolbar,
            text=f"{device['name']} · {'桌面观看' if view_only else '桌面控制'}",
            bg="#181a20",
            fg="#f3f4f7",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side="left", padx=(15, 18))

        if not view_only:
            self.keyboard_button = tk.Button(
                toolbar,
                text="键盘控制：关",
                command=self.toggle_keyboard,
                bg="#262930",
                fg="#e6e8ed",
                activebackground="#30343d",
                activeforeground="white",
                relief="flat",
                padx=12,
                cursor="hand2",
            )
            self.keyboard_button.pack(side="left", padx=4, pady=10)

        tk.Button(
            toolbar,
            text="全屏",
            command=self.toggle_fullscreen,
            bg="#262930",
            fg="#e6e8ed",
            activebackground="#30343d",
            activeforeground="white",
            relief="flat",
            padx=12,
            cursor="hand2",
        ).pack(side="left", padx=4, pady=10)
        tk.Button(
            toolbar,
            text="断开",
            command=self.close,
            bg="#513039",
            fg="#ffdce1",
            activebackground="#663844",
            activeforeground="white",
            relief="flat",
            padx=12,
            cursor="hand2",
        ).pack(side="left", padx=4, pady=10)

        self.status_var = tk.StringVar(value="正在连接…")
        tk.Label(
            toolbar,
            textvariable=self.status_var,
            bg="#181a20",
            fg="#9ca0ab",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right", padx=15)

        self.canvas = tk.Canvas(self, bg="#050609", highlightthickness=0, cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self.hint_id = self.canvas.create_text(
            0,
            0,
            text="正在获取远程画面…",
            fill="#777b86",
            font=("Microsoft YaHei UI", 11),
        )
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<ButtonPress-1>", lambda event: self._mouse_button(event, "mouse_down", 0))
        self.canvas.bind("<ButtonRelease-1>", lambda event: self._mouse_button(event, "mouse_up", 0))
        self.canvas.bind("<ButtonPress-2>", lambda event: self._mouse_button(event, "mouse_down", 1))
        self.canvas.bind("<ButtonRelease-2>", lambda event: self._mouse_button(event, "mouse_up", 1))
        self.canvas.bind("<ButtonPress-3>", lambda event: self._mouse_button(event, "mouse_down", 2))
        self.canvas.bind("<ButtonRelease-3>", lambda event: self._mouse_button(event, "mouse_up", 2))
        self.canvas.bind("<Motion>", self._mouse_move)
        self.canvas.bind("<MouseWheel>", self._mouse_wheel)
        self.bind("<KeyPress>", self._key_down)
        self.bind("<KeyRelease>", self._key_up)

        threading.Thread(target=self._frame_loop, name="lan-remote-frames", daemon=True).start()
        if not view_only:
            threading.Thread(target=self._input_loop, name="lan-remote-input", daemon=True).start()
        self.after(40, self._render_tick)
        self.after(0, lambda: enable_dark_title_bar(self))

    def _safe_status(self, text: str) -> None:
        if not self.active:
            return
        try:
            self.after(0, lambda value=text: self.status_var.set(value) if self.active else None)
        except tk.TclError:
            pass

    def _frame_loop(self) -> None:
        endpoint = target_url(self.device, f"/screen?token={quote(self.token, safe='')}")
        while self.active:
            try:
                request = Request(endpoint, headers={"Cache-Control": "no-cache"})
                with urlopen(request, timeout=6) as response:
                    data = response.read()
                image = Image.open(io.BytesIO(data)).convert("RGB")
                image.load()
                with self.latest_lock:
                    self.latest_image = image
                self._safe_status(f"已连接 · {image.width} × {image.height}")
                time.sleep(0.04)
            except HTTPError as exc:
                if exc.code == HTTPStatus.UNAUTHORIZED:
                    self._safe_status("访问码已失效")
                    break
                self._safe_status(f"画面请求失败：{exc.code}")
                time.sleep(0.8)
            except Exception:
                self._safe_status("连接中断，正在重试…")
                time.sleep(0.8)

    def _render_tick(self) -> None:
        if not self.active:
            return
        with self.latest_lock:
            image = self.latest_image
        if image is not None and (image is not self.last_rendered_image or self.render_dirty):
            self._render_image(image)
            self.last_rendered_image = image
            self.render_dirty = False
        try:
            self.after(40, self._render_tick)
        except tk.TclError:
            pass

    def _render_image(self, image: Image.Image) -> None:
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        scale = min(width / image.width, height / image.height)
        render_width = max(1, int(image.width * scale))
        render_height = max(1, int(image.height * scale))
        rendered = image.resize((render_width, render_height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(rendered)
        left = (width - render_width) // 2
        top = (height - render_height) // 2
        self.canvas.delete("remote-frame")
        self.canvas.create_image(left, top, anchor="nw", image=self.photo, tags="remote-frame")
        self.canvas.tag_lower("remote-frame")
        self.canvas.itemconfigure(self.hint_id, state="hidden")
        self.render_box = (left, top, render_width, render_height)
        self.screen_size = (image.width, image.height)

    def _on_resize(self, event: tk.Event[Any]) -> None:
        self.canvas.coords(self.hint_id, event.width // 2, event.height // 2)
        self.render_dirty = True

    def _remote_point(self, event: tk.Event[Any]) -> tuple[int, int] | None:
        left, top, width, height = self.render_box
        screen_width, screen_height = self.screen_size
        if width <= 0 or height <= 0 or screen_width <= 0 or screen_height <= 0:
            return None
        if not (left <= event.x <= left + width and top <= event.y <= top + height):
            return None
        x = round((event.x - left) * screen_width / width)
        y = round((event.y - top) * screen_height / height)
        return max(0, min(x, screen_width - 1)), max(0, min(y, screen_height - 1))

    def _queue_input(self, payload: dict[str, Any]) -> None:
        if self.view_only or not self.active:
            return
        try:
            self.input_queue.put_nowait(payload)
        except queue.Full:
            if payload.get("type") != "mouse_move":
                self._safe_status("控制指令队列繁忙")

    def _mouse_move(self, event: tk.Event[Any]) -> None:
        now = time.monotonic()
        if now - self.last_move_at < 0.028:
            return
        self.last_move_at = now
        point = self._remote_point(event)
        if point:
            self._queue_input({"type": "mouse_move", "x": point[0], "y": point[1], "button": 0})

    def _mouse_button(self, event: tk.Event[Any], event_type: str, button: int) -> str:
        self.canvas.focus_set()
        point = self._remote_point(event)
        if point:
            self._queue_input({"type": event_type, "x": point[0], "y": point[1], "button": button})
        return "break"

    def _mouse_wheel(self, event: tk.Event[Any]) -> str:
        point = self._remote_point(event)
        if point:
            self._queue_input(
                {"type": "mouse_wheel", "x": point[0], "y": point[1], "button": 0, "delta": -event.delta}
            )
        return "break"

    def _keyboard_payload(self, event: tk.Event[Any], event_type: str) -> dict[str, str] | None:
        if not self.keyboard_enabled or self.view_only:
            return None
        key = self.KEY_NAMES.get(event.keysym)
        if key is None:
            if event.keysym.startswith("F") and event.keysym[1:].isdigit():
                key = event.keysym
            elif event.char:
                key = event.char
            else:
                key = event.keysym
        return {"type": event_type, "key": key, "code": ""}

    def _key_down(self, event: tk.Event[Any]) -> str | None:
        if self.keyboard_enabled and event.keysym == "Escape":
            self.toggle_keyboard()
            return "break"
        payload = self._keyboard_payload(event, "key_down")
        if payload:
            self._queue_input(payload)
            return "break"
        return None

    def _key_up(self, event: tk.Event[Any]) -> str | None:
        payload = self._keyboard_payload(event, "key_up")
        if payload:
            self._queue_input(payload)
            return "break"
        return None

    def _input_loop(self) -> None:
        endpoint = target_url(self.device, f"/input?token={quote(self.token, safe='')}")
        while self.active:
            try:
                payload = self.input_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if payload is None:
                break
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request = Request(
                endpoint,
                data=raw,
                method="POST",
                headers={"Content-Type": "application/json", "X-Remote-Token": self.token},
            )
            try:
                with urlopen(request, timeout=3) as response:
                    response.read()
            except Exception:
                self._safe_status("控制指令发送失败")

    def toggle_keyboard(self) -> None:
        if self.view_only:
            return
        self.keyboard_enabled = not self.keyboard_enabled
        self.keyboard_button.configure(
            text=f"键盘控制：{'开' if self.keyboard_enabled else '关'}",
            bg="#176b4d" if self.keyboard_enabled else "#262930",
        )
        if self.keyboard_enabled:
            self.canvas.focus_set()
            self.status_var.set("键盘控制已开启，按 Esc 关闭")

    def toggle_fullscreen(self) -> None:
        self.fullscreen_enabled = not self.fullscreen_enabled
        self.attributes("-fullscreen", self.fullscreen_enabled)

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            self.input_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass


class RemoteDesktopApp:
    BG = "#101116"
    PANEL = "#17191f"
    PANEL_2 = "#20232a"
    LINE = "#2b2e36"
    TEXT = "#f1f2f5"
    MUTED = "#9498a4"
    ACCENT = "#176b4d"
    ACCENT_HOVER = "#21835f"
    GREEN = "#32d583"

    def __init__(
        self,
        root: tk.Tk,
        server: RemoteServer,
        state: ServerState,
        discovery: DiscoveryService | None,
    ) -> None:
        self.root = root
        self.server = server
        self.state = state
        self.discovery = discovery
        self.devices: dict[str, dict[str, Any]] = {}
        self.selected_id = ""
        self.saved_tokens: dict[str, str] = {}
        self.connecting = False
        self.closing = False
        self.search_has_placeholder = True

        root.title("LAN Remote")
        root.geometry("1120x700")
        root.minsize(920, 580)
        root.configure(bg=self.BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_styles()
        self._build_ui()
        self.root.after(0, lambda: enable_dark_title_bar(self.root))
        self.refresh_devices()

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Remote.Treeview",
            background="#121318",
            fieldbackground="#121318",
            foreground="#dde0e7",
            rowheight=46,
            borderwidth=0,
            bordercolor=self.LINE,
            lightcolor=self.LINE,
            darkcolor=self.LINE,
            font=("Microsoft YaHei UI", 10),
        )
        style.map(
            "Remote.Treeview",
            background=[("selected", "#205742")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Remote.Treeview.Heading",
            background="#17191f",
            foreground="#8e929e",
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
        )
        style.map("Remote.Treeview.Heading", background=[("active", "#17191f")])
        style.configure(
            "Remote.Vertical.TScrollbar",
            background="#2a2d34",
            troughcolor="#14161b",
            bordercolor="#14161b",
            arrowcolor="#a4a8b1",
            lightcolor="#2a2d34",
            darkcolor="#2a2d34",
        )

    def _build_ui(self) -> None:
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

        top = tk.Frame(self.root, bg="#15171c", height=62, highlightbackground=self.LINE, highlightthickness=1)
        top.grid(row=0, column=0, columnspan=3, sticky="nsew")
        top.grid_propagate(False)
        tk.Label(
            top,
            text="★  LAN Remote",
            bg="#15171c",
            fg=self.TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(side="left", padx=18)
        search_box = tk.Frame(top, bg="#252832")
        search_box.pack(side="left", padx=28, pady=12)
        tk.Label(search_box, text="⌕", bg="#252832", fg="#858a97", font=("Segoe UI Symbol", 17)).pack(
            side="left", padx=(10, 2)
        )
        self.search_var = tk.StringVar()
        search = tk.Entry(
            search_box,
            textvariable=self.search_var,
            width=24,
            bg="#252832",
            fg="#858a97",
            insertbackground="white",
            relief="flat",
            font=("Microsoft YaHei UI", 10),
        )
        search.pack(side="left", padx=(0, 10), pady=8)
        self.search_entry = search
        search.insert(0, "搜索局域网设备")
        search.bind("<FocusIn>", self._search_focus_in)
        search.bind("<FocusOut>", self._search_focus_out)
        search.bind("<KeyRelease>", lambda _event: self._render_devices())
        self.online_var = tk.StringVar(value="正在发现设备…")
        tk.Label(
            top,
            textvariable=self.online_var,
            bg="#15171c",
            fg="#63a98e",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right", padx=18)

        sidebar = tk.Frame(self.root, bg="#15171c", width=240, highlightbackground=self.LINE, highlightthickness=1)
        sidebar.grid(row=1, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        tk.Label(
            sidebar,
            text="设备",
            bg="#15171c",
            fg=self.TEXT,
            font=("Microsoft YaHei UI", 15, "bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(22, 14))
        tk.Label(
            sidebar,
            text="▣  全部设备",
            bg="#1f513f",
            fg="#ffffff",
            font=("Microsoft YaHei UI", 10),
            anchor="w",
            padx=14,
            pady=10,
        ).pack(fill="x", padx=12)

        access = tk.Frame(sidebar, bg="#193329", highlightbackground="#28664f", highlightthickness=1)
        access.pack(fill="x", padx=12, pady=(24, 0))
        tk.Label(
            access,
            text="本机访问码",
            bg="#193329",
            fg="#a9d7c5",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(11, 2))
        tk.Label(
            access,
            text=self.state.token,
            bg="#193329",
            fg="#ffffff",
            font=("Cascadia Mono", 9, "bold"),
            anchor="w",
            wraplength=195,
        ).pack(fill="x", padx=12, pady=4)
        tk.Label(
            access,
            text="另一台电脑连接本机时输入此码",
            bg="#193329",
            fg="#829d92",
            font=("Microsoft YaHei UI", 8),
            justify="left",
            wraplength=195,
        ).pack(fill="x", padx=12, pady=(2, 8))
        tk.Button(
            access,
            text="复制访问码",
            command=self.copy_access_code,
            bg=self.ACCENT,
            fg="#ffffff",
            activebackground=self.ACCENT_HOVER,
            activeforeground="white",
            relief="flat",
            cursor="hand2",
        ).pack(fill="x", padx=12, pady=(0, 12))

        tk.Label(
            sidebar,
            text="两台电脑都运行 LAN Remote 后会自动出现在列表中。",
            bg="#15171c",
            fg="#6f7480",
            font=("Microsoft YaHei UI", 8),
            justify="left",
            wraplength=170,
        ).pack(fill="x", padx=20, pady=24)

        center = tk.Frame(self.root, bg="#111217", highlightbackground=self.LINE, highlightthickness=1)
        center.grid(row=1, column=1, sticky="nsew")
        center.grid_rowconfigure(1, weight=1)
        center.grid_columnconfigure(0, weight=1)
        center_head = tk.Frame(center, bg="#17191f", height=54)
        center_head.grid(row=0, column=0, sticky="ew")
        center_head.grid_propagate(False)
        tk.Label(
            center_head,
            text="全部设备",
            bg="#17191f",
            fg=self.TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side="left", padx=16)
        tk.Button(
            center_head,
            text="刷新",
            command=self.refresh_devices,
            bg="#272a31",
            fg="#d8dbe2",
            activebackground="#343840",
            activeforeground="white",
            relief="flat",
            padx=12,
            cursor="hand2",
        ).pack(side="right", padx=12, pady=10)

        tree_frame = tk.Frame(center, bg="#111217")
        tree_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_frame,
            style="Remote.Treeview",
            columns=("name", "address", "status"),
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("name", text="设备名", anchor="w")
        self.tree.heading("address", text="地址", anchor="w")
        self.tree.heading("status", text="状态", anchor="w")
        self.tree.column("name", width=230, minwidth=150, stretch=True)
        self.tree.column("address", width=170, minwidth=130, stretch=False)
        self.tree.column("status", width=80, minwidth=70, stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(
            tree_frame,
            style="Remote.Vertical.TScrollbar",
            orient="vertical",
            command=self.tree.yview,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_selection)
        self.tree.bind("<Double-1>", lambda _event: self.connect_selected(False))

        details = tk.Frame(self.root, bg="#17191f", width=300, highlightbackground=self.LINE, highlightthickness=1)
        details.grid(row=1, column=2, sticky="nsew")
        details.grid_propagate(False)
        self.detail_name = tk.StringVar(value="选择一台设备")
        self.detail_subtitle = tk.StringVar(value="")
        tk.Label(
            details,
            textvariable=self.detail_name,
            bg="#17191f",
            fg=self.TEXT,
            font=("Microsoft YaHei UI", 12, "bold"),
            anchor="w",
            wraplength=260,
        ).pack(fill="x", padx=18, pady=(24, 2))
        tk.Label(
            details,
            textvariable=self.detail_subtitle,
            bg="#17191f",
            fg=self.MUTED,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(fill="x", padx=18, pady=(0, 20))

        self.control_button = tk.Button(
            details,
            text="桌面控制",
            command=lambda: self.connect_selected(False),
            bg=self.ACCENT,
            fg="white",
            activebackground=self.ACCENT_HOVER,
            activeforeground="white",
            relief="flat",
            font=("Microsoft YaHei UI", 10),
            pady=13,
            state="disabled",
            cursor="hand2",
        )
        self.control_button.pack(fill="x", padx=18, pady=5)
        self.view_button = tk.Button(
            details,
            text="桌面观看",
            command=lambda: self.connect_selected(True),
            bg="#282b32",
            fg="#e1e3e8",
            activebackground="#343840",
            activeforeground="white",
            relief="flat",
            font=("Microsoft YaHei UI", 10),
            pady=13,
            state="disabled",
            cursor="hand2",
        )
        self.view_button.pack(fill="x", padx=18, pady=5)

        info = tk.Frame(details, bg="#1e2026", highlightbackground="#30333b", highlightthickness=1)
        info.pack(fill="x", padx=18, pady=(20, 0))
        self.detail_address = tk.StringVar(value="-")
        self.detail_id = tk.StringVar(value="-")
        self.detail_os = tk.StringVar(value="-")
        self.detail_mode = tk.StringVar(value="-")
        for label, variable in (
            ("设备地址", self.detail_address),
            ("设备 ID", self.detail_id),
            ("系统", self.detail_os),
            ("控制权限", self.detail_mode),
        ):
            row = tk.Frame(info, bg="#1e2026")
            row.pack(fill="x", padx=12, pady=6)
            tk.Label(row, text=label, bg="#1e2026", fg="#777c88", font=("Microsoft YaHei UI", 8)).pack(
                side="left"
            )
            tk.Label(
                row,
                textvariable=variable,
                bg="#1e2026",
                fg="#c6c9d0",
                font=("Microsoft YaHei UI", 7),
                wraplength=175,
                justify="right",
            ).pack(side="right")

        self.connection_status = tk.StringVar(value="")
        tk.Label(
            details,
            textvariable=self.connection_status,
            bg="#17191f",
            fg="#ff8293",
            font=("Microsoft YaHei UI", 8),
            wraplength=260,
            justify="left",
        ).pack(fill="x", padx=18, pady=14)

    def current_devices(self) -> list[dict[str, Any]]:
        ips = get_lan_ips()
        local = {
            "id": self.state.device_id,
            "name": self.state.device_name,
            "ip": ips[0] if ips else "127.0.0.1",
            "port": self.state.port,
            "os": f"Windows {platform.release()}",
            "view_only": self.state.view_only,
            "is_self": True,
        }
        return [local, *self.state.registry.online_devices()]

    def _search_focus_in(self, _event: tk.Event[Any]) -> None:
        if self.search_has_placeholder:
            self.search_entry.delete(0, "end")
            self.search_entry.configure(fg=self.TEXT)
            self.search_has_placeholder = False

    def _search_focus_out(self, _event: tk.Event[Any]) -> None:
        if not self.search_entry.get().strip():
            self.search_entry.insert(0, "搜索局域网设备")
            self.search_entry.configure(fg="#858a97")
            self.search_has_placeholder = True
            self._render_devices()

    def refresh_devices(self) -> None:
        if self.closing:
            return
        devices = self.current_devices()
        self.devices = {str(device["id"]): device for device in devices}
        if not self.selected_id or self.selected_id not in self.devices:
            remote = next((device for device in devices if not device.get("is_self")), None)
            self.selected_id = str((remote or devices[0])["id"]) if devices else ""
        self.online_var.set(f"● 发现 {len(devices)} 台在线设备")
        self._render_devices()
        self.root.after(2500, self.refresh_devices)

    def _render_devices(self) -> None:
        query = "" if self.search_has_placeholder else self.search_var.get().strip().casefold()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for device_id, device in self.devices.items():
            if query and query not in f"{device['name']} {device['ip']}".casefold():
                continue
            name = f"▣  {device['name']}{'  [本机]' if device.get('is_self') else ''}"
            self.tree.insert(
                "",
                "end",
                iid=device_id,
                values=(name, f"{device['ip']}:{device['port']}", "● 在线"),
            )
        if self.selected_id in self.tree.get_children():
            self.tree.selection_set(self.selected_id)
            self.tree.focus(self.selected_id)
        self._show_details()

    def _on_selection(self, _event: tk.Event[Any]) -> None:
        selection = self.tree.selection()
        if selection:
            self.selected_id = selection[0]
            self.connection_status.set("")
            self._show_details()

    def _show_details(self) -> None:
        device = self.devices.get(self.selected_id)
        if not device:
            self.detail_name.set("选择一台设备")
            self.detail_subtitle.set("")
            self.control_button.configure(state="disabled")
            self.view_button.configure(state="disabled")
            return
        self.detail_name.set(str(device["name"]))
        self.detail_subtitle.set("本机 · 在线" if device.get("is_self") else "局域网设备 · 在线")
        self.detail_address.set(f"{device['ip']}:{device['port']}")
        self.detail_id.set(str(device["id"]))
        self.detail_os.set(str(device.get("os", "Windows")))
        self.detail_mode.set("仅允许观看" if device.get("view_only") else "允许控制")
        self.control_button.configure(
            state="disabled" if device.get("view_only") or self.connecting else "normal"
        )
        self.view_button.configure(state="disabled" if self.connecting else "normal")

    def connect_selected(self, view_only: bool) -> None:
        device = self.devices.get(self.selected_id)
        if not device:
            return
        if not view_only and device.get("view_only"):
            messagebox.showinfo("LAN Remote", "目标电脑当前只允许桌面观看。", parent=self.root)
            return
        initial = self.saved_tokens.get(self.selected_id, self.state.token if device.get("is_self") else "")
        token = simpledialog.askstring(
            "连接设备",
            f"输入 {device['name']} 软件左侧显示的“本机访问码”：",
            parent=self.root,
            show="*",
            initialvalue=initial,
        )
        if token is None:
            return
        token = token.strip()
        if not token:
            messagebox.showwarning("LAN Remote", "请输入访问码。", parent=self.root)
            return
        self.connecting = True
        self.connection_status.set("正在验证访问码…")
        self.control_button.configure(state="disabled")
        self.view_button.configure(state="disabled")

        def worker() -> None:
            try:
                result = verify_remote_device(device, token)
                if not view_only and result.get("view_only"):
                    raise ValueError("目标电脑当前只允许观看")
                self.root.after(0, lambda: self._connect_success(device, token, view_only))
            except Exception as exc:
                message = str(exc)
                self.root.after(0, lambda value=message: self._connect_failed(value))

        threading.Thread(target=worker, name="lan-remote-verify", daemon=True).start()

    def _connect_success(self, device: dict[str, Any], token: str, view_only: bool) -> None:
        if self.closing:
            return
        self.connecting = False
        self.saved_tokens[str(device["id"])] = token
        self.connection_status.set("")
        self._show_details()
        RemoteSessionWindow(self, device, token, view_only)

    def _connect_failed(self, message: str) -> None:
        if self.closing:
            return
        self.connecting = False
        self._show_details()
        self.connection_status.set(message)
        messagebox.showerror("连接失败", message, parent=self.root)

    def copy_access_code(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.state.token)
        self.root.update_idletasks()
        messagebox.showinfo("LAN Remote", "本机访问码已复制。", parent=self.root)

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        if self.discovery:
            self.discovery.stop()
        try:
            self.server.shutdown()
            self.server.server_close()
        except OSError:
            pass
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LAN-only Windows remote desktop")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认开放给局域网")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP 端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT, help="UDP 设备发现端口")
    parser.add_argument("--name", default="", help="在设备列表中显示的名称，默认使用计算机名")
    parser.add_argument("--token", default="", help="固定访问码，留空则每次启动随机生成")
    parser.add_argument("--view-only", action="store_true", help="只允许远程观看，不接受控制输入")
    parser.add_argument("--no-discovery", action="store_true", help="关闭局域网 UDP 自动发现")
    parser.add_argument(
        "--allow-non-lan",
        action="store_true",
        help="允许非局域网地址访问（不建议）",
    )
    parser.add_argument("--secure-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--secure-port", type=int, default=SECURE_HELPER_PORT, help=argparse.SUPPRESS)
    parser.add_argument("--secure-secret-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--ui-shell", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-url", default="", help=argparse.SUPPRESS)
    parser.add_argument("--ui-maximized", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-remote", action="store_true", help=argparse.SUPPRESS)
    return parser


def print_banner(host: str, port: int, token: str, view_only: bool, discovery_enabled: bool) -> None:
    print()
    print("=" * 64)
    print(f"{APP_NAME} 已启动")
    print("=" * 64)
    print("本次访问码：")
    print(f"  {token}")
    print()
    print("在同一局域网的浏览器中打开：")
    print(f"  http://localhost:{port}")
    for ip in get_lan_ips():
        print(f"  http://{ip}:{port}")
    if host not in {"0.0.0.0", "::"}:
        print(f"  http://{host}:{port}")
    print()
    if view_only:
        print("当前模式：仅观看，远程输入已禁用。")
    else:
        print("当前模式：输入访问码后允许远程控制。")
    print(f"设备发现：{'已开启' if discovery_enabled else '已关闭'}")
    print("关闭此窗口或按 Ctrl+C 即可停止服务。")
    print("=" * 64)
    print()


def main(argv: list[str] | None = None) -> int:
    require_windows()
    set_dpi_awareness()
    configure_win32_signatures()

    args = build_parser().parse_args(argv)
    if args.secure_helper:
        return run_secure_desktop_helper(args.secure_port, args.secure_secret_file)
    if args.ui_shell:
        if not args.ui_url.startswith("http://127.0.0.1:"):
            return 2
        return run_webview_shell(args.ui_url, args.ui_maximized, args.ui_remote)
    if not (1 <= args.port <= 65535):
        raise SystemExit("TCP 端口必须在 1 到 65535 之间。")
    if not (1 <= args.discovery_port <= 65535):
        raise SystemExit("UDP 发现端口必须在 1 到 65535 之间。")

    settings = SettingsStore()
    token = args.token.strip() or generate_access_code()
    device_name = args.name.strip() or str(settings.values["device_name"]).strip() or socket.gethostname()
    view_only = bool(args.view_only or settings.values["view_only"])
    registry = DiscoveryRegistry()
    state = ServerState(
        token=token,
        token_expires_at=time.time() + TEMPORARY_ACCESS_CODE_TTL_SECONDS,
        view_only=view_only,
        allow_non_lan=bool(args.allow_non_lan),
        started_at=time.time(),
        device_id=stable_device_id(device_name),
        device_name=device_name,
        port=args.port,
        registry=registry,
        settings=settings,
    )

    try:
        server = RemoteServer((args.host, args.port), RemoteHandler, state)
    except OSError as exc:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"无法使用端口 {args.port}。请先关闭旧版 LAN Remote，或检查端口是否被占用。\n\n{exc}",
            "LAN Remote 无法启动",
            0x10,
        )
        return 1

    discovery: DiscoveryService | None = None
    if not args.no_discovery:
        discovery = DiscoveryService(
            registry=registry,
            device_id=state.device_id,
            device_name=state.device_name,
            service_port=state.port,
            discovery_port=args.discovery_port,
            view_only=state.view_only,
            enabled=bool(settings.values["discovery_enabled"]),
        )
        discovery.start()
        server.discovery_service = discovery

    server_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="lan-remote-server",
        daemon=True,
    )
    server_thread.start()

    ui_process: subprocess.Popen[Any] | None = None
    try:
        ui_process = subprocess.Popen(
            webview_shell_command(
                f"http://127.0.0.1:{args.port}/?v={quote(APP_VERSION)}",
                bool(settings.values["start_maximized"]),
            ),
            cwd=str(application_path()),
        )
        return int(ui_process.wait())
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"无法启动软件界面。请确认 Microsoft Edge WebView2 Runtime 已安装。\n\n{exc}",
            "LAN Remote 无法启动",
            0x10,
        )
        return 1
    finally:
        if ui_process is not None and ui_process.poll() is None:
            ui_process.terminate()
        if discovery:
            discovery.stop()
        try:
            server.shutdown()
            server.server_close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
