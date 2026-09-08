#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! python3 scripts/check_environment.py --require >/dev/null 2>&1; then
  if [[ "$(id -u)" != 0 ]] || ! command -v apt-get >/dev/null 2>&1; then
    printf '%s\n' 'Install Python NumPy and Pillow, then run setup again.' >&2
    exit 1
  fi
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-numpy python3-pil fonts-dejavu-core libxi6 libxrender1 libxxf86vm1 libxfixes3 libsm6 libgl1 libegl1 libegl-mesa0 libgles2 mesa-vulkan-drivers xvfb xauth curl
fi
python3 scripts/install_blender.py
