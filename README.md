# Bridge

Bridge is an event-driven handoff bus for autonomous agents.

It gives you a bounded place to create, receive, track, and close agent-to-agent work without turning every agent vault into a shared memory swamp.

## What it does

- creates explicit handoff records between agents
- stores sender and recipient copies on disk for auditability
- enforces route policy and status transitions
- exposes a local HTTP API for wrappers and automation
- supports immediate lifecycle notifications (`create`, `close`, `block`)
- supports read-only event consumers for surfacing lifecycle events into chat/UI adapters
- includes patrol tooling for reminders and unresolved-handoff detection

## Configuration model

Bridge ships with **no built-in agent IDs** and **no default route graph**.

You choose your own agent names. Examples in this README use placeholders like `agent-a` and `agent-b`, but those are only sample values.

Route policy is optional:

- if `BRIDGE_ALLOWED_ROUTES` is unset, Bridge allows any sender -> recipient pair
- if `BRIDGE_ALLOWED_ROUTES` is set, Bridge only allows the configured pairs
- API auth tokens are discovered from `BRIDGE_TOKEN_<AGENT_ID>` entries, so your deployed agent IDs come from config, not from the codebase

You do **not** need to edit `bridge_core/models.py` or `bridge_core/policy.py` to adopt your own ecosystem.

For deeper docs, start with `docs/README.md`.

## Repository layout

- `bridge_core/` - core models, policy, auth, repository, and service logic
- `scripts/` - CLI, wrappers, API server, patrol tooling, intake watcher, and token rotation
- `tests/` - pytest coverage for core flows, API, wrappers, patrol, and intake watchers
- `config/bridge_api.example.env` - example config for API tokens and notify hooks
- `deploy/systemd/` - generic systemd templates for self-hosted deployment
- `docs/` - architecture, deployment, and runtime documentation
- `examples/handoffs/` - example markdown payloads for common handoff patterns

## Quick start

### 1) Clone the repo

```bash
git clone https://github.com/camaragon/bridge.git
cd bridge
```

### 2) Confirm Python

Bridge uses the Python standard library at runtime.

```bash
python3 --version
```

Expected: Python 3.10+

### 3) Run tests

```bash
python3 -m pytest -q
```

### 4) Create a handoff with the direct CLI

This is the fastest way to see Bridge working.

```bash
python3 scripts/bridge_cli.py create \
  --sender agent-a \
  --recipient agent-b \
  --issue-type task \
  --subject "Demo handoff" \
  --requested-action "Inspect the repo and report back" \
  --minimal-context "Started from the README quickstart"
```

You should get JSON like:

```json
{
  "handoff_id": "HND-20260426-000000-abcd",
  "outbox": ".../bridge/outgoing/agent-a/HND-...md",
  "inbox": ".../bridge/incoming/agent-b/HND-...md"
}
```

List open handoffs for the recipient:

```bash
python3 scripts/bridge_cli.py list-open --agent agent-b
```

Mark it closed:

```bash
python3 scripts/bridge_cli.py set-status --actor agent-b HND-... closed --outcome "Completed and reported back"
```

Archive it:

```bash
python3 scripts/bridge_cli.py archive --actor agent-b HND-...
```

## HTTP API quick start

The wrappers prefer the local API.

### 1) Create a local config file

```bash
mkdir -p config
cp config/bridge_api.example.env config/bridge_api.env
mkdir -p bridge/incoming bridge/outgoing bridge/archive bridge/audit
```

Edit `config/bridge_api.env` and replace:

- `BRIDGE_TOKEN_AGENT_A`
- `BRIDGE_TOKEN_AGENT_B`
- `BRIDGE_TOKEN_AGENT_C`

with real secret values.

You usually do **not** need to change `BRIDGE_ROOT` or `BRIDGE_API_CONFIG` if you are running from this repo checkout.

### 2) Start the API server

```bash
python3 scripts/bridge_api_server.py
```

By default it listens on `127.0.0.1:8427`.

### 3) Check health

```bash
curl http://127.0.0.1:8427/v1/health
```

Expected:

```json
{"ok": true, "service": "bridge-api"}
```

### 4) Use a wrapper

```bash
python3 scripts/bridge_agent.py --agent agent-a create \
  --recipient agent-b \
  --issue-type task \
  --subject "Wrapper demo" \
  --requested-action "Verify API-backed handoff creation" \
  --minimal-context "Started from the README API quickstart"
```

List open handoffs:

```bash
python3 scripts/bridge_agent.py --agent agent-b list-open
```

Acknowledge receipt:

```bash
python3 scripts/bridge_agent.py --agent agent-b ack HND-...
```

Close with a resolution summary:

```bash
python3 scripts/bridge_agent.py --agent agent-b close HND-... --outcome "Verified and complete"
```

## API endpoints

- `GET /v1/health`
- `POST /v1/handoffs`
- `GET /v1/handoffs`
- `GET /v1/handoffs/{handoff_id}`
- `POST /v1/handoffs/{handoff_id}/ack`
- `POST /v1/handoffs/{handoff_id}/block`
- `POST /v1/handoffs/{handoff_id}/close`
- `POST /v1/handoffs/{handoff_id}/status`
- `POST /v1/handoffs/{handoff_id}/archive`

## Lifecycle notifications

Bridge supports two layers of notification behavior:

1. **recipient notify URLs**
   - configured with `BRIDGE_NOTIFY_URL_<AGENT>`
   - Bridge POSTs lifecycle events to recipient listeners

2. **read-only lifecycle event consumers**
   - configured with `BRIDGE_NOTIFY_EVENT_COMMAND_<AGENT>`
   - `bridge_intake_watch.py` can execute a command for `handoff_closed` and `handoff_blocked`
   - the command receives raw JSON on stdin
   - this keeps Bridge core separate from Telegram, BlueBubbles, Slack, or other UI-specific adapters

Run a recipient listener:

```bash
python3 scripts/bridge_intake_watch.py --agent agent-b --listen --port 8522
```

Run a one-shot inbox check:

```bash
python3 scripts/bridge_intake_watch.py --agent agent-b --once
```

## Patrol and unresolved handoff detection

Bridge includes patrol tooling for follow-up pressure without mutating the API surface.

See `docs/runtime-layout.md` for runtime directory expectations.

Run patrol manually:

```bash
python3 scripts/bridge_patrol.py --stuck-hours 24
```

Patrol can:

- detect unacknowledged open handoffs
- re-hit notify endpoints after a delay
- emit active unresolved alerts for placeholder summaries like `pending`
- deduplicate reminders through `bridge/audit/patrol-reminders.json`

## Configuration

Main runtime variables:

- `BRIDGE_PROJECT_ROOT`
- `BRIDGE_ROOT`
- `BRIDGE_API_CONFIG`
- `BRIDGE_API_HOST`
- `BRIDGE_API_PORT`
- `BRIDGE_TOKEN_AGENT_A`
- `BRIDGE_TOKEN_AGENT_B`
- `BRIDGE_TOKEN_AGENT_C`
- `BRIDGE_NOTIFY_URL_<AGENT_ID>`
- `BRIDGE_NOTIFY_EVENT_COMMAND_<AGENT_ID>`
- `BRIDGE_ALLOWED_ROUTES`

## Safety boundary

Bridge is intentionally split like this:

- **Bridge core** owns handoff state, auditability, and lifecycle events
- **adapters/listeners** decide how to surface those events to humans or agent runtimes

That keeps the core OSS-friendly and prevents messaging-platform logic from being baked into the handoff engine.

## Deployment

Generic systemd templates live in:

- `deploy/systemd/bridge-api.service`
- `deploy/systemd/bridge-intake-watch@.service`
- `deploy/systemd/example-agent.env`

These are templates for self-hosted deployments, not host-specific copies.

## Development

Run the full test suite:

```bash
pytest -q
```

Compile-check scripts and tests:

```bash
python3 -m py_compile bridge_core/*.py scripts/*.py tests/*.py
```

## License

Apache-2.0
