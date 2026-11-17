#!/bin/sh
# Power BI team workspace on this computer only.
#   ./run-workspace.sh add-folder "$HOME/Contoso/BI - Reports"
#   ./run-workspace.sh scan
#   ./run-workspace.sh                # no arguments: opens http://127.0.0.1:8766
cd "$(dirname "$0")" || exit 1
: "${DAXLIN_WORKSPACE_DB:=$(pwd)/pbi-workspace.db}"
export DAXLIN_WORKSPACE_DB
if [ $# -eq 0 ]; then set -- serve; fi
exec python3 -m dax_lineage workspace "$@"
