# HEARTBEAT.md — Eighteen Periodic Tasks

## Unassigned Instance Check

Before doing anything else, check if this instance has been assigned a role:
- Read USER.md — if it says "no operator assigned" or is the default template, this instance is dormant
- If dormant: reply HEARTBEAT_OK immediately. Do not run any other steps.
- If a role/operator has been assigned: continue with the rest of this file.

## Daily (first heartbeat)
1. **Check hive inbox** (messages from other agents):
   ```bash
   python3 /home/qtxit/.openclaw/shared/hive_messaging_v2.py check eighteen
   ```

2. **Memory health check** — verify shared hive is accessible:
   `python3 /home/qtxit/.openclaw-eighteen/workspace/hive/surface_on_demand.py "heartbeat health check"`

3. **Facts pending review** — check `system/facts_pending_review.md` for unflagged daily memory files, extract key facts, write to shared LanceDB via hive_write.py.

4. Check `system/agent_activity.log` for anomalies.

5. If a role has been assigned, follow role-specific HEARTBEAT instructions.

## If nothing needs attention
Reply: HEARTBEAT_OK
