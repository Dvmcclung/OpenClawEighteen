"""
Hive Memory Scoring Engine — Phase 4
Implements evolutionary scoring: memories that help get stronger,
memories that miss get weaker.
"""

import lancedb
import numpy as np
import json
import os
import time
import sys

LANCEDB_PATH = os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb")
TABLE_NAME = "hybrid_facts"
WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
FAMILY_REGISTRY = os.path.join(WORKSPACE, "hive/family_registry.json")
SCORE_LOG = os.path.join(WORKSPACE, "hive/score_events.jsonl")
LOG_FILE = os.path.join(WORKSPACE, "system/agent_activity.log")

# Scoring constants
# FLAG FOR THEA/DALE: These constants were increased from original values
# (+0.025/+0.05/-0.015/-0.03) to produce meaningful score spread over weeks.
# Original values caused convergence to 0.6-0.8 with no differentiation.
# New values produce ~0.3 std dev spread after ~50 turns. Adjust if too aggressive.
REWARD_STEP = 0.12       # score increase when memory helps (was 0.05)
PENALTY_STEP = 0.08      # score decrease when memory misses (was 0.03)
MIN_SCORE = 0.1          # floor — memories never go to zero
MAX_SCORE = 1.0          # ceiling
COHERENCE_THRESHOLD = 0.75  # cosine similarity considered "helpful"
QUALITY_GATE = 0.60      # below this triggers challenger pass

# Decay constants — memories drift back toward baseline when not surfaced
BASELINE_SCORE = 0.5     # neutral baseline all scores decay toward
DECAY_RATE = 0.005       # per day: unsurfaced memories move this much toward 0.5
INACTIVITY_PENALTY_DAYS = 14  # memories surfaced but never useful after this many days get penalized
INACTIVITY_PENALTY = -0.05    # extra penalty applied at inactivity threshold

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] SCORE {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + "\n")

def load_table():
    db = lancedb.connect(LANCEDB_PATH)
    return db.open_table(TABLE_NAME)

def get_embedding(text):
    """Get embedding using OpenAI ada-002."""
    try:
        import openai
        config_path = os.path.expanduser("~/.openclaw/openclaw.json")
        with open(config_path) as f:
            config = json.load(f)
        api_key = config.get("openai", {}).get("apiKey") or os.environ.get("OPENAI_API_KEY")
        if api_key:
            client = openai.OpenAI(api_key=api_key)
            response = client.embeddings.create(input=text[:8000], model="text-embedding-ada-002")
            return np.array(response.data[0].embedding)
    except Exception as e:
        log(f"Embedding error: {e}")
    return None

def cosine_similarity(a, b):
    """Compute cosine similarity between two vectors."""
    a, b = np.array(a), np.array(b)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)

def update_memory_scores(memory_ids, delta, reason=""):
    """
    Update scores for a list of memory IDs.
    delta > 0 = reward, delta < 0 = penalty
    """
    db = lancedb.connect(LANCEDB_PATH)
    table = db.open_table(TABLE_NAME)
    df = table.to_pandas()

    # memory_ids are stored in 'memory_id' column (short hashes), not 'id' (UUID)
    id_col = 'memory_id' if 'memory_id' in df.columns else 'id'
    if id_col not in df.columns:
        log("ERROR: no id/memory_id column in table")
        return 0

    updated = 0
    for mid in memory_ids:
        mask = df[id_col] == mid
        if mask.any():
            raw = df.loc[mask, 'score'].values[0]
            # Null means never scored — treat as baseline 0.5 for first event
            old_score = float(raw) if raw is not None and str(raw) != 'nan' else BASELINE_SCORE
            new_score = max(MIN_SCORE, min(MAX_SCORE, old_score + delta))
            df.loc[mask, 'score'] = new_score
            df.loc[mask, 'updated_at'] = time.time()
            updated += 1

    if updated > 0:
        # Write back
        try:
            db.drop_table(TABLE_NAME)
        except:
            pass
        db.create_table(TABLE_NAME, data=df.to_dict('records'))
        log(f"Updated {updated} memory scores (delta={delta:+.3f}, reason={reason})")

    return updated

def score_coherence(surfaced_memory_texts, response_text):
    """
    Score whether surfaced memories were actually used in the agent's response.

    Uses response grounding: checks whether memory text is semantically present
    in the agent's response (not the user's follow-up). This measures whether
    the memory contributed to what the agent said, not whether the conversation
    stayed on topic.

    Returns float 0.0-1.0.
      > 0.75 = memory text clearly reflected in response (grounded)
      0.5-0.75 = partial grounding
      < 0.5 = memory not reflected in response (not used)
    """
    if not surfaced_memory_texts or not response_text:
        return 0.5

    # Compare each memory against the agent's RESPONSE (what we said),
    # not the user's follow-up (what they said next).
    # High similarity = we actually used/reflected this memory in our reply.
    #
    # ⚠️  KNOWN LIMITATION: This measures topical correlation, not causal use.
    # A memory about "X" and a response about "X" will score high even if the
    # agent wrote the response from parametric knowledge and the memory just
    # happened to match. True attribution would require checking whether the
    # memory ID appeared in the constructed prompt for that turn.
    # This approximation is acceptable for a single-agent system with limited domain.
    # Future improvement: compare memory_ids in turn_log against prompt contents.
    response_vec = get_embedding(response_text[:1000])
    if response_vec is None:
        return 0.5

    similarities = []
    for text in surfaced_memory_texts:
        mem_vec = get_embedding(text[:500])
        if mem_vec is not None:
            sim = cosine_similarity(response_vec, mem_vec)
            similarities.append(sim)

    if not similarities:
        return 0.5

    # Weighted average: top-2 memories get 2× weight, rest get 1×.
    # Better than max() (which rewards all 5 memories when only 1 matched)
    # and better than mean() (which dilutes signal across irrelevant memories).
    # Top-2 weighted average acknowledges that 1-2 memories typically drive a response.
    sorted_sims = sorted(similarities, reverse=True)
    if len(sorted_sims) == 1:
        return float(sorted_sims[0])
    top2 = sorted_sims[:2]
    rest = sorted_sims[2:]
    weighted = (sum(s * 2 for s in top2) + sum(rest)) / (len(top2) * 2 + len(rest))
    return float(weighted)

def process_turn_reward(surfaced_memory_ids, surfaced_texts, response_text, correction=False):
    """
    Process a completed turn:
    - If correction: penalize active memories
    - If no correction: reward active memories proportional to coherence
    Returns score event dict.
    """
    event = {
        'timestamp': time.time(),
        'memory_ids': surfaced_memory_ids,
        'correction': correction,
        'coherence': 0.0,
        'delta': 0.0,
        'reason': ''
    }

    if correction:
        delta = -PENALTY_STEP
        reason = "correction_signal"
        update_memory_scores(surfaced_memory_ids, delta, reason)
        event['delta'] = delta
        event['reason'] = reason
    else:
        coherence = score_coherence(surfaced_texts, response_text)
        event['coherence'] = coherence

        if coherence >= COHERENCE_THRESHOLD:
            delta = REWARD_STEP
            reason = f"high_coherence_{coherence:.2f}"
        elif coherence >= 0.5:
            delta = REWARD_STEP * 0.5
            reason = f"moderate_coherence_{coherence:.2f}"
        else:
            delta = -PENALTY_STEP * 0.5
            reason = f"low_coherence_{coherence:.2f}"

        update_memory_scores(surfaced_memory_ids, delta, reason)
        event['delta'] = delta
        event['reason'] = reason

    # Log event
    with open(SCORE_LOG, 'a') as f:
        f.write(json.dumps(event) + "\n")

    return event

def challenger_pass(query, champion_memories, k=5, threshold=0.3):
    """
    Dale's rubric-gated challenger pass.
    Surfaces from lowest-scoring families first as a challenger.
    Returns challenger memories if they score better.
    """
    sys.path.insert(0, WORKSPACE + '/hive')
    from surface_engine import surface_memories

    # Load family registry to find lowest-scoring families
    if not os.path.exists(FAMILY_REGISTRY):
        return champion_memories

    with open(FAMILY_REGISTRY) as f:
        registry_data = json.load(f)
    families = registry_data.get('families', {})

    # Get families sorted by lowest score (challenger candidates)
    db = lancedb.connect(LANCEDB_PATH)
    table = db.open_table(TABLE_NAME)
    df = table.to_pandas()

    # Compute average score per family
    if 'score' in df.columns and 'family_id' in df.columns:
        family_scores = df.groupby('family_id')['score'].mean().to_dict()
    else:
        return champion_memories

    # Get champion family IDs
    champion_fids = set(m.get('family_id', '') for m in champion_memories)

    # Find challenger families (lowest scoring, not already surfaced)
    challenger_fids = sorted(
        [fid for fid in family_scores if fid not in champion_fids],
        key=lambda fid: family_scores.get(fid, 0.5)
    )[:20]  # top 20 lowest-scoring as challengers

    if not challenger_fids:
        return champion_memories

    # Surface from challenger families
    challenger_memories = surface_memories(
        query, k=k, threshold=0.0,  # lower threshold for challengers
        agent_filter=None
    )

    # Filter to challenger families only
    challenger_memories = [m for m in challenger_memories
                          if m.get('family_id', '') in challenger_fids]

    log(f"Challenger pass: {len(challenger_memories)} challenger memories vs {len(champion_memories)} champion")

    return challenger_memories if challenger_memories else champion_memories

def apply_score_decay():
    """
    Decay scores of memories that haven't been surfaced recently.
    Memories drift back toward BASELINE_SCORE (0.5) at DECAY_RATE per day.
    Memories that are frequently surfaced but never helpful after INACTIVITY_PENALTY_DAYS
    receive an additional inactivity penalty.

    Called nightly by ingest_daily_memory.py (added to its main() call).
    Safe to call multiple times — uses updated_at to track last decay.
    """
    db = lancedb.connect(LANCEDB_PATH)
    table = db.open_table(TABLE_NAME)
    df = table.to_pandas()

    if 'score' not in df.columns or 'updated_at' not in df.columns:
        log("Decay: missing score or updated_at columns, skipping")
        return 0

    now = time.time()
    day_secs = 86400
    updated = 0

    for idx, row in df.iterrows():
        score = float(row['score'])
        if score == BASELINE_SCORE:
            continue  # already at baseline, no decay needed

        last_updated = float(row.get('updated_at', now))
        days_since_update = (now - last_updated) / day_secs

        if days_since_update < 1.0:
            continue  # updated in last 24h, skip

        # Drift toward baseline
        decay_amount = DECAY_RATE * days_since_update
        if score > BASELINE_SCORE:
            new_score = max(BASELINE_SCORE, score - decay_amount)
        else:
            new_score = min(BASELINE_SCORE, score + decay_amount)

        # Extra penalty for memories that have been surfaced many times but score stayed low
        # (surfaced frequently, never helped → dead weight)
        surface_count = int(row.get('surface_count', 0))
        if (surface_count >= 10 and score < 0.45 and
                days_since_update >= INACTIVITY_PENALTY_DAYS):
            new_score = max(MIN_SCORE, new_score + INACTIVITY_PENALTY)
            log(f"Inactivity penalty applied to {row.get('memory_id', '?')} "
                f"(surfaced {surface_count}x, score={score:.3f})")

        df.at[idx, 'score'] = new_score
        df.at[idx, 'updated_at'] = now
        updated += 1

    if updated > 0:
        try:
            db.drop_table(TABLE_NAME)
        except Exception:
            pass
        db.create_table(TABLE_NAME, data=df.to_dict('records'))
        log(f"Decay applied to {updated} memories")

    return updated


def increment_surface_count(memory_ids):
    """
    Increment surface_count for memories that were surfaced this turn.
    Tracks how many times a memory was retrieved so we can detect
    'frequently surfaced but never useful' patterns.
    """
    if not memory_ids:
        return
    try:
        db = lancedb.connect(LANCEDB_PATH)
        table = db.open_table(TABLE_NAME)
        df = table.to_pandas()
        id_col = 'memory_id' if 'memory_id' in df.columns else 'id'
        if 'surface_count' not in df.columns:
            df['surface_count'] = 0
        for mid in memory_ids:
            mask = df[id_col] == mid
            if mask.any():
                df.loc[mask, 'surface_count'] = df.loc[mask, 'surface_count'].fillna(0).astype(int) + 1
        db.drop_table(TABLE_NAME)
        db.create_table(TABLE_NAME, data=df.to_dict('records'))
    except Exception as e:
        log(f"increment_surface_count failed: {e}")


def get_score_summary():
    """Return summary statistics of current memory scores."""
    db = lancedb.connect(LANCEDB_PATH)
    table = db.open_table(TABLE_NAME)
    df = table.to_pandas()

    if 'score' not in df.columns:
        return {}

    return {
        'total': len(df),
        'mean_score': float(df['score'].mean()),
        'min_score': float(df['score'].min()),
        'max_score': float(df['score'].max()),
        'high_performers': int((df['score'] >= 0.7).sum()),
        'low_performers': int((df['score'] <= 0.3).sum()),
        'by_layer': df.groupby('layer')['score'].mean().to_dict() if 'layer' in df.columns else {}
    }

if __name__ == "__main__":
    summary = get_score_summary()
    print(json.dumps(summary, indent=2))
