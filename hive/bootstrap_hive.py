#!/usr/bin/env python3
"""
Hive Memory Bootstrap - Phase 0
Enriches existing LanceDB records with hive schema fields
and ingests all existing knowledge sources.
"""

import lancedb
import pyarrow as pa
import pandas as pd
from sentence_transformers import SentenceTransformer
import os
import uuid
import time
import re
import glob
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
from hive_schema import LANCEDB_PATH, TABLE_NAME, EMBED_MODEL, EMBED_DIM
WORKSPACE    = Path("/home/qtxit/.openclaw-eighteen/workspace")
BATCH_SIZE   = 64
MAX_CHUNK    = 3000  # chars per chunk, keep under MiniLM sweet spot

_embedder = SentenceTransformer(EMBED_MODEL)

errors = []
new_records = []
stats = {"existing_processed": 0, "new_ingested": 0,
         "genome": 0, "hive": 0, "private": 0}

# ── Helpers ───────────────────────────────────────────────────────────────────

def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via the local MiniLM model."""
    vectors = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = [t[:MAX_CHUNK] for t in texts[i:i+BATCH_SIZE]]
        try:
            vecs = _embedder.encode(batch)
            vectors.extend([v.tolist() for v in vecs])
        except Exception as e:
            vec_len = EMBED_DIM
            vectors.extend([[0.0] * vec_len for _ in batch])
    return vectors


def classify_layer(text: str) -> tuple[str, str]:
    """Returns (layer, owner_agent) for an existing record."""
    t = text or ""
    if any(sig in t for sig in ["🜂 Thea", "IMfA", "INSTITUTIONAL_MEMORY"]):
        return "genome", "collective"
    for agent in ["iris", "guru", "pythagoras"]:
        if agent in t.lower():
            return "hive", agent
    return "hive", "thea"


def split_sections(text: str, min_chars: int = 100) -> list[str]:
    """Split markdown into sections at ## headings, then sub-chunk large sections."""
    parts = re.split(r'\n(?=#{1,3} )', text)
    chunks = []
    for p in parts:
        p = p.strip()
        if len(p) < min_chars:
            continue
        # Sub-chunk if too large
        if len(p) <= MAX_CHUNK:
            chunks.append(p)
        else:
            # Split on paragraph boundaries
            paras = re.split(r'\n{2,}', p)
            current = ""
            for para in paras:
                if len(current) + len(para) + 2 > MAX_CHUNK:
                    if current:
                        chunks.append(current.strip())
                    current = para
                else:
                    current = current + "\n\n" + para if current else para
            if current and len(current.strip()) >= min_chars:
                chunks.append(current.strip())
    return chunks


def make_record(text: str, vector: list[float], layer: str,
                owner_agent: str, source: str,
                decay_class: str = "permanent") -> dict:
    now = time.time()
    return {
        "id": str(uuid.uuid4()),
        "text": text,
        "vector": vector,
        "decay_class": decay_class,
        "created_at": now,
        "layer": layer,
        "owner_agent": owner_agent,
        "score": 0.5,
        "family_id": "",
        "activation_threshold": 0.3,
        "source": source,
        "updated_at": now,
    }


# ── Phase 0a: Open table and migrate schema ───────────────────────────────────

print("Connecting to LanceDB…")
db = lancedb.connect(LANCEDB_PATH)
tables_result = db.list_tables()
# list_tables() may return a TableList object; extract names
if hasattr(tables_result, 'tables'):
    table_names = [t if isinstance(t, str) else t.name for t in tables_result.tables]
elif isinstance(tables_result, list):
    table_names = tables_result
else:
    table_names = list(tables_result)
print(f"Tables: {table_names}")

missing = []  # schema migration columns
if TABLE_NAME not in table_names:
    print(f"Table {TABLE_NAME} not found – will create fresh.")
    tbl = None
else:
    tbl = db.open_table(TABLE_NAME)
    existing_count = tbl.count_rows()
    print(f"Existing rows: {existing_count}")

# Load all existing rows
if tbl is not None:
    df = tbl.to_pandas()
    existing_cols = set(df.columns.tolist())
    print(f"Existing columns: {existing_cols}")

    NEW_COLS = ["layer", "owner_agent", "score", "family_id", "activation_threshold",
                "source", "updated_at"]
    missing = [c for c in NEW_COLS if c not in existing_cols]

    if missing:
        print(f"Adding missing columns: {missing}")
        # Assign defaults based on text content
        layers, owners = zip(*[classify_layer(t) for t in df["text"].fillna("")])
        now = time.time()
        df["layer"]               = list(layers)
        df["owner_agent"]         = list(owners)
        df["score"]               = 0.5
        df["family_id"]           = ""
        df["activation_threshold"]= 0.3
        df["source"]              = "migrated"
        df["updated_at"]          = now
        stats["existing_processed"] = len(df)
    else:
        print("All new columns already present – no migration needed.")
        stats["existing_processed"] = len(df)

    # Count layers in existing data
    for layer in ["genome", "hive", "private"]:
        stats[layer] += int((df["layer"] == layer).sum())
else:
    df = pd.DataFrame()

# ── Phase 0b: Ingest new sources ──────────────────────────────────────────────

# Gather already-indexed sources to avoid duplication
existing_sources = set()
if "source" in df.columns:
    existing_sources = set(df["source"].dropna().unique())

# -- 1. Daily memory files (layer=private, owner=thea) --
memory_files = sorted(glob.glob(str(WORKSPACE / "memory" / "*.md")))
print(f"\nIngesting {len(memory_files)} daily memory files…")
for fpath in memory_files:
    src_key = f"memory/{Path(fpath).name}"
    if src_key in existing_sources:
        continue
    try:
        text = Path(fpath).read_text(encoding="utf-8", errors="replace")
        sections = split_sections(text)
        if not sections:
            sections = [text[:2000]] if len(text) > 50 else []
        if not sections:
            continue
        vecs = embed(sections)
        for chunk, vec in zip(sections, vecs):
            new_records.append(make_record(chunk, vec, "private", "thea",
                                           src_key, "active"))
        print(f"  ✓ {src_key} ({len(sections)} chunks)")
    except Exception as e:
        errors.append(f"memory/{Path(fpath).name}: {e}")
        print(f"  ✗ {src_key}: {e}")

# -- 2. IMfA file (layer=genome, owner=collective) --
imfa_path = WORKSPACE / "docs" / "INSTITUTIONAL_MEMORY.md"
if imfa_path.exists():
    src_key = "docs/INSTITUTIONAL_MEMORY.md"
    if src_key not in existing_sources:
        print(f"\nIngesting IMfA…")
        try:
            text = imfa_path.read_text(encoding="utf-8", errors="replace")
            sections = split_sections(text)
            vecs = embed(sections)
            for chunk, vec in zip(sections, vecs):
                new_records.append(make_record(chunk, vec, "genome", "collective",
                                               src_key, "permanent"))
            print(f"  ✓ IMfA ({len(sections)} sections)")
        except Exception as e:
            errors.append(f"IMfA: {e}")

# -- 3. Specialist identity files (layer=genome, owner=collective) --
agent_dirs = {
    "thea":       WORKSPACE,
    "iris":       Path("/home/qtxit/.openclaw-eighteen/workspace/teams/iris"),
    "guru":       Path("/home/qtxit/.openclaw-eighteen/workspace/teams/guru"),
    "pythagoras": Path("/home/qtxit/.openclaw-eighteen/workspace/teams/pythagoras"),
}
identity_files = ["AGENTS.md", "SOUL.md", "IDENTITY.md"]

print("\nIngesting specialist identity files…")
for agent, wdir in agent_dirs.items():
    for fname in identity_files:
        fpath = wdir / fname
        if not fpath.exists():
            continue
        src_key = f"{agent}/{fname}"
        if src_key in existing_sources:
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
            sections = split_sections(text)
            if not sections:
                sections = [text[:2000]] if len(text) > 50 else []
            if not sections:
                continue
            vecs = embed(sections)
            for chunk, vec in zip(sections, vecs):
                new_records.append(make_record(chunk, vec, "genome", "collective",
                                               src_key, "permanent"))
            print(f"  ✓ {src_key} ({len(sections)} chunks)")
        except Exception as e:
            errors.append(f"{src_key}: {e}")
            print(f"  ✗ {src_key}: {e}")

# ── Phase 0c: Merge and write back ────────────────────────────────────────────

# Count new records by layer
for rec in new_records:
    stats[rec["layer"]] += 1
stats["new_ingested"] = len(new_records)

print(f"\nNew records to write: {len(new_records)}")

# Define final schema
schema = pa.schema([
    pa.field("id",                   pa.utf8()),
    pa.field("text",                 pa.utf8()),
    pa.field("vector",               pa.list_(pa.float32(), EMBED_DIM)),
    pa.field("decay_class",          pa.utf8()),
    pa.field("created_at",           pa.float64()),
    pa.field("layer",                pa.utf8()),
    pa.field("owner_agent",          pa.utf8()),
    pa.field("score",                pa.float64()),
    pa.field("family_id",            pa.utf8()),
    pa.field("activation_threshold", pa.float64()),
    pa.field("source",               pa.utf8()),
    pa.field("updated_at",           pa.float64()),
])

new_df = pd.DataFrame(new_records) if new_records else pd.DataFrame()

if tbl is not None:
    if missing:
        # Need to recreate table with new schema (drop + create)
        print("\nRecreating table with migrated schema + new records…")
        all_df = pd.concat([df, new_df], ignore_index=True) if not new_df.empty else df
        db.drop_table(TABLE_NAME)
        tbl = db.create_table(TABLE_NAME, data=all_df, schema=schema)
        print(f"Table recreated with {len(all_df)} total rows.")
    elif not new_df.empty:
        # Schema already up to date — just add new records
        tbl.add(new_df)
        print(f"Added {len(new_df)} new records to existing table.")
    else:
        print("No new records to add.")
else:
    # Fresh table creation
    if new_df.empty:
        print("No records to write. Exiting.")
    else:
        tbl = db.create_table(TABLE_NAME, data=new_df, schema=schema)
        print(f"Created new table with {len(new_df)} records.")

final_count = tbl.count_rows() if tbl else 0
print(f"\nFinal row count: {final_count}")

# ── Write bootstrap report ────────────────────────────────────────────────────

report_path = WORKSPACE / "hive" / "bootstrap_report.md"
now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

report = f"""# Hive Memory Bootstrap Report
Generated: {now_str}

## Summary
| Metric | Value |
|--------|-------|
| Existing records processed | {stats['existing_processed']} |
| New records ingested | {stats['new_ingested']} |
| **Total records in store** | **{final_count}** |

## Layer Distribution
| Layer | Count |
|-------|-------|
| genome | {stats['genome']} |
| hive | {stats['hive']} |
| private | {stats['private']} |

## Sources Ingested
- Daily memory files: {len(memory_files)} files
- IMfA: 1 file (docs/INSTITUTIONAL_MEMORY.md)
- Specialist identity files: AGENTS.md, SOUL.md, IDENTITY.md for each agent

## Errors ({len(errors)})
"""
if errors:
    for e in errors:
        report += f"- {e}\n"
else:
    report += "None\n"

report_path.write_text(report)
print(f"\nBootstrap report written to {report_path}")
print(f"\nStats: {stats}")
print(f"Errors: {len(errors)}")
