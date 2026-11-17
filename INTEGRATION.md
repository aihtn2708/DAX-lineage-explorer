# Folding this into OpenTabular

This started as a standalone tool so it can be run against a Power BI model
before anything else exists. Inside `opentabular` it slots in as a second
front end onto the same lineage idea the project already has.

## What already overlaps

`src/opentabular/lineage.py` derives measure lineage from the **DAX tree** that
`src/opentabular/dax/` parses, for measures read out of **TMDL**. This tool
derives the same lineage from **`INFO.MEASURES()` output** — the live model
rather than a TMDL folder — using a scanner rather than the parser.

So the two differ only in where the DAX comes from, and that suggests the shape
of the merge.

## Suggested shape

1. **Keep the loader.** `dax_lineage/load.py` has no counterpart in the project
   and is where the tolerance for real-world exports lives (CSV/TSV/JSON, the
   REST envelope, `Measures[Name]` style headers). Move it to
   `src/opentabular/info.py` roughly unchanged.

2. **Drop the scanner in favour of the real parser.** `dax_lineage/dax.py`
   exists because this tool had to stand alone. Inside the project, the Pratt
   parser is better: it already resolves measure references for
   `lineage.py`, and it knows the precedence rules. Replace `scan()` with a
   call into `opentabular.dax` and keep `Reference`/`Scan` as the shared shape
   so `graph.py` does not change. The scanner's one advantage — it never fails
   on DAX the parser cannot yet handle — is worth keeping as a fallback path
   for measures that raise.

3. **Converge the graph.** `graph.py` here produces measure, column and table
   nodes with typed edges, depth, cycles and the statistics. If
   `lineage.py` already produces something similar, keep one of them and make
   the other a thin adapter; the HTML page only needs `to_dict()`.

4. **Two CLI verbs**, both on the existing `otab`:

   ```
   otab lineage --html lineage.html                 # from the TMDL model
   otab lineage --from-info exports/ --html out.html  # from a live model's INFO.* output
   ```

5. **Reuse the page.** `template.html` is self-contained and reads one JSON
   blob, so `otab serve` can hand it the same payload live at `/lineage`, which
   gives the metrics explorer a lineage tab for free.

## Why this is worth doing before the compatibility harness

The next milestone in the brief is running the same queries against Power BI or
Fabric and comparing. Getting `INFO.MEASURES` out of a real model is the first
step of that harness anyway: it is how the measures under test are enumerated,
and `INFO.TABLES` / `INFO.COLUMNS` / `INFO.RELATIONSHIPS` describe the model the
harness must reproduce. The loader here is that step, already written and
tested, and the lineage page is a useful thing to have in the meantime.

It also helps with a gap in the brief: the tool reports exactly which DAX
functions each measure uses, so running it over a real model gives a ranked list
of what the compiler would have to support to handle that model, measured
against `docs/dax-coverage.md` rather than guessed.

```bash
daxlin json -s exports/ -o lineage.json
python3 - <<'PY'
import collections, json
nodes = json.load(open("lineage.json"))["nodes"]
print(collections.Counter(f for n in nodes for f in n["functions"]).most_common(25))
PY
```
