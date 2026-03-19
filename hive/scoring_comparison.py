"""
Scoring Comparison — V1 vs V2 signal comparison over a date range.

Usage:
  python3 scoring_comparison.py [start_date] [end_date]
  python3 scoring_comparison.py 2026-03-01 2026-03-31

  Dates default to last 7 days if not provided.

Output: comparison table of v1 vs v2 score deltas for each turn.
"""

import json
import os
import sys
import datetime

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
TURN_LOG = os.path.join(WORKSPACE, "system/turn_log.jsonl")

sys.path.insert(0, WORKSPACE + "/hive")


def load_turns_in_range(start: datetime.datetime, end: datetime.datetime) -> list[dict]:
    """Load turns from turn_log.jsonl within the given date range."""
    if not os.path.exists(TURN_LOG):
        return []
    turns = []
    with open(TURN_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts_str = rec.get("timestamp", "")
                try:
                    ts = datetime.datetime.fromisoformat(ts_str)
                    if start <= ts <= end:
                        turns.append(rec)
                except ValueError:
                    pass
            except json.JSONDecodeError:
                pass
    return turns


def format_table(rows: list[dict]) -> str:
    """Format comparison rows as a plain text table."""
    if not rows:
        return "No turns with surfaced memories in this range."

    header = f"{'Turn ID':>14}  {'Agent':>8}  {'Memories':>8}  {'V1 Delta':>10}  {'V2 Delta':>10}  {'S1 Cite':>8}  {'S2 Corr':>8}  {'S3 Sess':>8}  {'Corrected':>10}"
    sep = "-" * len(header)
    lines = [header, sep]

    for r in rows:
        corrected = "YES" if r.get("correction_received") else "no"
        lines.append(
            f"{r['turn_id'][:12]:>14}  "
            f"{r.get('agent','?')[:8]:>8}  "
            f"{r.get('mem_count', 0):>8}  "
            f"{r.get('v1_delta', 0.0):>10.4f}  "
            f"{r.get('v2_delta', 0.0):>10.4f}  "
            f"{r.get('s1', 0.0):>8.4f}  "
            f"{r.get('s2', 0.0):>8.4f}  "
            f"{r.get('s3', 0.0):>8.4f}  "
            f"{corrected:>10}"
        )

    # Summary stats
    v1_deltas = [r['v1_delta'] for r in rows]
    v2_deltas = [r['v2_delta'] for r in rows]
    corrected_count = sum(1 for r in rows if r.get('correction_received'))

    lines.append(sep)
    lines.append(f"Total turns: {len(rows)} | Corrected: {corrected_count}")
    if rows:
        lines.append(f"V1 mean delta: {sum(v1_deltas)/len(v1_deltas):.4f}  |  V2 mean delta: {sum(v2_deltas)/len(v2_deltas):.4f}")
        v1_var = sum((x - sum(v1_deltas)/len(v1_deltas))**2 for x in v1_deltas) / len(v1_deltas) if len(v1_deltas) > 1 else 0
        v2_var = sum((x - sum(v2_deltas)/len(v2_deltas))**2 for x in v2_deltas) / len(v2_deltas) if len(v2_deltas) > 1 else 0
        lines.append(f"V1 variance:   {v1_var:.4f}          |  V2 variance:   {v2_var:.4f}")

    return "\n".join(lines)


def run_comparison(start: datetime.datetime, end: datetime.datetime) -> str:
    from scoring_v2 import compute_v1_delta, compute_v2_delta

    turns = load_turns_in_range(start, end)
    if not turns:
        return f"No turns found between {start.date()} and {end.date()}."

    # Filter to turns that have surfaced memories
    turns_with_memories = [t for t in turns if t.get("surfaced_memory_ids")]

    if not turns_with_memories:
        return f"Found {len(turns)} turns but none had surfaced memories."

    rows = []
    for turn in turns_with_memories:
        tid = turn["turn_id"]
        v1 = compute_v1_delta(tid)
        v2_result = compute_v2_delta(tid)

        rows.append({
            "turn_id": tid,
            "agent": turn.get("agent", "?"),
            "mem_count": len(turn.get("surfaced_memory_ids", [])),
            "v1_delta": v1,
            "v2_delta": v2_result["clamped_delta"],
            "s1": v2_result["signal1_citation"],
            "s2": v2_result["signal2_correction"],
            "s3": v2_result["signal3_session_outcome"],
            "correction_received": turn.get("correction_received", False),
        })

    header = [
        f"# Scoring V1 vs V2 Comparison",
        f"Date range: {start.date()} → {end.date()}",
        f"Turns with surfaced memories: {len(rows)} of {len(turns)} total",
        f"Note: Signal 1 (citation) requires response_text — shown as 0.0 here (CLI mode)",
        "",
    ]

    return "\n".join(header) + format_table(rows)


if __name__ == "__main__":
    now = datetime.datetime.now()

    if len(sys.argv) >= 3:
        try:
            start = datetime.datetime.fromisoformat(sys.argv[1])
            end = datetime.datetime.fromisoformat(sys.argv[2])
        except ValueError as e:
            print(f"Invalid date format: {e}")
            print("Use YYYY-MM-DD format")
            sys.exit(1)
    else:
        # Default: last 7 days
        end = now
        start = now - datetime.timedelta(days=7)

    print(run_comparison(start, end))
