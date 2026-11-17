"""A metadata.sqlitedb like the one inside a .pbix, built from the sample CSVs.

    python3 tests/js/make_metadata_db.py OUT.sqlitedb

Same table and column names as the engine's own (TMSCHEMA), the same
PRIMARY KEY("ID" ASC) row-id alias, plus the system tables a real model
carries (H$ hierarchy storage, a private date template) and a RowNumber
column, so the reader's filtering is exercised too. A long expression forces
SQLite overflow pages.
"""
import csv, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1])
out.unlink(missing_ok=True)
con = sqlite3.connect(out)
con.execute('PRAGMA page_size = 1024')
con.executescript('''
CREATE TABLE [Table]( [ID] INTEGER , [ModelID] INTEGER , [Name] TEXT , [Description] TEXT , [IsHidden] BOOLEAN ,
  [SystemFlags] INTEGER , [ShowAsVariationsOnly] BOOLEAN , [IsPrivate] BOOLEAN , [ModifiedTime] INTEGER ,
  PRIMARY KEY("ID" ASC) );
CREATE TABLE [Column]( [ID] INTEGER , [TableID] INTEGER , [ExplicitName] TEXT , [InferredName] TEXT ,
  [ExplicitDataType] INTEGER , [IsHidden] BOOLEAN , [Type] INTEGER , [Expression] TEXT , [DisplayFolder] TEXT ,
  [SortByColumnID] INTEGER , PRIMARY KEY("ID" ASC) );
CREATE TABLE [Measure]( [ID] INTEGER , [TableID] INTEGER , [Name] TEXT , [Description] TEXT , [DataType] INTEGER ,
  [Expression] TEXT , [FormatString] TEXT , [IsHidden] BOOLEAN , [State] INTEGER , [ErrorMessage] TEXT ,
  [DisplayFolder] TEXT , [ModifiedTime] INTEGER , PRIMARY KEY("ID" ASC) );
CREATE TABLE [Relationship]( [ID] INTEGER , [FromTableID] INTEGER , [FromColumnID] INTEGER ,
  [ToTableID] INTEGER , [ToColumnID] INTEGER , [IsActive] BOOLEAN , PRIMARY KEY("ID" ASC) );
CREATE TABLE [Model]( [ID] INTEGER PRIMARY KEY, [Name] TEXT );
''')
read = lambda name: list(csv.DictReader((ROOT / "examples" / name).open(encoding="utf-8", newline="")))
big = 133083615403533319   # a FILETIME past 2^53, as real models have
for t in read("info_tables.csv"):
    con.execute("insert into [Table] values (?,?,?,?,?,?,?,?,?)",
                (int(t["ID"]), 1, t["Name"], t["Description"], t["IsHidden"] == "True", 0, 0, 0, big))
con.execute("insert into [Table] values (900,1,'DateTableTemplate_1',NULL,1,2,0,1,?)", (big,))
con.execute("insert into [Table] values (901,1,'H$Sales (1)$OrderNumber (101)',NULL,1,1,0,0,?)", (big,))
for c in read("info_columns.csv"):
    con.execute("insert into [Column] values (?,?,?,?,?,?,?,?,?,?)",
                (int(c["ID"]), int(c["TableID"]), c["ExplicitName"], None, 2, c["IsHidden"] == "True",
                 2 if c["Expression"] else 1, c["Expression"] or None, c["DisplayFolder"] or None,
                 int(c["SortByColumnID"]) if c.get("SortByColumnID") else None))
for tid in (1, 2, 3, 4, 5):   # every real table has a hidden RowNumber column
    con.execute("insert into [Column] values (?,?,?,?,?,?,?,?,?,?)", (1000 + tid, tid, None, "RowNumber-2662979B", 6, 1, 3, None, None, None))
con.execute("insert into [Column] values (9001,901,'Key',NULL,2,1,1,NULL,NULL,NULL)")
for m in read("info_measures.csv"):
    expr = m["Expression"]
    if m["Name"] == "Discount Impact":
        expr += "\n// " + ("a long comment that forces SQLite overflow pages " * 60)
    con.execute("insert into [Measure] values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(m["ID"]), int(m["TableID"]), m["Name"], m["Description"] or None, 6, expr,
                 m["FormatString"] or None, m["IsHidden"] == "True", 1, None, m["DisplayFolder"] or None, big))
for r in read("info_relationships.csv"):
    con.execute("insert into [Relationship] values (?,?,?,?,?,?)",
                (int(r["ID"]), int(r["FromTableID"]), int(r["FromColumnID"]), int(r["ToTableID"]), int(r["ToColumnID"]), True))
con.execute("insert into [Model] values (1, 'Model')")
con.commit()
con.close()
print(out)
