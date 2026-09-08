"""Endpoint configuration validation; secrets never go in relay URLs."""

import ipaddress
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet


def validate_token(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 512:
        raise ValueError("Use a randomly generated relay token of at least 32 characters")
    if not token.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Relay token must contain printable ASCII without spaces")
    return token


def validate_config(config, role=None):
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    url = config.get("relay_url", "")
    if not isinstance(url, str):
        raise ValueError("relay_url must be a URL")
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a relay base URL without credentials, query, or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("Use the relay base URL, without /ws/device or /ws/controller")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Invalid relay port") from exc
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid relay port")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "wss":
        if not (parsed.scheme == "ws" and loopback and config.get("allow_insecure_localhost") is True):
            raise ValueError("wss:// is required; insecure ws:// is only an explicit loopback test option")
    token = validate_token(config.get("token"))
    key = config.get("peer_key")
    if not isinstance(key, str):
        raise ValueError("Missing endpoint peer_key; it must not be stored on Heroku")
    try:
        Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValueError("peer_key must be a valid locally generated Fernet key") from exc
    result = dict(config)
    result.update(relay_url=urlunsplit((parsed.scheme, parsed.netloc, "", "", "")), token=token, peer_key=key)
    return result


def endpoint_url(config, role):
    if role not in ("device", "controller"):
        raise ValueError("Unknown endpoint role")
    return validate_config(config)["relay_url"] + "/ws/" + role
