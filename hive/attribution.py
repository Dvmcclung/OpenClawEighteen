"""
Turn-Level Correction Attribution Helper

Usage:
  python3 attribution.py log_turn <agent> <session_id> <message_preview> <surfaced_json>
  python3 attribution.py log_correction <turn_id> <correction_text>
  python3 attribution.py report [days]

surfaced_json: JSON string with list of {id, similarity} dicts from surface_on_demand result
"""

import sys
import os
import json
import uuid
import time

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
TURN_LOG = os.path.join(WORKSPACE, "system/turn_log.jsonl")


def _ensure_turn_log():
    os.makedirs(os.path.dirname(TURN_LOG), exist_ok=True)
    if not os.path.exists(TURN_LOG):
        open(TURN_LOG, 'w').close()


def log_turn(agent: str, session_id: str, message_preview: str, surfaced_memories: list,
             turn_id: str = None) -> str:
    """
    Write a turn record to turn_log.jsonl.
    surfaced_memories: list of dicts from surface_on_demand (with text, similarity, etc.)
    turn_id: optional — if provided, uses this ID instead of generating a new one.
    Returns turn_id.
    """
    _ensure_turn_log()

    turn_id = turn_id or str(uuid.uuid4())
    iso_ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    # Extract IDs and scores (use source field as proxy for ID since LanceDB doesn't expose row ID easily)
    mem_ids = []
    mem_scores = []
    for m in surfaced_memories:
        # Prefer deterministic memory_id; fall back to text hash for legacy records
        mid = m.get('memory_id', '')
        if not mid:
            import hashlib
            mid = hashlib.sha256(m.get('text', '').encode()).hexdigest()[:12]
        mem_ids.append(mid)
        mem_scores.append(round(float(m.get('similarity', 0)), 4))

    record = {
        "turn_id": turn_id,
        "timestamp": iso_ts,
        "agent": agent,
        "session_id": session_id,
        "message_preview": message_preview[:100],
        "surfaced_memory_ids": mem_ids,
        "surfaced_memory_scores": mem_scores,
        "correction_received": False,
        "correction_text": None,
        "correction_timestamp": None,
        "source": "human",  # 'human' = real user turn; 'heartbeat'/'cron' = automated
    }

    with open(TURN_LOG, 'a') as f:
        f.write(json.dumps(record) + "\n")

    return turn_id


def log_correction(turn_id: str, correction_text: str) -> bool:
    """
    Find the turn record by turn_id and update it with correction info.
    Rewrites the file (JSONL is small; this is acceptable).
    Returns True if found and updated.
    """
    _ensure_turn_log()

    records = []
    found = False

    with open(TURN_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("turn_id") == turn_id:
                    rec["correction_received"] = True
                    rec["correction_text"] = correction_text
                    rec["correction_timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    found = True
                records.append(rec)
            except json.JSONDecodeError:
                pass

    if found:
        with open(TURN_LOG, 'w') as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    return found


def get_attribution_report(days: int = 7) -> str:
    """
    Print recent turns with corrections and the memories that were active.
    """
    _ensure_turn_log()

    import datetime
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)

    corrected_turns = []
    all_turns = []

    with open(TURN_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                try:
                    ts = datetime.datetime.fromisoformat(rec.get("timestamp", ""))
                    if ts >= cutoff:
                        all_turns.append(rec)
                        if rec.get("correction_received"):
                            corrected_turns.append(rec)
                except ValueError:
                    pass
            except json.JSONDecodeError:
                pass

    lines = [
        f"# Attribution Report — Last {days} days",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total turns in window: {len(all_turns)}",
        f"Turns with corrections: {len(corrected_turns)}",
        "",
    ]

    if not corrected_turns:
        lines.append("No corrections logged in this period.")
    else:
        lines.append("## Corrected Turns")
        lines.append("")
        for rec in corrected_turns:
            lines.append(f"### Turn {rec['turn_id'][:8]}...")
            lines.append(f"- **Agent:** {rec['agent']}")
            lines.append(f"- **Timestamp:** {rec['timestamp']}")
            lines.append(f"- **Message:** {rec['message_preview']}")
            lines.append(f"- **Correction:** {rec['correction_text']}")
            lines.append(f"- **Correction received:** {rec['correction_timestamp']}")
            lines.append(f"- **Active memory IDs (hashes):** {', '.join(rec['surfaced_memory_ids'])}")
            lines.append(f"- **Active memory scores:** {', '.join(str(s) for s in rec['surfaced_memory_scores'])}")
            lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "log_turn":
        if len(sys.argv) < 6:
            print("Usage: attribution.py log_turn <agent> <session_id> <message_preview> <surfaced_json>")
            sys.exit(1)
        agent = sys.argv[2]
        session_id = sys.argv[3]
        message_preview = sys.argv[4]
        try:
            surfaced = json.loads(sys.argv[5])
        except json.JSONDecodeError:
            surfaced = []
        turn_id = log_turn(agent, session_id, message_preview, surfaced)
        print(f"Logged turn: {turn_id}")

    elif cmd == "log_correction":
        if len(sys.argv) < 4:
            print("Usage: attribution.py log_correction <turn_id> <correction_text>")
            sys.exit(1)
        turn_id = sys.argv[2]
        correction_text = " ".join(sys.argv[3:])
        ok = log_correction(turn_id, correction_text)
        if ok:
            print(f"Correction logged for turn {turn_id}")
        else:
            print(f"Turn {turn_id} not found in log")
            sys.exit(1)

    elif cmd == "report":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        print(get_attribution_report(days))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
