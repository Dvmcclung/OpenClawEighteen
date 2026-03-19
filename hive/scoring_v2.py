"""
Scoring V2 — Better proxies for memory utility.
Replaces cosine similarity with three signals:

Signal 1: Citation tracking
  Did the memory content actually appear in the response?
  Method: Check if key phrases from the memory appear in the agent's output.
  Score: +0.15 if cited, 0 if not cited (neutral, not penalized)

Signal 2: Correction signal
  Was a correction received after this turn?
  Method: Check turn_log.jsonl for correction_received=True on this turn
  Score: -0.25 if correction received, +0.05 if no correction (same turn window)

Signal 3: Session outcome
  Did the session end without escalation/confusion?
  Method: Proxy — if the next 3 turns have no corrections, session is "healthy"
  Score: +0.08 per healthy follow-up turn (capped at 3)

Combined delta: sum of applicable signals, clamped to [-0.30, +0.20]

Usage:
  python3 scoring_v2.py <turn_id>          — compute v2 delta for a turn
  python3 scoring_v2.py signal1 <turn_id> <response_text>  — citation check
"""

import json
import os
import sys
import re
import hashlib
from typing import Optional

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
TURN_LOG = os.path.join(WORKSPACE, "system/turn_log.jsonl")

# Signal weights
CITATION_SCORE = 0.15
NO_CORRECTION_SCORE = 0.05
CORRECTION_PENALTY = -0.25
HEALTHY_TURN_SCORE = 0.08
MAX_HEALTHY_TURNS = 3

DELTA_MIN = -0.30
DELTA_MAX = 0.20


# ─────────────────────────────────────────────────────────────────────────────
# Signal 1: Citation tracking
# ─────────────────────────────────────────────────────────────────────────────

def _extract_key_phrases(text: str, max_phrases: int = 5) -> list[str]:
    """Extract short key phrases from memory text for citation matching."""
    # Split into sentences, take first few words of each
    phrases = []
    sentences = re.split(r'[.!?\n]', text)
    for sent in sentences:
        words = sent.strip().split()
        if len(words) >= 4:
            # Take a 4-word window from the middle of the sentence
            mid = len(words) // 2
            phrase = " ".join(words[max(0, mid-2):mid+2]).lower().strip()
            if len(phrase) > 8:
                phrases.append(phrase)
        if len(phrases) >= max_phrases:
            break
    return phrases


def signal1_citation(memory_text: str, response_text: str) -> float:
    """
    Signal 1: Did the memory content appear in the response?
    Returns CITATION_SCORE (0.15) if cited, 0.0 if not.
    """
    if not memory_text or not response_text:
        return 0.0

    response_lower = response_text.lower()
    memory_lower = memory_text.lower()

    # Check for long substring match (>30 chars from memory in response)
    if len(memory_lower) > 30:
        # Try 30-char windows across the memory
        for i in range(0, len(memory_lower) - 30, 15):
            chunk = memory_lower[i:i+30].strip()
            if chunk and chunk in response_lower:
                return CITATION_SCORE

    # Check key phrase matching
    phrases = _extract_key_phrases(memory_text)
    for phrase in phrases:
        if phrase in response_lower:
            return CITATION_SCORE

    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Signal 2: Correction signal
# ─────────────────────────────────────────────────────────────────────────────

def _load_turn_log() -> list[dict]:
    """Load all records from turn_log.jsonl."""
    if not os.path.exists(TURN_LOG):
        return []
    records = []
    with open(TURN_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def signal2_correction(turn_id: str) -> float:
    """
    Signal 2: Was a correction received for this turn?
    Returns -0.25 if corrected, +0.05 if no correction found.
    """
    records = _load_turn_log()
    for rec in records:
        if rec.get("turn_id") == turn_id:
            if rec.get("correction_received"):
                return CORRECTION_PENALTY
            else:
                return NO_CORRECTION_SCORE
    # Turn not found — neutral
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Signal 3: Session outcome (healthy follow-up turns)
# ─────────────────────────────────────────────────────────────────────────────

def signal3_session_outcome(turn_id: str, session_id: str = None) -> float:
    """
    Signal 3: Did the next 3 turns in the session have no corrections?
    Returns up to 3 * 0.08 = 0.24 (capped at DELTA_MAX after combining).
    """
    records = _load_turn_log()

    # Find the index of this turn
    this_idx = None
    inferred_session = session_id
    for i, rec in enumerate(records):
        if rec.get("turn_id") == turn_id:
            this_idx = i
            inferred_session = inferred_session or rec.get("session_id")
            break

    if this_idx is None:
        return 0.0

    # Look at next 3 turns in same session
    healthy_count = 0
    checked = 0
    for rec in records[this_idx + 1:]:
        if inferred_session and rec.get("session_id") != inferred_session:
            continue
        if checked >= MAX_HEALTHY_TURNS:
            break
        if not rec.get("correction_received"):
            healthy_count += 1
        checked += 1

    return healthy_count * HEALTHY_TURN_SCORE


# ─────────────────────────────────────────────────────────────────────────────
# Combined scoring
# ─────────────────────────────────────────────────────────────────────────────

def compute_v2_delta(
    turn_id: str,
    memory_text: str = "",
    response_text: str = "",
    session_id: str = None,
) -> dict:
    """
    Compute v2 score delta for a turn.

    Args:
        turn_id:       The turn to score.
        memory_text:   The memory content that was surfaced (for citation check).
        response_text: The agent's response text (for citation check).
        session_id:    Session ID (optional, inferred from turn_log if not provided).

    Returns:
        dict with individual signals, combined delta, and clamped delta.
    """
    s1 = signal1_citation(memory_text, response_text) if (memory_text and response_text) else 0.0
    s2 = signal2_correction(turn_id)
    s3 = signal3_session_outcome(turn_id, session_id)

    raw_delta = s1 + s2 + s3
    clamped_delta = max(DELTA_MIN, min(DELTA_MAX, raw_delta))

    return {
        "turn_id": turn_id,
        "signal1_citation": s1,
        "signal2_correction": s2,
        "signal3_session_outcome": s3,
        "raw_delta": round(raw_delta, 4),
        "clamped_delta": round(clamped_delta, 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# V1 score delta (cosine similarity proxy — baseline)
# ─────────────────────────────────────────────────────────────────────────────

def compute_v1_delta(turn_id: str) -> float:
    """
    V1 proxy: use mean surfaced similarity score as the delta.
    This is the existing approach — just a cosine similarity average.
    """
    records = _load_turn_log()
    for rec in records:
        if rec.get("turn_id") == turn_id:
            scores = rec.get("surfaced_memory_scores", [])
            if scores:
                return round(sum(scores) / len(scores) - 0.5, 4)  # center around 0
            return 0.0
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "signal1":
        if len(sys.argv) < 4:
            print("Usage: scoring_v2.py signal1 <memory_text> <response_text>")
            sys.exit(1)
        score = signal1_citation(sys.argv[2], sys.argv[3])
        print(f"Signal 1 (citation): {score}")

    elif cmd == "signal2":
        if len(sys.argv) < 3:
            print("Usage: scoring_v2.py signal2 <turn_id>")
            sys.exit(1)
        score = signal2_correction(sys.argv[2])
        print(f"Signal 2 (correction): {score}")

    elif cmd == "signal3":
        if len(sys.argv) < 3:
            print("Usage: scoring_v2.py signal3 <turn_id>")
            sys.exit(1)
        score = signal3_session_outcome(sys.argv[2])
        print(f"Signal 3 (session outcome): {score}")

    else:
        # Treat first arg as turn_id
        turn_id = sys.argv[1]
        result = compute_v2_delta(turn_id)
        print(json.dumps(result, indent=2))
