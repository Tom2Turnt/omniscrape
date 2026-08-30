#!/usr/bin/env bash
# Builds an isolated venv beside the skill and fetches the stealth browsers.
# Nothing is installed into your system Python.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

pick_python() {
  local c
  for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null \
      && { command -v "$c"; return 0; }
  done
  return 1
}

# Apple's bundled python3 is 3.9.6, and both parsel and scrapling require 3.10+.
# So look past it before giving up, and let uv fetch one if nothing suitable exists.
echo "==> creating .venv"
if PY="$(pick_python)"; then
  echo "    using $PY ($("$PY" -V 2>&1))"
  if command -v uv >/dev/null; then uv venv .venv --python "$PY"
  else "$PY" -m venv .venv || { echo "venv creation failed — on Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
  fi
elif command -v uv >/dev/null; then
  echo "    no Python 3.10+ on PATH — letting uv fetch one"
  uv venv .venv --python 3.12
else
  echo "need Python 3.10+ (found: $(python3 -V 2>&1)). Install it, or install uv:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "==> installing dependencies"
if command -v uv >/dev/null; then
  uv pip install --python .venv/bin/python -r requirements.txt
else
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt
fi

echo "==> fetching stealth browser binaries (a few hundred MB, one time)"
# On Linux this also runs 'playwright install-deps', which shells out to sudo.
# </dev/null so it can't block on a password prompt inside the installer.
.venv/bin/scrapling install </dev/null || {
  echo "!! 'scrapling install' failed — Tier 0 (plain HTTP) still works; the browser tiers will not."
  echo "   On Linux you may need: sudo .venv/bin/playwright install-deps chromium"
}

chmod +x scripts/omniscrape
echo
echo "Done. Smoke test:"
echo "  ./scripts/omniscrape https://example.com --extract h1"
