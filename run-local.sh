#!/bin/sh
# DAX lineage explorer on this computer only: ./run-local.sh
# Opens http://127.0.0.1:8765 in your browser. Ctrl+C to stop.
# Needs Python 3.9 or later; without it, the offline page opens instead.
cd "$(dirname "$0")" || exit 1
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    exec "$py" -m dax_lineage serve "$@"
  fi
done
echo "Python 3.9 or later was not found, so the offline page opens instead (it needs no Python)."
case "$(uname)" in
  Darwin) open docs/dax-lineage-explorer.html ;;
  *) xdg-open docs/dax-lineage-explorer.html 2>/dev/null || echo "Open docs/dax-lineage-explorer.html in your browser." ;;
esac
