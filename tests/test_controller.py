"""CLI tests use a local mock controller; no relay or Windows desktop is needed."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from agentbridge.controller import _exec, build_parser, main


TOKEN = "t" * 32
PEER_KEY = Fernet.generate_key().decode("ascii")


class MockController:
    instances: list["MockController"] = []
    responses: dict[str, object] = {}
    events_to_send: list[dict[str, object]] = []

    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        for event in self.events_to_send:
            self.events.put_nowait(event)
        type(self).instances.append(self)

    async def __aenter__(self) -> "MockController":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def request(self, method: str, params: dict[str, object]) -> object:
        self.calls.append((method, params))
        response = self.responses.get(method, {})
        if callable(response):
            return response(params)
        return response


class BlockingController:
    """A controller double that lets the test cancel a running CLI job."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.started = asyncio.Event()

    async def request(self, method: str, params: dict[str, object]) -> object:
        self.calls.append((method, params))
        if method == "exec.start":
            self.started.set()
            return {"job_id": "job-cancel"}
        return {}


class ControllerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        MockController.instances = []
        MockController.responses = {}
        MockController.events_to_send = []

    def options(self) -> list[str]:
        return [
            "--url", "wss://relay.example.test",
            "--token", TOKEN,
            "--peer-key", PEER_KEY,
        ]

    def invoke(self, command: list[str], *, stdin: io.StringIO | None = None) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("agentbridge.controller.Controller", MockController):
            status = main(self.options() + command, stdin=stdin, stdout=stdout, stderr=stderr)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_ping_uses_mock_controller_and_keeps_secrets_out_of_output(self) -> None:
        MockController.responses = {"ping": {"ok": True}}

        status, stdout, stderr = self.invoke(["ping"])

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout), {"ok": True})
        self.assertEqual(MockController.instances[0].calls, [("ping", {})])
        self.assertNotIn(TOKEN, stdout + stderr)
        self.assertNotIn(PEER_KEY, stdout + stderr)

    def test_plaintext_url_requires_explicit_loopback_test_option(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("agentbridge.controller.Controller", MockController):
            status = main([
                "--url", "ws://127.0.0.1:3000", "--token", TOKEN, "--peer-key", PEER_KEY, "ping",
            ], stdout=stdout, stderr=stderr)

        self.assertEqual(status, 2)
        self.assertEqual(MockController.instances, [])
        self.assertIn("wss:// is required", stderr.getvalue())
        self.assertNotIn(TOKEN, stderr.getvalue())

    def test_environment_endpoint_config_is_supported_without_echoing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.dict(os.environ, {
                "XDG_CONFIG_HOME": directory,
                "BRIDGE_URL": "wss://relay.example.test",
                "BRIDGE_CONTROLLER_TOKEN": TOKEN,
                "BRIDGE_PEER_KEY": PEER_KEY,
            }, clear=False), patch("agentbridge.controller.Controller", MockController):
                status = main(["ping"], stdout=stdout, stderr=stderr)

        self.assertEqual(status, 0)
        self.assertEqual(MockController.instances[0].config["relay_url"], "wss://relay.example.test")
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(PEER_KEY, stdout.getvalue() + stderr.getvalue())

    def test_configure_writes_private_file_without_echoing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            secrets = iter((TOKEN, PEER_KEY))
            status = main(
                ["configure", "--config", str(path)],
                stdout=stdout,
                stderr=stderr,
                input_func=lambda prompt: "wss://relay.example.test",
                getpass_func=lambda prompt: next(secrets),
            )

            self.assertEqual(status, 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["token"], TOKEN)
            if __import__("os").name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(PEER_KEY, stdout.getvalue() + stderr.getvalue())

    def test_keygen_prints_only_for_explicit_command_and_can_write_silently(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        self.assertEqual(main(["keygen"], stdout=stdout, stderr=stderr), 0)
        generated = stdout.getvalue().strip()
        self.assertEqual(Fernet(generated.encode("ascii")).decrypt(Fernet(generated.encode("ascii")).encrypt(b"ok")), b"ok")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.json"
            stored_stdout = io.StringIO()
            self.assertEqual(main(["keygen", "--write", "--config", str(path)],
                                  stdout=stored_stdout, stderr=io.StringIO()), 0)
            self.assertEqual(stored_stdout.getvalue(), "")
            self.assertIn("peer_key", json.loads(path.read_text(encoding="utf-8")))

    def test_put_sends_strictly_sequential_offsets_and_digest(self) -> None:
        MockController.responses = {
            "upload.begin": {"upload_id": "upload-1"},
            "upload.chunk": {},
            "upload.finish": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.bin"
            payload = b"abcdefg"
            source.write_bytes(payload)
            status, stdout, _ = self.invoke([
                "put", str(source), r"C:\\remote.bin", "--chunk-size", "2",
            ])

        self.assertEqual(status, 0)
        chunks = [params for method, params in MockController.instances[0].calls if method == "upload.chunk"]
        self.assertEqual([chunk["offset"] for chunk in chunks], [0, 2, 4, 6])
        self.assertEqual(
            MockController.instances[0].calls[-1],
            ("upload.finish", {"upload_id": "upload-1", "sha256": hashlib.sha256(payload).hexdigest()}),
        )
        self.assertEqual(json.loads(stdout)["sha256"], hashlib.sha256(payload).hexdigest())

    def test_get_rejects_wrong_offset_without_replacing_destination(self) -> None:
        MockController.responses = {
            "fs.stat": {"size": 3},
            "fs.read": {"offset": 1, "data": base64.b64encode(b"bad").decode("ascii"), "eof": True},
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.bin"
            target.write_bytes(b"keep")
            status, _, stderr = self.invoke(["get", r"C:\\remote.bin", str(target)])
            self.assertEqual(target.read_bytes(), b"keep")

        self.assertEqual(status, 2)
        self.assertIn("unexpected offset", stderr)

    def test_get_uses_sequential_offsets_and_atomically_replaces_checked_file(self) -> None:
        payload = b"abcdefg"

        def read(params: dict[str, object]) -> dict[str, object]:
            offset = params["offset"]
            self.assertIsInstance(offset, int)
            data = payload[offset:offset + 2]
            return {
                "offset": offset,
                "data": base64.b64encode(data).decode("ascii"),
                "eof": offset + len(data) == len(payload),
            }

        MockController.responses = {"fs.stat": {"size": len(payload)}, "fs.read": read}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.bin"
            target.write_bytes(b"old")
            status, stdout, _ = self.invoke([
                "get", r"C:\\remote.bin", str(target), "--chunk-size", "2",
                "--sha256", hashlib.sha256(payload).hexdigest(),
            ])
            self.assertEqual(target.read_bytes(), payload)

        self.assertEqual(status, 0)
        reads = [params for method, params in MockController.instances[0].calls if method == "fs.read"]
        self.assertEqual([params["offset"] for params in reads], [0, 2, 4, 6])
        self.assertEqual(json.loads(stdout)["sha256"], hashlib.sha256(payload).hexdigest())

    def test_exec_streams_each_channel_and_preserves_remote_exit_code(self) -> None:
        MockController.responses = {"exec.start": {"job_id": "job-1"}}
        MockController.events_to_send = [
            {
                "kind": "event", "event": "exec.output", "job_id": "job-1", "stream": "stdout",
                "data": base64.b64encode(b"out\n").decode("ascii"),
            },
            {
                "kind": "event", "event": "exec.output", "job_id": "job-1", "stream": "stderr",
                "data": base64.b64encode(b"err\n").decode("ascii"),
            },
            {"kind": "event", "event": "exec.exit", "job_id": "job-1", "exit_code": 7,
             "cancelled": False, "timed_out": False},
        ]

        status, stdout, stderr = self.invoke(["exec", "--", "cmd", "/c", "echo", "hello"])

        self.assertEqual(status, 7)
        self.assertEqual(stdout, "out\n")
        self.assertIn("err\n", stderr)
        self.assertEqual(MockController.instances[0].calls[0], (
            "exec.start", {"argv": ["cmd", "/c", "echo", "hello"]},
        ))

    def test_exec_cancellation_sends_one_remote_cancel_without_reconnect(self) -> None:
        async def run_cancelled_job() -> list[tuple[str, dict[str, object]]]:
            controller = BlockingController()
            args = build_parser().parse_args(["exec", "--", "cmd", "/c", "pause"])
            task = asyncio.create_task(_exec(
                controller, args, io.StringIO(), io.StringIO(), io.StringIO(),
            ))
            await controller.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return controller.calls

        calls = asyncio.run(run_cancelled_job())

        self.assertEqual([method for method, _ in calls], ["exec.start", "exec.cancel"])
        self.assertEqual(calls[-1], ("exec.cancel", {"job_id": "job-cancel"}))

    def test_exec_sends_requested_stdin_in_base64_and_closes_it(self) -> None:
        def stdin_response(params: dict[str, object]) -> dict[str, object]:
            if params["eof"]:
                MockController.instances[0].events.put_nowait({
                    "kind": "event", "event": "exec.exit", "job_id": "job-stdin",
                    "exit_code": 0, "cancelled": False, "timed_out": False,
                })
            return {}

        MockController.responses = {
            "exec.start": {"job_id": "job-stdin"},
            "exec.stdin": stdin_response,
        }

        status, _, _ = self.invoke(["exec", "--stdin", "hello", "--", "cmd", "/c", "more"])

        self.assertEqual(status, 0)
        stdin_calls = [params for method, params in MockController.instances[0].calls if method == "exec.stdin"]
        self.assertEqual(stdin_calls, [
            {"job_id": "job-stdin", "data": base64.b64encode(b"hello").decode("ascii"), "eof": False},
            {"job_id": "job-stdin", "data": "", "eof": True},
        ])

    def test_rpc_keeps_machine_output_json_lines(self) -> None:
        MockController.responses = {"ping": {"ok": True}}

        status, stdout, stderr = self.invoke(
            ["rpc"], stdin=io.StringIO('{"id":"one","method":"ping","params":{}}\n'),
        )

        self.assertEqual(status, 0)
        self.assertEqual([json.loads(line) for line in stdout.splitlines()], [
            {"id": "one", "ok": True, "result": {"ok": True}},
        ])
        self.assertIn("RPC session connected", stderr)

    def test_rpc_reuses_one_connection_for_multiple_json_lines(self) -> None:
        MockController.responses = {"ping": {"pong": True}}

        status, stdout, _ = self.invoke(
            ["rpc"],
            stdin=io.StringIO(
                '{"id":"one","method":"ping","params":{}}\n'
                '{"id":"two","method":"ping","params":{}}\n',
            ),
        )

        self.assertEqual(status, 0)
        self.assertEqual(len(MockController.instances), 1)
        self.assertEqual(MockController.instances[0].calls, [("ping", {}), ("ping", {})])
        self.assertEqual([json.loads(line)["id"] for line in stdout.splitlines()], ["one", "two"])

    def test_rpc_reports_invalid_json_without_calling_remote(self) -> None:
        status, stdout, _ = self.invoke(["rpc"], stdin=io.StringIO("not json\n"))

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout)["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(MockController.instances[0].calls, [])

    def test_invalid_call_json_is_rejected_before_opening_a_connection(self) -> None:
        status, _, stderr = self.invoke(["call", "ping", "--params", "not-json"])

        self.assertEqual(status, 2)
        self.assertEqual(MockController.instances, [])
        self.assertIn("params must be valid JSON", stderr)


if __name__ == "__main__":
    unittest.main()
