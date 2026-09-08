"""Local command-line controller for an AgentBridge device session.

The relay is deliberately only a transport.  This module keeps controller
configuration on the local machine and makes one connection per CLI invocation
(or per ``rpc`` JSON-lines session); it never replays a request after a loss of
connection.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
from typing import Any, Callable, TextIO

from cryptography.fernet import Fernet

from .config import validate_config
from .connection import Controller


CHUNK_BYTES = 262_144
STDIN_CHUNK_BYTES = 65_536
_CONFIG_FIELDS = ("relay_url", "token", "peer_key", "allow_insecure_localhost")
_ENV_FIELDS = {
    "relay_url": "BRIDGE_URL",
    "token": "BRIDGE_CONTROLLER_TOKEN",
    "peer_key": "BRIDGE_PEER_KEY",
}
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


class CliError(Exception):
    """A concise, safe-to-display command-line error."""


def default_config_path() -> Path:
    """Return the local, user-scoped controller configuration path."""

    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "agentbridge" / "controller.json"


def _path_from_args(args: argparse.Namespace) -> Path:
    value = getattr(args, "config", None)
    return Path(value).expanduser() if value else default_config_path()


def _read_config(path: Path, *, required: bool, verify_private: bool = True) -> dict[str, Any]:
    """Read one local JSON file without ever echoing its contents."""

    try:
        entry = path.lstat()
    except FileNotFoundError:
        if required:
            raise CliError("Controller configuration was not found; run 'agentbridge configure'") from None
        return {}
    except OSError as exc:
        raise CliError(f"Cannot read controller configuration ({type(exc).__name__})") from exc

    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise CliError("Controller configuration must be a regular local file")
    if verify_private and os.name != "nt" and entry.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CliError("Controller configuration must be private (chmod 600 <config>)")
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(f"Cannot read controller configuration ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise CliError("Controller configuration must be a JSON object")
    return {name: value[name] for name in _CONFIG_FIELDS if name in value}


def _write_private_config(path: Path, value: dict[str, Any]) -> None:
    """Atomically save local configuration with Unix owner-only permissions."""

    payload = {name: value[name] for name in _CONFIG_FIELDS if name in value}
    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".controller-", suffix=".json", dir=parent)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                path.chmod(0o600)
        finally:
            if descriptor != -1:
                os.close(descriptor)
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise CliError(f"Cannot save controller configuration ({type(exc).__name__})") from exc


def _resolve_config(args: argparse.Namespace) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Merge file, environment, and explicit options without printing secrets."""

    explicit_file = bool(getattr(args, "config", None))
    config = _read_config(_path_from_args(args), required=explicit_file)
    for name, environment_name in _ENV_FIELDS.items():
        if environment_name in os.environ:
            config[name] = os.environ[environment_name]
    for name in _ENV_FIELDS:
        supplied = getattr(args, name, None)
        if supplied is not None:
            config[name] = supplied
    if getattr(args, "allow_insecure_localhost", False):
        config["allow_insecure_localhost"] = True
    try:
        checked = validate_config(config, "controller")
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    return checked, tuple(str(checked[name]) for name in ("token", "peer_key") if checked.get(name))


def _status(stream: TextIO, message: str) -> None:
    stream.write(f"agentbridge: {message}\n")
    stream.flush()


def _write_json(stream: TextIO, value: Any) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def _write_bytes(stream: TextIO, value: bytes) -> None:
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(value)
        binary.flush()
        return
    # StringIO is useful for tests and still makes text-oriented terminals usable.
    stream.write(value.decode("utf-8", errors="replace"))
    stream.flush()


def _decode_base64(value: Any, *, description: str) -> bytes:
    if not isinstance(value, str):
        raise CliError(f"Malformed {description}: data is not base64 text")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise CliError(f"Malformed {description}: invalid base64 data") from exc


def _json_object(value: str, *, name: str = "params") -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CliError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise CliError(f"{name} must be a JSON object")
    return parsed


def _positive_chunk(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= parsed <= CHUNK_BYTES:
        raise argparse.ArgumentTypeError(f"must be between 1 and {CHUNK_BYTES}")
    return parsed


def _read_length(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 0 <= parsed <= CHUNK_BYTES:
        raise argparse.ArgumentTypeError(f"must be between 0 and {CHUNK_BYTES}")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not parsed > 0 or parsed == float("inf"):
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def _sha256_value(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a 64-character SHA-256 hex digest")
    return value.lower()


def _config_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default: Any = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--config", metavar="FILE", default=default,
                        help="private local controller JSON file")
    parser.add_argument("--url", "--relay-url", dest="relay_url", metavar="URL", default=default,
                        help="relay base URL (wss:// in production)")
    parser.add_argument("--token", metavar="TOKEN", default=default,
                        help="controller relay token; prefer BRIDGE_CONTROLLER_TOKEN")
    parser.add_argument("--peer-key", metavar="KEY", default=default,
                        help="local Fernet pairing key; prefer BRIDGE_PEER_KEY")
    parser.add_argument("--allow-insecure-localhost", action="store_true", default=default,
                        help="allow ws:// only for an explicit loopback test relay")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentbridge",
        description="Local controller for one trusted AgentBridge Windows device.",
    )
    parser.add_argument("--version", action="version", version="agentbridge 0.1.0")
    _config_options(parser)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        item = commands.add_parser(name, **kwargs)
        _config_options(item, suppress_defaults=True)
        return item

    configure = command("configure", help="prompt for and save local controller configuration")
    configure.set_defaults(command="configure")

    keygen = command("keygen", help="generate a local Fernet pairing key")
    keygen.add_argument("--write", action="store_true", help="write the new key into the private config file")
    keygen.add_argument("--force", action="store_true", help="replace an existing key when used with --write")

    command("ping", help="check the connected PC")
    command("info", help="show non-secret system information from the PC")

    call = command("call", help="call an RPC method with a JSON object")
    call.add_argument("method", metavar="METHOD")
    call.add_argument("--params", default="{}", metavar="JSON", help="method params as a JSON object")

    ls = command("ls", help="list a remote directory")
    ls.add_argument("path")
    stat_command = command("stat", help="show remote path metadata")
    stat_command.add_argument("path")

    read = command("read", help="read one remote chunk to stdout or a local file")
    read.add_argument("path")
    read.add_argument("--offset", type=_nonnegative_int, default=0)
    read.add_argument("--length", type=_read_length, default=CHUNK_BYTES)
    read.add_argument("--output", metavar="FILE", help="write the returned bytes to this local file")

    write = command("write", help="atomically replace one small remote file")
    write.add_argument("path")
    source = write.add_mutually_exclusive_group()
    source.add_argument("--data", help="UTF-8 text to write")
    source.add_argument("--file", metavar="FILE", help="read bytes from a local file")
    write.add_argument("--create-parents", action="store_true")

    put = command("put", help="upload a local file in checked chunks")
    put.add_argument("source")
    put.add_argument("destination")
    put.add_argument("--chunk-size", type=_positive_chunk, default=CHUNK_BYTES)

    get = command("get", help="download a remote file in checked chunks")
    get.add_argument("source")
    get.add_argument("destination")
    get.add_argument("--chunk-size", type=_positive_chunk, default=CHUNK_BYTES)
    get.add_argument("--sha256", type=_sha256_value, help="expected SHA-256 digest")

    execute = command("exec", help="start a command and stream its output")
    execute.add_argument("--cwd", help="remote working directory")
    execute.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                         help="remote environment entry; may be repeated")
    execute.add_argument("--timeout", type=_positive_seconds, help="remote execution timeout in seconds")
    mode = execute.add_mutually_exclusive_group()
    mode.add_argument("--script", help="PowerShell source text")
    mode.add_argument("--script-file", metavar="FILE", help="UTF-8 PowerShell source file")
    input_source = execute.add_mutually_exclusive_group()
    input_source.add_argument("--stdin", dest="stdin_text", help="UTF-8 bytes for the process stdin")
    input_source.add_argument("--stdin-file", metavar="FILE", help="local binary input file, or - for stdin")
    execute.add_argument("argv", nargs=argparse.REMAINDER, help="command argv; put it after --")

    command("rpc", help="keep one connection and proxy JSON-lines RPC on stdin/stdout")
    return parser


def _configured_value(
    explicit: Any,
    environment: str | None,
    existing: dict[str, Any],
    name: str,
    prompt: str,
    prompt_func: Callable[[str], str],
) -> str:
    if explicit is not None:
        return str(explicit)
    if environment is not None and environment in os.environ:
        return os.environ[environment]
    old = existing.get(name)
    if isinstance(old, str) and old:
        return old
    try:
        return prompt_func(prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        raise CliError("Configuration input was cancelled") from exc


def _configure(
    args: argparse.Namespace,
    *,
    stderr: TextIO,
    input_func: Callable[[str], str],
    getpass_func: Callable[[str], str],
) -> int:
    path = _path_from_args(args)
    # Rewriting a legacy non-private file is allowed here so this command can repair it.
    existing = _read_config(path, required=False, verify_private=False)
    relay_url = _configured_value(
        getattr(args, "relay_url", None), "BRIDGE_URL", existing, "relay_url",
        "Relay URL (wss://...): ", input_func,
    )
    token = _configured_value(
        getattr(args, "token", None), "BRIDGE_CONTROLLER_TOKEN", existing, "token",
        "Controller relay token: ", getpass_func,
    )
    peer_key = _configured_value(
        getattr(args, "peer_key", None), "BRIDGE_PEER_KEY", existing, "peer_key",
        "Local pairing key (run 'agentbridge keygen' first if needed): ", getpass_func,
    )
    value: dict[str, Any] = {"relay_url": relay_url, "token": token, "peer_key": peer_key}
    if getattr(args, "allow_insecure_localhost", False):
        value["allow_insecure_localhost"] = True
    elif existing.get("allow_insecure_localhost") is True:
        value["allow_insecure_localhost"] = True
    try:
        checked = validate_config(value, "controller")
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    _write_private_config(path, checked)
    _status(stderr, "Private controller configuration saved locally")
    return 0


def _keygen(args: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> int:
    if getattr(args, "force", False) and not getattr(args, "write", False):
        raise CliError("--force is only valid with --write")
    key = Fernet.generate_key().decode("ascii")
    if not getattr(args, "write", False):
        # The key is intentionally shown only for this explicit key-generation command.
        _write_bytes(stdout, key.encode("ascii") + b"\n")
        _status(stderr, "Keep this pairing key only on the controller and the chosen PC, never on Heroku")
        return 0

    path = _path_from_args(args)
    existing = _read_config(path, required=False, verify_private=False)
    if existing.get("peer_key") and not getattr(args, "force", False):
        raise CliError("A pairing key already exists; use --force only when intentionally replacing the pair")
    existing["peer_key"] = key
    _write_private_config(path, existing)
    _status(stderr, "New pairing key saved locally; it was not printed")
    return 0


async def _request_and_json(controller: Any, method: str, params: dict[str, Any], stdout: TextIO) -> int:
    result = await controller.request(method, params)
    _write_json(stdout, result)
    return 0


def _read_response(result: Any, expected_offset: int, *, description: str) -> tuple[bytes, bool]:
    if not isinstance(result, dict):
        raise CliError(f"Malformed {description} response")
    offset = result.get("offset")
    eof = result.get("eof")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset != expected_offset:
        raise CliError(f"Malformed {description} response: unexpected offset")
    if not isinstance(eof, bool):
        raise CliError(f"Malformed {description} response: eof is not boolean")
    return _decode_base64(result.get("data"), description=description), eof


async def _read_one(controller: Any, args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    result = await controller.request("fs.read", {
        "path": args.path,
        "offset": args.offset,
        "length": args.length,
    })
    data, _ = _read_response(result, args.offset, description="fs.read")
    if len(data) > args.length:
        raise CliError("Malformed fs.read response: chunk exceeds requested length")
    if args.output:
        target = Path(args.output).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise CliError(f"Cannot write local output ({type(exc).__name__})") from exc
        _status(stderr, "Remote chunk saved locally")
    else:
        _write_bytes(stdout, data)
    return 0


async def _write_one(controller: Any, args: argparse.Namespace, stdout: TextIO) -> int:
    if args.file:
        try:
            data = Path(args.file).expanduser().read_bytes()
        except OSError as exc:
            raise CliError(f"Cannot read local input ({type(exc).__name__})") from exc
    elif args.data is not None:
        data = args.data.encode("utf-8")
    else:
        raise CliError("write needs --data or --file; use put for larger files")
    if len(data) > CHUNK_BYTES:
        raise CliError(f"write accepts at most {CHUNK_BYTES} bytes; use put for larger files")
    result = await controller.request("fs.write", {
        "path": args.path,
        "data": base64.b64encode(data).decode("ascii"),
        "create_parents": bool(args.create_parents),
    })
    _write_json(stdout, result)
    return 0


async def _put(controller: Any, args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    source = Path(args.source).expanduser()
    try:
        if not source.is_file():
            raise OSError("not a regular file")
        size = source.stat().st_size
    except OSError as exc:
        raise CliError(f"Cannot read local upload source ({type(exc).__name__})") from exc
    started = await controller.request("upload.begin", {"path": args.destination, "size": size})
    if not isinstance(started, dict) or not isinstance(started.get("upload_id"), str) or not started["upload_id"]:
        raise CliError("Malformed upload.begin response")
    upload_id = started["upload_id"]
    digest = hashlib.sha256()
    offset = 0
    _status(stderr, f"Uploading {size} bytes")
    try:
        with source.open("rb") as stream:
            while offset < size:
                data = stream.read(min(args.chunk_size, size - offset))
                if not data:
                    raise CliError("Local upload source changed while it was being read")
                await controller.request("upload.chunk", {
                    "upload_id": upload_id,
                    "offset": offset,
                    "data": base64.b64encode(data).decode("ascii"),
                })
                digest.update(data)
                offset += len(data)
    except OSError as exc:
        raise CliError(f"Cannot read local upload source ({type(exc).__name__})") from exc
    if offset != size:
        raise CliError("Local upload source changed while it was being read")
    hexdigest = digest.hexdigest()
    await controller.request("upload.finish", {"upload_id": upload_id, "sha256": hexdigest})
    _write_json(stdout, {"path": args.destination, "bytes": offset, "sha256": hexdigest})
    return 0


async def _get(controller: Any, args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    metadata = await controller.request("fs.stat", {"path": args.source})
    if not isinstance(metadata, dict):
        raise CliError("Malformed fs.stat response")
    size = metadata.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise CliError("Malformed fs.stat response: file size is invalid")

    target = Path(args.destination).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
    except OSError as exc:
        raise CliError(f"Cannot create local download file ({type(exc).__name__})") from exc

    digest = hashlib.sha256()
    offset = 0
    committed = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            _status(stderr, f"Downloading {size} bytes")
            while True:
                result = await controller.request("fs.read", {
                    "path": args.source,
                    "offset": offset,
                    "length": args.chunk_size,
                })
                data, eof = _read_response(result, offset, description="fs.read")
                if len(data) > args.chunk_size:
                    raise CliError("Malformed fs.read response: chunk exceeds requested length")
                if offset + len(data) > size:
                    raise CliError("Remote file changed during download")
                if not data and not eof:
                    raise CliError("Malformed fs.read response: empty non-final chunk")
                stream.write(data)
                digest.update(data)
                offset += len(data)
                if eof:
                    break
            stream.flush()
            os.fsync(stream.fileno())
        if offset != size:
            raise CliError("Remote file changed during download")
        hexdigest = digest.hexdigest()
        if args.sha256 and not hmac.compare_digest(hexdigest, args.sha256):
            raise CliError("Downloaded file SHA-256 does not match --sha256")
        os.replace(temporary, target)
        committed = True
    except OSError as exc:
        raise CliError(f"Cannot write local download file ({type(exc).__name__})") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if not committed:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass
    _write_json(stdout, {"path": str(target), "bytes": offset, "sha256": digest.hexdigest()})
    return 0


def _exec_params(args: argparse.Namespace) -> tuple[dict[str, Any], bytes | Path | str | None]:
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv.pop(0)
    if args.script is not None:
        if argv:
            raise CliError("Use either --script or argv, not both")
        params: dict[str, Any] = {"script": args.script}
    elif args.script_file is not None:
        if argv:
            raise CliError("Use either --script-file or argv, not both")
        try:
            params = {"script": Path(args.script_file).expanduser().read_text(encoding="utf-8-sig")}
        except (OSError, UnicodeError) as exc:
            raise CliError(f"Cannot read local PowerShell source ({type(exc).__name__})") from exc
    elif argv:
        if any(not item for item in argv):
            raise CliError("argv items must not be empty")
        params = {"argv": argv}
    else:
        raise CliError("exec needs argv after --, --script, or --script-file")

    if args.cwd is not None:
        if not args.cwd:
            raise CliError("--cwd must not be empty")
        params["cwd"] = args.cwd
    if args.timeout is not None:
        params["timeout"] = args.timeout
    environment: dict[str, str] = {}
    for item in args.env:
        name, separator, value = item.partition("=")
        if not separator or not _ENV_NAME.fullmatch(name):
            raise CliError("--env must use NAME=VALUE with a conventional environment name")
        if name in environment:
            raise CliError("Each --env name may appear only once")
        environment[name] = value
    if environment:
        params["env"] = environment

    if args.stdin_text is not None:
        input_source: bytes | Path | str | None = args.stdin_text.encode("utf-8")
    elif args.stdin_file is not None:
        input_source = args.stdin_file if args.stdin_file == "-" else Path(args.stdin_file).expanduser()
        if isinstance(input_source, Path) and not input_source.is_file():
            raise CliError("--stdin-file must name a readable local file")
    else:
        input_source = None
    return params, input_source


async def _send_stdin(controller: Any, job_id: str, source: bytes | Path | str, stdin: TextIO) -> None:
    if isinstance(source, bytes):
        chunks = (source[position:position + STDIN_CHUNK_BYTES]
                  for position in range(0, len(source), STDIN_CHUNK_BYTES))
        for data in chunks:
            await controller.request("exec.stdin", {
                "job_id": job_id,
                "data": base64.b64encode(data).decode("ascii"),
                "eof": False,
            })
    else:
        close_after = isinstance(source, Path)
        if source == "-":
            binary = getattr(stdin, "buffer", None)
            stream: Any = binary if binary is not None else stdin
        else:
            stream = Path(source).open("rb")
        try:
            while True:
                data = stream.read(STDIN_CHUNK_BYTES)
                if not data:
                    break
                if isinstance(data, str):
                    data = data.encode("utf-8")
                await controller.request("exec.stdin", {
                    "job_id": job_id,
                    "data": base64.b64encode(data).decode("ascii"),
                    "eof": False,
                })
        finally:
            if close_after:
                stream.close()
    await controller.request("exec.stdin", {"job_id": job_id, "data": "", "eof": True})


def _handle_exec_event(event: Any, job_id: str, stdout: TextIO, stderr: TextIO) -> int | None:
    if not isinstance(event, dict) or event.get("kind") != "event":
        raise CliError("Malformed event from device")
    if event.get("event") == "bridge.disconnected":
        raise CliError("Bridge disconnected; unfinished actions were not retried")
    if event.get("job_id") != job_id:
        return None
    if event.get("event") == "exec.output":
        stream = event.get("stream")
        if stream not in ("stdout", "stderr"):
            raise CliError("Malformed exec.output event")
        data = _decode_base64(event.get("data"), description="exec.output")
        _write_bytes(stdout if stream == "stdout" else stderr, data)
        return None
    if event.get("event") == "exec.exit":
        exit_code = event.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise CliError("Malformed exec.exit event")
        cancelled = event.get("cancelled")
        timed_out = event.get("timed_out")
        if not isinstance(cancelled, bool) or not isinstance(timed_out, bool):
            raise CliError("Malformed exec.exit event")
        if cancelled:
            _status(stderr, "Remote job reported cancellation")
        elif timed_out:
            _status(stderr, "Remote job reported a timeout")
        return exit_code
    return None


async def _cancel_remote_job(controller: Any, job_id: str, stderr: TextIO) -> None:
    """Make one best-effort cancellation request; never reconnect or retry it."""

    try:
        await controller.request("exec.cancel", {"job_id": job_id})
    except asyncio.CancelledError:
        raise
    except Exception:
        _status(stderr, "Could not confirm remote cancellation; the session will close")


async def _exec(
    controller: Any,
    args: argparse.Namespace,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    prepared: tuple[dict[str, Any], bytes | Path | str | None] | None = None,
) -> int:
    params, input_source = prepared if prepared is not None else _exec_params(args)
    job_id: str | None = None
    event_task: asyncio.Task[Any] | None = None
    input_task: asyncio.Task[None] | None = None
    should_cancel = False
    try:
        started = await controller.request("exec.start", params)
        if not isinstance(started, dict) or not isinstance(started.get("job_id"), str) or not started["job_id"]:
            raise CliError("Malformed exec.start response")
        job_id = started["job_id"]
        _status(stderr, f"Remote job {job_id} started")
        event_task = asyncio.create_task(controller.events.get())
        if input_source is not None:
            input_task = asyncio.create_task(_send_stdin(controller, job_id, input_source, stdin))

        while True:
            waiting: list[asyncio.Task[Any]] = [event_task]
            if input_task is not None:
                waiting.append(input_task)
            completed, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if event_task in completed:
                event = event_task.result()
                event_task = None
                exit_code = _handle_exec_event(event, job_id, stdout, stderr)
                if exit_code is not None:
                    return exit_code
                event_task = asyncio.create_task(controller.events.get())
            if input_task is not None and input_task in completed:
                input_task.result()
                input_task = None
    except (asyncio.CancelledError, KeyboardInterrupt):
        if job_id is not None:
            _status(stderr, "Cancelling remote job")
            await _cancel_remote_job(controller, job_id, stderr)
        raise
    except Exception:
        should_cancel = job_id is not None
        raise
    finally:
        if should_cancel and job_id is not None:
            await _cancel_remote_job(controller, job_id, stderr)
        for task in (event_task, input_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (event_task, input_task) if task is not None),
                             return_exceptions=True)


class _LineReader:
    """Bridge blocking stdin to an asyncio queue without delaying RPC events."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[str | bytes] = asyncio.Queue(maxsize=32)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._read, name="AgentBridge RPC input", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _read(self) -> None:
        while not self.stop.is_set():
            try:
                line = self.stream.readline()
            except Exception:
                line = ""
            try:
                future = asyncio.run_coroutine_threadsafe(self.queue.put(line), self.loop)
                future.result()
            except Exception:
                return
            if line == "" or line == b"":
                return


def _rpc_error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


def _rpc_exception(request_id: Any, exc: Exception) -> dict[str, Any]:
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        code = "DISCONNECTED" if type(exc).__name__ == "BridgeDisconnected" else "FAILED"
    return _rpc_error(request_id, code, "Request failed" if code == "FAILED" else str(exc))


async def _rpc_request(controller: Any, request_id: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await controller.request(method, params)
        return {"id": request_id, "ok": True, "result": result}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _rpc_exception(request_id, exc)


def _parse_rpc_line(line: str | bytes) -> tuple[Any, str, dict[str, Any]] | dict[str, Any] | None:
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return _rpc_error(None, "INVALID_REQUEST", "Input must be UTF-8 JSON")
    if not line.strip():
        return None
    if len(line.encode("utf-8")) > 1_048_576:
        return _rpc_error(None, "INVALID_REQUEST", "Input line is too large")
    try:
        body = json.loads(line)
    except json.JSONDecodeError:
        return _rpc_error(None, "INVALID_REQUEST", "Input must be a JSON object")
    if not isinstance(body, dict):
        return _rpc_error(None, "INVALID_REQUEST", "Input must be a JSON object")
    request_id = body.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return _rpc_error(None, "INVALID_REQUEST", "id must be a string or integer")
    method = body.get("method")
    params = body.get("params")
    if not isinstance(method, str) or not method:
        return _rpc_error(request_id, "INVALID_REQUEST", "method must be a non-empty string")
    if not isinstance(params, dict):
        return _rpc_error(request_id, "INVALID_REQUEST", "params must be a JSON object")
    return request_id, method, params


async def _rpc(controller: Any, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    _status(stderr, "RPC session connected; JSON-lines responses and events go to stdout")
    reader = _LineReader(stdin)
    reader.start()
    line_task: asyncio.Task[str | bytes] | None = asyncio.create_task(reader.queue.get())
    event_task: asyncio.Task[Any] | None = asyncio.create_task(controller.events.get())
    requests: dict[asyncio.Task[dict[str, Any]], Any] = {}
    active_ids: set[str | int] = set()
    input_closed = False
    disconnected = False
    try:
        while True:
            waiting: list[asyncio.Task[Any]] = list(requests)
            if line_task is not None:
                waiting.append(line_task)
            if event_task is not None:
                waiting.append(event_task)
            if not waiting:
                return 1 if disconnected else 0
            completed, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)

            for task in list(requests):
                if task in completed:
                    request_id = requests.pop(task)
                    active_ids.discard(request_id)
                    _write_json(stdout, task.result())

            if event_task is not None and event_task in completed:
                event = event_task.result()
                if not isinstance(event, dict):
                    raise CliError("Malformed event from device")
                _write_json(stdout, event)
                if event.get("event") == "bridge.disconnected":
                    disconnected = True
                    event_task = None
                    if line_task is not None:
                        line_task.cancel()
                        line_task = None
                    for task, request_id in list(requests.items()):
                        task.cancel()
                        _write_json(stdout, _rpc_error(
                            request_id, "DISCONNECTED",
                            "Connection ended; action outcome may be unknown and was not retried",
                        ))
                    requests.clear()
                    active_ids.clear()
                else:
                    event_task = asyncio.create_task(controller.events.get())

            if line_task is not None and line_task in completed:
                line = line_task.result()
                line_task = None
                if line == "" or line == b"":
                    input_closed = True
                else:
                    parsed = _parse_rpc_line(line)
                    if parsed is not None:
                        if isinstance(parsed, dict):
                            _write_json(stdout, parsed)
                        else:
                            request_id, method, params = parsed
                            if request_id in active_ids:
                                _write_json(stdout, _rpc_error(
                                    request_id, "INVALID_REQUEST", "id is already in flight",
                                ))
                            elif len(requests) >= 32:
                                _write_json(stdout, _rpc_error(
                                    request_id, "BUSY", "Too many requests are already in flight",
                                ))
                            else:
                                task = asyncio.create_task(_rpc_request(controller, request_id, method, params))
                                requests[task] = request_id
                                active_ids.add(request_id)
                if not input_closed and not disconnected:
                    line_task = asyncio.create_task(reader.queue.get())

            if input_closed and not requests:
                return 1 if disconnected else 0
    finally:
        reader.stop.set()
        for task in (line_task, event_task, *requests):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (line_task, event_task, *requests) if task is not None),
                             return_exceptions=True)


async def _run_connected(
    args: argparse.Namespace,
    config: dict[str, Any],
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    controller_factory: Callable[[dict[str, Any]], Any] | None,
) -> int:
    # Parse generic params and execution source before opening a relay session.
    call_params = _json_object(args.params) if args.command == "call" else None
    exec_prepared = _exec_params(args) if args.command == "exec" else None
    factory = controller_factory or Controller
    async with factory(config) as controller:
        if args.command == "ping":
            return await _request_and_json(controller, "ping", {}, stdout)
        if args.command == "info":
            return await _request_and_json(controller, "system.info", {}, stdout)
        if args.command == "call":
            return await _request_and_json(controller, args.method, call_params or {}, stdout)
        if args.command == "ls":
            return await _request_and_json(controller, "fs.list", {"path": args.path}, stdout)
        if args.command == "stat":
            return await _request_and_json(controller, "fs.stat", {"path": args.path}, stdout)
        if args.command == "read":
            return await _read_one(controller, args, stdout, stderr)
        if args.command == "write":
            return await _write_one(controller, args, stdout)
        if args.command == "put":
            return await _put(controller, args, stdout, stderr)
        if args.command == "get":
            return await _get(controller, args, stdout, stderr)
        if args.command == "exec":
            return await _exec(controller, args, stdin, stdout, stderr, exec_prepared)
        if args.command == "rpc":
            return await _rpc(controller, stdin, stdout, stderr)
    raise CliError("Unknown command")


def _safe_message(exc: BaseException, secrets: tuple[str, ...]) -> str:
    message = str(exc) or type(exc).__name__
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<redacted>")
    # This is defensive: relay URLs with query credentials are rejected before use.
    return re.sub(r"([?&](?:token|key|secret|password)=)[^&#\s]+", r"\1<redacted>", message,
                  flags=re.IGNORECASE)


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    input_func: Callable[[str], str] | None = None,
    getpass_func: Callable[[str], str] | None = None,
    controller_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> int:
    """Run the CLI and return its exit status for both console and unit-test use."""

    parser = build_parser()
    args = parser.parse_args(argv)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    input_func = input_func or input
    if getpass_func is None:
        import getpass

        getpass_func = getpass.getpass

    secrets: tuple[str, ...] = ()
    try:
        if args.command == "configure":
            return _configure(args, stderr=stderr, input_func=input_func, getpass_func=getpass_func)
        if args.command == "keygen":
            return _keygen(args, stdout=stdout, stderr=stderr)
        config, secrets = _resolve_config(args)
        return asyncio.run(_run_connected(
            args, config, stdin=stdin, stdout=stdout, stderr=stderr,
            controller_factory=controller_factory,
        ))
    except KeyboardInterrupt:
        _status(stderr, "Cancelled")
        return 130
    except asyncio.CancelledError:
        _status(stderr, "Cancelled")
        return 130
    except (CliError, ValueError, OSError) as exc:
        _status(stderr, _safe_message(exc, secrets))
        return 2
    except Exception as exc:
        _status(stderr, _safe_message(exc, secrets))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
