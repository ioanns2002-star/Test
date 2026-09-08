#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f package-lock.json ]]; then
  npm ci --no-audit --no-fund
fi
bash scripts/install-tools.sh
