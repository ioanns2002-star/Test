"""Use the official OIDN-enabled build, not distribution builds without denoising."""

import hashlib
from pathlib import Path
import platform
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "4.5.3"
NAME = f"blender-{VERSION}-linux-x64"
SHA256 = "975c58fcb244273838534bba771e64ad87739216b0f9b39a888531a49a72d845"
TOOLS = ROOT / ".tools"


def main():
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        print("Install Blender 4.5 LTS from https://www.blender.org/download/lts/4-5/ on this OS.")
        return
    binary = TOOLS / NAME / "blender"
    if binary.exists():
        print(f"Official Blender {VERSION} already installed: {binary}")
        return
    TOOLS.mkdir(exist_ok=True)
    archive = TOOLS / f"{NAME}.tar.xz"
    if not archive.exists():
        partial = archive.with_suffix(".part")
        url = f"https://download.blender.org/release/Blender4.5/{NAME}.tar.xz"
        print(f"Downloading official Blender {VERSION}", flush=True)
        subprocess.run(["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "30", "--output", str(partial), url], check=True)
        partial.replace(archive)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != SHA256:
        archive.unlink()
        raise RuntimeError("Blender SHA-256 mismatch; untrusted archive removed")
    with tarfile.open(archive) as package:
        package.extractall(TOOLS, filter="data")
    archive.unlink()
    print(f"Installed and checksum-verified: {binary}")


if __name__ == "__main__":
    main()
