"""
On-Demand Hive Memory Surfacing
Usage: python3 surface_on_demand.py "query text" [k] [--domain DOMAIN] [--type TYPE] [--status STATUS]

Writes results to hive/active_context.md with timestamp, query, source=on-demand.
Returns results to stdout for agent incorporation.
Also writes a turn_id to active_context.md so agents can log attribution.

Sprint 5: Added --domain, --type, --status CLI flags for tag-based filtering.
          active_context.md now shows tags alongside each memory.

Exit codes: 0 = success, 1 = no results / error
"""

import sys
import os
import time
import json
import uuid
import argparse

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
OUTPUT_FILE = os.path.join(WORKSPACE, "hive/active_context.md")
TURN_LOG = os.path.join(WORKSPACE, "system/turn_log.jsonl")
SURFACE_LOG = os.path.join(WORKSPACE, "system/hive_surface.log")
LOG_FILE = os.path.join(WORKSPACE, "system/agent_activity.log")

sys.path.insert(0, os.path.join(WORKSPACE, "hive"))


def surface_on_demand(query: str, k: int = 5, agent: str = "main", session_id: str = "",
                      tag_domain: str = None, tag_type: str = None, tag_status: str = None):
    """
    Surface memories for query. Write active_context.md and return structured results.
    Sprint 5: optional tag_domain, tag_type, tag_status filters.
    Returns dict: {turn_id, memories, block}
    """
    from surface_engine import surface_memories, format_context_block

    agent = agent or os.environ.get("OPENCLAW_AGENT_ID", "main")
    session_id = session_id or os.environ.get("OPENCLAW_SESSION_ID", str(uuid.uuid4())[:8])

    turn_id = str(uuid.uuid4())
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    iso_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    memories = surface_memories(
        query, k=k,
        tag_domain=tag_domain,
        tag_type=tag_type,
        tag_status=tag_status,
    )

    block = format_context_block(memories, query)

    # Build active_context.md — Sprint 5 format: tags shown per memory
    query_preview = query[:200].strip()

    # Build filter summary line
    filter_parts = []
    if tag_domain:
        filter_parts.append(f"domain:{tag_domain}")
    if tag_type:
        filter_parts.append(f"type:{tag_type}")
    if tag_status:
        filter_parts.append(f"status:{tag_status}")
    filter_line = f"_Filters: {', '.join(filter_parts)}_\n" if filter_parts else ""

    lines = [
        f"# Active Context — {timestamp}",
        f"_Query: {query_preview}_",
        f"_Source: on-demand_",
        filter_line.rstrip() if filter_line else "",
        f"_Turn ID: {turn_id}_",
        "",
    ]
    # Remove blank line if no filter
    lines = [l for l in lines if l != ""]
    lines.append("")

    if memories:
        lines.append("## Surfaced Memories")
        lines.append("")
        for i, mem in enumerate(memories, 1):
            # Tag label
            tag_parts = []
            if mem.get('tag_domain'):
                tag_parts.append(f"domain:{mem['tag_domain']}")
            if mem.get('tag_type'):
                tag_parts.append(f"type:{mem['tag_type']}")
            if mem.get('tag_source'):
                tag_parts.append(f"source:{mem['tag_source']}")
            tag_label = f" [{' | '.join(tag_parts)}]" if tag_parts else ""

            layer_tag = f"[{mem['layer']}]" if mem['layer'] != 'hive' else ""
            agent_tag = f"({mem['owner_agent']})" if mem['owner_agent'] not in ('thea', 'collective') else ""
            header = f"Memory {i}{f' {layer_tag}' if layer_tag else ''}{f' {agent_tag}' if agent_tag else ''}".strip()
            score_str = f"{mem['similarity']:.2f}"

            lines.append(f"## {header} (score: {score_str}){tag_label}")
            lines.append(f"_turn_id: {turn_id}_")
            text = mem['text'][:400] + "..." if len(mem['text']) > 400 else mem['text']
            lines.append(text)
            lines.append("")
    else:
        lines.append("*No relevant memories above threshold for current context.*")
        lines.append("")

    lines.append("---")
    lines.append(f"*To log a correction for this turn: `python3 {WORKSPACE}/hive/attribution.py log_correction {turn_id} 'correction text'`*")

    output_content = "\n".join(lines)

    with open(OUTPUT_FILE, 'w') as f:
        f.write(output_content)

    # Log surface event
    top_scores = [f"{m['similarity']:.3f}" for m in memories[:3]]
    with open(SURFACE_LOG, 'a') as f:
        f.write(json.dumps({
            "timestamp": iso_timestamp,
            "query": query_preview,
            "source": "on-demand",
            "agent": agent,
            "session_id": session_id,
            "turn_id": turn_id,
            "num_memories": len(memories),
            "top_scores": top_scores,
            "tag_filters": {"domain": tag_domain, "type": tag_type, "status": tag_status},
        }) + "\n")

    filter_str = f" filters=[{','.join(filter_parts)}]" if filter_parts else ""
    with open(LOG_FILE, 'a') as f:
        f.write(f"[{timestamp}] SURFACE on-demand query='{query[:60].strip()}' memories={len(memories)} turn_id={turn_id}{filter_str}\n")

    try:
        import attribution
        attribution.log_turn(
            agent=agent,
            session_id=session_id,
            message_preview=query[:100],
            surfaced_memories=memories,
            turn_id=turn_id,
        )
    except Exception as e:
        with open(LOG_FILE, 'a') as f:
            f.write(f"[{timestamp}] WARN attribution.log_turn failed: {e}\n")

    # Increment surface_count for surfaced memories (feeds inactivity penalty scoring)
    if memories:
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            from score_engine import increment_surface_count
            memory_ids = [m.get('memory_id', '') for m in memories if m.get('memory_id')]
            if memory_ids:
                increment_surface_count(memory_ids)
        except Exception as e:
            with open(LOG_FILE, 'a') as f:
                f.write(f"[{timestamp}] WARN increment_surface_count failed: {e}\n")

    return {
        "turn_id": turn_id,
        "timestamp": iso_timestamp,
        "memories": memories,
        "block": output_content,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="On-demand hive memory surfacing")
    parser.add_argument("query", help="Query text")
    parser.add_argument("k", nargs="?", type=int, default=5, help="Max results (default 5)")
    parser.add_argument("--domain", dest="tag_domain", default=None,
                        help="Filter by tag_domain (ops|comms|supply-chain|math|cross-domain)")
    parser.add_argument("--type", dest="tag_type", default=None,
                        help="Filter by tag_type (fix|rubric|fact|insight|decision|procedure)")
    parser.add_argument("--status", dest="tag_status", default=None,
                        help="Filter by tag_status (active|under-review|superseded|provisional)")

    args = parser.parse_args()

    result = surface_on_demand(
        args.query, k=args.k,
        tag_domain=args.tag_domain,
        tag_type=args.tag_type,
        tag_status=args.tag_status,
    )

    print(result["block"])
    print()
    print(f"<!-- turn_id: {result['turn_id']} -->")
