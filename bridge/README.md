# Agent Bridge

Bounded inter-agent communication area for explicit, auditable handoffs.

## Purpose
The Bridge provides a **minimal shared transport**, not pooled memory. Phase 3 adds a reusable core package plus a local localhost-only API so wrappers can use the same policy and storage behavior without broadening access.

## Queue model
- sender creates a handoff once
- the same record is stored in sender outbox and recipient inbox
- audit log records the lifecycle
- closed items can be archived for review
- patrol/audit scripts scan the same bridge state

## Queues
- `incoming/hermes`
- `incoming/jarvy`
- `incoming/jordan`
- `outgoing/hermes`
- `outgoing/jarvy`
- `outgoing/jordan`
- `archive/`
- `audit/`

## bridge_core
`/home/caragon/agent-shared/bridge_core` holds the shared Bridge logic:
- `models.py` — handoff schema and defaults
- `policy.py` — route, actor, and status safety rules
- `auth.py` — token loading and validation
- `file_repository.py` / `service.py` — durable storage and lifecycle actions
- `tooling.py` — helpers for patrol/audit views

## Route safety guarantees
Allowed by default:
- Hermes → Jarvy
- Jarvy → Hermes
- Hermes → Jordan
- Jordan → Hermes

Denied by default:
- Jarvy → Jordan
- Jordan → Jarvy

This is enforced in both `bridge_cli.py` and `bridge_api_server.py` through `bridge_core.policy.require_route()`.

## Local API
Default server:
```bash
python3 /home/caragon/agent-shared/scripts/bridge_api_server.py
```
Default bind: `127.0.0.1:8427`

Endpoints:
- `GET /v1/health`
- `POST /v1/handoffs`
- `GET /v1/handoffs`
- `GET /v1/handoffs/{handoff_id}`
- `POST /v1/handoffs/{handoff_id}/ack`
- `POST /v1/handoffs/{handoff_id}/block`
- `POST /v1/handoffs/{handoff_id}/close`
- `POST /v1/handoffs/{handoff_id}/status`
- `POST /v1/handoffs/{handoff_id}/archive`

### Auth / token model
- authenticated by per-agent tokens
- accepted headers:
  - `Authorization: Bearer <token>`
  - `X-Bridge-Token: <token>`
- token file default: `/home/caragon/agent-shared/config/bridge_api.env`
- env overrides: `BRIDGE_TOKEN_HERMES`, `BRIDGE_TOKEN_JARVY`, `BRIDGE_TOKEN_JORDAN`

### Local-only binding
The intended posture is loopback-only operation on `127.0.0.1`. Do not expose the Bridge API on a public or LAN interface unless the policy and auth model are deliberately redesigned.

## Wrapper behavior
Preferred wrappers:
- Hermes: `python3 /home/caragon/agent-shared/scripts/bridge_hermes.py ...`
- Jarvy: `python3 /home/caragon/agent-shared/scripts/bridge_jarvy.py ...`
- Jordan: `python3 /home/caragon/agent-shared/scripts/bridge_jordan.py ...`

Phase 3 behavior:
- wrappers attempt the local API first
- if the API is unavailable, they fail closed by default
- direct filesystem CLI fallback is opt-in only:
  - `--allow-cli-fallback`
  - `BRIDGE_WRAPPER_ALLOW_CLI_FALLBACK=1`
- prompt intake can be automated with `python3 /home/caragon/agent-shared/scripts/bridge_intake_watch.py --agent hermes`
- the same notify endpoint now also receives lifecycle push payloads for `handoff_created`, `handoff_closed`, and `handoff_blocked`
- patrol can issue low-risk reminder/escalation nudges for unacknowledged open handoffs by reusing those same notify endpoints

## CLI helper usage
- `python3 /home/caragon/agent-shared/scripts/bridge_cli.py create ...`
- `python3 /home/caragon/agent-shared/scripts/bridge_cli.py list-open --agent hermes`
- `python3 /home/caragon/agent-shared/scripts/bridge_cli.py status --actor jordan HND-...`
- `python3 /home/caragon/agent-shared/scripts/bridge_cli.py set-status --actor hermes HND-... in_progress`
- `python3 /home/caragon/agent-shared/scripts/bridge_cli.py archive --actor jordan HND-...`

## Common wrapper shortcuts
- `python3 /home/caragon/agent-shared/scripts/bridge_hermes.py ack HND-...`
- `python3 /home/caragon/agent-shared/scripts/bridge_hermes.py block HND-... --outcome "Need approval"`
- `python3 /home/caragon/agent-shared/scripts/bridge_hermes.py close HND-... --outcome "Done"`
- same shortcut pattern works for `bridge_jarvy.py` and `bridge_jordan.py`

## Runbook
### Start server
```bash
python3 /home/caragon/agent-shared/scripts/bridge_api_server.py
```

### Health check
```bash
curl http://127.0.0.1:8427/v1/health
```

### Rotate tokens
1. update `/home/caragon/agent-shared/config/bridge_api.env`
2. restart the API server
3. verify authenticated wrapper/API calls still work

### Recover if API is down
- check health
- restart the API server
- use explicit wrapper fallback only if needed for continuity
- remove fallback override after recovery

### Verify no forbidden route opened
- run `python3 /home/caragon/agent-shared/scripts/bridge_patrol.py --stuck-hours 24`
- confirm denied Jarvy ↔ Jordan API creation still returns 403

### Reminder / escalation patrol
- defaults: active unresolved alert after 30 minutes, remind after 30 minutes, repeat every 2 hours, escalate after 6 hours, repeat every 24 hours
- state file: `bridge/audit/patrol-reminders.json`
- patrol now targets unresolved active handoffs, not only fresh open/unacknowledged ones
- active unresolved alert fires when a handoff stays active past threshold and still only has placeholder/admin summary text such as `pending` or `actively investigating`
- actionable summaries suppress the active unresolved alert
- useful overrides:
  - `--active-alert-hours`
  - `--reminder-after-hours`
  - `--reminder-repeat-hours`
  - `--escalate-after-hours`
  - `--escalate-repeat-hours`
  - env: `BRIDGE_PATROL_ACTIVE_ALERT_HOURS`, `BRIDGE_PATROL_REMINDER_AFTER_HOURS`, `BRIDGE_PATROL_REMINDER_REPEAT_HOURS`, `BRIDGE_PATROL_ESCALATE_AFTER_HOURS`, `BRIDGE_PATROL_ESCALATE_REPEAT_HOURS`

## Human audit notes
- `[[/home/caragon/hermes/System/Bridge Audit View.md|Bridge Audit View]]`
- `[[/home/caragon/hermes/System/Bridge Archive Index.md|Bridge Archive Index]]`

Commands that inspect or update an existing handoff remain scoped to the sender/recipient actor workflow.
