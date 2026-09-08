"""PyInstaller entry point for the Windows desktop application only."""

import json
from pathlib import Path
import sys

from agentbridge.tray import main


def self_test(report: Path) -> int:
    result = {"ok": False}
    try:
        import agentbridge.agent
        import agentbridge.operations
        import pystray._win32
        from PIL import Image, ImageDraw
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
        from agentbridge.windows_config import dpapi_protect, dpapi_unprotect

        sample = b"AgentBridge packaging check: not a real secret"
        if dpapi_unprotect(dpapi_protect(sample)) != sample:
            raise RuntimeError("Packaged Windows DPAPI round-trip failed")
        ImageDraw.Draw(Image.new("RGB", (16, 16))).point((0, 0))
        root = tk.Tk()
        try:
            root.withdraw()
            ttk.Label(root, text="AgentBridge packaging check").pack()
            scrolledtext.ScrolledText(root).pack()
            root.update_idletasks()
        finally:
            root.destroy()
        result.update(ok=True, checks=["imports", "dpapi", "pillow", "tk"])
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test(Path(sys.argv[2])))
    raise SystemExit(main())
