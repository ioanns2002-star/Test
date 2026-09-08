# AgentBridge v1

One PC, one controller, one relay process/dyno. No database, queue, server-side
files, execution, retries of commands, or stored results. The relay forwards opaque
binary frames between two authenticated WebSocket connections. It cannot decrypt
the contents. A restart disconnects the session; the PC reconnects, but commands
are never replayed automatically. More than one relay process/dyno is unsupported.

## Relay

- `/healthz` and `/`: anonymous, static health response; no device information.
- `/ws/device`: `Authorization: Bearer <BRIDGE_DEVICE_TOKEN>`.
- `/ws/controller`: `Authorization: Bearer <BRIDGE_CONTROLLER_TOKEN>`.
- Tokens are separate random secrets, at least 32 characters, stored in Heroku
  config vars, never in URLs. Browser-origin WebSocket requests are rejected.
- Exactly one connection per role. A second connection is rejected, not a takeover.
- The relay sends text `{"bridge":"paired"}` when both peers are present.
- Device stays connected between sessions; on controller departure it receives
  `{"bridge":"unpaired"}`. Device departure closes the controller connection.
- Peers send binary encrypted frames only. The relay does not queue frames when
  the other peer is absent. Frame limit: 1 MiB; bounded queues and backpressure.
- Production endpoints require `wss://`; explicitly enabled `ws://` is restricted
  to loopback addresses for tests. Standard certificate verification stays on.

## Endpoint authentication and encryption

The PC and controller share a separate Fernet key generated locally. This key is
NOT set on Heroku. Use `cryptography.fernet.Fernet`, not custom cryptographic
primitives. Every new pair performs a fresh encrypted nonce challenge:

1. Device sends encrypted `{"v":1,"kind":"hello","nonce":D}`.
2. Controller sends encrypted `{"v":1,"kind":"answer","device_nonce":D,"nonce":C}`.
3. Device sends encrypted `{"v":1,"kind":"ready","device_nonce":D,"controller_nonce":C}`.

D and C are fresh 32-byte random hex strings. Both sides derive the session ID
from SHA-256 of `agentbridge-v1:D:C`. Each subsequent encrypted envelope includes
`v`, `session`, `sender` (`device` or `controller`), strictly increasing `seq`
(starting at 1), and `body`. Reject wrong roles, session IDs, duplicate/reordered
sequences, and invalid ciphertext. Handshake timeout: 10 seconds. Reconnection
does not reuse a session. A single send lock covers sequence assignment and send.

Authenticated heartbeat bodies maintain endpoint liveness independently of relay
WebSocket pings. Losing the peer, pausing/quitting the tray, or a heartbeat timeout
cancels the session's child process trees, releases held input, and removes
unfinished uploads. Files already committed remain local. Deliberately detached
work outside the agent's process containment is not an access-control sandbox.

## RPC bodies

Request: `{"kind":"request","id":"opaque-id","method":"fs.list","params":{...}}`.
Success: `{"kind":"response","id":"opaque-id","ok":true,"result":...}`.
Failure: `{"kind":"response","id":"opaque-id","ok":false,"error":{"code":"...","message":"..."}}`.
Event: `{"kind":"event","event":"exec.output",...}`.
Heartbeat: `{"kind":"ping"}` / `{"kind":"pong"}`.

Operations are fully authorized with the Windows user's permissions; a work
directory is NOT a sandbox. No new process is elevated silently. No hidden
service, automatic startup, clipboard capture, or continuous screen recording.

## Operations contract

`Operations(data_dir: Path, emit: async callable(dict))` has
`async dispatch(method: str, params: dict)` and `async close()`.
Invalid calls raise `OperationError(code, message)`.

- `ping {}`: local responsiveness.
- `system.info {}`: OS, user, elevation, paths, supported capabilities.
- `fs.list {path}`; `fs.stat {path}`.
- `fs.read {path, offset=0, length=262144}`: base64 `data`, `offset`, `eof`.
- `fs.write {path, data, create_parents=false}`: atomic small-file replacement;
  `data` is base64, at most 262144 decoded bytes.
- `fs.mkdir {path, parents=false}`; `fs.move {source, destination}`;
  `fs.remove {path, recursive=false}`.
- `upload.begin {path, size}`: returns `upload_id`; temporary sibling file.
- `upload.chunk {upload_id, offset, data}`: strict sequential offsets, bounded data.
- `upload.finish {upload_id, sha256}`: verify size/digest, atomically commit.
- `exec.start {argv:[...]} OR {script:"PowerShell code"}`, with optional
  `cwd`, `env`, `timeout` (seconds; default 3600). Returns `job_id` immediately.
- `exec.stdin {job_id, data, eof=false}`: base64 bytes; `exec.cancel {job_id}`;
  `exec.list {}`.
- Events: `exec.output {job_id, stream:"stdout"|"stderr", data:base64}` and
  `exec.exit {job_id, exit_code, cancelled, timed_out}`. Output also stays in
  bounded local log files under `data_dir`, never on Heroku.
- `screen.capture {}`: Windows interactive desktop, returns local PNG `path`,
  `width`, `height`; transfer it using `fs.read`, not one oversized frame.
- `input.mouse {action, x?, y?, button?, delta?}`;
  `input.key {key, action:"press"|"down"|"up"}`; `input.text {text}`.
  These are explicit calls, not background surveillance. Unsupported desktop
  operations fail clearly outside an interactive Windows session.

## Desktop / controller integration

Endpoint config: `{"relay_url":"wss://app.herokuapp.com","token":"...","peer_key":"..."}`.
`allow_insecure_localhost: true` is test-only; never allows remote plaintext WS.

`AgentService(config: dict, data_dir: Path, on_status: callable(str,str))` exposes
`start()`, `pause()`, `resume()`, `stop()`. It owns its asyncio background thread.
Status names: `connecting`, `waiting`, `connected`, `paused`, `error`, `stopped`.
The Windows tray/configuration UI runs visibly in the user's desktop session.

`Controller(config: dict)` is an async context manager from
`agentbridge.connection`, exposes `await request(method, params)`,
`events: asyncio.Queue`, and `latency` via ping. CLI supports single calls plus
long-lived JSON-lines RPC so multiple actions don't repeat connection setup.

PC config is protected with Windows DPAPI in the current user's profile. Controller
config is a private file (0600 on Unix) or environment variables. No real secrets
belong in the repository, chat, workflow logs, or release archives.
