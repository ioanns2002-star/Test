"""Portable tests for the Windows-only DPAPI configuration boundary."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from agentbridge import windows_config
from agentbridge.tray import AgentBridgeTray


VALID_TOKEN = "d" * 32
VALID_RELAY_URL = "wss://agentbridge.example.herokuapp.com"


def _fake_protect(plaintext: bytes) -> bytes:
    return b"test-dpapi:" + base64.urlsafe_b64encode(plaintext)


def _fake_unprotect(ciphertext: bytes) -> bytes:
    marker = b"test-dpapi:"
    if not ciphertext.startswith(marker):
        raise windows_config.DpapiError("Test DPAPI value cannot be decrypted.")
    try:
        return base64.urlsafe_b64decode(ciphertext[len(marker) :])
    except ValueError as error:
        raise windows_config.DpapiError("Test DPAPI value cannot be decrypted.") from error


class WindowsConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = windows_config.WindowsConfigStore(Path(self.temporary_directory.name) / "AgentBridge")
        self.config = windows_config.DesktopConfig(
            relay_url=VALID_RELAY_URL,
            token=VALID_TOKEN,
            peer_key=Fernet.generate_key().decode("ascii"),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def fake_windows_dpapi(self):
        return patch.multiple(
            windows_config,
            is_windows=lambda: True,
            dpapi_protect=_fake_protect,
            dpapi_unprotect=_fake_unprotect,
        )

    def test_store_round_trip_keeps_secrets_out_of_the_json_document(self) -> None:
        with self.fake_windows_dpapi():
            self.store.save(self.config)
            raw_document = self.store.config_path.read_text(encoding="utf-8")
            document = json.loads(raw_document)
            loaded = self.store.load()

        self.assertEqual(document["version"], windows_config.CONFIG_VERSION)
        self.assertEqual(document["relay_url"], VALID_RELAY_URL)
        self.assertIn("device_token_dpapi", document)
        self.assertIn("peer_key_dpapi", document)
        self.assertNotIn(self.config.token, raw_document)
        self.assertNotIn(self.config.peer_key, raw_document)
        self.assertEqual(loaded.as_agent_config(), self.config.as_agent_config())
        self.assertNotIn(self.config.token, repr(loaded))
        self.assertNotIn(self.config.peer_key, repr(loaded))

    def test_store_rejects_corrupt_protected_value(self) -> None:
        with self.fake_windows_dpapi():
            self.store.save(self.config)
            document = json.loads(self.store.config_path.read_text(encoding="utf-8"))
            document["peer_key_dpapi"] = "not valid base64!"
            self.store.config_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(windows_config.CorruptConfigurationError):
                self.store.load()

    def test_store_rejects_unprotected_or_unknown_fields(self) -> None:
        with self.fake_windows_dpapi():
            self.store.save(self.config)
            document = json.loads(self.store.config_path.read_text(encoding="utf-8"))
            document["token"] = self.config.token
            self.store.config_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(windows_config.CorruptConfigurationError):
                self.store.load()

    def test_store_rejects_non_integer_config_version(self) -> None:
        with self.fake_windows_dpapi():
            self.store.save(self.config)
            document = json.loads(self.store.config_path.read_text(encoding="utf-8"))
            document["version"] = True
            self.store.config_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(windows_config.CorruptConfigurationError):
                self.store.load()

    def test_store_rejects_malformed_json(self) -> None:
        with self.fake_windows_dpapi():
            self.store.profile_dir.mkdir(parents=True)
            self.store.config_path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(windows_config.CorruptConfigurationError):
                self.store.load()

    def test_save_never_falls_back_to_plaintext_off_windows(self) -> None:
        with patch.object(windows_config, "is_windows", return_value=False):
            with self.assertRaises(windows_config.WindowsOnlyError):
                self.store.save(self.config)
        self.assertFalse(self.store.config_path.exists())


class WindowsConfigValidationTests(unittest.TestCase):
    def test_dpapi_is_guarded_off_windows(self) -> None:
        with patch.object(windows_config, "is_windows", return_value=False):
            with self.assertRaises(windows_config.WindowsOnlyError):
                windows_config.dpapi_protect(b"secret")
            with self.assertRaises(windows_config.WindowsOnlyError):
                windows_config.dpapi_unprotect(b"protected")

    def test_relay_url_requires_secure_base_url(self) -> None:
        self.assertEqual(
            windows_config.validate_relay_url(VALID_RELAY_URL + "/"),
            VALID_RELAY_URL,
        )
        with self.assertRaises(windows_config.InvalidConfigurationError):
            windows_config.validate_relay_url("https://agentbridge.example.herokuapp.com")
        with self.assertRaises(windows_config.InvalidConfigurationError):
            windows_config.validate_relay_url("wss://agentbridge.example.herokuapp.com/ws/device")
        with self.assertRaises(windows_config.InvalidConfigurationError):
            windows_config.validate_relay_url("wss://[not-an-ipv6-address")
        self.assertEqual(
            windows_config.validate_relay_url("ws://127.0.0.1:8765", allow_insecure_localhost=True),
            "ws://127.0.0.1:8765",
        )

    def test_device_token_matches_relay_safe_character_rules(self) -> None:
        self.assertEqual(windows_config.validate_device_token(VALID_TOKEN), VALID_TOKEN)
        for invalid_token in ("short", "a" * 513, "a" * 31 + "\n", "é" * 32):
            with self.subTest(invalid_token=invalid_token):
                with self.assertRaises(windows_config.InvalidConfigurationError):
                    windows_config.validate_device_token(invalid_token)

    def test_peer_key_rejects_whitespace_and_non_urlsafe_base64(self) -> None:
        key = Fernet.generate_key().decode("ascii")
        self.assertEqual(windows_config.validate_peer_key(key), key)
        for invalid_key in (key[:-1] + "\n", "+" + key[1:], "a" * 44):
            with self.subTest(invalid_key=invalid_key):
                with self.assertRaises(windows_config.InvalidConfigurationError):
                    windows_config.validate_peer_key(invalid_key)


class TrayLoggingTests(unittest.TestCase):
    def test_local_status_log_redacts_active_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = windows_config.WindowsConfigStore(Path(temporary_directory) / "AgentBridge")
            peer_key = Fernet.generate_key().decode("ascii")
            tray = AgentBridgeTray(store=store)
            tray._active_secrets = (VALID_TOKEN, peer_key)

            tray._append_log(
                f"Authorization: Bearer {VALID_TOKEN}; peer_key={peer_key}; connection failed"
            )
            contents = (store.data_dir / "tray.log").read_text(encoding="utf-8")

        self.assertNotIn(VALID_TOKEN, contents)
        self.assertNotIn(peer_key, contents)
        self.assertIn("[redacted]", contents)


class TrayLifecycleTests(unittest.TestCase):
    def test_connect_pause_resume_and_disconnect_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile_dir = Path(temporary_directory) / "AgentBridge"
            config = windows_config.DesktopConfig(
                relay_url=VALID_RELAY_URL,
                token=VALID_TOKEN,
                peer_key=Fernet.generate_key().decode("ascii"),
            )

            class Store:
                data_dir = profile_dir / "data"

                def load(self):
                    return config

                def ensure_data_dir(self):
                    self.data_dir.mkdir(parents=True, exist_ok=True)
                    return self.data_dir

            class Service:
                def __init__(self):
                    self.started = False
                    self.paused = False
                    self.stopped = False

                def start(self):
                    self.started = True

                def pause(self):
                    self.paused = True

                def resume(self):
                    self.paused = False

                def stop(self):
                    self.stopped = True

            services: list[Service] = []

            def create_service(_config, _data_dir, _on_status):
                service = Service()
                services.append(service)
                return service

            tray = AgentBridgeTray(store=Store(), service_factory=create_service)
            self.assertEqual(services, [])

            tray.connect()
            self.assertTrue(services[0].started)
            self.assertEqual(tray._status, "connecting")

            tray.pause_or_resume()
            self.assertTrue(services[0].paused)
            self.assertEqual(tray._status, "paused")

            tray.pause_or_resume()
            self.assertFalse(services[0].paused)
            self.assertEqual(tray._status, "connecting")

            self.assertTrue(tray.disconnect())
            self.assertTrue(services[0].stopped)
            self.assertEqual(tray._status, "stopped")


@unittest.skipUnless(windows_config.is_windows(), "requires native Windows DPAPI")
class NativeDpapiTests(unittest.TestCase):
    def test_dpapi_round_trip_for_current_windows_user(self) -> None:
        plaintext = b"AgentBridge native DPAPI test"
        protected = windows_config.dpapi_protect(plaintext)

        self.assertNotEqual(protected, plaintext)
        self.assertEqual(windows_config.dpapi_unprotect(protected), plaintext)


if __name__ == "__main__":
    unittest.main()
