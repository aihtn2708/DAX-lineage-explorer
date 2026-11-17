"""Write the raw rows and Python's lineage for a sample folder (and optionally a
report), for the JS parity test."""
import csv, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dax_lineage.graph import build_graph  # noqa: E402
from dax_lineage.load import autodiscover, load_catalogue  # noqa: E402
from dax_lineage.pbi_report import read_report  # noqa: E402

folder = Path(sys.argv[1])
found = autodiscover(folder)
found.pop("relationships_path", None)
sources = []
for path in found.values():
    with open(path, encoding="utf-8", newline="") as fh:
        sources.append({"label": path.stem, "rows": list(csv.DictReader(fh))})
# relationships stay in the catalogue (they mark key columns as used) but draw no edges
found_rel = autodiscover(folder).get("relationships_path")
if found_rel:
    with open(found_rel, encoding="utf-8", newline="") as fh:
        sources.append({"label": found_rel.stem, "rows": list(csv.DictReader(fh))})
    found["relationships_path"] = found_rel
usages = read_report(sys.argv[2]).usages if len(sys.argv) > 2 else None
print(json.dumps({"sources": sources, "python": build_graph(load_catalogue(**found), usages=usages).to_dict()}))
