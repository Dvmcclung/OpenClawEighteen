import lancedb
import json

db = lancedb.connect(os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb"))
print("Tables:", db.table_names())
for tname in db.table_names():
    t = db.open_table(tname)
    print(f"\nTable: {tname}")
    print(f"Schema: {t.schema}")
    print(f"Row count: {t.count_rows()}")
    rows = t.to_pandas().head(2)
    print(rows.to_string())
