#!/bin/sh
# Local pre-push gate: the same checks CI runs (ruff + pytest).
set -e
cd "$(dirname "$0")/.."
python3 -m ruff check .
python3 -m pytest -q
