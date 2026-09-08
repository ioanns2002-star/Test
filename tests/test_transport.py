import asyncio
from http import HTTPStatus
import json
import unittest

from cryptography.fernet import Fernet
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from agentbridge.config import validate_config
from agentbridge.connection import open_socket
from agentbridge.protocol import (MAX_FRAME, ProtocolError, SecureChannel, controller_handshake,
                                  device_handshake, pack)
from agentbridge.relay import Relay

DEVICE_TOKEN = "test-device-" + "d" * 48
CONTROL_TOKEN = "test-controller-" + "c" * 48


class MemorySocket:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        await asyncio.sleep(0)
        self.frames.append(frame)


class CryptoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.key = Fernet.generate_key()
        self.cipher = Fernet(self.key)
        self.socket = MemorySocket()
        self.sender = SecureChannel(self.socket, self.cipher, "controller", "a" * 64, "b" * 64)
        self.receiver = SecureChannel(None, self.cipher, "device", "a" * 64, "b" * 64)

    async def test_concurrent_sends_keep_sequence_and_do_not_expose_contents(self):
        await asyncio.gather(*(self.sender.send({"message": f"private-value-{i}"}) for i in range(30)))
        decoded = [self.receiver.decode(frame) for frame in self.socket.frames]
        self.assertEqual({v["message"] for v in decoded}, {f"private-value-{i}" for i in range(30)})
        self.assertEqual(self.receiver.received, 30)
        self.assertFalse(any(b"private-value" in f for f in self.socket.frames))

    async def test_replay_tampering_wrong_key_and_wrong_session_are_rejected(self):
        await self.sender.send({"kind": "request", "id": "1", "method": "exec.start"})
        frame = self.socket.frames[0]
        self.receiver.decode(frame)
        with self.assertRaises(ProtocolError):
            self.receiver.decode(frame)
        receivers = [
            SecureChannel(None, self.cipher, "device", "c" * 64, "d" * 64),
            SecureChannel(None, self.cipher, "controller", "a" * 64, "b" * 64),
            SecureChannel(None, Fernet(Fernet.generate_key()), "device", "a" * 64, "b" * 64),
        ]
        for receiver in receivers:
            with self.assertRaises(ProtocolError):
                receiver.decode(frame)
        fresh = SecureChannel(None, self.cipher, "device", "a" * 64, "b" * 64)
        with self.assertRaises(ProtocolError):
            fresh.decode(frame[:-8] + b"AAAAAAAA")
        with self.assertRaises(ProtocolError):
            fresh.decode('{"method":"exec.start"}')

    async def test_message_bounds_and_missing_sequences(self):
        with self.assertRaises(ProtocolError):
            await self.sender.send({"data": "x" * 700_001})
        self.assertEqual(self.sender.sent, 0)
        await self.sender.send({"n": 1})
        await self.sender.send({"n": 2})
        with self.assertRaises(ProtocolError):
            self.receiver.decode(self.socket.frames[1])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = {"relay_url": "wss://relay.example", "token": CONTROL_TOKEN,
                       "peer_key": Fernet.generate_key().decode()}

    def test_remote_plaintext_credentials_and_query_are_forbidden(self):
        for url in ("ws://relay.example", "https://relay.example", "wss://u:p@relay.example",
                    "wss://relay.example/?token=x", "wss://relay.example/#x", "wss://relay.example/ws/controller"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_config({**self.config, "relay_url": url, "allow_insecure_localhost": True})
        self.assertEqual(validate_config(self.config)["relay_url"], "wss://relay.example")

    def test_loopback_requires_explicit_opt_in_and_keys_are_validated(self):
        local = {**self.config, "relay_url": "ws://127.0.0.1:3000"}
        with self.assertRaises(ValueError):
            validate_config(local)
        self.assertEqual(validate_config({**local, "allow_insecure_localhost": True})["relay_url"], local["relay_url"])
        for extra in ({"peer_key": "bad"}, {"token": "password"}, {"token": "x" * 40 + "\n"}):
            with self.assertRaises(ValueError):
                validate_config({**self.config, **extra})


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = Relay(DEVICE_TOKEN, CONTROL_TOKEN)
        self.server = await self.relay.serve()
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        self.key = Fernet.generate_key().decode()
        self.device_config = {"relay_url": self.url, "token": DEVICE_TOKEN, "peer_key": self.key, "allow_insecure_localhost": True}
        self.control_config = {**self.device_config, "token": CONTROL_TOKEN}

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def expect_status(self, config, role, status):
        with self.assertRaises(InvalidStatus) as caught:
            async with open_socket(config, role):
                pass
        self.assertEqual(caught.exception.response.status_code, status)

    async def test_anonymous_health_contains_no_peer_or_authentication_data(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        self.assertIn(b"200 OK", response)
        data = json.loads(response.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(data, {"ok": True, "service": "AgentBridge", "protocol": 1})
        self.assertNotIn(DEVICE_TOKEN.encode(), response)

    async def test_role_authentication_and_offline_device(self):
        await self.expect_status(self.control_config, "controller", 503)
        await self.expect_status({**self.device_config, "token": CONTROL_TOKEN}, "device", 401)
        with self.assertRaises(InvalidStatus) as caught:
            async with connect(self.url + "/ws/device", proxy=None):
                pass
        self.assertEqual(caught.exception.response.status_code, 401)
        with self.assertRaises(InvalidStatus) as caught:
            async with connect(self.url + "/ws/device", origin="https://evil.example", additional_headers={"Authorization": "Bearer " + DEVICE_TOKEN}, proxy=None):
                pass
        self.assertEqual(caught.exception.response.status_code, 401)

    async def test_binary_relay_duplicate_rejection_and_clean_new_session(self):
        async with open_socket(self.device_config, "device") as device:
            await self.expect_status(self.device_config, "device", 409)
            async with open_socket(self.control_config, "controller") as control:
                self.assertEqual(json.loads(await device.recv()), {"bridge": "paired"})
                self.assertEqual(json.loads(await control.recv()), {"bridge": "paired"})
                await self.expect_status(self.control_config, "controller", 409)
                await control.send(b"opaque ciphertext")
                self.assertEqual(await device.recv(), b"opaque ciphertext")
                await device.send(b"opaque reply")
                self.assertEqual(await control.recv(), b"opaque reply")
            self.assertEqual(json.loads(await device.recv()), {"bridge": "unpaired"})
            await device.send(b"old-session-data-must-not-be-queued")
            # A round trip to the relay ensures it consumed the old frame first.
            await (await device.ping())
            async with open_socket(self.control_config, "controller") as control:
                self.assertEqual(json.loads(await device.recv()), {"bridge": "paired"})
                self.assertEqual(json.loads(await control.recv()), {"bridge": "paired"})
                await device.send(b"new session")
                self.assertEqual(await control.recv(), b"new session")

    async def test_plaintext_and_oversized_frames_are_closed(self):
        async with open_socket(self.device_config, "device") as device:
            async with open_socket(self.control_config, "controller") as control:
                await device.recv()
                await control.recv()
                await control.send('{"method":"exec.start"}')
                with self.assertRaises(ConnectionClosed) as caught:
                    await control.recv()
                self.assertEqual(caught.exception.rcvd.code, 1008)
            await device.recv()
            async with open_socket(self.control_config, "controller") as control:
                await device.recv()
                await control.recv()
                await control.send(b"x" * (MAX_FRAME + 1))
                with self.assertRaises(ConnectionClosed) as caught:
                    await control.recv()
                self.assertEqual(caught.exception.rcvd.code, 1009)

    async def test_actual_encrypted_handshake_and_bidirectional_rpc_body(self):
        async with open_socket(self.device_config, "device") as device:
            async with open_socket(self.control_config, "controller") as control:
                await device.recv()
                device_task = asyncio.create_task(device_handshake(device, self.key))
                controller = await controller_handshake(control, self.key)
                agent = await device_task
                await controller.send({"kind": "request", "id": "x", "method": "ping", "params": {}})
                self.assertEqual(agent.decode(await device.recv())["method"], "ping")
                await agent.send({"kind": "response", "id": "x", "ok": True, "result": "pong"})
                self.assertEqual(controller.decode(await control.recv())["result"], "pong")

    async def test_redirects_are_not_followed_with_relay_credentials(self):
        calls = []

        async def redirect(connection, request):
            calls.append(request.path)
            response = connection.respond(HTTPStatus.TEMPORARY_REDIRECT, "redirect")
            response.headers["Location"] = self.url + "/ws/device"
            return response

        async with serve(lambda ws: None, "127.0.0.1", 0, process_request=redirect) as server:
            port = server.sockets[0].getsockname()[1]
            config = {**self.device_config, "relay_url": f"ws://127.0.0.1:{port}"}
            await self.expect_status(config, "device", 307)
        self.assertEqual(calls, ["/ws/device"])
        self.assertFalse(self.relay.peers)


if __name__ == "__main__":
    unittest.main()
