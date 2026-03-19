"""
Hive Token Usage Estimator
Estimates token overhead from hive memory operations vs. pre-hive baseline.
"""

import os
import json
import time
import glob

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
SCORE_LOG = os.path.join(WORKSPACE, "hive/score_events.jsonl")
ACTIVE_CONTEXT = os.path.join(WORKSPACE, "hive/active_context.md")

# Token pricing (as of 2026-03-05)
# Claude Sonnet 4.6: ~$3/1M input tokens, $15/1M output tokens
INPUT_TOKEN_COST_PER_M = 3.0
OUTPUT_TOKEN_COST_PER_M = 15.0

# OpenAI ada-002 embedding: $0.10/1M tokens
EMBEDDING_COST_PER_M = 0.10

def estimate_chars_to_tokens(chars):
    """Rough estimate: 4 chars per token."""
    return chars / 4

def get_active_context_size():
    """Get current size of active_context.md in tokens."""
    if not os.path.exists(ACTIVE_CONTEXT):
        return 0
    with open(ACTIVE_CONTEXT) as f:
        content = f.read()
    return estimate_chars_to_tokens(len(content))

def estimate_daily_embedding_cost():
    """
    Estimate daily embedding API cost from hive operations.
    - update_active_context.py runs every 5 min = 288 times/day
    - Each run embeds ~1 user message (~50 tokens avg)
    - ingest_daily_memory.py runs once, embeds ~10 chunks of ~750 tokens each
    """
    # Surface engine calls
    surface_calls_per_day = 288
    tokens_per_surface_call = 50  # avg user message
    surface_tokens = surface_calls_per_day * tokens_per_surface_call

    # Daily ingest
    ingest_chunks = 10  # avg chunks per daily memory file
    tokens_per_chunk = 750
    ingest_tokens = ingest_chunks * tokens_per_chunk

    total_embedding_tokens = surface_tokens + ingest_tokens
    daily_embedding_cost = (total_embedding_tokens / 1_000_000) * EMBEDDING_COST_PER_M

    return {
        'surface_tokens': surface_tokens,
        'ingest_tokens': ingest_tokens,
        'total_embedding_tokens': total_embedding_tokens,
        'daily_cost_usd': daily_embedding_cost
    }

def estimate_context_overhead_per_session():
    """
    Estimate additional tokens injected into each conversation session
    from hive memory surfacing.
    Pre-hive: 0 additional tokens from memory surfacing
    Post-hive: up to 5 memories x 400 chars each + headers = ~600-700 tokens
    """
    active_context_tokens = get_active_context_size()

    pre_hive_overhead = 0
    post_hive_overhead = active_context_tokens

    # Cost of the overhead (input tokens)
    overhead_cost_per_session = (post_hive_overhead / 1_000_000) * INPUT_TOKEN_COST_PER_M

    return {
        'pre_hive_tokens': pre_hive_overhead,
        'post_hive_tokens': post_hive_overhead,
        'delta_tokens': post_hive_overhead - pre_hive_overhead,
        'cost_per_session_usd': overhead_cost_per_session,
        'sessions_per_day_estimate': 10,  # typical active usage
        'daily_overhead_cost_usd': overhead_cost_per_session * 10
    }

def estimate_efficiency_gain():
    """
    Estimate token efficiency gain from proactive surfacing.

    Pre-hive: User had to remind agent of context manually.
    Each reminder = ~200-500 tokens of user input + agent processing.
    Estimate 3-5 reminders per session on average (based on observed behavior).

    Post-hive: Memories surface proactively.
    If surfacing eliminates 80% of reminders, net savings =
    (reminders_eliminated * avg_reminder_tokens) - hive_overhead_tokens
    """
    reminders_per_session_pre_hive = 4  # observed: forgot team, forgot PACCAR, forgot rubric, etc.
    avg_reminder_tokens = 300  # user reminder + agent ack + re-explanation
    pre_hive_reminder_cost = reminders_per_session_pre_hive * avg_reminder_tokens

    # Post-hive: assume 70% reduction in reminders (conservative)
    reminder_reduction_rate = 0.70
    reminders_eliminated = reminders_per_session_pre_hive * reminder_reduction_rate
    tokens_saved = reminders_eliminated * avg_reminder_tokens

    # Net efficiency
    overhead = estimate_context_overhead_per_session()['delta_tokens']
    net_token_delta = tokens_saved - overhead

    return {
        'pre_hive_reminder_tokens_per_session': pre_hive_reminder_cost,
        'estimated_reminder_reduction_rate': reminder_reduction_rate,
        'tokens_saved_per_session': tokens_saved,
        'hive_overhead_tokens': overhead,
        'net_token_delta_per_session': net_token_delta,
        'efficiency_verdict': 'net_positive' if net_token_delta > 0 else 'net_negative',
        'note': 'Conservative estimate. Improves as hive matures and reminder rate drops further.'
    }

def get_score_event_stats():
    """Read score_events.jsonl and compute basic stats."""
    if not os.path.exists(SCORE_LOG):
        return {'total_events': 0, 'rewards': 0, 'penalties': 0, 'corrections': 0, 'avg_coherence': 0.0}

    events = []
    with open(SCORE_LOG) as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except:
                continue

    rewards = sum(1 for e in events if e.get('delta', 0) > 0)
    penalties = sum(1 for e in events if e.get('delta', 0) < 0)
    corrections = sum(1 for e in events if e.get('correction', False))

    return {
        'total_events': len(events),
        'rewards': rewards,
        'penalties': penalties,
        'corrections': corrections,
        'avg_coherence': sum(e.get('coherence', 0) for e in events) / max(len(events), 1)
    }

def get_quality_observations():
    """Load quality observations log."""
    obs_file = os.path.join(WORKSPACE, "hive/quality_observations.jsonl")
    if not os.path.exists(obs_file):
        return []

    obs = []
    with open(obs_file) as f:
        for line in f:
            try:
                obs.append(json.loads(line))
            except:
                continue
    return obs

def build_health_report():
    """Build the full hive health report."""
    embedding = estimate_daily_embedding_cost()
    context = estimate_context_overhead_per_session()
    efficiency = estimate_efficiency_gain()
    scores = get_score_event_stats()
    observations = get_quality_observations()

    pre_hive_obs = [o for o in observations if o.get('phase') == 'pre-hive']
    post_hive_obs = [o for o in observations if o.get('phase') == 'post-hive']
    proactive_hits = [o for o in post_hive_obs if o.get('type') == 'proactive_hit']
    had_to_remind = [o for o in post_hive_obs if o.get('type') == 'had_to_remind']
    corrections_obs = [o for o in post_hive_obs if o.get('type') == 'correction']

    report_date = time.strftime("%Y-%m-%d")

    lines = [
        f"HIVE MEMORY HEALTH REPORT — {report_date}",
        "=" * 50,
        "",
        "## QUALITY METRICS",
        "",
        f"Pre-hive incidents logged: {len(pre_hive_obs)}",
        f"  - Had to remind: {len([o for o in pre_hive_obs if o['type'] == 'had_to_remind'])}",
        "",
        f"Post-hive observations: {len(post_hive_obs)}",
        f"  - Proactive hits (surfaced without prompting): {len(proactive_hits)}",
        f"  - Had to remind (hive missed): {len(had_to_remind)}",
        f"  - Corrections: {len(corrections_obs)}",
        "",
    ]

    if proactive_hits or had_to_remind:
        hit_rate = len(proactive_hits) / max(len(proactive_hits) + len(had_to_remind), 1)
        lines.append(f"  Proactive hit rate: {hit_rate:.0%}")
        lines.append("")

    lines += [
        "## SCORE ENGINE STATS",
        "",
        f"Total score events: {scores['total_events']}",
        f"  Rewards: {scores['rewards']}",
        f"  Penalties: {scores['penalties']}",
        f"  Correction signals: {scores['corrections']}",
        f"  Avg coherence score: {scores['avg_coherence']:.3f}",
        "",
        "## TOKEN USAGE ANALYSIS",
        "",
        "### Context overhead per session",
        f"  Pre-hive overhead: {context['pre_hive_tokens']:.0f} tokens",
        f"  Post-hive overhead: {context['post_hive_tokens']:.0f} tokens (active_context.md injection)",
        f"  Delta: +{context['delta_tokens']:.0f} tokens per session",
        f"  Cost per session: ~${context['cost_per_session_usd']:.4f}",
        f"  Est. daily overhead cost (10 sessions): ~${context['daily_overhead_cost_usd']:.4f}",
        "",
        "### Embedding API costs",
        f"  Surface engine calls/day: ~288 (every 5 min)",
        f"  Embedding tokens/day: ~{embedding['total_embedding_tokens']:,}",
        f"  Daily embedding cost: ~${embedding['daily_cost_usd']:.4f}",
        "",
        "### Efficiency gain estimate",
        f"  Pre-hive reminder tokens/session: ~{efficiency['pre_hive_reminder_tokens_per_session']} tokens",
        f"  Estimated reminder reduction: {efficiency['estimated_reminder_reduction_rate']:.0%}",
        f"  Tokens saved/session (from fewer reminders): ~{efficiency['tokens_saved_per_session']:.0f}",
        f"  Hive overhead tokens/session: ~{efficiency['hive_overhead_tokens']:.0f}",
        f"  NET token delta/session: {efficiency['net_token_delta_per_session']:+.0f} tokens",
        f"  Verdict: {efficiency['efficiency_verdict'].upper()}",
        "",
        "### Total daily cost estimate",
        f"  Context overhead: ~${context['daily_overhead_cost_usd']:.4f}",
        f"  Embedding calls: ~${embedding['daily_cost_usd']:.4f}",
        f"  Total hive overhead: ~${context['daily_overhead_cost_usd'] + embedding['daily_cost_usd']:.4f}/day",
        "",
        "## NOTES",
        "",
        efficiency['note'],
        "",
        "Observation log: hive/quality_observations.jsonl",
        "Add entries with: {type: proactive_hit|had_to_remind|correction, description: ..., phase: post-hive}",
        "",
        "Score history: hive/score_events.jsonl",
        "Score divergence will be visible after 2-4 weeks of active use.",
    ]

    return "\n".join(lines)

if __name__ == "__main__":
    report = build_health_report()
    print(report)

    # Save to file
    output = os.path.join(WORKSPACE, "hive/hive_health_report.txt")
    with open(output, 'w') as f:
        f.write(report)
    print(f"\nSaved to {output}")
