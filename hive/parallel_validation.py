#!/usr/bin/env python3
"""
Parallel Query Validation — Sprint 4
Compare keyword search (SQLite FTS5) vs vector search (LanceDB) quality
across all agent domains.

Run after 2+ weeks of live traffic.

Usage:
  python3 hive/parallel_validation.py              # full run, all queries
  python3 hive/parallel_validation.py --domain thea  # filter by domain
"""

import os
import sys
import json
import time
import sqlite3
import datetime
import argparse
from collections import defaultdict

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
LANCEDB_PATH = os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb")
TABLE_NAME = "hybrid_facts"
TOP_K = 5

# ─── Test query sets ─────────────────────────────────────────────────────────

QUERY_SETS = {
    "thea": [
        "invoice payment audit freight carrier",
        "cron job failure alert monitoring",
        "agent memory architecture proactive surfacing",
    ],
    "iris": [
        "executive communication writing framework clarity",
        "email draft tone professional audience",
        "Anett Grant writing principles structure",
    ],
    "guru": [
        "supply chain carrier rate negotiation truckload",
        "APICS logistics demand forecasting inventory",
        "freight market capacity spot rate trends",
    ],
    "pythagoras": [
        "statistical process control SPC control chart",
        "Monte Carlo simulation uncertainty quantification",
        "Fourier analysis signal decomposition frequency",
    ],
}

# Which owner_agent values map to each domain
DOMAIN_AGENTS = {
    "thea": ["thea"],
    "iris": ["iris"],
    "guru": ["guru"],
    "pythagoras": ["pythagoras"],
}


# ─── Embedding ───────────────────────────────────────────────────────────────

def embed_text(text: str) -> list | None:
    """Embed text using text-embedding-ada-002 (same as hive store)."""
    try:
        import openai
        config_path = os.path.expanduser("~/.openclaw/openclaw.json")
        with open(config_path) as f:
            config = json.load(f)
        api_key = config.get("openai", {}).get("apiKey") or os.environ.get("OPENAI_API_KEY")
        if api_key:
            client = openai.OpenAI(api_key=api_key)
            response = client.embeddings.create(input=text, model="text-embedding-ada-002")
            return response.data[0].embedding
    except Exception as e:
        print(f"  [embed] OpenAI failed: {e}", file=sys.stderr)

    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        return model.encode(text).tolist()
    except Exception as e:
        print(f"  [embed] SentenceTransformer fallback failed: {e}", file=sys.stderr)

    return None


# ─── Load corpus from LanceDB ─────────────────────────────────────────────────

def load_corpus() -> list[dict]:
    """Load all records from LanceDB hybrid_facts table."""
    import lancedb
    db = lancedb.connect(LANCEDB_PATH)
    try:
        tbl = db.open_table(TABLE_NAME)
        df = tbl.to_pandas()
    except Exception as e:
        print(f"ERROR: Could not open LanceDB table: {e}", file=sys.stderr)
        sys.exit(1)

    records = []
    for _, row in df.iterrows():
        records.append({
            "id": str(row.get("id", "")),
            "text": str(row.get("text", "")),
            "layer": str(row.get("layer", "hive")),
            "owner_agent": str(row.get("owner_agent", "unknown")),
            "source": str(row.get("source", "")),
            "created_at": float(row.get("created_at", 0) or 0),
        })
    return records


# ─── SQLite FTS5 keyword search ───────────────────────────────────────────────

def build_fts_index(corpus: list[dict]) -> sqlite3.Connection:
    """Build an in-memory SQLite FTS5 index over the corpus."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE docs (
            rowid INTEGER PRIMARY KEY,
            doc_id TEXT,
            text TEXT,
            layer TEXT,
            owner_agent TEXT,
            source TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            text,
            content='docs',
            content_rowid='rowid',
            tokenize='porter ascii'
        )
    """)
    for i, rec in enumerate(corpus):
        conn.execute(
            "INSERT INTO docs(rowid, doc_id, text, layer, owner_agent, source) VALUES (?,?,?,?,?,?)",
            (i + 1, rec["id"], rec["text"], rec["layer"], rec["owner_agent"], rec["source"]),
        )
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES ('rebuild')")
    conn.commit()
    return conn


def fts_search(conn: sqlite3.Connection, query: str, k: int = TOP_K) -> list[dict]:
    """Run FTS5 BM25 search. Returns top-k results."""
    # FTS5 bm25() returns negative values; lower = better match
    rows = conn.execute(
        """
        SELECT d.doc_id, d.text, d.layer, d.owner_agent,
               bm25(docs_fts) AS score
        FROM docs_fts
        JOIN docs d ON docs_fts.rowid = d.rowid
        WHERE docs_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (query, k),
    ).fetchall()
    return [
        {
            "id": r[0],
            "text": r[1],
            "layer": r[2],
            "owner_agent": r[3],
            "score": abs(r[4]),  # make positive for readability
        }
        for r in rows
    ]


# ─── LanceDB vector search ────────────────────────────────────────────────────

def vector_search(query: str, k: int = TOP_K) -> list[dict]:
    """Run LanceDB vector search. Returns top-k results."""
    import lancedb
    db = lancedb.connect(LANCEDB_PATH)
    tbl = db.open_table(TABLE_NAME)

    vec = embed_text(query)
    if vec is None:
        print(f"  [vector] No embedding for query: {query[:60]}", file=sys.stderr)
        return []

    try:
        results = (
            tbl.search(vec)
            .limit(k)
            .to_list()
        )
    except Exception as e:
        print(f"  [vector] Search error: {e}", file=sys.stderr)
        return []

    out = []
    for r in results:
        # LanceDB returns _distance; convert to similarity score (lower distance = higher score)
        dist = r.get("_distance", 0)
        score = max(0.0, 1.0 - dist)
        out.append({
            "id": str(r.get("id", "")),
            "text": str(r.get("text", "")),
            "layer": str(r.get("layer", "hive")),
            "owner_agent": str(r.get("owner_agent", "unknown")),
            "score": round(score, 4),
        })
    return out


# ─── Overlap calculation ──────────────────────────────────────────────────────

def compute_overlap(fts_results: list[dict], vec_results: list[dict]) -> int:
    """Count how many result IDs appear in both sets."""
    fts_ids = {r["id"] for r in fts_results}
    vec_ids = {r["id"] for r in vec_results}
    return len(fts_ids & vec_ids)


# ─── Domain bias check ────────────────────────────────────────────────────────

def domain_bias_check(domain: str, vec_results: list[dict]) -> dict:
    """
    For a domain's query results, what fraction of top-5 vector results
    come from the expected domain's agents vs others?
    Returns bias info dict.
    """
    expected_agents = DOMAIN_AGENTS.get(domain, [domain])
    in_domain = sum(1 for r in vec_results if r["owner_agent"] in expected_agents)
    total = len(vec_results)
    fraction_in = in_domain / total if total > 0 else 0.0

    # Find dominant agent
    agent_counts = defaultdict(int)
    for r in vec_results:
        agent_counts[r["owner_agent"]] += 1
    dominant = max(agent_counts, key=agent_counts.get) if agent_counts else "none"
    dominant_frac = agent_counts[dominant] / total if total > 0 else 0.0

    bias_flag = (dominant not in expected_agents) and (dominant_frac > 0.50)

    return {
        "domain": domain,
        "expected_agents": expected_agents,
        "in_domain_count": in_domain,
        "total": total,
        "fraction_in_domain": round(fraction_in, 3),
        "dominant_agent": dominant,
        "dominant_fraction": round(dominant_frac, 3),
        "bias_detected": bias_flag,
    }


# ─── Main validation run ──────────────────────────────────────────────────────

def run_validation(domain_filter: str | None = None) -> dict:
    """
    Run full parallel validation across all query sets (or one domain).
    Returns structured results dict.
    """
    print("Loading corpus from LanceDB...")
    corpus = load_corpus()
    print(f"  Corpus: {len(corpus)} records")

    print("Building SQLite FTS5 index...")
    fts_conn = build_fts_index(corpus)
    print("  FTS5 index ready.")
    print()

    results = []
    bias_accumulator = defaultdict(list)  # domain → list of vec_results across queries

    domains = [domain_filter] if domain_filter else list(QUERY_SETS.keys())

    for domain in domains:
        queries = QUERY_SETS[domain]
        print(f"── Domain: {domain.upper()} ──────────────────────────────────────")

        for query in queries:
            print(f"  Query: {query[:70]}")

            fts_res = fts_search(fts_conn, query)
            vec_res = vector_search(query)

            overlap = compute_overlap(fts_res, vec_res)
            top_fts_score = fts_res[0]["score"] if fts_res else 0.0
            top_vec_score = vec_res[0]["score"] if vec_res else 0.0

            print(f"    FTS5: {len(fts_res)} results, top_score={top_fts_score:.3f} | "
                  f"Vector: {len(vec_res)} results, top_score={top_vec_score:.4f} | "
                  f"Overlap: {overlap}")

            results.append({
                "domain": domain,
                "query": query,
                "fts_results": [{"id": r["id"][:16], "score": r["score"],
                                  "owner_agent": r["owner_agent"], "text_preview": r["text"][:100]}
                                 for r in fts_res],
                "vector_results": [{"id": r["id"][:16], "score": r["score"],
                                     "owner_agent": r["owner_agent"], "text_preview": r["text"][:100]}
                                    for r in vec_res],
                "overlap_count": overlap,
                "top_fts_score": round(top_fts_score, 4),
                "top_vector_score": round(top_vec_score, 4),
            })

            bias_accumulator[domain].extend(vec_res)

        print()

    # ─── Domain bias summary ───────────────────────────────────────────────
    print("── Domain Bias Analysis ──────────────────────────────────────────────")
    bias_results = []
    for domain in domains:
        all_vec = bias_accumulator[domain]
        bias = domain_bias_check(domain, all_vec)
        bias_results.append(bias)

        flag = "⚠️  BIAS DETECTED" if bias["bias_detected"] else "✓"
        print(f"  {domain.upper():12s} {flag}")
        print(f"    In-domain: {bias['in_domain_count']}/{bias['total']} ({bias['fraction_in_domain']*100:.0f}%)")
        print(f"    Dominant agent: {bias['dominant_agent']} ({bias['dominant_fraction']*100:.0f}%)")

    print()

    # ─── Summary table ─────────────────────────────────────────────────────
    print("── Per-Query Summary ────────────────────────────────────────────────")
    print(f"{'Domain':<12} {'Query (truncated)':<45} {'FTS':>5} {'Vec':>6} {'Overlap':>7}")
    print("-" * 80)
    for r in results:
        q_short = r["query"][:43]
        print(f"{r['domain']:<12} {q_short:<45} {r['top_fts_score']:>5.3f} {r['top_vector_score']:>6.4f} {r['overlap_count']:>7}")
    print()

    return {
        "run_at": datetime.datetime.now().isoformat(),
        "corpus_size": len(corpus),
        "domain_filter": domain_filter,
        "query_results": results,
        "bias_analysis": bias_results,
    }


def save_results(data: dict):
    """Save results to system/validation_results_YYYY-MM-DD.json."""
    date_str = datetime.date.today().isoformat()
    out_path = os.path.join(WORKSPACE, f"system/validation_results_{date_str}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Results saved to: {out_path}")
    return out_path


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sprint 4 Parallel Query Validation")
    parser.add_argument("--domain", choices=list(QUERY_SETS.keys()),
                        help="Run only queries for this domain")
    parser.add_argument("--no-save", action="store_true",
                        help="Don't save JSON results file")
    args = parser.parse_args()

    print("=" * 80)
    print("  Sprint 4 Parallel Validation: FTS5 vs Vector Search")
    print(f"  Run at: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()

    data = run_validation(domain_filter=args.domain)

    if not args.no_save:
        save_results(data)

    # Overall stats
    total_queries = len(data["query_results"])
    avg_overlap = sum(r["overlap_count"] for r in data["query_results"]) / total_queries if total_queries else 0
    biased = [b for b in data["bias_analysis"] if b["bias_detected"]]

    print()
    print("── Overall ──────────────────────────────────────────────────────────")
    print(f"  Queries run: {total_queries}")
    print(f"  Avg FTS5/Vector overlap: {avg_overlap:.1f} results per query")
    print(f"  Domain bias warnings: {len(biased)}")
    if biased:
        for b in biased:
            print(f"    ⚠️  {b['domain']}: {b['dominant_agent']} dominates ({b['dominant_fraction']*100:.0f}%)")
    print()
