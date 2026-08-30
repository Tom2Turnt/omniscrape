#!/usr/bin/env bash
# Builds an isolated venv beside the skill and fetches the stealth browsers.
# Nothing is installed into your system Python.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "need python3 on PATH"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' \
  || { echo "need Python 3.10+; found $($PY -V)"; exit 1; }

echo "==> creating .venv"
if command -v uv >/dev/null; then uv venv .venv --python "$PY"; else "$PY" -m venv .venv; fi

echo "==> installing dependencies"
if command -v uv >/dev/null; then
  VIRTUAL_ENV=.venv uv pip install -r requirements.txt
else
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt
fi

echo "==> fetching stealth browser binaries (a few hundred MB, one time)"
.venv/bin/scrapling install || echo "!! 'scrapling install' failed — Tiers 0-1 still work."

chmod +x scripts/omniscrape
echo
echo "Done. Smoke test:"
echo "  ./scripts/omniscrape https://example.com --extract h1"
