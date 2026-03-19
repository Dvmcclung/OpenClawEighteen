"""
Quick utility to log a quality observation.
Usage: python3 hive/log_observation.py <type> "<description>"
Types: proactive_hit | had_to_remind | correction
"""
import sys
import json
import time
import os

WORKSPACE = os.path.expanduser("~/.openclaw-eighteen/workspace")
OBS_FILE = os.path.join(WORKSPACE, "hive/quality_observations.jsonl")

def log_observation(obs_type, description, phase="post-hive"):
    obs = {
        "timestamp": time.time(),
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "type": obs_type,
        "description": description,
        "phase": phase
    }
    with open(OBS_FILE, 'a') as f:
        f.write(json.dumps(obs) + "\n")
    print(f"Logged: [{obs_type}] {description}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 log_observation.py <type> '<description>'")
        print("Types: proactive_hit | had_to_remind | correction")
        sys.exit(1)
    log_observation(sys.argv[1], sys.argv[2])
