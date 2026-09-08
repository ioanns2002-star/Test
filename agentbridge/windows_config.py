"""Windows-only, DPAPI-backed configuration for the desktop tray app.

The relay URL is not a secret.  The device token and Fernet peer key are
encrypted with the current Windows user's DPAPI before they are written to
disk, and are returned only by :meth:`WindowsConfigStore.load`.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import ipaddress
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


APP_NAME = "AgentBridge"
CONFIG_FILE_NAME = "config.json"
DATA_DIRECTORY_NAME = "data"
CONFIG_VERSION = 1
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CONFIG_KEYS = frozenset(
    {"version", "relay_url", "device_token_dpapi", "peer_key_dpapi"}
)


class WindowsConfigError(RuntimeError):
    """Base class for safe-to-display local configuration errors."""


class WindowsOnlyError(WindowsConfigError):
    """Raised when DPAPI-backed configuration is requested off Windows."""


class InvalidConfigurationError(WindowsConfigError):
    """Raised when a value cannot be used as desktop agent configuration."""


class DpapiError(WindowsConfigError):
    """Raised when Windows cannot protect or decrypt local configuration."""


class CorruptConfigurationError(WindowsConfigError):
    """Raised when the encrypted configuration file cannot be trusted."""


# Keep the common all-caps spelling available for callers without exposing any
# platform implementation detail in their error handling.
DPAPIError = DpapiError


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


@dataclass(frozen=True)
class DesktopConfig:
    """The in-memory configuration passed to ``AgentService``.

    ``repr=False`` prevents an accidental diagnostic representation from
    including either long-lived secret.
    """

    relay_url: str
    token: str = field(repr=False)
    peer_key: str = field(repr=False)

    def as_agent_config(self) -> dict[str, str]:
        """Return only the endpoint fields accepted by ``AgentService``."""

        return {
            "relay_url": self.relay_url,
            "token": self.token,
            "peer_key": self.peer_key,
        }


def is_windows() -> bool:
    """Return whether the current interpreter is a native Windows build."""

    return os.name == "nt"


def default_profile_dir() -> Path:
    """Return the per-user directory shared by config, logs, and agent data."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME

    # LOCALAPPDATA is always present for an interactive Windows user.  The
    # fallback makes the path deterministic for restricted test environments.
    if is_windows():
        return Path.home() / "AppData" / "Local" / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def _require_windows() -> None:
    if not is_windows():
        raise WindowsOnlyError("AgentBridge desktop configuration is available only on Windows.")


def _make_blob(data: bytes) -> tuple[_DataBlob, Any]:
    """Build a DATA_BLOB while retaining the backing buffer for the API call."""

    buffer = (ctypes.c_byte * max(1, len(data)))()
    if data:
        ctypes.memmove(buffer, data, len(data))
    return (
        _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        ),
        buffer,
    )


def _windows_crypto_apis() -> tuple[Any, Any]:
    """Load and type the two Windows APIs used by DPAPI lazily."""

    _require_windows()
    try:
        crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    except (AttributeError, OSError) as error:
        raise DpapiError("Windows DPAPI is unavailable for this user session.") from error

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _free_blob(kernel32: Any, blob: _DataBlob) -> None:
    if blob.pbData:
        kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def dpapi_protect(plaintext: bytes) -> bytes:
    """Encrypt bytes for the current Windows user without showing any prompt."""

    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("DPAPI plaintext must be bytes.")

    crypt32, kernel32 = _windows_crypto_apis()
    source, source_buffer = _make_blob(bytes(plaintext))
    protected = _DataBlob()
    ctypes.set_last_error(0)
    success = crypt32.CryptProtectData(
        ctypes.byref(source),
        "AgentBridge configuration",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(protected),
    )
    if not success:
        _free_blob(kernel32, protected)
        raise DpapiError("Windows could not protect the local configuration.")

    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        _free_blob(kernel32, protected)
        # Keep the input buffer alive through the native call, then release it.
        del source_buffer


def dpapi_unprotect(ciphertext: bytes) -> bytes:
    """Decrypt bytes protected by :func:`dpapi_protect` for this Windows user."""

    if not isinstance(ciphertext, (bytes, bytearray)):
        raise TypeError("DPAPI ciphertext must be bytes.")
    if not ciphertext:
        raise DpapiError("The protected configuration value is empty.")

    crypt32, kernel32 = _windows_crypto_apis()
    source, source_buffer = _make_blob(bytes(ciphertext))
    plaintext = _DataBlob()
    ctypes.set_last_error(0)
    success = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(plaintext),
    )
    if not success:
        _free_blob(kernel32, plaintext)
        raise DpapiError("Windows could not decrypt the local configuration.")

    try:
        return ctypes.string_at(plaintext.pbData, plaintext.cbData)
    finally:
        _free_blob(kernel32, plaintext)
        # Keep the input buffer alive through the native call, then release it.
        del source_buffer


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_relay_url(value: str, *, allow_insecure_localhost: bool = False) -> str:
    """Validate and normalize the relay base URL without accepting remote WS."""

    if not isinstance(value, str):
        raise InvalidConfigurationError("Relay URL must be text.")
    url = value.strip()
    if not url:
        raise InvalidConfigurationError("Relay URL is required.")
    if any(character.isspace() for character in url):
        raise InvalidConfigurationError("Relay URL cannot contain whitespace.")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise InvalidConfigurationError("Relay URL is malformed or has an invalid port.") from error

    if not hostname or parsed.username or parsed.password:
        raise InvalidConfigurationError("Relay URL must contain only a host and optional port.")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise InvalidConfigurationError("Relay URL must be a WebSocket base URL without a path.")
    if port is not None and not 1 <= port <= 65535:
        raise InvalidConfigurationError("Relay URL has an invalid port.")

    if parsed.scheme == "wss":
        pass
    elif (
        parsed.scheme == "ws"
        and allow_insecure_localhost
        and _is_loopback_host(hostname)
    ):
        pass
    else:
        raise InvalidConfigurationError(
            "Relay URL must use wss:// (ws:// is allowed only for explicit localhost tests)."
        )

    return url.rstrip("/")


def validate_device_token(value: str) -> str:
    """Validate the device token without copying it into an error message."""

    if not isinstance(value, str):
        raise InvalidConfigurationError("Device token must be text.")
    if not 32 <= len(value) <= 512:
        raise InvalidConfigurationError("Device token must contain 32 to 512 characters.")
    if not value.isascii() or any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise InvalidConfigurationError(
            "Device token must use printable ASCII characters without whitespace."
        )
    return value


def validate_peer_key(value: str) -> str:
    """Validate a url-safe Fernet key without disclosing its contents."""

    if not isinstance(value, str):
        raise InvalidConfigurationError("Shared peer key must be text.")
    if (
        len(value) != 44
        or not value.endswith("=")
        or not value[:-1].isascii()
        or any(not (character.isalnum() or character in "-_") for character in value[:-1])
    ):
        raise InvalidConfigurationError("Shared peer key must be a valid Fernet key.")
    try:
        encoded = value.encode("ascii")
        Fernet(encoded)
    except (UnicodeEncodeError, ValueError, TypeError) as error:
        raise InvalidConfigurationError("Shared peer key must be a valid Fernet key.") from error
    return value


def generate_peer_key() -> str:
    """Generate a new Fernet key for a device/controller pair."""

    return Fernet.generate_key().decode("ascii")


def normalize_desktop_config(config: DesktopConfig | Mapping[str, str]) -> DesktopConfig:
    """Return validated endpoint config from a dataclass or mapping."""

    if isinstance(config, DesktopConfig):
        relay_url = config.relay_url
        token = config.token
        peer_key = config.peer_key
    elif isinstance(config, Mapping):
        try:
            relay_url = config["relay_url"]
            token = config["token"]
            peer_key = config["peer_key"]
        except KeyError as error:
            raise InvalidConfigurationError("Relay URL, device token, and shared peer key are required.") from error
    else:
        raise TypeError("Desktop config must be a DesktopConfig or mapping.")

    return DesktopConfig(
        relay_url=validate_relay_url(relay_url),
        token=validate_device_token(token),
        peer_key=validate_peer_key(peer_key),
    )


class WindowsConfigStore:
    """Read and atomically write one current-user encrypted desktop config."""

    def __init__(self, profile_dir: str | Path | None = None) -> None:
        self.profile_dir = Path(profile_dir) if profile_dir is not None else default_profile_dir()

    @property
    def config_path(self) -> Path:
        return self.profile_dir / CONFIG_FILE_NAME

    @property
    def data_dir(self) -> Path:
        return self.profile_dir / DATA_DIRECTORY_NAME

    def has_config(self) -> bool:
        return self.config_path.is_file()

    def ensure_data_dir(self) -> Path:
        """Create the long-lived per-user data directory when it is needed."""

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise WindowsConfigError("Could not create the local AgentBridge data directory.") from error
        _restrict_file_mode(self.data_dir, mode=0o700)
        return self.data_dir

    def save(self, config: DesktopConfig | Mapping[str, str]) -> None:
        """Validate and persist config, encrypting every secret before writing."""

        _require_windows()
        normalized = normalize_desktop_config(config)
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise WindowsConfigError("Could not create the local AgentBridge profile.") from error
        _restrict_file_mode(self.profile_dir, mode=0o700)

        document = {
            "version": CONFIG_VERSION,
            "relay_url": normalized.relay_url,
            "device_token_dpapi": _encode_protected(normalized.token),
            "peer_key_dpapi": _encode_protected(normalized.peer_key),
        }
        _write_json_atomically(self.config_path, document)

    def load(self) -> DesktopConfig:
        """Load and decrypt current-user config into memory only."""

        _require_windows()
        try:
            raw_document = self.config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError as error:
            raise WindowsConfigError("Could not read the saved AgentBridge configuration.") from error

        try:
            document = json.loads(raw_document)
        except (json.JSONDecodeError, TypeError) as error:
            raise CorruptConfigurationError("Saved AgentBridge configuration is not valid JSON.") from error
        if (
            not isinstance(document, dict)
            or set(document) != _CONFIG_KEYS
            or type(document.get("version")) is not int
            or document.get("version") != CONFIG_VERSION
        ):
            raise CorruptConfigurationError("Saved AgentBridge configuration has an unsupported format.")

        try:
            relay_url = document["relay_url"]
            token = _decode_protected(document["device_token_dpapi"])
            peer_key = _decode_protected(document["peer_key_dpapi"])
            return normalize_desktop_config(
                {"relay_url": relay_url, "token": token, "peer_key": peer_key}
            )
        except (KeyError, TypeError, UnicodeDecodeError, DpapiError, InvalidConfigurationError) as error:
            raise CorruptConfigurationError(
                "Saved AgentBridge configuration could not be decrypted for this Windows user."
            ) from error


def _encode_protected(value: str) -> str:
    protected = dpapi_protect(value.encode("utf-8"))
    return base64.b64encode(protected).decode("ascii")


def _decode_protected(value: object) -> str:
    if not isinstance(value, str):
        raise DpapiError("Protected configuration value has an invalid format.")
    try:
        protected = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise DpapiError("Protected configuration value is not valid base64.") from error
    return dpapi_unprotect(protected).decode("utf-8")


def _restrict_file_mode(path: Path, *, mode: int) -> None:
    """Apply a useful restrictive mode where the filesystem honors POSIX bits."""

    try:
        path.chmod(mode)
    except OSError:
        # DPAPI is the secret boundary on Windows; inherited ACLs remain in
        # effect if a filesystem does not support POSIX-style mode bits.
        pass


def _write_json_atomically(path: Path, document: Mapping[str, object]) -> None:
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=".agentbridge-",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _restrict_file_mode(path, mode=0o600)
    except OSError as error:
        raise WindowsConfigError("Could not save the AgentBridge configuration.") from error
    finally:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            if temporary_path is not None:
                temporary_path.unlink()
        except OSError:
            pass
