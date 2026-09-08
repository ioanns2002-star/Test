"""Endpoint-local RPC operations for AgentBridge.

The relay only transports encrypted messages. This module never opens a network
connection; files, logs, screenshots, and process output stay on the endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import errno
import getpass
import hashlib
import hmac
import json
import math
import os
import platform
import re
import secrets
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .windows_process import ContainedProcess, ProcessLaunchError, start_contained_process


MAX_TRANSFER_BYTES = 262_144
DEFAULT_READ_LENGTH = MAX_TRANSFER_BYTES
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 500
MAX_LIST_OFFSET = 1_000_000
MAX_LIST_RESPONSE_BYTES = 500_000
MAX_PATH_CHARS = 32_767
MAX_FILE_OFFSET = (1 << 63) - 1
MAX_ACTIVE_UPLOADS = 8
MAX_ACTIVE_JOBS = 32
MAX_EXEC_ARGS = 128
MAX_EXEC_ARGUMENT_BYTES = 262_144
MAX_SCRIPT_CHARS = 30_000
MAX_ENV_ITEMS = 128
MAX_ENV_BYTES = 131_072
DEFAULT_EXEC_TIMEOUT = 3_600.0
MAX_EXEC_TIMEOUT = 86_400.0
OUTPUT_CHUNK_BYTES = 65_536
MAX_LOG_BYTES_PER_STREAM = 4 * 1024 * 1024
MAX_TEXT_CHARS = 8_192


class OperationError(Exception):
    """A request failed in a form safe to return through the RPC protocol."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class _Upload:
    upload_id: str
    destination: Path
    temporary: Path
    size: int
    received: int = 0
    cancelled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Job:
    job_id: str
    process: ContainedProcess
    argv: list[str]
    started: float
    timeout: float
    stdout_log: Path
    stderr_log: Path
    cancelled: bool = False
    timed_out: bool = False
    stdin_closed: bool = False
    stdin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancellation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pumps: list[asyncio.Task[Any]] = field(default_factory=list)
    watcher: asyncio.Task[Any] | None = None


class _NotRegularFile(Exception):
    pass


class _UploadSizeMismatch(Exception):
    pass


class _UploadDigestMismatch(Exception):
    pass


class _PillowUnavailable(Exception):
    pass


class Operations:
    """Run the endpoint operations for one authenticated controller session."""

    def __init__(self, data_dir: Path, emit: Callable[[dict[str, Any]], Awaitable[None]]):
        self.data_dir = _absolute_path(Path(data_dir).expanduser())
        self._emit = emit
        self._uploads: dict[str, _Upload] = {}
        self._jobs: dict[str, _Job] = {}
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self._closed = False
        self._pending_upload_starts = 0
        self._pending_job_starts = 0
        self._pending_starts = 0
        self._no_pending_starts = asyncio.Event()
        self._no_pending_starts.set()
        self._held_keys: dict[str, tuple[int, bool]] = {}
        self._held_buttons: set[str] = set()
        self._desktop: _WindowsDesktop | None = None

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Dispatch one validated operation and return its local result."""

        if self._closed:
            raise OperationError("SESSION_CLOSED", "The controller session has ended")
        if not isinstance(method, str) or not method or len(method) > 100:
            raise OperationError("INVALID_PARAMS", "method must be a non-empty string")
        if not isinstance(params, dict):
            raise OperationError("INVALID_PARAMS", "params must be an object")
        handler = {
            "ping": self._ping,
            "system.info": self._system_info,
            "fs.list": self._fs_list,
            "fs.stat": self._fs_stat,
            "fs.read": self._fs_read,
            "fs.write": self._fs_write,
            "fs.mkdir": self._fs_mkdir,
            "fs.move": self._fs_move,
            "fs.remove": self._fs_remove,
            "upload.begin": self._upload_begin,
            "upload.chunk": self._upload_chunk,
            "upload.finish": self._upload_finish,
            "exec.start": self._exec_start,
            "exec.stdin": self._exec_stdin,
            "exec.cancel": self._exec_cancel,
            "exec.list": self._exec_list,
            "screen.capture": self._screen_capture,
            "input.mouse": self._input_mouse,
            "input.key": self._input_key,
            "input.text": self._input_text,
        }.get(method)
        if handler is None:
            raise OperationError("UNKNOWN_METHOD", f"Unsupported operation: {method}")
        return await handler(params)

    async def close(self) -> None:
        """Cancel session-owned work and release explicit desktop input."""

        async with self._close_lock:
            if self._closed:
                return
            async with self._state_lock:
                self._closed = True
                uploads = list(self._uploads.values())
                jobs = list(self._jobs.values())
                self._uploads.clear()
                for upload in uploads:
                    upload.cancelled = True

            await self._release_held_input()
            await asyncio.gather(*(self._discard_upload(upload) for upload in uploads), return_exceptions=True)
            await asyncio.gather(*(self._stop_job(job, cancelled=True) for job in jobs), return_exceptions=True)
            await self._no_pending_starts.wait()
            watchers = [job.watcher for job in jobs if job.watcher is not None]
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)

    async def _ping(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, set())
        return {"pong": True}

    async def _system_info(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, set())
        return {
            "os": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
            },
            "user": getpass.getuser(),
            "elevated": _is_elevated(),
            "paths": {
                "cwd": str(Path.cwd()),
                "data_dir": str(self.data_dir),
                "python": sys.executable,
            },
            "capabilities": {
                "filesystem": True,
                "process": True,
                "powershell": os.name == "nt",
                "screen_capture": os.name == "nt" and _pillow_available(),
                "input": os.name == "nt",
            },
        }

    async def _fs_list(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"path", "offset", "limit"})
        path = _path_param(params, "path")
        offset = _integer(params, "offset", default=0, minimum=0, maximum=MAX_LIST_OFFSET)
        limit = _integer(params, "limit", default=DEFAULT_LIST_LIMIT, minimum=1, maximum=MAX_LIST_LIMIT)
        try:
            return await asyncio.to_thread(_list_directory, path, offset, limit)
        except OSError as exc:
            _raise_filesystem_error(exc, "Could not list directory")

    async def _fs_stat(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"path"})
        path = _path_param(params, "path")
        try:
            return await asyncio.to_thread(_path_metadata, path)
        except OSError as exc:
            _raise_filesystem_error(exc, "Could not stat path")

    async def _fs_read(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"path", "offset", "length"})
        path = _path_param(params, "path")
        offset = _integer(params, "offset", default=0, minimum=0, maximum=MAX_FILE_OFFSET)
        length = _integer(params, "length", default=DEFAULT_READ_LENGTH, minimum=0, maximum=MAX_TRANSFER_BYTES)
        try:
            data, eof = await asyncio.to_thread(_read_regular_file, path, offset, length)
        except _NotRegularFile:
            raise OperationError("UNSUPPORTED", "fs.read supports regular files only") from None
        except OSError as exc:
            _raise_filesystem_error(exc, "Could not read file")
        return {"data": _b64encode(data), "offset": offset, "eof": eof}

    async def _fs_write(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"path", "data", "create_parents"})
        path = _path_param(params, "path")
        data = _data_param(params, "data")
        create_parents = _boolean(params, "create_parents", default=False)
        try:
            size = await asyncio.to_thread(_atomic_write, path, data, create_parents)
        except OSError as exc:
            _raise_filesystem_error(exc, "Could not write file")
        return {"path": str(path), "size": size}

    async def _fs_mkdir(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"path", "parents"})
        path = _path_param(params, "path")
        parents = _boolean(params, "parents", default=False)
        try:
            await asyncio.to_thread(path.mkdir, parents=parents, exist_ok=False)
        except OSError as exc:
            _raise_filesystem_error(exc, "Could not create directory")
        return {"path": str(path), "created": True}

    async def _fs_move(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"source", "destination"})
        source = _path_param(params, "source")
        destination = _path_param(params, "destination")
        try:
            await asyncio.to_thread(_move_path, source, destination)
        except OSError as exc:
            _raise_filesystem_error(exc, "Could not move path")
        return {"source": str(source), "destination": str(destination)}

    async def _fs_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"path", "recursive"})
        path = _path_param(params, "path")
        recursive = _boolean(params, "recursive", default=False)
        try:
            await asyncio.to_thread(_remove_path, path, recursive)
        except OSError as exc:
            _raise_filesystem_error(exc, "Could not remove path")
        return {"path": str(path), "removed": True}

    async def _upload_begin(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"path", "size"})
        destination = _path_param(params, "path")
        size = _integer(params, "size", minimum=0, maximum=MAX_FILE_OFFSET)
        upload_id = secrets.token_urlsafe(18)
        async with self._state_lock:
            self._ensure_open()
            if len(self._uploads) + self._pending_upload_starts >= MAX_ACTIVE_UPLOADS:
                raise OperationError("RESOURCE_LIMIT", "Too many active uploads")
            self._pending_upload_starts += 1
            self._pending_starts += 1
            self._no_pending_starts.clear()

        temporary: Path | None = None
        registered = False
        try:
            creation_task = asyncio.create_task(asyncio.to_thread(_create_upload_file, destination))
            try:
                temporary = await asyncio.shield(creation_task)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    temporary = await creation_task
                if temporary is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(_unlink_if_exists, temporary)
                raise
            except OSError as exc:
                raise _filesystem_error(exc, "Could not create upload temporary file") from exc

            upload = _Upload(upload_id, destination, temporary, size)
            async with self._state_lock:
                if self._closed:
                    upload.cancelled = True
                else:
                    self._uploads[upload_id] = upload
                    registered = True
            if not registered:
                await self._discard_upload(upload)
                raise OperationError("SESSION_CLOSED", "The controller session has ended")
            return {"upload_id": upload_id, "path": str(destination), "size": size}
        except BaseException:
            if temporary is not None and not registered:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(_unlink_if_exists, temporary)
            raise
        finally:
            await asyncio.shield(self._release_upload_start_slot())

    async def _upload_chunk(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"upload_id", "offset", "data"})
        upload_id = _string_param(params, "upload_id", maximum=128)
        offset = _integer(params, "offset", minimum=0, maximum=MAX_FILE_OFFSET)
        data = _data_param(params, "data")
        upload = await self._get_upload(upload_id)
        async with upload.lock:
            if self._closed or upload.cancelled:
                raise OperationError("SESSION_CLOSED", "The upload was cancelled")
            if offset != upload.received:
                raise OperationError("OFFSET_MISMATCH", "Upload chunks must use the next sequential offset")
            if len(data) > upload.size - upload.received:
                raise OperationError("SIZE_MISMATCH", "Upload data exceeds the declared size")
            try:
                await asyncio.to_thread(_write_upload_chunk, upload.temporary, offset, data)
            except OSError as exc:
                upload.cancelled = True
                await self._forget_upload(upload)
                await asyncio.to_thread(_unlink_if_exists, upload.temporary)
                _raise_filesystem_error(exc, "Could not write upload chunk")
            upload.received += len(data)
            return {
                "upload_id": upload.upload_id,
                "offset": offset,
                "received": upload.received,
                "remaining": upload.size - upload.received,
            }

    async def _upload_finish(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"upload_id", "sha256"})
        upload_id = _string_param(params, "upload_id", maximum=128)
        expected_digest = _sha256_param(params, "sha256")
        upload = await self._get_upload(upload_id)
        cleanup = False
        async with upload.lock:
            if self._closed or upload.cancelled:
                cleanup = True
                failure = OperationError("SESSION_CLOSED", "The upload was cancelled")
            elif upload.received != upload.size:
                raise OperationError("INCOMPLETE_UPLOAD", "Upload has not received its declared size")
            else:
                try:
                    await asyncio.to_thread(_verify_upload_file, upload.temporary, upload.size, expected_digest)
                except _UploadSizeMismatch:
                    cleanup = True
                    failure = OperationError("SIZE_MISMATCH", "Upload temporary file has an unexpected size")
                except _UploadDigestMismatch:
                    cleanup = True
                    failure = OperationError("CHECKSUM_MISMATCH", "Upload SHA-256 does not match")
                except OSError as exc:
                    cleanup = True
                    failure = _filesystem_error(exc, "Could not verify upload")
                else:
                    # Serialise the final rename with close: it either commits before
                    # disconnect cleanup or remains an unfinished temporary file.
                    async with self._state_lock:
                        if self._closed or upload.cancelled:
                            cleanup = True
                            failure = OperationError("SESSION_CLOSED", "The upload was cancelled")
                        else:
                            try:
                                await asyncio.to_thread(os.replace, upload.temporary, upload.destination)
                            except OSError as exc:
                                cleanup = True
                                failure = _filesystem_error(exc, "Could not commit upload")
                            else:
                                self._uploads.pop(upload.upload_id, None)
                                upload.cancelled = True
                                return {
                                    "path": str(upload.destination),
                                    "size": upload.size,
                                    "sha256": expected_digest,
                                }
            upload.cancelled = True

        if cleanup:
            await self._forget_upload(upload)
            await asyncio.to_thread(_unlink_if_exists, upload.temporary)
        raise failure

    async def _exec_start(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"argv", "script", "cwd", "env", "timeout"})
        has_argv = "argv" in params
        has_script = "script" in params
        if has_argv == has_script:
            raise OperationError("INVALID_PARAMS", "Specify exactly one of argv or script")
        if has_argv:
            argv = _argv_param(params["argv"])
        else:
            script = _string_value(params["script"], "script", maximum=MAX_SCRIPT_CHARS)
            if not script:
                raise OperationError("INVALID_PARAMS", "script must not be empty")
            if os.name != "nt":
                raise OperationError("UNSUPPORTED", "PowerShell scripts require an interactive Windows endpoint")
            # No shell, profile, execution-policy, elevation, or UAC bypass is added.
            argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]
        _validate_windows_command_line(argv)
        cwd = _optional_path_param(params, "cwd")
        if cwd is not None:
            try:
                is_directory = await asyncio.to_thread(Path.is_dir, cwd)
            except OSError as exc:
                _raise_filesystem_error(exc, "Could not inspect cwd")
            if not is_directory:
                raise OperationError("NOT_DIRECTORY", "cwd must name an existing directory")
        environment = _environment_param(params)
        timeout = _number(params, "timeout", default=DEFAULT_EXEC_TIMEOUT, minimum=0.001, maximum=MAX_EXEC_TIMEOUT)
        job_id = secrets.token_urlsafe(18)
        stdout_log = self.data_dir / "logs" / f"{job_id}.stdout.log"
        stderr_log = self.data_dir / "logs" / f"{job_id}.stderr.log"

        async with self._state_lock:
            self._ensure_open()
            if len(self._jobs) + self._pending_job_starts >= MAX_ACTIVE_JOBS:
                raise OperationError("RESOURCE_LIMIT", "Too many active process jobs")
            self._pending_job_starts += 1
            self._pending_starts += 1
            self._no_pending_starts.clear()

        process: ContainedProcess | None = None
        logs_prepared = False
        registered = False
        try:
            log_task = asyncio.create_task(asyncio.to_thread(_prepare_logs, stdout_log, stderr_log))
            try:
                await asyncio.shield(log_task)
                logs_prepared = True
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await log_task
                    logs_prepared = True
                raise
            except OSError as exc:
                raise _filesystem_error(exc, "Could not prepare local process logs") from exc

            launch_task = asyncio.create_task(
                start_contained_process(argv, cwd=str(cwd) if cwd is not None else None, environment=environment)
            )
            try:
                process = await asyncio.shield(launch_task)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    process = await launch_task
                raise
            except FileNotFoundError as exc:
                raise OperationError("NOT_FOUND", "Program was not found") from exc
            except PermissionError as exc:
                raise OperationError("ACCESS_DENIED", "Program could not be started with this user account") from exc
            except (OSError, ProcessLaunchError) as exc:
                raise OperationError("EXEC_FAILED", "Could not start process") from exc
            async with self._state_lock:
                if not self._closed:
                    job = _Job(job_id, process, argv, time.time(), timeout, stdout_log, stderr_log)
                    self._jobs[job_id] = job
                    job.pumps = [
                        asyncio.create_task(self._pump_output(job, "stdout")),
                        asyncio.create_task(self._pump_output(job, "stderr")),
                    ]
                    job.watcher = asyncio.create_task(self._watch_job(job))
                    registered = True
            if not registered:
                raise OperationError("SESSION_CLOSED", "The controller session has ended")
            return {
                "job_id": job.job_id,
                "pid": job.process.pid,
                "stdout_log": str(job.stdout_log),
                "stderr_log": str(job.stderr_log),
            }
        except BaseException:
            if not registered and process is not None:
                await self._dispose_unregistered_process(process)
            if not registered and logs_prepared:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(_remove_logs, stdout_log, stderr_log)
            raise
        finally:
            await asyncio.shield(self._release_job_start_slot())

    async def _exec_stdin(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"job_id", "data", "eof"})
        job_id = _string_param(params, "job_id", maximum=128)
        data = _data_param(params, "data")
        eof = _boolean(params, "eof", default=False)
        job = await self._get_job(job_id)
        async with job.stdin_lock:
            if self._closed:
                raise OperationError("SESSION_CLOSED", "The controller session has ended")
            if job.process.returncode is not None:
                raise OperationError("PROCESS_EXITED", "Process has already exited")
            if job.stdin_closed:
                raise OperationError("PROCESS_EXITED", "Process standard input is already closed")
            try:
                if data:
                    await job.process.write_stdin(data)
                if eof:
                    await job.process.close_stdin()
                    job.stdin_closed = True
            except (BrokenPipeError, ConnectionResetError):
                job.stdin_closed = True
                raise OperationError("PROCESS_EXITED", "Process closed standard input") from None
        return {"job_id": job.job_id, "written": len(data), "eof": job.stdin_closed}

    async def _exec_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"job_id"})
        job_id = _string_param(params, "job_id", maximum=128)
        job = await self._get_job(job_id)
        if job.process.returncode is not None:
            raise OperationError("PROCESS_EXITED", "Process has already exited")
        await self._stop_job(job, cancelled=True)
        return {"job_id": job.job_id, "cancelling": True}

    async def _exec_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        _only(params, set())
        async with self._state_lock:
            jobs = list(self._jobs.values())
        return [
            {
                "job_id": job.job_id,
                "pid": job.process.pid,
                "argv": list(job.argv),
                "started": job.started,
                "timeout": job.timeout,
                "running": job.process.returncode is None,
                "stdout_log": str(job.stdout_log),
                "stderr_log": str(job.stderr_log),
            }
            for job in sorted(jobs, key=lambda item: item.started)
        ]

    async def _screen_capture(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, set())
        self._require_windows_desktop()
        directory = self.data_dir / "screens"
        path = directory / f"capture-{int(time.time() * 1000)}-{secrets.token_hex(8)}.png"
        try:
            width, height = await asyncio.to_thread(_capture_screen, path)
        except _PillowUnavailable:
            raise OperationError("UNSUPPORTED", "screen.capture requires the optional Windows Pillow package") from None
        except Exception as exc:
            raise OperationError("DESKTOP_UNAVAILABLE", "Could not capture the interactive Windows desktop") from exc
        return {"path": str(path), "width": width, "height": height}

    async def _input_mouse(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"action", "x", "y", "button", "delta"})
        desktop = self._require_windows_desktop()
        action = _string_param(params, "action", maximum=32).lower()
        if action not in {"move", "down", "up", "click", "double_click", "scroll"}:
            raise OperationError("INVALID_PARAMS", "Unsupported mouse action")
        coordinates = _optional_coordinates(params)
        if action == "move" and coordinates is None:
            raise OperationError("INVALID_PARAMS", "move requires x and y")
        if action in {"down", "up", "click", "double_click"}:
            button = _string_param(params, "button", default="left", maximum=16).lower()
            if button not in _MOUSE_BUTTONS:
                raise OperationError("INVALID_PARAMS", "button must be left, right, or middle")
            if "delta" in params:
                raise OperationError("INVALID_PARAMS", "delta is valid only for scroll")
        else:
            button = None
            if "button" in params:
                raise OperationError("INVALID_PARAMS", "button is valid only for button actions")
        if action == "scroll":
            delta = _integer(params, "delta", minimum=-(1 << 31), maximum=(1 << 31) - 1)
            if "button" in params:
                raise OperationError("INVALID_PARAMS", "button is not valid for scroll")
        else:
            delta = None
            if "delta" in params:
                raise OperationError("INVALID_PARAMS", "delta is valid only for scroll")

        async with self._input_lock:
            if self._closed:
                raise OperationError("SESSION_CLOSED", "The controller session has ended")
            if action == "down" and button in self._held_buttons:
                raise OperationError("INPUT_STATE", "That mouse button is already held by this session")
            if action == "up" and button not in self._held_buttons:
                raise OperationError("INPUT_STATE", "That mouse button is not held by this session")
            if action in {"click", "double_click"} and button in self._held_buttons:
                raise OperationError("INPUT_STATE", "Release the held mouse button before clicking it")
            try:
                position = await asyncio.to_thread(desktop.mouse, action, coordinates, button, delta)
            except OSError as exc:
                raise OperationError("DESKTOP_UNAVAILABLE", "Windows did not accept mouse input") from exc
            if action == "down":
                self._held_buttons.add(button)
            elif action == "up":
                self._held_buttons.discard(button)
        result: dict[str, Any] = {"action": action}
        if position is not None:
            result["position"] = {"x": position[0], "y": position[1]}
        return result

    async def _input_key(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"key", "action"})
        desktop = self._require_windows_desktop()
        key = _canonical_key(_string_param(params, "key", maximum=32).lower())
        action = _string_param(params, "action", maximum=16).lower()
        if action not in {"press", "down", "up"}:
            raise OperationError("INVALID_PARAMS", "action must be press, down, or up")
        key_info = _virtual_key(key)
        if key_info is None:
            raise OperationError("INVALID_PARAMS", "Unsupported key; use input.text for ordinary Unicode text")

        async with self._input_lock:
            if self._closed:
                raise OperationError("SESSION_CLOSED", "The controller session has ended")
            held = key in self._held_keys
            if action == "down" and held:
                raise OperationError("INPUT_STATE", "That key is already held by this session")
            if action == "up" and not held:
                raise OperationError("INPUT_STATE", "That key is not held by this session")
            if action == "press" and held:
                raise OperationError("INPUT_STATE", "Release the held key before pressing it")
            try:
                await asyncio.to_thread(desktop.key, key_info, action)
            except OSError as exc:
                raise OperationError("DESKTOP_UNAVAILABLE", "Windows did not accept keyboard input") from exc
            if action == "down":
                self._held_keys[key] = key_info
            elif action == "up":
                self._held_keys.pop(key, None)
        return {"key": key, "action": action}

    async def _input_text(self, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, {"text"})
        desktop = self._require_windows_desktop()
        text = _string_param(params, "text", maximum=MAX_TEXT_CHARS, allow_empty=True)
        try:
            units = list(memoryview(text.encode("utf-16-le")).cast("H"))
        except UnicodeEncodeError as exc:
            raise OperationError("INVALID_PARAMS", "text contains an invalid Unicode surrogate") from exc
        if len(units) > MAX_TEXT_CHARS * 2:
            raise OperationError("INVALID_PARAMS", "text is too long")
        async with self._input_lock:
            if self._closed:
                raise OperationError("SESSION_CLOSED", "The controller session has ended")
            try:
                await asyncio.to_thread(desktop.text, units)
            except OSError as exc:
                raise OperationError("DESKTOP_UNAVAILABLE", "Windows did not accept Unicode text input") from exc
        return {"written": len(text)}

    async def _get_upload(self, upload_id: str) -> _Upload:
        async with self._state_lock:
            upload = self._uploads.get(upload_id)
        if upload is None:
            raise OperationError("UPLOAD_NOT_FOUND", "Unknown or finished upload")
        return upload

    async def _forget_upload(self, upload: _Upload) -> None:
        async with self._state_lock:
            if self._uploads.get(upload.upload_id) is upload:
                self._uploads.pop(upload.upload_id, None)

    async def _discard_upload(self, upload: _Upload) -> None:
        async with upload.lock:
            upload.cancelled = True
            await asyncio.to_thread(_unlink_if_exists, upload.temporary)

    async def _get_job(self, job_id: str) -> _Job:
        async with self._state_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise OperationError("JOB_NOT_FOUND", "Unknown or finished process job")
        return job

    async def _release_upload_start_slot(self) -> None:
        async with self._state_lock:
            self._pending_upload_starts -= 1
            self._pending_starts -= 1
            if self._pending_starts == 0:
                self._no_pending_starts.set()

    async def _release_job_start_slot(self) -> None:
        async with self._state_lock:
            self._pending_job_starts -= 1
            self._pending_starts -= 1
            if self._pending_starts == 0:
                self._no_pending_starts.set()

    async def _dispose_unregistered_process(self, process: ContainedProcess) -> None:
        with contextlib.suppress(Exception):
            await process.terminate_tree()
        with contextlib.suppress(Exception):
            await process.wait()
        with contextlib.suppress(Exception):
            await process.close()

    async def _stop_job(self, job: _Job, *, cancelled: bool) -> None:
        async with job.cancellation_lock:
            if cancelled:
                job.cancelled = True
            with contextlib.suppress(Exception):
                await job.process.close_stdin()
            if job.process.returncode is None:
                with contextlib.suppress(Exception):
                    await job.process.terminate_tree()

    async def _pump_output(self, job: _Job, stream: str) -> None:
        log_path = job.stdout_log if stream == "stdout" else job.stderr_log
        try:
            while True:
                data = await job.process.read_output(stream, OUTPUT_CHUNK_BYTES)
                if not data:
                    return
                try:
                    await asyncio.to_thread(_append_bounded_log, log_path, data)
                except OSError:
                    # Process execution still remains observable through encrypted events.
                    pass
                await self._safe_emit(
                    {"event": "exec.output", "job_id": job.job_id, "stream": stream, "data": _b64encode(data)}
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A closed pipe or a failed transport must not leave the process unmanaged.
            return

    async def _watch_job(self, job: _Job) -> None:
        exit_code: int | None = None
        try:
            try:
                exit_code = await asyncio.wait_for(job.process.wait(), timeout=job.timeout)
            except TimeoutError:
                job.timed_out = True
                await self._stop_job(job, cancelled=False)
                exit_code = await job.process.wait()
        except asyncio.CancelledError:
            job.cancelled = True
            await self._stop_job(job, cancelled=True)
            with contextlib.suppress(Exception):
                exit_code = await job.process.wait()
            raise
        except Exception:
            job.cancelled = True
            await self._stop_job(job, cancelled=True)
            with contextlib.suppress(Exception):
                exit_code = await job.process.wait()
        finally:
            with contextlib.suppress(Exception):
                await job.process.close_stdin()
            if job.pumps:
                await asyncio.gather(*job.pumps, return_exceptions=True)
            if exit_code is None:
                exit_code = job.process.returncode
            if exit_code is None:
                exit_code = -1
            async with self._state_lock:
                if self._jobs.get(job.job_id) is job:
                    self._jobs.pop(job.job_id, None)
            await self._safe_emit(
                {
                    "event": "exec.exit",
                    "job_id": job.job_id,
                    "exit_code": exit_code,
                    "cancelled": job.cancelled,
                    "timed_out": job.timed_out,
                }
            )
            with contextlib.suppress(Exception):
                await job.process.close()

    async def _safe_emit(self, event: dict[str, Any]) -> None:
        try:
            await self._emit(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The owning session handles relay disconnects and will call close().
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise OperationError("SESSION_CLOSED", "The controller session has ended")

    def _require_windows_desktop(self) -> "_WindowsDesktop":
        if os.name != "nt":
            raise OperationError("UNSUPPORTED", "Desktop operations require an interactive Windows endpoint")
        if self._desktop is None:
            try:
                self._desktop = _WindowsDesktop()
            except Exception as exc:
                raise OperationError("DESKTOP_UNAVAILABLE", "Windows desktop APIs are unavailable") from exc
        return self._desktop

    async def _release_held_input(self) -> None:
        async with self._input_lock:
            keys = list(self._held_keys.values())
            buttons = list(self._held_buttons)
            self._held_keys.clear()
            self._held_buttons.clear()
            if not keys and not buttons:
                return
            if self._desktop is None:
                return
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._desktop.release, keys, buttons)


def _only(params: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(params).difference(allowed)
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise OperationError("INVALID_PARAMS", f"Unexpected parameter(s): {names}")


def _string_value(value: Any, name: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise OperationError("INVALID_PARAMS", f"{name} must be a string")
    if not allow_empty and not value:
        raise OperationError("INVALID_PARAMS", f"{name} must not be empty")
    if len(value) > maximum:
        raise OperationError("INVALID_PARAMS", f"{name} is too long")
    if "\0" in value:
        raise OperationError("INVALID_PARAMS", f"{name} must not contain NUL")
    return value


def _string_param(
    params: Mapping[str, Any], name: str, *, default: str | None = None, maximum: int, allow_empty: bool = False
) -> str:
    if name not in params:
        if default is None:
            raise OperationError("INVALID_PARAMS", f"Missing required parameter: {name}")
        return default
    return _string_value(params[name], name, maximum=maximum, allow_empty=allow_empty)


def _path_param(params: Mapping[str, Any], name: str) -> Path:
    value = _string_param(params, name, maximum=MAX_PATH_CHARS)
    try:
        return _absolute_path(Path(value).expanduser())
    except (OSError, RuntimeError) as exc:
        raise OperationError("INVALID_PARAMS", f"Invalid path: {name}") from exc


def _optional_path_param(params: Mapping[str, Any], name: str) -> Path | None:
    if name not in params:
        return None
    return _path_param(params, name)


def _absolute_path(path: Path) -> Path:
    # Do not resolve: move/remove must act on a symlink itself, not its target.
    return Path(os.path.abspath(os.fspath(path)))


def _integer(
    params: Mapping[str, Any], name: str, *, default: int | None = None, minimum: int, maximum: int
) -> int:
    if name not in params:
        if default is None:
            raise OperationError("INVALID_PARAMS", f"Missing required parameter: {name}")
        return default
    value = params[name]
    if type(value) is not int or value < minimum or value > maximum:
        raise OperationError("INVALID_PARAMS", f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _number(
    params: Mapping[str, Any], name: str, *, default: float, minimum: float, maximum: float
) -> float:
    if name not in params:
        return default
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise OperationError("INVALID_PARAMS", f"{name} must be a finite number")
    value = float(value)
    if value < minimum or value > maximum:
        raise OperationError("INVALID_PARAMS", f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _boolean(params: Mapping[str, Any], name: str, *, default: bool) -> bool:
    if name not in params:
        return default
    if type(params[name]) is not bool:
        raise OperationError("INVALID_PARAMS", f"{name} must be a boolean")
    return params[name]


def _data_param(params: Mapping[str, Any], name: str) -> bytes:
    if name not in params:
        raise OperationError("INVALID_PARAMS", f"Missing required parameter: {name}")
    value = params[name]
    if not isinstance(value, str):
        raise OperationError("INVALID_PARAMS", f"{name} must be base64 text")
    maximum_encoded = 4 * ((MAX_TRANSFER_BYTES + 2) // 3)
    if len(value) > maximum_encoded:
        raise OperationError("INVALID_PARAMS", f"{name} exceeds the {MAX_TRANSFER_BYTES}-byte transfer limit")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise OperationError("INVALID_PARAMS", f"{name} must be valid standard base64") from exc
    if len(raw) > MAX_TRANSFER_BYTES:
        raise OperationError("INVALID_PARAMS", f"{name} exceeds the {MAX_TRANSFER_BYTES}-byte transfer limit")
    return raw


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _sha256_param(params: Mapping[str, Any], name: str) -> str:
    value = _string_param(params, name, maximum=64)
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise OperationError("INVALID_PARAMS", f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _argv_param(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise OperationError("INVALID_PARAMS", "argv must be a non-empty array of strings")
    if len(value) > MAX_EXEC_ARGS:
        raise OperationError("INVALID_PARAMS", "argv has too many arguments")
    argv: list[str] = []
    encoded_size = 0
    for item in value:
        argument = _string_value(item, "argv item", maximum=MAX_PATH_CHARS)
        argv.append(argument)
        encoded_size += len(argument.encode("utf-8")) + 1
    if encoded_size > MAX_EXEC_ARGUMENT_BYTES:
        raise OperationError("INVALID_PARAMS", "argv is too large")
    return argv


def _validate_windows_command_line(argv: list[str]) -> None:
    if os.name == "nt" and len(subprocess.list2cmdline(argv)) > MAX_PATH_CHARS:
        raise OperationError("INVALID_PARAMS", "argv exceeds the Windows command-line limit")


def _environment_param(params: Mapping[str, Any]) -> dict[str, str] | None:
    if "env" not in params:
        return None
    value = params["env"]
    if not isinstance(value, dict):
        raise OperationError("INVALID_PARAMS", "env must be an object of string values")
    if len(value) > MAX_ENV_ITEMS:
        raise OperationError("INVALID_PARAMS", "env has too many entries")
    result = dict(os.environ)
    size = 0
    for key, item in value.items():
        if not isinstance(key, str) or not key or "=" in key or "\0" in key:
            raise OperationError("INVALID_PARAMS", "env keys must be non-empty environment variable names")
        text = _string_value(item, f"env.{key}", maximum=MAX_EXEC_ARGUMENT_BYTES)
        size += len(key.encode("utf-8")) + len(text.encode("utf-8")) + 2
        if size > MAX_ENV_BYTES:
            raise OperationError("INVALID_PARAMS", "env is too large")
        result[key] = text
    return result


def _optional_coordinates(params: Mapping[str, Any]) -> tuple[int, int] | None:
    has_x = "x" in params
    has_y = "y" in params
    if has_x != has_y:
        raise OperationError("INVALID_PARAMS", "x and y must be supplied together")
    if not has_x:
        return None
    x = _integer(params, "x", minimum=-(1 << 31), maximum=(1 << 31) - 1)
    y = _integer(params, "y", minimum=-(1 << 31), maximum=(1 << 31) - 1)
    return x, y


def _path_kind(mode: int) -> str:
    if stat_module.S_ISREG(mode):
        return "file"
    if stat_module.S_ISDIR(mode):
        return "directory"
    if stat_module.S_ISLNK(mode):
        return "symlink"
    return "other"


def _metadata_from_stat(path: Path, info: os.stat_result) -> dict[str, Any]:
    return {
        "path": str(path),
        "type": _path_kind(info.st_mode),
        "size": info.st_size,
        "modified": info.st_mtime,
        "changed": info.st_ctime,
        "mode": stat_module.S_IMODE(info.st_mode),
    }


def _path_metadata(path: Path) -> dict[str, Any]:
    info = os.lstat(path)
    result = _metadata_from_stat(path, info)
    if stat_module.S_ISLNK(info.st_mode):
        try:
            result["link_target"] = os.readlink(path)
        except OSError:
            pass
    return result


def _list_directory(path: Path, offset: int, limit: int) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    encoded_bytes = 0
    truncated = False
    with os.scandir(path) as directory:
        iterator = iter(directory)
        for _ in range(offset):
            try:
                next(iterator)
            except StopIteration:
                return {
                    "path": str(path),
                    "offset": offset,
                    "entries": entries,
                    "truncated": False,
                    "next_offset": None,
                }
        for entry in iterator:
            entry_path = path / entry.name
            try:
                record = _metadata_from_stat(entry_path, entry.stat(follow_symlinks=False))
            except OSError:
                record = {
                    "path": str(entry_path),
                    "type": "unknown",
                    "size": None,
                    "modified": None,
                    "changed": None,
                    "mode": None,
                }
            record["name"] = entry.name
            record_size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if entries and (len(entries) >= limit or encoded_bytes + record_size > MAX_LIST_RESPONSE_BYTES):
                truncated = True
                break
            entries.append(record)
            encoded_bytes += record_size
            if len(entries) >= limit:
                try:
                    next(iterator)
                except StopIteration:
                    pass
                else:
                    truncated = True
                break
    next_offset = offset + len(entries) if truncated else None
    return {
        "path": str(path),
        "offset": offset,
        "entries": entries,
        "truncated": truncated,
        "next_offset": next_offset,
    }


def _read_regular_file(path: Path, offset: int, length: int) -> tuple[bytes, bool]:
    if not stat_module.S_ISREG(os.stat(path).st_mode):
        raise _NotRegularFile()
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat_module.S_ISREG(info.st_mode):
            raise _NotRegularFile()
        stream.seek(offset)
        data = stream.read(length)
        eof = stream.tell() >= os.fstat(stream.fileno()).st_size
    return data, eof


def _atomic_write(path: Path, data: bytes, create_parents: bool) -> int:
    parent = path.parent
    if create_parents:
        parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    try:
        existing_mode = stat_module.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".agentbridge-write.tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            _write_all(stream, data)
            stream.flush()
            os.fsync(stream.fileno())
        if existing_mode is not None:
            os.chmod(temporary, existing_mode)
        os.replace(temporary, path)
    except Exception:
        _unlink_if_exists(temporary)
        raise
    return len(data)


def _move_path(source: Path, destination: Path) -> None:
    if not os.path.lexists(source):
        raise FileNotFoundError(os.fspath(source))
    if os.path.lexists(destination):
        raise FileExistsError(os.fspath(destination))
    try:
        os.rename(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(os.fspath(source), os.fspath(destination))


def _remove_path(path: Path, recursive: bool) -> None:
    info = os.lstat(path)
    if stat_module.S_ISDIR(info.st_mode) and not stat_module.S_ISLNK(info.st_mode):
        if recursive:
            shutil.rmtree(path)
        else:
            os.rmdir(path)
    else:
        os.unlink(path)


def _create_upload_file(destination: Path) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.agentbridge-upload-", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    return Path(temporary_name)


def _write_upload_chunk(path: Path, offset: int, data: bytes) -> None:
    with path.open("r+b", buffering=0) as stream:
        stream.seek(offset)
        _write_all(stream, data)
        stream.flush()


def _verify_upload_file(path: Path, size: int, expected_digest: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb+") as stream:
        stream.flush()
        os.fsync(stream.fileno())
        info = os.fstat(stream.fileno())
        if info.st_size != size:
            raise _UploadSizeMismatch()
        while block := stream.read(OUTPUT_CHUNK_BYTES):
            digest.update(block)
    if not hmac.compare_digest(digest.hexdigest(), expected_digest):
        raise _UploadDigestMismatch()


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _prepare_logs(stdout_log: Path, stderr_log: Path) -> None:
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        for path in (stdout_log, stderr_log):
            with path.open("xb"):
                pass
            created.append(path)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
    except Exception:
        for path in created:
            _unlink_if_exists(path)
        raise


def _remove_logs(stdout_log: Path, stderr_log: Path) -> None:
    _unlink_if_exists(stdout_log)
    _unlink_if_exists(stderr_log)


def _append_bounded_log(path: Path, data: bytes) -> None:
    try:
        existing_size = path.stat().st_size
    except FileNotFoundError:
        return
    remaining = MAX_LOG_BYTES_PER_STREAM - existing_size
    if remaining <= 0:
        return
    with path.open("ab", buffering=0) as stream:
        _write_all(stream, data[:remaining])


def _write_all(stream, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not written:
            raise OSError("Short write")
        view = view[written:]


def _filesystem_error(exc: OSError, action: str) -> OperationError:
    if isinstance(exc, FileNotFoundError):
        return OperationError("NOT_FOUND", f"{action}: path does not exist")
    if isinstance(exc, FileExistsError):
        return OperationError("ALREADY_EXISTS", f"{action}: destination already exists")
    if isinstance(exc, PermissionError):
        return OperationError("ACCESS_DENIED", f"{action}: access was denied")
    if isinstance(exc, IsADirectoryError):
        return OperationError("IS_DIRECTORY", f"{action}: expected a file")
    if isinstance(exc, NotADirectoryError):
        return OperationError("NOT_DIRECTORY", f"{action}: a path component is not a directory")
    if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
        return OperationError("DIRECTORY_NOT_EMPTY", f"{action}: directory is not empty")
    return OperationError("IO_ERROR", f"{action} failed")


def _raise_filesystem_error(exc: OSError, action: str) -> None:
    raise _filesystem_error(exc, action) from exc


def _is_elevated() -> bool:
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def _pillow_available() -> bool:
    try:
        from PIL import ImageGrab  # noqa: F401
    except ImportError:
        return False
    return True


def _capture_screen(path: Path) -> tuple[int, int]:
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise _PillowUnavailable() from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    image = ImageGrab.grab()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        image.save(temporary, format="PNG")
        os.replace(temporary, path)
    except Exception:
        _unlink_if_exists(temporary)
        raise
    return image.width, image.height


_MOUSE_BUTTONS = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}

_SPECIAL_KEYS: dict[str, tuple[int, bool]] = {
    "backspace": (0x08, False),
    "tab": (0x09, False),
    "enter": (0x0D, False),
    "return": (0x0D, False),
    "shift": (0x10, False),
    "ctrl": (0x11, False),
    "control": (0x11, False),
    "alt": (0x12, False),
    "pause": (0x13, False),
    "capslock": (0x14, False),
    "escape": (0x1B, False),
    "esc": (0x1B, False),
    "space": (0x20, False),
    "pageup": (0x21, True),
    "pagedown": (0x22, True),
    "end": (0x23, True),
    "home": (0x24, True),
    "left": (0x25, True),
    "up": (0x26, True),
    "right": (0x27, True),
    "down": (0x28, True),
    "insert": (0x2D, True),
    "delete": (0x2E, True),
    "win": (0x5B, True),
}

_KEY_ALIASES = {"ctrl": "control", "return": "enter", "esc": "escape"}


def _canonical_key(key: str) -> str:
    return _KEY_ALIASES.get(key, key)


def _virtual_key(key: str) -> tuple[int, bool] | None:
    if key in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[key]
    if len(key) == 2 and key.startswith("f") and key[1:].isdigit():
        number = int(key[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1, False
    if len(key) == 1 and ("a" <= key <= "z" or "0" <= key <= "9"):
        return ord(key.upper()), False
    return None


class _WindowsDesktop:
    """Minimal explicit desktop input wrapper, constructed only on Windows."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._ULONG_PTR = ctypes.c_size_t

        class _MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class _KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class _HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

        class _INPUT(ctypes.Structure):
            _anonymous_ = ("union",)
            _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

        self._MOUSEINPUT = _MOUSEINPUT
        self._KEYBDINPUT = _KEYBDINPUT
        self._INPUT = _INPUT
        self._POINT = wintypes.POINT
        self._user32.SetCursorPos.argtypes = [wintypes.INT, wintypes.INT]
        self._user32.SetCursorPos.restype = wintypes.BOOL
        self._user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self._user32.GetCursorPos.restype = wintypes.BOOL
        self._user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        self._user32.SendInput.restype = wintypes.UINT

    def mouse(
        self, action: str, coordinates: tuple[int, int] | None, button: str | None, delta: int | None
    ) -> tuple[int, int] | None:
        if coordinates is not None and not self._user32.SetCursorPos(*coordinates):
            raise OSError(self._ctypes.get_last_error(), "SetCursorPos failed")
        inputs = []
        if action in {"down", "up", "click", "double_click"}:
            down, up = _MOUSE_BUTTONS[button]
            if action == "down":
                inputs.append(self._mouse_input(down))
            elif action == "up":
                inputs.append(self._mouse_input(up))
            else:
                repetitions = 2 if action == "double_click" else 1
                for _ in range(repetitions):
                    inputs.extend((self._mouse_input(down), self._mouse_input(up)))
        elif action == "scroll":
            inputs.append(self._mouse_input(0x0800, delta))
        if inputs:
            self._send(inputs)
        point = self._POINT()
        if not self._user32.GetCursorPos(self._ctypes.byref(point)):
            raise OSError(self._ctypes.get_last_error(), "GetCursorPos failed")
        return point.x, point.y

    def key(self, key_info: tuple[int, bool], action: str) -> None:
        virtual_key, extended = key_info
        down = self._key_input(virtual_key, extended, up=False)
        up = self._key_input(virtual_key, extended, up=True)
        if action == "down":
            self._send([down])
        elif action == "up":
            self._send([up])
        else:
            self._send([down, up])

    def text(self, units: list[int]) -> None:
        for start in range(0, len(units), 64):
            inputs = []
            for unit in units[start : start + 64]:
                inputs.append(self._unicode_input(unit, up=False))
                inputs.append(self._unicode_input(unit, up=True))
            if inputs:
                self._send(inputs)

    def release(self, keys: list[tuple[int, bool]], buttons: list[str]) -> None:
        inputs = [self._key_input(key, extended, up=True) for key, extended in keys]
        inputs.extend(self._mouse_input(_MOUSE_BUTTONS[button][1]) for button in buttons)
        if inputs:
            self._send(inputs)

    def _mouse_input(self, flags: int, delta: int | None = None):
        value = self._ctypes.c_uint32(delta or 0).value
        item = self._INPUT()
        item.type = 0
        item.mi = self._MOUSEINPUT(0, 0, value, flags, 0, 0)
        return item

    def _key_input(self, virtual_key: int, extended: bool, *, up: bool):
        flags = (0x0001 if extended else 0) | (0x0002 if up else 0)
        item = self._INPUT()
        item.type = 1
        item.ki = self._KEYBDINPUT(virtual_key, 0, flags, 0, 0)
        return item

    def _unicode_input(self, unit: int, *, up: bool):
        flags = 0x0004 | (0x0002 if up else 0)
        item = self._INPUT()
        item.type = 1
        item.ki = self._KEYBDINPUT(0, unit, flags, 0, 0)
        return item

    def _send(self, values) -> None:
        items = (self._INPUT * len(values))(*values)
        sent = self._user32.SendInput(len(values), items, self._ctypes.sizeof(self._INPUT))
        if sent != len(values):
            raise OSError(self._ctypes.get_last_error(), "SendInput failed")
