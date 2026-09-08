"""One in-memory WebSocket pair. No execution, decryption, files, or database."""

import asyncio
import hmac
import json
import logging
import os
import signal
from http import HTTPStatus

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

MAX_FRAME = 1024 * 1024
HEALTH = json.dumps({"ok": True, "service": "AgentBridge", "protocol": 1}) + "\n"


def relay_token(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 512 or not value.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("Configure separate randomly generated relay tokens of at least 32 characters")
    return value


class Relay:
    def __init__(self, device_token, controller_token):
        self.tokens = {"device": relay_token(device_token), "controller": relay_token(controller_token)}
        if hmac.compare_digest(device_token, controller_token):
            raise ValueError("Device and controller relay tokens must be different")
        self.peers = {}
        self.lock = asyncio.Lock()

    async def process_request(self, connection, request):
        if request.path in ("/", "/healthz"):
            response = connection.respond(HTTPStatus.OK, HEALTH)
            del response.headers["Content-Type"]
            response.headers["Content-Type"] = "application/json; charset=utf-8"
            response.headers["Cache-Control"] = "no-store"
            return response
        role = {"/ws/device": "device", "/ws/controller": "controller"}.get(request.path)
        if role is None:
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        values = request.headers.get_all("Authorization")
        supplied = values[0] if len(values) == 1 else ""
        if request.headers.get_all("Origin") or not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + self.tokens[role]).encode("ascii")):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        async with self.lock:
            if role in self.peers:
                return connection.respond(HTTPStatus.CONFLICT, "Role already connected\n")
            if role == "controller" and "device" not in self.peers:
                return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "PC is offline\n")
        return None

    async def handler(self, websocket):
        role = websocket.request.path.rsplit("/", 1)[-1]
        other_role = "controller" if role == "device" else "device"
        async with self.lock:
            if role in self.peers or (role == "controller" and "device" not in self.peers):
                await websocket.close(1013, "Role busy or PC offline")
                return
            self.peers[role] = websocket
            other = self.peers.get(other_role)
            if other:
                try:
                    await self.peers["device"].send('{"bridge":"paired"}')
                    await self.peers["controller"].send('{"bridge":"paired"}')
                except ConnectionClosed:
                    pass
        try:
            async for frame in websocket:
                if not isinstance(frame, bytes):
                    await websocket.close(1008, "Only encrypted binary frames are allowed")
                    break
                other = self.peers.get(other_role)
                if other is None:
                    continue
                try:
                    await other.send(frame)
                except ConnectionClosed:
                    # A disconnect must never route an old command to a new peer.
                    continue
        except ConnectionClosed:
            pass
        finally:
            async with self.lock:
                if self.peers.get(role) is websocket:
                    del self.peers[role]
                    other = self.peers.get(other_role)
                    if other:
                        try:
                            if role == "device":
                                await other.close(1012, "PC disconnected; commands are not retried")
                            else:
                                await other.send('{"bridge":"unpaired"}')
                        except ConnectionClosed:
                            pass

    def serve(self, host="127.0.0.1", port=0):
        return serve(self.handler, host, port, process_request=self.process_request,
                     compression=None, max_size=MAX_FRAME, max_queue=8,
                     write_limit=32768, open_timeout=5, ping_interval=20,
                     ping_timeout=20, close_timeout=2, server_header=None)


async def run():
    if os.environ.get("WEB_CONCURRENCY", "1") != "1":
        raise ValueError("AgentBridge requires one process on one web dyno")
    relay = Relay(os.environ.get("BRIDGE_DEVICE_TOKEN"), os.environ.get("BRIDGE_CONTROLLER_TOKEN"))
    port = int(os.environ.get("PORT", "3000"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid PORT")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    async with relay.serve("0.0.0.0", port):
        print(f"AgentBridge relay listening on port {port}; one dyno, no storage", flush=True)
        await stop.wait()


def main():
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
