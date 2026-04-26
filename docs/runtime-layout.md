# Runtime Layout

Bridge runtime state is deployment-local and created at runtime. It is **not** part of the OSS source tree.

## Runtime directories

Bridge uses these directories under `BRIDGE_ROOT`:

- `incoming/<agent-id>/`
- `outgoing/<agent-id>/`
- `archive/`
- `audit/`

Per-agent directories are created on demand. Bridge ships with no built-in agent IDs.

## What lives there

- sender outbox copies
- recipient inbox copies
- archived closed handoffs
- audit logs and patrol reminder state

Typical examples:

- `bridge/audit/handoff-log.md`
- `bridge/audit/patrol-reminders.json`
- `bridge/archive/HND-.../`

## Important boundary

Keep runtime state out of git.

Bridge source code lives in:

- `bridge_core/` — core models, policy, auth, storage, and service logic
- `scripts/` — CLI, API server, patrol, intake watcher, wrappers, and helpers
- `tests/` — regression and integration coverage
- `config/` — example configuration only
- `deploy/` — deployment templates

Runtime state should be created by the deployment, not tracked in the repository.
