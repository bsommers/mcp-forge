#!/usr/bin/env bash
# Usage: validate-mcp.sh [--allow-skip] [--strict] <server_dir>
# Exits non-zero if any step fails, or is skipped (unless --allow-skip).
set -u
here="$(cd "$(dirname "$0")" && pwd)"
[ $# -ge 1 ] || { echo "usage: $0 [--allow-skip] [--strict] <server_dir>" >&2; exit 2; }
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec "${FORGE_PYTHON:-python3}" -m forge.validate "$@"
