import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import unittest

from cryptography.fernet import Fernet

from agentbridge.agent import serve_device_connection
from agentbridge.connection import BridgeDisconnected, Controller, RemoteError, open_socket
from agentbridge.relay import Relay


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.statuses = asyncio.Queue()
        self.relay = Relay("test-device-" + "d" * 48, "test-controller-" + "c" * 48)
        self.server = await self.relay.serve()
        port = self.server.sockets[0].getsockname()[1]
        self.device_config = {"relay_url": f"ws://127.0.0.1:{port}", "token": self.relay.tokens["device"],
                              "peer_key": Fernet.generate_key().decode(), "allow_insecure_localhost": True}
        self.control_config = {**self.device_config, "token": self.relay.tokens["controller"]}
        self.device = await open_socket(self.device_config, "device")
        self.worker = asyncio.create_task(serve_device_connection(
            self.device, self.device_config["peer_key"], self.root / "local-data",
            lambda state, detail: self.statuses.put_nowait(state)))
        await self.wait_status("waiting")

    async def asyncTearDown(self):
        await self.device.close()
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()
        self.temporary.cleanup()

    async def wait_status(self, expected):
        async with asyncio.timeout(15):
            while await self.statuses.get() != expected:
                pass

    async def job_events(self, controller, job_id):
        stdout, stderr = bytearray(), bytearray()
        async with asyncio.timeout(20):
            while True:
                event = await controller.events.get()
                if event.get("event") == "bridge.disconnected":
                    self.fail(event["message"])
                if event.get("job_id") != job_id:
                    continue
                if event["event"] == "exec.output":
                    target = stdout if event["stream"] == "stdout" else stderr
                    target.extend(base64.b64decode(event["data"], validate=True))
                elif event["event"] == "exec.exit":
                    return bytes(stdout), bytes(stderr), event

    async def test_full_file_roundtrip_and_atomic_large_upload(self):
        async with Controller(self.control_config) as controller:
            target = self.root / "a folder" / "test data.bin"
            payload = b"small\x00file\xff"
            await controller.request("fs.write", {"path": str(target), "data": base64.b64encode(payload).decode(), "create_parents": True})
            result = await controller.request("fs.read", {"path": str(target)})
            self.assertEqual(base64.b64decode(result["data"]), payload)
            self.assertTrue(result["eof"])
            await controller.request("fs.stat", {"path": str(target)})
            await controller.request("fs.list", {"path": str(target.parent)})
            content = bytes(range(256)) * 9000
            upload = await controller.request("upload.begin", {"path": str(target), "size": len(content)})
            for offset in range(0, len(content), 262144):
                await controller.request("upload.chunk", {"upload_id": upload["upload_id"], "offset": offset,
                                         "data": base64.b64encode(content[offset:offset + 262144]).decode()})
                self.assertEqual(target.read_bytes(), payload)
            await controller.request("upload.finish", {"upload_id": upload["upload_id"], "sha256": hashlib.sha256(content).hexdigest()})
            self.assertEqual(target.read_bytes(), content)
            received = bytearray()
            while True:
                part = await controller.request("fs.read", {"path": str(target), "offset": len(received), "length": 262144})
                received.extend(base64.b64decode(part["data"]))
                if part["eof"]:
                    break
            self.assertEqual(hashlib.sha256(received).digest(), hashlib.sha256(content).digest())

    async def test_streaming_process_stdin_stderr_and_exit_code(self):
        async with Controller(self.control_config) as controller:
            script = "import sys; line=sys.stdin.buffer.readline(); sys.stdout.buffer.write(b'OUT:'+line); sys.stdout.flush(); sys.stderr.write('ERR'); sys.exit(7)"
            result = await controller.request("exec.start", {"argv": [sys.executable, "-u", "-c", script], "cwd": str(self.root)})
            await controller.request("exec.stdin", {"job_id": result["job_id"], "data": base64.b64encode(b"hello\n").decode(), "eof": True})
            out, err, event = await self.job_events(controller, result["job_id"])
            self.assertEqual(out, b"OUT:hello\n")
            self.assertEqual(err, b"ERR")
            self.assertEqual(event["exit_code"], 7)
            self.assertFalse(event["timed_out"])

    async def test_cli_subprocess_transfers_files_and_streams_a_command(self):
        environment = {
            **os.environ,
            "BRIDGE_URL": self.control_config["relay_url"],
            "BRIDGE_CONTROLLER_TOKEN": self.control_config["token"],
            "BRIDGE_PEER_KEY": self.control_config["peer_key"],
        }

        async def cli(*arguments):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "agentbridge.controller",
                "--allow-insecure-localhost", *arguments,
                env=environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            return process.returncode, stdout, stderr

        source = self.root / "controller-source.bin"
        remote = self.root / "device-target.bin"
        downloaded = self.root / "controller-download.bin"
        payload = bytes(range(256)) * 1400
        source.write_bytes(payload)
        code, _, stderr = await cli("put", str(source), str(remote))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(remote.read_bytes(), payload)
        await self.wait_status("waiting")

        code, _, stderr = await cli("get", str(remote), str(downloaded), "--sha256", hashlib.sha256(payload).hexdigest())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(downloaded.read_bytes(), payload)
        await self.wait_status("waiting")

        script = "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(b'OUT:'+data); sys.stderr.buffer.write(b'ERR'); sys.exit(7)"
        code, stdout, stderr = await cli("exec", "--stdin", "hello\n", "--", sys.executable, "-u", "-c", script)
        self.assertEqual(code, 7, stderr)
        self.assertEqual(stdout, b"OUT:hello\n")
        self.assertIn(b"ERR", stderr)
        await self.wait_status("waiting")

    async def test_cancel_and_session_teardown_remove_unfinished_uploads(self):
        async with Controller(self.control_config) as controller:
            await self.wait_status("connected")
            target = self.root / "unfinished.bin"
            upload = await controller.request("upload.begin", {"path": str(target), "size": 100})
            await controller.request("upload.chunk", {"upload_id": upload["upload_id"], "offset": 0,
                                     "data": base64.b64encode(b"partial").decode()})
            result = await controller.request("exec.start", {"argv": [sys.executable, "-u", "-c", "import time; print('ready',flush=True); time.sleep(60)"]})
            await controller.request("exec.cancel", {"job_id": result["job_id"]})
            _, _, event = await self.job_events(controller, result["job_id"])
            self.assertTrue(event["cancelled"])
        await self.wait_status("waiting")
        self.assertFalse(target.exists())
        self.assertEqual([p for p in self.root.iterdir() if p.name != "local-data"], [])

    async def test_disconnect_stops_running_process_before_next_session(self):
        controller = await Controller(self.control_config).__aenter__()
        try:
            await self.wait_status("connected")
            marker = self.root / "pid.txt"
            code = "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); print('ready',flush=True); time.sleep(120)"
            result = await controller.request("exec.start", {"argv": [sys.executable, "-u", "-c", code, str(marker)]})
            async with asyncio.timeout(15):
                while True:
                    event = await controller.events.get()
                    if event.get("event") == "exec.output" and event.get("job_id") == result["job_id"]:
                        break
            pid = int(marker.read_text())
        finally:
            await controller.close()
        await self.wait_status("waiting")
        with self.assertRaises(OSError):
            os.kill(pid, 0)
        async with Controller(self.control_config) as fresh:
            await fresh.request("ping", {})
            self.assertEqual(await fresh.request("exec.list", {}), [])

    async def test_invalid_operation_does_not_disconnect_healthy_session(self):
        async with Controller(self.control_config) as controller:
            with self.assertRaises(RemoteError):
                await controller.request("not.a.method", {})
            with self.assertRaises(RemoteError):
                await controller.request("fs.read", {"path": str(self.root / "missing")})
            await controller.request("ping", {})

    async def test_wrong_peer_key_cannot_execute_even_with_valid_relay_token(self):
        wrong = {**self.control_config, "peer_key": Fernet.generate_key().decode()}
        with self.assertRaises(BridgeDisconnected):
            async with Controller(wrong):
                self.fail("An unpaired controller was authenticated")
        self.assertFalse((self.root / "local-data" / "audit.jsonl").exists())

    async def test_persistent_connection_roundtrip_latency(self):
        measurements = []
        async with Controller(self.control_config) as controller:
            for _ in range(15):
                start = time.perf_counter()
                await controller.request("ping", {})
                measurements.append((time.perf_counter() - start) * 1000)
        print(json.dumps({"test": "loopback_encrypted_rpc", "median_ms": round(statistics.median(measurements), 2),
                          "samples": len(measurements)}))


if __name__ == "__main__":
    unittest.main()
