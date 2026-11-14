#!/bin/sh
# Link `wiki` into ~/.local/bin, create the data repo and show what is left to wire.
#   ./install.sh                 the CLI and an empty wiki
#   ./install.sh claude-code     also the Claude Code session hook (or: omp)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
command -v python3 >/dev/null || { echo "install: python3 (3.9 or newer) is required" >&2; exit 1; }
command -v git >/dev/null || { echo "install: git is required" >&2; exit 1; }
python3 "$here/wiki.py" install bin "$@"
python3 "$here/wiki.py" init
python3 "$here/wiki.py" doctor || true
