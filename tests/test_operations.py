"""Linux-runnable coverage for endpoint-local AgentBridge operations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

from agentbridge.operations import OperationError, Operations


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.events: asyncio.Queue[dict] = asyncio.Queue()

        async def emit(event: dict) -> None:
            await self.events.put(event)

        self.operations = Operations(self.root / "agent-data", emit)

    async def asyncTearDown(self) -> None:
        await self.operations.close()
        self.temporary.cleanup()

    async def _events_until_exit(self, job_id: str, timeout: float = 8) -> tuple[list[dict], dict]:
        events: list[dict] = []
        async with asyncio.timeout(timeout):
            while True:
                event = await self.events.get()
                if event.get("job_id") != job_id:
                    continue
                events.append(event)
                if event.get("event") == "exec.exit":
                    return events, event

    async def test_filesystem_operations_and_chunked_read(self) -> None:
        nested = self.root / "files" / "nested"
        created = await self.operations.dispatch("fs.mkdir", {"path": str(nested), "parents": True})
        self.assertEqual(created, {"path": str(nested), "created": True})

        source = nested / "source.bin"
        content = b"hello endpoint filesystem"
        written = await self.operations.dispatch(
            "fs.write",
            {"path": str(source), "data": base64.b64encode(content).decode("ascii")},
        )
        self.assertEqual(written["path"], str(source))
        self.assertEqual(written["size"], len(content))

        read = await self.operations.dispatch("fs.read", {"path": str(source), "offset": 6, "length": 8})
        self.assertEqual(base64.b64decode(read["data"]), b"endpoint")
        self.assertEqual(read["offset"], 6)
        self.assertFalse(read["eof"])

        metadata = await self.operations.dispatch("fs.stat", {"path": str(source)})
        self.assertEqual(metadata["type"], "file")
        self.assertEqual(metadata["size"], len(content))
        self.assertEqual(metadata["path"], str(source))

        listing = await self.operations.dispatch("fs.list", {"path": str(nested)})
        self.assertEqual(listing["path"], str(nested))
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["source.bin"])
        self.assertFalse(listing["truncated"])
        self.assertIsNone(listing["next_offset"])

        destination = nested / "moved.bin"
        moved = await self.operations.dispatch(
            "fs.move", {"source": str(source), "destination": str(destination)}
        )
        self.assertEqual(moved, {"source": str(source), "destination": str(destination)})
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), content)

        removed = await self.operations.dispatch("fs.remove", {"path": str(nested), "recursive": True})
        self.assertEqual(removed, {"path": str(nested), "removed": True})
        self.assertFalse(nested.exists())

    async def test_upload_is_sequential_verified_and_atomic(self) -> None:
        destination = self.root / "uploaded.bin"
        content = b"verified upload payload"
        begun = await self.operations.dispatch("upload.begin", {"path": str(destination), "size": len(content)})
        upload_id = begun["upload_id"]

        with self.assertRaises(OperationError) as raised:
            await self.operations.dispatch(
                "upload.chunk",
                {"upload_id": upload_id, "offset": 1, "data": base64.b64encode(content[:4]).decode("ascii")},
            )
        self.assertEqual(raised.exception.code, "OFFSET_MISMATCH")

        first = content[:7]
        chunk = await self.operations.dispatch(
            "upload.chunk",
            {"upload_id": upload_id, "offset": 0, "data": base64.b64encode(first).decode("ascii")},
        )
        self.assertEqual(chunk["received"], len(first))
        self.assertEqual(chunk["remaining"], len(content) - len(first))
        await self.operations.dispatch(
            "upload.chunk",
            {"upload_id": upload_id, "offset": len(first), "data": base64.b64encode(content[len(first) :]).decode("ascii")},
        )

        finished = await self.operations.dispatch(
            "upload.finish",
            {"upload_id": upload_id, "sha256": hashlib.sha256(content).hexdigest()},
        )
        self.assertEqual(finished["path"], str(destination))
        self.assertEqual(finished["size"], len(content))
        self.assertEqual(destination.read_bytes(), content)
        self.assertFalse(list(self.root.glob(".*.agentbridge-upload-*.tmp")))

    async def test_close_removes_unfinished_upload(self) -> None:
        destination = self.root / "unfinished.bin"
        await self.operations.dispatch("upload.begin", {"path": str(destination), "size": 10})
        self.assertTrue(list(self.root.glob(".*.agentbridge-upload-*.tmp")))
        await self.operations.close()
        self.assertFalse(list(self.root.glob(".*.agentbridge-upload-*.tmp")))
        self.assertFalse(destination.exists())

    async def test_exec_streams_output_accepts_stdin_and_writes_logs(self) -> None:
        code = (
            "import sys; "
            "data = sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(b'out:' + data); sys.stdout.buffer.flush(); "
            "sys.stderr.buffer.write(b'err'); sys.stderr.buffer.flush()"
        )
        started = await self.operations.dispatch("exec.start", {"argv": [sys.executable, "-c", code], "timeout": 10})
        job_id = started["job_id"]
        listed = await self.operations.dispatch("exec.list", {})
        self.assertIn(job_id, [job["job_id"] for job in listed])

        sent = await self.operations.dispatch(
            "exec.stdin",
            {"job_id": job_id, "data": base64.b64encode(b"data").decode("ascii"), "eof": True},
        )
        self.assertEqual(sent["written"], 4)
        self.assertTrue(sent["eof"])

        events, exit_event = await self._events_until_exit(job_id)

        self.assertEqual(exit_event["exit_code"], 0)
        self.assertFalse(exit_event["cancelled"])
        self.assertFalse(exit_event["timed_out"])
        stdout = b"".join(
            base64.b64decode(event["data"])
            for event in events
            if event["event"] == "exec.output" and event["stream"] == "stdout"
        )
        stderr = b"".join(
            base64.b64decode(event["data"])
            for event in events
            if event["event"] == "exec.output" and event["stream"] == "stderr"
        )
        self.assertEqual(stdout, b"out:data")
        self.assertEqual(stderr, b"err")

        log = await self.operations.dispatch("fs.read", {"path": started["stdout_log"]})
        self.assertEqual(base64.b64decode(log["data"]), b"out:data")

    async def test_exec_cancel_stops_a_running_process_tree(self) -> None:
        started = await self.operations.dispatch(
            "exec.start", {"argv": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout": 40}
        )
        job_id = started["job_id"]
        cancelled = await self.operations.dispatch("exec.cancel", {"job_id": job_id})
        self.assertEqual(cancelled, {"job_id": job_id, "cancelling": True})
        _, exit_event = await self._events_until_exit(job_id)
        self.assertTrue(exit_event["cancelled"])
        self.assertFalse(exit_event["timed_out"])

    async def test_invalid_transfer_payload_is_rejected(self) -> None:
        with self.assertRaises(OperationError) as raised:
            await self.operations.dispatch("fs.write", {"path": str(self.root / "x"), "data": "not base64!"})
        self.assertEqual(raised.exception.code, "INVALID_PARAMS")

    async def test_non_windows_desktop_calls_fail_clearly(self) -> None:
        if os.name == "nt":
            self.skipTest("Native desktop behavior requires an interactive Windows session")
        with self.assertRaises(OperationError) as raised:
            await self.operations.dispatch("screen.capture", {})
        self.assertEqual(raised.exception.code, "UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
