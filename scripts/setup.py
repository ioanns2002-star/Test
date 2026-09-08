"""Idempotent local setup for the relay/controller and integration tests."""

import os
from pathlib import Path
import subprocess
import sys
import venv

root = Path(__file__).resolve().parents[1]
environment = root / ".venv"
venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(environment)
python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-e", str(root)], check=True)
