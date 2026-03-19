"""
Daily Memory Ingest — Hive Phase maintenance
Runs nightly after session_distill. Ingests today's memory file into the hive store.
"""
import os
import sys
import time
import json
import lancedb
import uuid
import numpy as np
from sentence_transformers import SentenceTransformer

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
from hive_schema import EMBED_MODEL
LANCEDB_PATH = os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb")
TABLE_NAME = "hybrid_facts"
LOG_FILE = os.path.join(WORKSPACE, "system/agent_activity.log")

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] INGEST {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + "\n")

def get_embedding(text):
    try:
        import openai
        config_path = os.path.expanduser("~/.openclaw/openclaw.json")
        with open(config_path) as f:
            config = json.load(f)
        api_key = config.get("openai", {}).get("apiKey") or os.environ.get("OPENAI_API_KEY")
        if api_key:
            client = openai.OpenAI(api_key=api_key)
            response = client.embeddings.create(input=text[:8000], model="text-embedding-ada-002")
            return response.data[0].embedding
    except Exception as e:
        log(f"Embedding error: {e}")
    return None

def chunk_text(text, max_chars=3000):
    """
    Split text into chunks at paragraph boundaries (double newlines).
    Chunks stay within max_chars but never split mid-paragraph — each chunk
    is a complete set of paragraphs, making vectors semantically coherent.
    Minimum chunk size: 50 chars (filters whitespace-only splits).
    """
    paragraphs = text.split('\n\n')
    chunks = []
    current = []
    current_len = 0
    for p in paragraphs:
        if current_len + len(p) > max_chars and current:
            chunks.append('\n\n'.join(current))
            current = [p]
            current_len = len(p)
        else:
            current.append(p)
            current_len += len(p)
    if current:
        chunks.append('\n\n'.join(current))
    return [c for c in chunks if len(c.strip()) > 50]

# Other agent workspace roots for cross-pollination ingest.
# Add new agents here as they come online.
OTHER_AGENT_WORKSPACES = {
    "iris":        "/home/qtxit/.openclaw-eighteen/workspace/teams/iris",
    "guru":        "/home/qtxit/.openclaw-eighteen/workspace/teams/guru",
    "pythagoras":  "/home/qtxit/.openclaw-eighteen/workspace/teams/pythagoras",
}

# shared_insights/ directory — any agent can drop a .md file here for
# immediate cross-agent availability (no waiting for next midnight ingest).
SHARED_INSIGHTS_DIR = os.path.join(WORKSPACE, "shared_insights")


def _make_record(text, vector, layer, owner_agent, source_label):
    """Build a LanceDB record dict with all required schema fields."""
    uid = str(uuid.uuid4())
    return {
        'id': uid,
        'memory_id': uid[:16],
        'text': text,
        'vector': vector,
        'layer': layer,
        'owner_agent': owner_agent,
        'score': 0.5,
        'family_id': '',
        'activation_threshold': 0.3,
        'source': source_label,
        'created_at': time.time(),
        'updated_at': time.time(),
        'decay_class': 'active',
        'tag_domain': '',
        'tag_type': 'fact',
        'tag_source': 'session',
        'tag_status': 'active',
        'superseded_by': '',
        'surfacing_threshold_override': 0.0,
        'surface_count': 0,
    }


def ingest_file(db, file_path, source_label, owner_agent, layer="hive"):
    """
    Ingest a single markdown file into LanceDB.
    Skips if already ingested (checks source column).
    Returns number of records added.
    """
    if not os.path.exists(file_path):
        return 0

    if TABLE_NAME in db.table_names():
        table = db.open_table(TABLE_NAME)
        df = table.to_pandas()
        if 'source' in df.columns:
            already = df[df['source'] == source_label]
            if len(already) > 0:
                log(f"Already ingested {source_label} ({len(already)} chunks), skipping")
                return 0

    with open(file_path) as f:
        content = f.read()

    chunks = chunk_text(content)
    log(f"Ingesting {source_label}: {len(chunks)} chunks (owner={owner_agent}, layer={layer})")

    # Load existing vectors once for deduplication checks
    existing_vectors = []
    if TABLE_NAME in db.table_names():
        existing_df = db.open_table(TABLE_NAME).to_pandas()
        if 'vector' in existing_df.columns:
            existing_vectors = existing_df['vector'].dropna().tolist()

    records = []
    skipped_dupes = 0
    for chunk in chunks:
        vector = get_embedding(chunk)
        if vector is None:
            continue

        # Deduplication: skip if cosine similarity > 0.87 with any existing vector
        # Only compare if dims match (guard against mixed-dim tables after re-embedding)
        if existing_vectors:
            v = np.array(vector)
            v_norm = v / (np.linalg.norm(v) + 1e-9)
            is_dupe = False
            for ev in existing_vectors:
                ev_arr = np.array(ev)
                if len(ev_arr) != len(v):
                    continue  # skip dim mismatch — different embedding model
                ev_norm = ev_arr / (np.linalg.norm(ev_arr) + 1e-9)
                sim = float(np.dot(v_norm, ev_norm))
                if sim > 0.87:
                    skipped_dupes += 1
                    is_dupe = True
                    break
            if not is_dupe:
                records.append(_make_record(chunk, vector, layer, owner_agent, source_label))
        else:
            # No existing vectors yet — no dedup needed
            records.append(_make_record(chunk, vector, layer, owner_agent, source_label))

    if skipped_dupes:
        log(f"Dedup: skipped {skipped_dupes} near-duplicate chunks (cosine > 0.87) from {source_label}")

    if not records:
        return 0

    if TABLE_NAME in db.table_names():
        table = db.open_table(TABLE_NAME)
        table.add(records)
    else:
        db.create_table(TABLE_NAME, data=records)

    return len(records)


def main():
    today = time.strftime("%Y-%m-%d")
    db = lancedb.connect(LANCEDB_PATH)
    total_ingested = 0

    # ── 1. Thea's own daily memory file ───────────────────────────────────
    memory_file = os.path.join(WORKSPACE, f"memory/{today}.md")
    n = ingest_file(db, memory_file, f"memory/{today}.md", "thea", layer="private")
    total_ingested += n
    if n == 0 and not os.path.exists(memory_file):
        log(f"No memory file for {today}, skipping thea daily ingest")

    # ── 2. shared_insights/ — any agent can write here for fast ingestion ─
    os.makedirs(SHARED_INSIGHTS_DIR, exist_ok=True)
    insight_files = sorted([
        f for f in os.listdir(SHARED_INSIGHTS_DIR)
        if f.endswith('.md') or f.endswith('.txt')
    ])
    for fname in insight_files:
        fpath = os.path.join(SHARED_INSIGHTS_DIR, fname)
        source_label = f"shared_insights/{fname}"
        # Infer owner from filename convention: "agentname_*.md"
        owner = fname.split("_")[0] if "_" in fname else "unknown"
        n = ingest_file(db, fpath, source_label, owner, layer="hive")
        total_ingested += n

    # ── 3. Other agents' workspace memory files (cross-pollination) ───────
    # Ingests their today file AND any unprocessed recent files (up to 7 days).
    # This catches the 12-24h delay: if Iris wrote at 6AM, Guru sees it at midnight.
    # Not real-time, but reduces latency from "next cycle" to "tonight".
    for agent_name, agent_ws in OTHER_AGENT_WORKSPACES.items():
        if not os.path.isdir(agent_ws):
            continue  # agent workspace not present on this VPS
        memory_dir = os.path.join(agent_ws, "memory")
        if not os.path.isdir(memory_dir):
            continue
        # Check last 7 days
        for days_ago in range(7):
            day = (time.strftime("%Y-%m-%d",
                   time.localtime(time.time() - days_ago * 86400)))
            agent_file = os.path.join(memory_dir, f"{day}.md")
            source_label = f"{agent_name}/memory/{day}.md"
            n = ingest_file(db, agent_file, source_label, agent_name, layer="hive")
            total_ingested += n

    log(f"Total ingested this run: {total_ingested} chunks")

    # ── 4. Apply score decay (nightly maintenance) ─────────────────────────
    try:
        sys.path.insert(0, os.path.join(WORKSPACE, "hive"))
        from score_engine import apply_score_decay
        decayed = apply_score_decay()
        if decayed > 0:
            log(f"Score decay applied to {decayed} memories")
    except Exception as e:
        log(f"Score decay failed (non-fatal): {e}")

    # ── 5. Backup score events ─────────────────────────────────────────────
    score_log = os.path.join(WORKSPACE, "hive/score_events.jsonl")
    if os.path.exists(score_log):
        backup_dir = os.path.join(WORKSPACE, "hive/score_backups")
        os.makedirs(backup_dir, exist_ok=True)
        backup_file = os.path.join(backup_dir, f"score_events_{today}.jsonl")
        if not os.path.exists(backup_file):
            import shutil
            shutil.copy2(score_log, backup_file)
            log(f"Score events backed up to {backup_file}")


if __name__ == "__main__":
    main()
