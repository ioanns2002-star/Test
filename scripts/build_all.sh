#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

blender="${BLENDER_BIN:-.tools/blender-4.5.3-linux-x64/blender}"
if [[ ! -x "$blender" ]] && [[ -z "${BLENDER_BIN:-}" ]]; then
  blender="$(command -v blender || true)"
fi
if [[ -z "$blender" ]] || [[ ! -x "$blender" ]]; then
  printf '%s\n' 'Install Blender 4.5 LTS or set BLENDER_BIN to its executable.' >&2
  exit 1
fi

mkdir -p build
threads="${BLENDER_THREADS:-4}"
python3 scripts/make_textures.py
"$blender" -b -t "$threads" --python-exit-code 1 --python scripts/build_scene.py -- --quality "${QUALITY:-final}" > build/render-final.log 2>&1
"$blender" -b public/downloads/rain-gallery.blend -t "$threads" --python-exit-code 1 --python scripts/verify_blender.py > build/verify-blender.log 2>&1
"$blender" -b public/downloads/rain-gallery.blend -t "$threads" --python-exit-code 1 --python scripts/render_views.py > build/render-views.log 2>&1
python3 scripts/prepare_images.py
npm test
npm run validate:model
npm run build
npm run build:offline
python3 scripts/package_godot.py
npm run build:catalog
npm run verify:artifacts
npm run build
printf '%s\n' 'Scene, render views, standalone HTML, Godot ZIP and dist/ are ready.'
