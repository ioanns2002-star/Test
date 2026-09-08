"""Subprocess containment used by the endpoint's explicit exec operations.

Windows children are placed in a Job Object so a disconnect terminates the whole
child tree. POSIX uses a fresh process group for the same lifecycle guarantee.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from typing import Mapping, Sequence


class ProcessLaunchError(RuntimeError):
    """A process could not be launched with the required containment."""


class ContainedProcess:
    """Small async interface shared by native Windows and asyncio subprocesses."""

    pid: int

    @property
    def returncode(self) -> int | None:
        raise NotImplementedError

    async def read_output(self, stream: str, size: int) -> bytes:
        raise NotImplementedError

    async def write_stdin(self, data: bytes) -> None:
        raise NotImplementedError

    async def close_stdin(self) -> None:
        raise NotImplementedError

    async def wait(self) -> int:
        raise NotImplementedError

    async def terminate_tree(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class _AsyncioContainedProcess(ContainedProcess):
    def __init__(self, process: asyncio.subprocess.Process):
        self._process = process
        self.pid = process.pid
        self._termination_lock = asyncio.Lock()

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def read_output(self, stream: str, size: int) -> bytes:
        reader = self._process.stdout if stream == "stdout" else self._process.stderr
        if reader is None:
            return b""
        return await reader.read(size)

    async def write_stdin(self, data: bytes) -> None:
        if self._process.stdin is None or self._process.stdin.is_closing():
            raise BrokenPipeError("Process standard input is closed")
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def close_stdin(self) -> None:
        stream = self._process.stdin
        if stream is None or stream.is_closing():
            return
        stream.close()
        try:
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def wait(self) -> int:
        return await self._process.wait()

    async def terminate_tree(self) -> None:
        async with self._termination_lock:
            if self.returncode is not None:
                return
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(asyncio.shield(self._process.wait()), timeout=2)
                return
            except TimeoutError:
                pass
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def close(self) -> None:
        await self.close_stdin()


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _HANDLE_FLAG_INHERIT = 0x00000001
    _STARTF_USESTDHANDLES = 0x00000100
    _CREATE_SUSPENDED = 0x00000004
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _INFINITE = 0xFFFFFFFF
    _WAIT_OBJECT_0 = 0
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    ]
    _kernel32.CreatePipe.restype = wintypes.BOOL
    _kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    _kernel32.SetHandleInformation.restype = wintypes.BOOL
    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    _kernel32.CreateProcessW.restype = wintypes.BOOL
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL

    def _handle_value(handle) -> int:
        return int(ctypes.cast(handle, ctypes.c_void_p).value or 0)

    def _raise_winerror(message: str) -> None:
        error = ctypes.get_last_error()
        detail = ctypes.WinError(error) if error else OSError(message)
        raise ProcessLaunchError(f"{message}: {detail}")

    def _close_handle(handle) -> None:
        if _handle_value(handle):
            _kernel32.CloseHandle(handle)

    class _WindowsJob:
        def __init__(self):
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                _raise_winerror("Could not create Windows Job Object")
            self.handle = handle
            limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                self.handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                error = ctypes.get_last_error()
                _close_handle(self.handle)
                self.handle = wintypes.HANDLE()
                raise ProcessLaunchError(f"Could not configure Windows Job Object: {ctypes.WinError(error)}")

        def assign(self, process_handle) -> None:
            if not _kernel32.AssignProcessToJobObject(self.handle, process_handle):
                _raise_winerror("Could not assign process to Windows Job Object")

        def terminate(self) -> None:
            if _handle_value(self.handle):
                _kernel32.TerminateJobObject(self.handle, 1)

        def close(self) -> None:
            if _handle_value(self.handle):
                _close_handle(self.handle)
                self.handle = wintypes.HANDLE()

    class _WindowsContainedProcess(ContainedProcess):
        def __init__(self, process_handle, job: _WindowsJob, pid: int, stdin, stdout, stderr):
            self._process_handle = process_handle
            self._job = job
            self.pid = pid
            self._stdin = stdin
            self._stdout = stdout
            self._stderr = stderr
            self._returncode: int | None = None
            self._wait_task: asyncio.Task[int] | None = None
            self._termination_lock = asyncio.Lock()
            self._close_lock = asyncio.Lock()
            self._closed = False

        @property
        def returncode(self) -> int | None:
            return self._returncode

        async def read_output(self, stream: str, size: int) -> bytes:
            file = self._stdout if stream == "stdout" else self._stderr
            if file is None:
                return b""
            return await asyncio.to_thread(file.read, size)

        async def write_stdin(self, data: bytes) -> None:
            if self._stdin is None:
                raise BrokenPipeError("Process standard input is closed")
            await asyncio.to_thread(_write_all, self._stdin, data)

        async def close_stdin(self) -> None:
            if self._stdin is None:
                return
            stream, self._stdin = self._stdin, None
            await asyncio.to_thread(_close_file, stream)

        async def _wait(self) -> int:
            code = await asyncio.to_thread(_wait_for_windows_process, self._process_handle)
            self._returncode = code
            return code

        async def wait(self) -> int:
            if self._wait_task is None:
                self._wait_task = asyncio.create_task(self._wait())
            return await asyncio.shield(self._wait_task)

        async def terminate_tree(self) -> None:
            async with self._termination_lock:
                if self.returncode is None:
                    await asyncio.to_thread(self._job.terminate)

        async def close(self) -> None:
            async with self._close_lock:
                if self._closed:
                    return
                self._closed = True
                await self.close_stdin()
                for attribute in ("_stdout", "_stderr"):
                    stream = getattr(self, attribute)
                    if stream is not None:
                        setattr(self, attribute, None)
                        await asyncio.to_thread(_close_file, stream)
                await asyncio.to_thread(self._job.close)
                if _handle_value(self._process_handle):
                    await asyncio.to_thread(_close_handle, self._process_handle)
                    self._process_handle = wintypes.HANDLE()

    def _write_all(stream, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = stream.write(view)
            if not written:
                raise BrokenPipeError("Could not write to process standard input")
            view = view[written:]

    def _close_file(stream) -> None:
        try:
            stream.close()
        except OSError:
            pass

    def _wait_for_windows_process(process_handle) -> int:
        status = _kernel32.WaitForSingleObject(process_handle, _INFINITE)
        if status != _WAIT_OBJECT_0:
            _raise_winerror("Could not wait for process")
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(process_handle, ctypes.byref(code)):
            _raise_winerror("Could not read process exit code")
        return int(code.value)

    def _make_pipe(attributes: _SECURITY_ATTRIBUTES):
        read_handle = wintypes.HANDLE()
        write_handle = wintypes.HANDLE()
        if not _kernel32.CreatePipe(ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(attributes), 0):
            _raise_winerror("Could not create process pipe")
        return read_handle, write_handle

    def _clear_inherit(handle) -> None:
        if not _kernel32.SetHandleInformation(handle, _HANDLE_FLAG_INHERIT, 0):
            _raise_winerror("Could not secure process pipe")

    def _environment_block(environment: Mapping[str, str] | None):
        if environment is None:
            return None
        values = [f"{key}={value}" for key, value in environment.items()]
        values.sort(key=str.upper)
        return ctypes.create_unicode_buffer("\0".join(values) + "\0\0")

    def _open_pipe(handle, mode: str):
        value = _handle_value(handle)
        flags = os.O_BINARY | (os.O_WRONLY if "w" in mode else os.O_RDONLY)
        descriptor = msvcrt.open_osfhandle(value, flags)
        try:
            return os.fdopen(descriptor, mode, buffering=0)
        except Exception:
            os.close(descriptor)
            raise

    def _create_windows_process(argv: Sequence[str], cwd: str | None, environment: Mapping[str, str] | None):
        job = _WindowsJob()
        stdin_read = stdin_write = stdout_read = stdout_write = stderr_read = stderr_write = None
        process_info = _PROCESS_INFORMATION()
        stdin = stdout = stderr = None
        try:
            attributes = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
            stdin_read, stdin_write = _make_pipe(attributes)
            stdout_read, stdout_write = _make_pipe(attributes)
            stderr_read, stderr_write = _make_pipe(attributes)
            _clear_inherit(stdin_write)
            _clear_inherit(stdout_read)
            _clear_inherit(stderr_read)

            startup = _STARTUPINFOW()
            startup.cb = ctypes.sizeof(startup)
            startup.dwFlags = _STARTF_USESTDHANDLES
            startup.hStdInput = stdin_read
            startup.hStdOutput = stdout_write
            startup.hStdError = stderr_write
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
            environment_block = _environment_block(environment)
            environment_pointer = ctypes.cast(environment_block, wintypes.LPVOID) if environment_block else None
            flags = _CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP | _CREATE_UNICODE_ENVIRONMENT
            if not _kernel32.CreateProcessW(
                None,
                command_line,
                None,
                None,
                True,
                flags,
                environment_pointer,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(process_info),
            ):
                _raise_winerror("Could not create process")

            _close_handle(stdin_read)
            stdin_read = None
            _close_handle(stdout_write)
            stdout_write = None
            _close_handle(stderr_write)
            stderr_write = None

            # The child cannot execute until it is inside the kill-on-close Job Object.
            job.assign(process_info.hProcess)
            stdin = _open_pipe(stdin_write, "wb")
            stdin_write = None
            stdout = _open_pipe(stdout_read, "rb")
            stdout_read = None
            stderr = _open_pipe(stderr_read, "rb")
            stderr_read = None
            if _kernel32.ResumeThread(process_info.hThread) == _INFINITE:
                _raise_winerror("Could not resume contained process")
            _close_handle(process_info.hThread)
            process_info.hThread = wintypes.HANDLE()
            return process_info.hProcess, job, int(process_info.dwProcessId), stdin, stdout, stderr
        except Exception:
            if _handle_value(process_info.hProcess):
                _kernel32.TerminateProcess(process_info.hProcess, 1)
            job.terminate()
            for stream in (stdin, stdout, stderr):
                if stream is not None:
                    _close_file(stream)
            for handle in (stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write,
                           process_info.hThread, process_info.hProcess):
                if handle is not None:
                    _close_handle(handle)
            job.close()
            raise


async def start_contained_process(
    argv: Sequence[str], *, cwd: str | None = None, environment: Mapping[str, str] | None = None
) -> ContainedProcess:
    """Start an argv-only process that is killed with its descendants on close."""

    if os.name == "nt":
        process_handle, job, pid, stdin, stdout, stderr = await asyncio.to_thread(
            _create_windows_process, argv, cwd, environment
        )
        return _WindowsContainedProcess(process_handle, job, pid, stdin, stdout, stderr)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=environment,
        start_new_session=True,
    )
    return _AsyncioContainedProcess(process)
