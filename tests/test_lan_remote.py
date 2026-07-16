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
from unittest.mock import Mock, patch

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
            {"type": "mouse_wheel", "x": 12, "y": 18, "delta": -120},
            {"type": "key_down", "key": "a", "code": "KeyA"},
            {"type": "key_up", "key": "Shift", "code": "ShiftLeft"},
            {"type": "text", "text": "Hello 中文"},
        ]
        for payload in valid:
            lan_remote.validate_remote_input_payload(payload)

        invalid = [
            {"type": "mouse_unknown", "x": 0, "y": 0},
            {"type": "mouse_down", "x": 0, "y": 0, "button": 9},
            {"type": "mouse_move", "x": "left", "y": 0},
            {"type": "key_down", "key": "", "code": ""},
            {"type": "text", "text": ""},
            {"type": "text", "text": "x" * 257},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                lan_remote.validate_remote_input_payload(payload)


class SettingsAndAuthenticationTests(unittest.TestCase):
    def test_saved_settings_and_permanent_password_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APPDATA": directory}):
            settings = lan_remote.SettingsStore()
            settings.values["device_name"] = "Persistent device"
            settings.values["view_only"] = True
            settings.values["auto_install_updates"] = False
            settings.set_permanent_password("persistent password value")
            settings.save()

            reloaded = lan_remote.SettingsStore()
            self.assertEqual(reloaded.values["device_name"], "Persistent device")
            self.assertIs(reloaded.values["view_only"], True)
            self.assertIs(reloaded.values["auto_install_updates"], False)
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
                        "auto_check_updates": 1,
                        "auto_install_updates": "yes",
                        "permanent_password_salt": [],
                        "permanent_password_hash": "partial",
                    }
                ),
                encoding="utf-8",
            )
            settings = lan_remote.SettingsStore()
            self.assertEqual(settings.values["device_name"], "")
            self.assertIs(settings.values["view_only"], False)
            self.assertEqual(settings.values["frame_delay_ms"], 120)
            self.assertIs(settings.values["auto_check_updates"], True)
            self.assertIs(settings.values["auto_install_updates"], True)
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

        received: list[dict[str, object]] = []
        with (
            patch.object(lan_remote, "secure_desktop_active", return_value=False),
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

    def test_invalid_input_monitor_and_view_only_are_rejected(self) -> None:
        headers = {"X-Remote-Token": "TEST-TEMP-CODE", "Content-Type": "application/json"}
        status, _, _ = self.request(
            "POST",
            "/input",
            body=json.dumps({"type": "mouse_unknown", "x": 0, "y": 0}).encode("utf-8"),
            headers=headers,
        )
        self.assertEqual(status, 400)

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
            {"device_name": "Renamed", "frame_delay_ms": 80, "auto_install_updates": False}
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
        set_startup.assert_called_once_with(False)

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
