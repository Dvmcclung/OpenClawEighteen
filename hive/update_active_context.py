"""
Active Context Updater
Reads the last user message from session log, surfaces relevant memories,
writes to hive/active_context.md for Thea to read at session start.
"""
import json
import os
import glob
import time
import sys

SESSIONS_DIR = os.path.expanduser("~/.openclaw/agents/main/sessions")

KNOWN_SCHEMA_SIGNATURES = [
    # OpenClaw JSONL format signatures we know work
    # Format: (field_to_check, expected_value_or_None_if_just_presence)
    ('type', 'message'),      # message events
    ('message', None),        # message wrapper
]

def detect_schema_version(session_file):
    """
    Read the first few lines of a session file and check for known schema signatures.
    Returns 'known' or 'unknown'.
    Logs a warning if schema looks different from expected.
    """
    try:
        with open(session_file) as f:
            for i, line in enumerate(f):
                if i > 20:
                    break
                try:
                    event = json.loads(line)
                    if event.get('type') == 'message' and 'message' in event:
                        return 'known'
                except:
                    continue
    except:
        pass
    return 'unknown'
WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
OUTPUT_FILE = os.path.join(WORKSPACE, "hive/active_context.md")
LOG_FILE = os.path.join(WORKSPACE, "system/agent_activity.log")

def get_last_user_message():
    """Get the most recent user message from main session logs."""
    session_files = glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl"))
    if not session_files:
        return None

    latest = max(session_files, key=os.path.getmtime)

    schema = detect_schema_version(latest)
    if schema == 'unknown':
        with open(LOG_FILE, 'a') as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] SURFACE WARNING: session log schema may have changed — check update_active_context.py compatibility after OpenClaw upgrade\n")
        # Still try to run, but flag the warning

    last_user_msg = None
    with open(latest) as f:
        for line in f:
            try:
                event = json.loads(line)
                # Handle openclaw session format: {type: "message", message: {role, content}}
                msg = event.get('message', event)
                if msg.get('role') == 'user':
                    content = msg.get('content', '')
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get('type') == 'text':
                                text = block.get('text', '')
                                if text.strip():
                                    last_user_msg = text
                    elif isinstance(content, str) and content.strip():
                        last_user_msg = content
            except:
                continue

    return last_user_msg

def main():
    sys.path.insert(0, os.path.join(WORKSPACE, 'hive'))
    from surface_engine import surface_memories, format_context_block

    query = get_last_user_message()
    if not query or len(query.strip()) < 10:
        return

    # Skip system/heartbeat messages
    skip_phrases = ['heartbeat', 'HEARTBEAT', 'memory_refresh', 'Pre-compaction']
    if any(p in query for p in skip_phrases):
        return

    memories = surface_memories(query, k=5)

    # Write active context using new standardized format
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    iso_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    query_preview = query[:200].strip()

    lines = [
        f"# Active Context — {timestamp}",
        f"_Query: {query_preview}_",
        f"_Source: cron_",
        "",
    ]

    if memories:
        lines.append("## Surfaced Memories")
        lines.append("")
        for i, mem in enumerate(memories, 1):
            layer_tag = f"[{mem['layer']}]" if mem['layer'] != 'hive' else ""
            agent_tag = f"({mem['owner_agent']})" if mem['owner_agent'] not in ('thea', 'collective') else ""
            header = f"{i}. {layer_tag}{agent_tag}".strip()
            score_str = f"{mem['similarity']:.2f}"
            lines.append(f"**{header}** *(relevance: {score_str})*")
            text = mem['text'][:400] + "..." if len(mem['text']) > 400 else mem['text']
            lines.append(text)
            lines.append("")
    else:
        lines.append("*No relevant memories above threshold for current context.*")

    with open(OUTPUT_FILE, 'w') as f:
        f.write("\n".join(lines))

    # Log surface event (JSON for consistency with surface_on_demand)
    import json as _json
    top_scores = [f"{m['similarity']:.3f}" for m in memories[:3]]
    with open(LOG_FILE, 'a') as f:
        f.write(_json.dumps({
            "timestamp": iso_timestamp,
            "query": query_preview[:60],
            "source": "cron",
            "num_memories": len(memories),
            "top_scores": top_scores,
        }) + "\n")

if __name__ == "__main__":
    main()
