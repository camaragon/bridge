# Architecture

## Design goal
Bridge provides explicit, auditable handoffs between autonomous agents without shared long-term memory.

## Main components
- `bridge_core/` — Python package with models, policy, auth, repository, runtime helpers, and service logic
- `scripts/bridge_api_server.py` — local HTTP API for wrappers and automation
- `scripts/bridge_cli.py` — direct filesystem/API CLI for manual operations and testing
- `scripts/bridge_agent.py` — generic per-agent wrapper interface
- `scripts/bridge_intake_watch.py` — recipient listener and read-only lifecycle consumer entrypoint
- `scripts/bridge_patrol.py` — unresolved handoff detection and reminder tooling

## Data flow
1. sender creates handoff through CLI or API-backed wrapper
2. Bridge validates auth, route policy, and status rules
3. Bridge v2 atomically writes a canonical record, then recoverable sender/recipient queue views
4. Bridge durably enqueues a versioned lifecycle event with at-least-once delivery semantics
5. optional recipient notify URL receives an immediate compatibility fast path
6. recipient acknowledges, progresses, blocks, closes, or archives; sender may explicitly cancel/withdraw
7. outbox delivery retries consumer outages with stable event IDs and dead-letter evidence

## Boundaries
- Bridge ships with no built-in agent IDs
- route policy comes from config, not code constants
- lifecycle consumers are adapters outside core logic
- runtime state stays under `BRIDGE_ROOT`, outside tracked source
- agent-private memory stays outside Bridge entirely
- durable work state and worker dispatch stay in external work queues, not Bridge

## Reliability v2

See [`bridge-v2-reliability.md`](bridge-v2-reliability.md) for the canonical-record transaction model, optimistic revisions, lifecycle authorization matrix, durable outbox, v1 compatibility, adapter contract, migration, deployment, and rollback procedures.

## Why package name stays `bridge_core/`
`bridge_core/` is importable Python package name used across scripts and tests. Renaming it to bare `src/` would not be a like-for-like cleanup; `src/` is usually container directory for package layout, not package name itself.

If a future packaging cleanup is desired, correct shape would be one of:
- keep current flat layout: `bridge_core/`
- adopt src-layout: `src/bridge_core/`
- rename package semantically: `src/bridge/`

Plain `src/` alone would make imports and packaging less clear, not more.
