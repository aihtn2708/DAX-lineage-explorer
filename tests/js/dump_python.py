"""Write the raw rows and Python's lineage for a sample folder, for the JS parity test."""
import csv, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dax_lineage.graph import build_graph  # noqa: E402
from dax_lineage.load import autodiscover, load_catalogue  # noqa: E402

folder = Path(sys.argv[1])
found = autodiscover(folder)
found.pop("relationships_path", None)
sources = []
for path in found.values():
    with open(path, encoding="utf-8", newline="") as fh:
        sources.append({"label": path.stem, "rows": list(csv.DictReader(fh))})
print(json.dumps({"sources": sources, "python": build_graph(load_catalogue(**found)).to_dict()}))
