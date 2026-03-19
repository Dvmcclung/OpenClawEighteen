"""
Turn Tracker — Phase 4
Monitors session logs for completed turns, detects corrections,
and triggers the scoring engine.

Runs every 10 minutes via cron.
"""

import json
import os
import glob
import time
import sys

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
SESSIONS_DIR = os.path.expanduser("~/.openclaw/agents/main/sessions")
STATE_FILE = os.path.join(WORKSPACE, "hive/turn_tracker_state.json")
ACTIVE_CONTEXT_FILE = os.path.join(WORKSPACE, "hive/active_context.md")
TURN_LOG = os.path.join(WORKSPACE, "system/turn_log.jsonl")
LOG_FILE = os.path.join(WORKSPACE, "system/agent_activity.log")

# Correction signal phrases — short, natural, conversational
# FLAG FOR THEA/DALE: Expanded from original set. Dale's correction style
# tends toward brief redirects ("no", "actually", "wait") rather than
# textbook phrases. New signals added 2026-03-11 to catch these patterns.
CORRECTION_SIGNALS_EXACT = [
    "no.", "wrong.", "nope.", "incorrect.", "no,", "nope,",
]

CORRECTION_SIGNALS_CONTAINS = [
    # Existing
    "that's wrong", "that's not right", "not what i asked", "not what i meant",
    "try again", "redo this", "redo that", "start over", "you missed",
    "that's off", "rethink", "not correct", "you got that wrong",
    "that's not it", "no that's", "no, that's", "wrong answer",
    "that's incorrect", "fix that", "that needs to be redone",
    # Redirect signals — Dale's actual correction style
    "actually,", "actually —", "actually -", "wait,", "wait —",
    "no but", "no, but", "i meant", "i mean,",
    "that's not what", "not quite", "close but",
    "you're missing", "you missed the point", "not exactly",
    "let me clarify", "to clarify", "let me rephrase",
    "that's backwards", "other way", "other way around",
    "wrong direction", "opposite", "flip that",
    "disregard", "ignore that", "scratch that",
]

# Short redirect starters — if the message is ≤8 words AND starts with one of these,
# treat as a correction. Catches "no, do X instead" without false-positives on longer msgs.
CORRECTION_REDIRECT_STARTERS = [
    "no,", "no —", "nope,", "actually", "wait,", "wait —",
    "wrong,", "incorrect,", "not quite", "hold on",
]

# Low-signal turns — acknowledgements with no substantive content.
# These should not trigger rewards (positive bias from benign exchanges).
LOW_SIGNAL_EXACT = {
    "ok", "okay", "ok.", "okay.", "ok!", "okay!",
    "thanks", "thank you", "thanks.", "thank you.",
    "got it", "got it.", "sounds good", "sounds good.",
    "great", "great.", "perfect", "perfect.",
    "sure", "sure.", "yep", "yep.", "yes", "yes.",
    "👍", "✅", "👌",
}

def is_low_signal(text):
    """Return True if the message is a brief acknowledgement with no scoreable content."""
    stripped = text.strip().lower()
    if stripped in LOW_SIGNAL_EXACT:
        return True
    # Very short messages (≤3 words) that don't contain a question or action verb
    words = stripped.split()
    if len(words) <= 3 and '?' not in stripped:
        # Check it doesn't look like a command or question
        action_starters = ('show', 'get', 'run', 'check', 'find', 'list', 'tell', 'what', 'how', 'why', 'when', 'where', 'can', 'do', 'is', 'are', 'will', 'fix', 'add', 'remove', 'update', 'delete', 'create', 'make')
        if not any(stripped.startswith(s) for s in action_starters):
            return True
    return False

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] TRACKER {msg}"
    with open(LOG_FILE, 'a') as f:
        f.write(line + "\n")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {'last_processed_line': 0, 'last_session': '', 'pending_memory_ids': []}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def detect_correction(text):
    """
    Detect if a user message contains a correction signal.
    Handles both explicit corrections and implicit ones (short "no" messages).
    """
    text_stripped = text.strip().lower().rstrip('.,!')

    # Exact short corrections
    if text_stripped in ['no', 'wrong', 'nope', 'incorrect', "that's wrong", 'redo', 'redo this']:
        return True

    # Exact phrase matches
    for phrase in CORRECTION_SIGNALS_EXACT:
        if text_stripped == phrase.rstrip('.'):
            return True

    # Contains matches
    text_lower = text.lower()
    for phrase in CORRECTION_SIGNALS_CONTAINS:
        if phrase in text_lower:
            return True

    # Short message starting with "no" (high precision signal)
    if len(text.split()) <= 5 and text_lower.startswith('no '):
        return True

    # Short redirect starters (≤8 words) — catches "actually, do X" patterns
    words = text.split()
    if len(words) <= 8:
        for starter in CORRECTION_REDIRECT_STARTERS:
            if text_lower.startswith(starter):
                return True

    return False

def get_recent_turn_memory_ids(lookback_minutes=None):
    """
    Read the most recent turn from turn_log.jsonl and return its surfaced memory IDs.
    lookback_minutes: if set, only consider turns within that window.
                      if None, returns the most recent turn regardless of age (within current session day).
    Returns list of (memory_id, score) tuples from the most recent qualifying turn.
    """
    if not os.path.exists(TURN_LOG):
        return []

    import datetime
    cutoff = None
    if lookback_minutes is not None:
        cutoff = datetime.datetime.now() - datetime.timedelta(minutes=lookback_minutes)

    # Use 6-hour rolling window instead of hard midnight cutoff — prevents
    # session boundary failures for conversations that span midnight.
    session_cutoff = datetime.datetime.now() - datetime.timedelta(hours=6)

    all_turns = []
    try:
        with open(TURN_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("timestamp", "")
                    ts = datetime.datetime.fromisoformat(ts_str)
                    if ts < session_cutoff:
                        continue
                    if cutoff and ts < cutoff:
                        continue
                    all_turns.append(rec)
                except (json.JSONDecodeError, ValueError):
                    pass
    except Exception:
        return []

    if not all_turns:
        return []

    # Use the most recent qualifying turn's memory IDs
    latest = all_turns[-1]
    ids = latest.get("surfaced_memory_ids", [])
    scores = latest.get("surfaced_memory_scores", [])
    return list(zip(ids, scores)) if ids else []


def get_active_memory_ids():
    """
    Return memory IDs from the most recent logged turn.
    Used for correction penalty attribution.
    """
    pairs = get_recent_turn_memory_ids(lookback_minutes=15)
    return [mid for mid, _ in pairs]


def get_preceding_turn_memory_ids(as_pairs=False):
    """
    Return memory IDs from the SECOND-TO-LAST turn in turn_log.jsonl (today only).

    Why: when scoring a user message at turn N, the most recent turn_log entry
    is for turn N (just surfaced). We want the memories from turn N-1 — the ones
    that were surfaced before the agent's response that the user is now reacting to.

    as_pairs=True: return list of (memory_id, score) tuples
    as_pairs=False: return list of memory_id strings
    """
    if not os.path.exists(TURN_LOG):
        return [] if not as_pairs else []

    import datetime
    # Use a 6-hour lookback instead of a hard midnight cutoff.
    # Midnight cutoff breaks conversations that span day boundaries (e.g. 11:55 PM → 12:05 AM).
    # 6 hours covers any realistic conversation gap while still excluding ancient history.
    six_hours_ago = datetime.datetime.now() - datetime.timedelta(hours=6)

    all_turns = []
    try:
        with open(TURN_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("timestamp", "")
                    ts = datetime.datetime.fromisoformat(ts_str)
                    # Only count human turns — exclude heartbeat/cron turns
                    # (legacy entries without 'source' field are assumed human)
                    if ts >= six_hours_ago and rec.get("source", "human") == "human":
                        all_turns.append(rec)
                except (json.JSONDecodeError, ValueError):
                    pass
    except Exception:
        return []

    # Need at least 2 turns to get the preceding one
    if len(all_turns) < 2:
        # Fall back to most recent if only one turn exists
        target = all_turns[-1] if all_turns else None
    else:
        target = all_turns[-2]  # Second-to-last = the turn before the current

    if not target:
        return []

    ids = target.get("surfaced_memory_ids", [])
    scores = target.get("surfaced_memory_scores", [])

    if as_pairs:
        return list(zip(ids, scores)) if ids else []
    return ids


def fetch_memory_texts(memory_ids):
    """
    Look up the text content for a list of memory_ids from LanceDB.
    Returns a list of text strings (same order as memory_ids, empty string if not found).
    """
    if not memory_ids:
        return []
    try:
        import lancedb
        db = lancedb.connect(os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb"))
        table = db.open_table("hybrid_facts")
        df = table.to_pandas()
        id_to_text = dict(zip(df["memory_id"].tolist(), df["text"].tolist()))
        return [id_to_text.get(mid, "") for mid in memory_ids]
    except Exception as e:
        log(f"fetch_memory_texts failed: {e}")
        return [""] * len(memory_ids)

def main():
    state = load_state()

    # Find latest session
    session_files = glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl"))
    if not session_files:
        return

    latest = max(session_files, key=os.path.getmtime)

    # Reset line counter if session file changed
    if state.get('last_session') != latest:
        log(f"New session detected: {os.path.basename(latest)} — resetting line counter")
        state['last_processed_line'] = 0
        state['last_session'] = latest

    # Also reset if stored line count exceeds actual file length (stale state)
    try:
        with open(latest) as f:
            file_len = sum(1 for _ in f)
        if state.get('last_processed_line', 0) > file_len:
            log(f"State line {state['last_processed_line']} > file len {file_len} — resetting")
            state['last_processed_line'] = 0
    except Exception:
        pass

    # Track new turns since last run
    messages = []
    with open(latest) as f:
        for i, line in enumerate(f):
            if i < state.get('last_processed_line', 0):
                continue
            try:
                event = json.loads(line)
                msg_type = event.get('type', '')
                if msg_type == 'message':
                    msg = event.get('message', {})
                    role = msg.get('role', '')
                    content = msg.get('content', '')
                    if isinstance(content, list):
                        text = ' '.join(b.get('text', '') for b in content if isinstance(b, dict))
                    else:
                        text = str(content)
                    messages.append({'role': role, 'text': text, 'line': i})
            except:
                continue

    if not messages:
        return

    # Update last processed line
    state['last_processed_line'] = messages[-1]['line'] + 1 if messages else state.get('last_processed_line', 0)

    # Check for correction signals in user messages
    sys.path.insert(0, WORKSPACE + '/hive')
    from score_engine import process_turn_reward, get_score_summary

    corrections_found = 0
    rewards_applied = 0

    for i, msg in enumerate(messages):
        if msg['role'] != 'user':
            continue

        # Find the preceding agent response (the message we're scoring)
        agent_response_text = ""
        for j in range(i - 1, max(i - 5, -1), -1):
            if messages[j]['role'] == 'assistant':
                agent_response_text = messages[j]['text'][:1000]
                break

        # Skip low-signal turns entirely — no reward, no penalty
        if is_low_signal(msg['text']):
            continue

        # Skip if the agent response itself is low-signal (e.g. HEARTBEAT_OK, one-liners)
        # Coherence scoring against empty/trivial responses always returns 0.5 — no signal
        if not agent_response_text or is_low_signal(agent_response_text) or len(agent_response_text.split()) < 10:
            continue

        if detect_correction(msg['text']):
            # Penalize memories that were surfaced BEFORE the agent's response
            # (i.e., the turn that produced the bad response we're now correcting).
            # Use get_preceding_turn_memory_ids() to get the turn before the most recent.
            active_ids = get_preceding_turn_memory_ids()
            if active_ids:
                mem_texts = fetch_memory_texts(active_ids)
                process_turn_reward(active_ids, mem_texts, agent_response_text, correction=True)
                corrections_found += 1
                log(f"Correction detected ('{msg['text'][:60]}'), penalized {len(active_ids)} memories: {active_ids}")
        else:
            # Reward memories from the preceding agent turn — the ones that were
            # surfaced and contributed to the response the user just accepted.
            # agent_response_text is the assistant message immediately before msg[i].
            pairs = get_preceding_turn_memory_ids(as_pairs=True)
            if pairs:
                reward_ids = [mid for mid, _ in pairs if mid]
                if reward_ids:
                    mem_texts = fetch_memory_texts(reward_ids)
                    process_turn_reward(reward_ids, mem_texts, agent_response_text, correction=False)
                    rewards_applied += 1

    if corrections_found or rewards_applied:
        log(f"Scoring pass: {corrections_found} correction(s), {rewards_applied} reward(s)")

    save_state(state)

    # Log current score summary periodically (every ~hour = 6 runs)
    run_count = state.get('run_count', 0) + 1
    state['run_count'] = run_count
    if run_count % 6 == 0:
        summary = get_score_summary()
        log(f"Score summary: mean={summary.get('mean_score', 0):.3f} high={summary.get('high_performers', 0)} low={summary.get('low_performers', 0)}")

    save_state(state)

if __name__ == "__main__":
    main()
