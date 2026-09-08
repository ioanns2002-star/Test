"""Authenticated endpoint sessions over an untrusted byte-forwarding relay."""

import asyncio
import hashlib
import json
import secrets

from cryptography.fernet import Fernet, InvalidToken

MAX_FRAME = 1024 * 1024
MAX_PLAINTEXT = 700_000
HANDSHAKE_TIMEOUT = 10
HEARTBEAT_INTERVAL = 10
PEER_TIMEOUT = 35


class ProtocolError(Exception):
    pass


def pack(cipher, body):
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_PLAINTEXT:
        raise ProtocolError("Message too large; use chunked file transfer")
    return cipher.encrypt(data)


def unpack(cipher, frame):
    if not isinstance(frame, bytes) or len(frame) > MAX_FRAME:
        raise ProtocolError("Expected a bounded encrypted binary frame")
    try:
        value = json.loads(cipher.decrypt(frame))
    except (InvalidToken, ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise ProtocolError("Invalid encrypted message or wrong peer key") from exc
    if not isinstance(value, dict) or value.get("v") != 1:
        raise ProtocolError("Unsupported encrypted message")
    return value


def nonce(value):
    if not isinstance(value, str) or len(value) != 64:
        raise ProtocolError("Invalid handshake nonce")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ProtocolError("Invalid handshake nonce") from exc
    if value.lower() != value or len(decoded) != 32:
        raise ProtocolError("Invalid handshake nonce")
    return value


def broker_event(frame):
    if not isinstance(frame, str) or len(frame) > 100:
        raise ProtocolError("Expected relay session notification")
    try:
        value = json.loads(frame)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("Invalid relay notification") from exc
    if value not in ({"bridge": "paired"}, {"bridge": "unpaired"}):
        raise ProtocolError("Unknown relay notification")
    return value["bridge"]


class SecureChannel:
    def __init__(self, websocket, cipher, role, device_nonce, controller_nonce):
        self.websocket = websocket
        self.cipher = cipher
        self.role = role
        self.peer = "controller" if role == "device" else "device"
        self.session = hashlib.sha256(f"agentbridge-v1:{device_nonce}:{controller_nonce}".encode("ascii")).hexdigest()
        self.sent = 0
        self.received = 0
        self.lock = asyncio.Lock()

    async def send(self, body):
        async with self.lock:
            sequence = self.sent + 1
            frame = pack(self.cipher, {"v": 1, "session": self.session, "sender": self.role, "seq": sequence, "body": body})
            await self.websocket.send(frame)
            self.sent = sequence

    def decode(self, frame):
        value = unpack(self.cipher, frame)
        if value.get("session") != self.session or value.get("sender") != self.peer:
            raise ProtocolError("Message is from a different session or role")
        if type(value.get("seq")) is not int or value["seq"] != self.received + 1:
            raise ProtocolError("Duplicate, reordered, or missing message")
        if not isinstance(value.get("body"), dict):
            raise ProtocolError("Invalid message body")
        self.received = value["seq"]
        return value["body"]


async def device_handshake(websocket, peer_key):
    cipher = Fernet(peer_key.encode("ascii"))
    device_nonce = secrets.token_hex(32)
    async with asyncio.timeout(HANDSHAKE_TIMEOUT):
        await websocket.send(pack(cipher, {"v": 1, "kind": "hello", "nonce": device_nonce}))
        answer = unpack(cipher, await websocket.recv())
        if answer.get("kind") != "answer" or answer.get("device_nonce") != device_nonce:
            raise ProtocolError("Endpoint challenge failed")
        controller_nonce = nonce(answer.get("nonce"))
        await websocket.send(pack(cipher, {"v": 1, "kind": "ready", "device_nonce": device_nonce, "controller_nonce": controller_nonce}))
    return SecureChannel(websocket, cipher, "device", device_nonce, controller_nonce)


async def controller_handshake(websocket, peer_key):
    cipher = Fernet(peer_key.encode("ascii"))
    async with asyncio.timeout(HANDSHAKE_TIMEOUT):
        if broker_event(await websocket.recv()) != "paired":
            raise ProtocolError("PC is unavailable")
        hello = unpack(cipher, await websocket.recv())
        if hello.get("kind") != "hello":
            raise ProtocolError("Expected endpoint challenge")
        device_nonce = nonce(hello.get("nonce"))
        controller_nonce = secrets.token_hex(32)
        await websocket.send(pack(cipher, {"v": 1, "kind": "answer", "device_nonce": device_nonce, "nonce": controller_nonce}))
        ready = unpack(cipher, await websocket.recv())
        if ready != {"v": 1, "kind": "ready", "device_nonce": device_nonce, "controller_nonce": controller_nonce}:
            raise ProtocolError("Endpoint challenge failed")
    return SecureChannel(websocket, cipher, "controller", device_nonce, controller_nonce)
