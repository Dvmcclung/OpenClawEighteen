"""
Memory lifecycle management.

Staleness score = f(days_since_last_surface, days_since_written, surface_count)
  - Never surfaced after 30 days: staleness = HIGH
  - Last surfaced >60 days ago: staleness = MEDIUM
  - Surface count = 0 after 14 days: candidate for review

Commands:
  python3 lifecycle.py report          — print staleness report
  python3 lifecycle.py candidates      — list HIGH staleness memories
  python3 lifecycle.py mark-reviewed <id>  — reset staleness clock

Data source:
  - Primary: LanceDB hybrid_facts table (created_at, score fields)
  - Surface history: system/hive_surface.log + system/turn_log.jsonl
"""

import os
import sys
import json
import time
import datetime
from collections import defaultdict

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
SURFACE_LOG = os.path.join(WORKSPACE, "system/hive_surface.log")
TURN_LOG = os.path.join(WORKSPACE, "system/turn_log.jsonl")
LIFECYCLE_REVIEWED = os.path.join(WORKSPACE, "system/lifecycle_reviewed.json")
LANCEDB_PATH = os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb")
TABLE_NAME = "hybrid_facts"

# Staleness thresholds (days)
HIGH_NEVER_SURFACED_DAYS = 30
MEDIUM_LAST_SURFACED_DAYS = 60
CANDIDATE_REVIEW_DAYS = 14

# Sprint 5: Source-based staleness windows (days)
STALENESS_WINDOWS = {
    "external": 60,
    "session":  90,
    "kb":       180,
    "paper":    180,
    "inferred": 30,
    None:       90,  # default
}

STALENESS_HIGH = "HIGH"
STALENESS_MEDIUM = "MEDIUM"
STALENESS_LOW = "LOW"
STALENESS_COLD = "COLD"  # Written since last re-seed, zero activations, not yet prunable
STALENESS_OK = "OK"

# COLD threshold: records are COLD if < 30 days old with zero activations
# They are excluded from pruning candidates until 30+ days old OR have ≥1 activation
COLD_PRUNE_AGE_DAYS = 30


def load_reviewed_registry() -> dict:
    """Load the lifecycle reviewed registry (maps memory_id → last_reviewed_ts)."""
    if not os.path.exists(LIFECYCLE_REVIEWED):
        return {}
    with open(LIFECYCLE_REVIEWED) as f:
        return json.load(f)


def save_reviewed_registry(reg: dict):
    """Save the lifecycle reviewed registry."""
    os.makedirs(os.path.dirname(LIFECYCLE_REVIEWED), exist_ok=True)
    with open(LIFECYCLE_REVIEWED, 'w') as f:
        json.dump(reg, f, indent=2)


def _load_surface_counts() -> dict:
    """
    Build a map of memory_text_hash → surface_count from hive_surface.log.
    Also returns last_surfaced_ts per hash.
    """
    surface_counts = defaultdict(int)
    last_surfaced = {}

    # Parse hive_surface.log for top-level surface events
    if os.path.exists(SURFACE_LOG):
        with open(SURFACE_LOG) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    ts = rec.get("timestamp", "")
                    # hive_surface.log records queries but not individual memory IDs
                    # We use turn_log for per-memory tracking
                except (json.JSONDecodeError, AttributeError):
                    pass

    # Parse turn_log for per-memory surface events
    if os.path.exists(TURN_LOG):
        with open(TURN_LOG) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    ts = rec.get("timestamp", "")
                    for mem_id in rec.get("surfaced_memory_ids", []):
                        surface_counts[mem_id] += 1
                        if ts > last_surfaced.get(mem_id, ""):
                            last_surfaced[mem_id] = ts
                except (json.JSONDecodeError, AttributeError):
                    pass

    return dict(surface_counts), last_surfaced


def _days_since(ts_float: float) -> float:
    """Days since a Unix timestamp."""
    return (time.time() - ts_float) / 86400.0


def _days_since_iso(iso_str: str) -> float:
    """Days since an ISO timestamp string."""
    try:
        ts = datetime.datetime.fromisoformat(iso_str)
        return (datetime.datetime.now() - ts).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 9999.0


def classify_staleness(
    days_since_written: float,
    days_since_last_surface: float,
    surface_count: int,
    reviewed_recently: bool = False,
) -> str:
    """
    Classify staleness level for a memory.

    Categories:
      COLD   — zero activations AND < 30 days old. Hasn't had a fair match yet.
               NOT the same as stale. Excluded from pruning.
      LOW    — zero activations but recently written (< CANDIDATE_REVIEW_DAYS)
               [Legacy: now mostly subsumed by COLD since COLD_PRUNE_AGE_DAYS > CANDIDATE_REVIEW_DAYS]
      MEDIUM — zero activations ≥ CANDIDATE_REVIEW_DAYS, OR last surfaced ≥ 60d ago
      HIGH   — zero activations ≥ HIGH_NEVER_SURFACED_DAYS (pruning candidate)
      OK     — has activations and recently surfaced, or reviewed recently
    """
    if reviewed_recently:
        return STALENESS_OK

    if surface_count == 0:
        if days_since_written >= HIGH_NEVER_SURFACED_DAYS:
            return STALENESS_HIGH
        elif days_since_written < COLD_PRUNE_AGE_DAYS:
            # Written since re-seed, never activated — COLD (not yet prunable)
            return STALENESS_COLD
        elif days_since_written >= CANDIDATE_REVIEW_DAYS:
            return STALENESS_MEDIUM
        else:
            return STALENESS_LOW

    if days_since_last_surface >= MEDIUM_LAST_SURFACED_DAYS:
        return STALENESS_MEDIUM

    return STALENESS_OK


def is_pruning_candidate(memory: dict) -> bool:
    """
    Return True if a memory is eligible for pruning.
    COLD records (zero activations, < 30 days) are excluded from pruning.
    """
    if memory["staleness"] == STALENESS_COLD:
        return False
    if memory["staleness"] == STALENESS_HIGH:
        return True
    return False


def get_memories_with_staleness() -> list[dict]:
    """
    Load all memories from LanceDB and compute staleness for each.
    Returns list of dicts with staleness info.
    """
    import lancedb
    import pandas as pd

    surface_counts, last_surfaced = _load_surface_counts()
    reviewed_registry = load_reviewed_registry()

    results = []
    now_ts = time.time()

    try:
        db = lancedb.connect(LANCEDB_PATH)
        tbl = db.open_table(TABLE_NAME)
        df = tbl.to_pandas()
    except Exception as e:
        print(f"Could not open LanceDB table: {e}", file=sys.stderr)
        return []

    for _, row in df.iterrows():
        text = str(row.get("text", ""))
        created_at = float(row.get("created_at", 0) or 0)
        mem_id = str(row.get("id", ""))

        # Use sha256 hash of text as the surface-log lookup key (matches attribution.py)
        import hashlib
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:12]

        surf_count = surface_counts.get(text_hash, 0)
        last_surf_iso = last_surfaced.get(text_hash, "")

        days_written = _days_since(created_at) if created_at > 0 else 9999.0
        days_last_surf = _days_since_iso(last_surf_iso) if last_surf_iso else days_written

        # Check if reviewed recently (within 30 days)
        last_reviewed = reviewed_registry.get(mem_id, reviewed_registry.get(text_hash, ""))
        reviewed_recently = bool(last_reviewed and _days_since_iso(last_reviewed) < 30)

        staleness = classify_staleness(
            days_since_written=days_written,
            days_since_last_surface=days_last_surf,
            surface_count=surf_count,
            reviewed_recently=reviewed_recently,
        )

        results.append({
            "id": mem_id,
            "text_hash": text_hash,
            "text_preview": text[:120].replace("\n", " "),
            "layer": str(row.get("layer", "hive")),
            "owner_agent": str(row.get("owner_agent", "?")),
            "created_at": created_at,
            "days_since_written": round(days_written, 1),
            "surface_count": surf_count,
            "last_surfaced": last_surf_iso or "never",
            "days_since_last_surface": round(days_last_surf, 1),
            "staleness": staleness,
            "reviewed_recently": reviewed_recently,
        })

    return results


def report() -> str:
    """Print a staleness report, broken down by layer and staleness category."""
    memories = get_memories_with_staleness()
    if not memories:
        return "No memories found or LanceDB unavailable."

    # Global counts
    counts = defaultdict(int)
    for m in memories:
        counts[m["staleness"]] += 1

    # Per-layer counts
    layer_counts = defaultdict(lambda: defaultdict(int))
    for m in memories:
        layer_counts[m["layer"]][m["staleness"]] += 1

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# Memory Lifecycle Staleness Report",
        f"Generated: {now}",
        f"Total memories: {len(memories)}",
        "",
        "## Global Counts",
        f"  COLD:   {counts[STALENESS_COLD]} memories (written since re-seed, zero activations, <{COLD_PRUNE_AGE_DAYS}d — NOT prunable)",
        f"  HIGH:   {counts[STALENESS_HIGH]} memories (never surfaced ≥{HIGH_NEVER_SURFACED_DAYS}d — pruning candidates)",
        f"  MEDIUM: {counts[STALENESS_MEDIUM]} memories (last surface ≥{MEDIUM_LAST_SURFACED_DAYS}d OR new ≥{CANDIDATE_REVIEW_DAYS}d unsurfaced)",
        f"  LOW:    {counts[STALENESS_LOW]} memories (new, <{CANDIDATE_REVIEW_DAYS}d, not yet surfaced)",
        f"  OK:     {counts[STALENESS_OK]} memories",
        "",
        "## Per-Layer Breakdown (COLD / HIGH / MEDIUM / LOW / OK)",
        "",
    ]

    for layer in sorted(layer_counts.keys()):
        lc = layer_counts[layer]
        total_layer = sum(lc.values())
        lines.append(
            f"  {layer:<20} "
            f"COLD={lc[STALENESS_COLD]:>4}  "
            f"HIGH={lc[STALENESS_HIGH]:>4}  "
            f"MED={lc[STALENESS_MEDIUM]:>4}  "
            f"LOW={lc[STALENESS_LOW]:>4}  "
            f"OK={lc[STALENESS_OK]:>4}  "
            f"(total={total_layer})"
        )

    lines.append("")
    lines.append("## HIGH Staleness (sample — top 10, pruning candidates)")
    lines.append("")

    high = [m for m in memories if m["staleness"] == STALENESS_HIGH]
    high.sort(key=lambda m: m["days_since_written"], reverse=True)
    for m in high[:10]:
        lines.append(f"- [{m['layer']}] ({m['owner_agent']}) age={m['days_since_written']}d surf={m['surface_count']}")
        lines.append(f"  {m['text_preview'][:100]}")
        lines.append(f"  id={m['id'][:16]}...")
        lines.append("")

    lines.append("## MEDIUM Staleness (sample — top 10)")
    lines.append("")
    medium = [m for m in memories if m["staleness"] == STALENESS_MEDIUM]
    medium.sort(key=lambda m: m["days_since_last_surface"], reverse=True)
    for m in medium[:10]:
        lines.append(f"- [{m['layer']}] ({m['owner_agent']}) age={m['days_since_written']}d last_surf={m['last_surfaced'][:10]}")
        lines.append(f"  {m['text_preview'][:100]}")
        lines.append("")

    # Sprint 5: Tag distribution
    lines.append("")
    lines.append("## Sprint 5: Tag Distribution")
    lines.append("")

    try:
        import lancedb as _lancedb
        _db = _lancedb.connect(LANCEDB_PATH)
        _tbl = _db.open_table(TABLE_NAME)
        _df = _tbl.to_pandas()

        if 'tag_domain' in _df.columns:
            domain_counts = _df['tag_domain'].value_counts().to_dict()
            lines.append("### By domain:")
            for k, v in sorted(domain_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {k or '(untagged)'}: {v}")
            lines.append("")

        if 'tag_type' in _df.columns:
            type_counts = _df['tag_type'].value_counts().to_dict()
            lines.append("### By type:")
            for k, v in sorted(type_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {k or '(untagged)'}: {v}")
            lines.append("")

        if 'tag_status' in _df.columns:
            status_counts = _df['tag_status'].value_counts().to_dict()
            lines.append("### By status:")
            for k, v in sorted(status_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {k or '(untagged)'}: {v}")
            lines.append("")

            # Flag under-review records explicitly
            under_review = _df[_df['tag_status'] == 'under-review'] if 'tag_status' in _df.columns else _df.iloc[0:0]
            if len(under_review) > 0:
                lines.append(f"### ⚠️  UNDER-REVIEW ({len(under_review)} records — require Monday curation)")
                lines.append("")
                for _, row in under_review.head(20).iterrows():
                    text_preview = str(row.get('text', ''))[:120].replace('\n', ' ')
                    mem_id = str(row.get('memory_id', row.get('id', '?')))[:16]
                    lines.append(f"- [{row.get('layer','?')}] ({row.get('owner_agent','?')}) id={mem_id}")
                    lines.append(f"  {text_preview}")
                    lines.append("")
            else:
                lines.append("*No under-review records. ✓*")
                lines.append("")

    except Exception as e:
        lines.append(f"  (Tag distribution unavailable: {e})")
        lines.append("")

    lines.append("---")
    lines.append("COLD records are excluded from pruning (zero activations but < 30 days old).")
    lines.append("Run `python3 hive/lifecycle.py candidates` for full HIGH list.")
    lines.append("Run `python3 hive/lifecycle.py mark-reviewed <id>` to reset staleness clock.")

    return "\n".join(lines)


def candidates() -> str:
    """List HIGH staleness memories (pruning candidates) and COLD count separately."""
    memories = get_memories_with_staleness()
    high = [m for m in memories if m["staleness"] == STALENESS_HIGH]
    cold = [m for m in memories if m["staleness"] == STALENESS_COLD]
    high.sort(key=lambda m: m["days_since_written"], reverse=True)

    lines = [
        f"# Lifecycle Candidates Report ({len(memories)} total memories)",
        "",
        f"── COLD ({len(cold)} records) — NOT prunable ──────────────────────────────",
        f"   Zero activations, < {COLD_PRUNE_AGE_DAYS} days old. Hasn't had a fair match yet.",
        f"   Excluded from pruning until 30+ days old OR ≥1 activation.",
        "",
    ]

    if cold:
        # Show layer distribution for cold
        cold_by_layer = defaultdict(int)
        for m in cold:
            cold_by_layer[m["layer"]] += 1
        for layer, cnt in sorted(cold_by_layer.items()):
            lines.append(f"   {layer}: {cnt}")
        lines.append("")

    lines.append(f"── HIGH Staleness ({len(high)} records) — Pruning Candidates ────────────────")
    lines.append("")

    if not high:
        lines.append("   No HIGH staleness memories found.")
    else:
        for m in high:
            lines.append(f"ID: {m['id']}")
            lines.append(f"  Layer: {m['layer']} | Agent: {m['owner_agent']}")
            lines.append(f"  Age: {m['days_since_written']}d | Surfaces: {m['surface_count']}")
            lines.append(f"  Preview: {m['text_preview'][:120]}")
            lines.append("")

    return "\n".join(lines)


def mark_reviewed(memory_id: str) -> str:
    """Reset the staleness clock for a memory by ID."""
    reg = load_reviewed_registry()
    ts = datetime.datetime.now().isoformat()
    reg[memory_id] = ts
    save_reviewed_registry(reg)
    return f"Marked {memory_id[:16]}... as reviewed at {ts}"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "report":
        print(report())

    elif cmd == "candidates":
        print(candidates())

    elif cmd == "mark-reviewed":
        if len(sys.argv) < 3:
            print("Usage: lifecycle.py mark-reviewed <id>")
            sys.exit(1)
        print(mark_reviewed(sys.argv[2]))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
