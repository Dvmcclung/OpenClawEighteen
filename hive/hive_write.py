"""
Hive Memory Write Utility
Write memories to the shared hive LanceDB store.

Usage:
    import sys
    sys.path.insert(0, '/home/qtxit/.openclaw-eighteen/workspace/hive')
    from hive_write import write_hive_memory

    write_hive_memory(
        text="Your memory text here",
        layer="hive",
        owner_agent="thea",
        source="session/task-name",
        tag_domain="ops",
        tag_type="fix",
        tag_source="session",
        tag_status="active",
    )

Sprint 5: Added controlled vocabulary tag parameters.
"""

import os
import uuid
import time
import hashlib
import lancedb
from sentence_transformers import SentenceTransformer
from hive_schema import (
    LANCEDB_PATH, TABLE_NAME, EMBED_MODEL, EMBED_DIM,
    LAYERS, AGENTS, DEFAULT_SCORE, DEFAULT_THRESHOLD,
    TAG_DOMAINS, TAG_TYPES, TAG_SOURCES, TAG_STATUSES,
    FIX_THRESHOLD_OVERRIDE, SUPERSEDED_THRESHOLD,
)

_embedder = None
_db = None
_tbl = None


def make_memory_id(text: str, layer: str, owner_agent: str) -> str:
    """Deterministic ID: sha256 of normalized content + layer + owner."""
    normalized = text.strip().lower()
    payload = f"{layer}::{owner_agent}::{normalized}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def make_tag_dict(
    domain: str = None,
    type: str = None,
    source: str = None,
    status: str = "active",
    superseded_by: str = None,
) -> dict:
    """
    Build and validate a tag dict.
    Raises ValueError if any value is not in the controlled vocabulary.
    """
    if domain and domain not in TAG_DOMAINS:
        raise ValueError(f"Invalid tag_domain '{domain}'. Must be one of: {TAG_DOMAINS}")
    if type and type not in TAG_TYPES:
        raise ValueError(f"Invalid tag_type '{type}'. Must be one of: {TAG_TYPES}")
    if source and source not in TAG_SOURCES:
        raise ValueError(f"Invalid tag_source '{source}'. Must be one of: {TAG_SOURCES}")
    if status and status not in TAG_STATUSES:
        raise ValueError(f"Invalid tag_status '{status}'. Must be one of: {TAG_STATUSES}")
    if status == "superseded" and not superseded_by:
        raise ValueError("tag_status='superseded' requires superseded_by field")

    return {
        "tag_domain":    domain or "",
        "tag_type":      type or "",
        "tag_source":    source or "",
        "tag_status":    status or "active",
        "superseded_by": superseded_by or "",
    }


def _get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def _get_table():
    global _db, _tbl
    if _tbl is None:
        _db = lancedb.connect(LANCEDB_PATH)
        _tbl = _db.open_table(TABLE_NAME)
    return _tbl


def _embed(text: str) -> list[float]:
    """Embed a single text string. Truncates to safe length."""
    safe_text = text[:6000]
    model = _get_embedder()
    return model.encode([safe_text])[0].tolist()


def write_hive_memory(
    text: str,
    layer: str = "hive",
    owner_agent: str = "thea",
    score: float = DEFAULT_SCORE,
    source: str = "",
    family_id: str = "",
    threshold: float = DEFAULT_THRESHOLD,
    decay_class: str = "permanent",
    # Sprint 5: Controlled vocabulary tag parameters
    tag_domain: str = None,
    tag_type: str = None,
    tag_source: str = None,  # defaults to inferred from source param if not set
    tag_status: str = "active",
    superseded_by: str = None,
) -> str:
    """
    Write a memory to the shared hive LanceDB store.

    Args:
        text:         The memory content to store.
        layer:        "genome" | "hive" | "private"
        owner_agent:  "thea" | "iris" | "guru" | "pythagoras" | "collective"
        score:        Activation score 0.0-1.0 (default 0.5)
        source:       File path, session ID, or other provenance string
        family_id:    Cluster family ID (assigned in Phase 3, default "")
        threshold:    Minimum score to surface (default 0.3)
        decay_class:  LanceDB decay class (default "permanent")
        tag_domain:   Controlled vocab: ops | comms | supply-chain | math | cross-domain
        tag_type:     Controlled vocab: fix | rubric | fact | insight | decision | procedure
        tag_source:   Controlled vocab: session | kb | paper | external | inferred
        tag_status:   Controlled vocab: active | under-review | superseded | provisional
        superseded_by: memory_id of replacement (required when tag_status="superseded")

    Returns:
        The memory_id (deterministic hash) of the written record.

    Raises:
        ValueError: If layer, owner_agent, or tag values are invalid.
    """
    if layer not in LAYERS:
        raise ValueError(f"Invalid layer '{layer}'. Must be one of: {LAYERS}")
    if owner_agent not in AGENTS:
        raise ValueError(f"Invalid agent '{owner_agent}'. Must be one of: {AGENTS}")

    # Validate and build tag dict
    tags = make_tag_dict(
        domain=tag_domain,
        type=tag_type,
        source=tag_source,
        status=tag_status,
        superseded_by=superseded_by,
    )

    # type:fix → hardcoded lower threshold override
    surfacing_threshold_override = 0.0
    if tag_type == "fix":
        surfacing_threshold_override = FIX_THRESHOLD_OVERRIDE

    # status:superseded → effectively suppressed
    if tag_status == "superseded":
        surfacing_threshold_override = SUPERSEDED_THRESHOLD

    vector = _embed(text)
    now = time.time()
    record_id = str(uuid.uuid4())
    memory_id = make_memory_id(text, layer, owner_agent)

    record = {
        "id":                          record_id,
        "memory_id":                   memory_id,
        "text":                        text,
        "vector":                      vector,
        "decay_class":                 decay_class,
        "created_at":                  now,
        "layer":                       layer,
        "owner_agent":                 owner_agent,
        "score":                       score,
        "family_id":                   family_id,
        "activation_threshold":        threshold,
        "source":                      source,
        "updated_at":                  now,
        # Sprint 5 tag fields
        "tag_domain":                  tags["tag_domain"],
        "tag_type":                    tags["tag_type"],
        "tag_source":                  tags["tag_source"],
        "tag_status":                  tags["tag_status"],
        "superseded_by":               tags["superseded_by"],
        "surfacing_threshold_override": surfacing_threshold_override,
    }

    import pandas as pd
    tbl = _get_table()
    tbl.add(pd.DataFrame([record]))

    return memory_id


def search_hive_memory(
    query: str,
    layer: str = None,
    owner_agent: str = None,
    limit: int = 10,
    min_score: float = None,
) -> list[dict]:
    """
    Search the hive memory store by semantic similarity.

    Args:
        query:       Natural language query.
        layer:       Filter by layer (optional).
        owner_agent: Filter by agent (optional).
        limit:       Max results to return.
        min_score:   Minimum activation_threshold filter (optional).

    Returns:
        List of matching records as dicts.
    """
    vector = _embed(query)
    tbl = _get_table()

    results = tbl.search(vector).limit(limit * 3).to_pandas()

    if layer:
        results = results[results["layer"] == layer]
    if owner_agent:
        results = results[results["owner_agent"] == owner_agent]
    if min_score is not None:
        results = results[results["score"] >= min_score]

    return results.head(limit).to_dict(orient="records")


if __name__ == "__main__":
    # Quick smoke test
    print("Testing write_hive_memory…")
    rid = write_hive_memory(
        text="Hive write utility smoke test. This record can be safely ignored.",
        layer="hive",
        owner_agent="thea",
        source="hive_write/smoke-test",
        decay_class="session",
        tag_domain="ops",
        tag_type="fact",
        tag_source="session",
    )
    print(f"✓ Written record ID: {rid}")

    print("\nTesting search_hive_memory…")
    results = search_hive_memory("hive memory architecture agents", limit=3)
    for r in results:
        print(f"  [{r.get('layer','?')}] {r['text'][:80]}…")
    print("✓ Search complete")
