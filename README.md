# Agent Shared

This directory is the **small shared layer** for Cameron's agent ecosystem.
It is **not** a shared memory pool.

## Purpose
- keep shared role/boundary facts in one place
- provide the bounded Bridge handoff surface for Hermes, Jarvy, and Jordan
- keep each private vault primary while allowing explicit, auditable coordination
- host `bridge_core`, the reusable package behind the CLI, wrappers, patrol, audit view, and local API server

## Layout
- `core/` — shared read-mostly policy/context notes
- `bridge/` — local runtime inbox/outbox queues, archive, and audit log (runtime data, not for git)
- `bridge_core/` — Phase 3 core package: models, policy, auth, repository, and service logic
- `scripts/` — API server, CLI, wrappers, patrol, and audit view generators
- `config/bridge_api.example.env` — example token and local API config
- `deploy/systemd/` — example service units for self-hosted deployments

## OSS repo boundaries
Track in git:
- `bridge_core/`
- `scripts/`
- `tests/`
- `config/bridge_api.example.env`
- `deploy/`
- this README and other docs

Keep local/private and out of git:
- `config/bridge_api.env`
- `config/*.bak-*`
- `bridge/incoming/`
- `bridge/outgoing/`
- `bridge/archive/`
- `bridge/audit/`
- caches like `__pycache__/` and `.pytest_cache/`

Bridge is intended to OSS cleanly with a hard separation:
- **Bridge core emits lifecycle events and stores handoff state**
- **agent-specific consumers decide how to surface those events**

## bridge_core
`bridge_core` is the single source of truth for Bridge behavior:
- route policy and actor access checks
- handoff/status validation
- token loading and auth helpers
- filesystem repository behavior
- audit/archive support used by all entrypoints

This keeps the direct CLI path and local API path aligned.

## Local API and wrappers
- Local API server: `python3 /home/caragon/agent-shared/scripts/bridge_api_server.py`
- Default bind: `127.0.0.1:8427`
- Health check: `curl http://127.0.0.1:8427/v1/health`
- Preferred wrappers:
  - Hermes: `python3 /home/caragon/agent-shared/scripts/bridge_hermes.py ...`
  - Jarvy: `python3 /home/caragon/agent-shared/scripts/bridge_jarvy.py ...`
  - Jordan: `python3 /home/caragon/agent-shared/scripts/bridge_jordan.py ...`
- Prompt intake helper: `python3 /home/caragon/agent-shared/scripts/bridge_intake_watch.py --agent hermes --once`

Wrapper behavior in Phase 3:
- wrappers try the local API first by default
- wrappers **do not** silently fall back to direct filesystem CLI mode
- explicit fallback knob:
  - one-shot: `--allow-cli-fallback`
  - env: `BRIDGE_WRAPPER_ALLOW_CLI_FALLBACK=1`

## Local API endpoints
- `GET /v1/health` — unauthenticated liveness check
- `POST /v1/handoffs` — create a handoff as the authenticated agent
- `GET /v1/handoffs` — list visible handoffs for the authenticated agent (`active_only=true` by default)
- `GET /v1/handoffs/{handoff_id}` — fetch one visible handoff
- `POST /v1/handoffs/{handoff_id}/ack`
- `POST /v1/handoffs/{handoff_id}/block`
- `POST /v1/handoffs/{handoff_id}/close`
- `POST /v1/handoffs/{handoff_id}/status`
- `POST /v1/handoffs/{handoff_id}/archive`

Acknowledgement semantics are intentionally additive:
- `status=acknowledged` means the recipient side has received the handoff into Bridge
- `acknowledgment_source=manual` means the recipient explicitly acked it
- `acknowledgment_source=auto` means an intake watcher/listener auto-acked it after Bridge delivery
- `acknowledged_at` records when that receipt acknowledgment happened
- auto-ack is *not* the same as human review or completion; use later status updates for that

Reminder/escalation remains patrol-based to avoid API churn:
- `bridge_patrol.py` can re-hit the existing recipient notify endpoint for *unacknowledged open* handoffs after a conservative delay
- reminder attempts are deduplicated in `bridge/audit/patrol-reminders.json`
- patrol escalates by surfacing warnings for handoffs that stay unacknowledged well past the reminder window
- defaults are conservative and additive: remind after 30 minutes, repeat every 2 hours, escalate after 6 hours, repeat every 24 hours

## Auth / token model
- wrappers and API clients send `Authorization: Bearer <token>`
- API also accepts `X-Bridge-Token: <token>` for simple local callers
- tokens are mapped per agent through `/home/caragon/agent-shared/config/bridge_api.env`
- env vars override file values: `BRIDGE_TOKEN_HERMES`, `BRIDGE_TOKEN_JARVY`, `BRIDGE_TOKEN_JORDAN`
- a token resolves to exactly one agent; missing/invalid tokens are rejected

Use `/home/caragon/agent-shared/config/bridge_api.example.env` as the template and keep real tokens out of version control.

## Route safety guarantees
Allowed by default:
- Hermes ↔ Jarvy
- Hermes ↔ Jordan

Denied by default:
- Jarvy ↔ Jordan
- Jordan ↔ Jarvy

The policy lives in `bridge_core/policy.py` and is enforced by both the CLI and API.

## Runbook
### Start the local API server
```bash
python3 /home/caragon/agent-shared/scripts/bridge_api_server.py
```
Optional overrides:
- `BRIDGE_API_HOST` (default `127.0.0.1`)
- `BRIDGE_API_PORT` (default `8427`)
- `BRIDGE_ROOT`
- `BRIDGE_API_CONFIG`

### Health check
```bash
curl http://127.0.0.1:8427/v1/health
```
Expected: `{"ok": true, "service": "bridge-api"}`

### Rotate tokens
1. Edit `/home/caragon/agent-shared/config/bridge_api.env`
2. Replace one or more `BRIDGE_TOKEN_*` values with new secrets
3. Restart `bridge_api_server.py`
4. Update any wrapper callers that were using exported env tokens
5. Confirm with health + an authenticated list call

### Recover if API is down
1. Check health: `curl http://127.0.0.1:8427/v1/health`
2. If unreachable, restart `python3 /home/caragon/agent-shared/scripts/bridge_api_server.py`
3. If work must continue before restart is fixed, use explicit CLI fallback:
   - `python3 /home/caragon/agent-shared/scripts/bridge_hermes.py --allow-cli-fallback ...`
   - or `BRIDGE_WRAPPER_ALLOW_CLI_FALLBACK=1 ...`
4. Remove the fallback override once the API is healthy again

### Verify no forbidden route was opened
```bash
python3 /home/caragon/agent-shared/scripts/bridge_patrol.py --stuck-hours 24
```
Also perform a denied-route smoke test against the API: attempt Jarvy → Jordan creation and expect HTTP 403 with `route not allowed by default`.

### Enable reminder/escalation patrol
Recommended low-risk wiring is to keep using the existing patrol/watch path:
1. Keep each recipient `bridge_intake_watch.py --listen` endpoint running and mapped through `BRIDGE_NOTIFY_URL_*`
2. Run patrol on a schedule, for example every 15 minutes:
   ```bash
   python3 /home/caragon/agent-shared/scripts/bridge_patrol.py --stuck-hours 24
   ```
3. Patrol now has a built-in active unresolved handoff alert:
   - default threshold: `0.5h`
   - triggers when a handoff is still active and its `resolution_summary` is only placeholder/admin text like `pending` or `actively investigating`
   - actionable summaries with root cause / stable target / retry guidance do not trigger this warning
4. Override thresholds only if needed:
   - `--active-alert-hours`
   - `--reminder-after-hours`
   - `--reminder-repeat-hours`
   - `--escalate-after-hours`
   - `--escalate-repeat-hours`
   - or env vars `BRIDGE_PATROL_ACTIVE_ALERT_HOURS`, `BRIDGE_PATROL_REMINDER_AFTER_HOURS`, `BRIDGE_PATROL_REMINDER_REPEAT_HOURS`, `BRIDGE_PATROL_ESCALATE_AFTER_HOURS`, `BRIDGE_PATROL_ESCALATE_REPEAT_HOURS`
5. Inspect `bridge/audit/patrol-reminders.json` if you need to confirm when reminders/escalations were last emitted

## Private Vaults Stay Primary
- Hermes private vault: `/home/caragon/hermes`
- Jordan private vault: `/home/caragon/jordan`
- Jarvy private vault: `/home/caragon/jarvy/obsidian`

## Hard Rule
No agent should treat this directory as a dump of private vault memory.
Only put shared policy, bridge metadata, and explicit handoff payloads here.
