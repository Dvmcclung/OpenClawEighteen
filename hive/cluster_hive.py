"""
Hive Memory Clustering — Phase 3
Groups semantically similar memories into families using HDBSCAN or AgglomerativeClustering.
Assigns family_id to each record, computes centroids and archetypes.
"""

import lancedb
import numpy as np
import json
import os
import time
import uuid
import sys

LANCEDB_PATH = os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb")
TABLE_NAME = "hybrid_facts"
WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
FAMILY_REGISTRY = os.path.join(WORKSPACE, "hive/family_registry.json")
LOG_FILE = os.path.join(WORKSPACE, "system/agent_activity.log")

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] CLUSTER {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + "\n")

def load_vectors():
    """Load all records and their vectors from LanceDB."""
    db = lancedb.connect(LANCEDB_PATH)
    table = db.open_table(TABLE_NAME)
    df = table.to_pandas()
    log(f"Loaded {len(df)} records")
    return df

def cluster_vectors(vectors, n_records):
    """
    Cluster vectors using HDBSCAN if available, else AgglomerativeClustering.
    Returns array of cluster labels (-1 = noise/unclustered).
    """
    try:
        import hdbscan
        log("Using HDBSCAN clustering")
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=5,
            min_samples=3,
            metric='euclidean',
            cluster_selection_method='eom'
        )
        labels = clusterer.fit_predict(vectors)
        n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
        n_noise = sum(1 for l in labels if l == -1)
        log(f"HDBSCAN found {n_clusters} clusters, {n_noise} noise points")
        return labels
    except ImportError:
        pass

    # Fallback: AgglomerativeClustering
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.preprocessing import normalize

    log("Using AgglomerativeClustering (HDBSCAN not available)")

    # Normalize vectors for cosine similarity
    vecs_norm = normalize(vectors)

    # Estimate good number of clusters: sqrt(n/2) is a rough heuristic
    n_clusters = max(10, int(np.sqrt(n_records / 2)))
    n_clusters = min(n_clusters, 200)  # cap at 200 families

    log(f"Targeting {n_clusters} clusters for {n_records} records")

    clusterer = AgglomerativeClustering(
        n_clusters=n_clusters,
        metric='cosine',
        linkage='average'
    )
    labels = clusterer.fit_predict(vecs_norm)
    log(f"AgglomerativeClustering produced {n_clusters} clusters")
    return labels

def compute_archetype(texts, max_chars=600):
    """Generate a short archetype description for a family by summarizing its texts."""
    sentences = []
    seen = set()
    for text in texts[:10]:  # limit to first 10 members
        first_line = text.split('\n')[0].strip()[:150]
        if first_line and first_line not in seen:
            sentences.append(first_line)
            seen.add(first_line)
    archetype = " | ".join(sentences)
    return archetype[:max_chars]

WARN_RECORD_COUNT = 5000   # warn but continue
BAIL_RECORD_COUNT = 50000  # abort with instructions to switch to incremental


def check_scale_guards(df):
    """
    Warn or abort based on record count and embedding model version.
    Returns True if safe to proceed, False if we should bail.
    """
    n = len(df)

    # ── Record count guards ────────────────────────────────────────────────
    if n >= BAIL_RECORD_COUNT:
        log(
            f"BAIL: {n} records exceeds HDBSCAN limit ({BAIL_RECORD_COUNT}). "
            f"HDBSCAN on {n}x1536 vectors will OOM or take hours on a VPS. "
            f"Switch to incremental clustering. See docs/specs/incremental_clustering_spec.md."
        )
        return False

    if n >= WARN_RECORD_COUNT:
        log(
            f"WARN: {n} records (threshold: {WARN_RECORD_COUNT}). "
            f"HDBSCAN clustering will be slow (est. {n * 1536 * 4 / 1e6:.0f}MB RAM). "
            f"Plan migration to incremental clustering before {BAIL_RECORD_COUNT} records."
        )

    # ── Embedding model version guard ─────────────────────────────────────
    # Check for vector dimension consistency — mixed models cause silent corruption.
    if 'vector' in df.columns:
        dims = set()
        sample = df['vector'].dropna().head(100)
        for v in sample:
            try:
                dims.add(len(v))
            except Exception:
                pass
        if len(dims) > 1:
            log(
                f"BAIL: Mixed vector dimensions detected: {dims}. "
                f"This means multiple embedding models are coexisting in the same table. "
                f"Run hive/inspect_lancedb.py to identify contaminated records. "
                f"Do NOT cluster until resolved — centroids will be meaningless."
            )
            return False
        # 384-dim is now the canonical embedder (all-MiniLM-L6-v2); skip old ada-002 guard
        # if dims and 384 in dims: <removed>

    # Log the embedding model version tag to registry for posterity
    model_tag = "all-MiniLM-L6-v2"  # updated to match hive_schema.EMBED_MODEL
    log(f"Embedding model: {model_tag} | Records: {n} | Dims: {list(dims)[0] if dims else 'unknown'}")

    return True


def main():
    log("START Phase 3 clustering")

    # Load data
    df = load_vectors()

    # Scale guards — warn or abort before HDBSCAN
    if not check_scale_guards(df):
        alert_msg = (
            f"ALERT: cluster_hive.py aborted by scale guard at {len(df)} records. "
            f"Clustering has NOT run. See system/hive_cluster.log for details. "
            f"Action required: review docs/specs/incremental_clustering_spec.md."
        )
        log(alert_msg)
        # Write to agent_activity.log so cron monitors pick it up
        try:
            with open(os.path.join(os.path.dirname(LOG_FILE), "agent_activity.log"), 'a') as f:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] CLUSTER_ALERT {alert_msg}\n")
        except Exception:
            pass
        # Print to stdout so OpenClaw cron delivery surfaces it to Dale
        print(alert_msg)
        return

    # Extract vectors
    vector_col = 'vector'
    if vector_col not in df.columns:
        log("ERROR: no vector column found")
        return

    vectors = np.array(df[vector_col].tolist())
    log(f"Vector matrix: {vectors.shape}")

    # Cluster
    labels = cluster_vectors(vectors, len(df))

    # Build family registry
    unique_labels = set(labels)
    families = {}

    for label in unique_labels:
        if label == -1:
            # Noise points get their own singleton family_id
            continue

        mask = labels == label
        family_id = str(uuid.uuid4())[:8]  # short ID
        family_vectors = vectors[mask]
        family_texts = df[mask]['text'].tolist()
        family_layers = df[mask]['layer'].tolist() if 'layer' in df.columns else []

        centroid = family_vectors.mean(axis=0).tolist()
        archetype = compute_archetype(family_texts)

        # Dominant layer
        from collections import Counter
        layer_counts = Counter(family_layers)
        dominant_layer = layer_counts.most_common(1)[0][0] if layer_counts else 'hive'

        families[family_id] = {
            'family_id': family_id,
            'label': int(label),
            'size': int(mask.sum()),
            'centroid': centroid,
            'archetype': archetype,
            'dominant_layer': dominant_layer,
            'created_at': time.time()
        }

    log(f"Built {len(families)} families")

    # Assign family_ids back to records
    label_to_fid = {v['label']: k for k, v in families.items()}

    family_ids = []
    for label in labels:
        if label == -1:
            family_ids.append(str(uuid.uuid4())[:8])  # singleton
        else:
            family_ids.append(label_to_fid.get(int(label), ""))

    df['family_id'] = family_ids

    # Write back to LanceDB — recreate table with updated family_ids
    log("Writing family_ids back to LanceDB...")
    db = lancedb.connect(LANCEDB_PATH)

    # Drop and recreate (LanceDB doesn't support UPDATE efficiently)
    try:
        db.drop_table(TABLE_NAME)
    except:
        pass

    db.create_table(TABLE_NAME, data=df.to_dict('records'))
    log(f"LanceDB updated with {len(df)} records")

    # Write family registry
    with open(FAMILY_REGISTRY, 'w') as f:
        json.dump({
            'generated_at': time.time(),
            'total_families': len(families),
            'total_records': len(df),
            'noise_records': int(sum(1 for l in labels if l == -1)),
            'families': families
        }, f, indent=2)

    log(f"Family registry written: {len(families)} families")

    # Summary
    sizes = [f['size'] for f in families.values()]
    if sizes:
        log(f"Family sizes: min={min(sizes)}, max={max(sizes)}, avg={sum(sizes)/len(sizes):.1f}")

    log("DONE Phase 3 clustering")
    return families

if __name__ == "__main__":
    main()
