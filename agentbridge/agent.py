"""PC-side session owner, exposed only through a visible desktop controller."""

import asyncio
import json
from pathlib import Path
import random
import threading
import time

from .config import validate_config
from .connection import connection_error, open_socket
from .operations import OperationError, Operations
from .protocol import HEARTBEAT_INTERVAL, PEER_TIMEOUT, ProtocolError, broker_event, device_handshake


def audit(data_dir, method, outcome):
    path = data_dir / "audit.jsonl"
    try:
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            path.replace(data_dir / "audit.previous.jsonl")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": time.time(), "method": method, "outcome": outcome}) + "\n")
    except OSError:
        pass


async def run_session(websocket, channel, data_dir):
    active = True
    calls = set()
    seen_ids = set()

    async def emit(event):
        if active:
            await channel.send({"kind": "event", **event})

    operations = Operations(data_dir, emit)

    async def respond(body):
        request_id, method = body["id"], body["method"]
        try:
            result = await operations.dispatch(method, body["params"])
            response = {"kind": "response", "id": request_id, "ok": True, "result": result}
            audit(data_dir, method, "ok")
        except OperationError as exc:
            response = {"kind": "response", "id": request_id, "ok": False, "error": {"code": exc.code, "message": str(getattr(exc, "message", exc))}}
            audit(data_dir, method, exc.code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = {"kind": "response", "id": request_id, "ok": False, "error": {"code": "FAILED", "message": f"Operation failed ({type(exc).__name__})"}}
            audit(data_dir, method, type(exc).__name__)
        if active:
            try:
                await channel.send(response)
            except ProtocolError:
                await channel.send({"kind": "response", "id": request_id, "ok": False,
                                    "error": {"code": "RESPONSE_TOO_LARGE", "message": "Use pagination or chunked transfer"}})

    async def heartbeat():
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await channel.send({"kind": "ping"})

    heartbeater = asyncio.create_task(heartbeat())
    try:
        while True:
            if heartbeater.done():
                await heartbeater
            frame = await asyncio.wait_for(websocket.recv(), PEER_TIMEOUT)
            if isinstance(frame, str):
                if broker_event(frame) == "unpaired":
                    return
                raise ProtocolError("A new pair cannot replace an active session")
            body = channel.decode(frame)
            kind = body.get("kind")
            if kind == "ping":
                await channel.send({"kind": "pong"})
            elif kind == "pong":
                continue
            elif kind == "request":
                request_id = body.get("id")
                method = body.get("method")
                if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
                        or not isinstance(method, str) or not 1 <= len(method) <= 100
                        or not isinstance(body.get("params"), dict)):
                    raise ProtocolError("Malformed request")
                if request_id in seen_ids:
                    raise ProtocolError("A request ID cannot be replayed")
                if len(calls) >= 32 or len(seen_ids) >= 100_000:
                    raise ProtocolError("Session request capacity exceeded")
                seen_ids.add(request_id)
                task = asyncio.create_task(respond(body))
                calls.add(task)
                def finished(completed):
                    calls.discard(completed)
                    if not completed.cancelled() and completed.exception():
                        asyncio.create_task(websocket.close(1011, "Endpoint response failed"))

                task.add_done_callback(finished)
            else:
                raise ProtocolError("Unexpected controller message")
    finally:
        active = False
        heartbeater.cancel()
        for task in calls:
            task.cancel()
        await asyncio.gather(heartbeater, *calls, return_exceptions=True)
        await operations.close()


async def serve_device_connection(websocket, peer_key, data_dir, on_status=lambda *_: None):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    on_status("waiting", "Relay connected; waiting for the trusted controller")
    async for frame in websocket:
        if broker_event(frame) == "unpaired":
            continue
        channel = await device_handshake(websocket, peer_key)
        on_status("connected", "Trusted controller connected")
        try:
            await run_session(websocket, channel, data_dir)
        finally:
            on_status("waiting", "Controller disconnected; session work stopped")


class AgentService:
    def __init__(self, config, data_dir, on_status=lambda *_: None):
        self.config = validate_config(config, "device")
        self.data_dir = Path(data_dir)
        self.on_status = on_status
        self.thread = None
        self.loop = None
        self.task = None
        self.session_task = None
        self.paused = threading.Event()
        self.stopping = threading.Event()

    def _status(self, name, detail):
        if self.paused.is_set() and name != "stopped":
            name, detail = "paused", "Remote access is paused"
        try:
            self.on_status(name, detail)
        except Exception:
            pass

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stopping.clear()
        self.thread = threading.Thread(target=self._thread_main, name="AgentBridge connection", daemon=False)
        self.thread.start()

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        finally:
            self.loop = None
            self.task = None
            self._status("stopped", "Remote access is off")

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        delay = 1
        try:
            while not self.stopping.is_set():
                if self.paused.is_set():
                    await asyncio.sleep(0.1)
                    continue
                self._status("connecting", "Connecting to the relay")
                try:
                    async with open_socket(self.config, "device") as websocket:
                        delay = 1
                        if self.paused.is_set() or self.stopping.is_set():
                            continue
                        self.session_task = asyncio.create_task(serve_device_connection(websocket, self.config["peer_key"], self.data_dir, self._status))
                        try:
                            await self.session_task
                        except asyncio.CancelledError:
                            if self.stopping.is_set():
                                raise
                            if not self.paused.is_set():
                                raise
                        finally:
                            self.session_task = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._status("error", connection_error(exc))
                if not self.paused.is_set() and not self.stopping.is_set():
                    await asyncio.sleep(delay + random.random() * 0.25)
                    delay = min(delay * 2, 15)
        except asyncio.CancelledError:
            pass

    def pause(self):
        self.paused.set()
        if self.loop and self.session_task:
            self.loop.call_soon_threadsafe(self.session_task.cancel)
        self._status("paused", "Remote access is paused; running session work is stopping")

    def resume(self):
        if self.stopping.is_set():
            return
        self.paused.clear()
        if not self.thread or not self.thread.is_alive():
            self.start()
        self._status("connecting", "Remote access enabled")

    def stop(self):
        self.stopping.set()
        if self.loop and self.task:
            self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)
        if self.thread and self.thread.is_alive():
            raise RuntimeError("Agent is still stopping; keep the tray open until cleanup completes")
