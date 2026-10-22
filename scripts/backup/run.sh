#!/bin/bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3.14 >/dev/null 2>&1; then
  echo "ERRORE: installare CPython 3.14.9 o patch successiva del ramo 3.14. Vedi scripts/backup/README.md." >&2
  exit 1
fi
exec python3.14 -E -s "$SCRIPT_DIR/backup.py" "$@"
