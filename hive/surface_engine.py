"""
Hive Memory Surface Engine
Searches the hive store for memories relevant to a query and returns top-k results.

Sprint 5: Added tag-based filtering and tag fields in results.
          type:fix records use surfacing_threshold_override (0.45) if present.

## Schema field reference (LanceDB hybrid_facts)

activation_threshold (float, default 0.3):
    Per-record minimum cosine similarity required to surface this memory.
    Used as a per-record floor — memories with low threshold appear more easily.
    Currently all records default to 0.3; can be raised for noisy/low-quality memories
    or lowered for high-priority ones. Applied in hive_write.py read path (min_score filter).

surfacing_threshold_override (float, default 0.0):
    If > 0, overrides the global surfacing threshold for this specific record.
    Used to require higher confidence before surfacing sensitive or easily-misapplied memories.
    Example: type:fix records use 0.45 to avoid surfacing bug-fix notes in unrelated contexts.
    0.0 means "use global threshold" (no override active).
"""

import lancedb
import json
import sys
import os
import time
import re
from sentence_transformers import SentenceTransformer

LANCEDB_PATH = os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb")
TABLE_NAME = "hybrid_facts"
DEFAULT_K = 5
DEFAULT_THRESHOLD = 0.3

# Log the active embedding model once at module load (helps detect unexpected model changes)
_EMBED_MODEL = "all-MiniLM-L6-v2"
_embedder = None
try:
    _log_path = os.path.join(os.path.expanduser("~/.openclaw-eighteen/workspace"), "system/agent_activity.log")
    with open(_log_path, 'a') as _f:
        _f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] EMBED using {_EMBED_MODEL}\n")
except Exception:
    pass

def _get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(_EMBED_MODEL)
    return _embedder


def embed_text(text):
    """Embed text using the same model as the hive store."""
    try:
        model = _get_embedder()
        return model.encode([text[:6000]])[0].tolist()
    except Exception as e:
        print(f"Embedding failed: {e}", file=sys.stderr)
        return None



SQLITE_DB_PATH = os.path.expanduser("/home/qtxit/.openclaw/shared/memory/facts.db")

def _escape_fts5(query: str) -> str:
    """Return a safe FTS5 query string built from plain keywords."""
    tokens = re.findall(r"[a-z0-9]{3,}", query.lower())
    return " OR ".join(tokens[:10]) if tokens else "x"



def search_sqlite_fts(query: str, k: int = 5) -> list:
    """
    Search SQLite FTS5 store for keyword-relevant facts.
    Returns list of dicts with text, similarity (BM25-derived), layer, owner_agent, source.

    These facts are in addition to LanceDB semantic results — they represent
    structured, keyword-searchable knowledge that may not be in LanceDB.
    """
    if not os.path.exists(SQLITE_DB_PATH):
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(SQLITE_DB_PATH)
        # FTS5 BM25 ranking — negative rank means better match
        cursor = conn.execute(
            """
            SELECT f.text, f.source, f.entity, f.key, bm25(facts_fts) as rank
            FROM facts_fts
            JOIN facts f ON facts_fts.rowid = f.rowid
            WHERE facts_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (_escape_fts5(query), k * 2)
        )
        rows = cursor.fetchall()
        conn.close()

        results = []
        for text, source, entity, key, rank in rows:
            if not text or len(text.strip()) < 20:
                continue
            # Normalize BM25 rank to a 0-1 similarity score
            # BM25 scores are negative (more negative = better); map to 0.3–0.7 range
            sim = max(0.3, min(0.7, 0.7 + rank * 0.02))
            results.append({
                'memory_id': '',
                'text': text,
                'similarity': sim,
                'layer': 'hive',
                'owner_agent': 'thea',
                'source': source or 'sqlite',
                'score': 0.5,
                'family_id': '',
                'tag_domain': '',
                'tag_type': 'fact',
                'tag_source': 'session',
                'tag_status': 'active',
                '_from_sqlite': True,
            })
        return results[:k]
    except Exception as e:
        print(f"SQLite FTS search failed: {e}", file=sys.stderr)
        return []


def search_hive(query: str, k: int = DEFAULT_K, threshold: float = DEFAULT_THRESHOLD,
                layer_filter=None, agent_filter=None,
                tag_domain: str = None, tag_type: str = None, tag_status: str = None):
    """
    Search hive for memories relevant to query. Supports tag-based filtering.

    Args:
        query:       Natural language query.
        k:           Max results to return.
        threshold:   Minimum similarity threshold (default 0.3).
        layer_filter: List of layers to include (optional).
        agent_filter: List of agents to include (optional).
        tag_domain:  Filter by tag_domain (optional).
        tag_type:    Filter by tag_type (optional).
        tag_status:  Filter by tag_status (optional, default excludes superseded).

    Returns list of dicts with text, score, layer, owner_agent, source, + tag fields.
    """
    db = lancedb.connect(LANCEDB_PATH)

    try:
        available = db.list_tables() if hasattr(db, 'list_tables') else db.table_names()
        table_names = getattr(available, 'tables', None) or list(available)
    except Exception:
        table_names = []

    if TABLE_NAME not in table_names:
        return []

    try:
        table = db.open_table(TABLE_NAME)
    except Exception as e:
        print(f"Could not open hive table: {e}", file=sys.stderr)
        return []

    vector = embed_text(query)
    if vector is None:
        return []

    # Over-fetch to allow filtering
    results = (
        table.search(vector)
        .limit(k * 6)
        .to_pandas()
    )

    # Compute similarity
    if '_distance' in results.columns:
        results['similarity'] = 1 - results['_distance'] / 2

    # Apply per-record threshold override (type:fix uses 0.45 instead of default)
    # Records without override use the global threshold
    if 'surfacing_threshold_override' in results.columns and 'similarity' in results.columns:
        def passes_threshold(row):
            override = float(row.get('surfacing_threshold_override', 0) or 0)
            effective_threshold = override if override > 0 else threshold
            return float(row.get('similarity', 0)) >= effective_threshold
        results = results[results.apply(passes_threshold, axis=1)]
    elif 'similarity' in results.columns:
        results = results[results['similarity'] >= threshold]

    # Filter by layer if specified
    if layer_filter and 'layer' in results.columns:
        results = results[results['layer'].isin(layer_filter)]

    # Filter by agent if specified
    if agent_filter and 'owner_agent' in results.columns:
        results = results[results['owner_agent'].isin(agent_filter)]

    # Sprint 5: Tag-based filtering
    if tag_domain and 'tag_domain' in results.columns:
        results = results[results['tag_domain'] == tag_domain]

    if tag_type and 'tag_type' in results.columns:
        results = results[results['tag_type'] == tag_type]

    if tag_status and 'tag_status' in results.columns:
        results = results[results['tag_status'] == tag_status]
    elif 'tag_status' in results.columns:
        # By default, suppress superseded records (threshold 0.95 is handled above,
        # but also filter them out entirely unless explicitly requested)
        results = results[results['tag_status'] != 'superseded']

    # Sort: genome first when tied, then by similarity
    if 'layer' in results.columns:
        layer_order = {'genome': 0, 'hive': 1, 'private': 2}
        results['layer_priority'] = results['layer'].map(layer_order).fillna(1)
        results = results.sort_values(['similarity', 'layer_priority'], ascending=[False, True])
    elif 'similarity' in results.columns:
        results = results.sort_values('similarity', ascending=False)

    results = results.head(k)

    # Format output — include tag fields
    output = []
    for _, row in results.iterrows():
        output.append({
            'memory_id':   str(row.get('memory_id', '')) if 'memory_id' in row and row.get('memory_id') is not None else '',
            'text':        row.get('text', ''),
            'similarity':  float(row.get('similarity', 0)),
            'layer':       str(row.get('layer', 'hive')),
            'owner_agent': str(row.get('owner_agent', 'thea')),
            'source':      str(row.get('source', '')),
            'score':       float(row.get('score', 0.5)) if 'score' in row and row.get('score') is not None else 0.5,
            'family_id':   str(row.get('family_id', '')) if 'family_id' in row else '',
            # Sprint 5: tag fields
            'tag_domain':  str(row.get('tag_domain', '')) if 'tag_domain' in row else '',
            'tag_type':    str(row.get('tag_type', '')) if 'tag_type' in row else '',
            'tag_source':  str(row.get('tag_source', '')) if 'tag_source' in row else '',
            'tag_status':  str(row.get('tag_status', 'active')) if 'tag_status' in row else 'active',
        })

    # ── Hybrid: merge SQLite FTS results ─────────────────────────────────────
    # SQLite holds 3,400+ structured facts not in LanceDB. Run a keyword search
    # and blend the top results, deduplicating against LanceDB results by text overlap.
    sqlite_results = search_sqlite_fts(query, k=3)
    if sqlite_results:
        lancedb_texts = {r['text'][:100].lower() for r in output}
        for sr in sqlite_results:
            # Skip if text is substantially similar to an existing result
            sr_preview = sr['text'][:100].lower()
            is_dupe = any(
                len(set(sr_preview.split()) & set(lt.split())) / max(len(sr_preview.split()), 1) > 0.6
                for lt in lancedb_texts
            )
            if not is_dupe and len(output) < k:
                output.append(sr)
                lancedb_texts.add(sr_preview)

        # Re-sort by similarity after merge
        output.sort(key=lambda x: x['similarity'], reverse=True)
        output = output[:k]

    return output


# Keep old function name as alias for backwards compatibility
def surface_memories(query, k=DEFAULT_K, threshold=DEFAULT_THRESHOLD,
                      layer_filter=None, agent_filter=None,
                      tag_domain=None, tag_type=None, tag_status=None):
    """Alias for search_hive (backwards compatible)."""
    return search_hive(query, k=k, threshold=threshold,
                       layer_filter=layer_filter, agent_filter=agent_filter,
                       tag_domain=tag_domain, tag_type=tag_type, tag_status=tag_status)


def load_family_registry():
    """Load the family registry."""
    registry_path = os.path.join(os.path.dirname(__file__), 'family_registry.json')
    if not os.path.exists(registry_path):
        return {}
    with open(registry_path) as f:
        data = json.load(f)
    return data.get('families', {})


def surface_with_family(query, k=DEFAULT_K, threshold=DEFAULT_THRESHOLD,
                         layer_filter=None, agent_filter=None,
                         tag_domain=None, tag_type=None, tag_status=None):
    """
    Surface memories with family context. Sprint 5: tag filter passthrough.
    """
    memories = surface_memories(query, k=k, threshold=threshold,
                                 layer_filter=layer_filter, agent_filter=agent_filter,
                                 tag_domain=tag_domain, tag_type=tag_type, tag_status=tag_status)

    if not memories:
        return memories, []

    registry = load_family_registry()
    fid_to_family = {v['family_id']: v for v in registry.values()} if registry else {}

    seen_families = set()
    family_archetypes = []

    for mem in memories:
        fid = mem.get('family_id', '')
        if fid and fid in fid_to_family and fid not in seen_families:
            family = fid_to_family[fid]
            if family.get('size', 0) >= 3:
                seen_families.add(fid)
                family_archetypes.append({
                    'family_id': fid,
                    'archetype': family['archetype'],
                    'size': family['size'],
                    'dominant_layer': family['dominant_layer']
                })

    return memories, family_archetypes


def _format_tag_line(mem: dict) -> str:
    """Format tag fields as inline label for active_context.md."""
    parts = []
    if mem.get('tag_domain'):
        parts.append(f"domain:{mem['tag_domain']}")
    if mem.get('tag_type'):
        parts.append(f"type:{mem['tag_type']}")
    if mem.get('tag_source'):
        parts.append(f"source:{mem['tag_source']}")
    if not parts:
        return ""
    return " | ".join(parts)


def format_context_block(memories, query="", family_archetypes=None):
    """Format surfaced memories as a context block for injection.
    Sprint 5: includes tag labels in headers.
    """
    if not memories:
        return ""

    lines = ["## Relevant Context (Hive Memory)", ""]
    if query:
        lines.append(f"*Surfaced for: \"{query[:100]}\"*")
        lines.append("")

    if family_archetypes:
        lines.append("### Family Context")
        lines.append("")
        for fa in family_archetypes:
            lines.append(f"**Family [{fa['family_id']}]** ({fa['size']} members, {fa['dominant_layer']})")
            lines.append(fa['archetype'][:400])
            lines.append("")
        lines.append("---")
        lines.append("")

    for i, mem in enumerate(memories, 1):
        layer_tag = f"[{mem['layer']}]" if mem['layer'] != 'hive' else ""
        agent_tag = f"({mem['owner_agent']})" if mem['owner_agent'] not in ('thea', 'collective') else ""
        tag_line = _format_tag_line(mem)
        tag_inline = f" [{tag_line}]" if tag_line else ""
        header = f"{i}. {layer_tag}{agent_tag}".strip()
        lines.append(f"**{header}** *(relevance: {mem['similarity']:.2f})*{tag_inline}")
        text = mem['text'][:400] + "..." if len(mem['text']) > 400 else mem['text']
        lines.append(text)
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    # CLI usage: python3 surface_engine.py "query text" [k]
    if len(sys.argv) < 2:
        print("Usage: surface_engine.py <query> [k]")
        sys.exit(1)

    query = sys.argv[1]
    k = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_K

    memories = surface_memories(query, k=k)
    block = format_context_block(memories, query)
    print(block if block else "No relevant memories found above threshold.")
