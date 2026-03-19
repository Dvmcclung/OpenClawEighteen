"""
Weekly Hive Score Report
Runs every Monday at 8 AM. Emails Dale a summary of hive memory evolution.
"""
import os
import sys
import json
import time
import lancedb
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
LANCEDB_PATH = os.path.expanduser("/home/qtxit/.openclaw/shared/memory/lancedb")
TABLE_NAME = "hybrid_facts"
SCORE_LOG = os.path.join(WORKSPACE, "hive/score_events.jsonl")
FAMILY_REGISTRY = os.path.join(WORKSPACE, "hive/family_registry.json")

def get_smtp_creds():
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    # Fall back to reading from mwf_briefing.py env pattern
    return "dvmcclung@me.com", "mrrk-qjxw-rpec-oiqw"

def get_score_stats():
    db = lancedb.connect(LANCEDB_PATH)
    if TABLE_NAME not in db.table_names():
        return {}
    table = db.open_table(TABLE_NAME)
    df = table.to_pandas()
    if 'score' not in df.columns:
        return {}
    
    stats = {
        'total': len(df),
        'mean': float(df['score'].mean()),
        'high': int((df['score'] >= 0.7).sum()),
        'low': int((df['score'] <= 0.3).sum()),
        'mid': int(((df['score'] > 0.3) & (df['score'] < 0.7)).sum()),
    }
    
    # Top 5 rising memories (highest score)
    if 'text' in df.columns:
        top = df.nlargest(5, 'score')[['text', 'score', 'layer']].to_dict('records')
        stats['top_memories'] = [{'text': r['text'][:120], 'score': float(r['score']), 'layer': r.get('layer','')} for r in top]
    
    # Layer breakdown
    if 'layer' in df.columns:
        stats['by_layer'] = df.groupby('layer')['score'].mean().to_dict()
    
    return stats

def get_score_event_count():
    if not os.path.exists(SCORE_LOG):
        return 0
    with open(SCORE_LOG) as f:
        return sum(1 for line in f if line.strip())

def get_family_count():
    if not os.path.exists(FAMILY_REGISTRY):
        return 0
    with open(FAMILY_REGISTRY) as f:
        data = json.load(f)
    return data.get('total_families', 0)

def build_report():
    stats = get_score_stats()
    events = get_score_event_count()
    families = get_family_count()
    week = time.strftime("%Y-%m-%d")
    
    lines = [
        f"Hive Memory Weekly Report — {week}",
        "=" * 45,
        "",
        f"Total memories: {stats.get('total', 0)}",
        f"Active families: {families}",
        f"Score events this week: {events}",
        "",
        "Score Distribution:",
        f"  Mean score: {stats.get('mean', 0):.3f}",
        f"  High performers (>=0.7): {stats.get('high', 0)}",
        f"  Mid range (0.3-0.7): {stats.get('mid', 0)}",
        f"  Low performers (<=0.3): {stats.get('low', 0)}",
        "",
    ]
    
    if 'by_layer' in stats:
        lines.append("Score by layer:")
        for layer, score in stats['by_layer'].items():
            lines.append(f"  {layer}: {score:.3f}")
        lines.append("")
    
    if 'top_memories' in stats:
        lines.append("Top 5 memories by score:")
        for i, m in enumerate(stats['top_memories'], 1):
            lines.append(f"  {i}. [{m['layer']}] score={m['score']:.3f}")
            lines.append(f"     {m['text'][:100]}...")
        lines.append("")
    
    lines.append("The hive is learning. Check hive/score_events.jsonl for full event history.")

    try:
        sys.path.insert(0, WORKSPACE + '/hive')
        from token_estimator import build_health_report
        hive_report = build_health_report()
        return "\n".join(lines) + "\n\n" + "=" * 45 + "\n\n" + hive_report
    except Exception as e:
        return "\n".join(lines) + f"\n\n[Health report unavailable: {e}]"

def main():
    report = build_report()
    print(report)
    
    # Email
    smtp_user, smtp_pass = get_smtp_creds()
    recipients = ["dvmcclung@me.com", "dmcclung@quantixscs.com"]
    
    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"Hive Memory Weekly Report — {time.strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(report, "plain"))
    
    with smtplib.SMTP("smtp.mail.me.com", 587) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.sendmail(smtp_user, recipients, msg.as_string())
    
    print("Weekly report emailed.")

if __name__ == "__main__":
    main()
