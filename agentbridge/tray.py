"""Visible Windows tray application for the local AgentBridge endpoint.

The Tk event loop stays on the main thread.  pystray callbacks and
``AgentService`` status notifications only enqueue work, which the Tk loop
then handles safely.
"""

from __future__ import annotations

import atexit
from datetime import datetime
import os
from pathlib import Path
import queue
import re
import signal
import sys
import threading
from typing import Any, Callable

from .windows_config import (
    CorruptConfigurationError,
    DesktopConfig,
    InvalidConfigurationError,
    WindowsConfigError,
    WindowsConfigStore,
    WindowsOnlyError,
    generate_peer_key,
    is_windows,
    validate_peer_key,
)


_VALID_STATUSES = frozenset(
    {"connecting", "waiting", "connected", "paused", "error", "stopped"}
)
_STATUS_LABELS = {
    "connecting": "Connecting",
    "waiting": "Waiting — access enabled",
    "connected": "Connected — controller access enabled",
    "paused": "Paused — access disabled",
    "error": "Error — check local logs",
    "stopped": "Disconnected — access disabled",
}
_DEFAULT_DETAILS = {
    "connecting": "Connecting to the relay.",
    "waiting": "Waiting for the trusted controller.",
    "connected": "The trusted controller is connected.",
    "paused": "Controller access is paused.",
    "error": "The connection needs attention. Open local logs for details.",
    "stopped": "Select Connect to enable controller access.",
}
_STATUS_COLOURS = {
    "connecting": "#eab308",
    "waiting": "#3b82f6",
    "connected": "#16a34a",
    "paused": "#6b7280",
    "error": "#dc2626",
    "stopped": "#6b7280",
}
_LOG_FILE_NAME = "tray.log"
_PREVIOUS_LOG_FILE_NAME = "tray.previous.log"
_MAX_LOG_BYTES = 256 * 1024


def _default_service_factory(
    config: dict[str, str], data_dir: Path, on_status: Callable[[str, str], None]
) -> Any:
    """Import the service lazily so the core package stays platform-neutral."""

    from .agent import AgentService

    return AgentService(config=config, data_dir=data_dir, on_status=on_status)


class AgentBridgeTray:
    """Own the visible tray UI and one explicitly started ``AgentService``."""

    def __init__(
        self,
        *,
        store: WindowsConfigStore | None = None,
        service_factory: Callable[[dict[str, str], Path, Callable[[str, str], None]], Any]
        | None = None,
    ) -> None:
        self._store = store or WindowsConfigStore()
        self._service_factory = service_factory or _default_service_factory
        self._events: queue.SimpleQueue[tuple[str, tuple[Any, ...]]] = queue.SimpleQueue()
        self._service_lock = threading.RLock()
        self._service: Any | None = None
        self._generation = 0
        self._active_secrets: tuple[str, ...] = ()
        self._closing = threading.Event()

        self._status = "stopped"
        self._status_detail = _DEFAULT_DETAILS["stopped"]
        self._root: Any | None = None
        self._tk: Any | None = None
        self._ttk: Any | None = None
        self._messagebox: Any | None = None
        self._scrolledtext: Any | None = None
        self._icon: Any | None = None
        self._pystray: Any | None = None
        self._settings_window: Any | None = None
        self._logs_window: Any | None = None
        self._session_cleanup: _WindowsSessionCleanup | None = None
        self._exit_cleanup_registered = False
        self._previous_signal_handlers: dict[int, Any] = {}

    def run(self) -> int:
        """Run the visible desktop UI.  This must be called on the main thread."""

        if not is_windows():
            raise WindowsOnlyError("AgentBridge desktop is available only on Windows.")
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("AgentBridge desktop must run on the main thread.")

        self._load_tk()
        self._root = self._tk.Tk()
        self._root.title("AgentBridge")
        self._root.withdraw()
        self._root.protocol("WM_DELETE_WINDOW", self._request_quit)

        try:
            self._create_tray_icon()
            self._install_session_cleanup()
            self._install_exit_cleanup()
            self._root.after(50, self._drain_events)
            self._root.after_idle(self._initialize)
            self._root.mainloop()
        finally:
            self._shutdown()
        return 0

    def _load_tk(self) -> None:
        # Imported here because the controller and relay must not need Tk.
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk

        self._tk = tk
        self._ttk = ttk
        self._messagebox = messagebox
        self._scrolledtext = scrolledtext

    def _initialize(self) -> None:
        if self._closing.is_set():
            return
        if self._store.has_config():
            self._set_status("stopped", _DEFAULT_DETAILS["stopped"], write_log=False)
        else:
            self._set_status(
                "stopped",
                "Configure this PC before enabling controller access.",
                write_log=False,
            )
            self.open_settings(first_run=True)

    def _create_tray_icon(self) -> None:
        try:
            import pystray
        except ImportError as error:
            raise RuntimeError("pystray and Pillow are required for AgentBridge desktop.") from error

        self._pystray = pystray
        self._icon = pystray.Icon(
            "AgentBridge",
            self._make_icon_image(self._status),
            self._tray_title(),
            pystray.Menu(
                pystray.MenuItem(self._tray_status_text, None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Connect", self._tray_callback("connect")),
                pystray.MenuItem(self._pause_menu_text, self._tray_callback("pause")),
                pystray.MenuItem("Disconnect", self._tray_callback("disconnect")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Settings…", self._tray_callback("settings")),
                pystray.MenuItem("Show local logs…", self._tray_callback("logs")),
                pystray.MenuItem("Open data folder", self._tray_callback("data")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._tray_callback("quit")),
            ),
        )
        # pystray owns its Windows message loop; Tk remains the application's
        # main-thread UI loop and receives actions through ``self._events``.
        self._icon.run_detached()

    def _make_icon_image(self, status: str) -> Any:
        # Pillow is lazy-imported so non-desktop AgentBridge uses no GUI module.
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        colour = _STATUS_COLOURS[status]
        draw.ellipse((4, 4, 60, 60), fill=colour)
        draw.ellipse((11, 11, 53, 53), outline="#ffffff", width=3)

        if status == "connected":
            draw.line((19, 33, 28, 42, 46, 22), fill="#ffffff", width=5, joint="curve")
        elif status == "paused":
            draw.rounded_rectangle((23, 20, 29, 44), radius=2, fill="#ffffff")
            draw.rounded_rectangle((35, 20, 41, 44), radius=2, fill="#ffffff")
        elif status == "error":
            draw.rectangle((29, 18, 35, 37), fill="#ffffff")
            draw.ellipse((29, 42, 35, 48), fill="#ffffff")
        elif status == "waiting":
            draw.ellipse((27, 27, 37, 37), fill="#ffffff")
        elif status == "connecting":
            draw.arc((17, 17, 47, 47), start=35, end=300, fill="#ffffff", width=5)
        else:
            draw.ellipse((27, 27, 37, 37), fill="#ffffff")
        return image

    def _tray_callback(self, action: str) -> Callable[..., None]:
        def callback(*_unused: Any) -> None:
            # pystray invokes this on its own backend thread.  Never call Tk
            # from here; its event loop drains this queue on the main thread.
            self._events.put(("action", (action,)))

        return callback

    def _tray_status_text(self, *_unused: Any) -> str:
        return f"Status: {_STATUS_LABELS[self._status]}"

    def _pause_menu_text(self, *_unused: Any) -> str:
        return "Resume access" if self._status == "paused" else "Pause access"

    def _tray_title(self) -> str:
        return f"AgentBridge — {_STATUS_LABELS[self._status]}"

    def _drain_events(self) -> None:
        if self._closing.is_set():
            return

        try:
            while True:
                event, payload = self._events.get_nowait()
                if event == "action":
                    self._handle_action(str(payload[0]))
                elif event == "status":
                    generation, status, detail = payload
                    self._apply_service_status(int(generation), str(status), detail)
                elif event == "session_cleanup":
                    self._set_status(
                        "stopped",
                        "Windows is ending this session; controller access was disconnected.",
                    )
        except queue.Empty:
            pass

        if self._root is not None and not self._closing.is_set():
            self._root.after(75, self._drain_events)

    def _handle_action(self, action: str) -> None:
        if action == "connect":
            self.connect()
        elif action == "pause":
            self.pause_or_resume()
        elif action == "disconnect":
            self.disconnect()
        elif action == "settings":
            self.open_settings()
        elif action == "logs":
            self.show_logs()
        elif action == "data":
            self.open_data_folder()
        elif action == "quit":
            self._shutdown()

    def _on_service_status(self, generation: int, status: str, detail: str = "") -> None:
        """Receive an AgentService background-thread notification safely."""

        self._events.put(("status", (generation, status, detail)))

    def _apply_service_status(self, generation: int, status: str, detail: object) -> None:
        with self._service_lock:
            if generation != self._generation or self._closing.is_set():
                return
            active_secrets = self._active_secrets

        safe_detail = self._redact_text(detail, active_secrets)
        if status not in _VALID_STATUSES:
            self._set_status("error", "The local agent reported an unknown state.")
            return

        if status == "stopped":
            with self._service_lock:
                if generation != self._generation:
                    return
                self._service = None
                self._active_secrets = ()
                self._generation += 1
        self._set_status(status, safe_detail or _DEFAULT_DETAILS[status])

    def connect(self) -> None:
        """Explicitly start or resume the endpoint; never auto-connect at launch."""

        if self._closing.is_set():
            return
        service = self._current_service()
        if service is not None:
            if self._status == "paused":
                try:
                    service.resume()
                except Exception:
                    self._fail_closed("Could not resume access. The local agent was disconnected.")
                else:
                    self._set_status("connecting", "Resuming controller access.")
                return
            if self._status in {"connecting", "waiting", "connected"}:
                return
            if not self.disconnect(detail="Restarting the local agent."):
                return

        try:
            config = self._store.load()
        except FileNotFoundError:
            self._set_status("stopped", "Configure this PC before enabling controller access.")
            self.open_settings(first_run=True)
            return
        except CorruptConfigurationError:
            self._set_status("error", "Saved settings cannot be read. Open Settings to replace them.")
            self._show_error(
                "Saved settings cannot be decrypted for this Windows user. "
                "Enter the relay URL, device token, and shared peer key again."
            )
            return
        except WindowsConfigError:
            self._set_status("error", "Could not read saved settings. Open Settings to check them.")
            self._show_error("AgentBridge could not read its local settings.")
            return

        try:
            data_dir = self._store.ensure_data_dir()
            generation = self._reserve_generation()
            callback = lambda status, detail="", generation=generation: self._on_service_status(
                generation, status, detail
            )
            new_service = self._service_factory(config.as_agent_config(), data_dir, callback)
            with self._service_lock:
                if self._closing.is_set() or generation != self._generation:
                    should_stop = True
                else:
                    self._service = new_service
                    self._active_secrets = (config.token, config.peer_key)
                    should_stop = False
            if should_stop:
                new_service.stop()
                return
            self._set_status("connecting", "Connecting to the relay.")
            new_service.start()
        except Exception:
            self._discard_service(generation if "generation" in locals() else None)
            self._set_status("error", "Could not start the local agent. Check Settings and local logs.")

    def pause_or_resume(self) -> None:
        """Pause access or resume the already configured local endpoint."""

        service = self._current_service()
        if service is None:
            self._set_status("stopped", "Nothing is connected. Select Connect first.")
            return
        try:
            if self._status == "paused":
                service.resume()
                self._set_status("connecting", "Resuming controller access.")
            else:
                service.pause()
                self._set_status("paused", "Paused. Controller access is disabled.")
        except Exception:
            self._fail_closed("Could not change access state. The local agent was disconnected.")

    def disconnect(self, *, detail: str = "Disconnected. Controller access is disabled.") -> bool:
        """Stop the service so it can cancel jobs and release held input."""

        stopped = self._stop_active_service()
        if not self._closing.is_set():
            if stopped:
                self._set_status("stopped", detail)
            else:
                self._set_status(
                    "error",
                    "AgentBridge could not confirm disconnection. Quit the application before retrying.",
                )
        return stopped

    def _fail_closed(self, detail: str) -> None:
        """Stop the service after a failed state transition rather than leave access uncertain."""

        if self._stop_active_service():
            self._set_status("error", detail)
        else:
            self._set_status(
                "error",
                "AgentBridge could not confirm controller access was disabled. Quit the application now.",
            )

    def _reserve_generation(self) -> int:
        with self._service_lock:
            self._generation += 1
            return self._generation

    def _current_service(self) -> Any | None:
        with self._service_lock:
            return self._service

    def _discard_service(self, generation: int | None) -> None:
        with self._service_lock:
            if generation is not None and generation != self._generation:
                return
            service = self._service
            self._service = None
            self._active_secrets = ()
            self._generation += 1
        if service is not None:
            try:
                service.stop()
            except Exception:
                pass

    def _stop_active_service(self) -> bool:
        with self._service_lock:
            self._generation += 1
            service = self._service
            self._service = None
            self._active_secrets = ()
        if service is not None:
            try:
                service.stop()
            except Exception:
                # Stopping is best-effort during shutdown; do not leave the
                # tray process alive because a service thread raised an error.
                return False
        return True

    def _set_status(self, status: str, detail: object, *, write_log: bool = True) -> None:
        if status not in _VALID_STATUSES:
            status = "error"
            detail = "The local agent reported an unknown state."
        safe_detail = self._redact_text(detail)
        if not safe_detail:
            safe_detail = _DEFAULT_DETAILS[status]
        changed = (status, safe_detail) != (self._status, self._status_detail)
        self._status = status
        self._status_detail = safe_detail
        self._refresh_tray()
        if changed and write_log:
            self._append_log(f"{_STATUS_LABELS[status]}. {safe_detail}")

    def _refresh_tray(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.icon = self._make_icon_image(self._status)
            self._icon.title = self._tray_title()
            self._icon.update_menu()
        except Exception:
            # The icon may already be tearing down after a Windows logoff.
            pass

    def _redact_text(self, value: object, secrets: tuple[str, ...] | None = None) -> str:
        try:
            text = str(value or "")
        except Exception:
            return ""
        if secrets is None:
            with self._service_lock:
                secrets = self._active_secrets
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[redacted]")
        text = re.sub(
            r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
            r"\1[redacted]",
            text,
        )
        text = re.sub(
            r"(?i)\b(device[ _-]?token|token|peer[ _-]?key)\s*[=:]\s*[^\s,;]+",
            r"\1=[redacted]",
            text,
        )
        return " ".join(text.split())[:500]

    @property
    def _log_path(self) -> Path:
        return self._store.data_dir / _LOG_FILE_NAME

    def _append_log(self, message: object) -> None:
        """Write bounded local status logs after removing known secret values."""

        try:
            data_dir = self._store.ensure_data_dir()
            path = data_dir / _LOG_FILE_NAME
            if path.exists() and path.stat().st_size >= _MAX_LOG_BYTES:
                previous = data_dir / _PREVIOUS_LOG_FILE_NAME
                try:
                    os.replace(path, previous)
                except OSError:
                    pass
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{timestamp} {self._redact_text(message)}\n")
        except OSError:
            # A logging problem must not block disconnecting or quitting.
            pass

    def open_settings(self, *, first_run: bool = False) -> None:
        """Show the local configuration window without starting the agent."""

        if self._root is None or self._closing.is_set():
            return
        if self._window_is_open(self._settings_window):
            self._settings_window.deiconify()
            self._settings_window.lift()
            self._settings_window.focus_force()
            return

        relay_url = ""
        token = ""
        peer_key = ""
        if self._store.has_config():
            try:
                config = self._store.load()
                relay_url, token, peer_key = config.relay_url, config.token, config.peer_key
            except CorruptConfigurationError:
                self._show_error(
                    "Saved settings cannot be decrypted for this Windows user. "
                    "Enter replacement settings below."
                )
            except WindowsConfigError:
                self._show_error("Saved AgentBridge settings could not be read.")

        window = self._tk.Toplevel(self._root)
        self._settings_window = window
        window.title("AgentBridge settings")
        window.resizable(False, False)
        window.transient(self._root)
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_settings(window))

        content = self._ttk.Frame(window, padding=16)
        content.grid(sticky="nsew")
        content.columnconfigure(1, weight=1)

        warning = (
            "While enabled, one trusted controller can run commands and supported desktop "
            "operations with this Windows account's current rights. This is one trusted "
            "controller decision when you select Connect, not a prompt for every command.\n\n"
            "AgentBridge does not elevate itself, does not run as a hidden service, and can "
            "be paused or disconnected at any time from the tray icon. Only configure a "
            "controller you trust."
        )
        self._ttk.Label(
            content,
            text=warning,
            justify="left",
            wraplength=590,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))

        self._ttk.Label(content, text="Relay URL (wss://Heroku app)").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=4
        )
        relay_var = self._tk.StringVar(value=relay_url)
        relay_entry = self._ttk.Entry(content, textvariable=relay_var, width=62)
        relay_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)

        self._ttk.Label(content, text="Device token (BRIDGE_DEVICE_TOKEN)").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4
        )
        token_var = self._tk.StringVar(value=token)
        token_entry = self._ttk.Entry(content, textvariable=token_var, show="*", width=62)
        token_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)

        self._ttk.Label(content, text="Shared peer key (Fernet)").grid(
            row=3, column=0, sticky="w", padx=(0, 10), pady=4
        )
        peer_var = self._tk.StringVar(value=peer_key)
        peer_entry = self._ttk.Entry(content, textvariable=peer_var, show="*", width=62)
        peer_entry.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)

        help_text = (
            "The device token and peer key are encrypted with Windows DPAPI in this user's "
            "local profile. They are never stored in this project or sent to GitHub."
        )
        self._ttk.Label(content, text=help_text, justify="left", wraplength=590).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 8)
        )

        feedback_var = self._tk.StringVar(value="")
        self._ttk.Label(content, textvariable=feedback_var, wraplength=590).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        def generate_key() -> None:
            if peer_var.get() and not self._messagebox.askyesno(
                "Replace shared peer key?",
                "Generating a key replaces the value in this form. Existing controllers "
                "will need the new key after you save it.",
                parent=window,
            ):
                return
            peer_var.set(generate_peer_key())
            peer_entry.configure(show="*")
            reveal_button.configure(text="Reveal key")
            feedback_var.set("New shared key generated. Reveal or copy it deliberately for the controller.")

        def toggle_reveal() -> None:
            if peer_entry.cget("show"):
                if not self._messagebox.askyesno(
                    "Reveal shared key?",
                    "Show the shared peer key on this screen? Make sure nobody else can see it.",
                    parent=window,
                ):
                    return
                peer_entry.configure(show="")
                reveal_button.configure(text="Hide key")
            else:
                peer_entry.configure(show="*")
                reveal_button.configure(text="Reveal key")

        def copy_key() -> None:
            try:
                key = validate_peer_key(peer_var.get())
            except InvalidConfigurationError:
                self._show_error("Generate or enter a valid shared peer key before copying it.", parent=window)
                return
            if not self._messagebox.askyesno(
                "Copy shared key?",
                "Copy the shared peer key to this computer's clipboard? Paste it only into "
                "your trusted controller configuration.",
                parent=window,
            ):
                return
            self._root.clipboard_clear()
            self._root.clipboard_append(key)
            self._root.update()
            feedback_var.set("Shared key copied locally. Clear the clipboard after configuring the controller.")

        def save() -> None:
            try:
                self._store.save(
                    DesktopConfig(
                        relay_url=relay_var.get(),
                        token=token_var.get(),
                        peer_key=peer_var.get(),
                    )
                )
            except InvalidConfigurationError as error:
                self._show_error(str(error), parent=window)
                return
            except WindowsConfigError:
                self._show_error("AgentBridge could not save the local settings.", parent=window)
                return

            was_active = self._current_service() is not None
            if was_active:
                if not self.disconnect(
                    detail="Settings saved. Reconnect to enable access with the new settings."
                ):
                    self._show_error(
                        "Settings were saved, but AgentBridge could not confirm the old connection "
                        "was closed. Quit the application before retrying.",
                        parent=window,
                    )
                    return
            else:
                self._set_status("stopped", "Settings saved. Select Connect to enable controller access.")
            self._append_log("Settings saved locally; sensitive values are protected with Windows DPAPI.")
            self._close_settings(window)

        actions = self._ttk.Frame(content)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self._ttk.Button(actions, text="Generate key", command=generate_key).grid(row=0, column=0, padx=(0, 6))
        reveal_button = self._ttk.Button(actions, text="Reveal key", command=toggle_reveal)
        reveal_button.grid(row=0, column=1, padx=6)
        self._ttk.Button(actions, text="Copy key", command=copy_key).grid(row=0, column=2, padx=6)
        self._ttk.Button(actions, text="Save settings", command=save).grid(row=0, column=3, padx=(22, 6))
        self._ttk.Button(actions, text="Cancel", command=lambda: self._close_settings(window)).grid(
            row=0, column=4, padx=(6, 0)
        )

        if first_run:
            relay_entry.focus_set()
        else:
            window.lift()
            window.focus_force()

    def _close_settings(self, window: Any) -> None:
        if self._settings_window is window:
            self._settings_window = None
        try:
            window.destroy()
        except Exception:
            pass

    def show_logs(self) -> None:
        """Display the bounded local tray status log in a visible Tk window."""

        if self._root is None or self._closing.is_set():
            return
        if self._window_is_open(self._logs_window):
            self._logs_window.deiconify()
            self._logs_window.lift()
            self._logs_window.focus_force()
            return

        window = self._tk.Toplevel(self._root)
        self._logs_window = window
        window.title("AgentBridge local logs")
        window.geometry("760x440")
        window.minsize(520, 260)
        window.transient(self._root)
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_logs(window))

        contents: list[str] = []
        try:
            for name in (_PREVIOUS_LOG_FILE_NAME, _LOG_FILE_NAME):
                path = self._store.data_dir / name
                if path.exists():
                    contents.append(f"--- {name} ---\n{path.read_text(encoding='utf-8', errors='replace')}")
        except OSError:
            contents.append("Could not read the local tray log.")
        if not contents:
            contents.append("No local AgentBridge tray log has been written yet.")

        text = self._scrolledtext.ScrolledText(window, wrap="word", state="normal")
        text.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        text.insert("1.0", "\n\n".join(contents))
        text.configure(state="disabled")
        self._ttk.Button(window, text="Close", command=lambda: self._close_logs(window)).pack(
            anchor="e", padx=12, pady=(0, 12)
        )

    def _close_logs(self, window: Any) -> None:
        if self._logs_window is window:
            self._logs_window = None
        try:
            window.destroy()
        except Exception:
            pass

    def open_data_folder(self) -> None:
        """Open the per-user directory containing local logs and operation data."""

        try:
            path = self._store.ensure_data_dir()
            os.startfile(path)  # type: ignore[attr-defined]  # Windows-only application.
        except OSError:
            self._set_status("error", "Could not open the local data folder.")

    def _window_is_open(self, window: Any | None) -> bool:
        if window is None:
            return False
        try:
            return bool(window.winfo_exists())
        except Exception:
            return False

    def _show_error(self, message: str, *, parent: Any | None = None) -> None:
        if self._messagebox is not None:
            self._messagebox.showerror("AgentBridge", message, parent=parent or self._root)

    def _request_quit(self) -> None:
        self._events.put(("action", ("quit",)))

    def _install_exit_cleanup(self) -> None:
        if not self._exit_cleanup_registered:
            atexit.register(self._emergency_stop)
            self._exit_cleanup_registered = True
        # Console control events are uncommon for the windowed build, but this
        # covers an explicit Ctrl+C during development without touching Tk.
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_signal_handlers[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, self._handle_signal)
            except (ValueError, OSError):
                pass

    def _handle_signal(self, _signal_number: int, _frame: Any) -> None:
        self._events.put(("action", ("quit",)))

    def _install_session_cleanup(self) -> None:
        if self._root is None:
            return
        try:
            self._session_cleanup = _WindowsSessionCleanup(
                int(self._root.winfo_id()), self._emergency_stop
            )
        except (AttributeError, OSError):
            # atexit and the normal Quit command remain available if Tcl's
            # native window cannot be subclassed on a particular desktop.
            self._session_cleanup = None

    def _emergency_stop(self) -> None:
        """Stop AgentService without touching Tk during logoff/process exit."""

        self._stop_active_service()
        self._events.put(("session_cleanup", ()))

    def _shutdown(self) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        self._stop_active_service()

        if self._session_cleanup is not None:
            self._session_cleanup.restore()
            self._session_cleanup = None
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
        for signal_number, previous in self._previous_signal_handlers.items():
            try:
                signal.signal(signal_number, previous)
            except (ValueError, OSError):
                pass
        self._previous_signal_handlers.clear()

        if self._root is not None:
            try:
                self._root.quit()
                self._root.destroy()
            except Exception:
                pass


class _WindowsSessionCleanup:
    """Ask AgentService to stop before Windows closes this user's Tk window."""

    _GWL_WNDPROC = -4
    _WM_QUERYENDSESSION = 0x0011
    _WM_ENDSESSION = 0x0016
    _WM_CLOSE = 0x0010

    def __init__(self, hwnd: int, cleanup: Callable[[], None]) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._cleanup = cleanup
        self._hwnd = hwnd
        self._restored = False
        self._long_ptr = ctypes.c_ssize_t
        self._result = ctypes.c_ssize_t
        self._wndproc_type = ctypes.WINFUNCTYPE(
            self._result,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
        self._setter = getattr(self._user32, "SetWindowLongPtrW", self._user32.SetWindowLongW)
        self._setter.argtypes = [wintypes.HWND, ctypes.c_int, self._long_ptr]
        self._setter.restype = self._long_ptr
        self._user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.CallWindowProcW.restype = self._result
        self._callback = self._wndproc_type(self._window_proc)
        callback_address = ctypes.cast(self._callback, ctypes.c_void_p).value
        if callback_address is None:
            raise OSError("Could not install Windows session cleanup.")
        ctypes.set_last_error(0)
        self._previous_proc = self._setter(hwnd, self._GWL_WNDPROC, callback_address)
        if not self._previous_proc and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message in {self._WM_QUERYENDSESSION, self._WM_ENDSESSION, self._WM_CLOSE}:
            try:
                self._cleanup()
            except Exception:
                pass
        return self._user32.CallWindowProcW(self._previous_proc, hwnd, message, wparam, lparam)

    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        try:
            self._setter(self._hwnd, self._GWL_WNDPROC, self._previous_proc)
        except OSError:
            pass


def _show_startup_failure() -> None:
    """Show a visible, secret-free failure message for the windowed executable."""

    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "AgentBridge",
            "AgentBridge could not start. Confirm this is the packaged Windows application "
            "and that its local files are available.",
            parent=root,
        )
        root.destroy()
    except Exception:
        pass


def main() -> int:
    """Entry point for the packaged visible Windows desktop application."""

    if not is_windows():
        sys.stderr.write("AgentBridge desktop is available only on Windows.\n")
        return 1
    try:
        return AgentBridgeTray().run()
    except Exception:
        _show_startup_failure()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
