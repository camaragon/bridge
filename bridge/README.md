# Bridge Runtime Directory

Bounded inter-agent communication area for explicit, auditable handoffs.

## Purpose

The `./bridge` directory is Bridge's local runtime state, not its shared memory. It holds queue files, archives, and audit artifacts produced by the core scripts and API.

## Queue model

- sender creates a handoff once
- the same record is stored in sender outbox and recipient inbox
- audit log records the lifecycle
- closed items can be archived for review
- patrol/audit scripts scan the same bridge state

## Runtime layout

- `incoming/<agent-id>/`
- `outgoing/<agent-id>/`
- `archive/`
- `audit/`

Per-agent directories are created on demand. Bridge does **not** ship with fixed agent IDs.

## bridge_core

`./bridge_core` holds the shared Bridge logic:

- `models.py` — handoff schema and defaults
- `policy.py` — route, actor, and status safety rules
- `auth.py` — token loading and validation
- `file_repository.py` / `service.py` — durable storage and lifecycle actions
- `tooling.py` — helpers for patrol/audit views
- `runtime.py` — agent discovery, env parsing, and route/token config helpers

## Route safety guarantees

Route policy is config-driven:

- if `BRIDGE_ALLOWED_ROUTES` is unset, Bridge allows any sender -> recipient pair
- if `BRIDGE_ALLOWED_ROUTES` is set, Bridge only allows listed pairs
- enforcement happens in both `bridge_cli.py` and `bridge_api_server.py` through `bridge_core.policy.require_route()`

Example:

```bash
export BRIDGE_ALLOWED_ROUTES="planner:coder,coder:planner,planner:reviewer,reviewer:planner"
```

## Local API

Default server:

```bash
python3 ./scripts/bridge_api_server.py
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
- token file default: `./config/bridge_api.env`
- token keys are discovered dynamically from `BRIDGE_TOKEN_<AGENT_ID>` entries

Examples:

```env
BRIDGE_TOKEN_PLANNER=...
BRIDGE_TOKEN_CODER=...
BRIDGE_TOKEN_REVIEWER=...
```

### Local-only binding

The intended posture is loopback-only operation on `127.0.0.1`. Do not expose the Bridge API on a public or LAN interface unless the policy and auth model are deliberately redesigned.

## Wrapper behavior

Preferred wrapper pattern:

```bash
python3 ./scripts/bridge_agent.py --agent <agent-id> ...
```

Phase 3 behavior:

- wrappers attempt the local API first
- if the API is unavailable, they fail closed by default
- direct filesystem CLI fallback is opt-in only:
  - `--allow-cli-fallback`
  - `BRIDGE_WRAPPER_ALLOW_CLI_FALLBACK=1`
- prompt intake can be automated with `python3 ./scripts/bridge_intake_watch.py --agent <agent-id>`
- the same notify endpoint also receives lifecycle push payloads for `handoff_created`, `handoff_closed`, and `handoff_blocked`
- patrol can issue low-risk reminder/escalation nudges for unacknowledged or unresolved active handoffs by reusing those notify endpoints

## CLI helper usage

- `python3 ./scripts/bridge_cli.py create ...`
- `python3 ./scripts/bridge_cli.py list-open --agent <agent-id>`
- `python3 ./scripts/bridge_cli.py status --actor <agent-id> HND-...`
- `python3 ./scripts/bridge_cli.py set-status --actor <agent-id> HND-... in_progress`
- `python3 ./scripts/bridge_cli.py archive --actor <agent-id> HND-...`

## Common wrapper shortcuts

- `python3 ./scripts/bridge_agent.py --agent <agent-id> ack HND-...`
- `python3 ./scripts/bridge_agent.py --agent <agent-id> block HND-... --outcome "Need approval"`
- `python3 ./scripts/bridge_agent.py --agent <agent-id> close HND-... --outcome "Done"`

## Runbook

### Start server

```bash
python3 ./scripts/bridge_api_server.py
```

### Health check

```bash
curl http://127.0.0.1:8427/v1/health
```

### Rotate tokens

1. update `./config/bridge_api.env`
2. restart the API server
3. verify authenticated wrapper/API calls still work

### Recover if API is down

- check health
- restart the API server
- use explicit wrapper fallback only if needed for continuity
- remove fallback override after recovery

### Verify route policy

- run `python3 ./scripts/bridge_patrol.py --stuck-hours 24`
- if you configured `BRIDGE_ALLOWED_ROUTES`, confirm a disallowed pair still returns 403

### Reminder / escalation patrol

- defaults: active unresolved alert after 30 minutes, remind after 30 minutes, repeat every 2 hours, escalate after 6 hours, repeat every 24 hours
- state file: `bridge/audit/patrol-reminders.json`
- patrol targets unresolved active handoffs, not only fresh open/unacknowledged ones
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

Generated audit notes are deployment-local. Point them wherever your own environment expects them.

Commands that inspect or update an existing handoff remain scoped to the sender/recipient actor workflow.
