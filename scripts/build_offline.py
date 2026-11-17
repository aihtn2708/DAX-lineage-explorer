"""Build the single-file offline explorer from docs/.

    python3 scripts/build_offline.py

Writes the same page twice: docs/dax-lineage-explorer.html (offered as a
download on the site) and dax_lineage/web/dax-lineage-explorer.html (what
`daxlin serve` and `daxlin offline` use). tests/test_local.py fails if either
is out of date.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dax_lineage.offline import build  # noqa: E402

html = build(ROOT / "docs")
for target in (ROOT / "docs" / "dax-lineage-explorer.html", ROOT / "dax_lineage" / "web" / "dax-lineage-explorer.html"):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    print(f"wrote {target.relative_to(ROOT)} ({len(html.encode()) // 1024} KB)")
