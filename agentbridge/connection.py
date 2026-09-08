"""Controller session with streaming events and no automatic command retries."""

import asyncio
import math
import uuid

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .config import endpoint_url, validate_config
from .protocol import HEARTBEAT_INTERVAL, MAX_FRAME, PEER_TIMEOUT, ProtocolError, controller_handshake


class BridgeDisconnected(Exception):
    pass


class RemoteError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class DirectConnect(connect):
    def process_redirect(self, exc):
        return exc


def open_socket(config, role):
    return DirectConnect(endpoint_url(config, role),
                         additional_headers={"Authorization": "Bearer " + config["token"]},
                         compression=None, max_size=MAX_FRAME, max_queue=8,
                         write_limit=32768, open_timeout=10, close_timeout=2,
                         ping_interval=20, ping_timeout=20, proxy=None)


def connection_error(exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return {401: "Relay authentication rejected", 409: "This role is already connected",
                503: "PC is offline"}.get(status, f"Relay rejected the connection (HTTP {status})")
    if isinstance(exc, ProtocolError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "The authenticated peer stopped responding"
    return f"Connection ended ({type(exc).__name__}); actions are not retried"


class Controller:
    def __init__(self, config):
        self.config = validate_config(config, "controller")
        self.events = asyncio.Queue(maxsize=128)
        self.closed = asyncio.Event()
        self.pending = {}
        self.websocket = None
        self.channel = None
        self.receiver = None
        self.heartbeater = None
        self.disconnect_error = None

    async def __aenter__(self):
        try:
            self.websocket = await open_socket(self.config, "controller")
            self.channel = await controller_handshake(self.websocket, self.config["peer_key"])
        except Exception as exc:
            if self.websocket:
                await self.websocket.close()
            raise BridgeDisconnected(connection_error(exc)) from exc
        self.receiver = asyncio.create_task(self._receive())
        self.heartbeater = asyncio.create_task(self._heartbeat())
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()

    def _disconnected(self, message):
        if self.closed.is_set():
            return
        self.disconnect_error = BridgeDisconnected(message)
        self.closed.set()
        for future in self.pending.values():
            if not future.done():
                future.set_exception(self.disconnect_error)
        notice = {"kind": "event", "event": "bridge.disconnected", "message": message}
        if self.events.full():
            self.events.get_nowait()
        self.events.put_nowait(notice)

    async def _receive(self):
        message = "Session closed; unfinished actions are not retried"
        try:
            while True:
                frame = await asyncio.wait_for(self.websocket.recv(), PEER_TIMEOUT)
                body = self.channel.decode(frame)
                kind = body.get("kind")
                if kind == "response":
                    future = self.pending.get(body.get("id"))
                    if future and not future.done():
                        if body.get("ok") is True:
                            future.set_result(body.get("result"))
                        elif body.get("ok") is False and isinstance(body.get("error"), dict):
                            error = body["error"]
                            future.set_exception(RemoteError(error.get("code", "ERROR"), error.get("message", "Operation failed")))
                        else:
                            raise ProtocolError("Malformed RPC response")
                elif kind == "event":
                    await self.events.put(body)
                elif kind == "ping":
                    await self.channel.send({"kind": "pong"})
                elif kind != "pong":
                    raise ProtocolError("Unexpected endpoint message")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = connection_error(exc)
        finally:
            self._disconnected(message)
            if self.heartbeater:
                self.heartbeater.cancel()
            await self.websocket.close()

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await self.channel.send({"kind": "ping"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._disconnected(connection_error(exc))
            await self.websocket.close()

    async def request(self, method, params=None, timeout=30):
        if not self.channel or self.closed.is_set():
            raise self.disconnect_error or BridgeDisconnected("Not connected")
        if not isinstance(method, str) or not 1 <= len(method) <= 100:
            raise ValueError("Invalid method")
        if params is not None and not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if len(self.pending) >= 32:
            raise ValueError("Too many concurrent requests")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.channel.send({"kind": "request", "id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            await self.close()
            raise BridgeDisconnected("Request timed out; outcome may be unknown and was not retried") from exc
        except ConnectionClosed as exc:
            raise BridgeDisconnected(connection_error(exc)) from exc
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def close(self):
        self._disconnected("Session closed; unfinished actions are not retried")
        for task in (self.receiver, self.heartbeater):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.receiver, self.heartbeater) if t), return_exceptions=True)
        if self.websocket:
            await self.websocket.close()
