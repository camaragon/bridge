# Bridge v2 reliability foundation

## Scope and non-goals

Bridge v2 is a backward-compatible reliability layer for explicit, scoped, redacted agent handoffs. It is an at-least-once event bus with idempotent producers and consumers. It does **not** provide exactly-once delivery, worker dispatch, shared memory, or durable work-state ownership. External work queues remain independent systems.

Bridge core stays generic: `bridge_core/` imports no Hermes, Kanban, profile, vault, or messaging-platform code.

## Transaction and recovery model

### Canonical state and queue views

A v2 handoff has one canonical Markdown record at:

```text
records/<handoff_id>.md
```

The existing files remain as backward-compatible queue views:

```text
outgoing/<sender>/<handoff_id>.md
incoming/<recipient>/<handoff_id>.md
```

The canonical record is authoritative. Record writes use a same-directory temporary file, flush, file `fsync`, mode `0600`, atomic rename, and parent-directory `fsync`. Creation and lifecycle mutation use an exclusive per-handoff `flock` plus an in-process reentrant lock.

A create writes canonical state before queue views. If the process stops after one view, `reconcile` rebuilds the missing view from canonical evidence. A conflicting view is copied to `audit/conflicts/`, an audit event is appended, and only then may the view be replaced. Ambiguous v1 conflicts without canonical evidence are never auto-selected.

Two byte-identical v1 queue copies without a canonical record are unambiguous legacy state, not divergence. Check-only reconciliation leaves them quiet. Repair mode materializes the v2 canonical record and rewrites both queue views to the canonical v2 representation without creating false conflict evidence. Non-identical v1 copies remain an operator decision.

### Idempotent create

Clients may send `Idempotency-Key` or `idempotency_key`. The key is scoped to the authenticated sender. Bridge stores only its SHA-256 digest in `idempotency/<sender>/`; it stores a canonical request SHA-256 over material request fields. The raw key is not written.

- same sender + key + canonical payload: return the original handoff;
- same sender + key + materially different payload: conflict (`HTTP 409`);
- concurrent identical requests: serialized by the sender/key lock and produce one handoff.

Canonical state is written before its idempotency index. If a process stops in that interval, a retry under the same sender/key lock scans canonical records for the stored key digest, validates the canonical request hash, rebuilds the missing index, repairs queue views, and returns the original handoff. Multiple canonical claims for one sender/key digest fail closed as a repository conflict.

### Lifecycle transactions and outbox

Before a lifecycle state commit, Bridge writes a prepared transaction journal containing the immutable event envelope. It then:

1. atomically commits canonical handoff state and queue views;
2. atomically writes the outbox event;
3. fsync-appends audit evidence;
4. removes the prepared transaction.

If a process stops after state commit but before outbox enqueue, `outbox-recover` compares the prepared event with canonical revision/status and enqueues the same `event_id`. Consumer failure never rolls back handoff state.

Automatic recovery deliberately leaves a prepared event untouched when canonical state has advanced or disappeared. An operator must inspect the transaction, canonical audit history, and downstream consumer state, then explicitly resolve it with `outbox-resolve-transaction`: `enqueue` preserves at-least-once historical delivery with the original `event_id`; `discard` is only for proven duplicate/invalid transactions. Both actions require a reason and preserve resolution evidence under `audit/resolved-transactions/` before removing the journal.

Events live at `outbox/events/<event_id>.json`. State transitions are `pending` → `retrying` → `delivered` or `dead_letter`. Backoff is bounded exponential delay plus jitter. Manual replay resets attempts and scheduling but retains `event_id`.

Immediate notify remains a compatibility fast path. Its body preserves the v1 create/block/close shape and carries `X-Bridge-Event-ID`. Durable `outbox-deliver` sends the full v2 event body and the same header.

## Record contract

New records use:

- `schema_version: 2`
- `revision: 0` at create, incremented once per lifecycle mutation
- optional `correlation_id`
- optional `causation_id`
- `canonical_request_hash`
- `idempotency_key_hash` when create idempotency is used

Missing v2 fields mean a v1 record. A v1 record loads as `schema_version=1`, `revision=0` without an eager migration. Its first successful lifecycle mutation writes v2 canonical state and v2 queue views at revision 1.

## Lifecycle event contract

Every durable lifecycle event has:

- `schema_version`
- unique stable `event_id`
- `handoff_id`
- `event_type`
- `revision`
- `occurred_at`
- `target_agent`, `sender`, `recipient`, `actor`
- `status`, `subject`, `resolution_summary`
- optional `correlation_id`, `causation_id`
- optional generic `external_correlation` mapping
- `attempts`, `next_attempt_at`, `last_error`, `delivery_state`

Lifecycle event types are:

- `handoff_created`
- `handoff_acknowledged`
- `handoff_in_progress`
- `handoff_blocked`
- `handoff_closed`
- `handoff_cancelled`
- `handoff_archived`

`retrying` and `dead_letter` are durable delivery states for the same immutable event. Diagnostics/adapters may surface these as `handoff_retrying` and `handoff_dead_lettered` operational notifications without creating a second recursively delivered outbox item.

## Optimistic concurrency

API callers may send `expected_revision` or `If-Match: "<revision>"`. CLI/wrapper callers use `--expected-revision`. A stale update returns `HTTP 409 revision_conflict`.

A repeated same-state request is a no-op when its outcome is empty or exactly matches the stored outcome. It does not increment revision or create another event. Authorization is checked before this no-op rule.

## Lifecycle authorization matrix

| Action | Sender | Recipient | Host maintenance |
|---|---:|---:|---:|
| create | yes | no | explicit tooling only |
| acknowledge | no | yes | recovery override |
| in progress | no | yes | recovery override |
| block | no | yes | recovery override |
| close with verified outcome | no | yes | recovery override |
| cancel/withdraw with reason | yes | no | recovery override |
| archive closed or sender-cancelled handoff | no | yes | recovery override |

The API exposes no maintenance override. The service-layer `maintenance=True` path is deliberately explicit for offline host recovery tooling and must not be granted through participant tokens.

## Generic work-queue adapter contract

An external adapter consumes full v2 outbox events. It must persist processed `event_id` values and maintain a mapping keyed by `handoff_id`. Duplicate `event_id` is a no-op.

Recommended reconciliation:

- `handoff_acknowledged`: create or find the durable work item; persist its external ID in adapter-owned state.
- `handoff_in_progress`: update progress evidence, without making Bridge a dispatcher.
- `handoff_blocked`: update the existing work item with blocker evidence.
- `handoff_closed`: close/update the existing work item only after the event contains a verified outcome acceptable to local policy.
- `handoff_cancelled`: mark the work item withdrawn/cancelled under local policy.
- delivery failure: leave the event retrying; exhausted attempts provide dead-letter evidence.
- duplicate event: no-op and return success.

An adapter may store a Kanban card ID in its own mapping or in generic `external_correlation` metadata. Bridge core neither imports nor calls Kanban.

## Commands

```bash
python3 scripts/bridge_cli.py outbox-list --state pending
python3 scripts/bridge_cli.py outbox-list --state dead_letter
python3 scripts/bridge_cli.py outbox-recover
python3 scripts/bridge_cli.py outbox-deliver --limit 100
python3 scripts/bridge_cli.py outbox-replay EVT-...
python3 scripts/bridge_cli.py outbox-resolve-transaction EVT-... --action enqueue --reason "verified missing delivery"
python3 scripts/bridge_cli.py outbox-resolve-transaction EVT-... --action discard --reason "verified duplicate event"
python3 scripts/bridge_cli.py reconcile --check-only
python3 scripts/bridge_cli.py reconcile HND-...
```

`reconcile --check-only` is read-only. Repair mode restores only from unambiguous canonical evidence; conflict evidence is preserved before replacement.

## Backward-compatible migration plan

1. Deploy code without changing live data.
2. Run compile/tests and `reconcile --check-only` against a copied Bridge root. Identical v1 mirror pairs should not be reported as divergent.
3. Back up the live Bridge root with metadata and verify the backup can be listed/read.
4. Stop API/intake writers during the separately approved maintenance window.
5. Run `outbox-recover`, then `reconcile --check-only` on live data.
6. Resolve ambiguous divergent v1 copies manually; do not select by timestamp alone.
7. Run `reconcile` to lazily materialize canonical records and missing views. Verify identical v1 pairs migrate without conflict evidence.
8. Start the API, then run one bounded `outbox-deliver` pass.
9. Verify health, patrol, route/token authorization, open handoff counts, outbox/dead-letter counts, and one isolated synthetic route approved for the maintenance window.
10. Add one separately approved periodic invocation of `outbox-recover` + `outbox-deliver`; do not treat the API fast path as the retry scheduler. Enforce a single active deliverer with the service manager or an external lock.

No bulk rewrite is required. Untouched v1 handoffs continue to load.

## Live deployment plan (separate approval required)

Deployment mutates production code/data and requires a doctor rundown plus explicit Cameron approval. The approved window should include: immutable backup path, exact release artifact, writer quiescence, pre/post patrol captures, service-specific restart scope, and rollback trigger.

Do not modify token values or `config/bridge_api.env`. Existing route/token configuration remains valid. Optional retry settings are additive environment values:

- `BRIDGE_OUTBOX_MAX_ATTEMPTS`
- `BRIDGE_OUTBOX_BASE_DELAY_SECONDS`
- `BRIDGE_OUTBOX_MAX_DELAY_SECONDS`
- `BRIDGE_PATROL_OUTBOX_PENDING_SECONDS`

Adding those values or a periodic scheduler is a separate config/service approval.

## Rollback

1. Stop only Bridge API/intake/outbox writers in the approved window.
2. Preserve the current v2 Bridge root as forensic evidence; do not delete it.
3. Restore the pre-deployment Bridge-root backup atomically, including modes/ownership.
4. Restore the prior code release/symlink or package artifact.
5. Restore the prior service definitions only if they changed.
6. Start only the previously running Bridge units.
7. Verify local API health, route/token authorization, open handoff counts, and patrol.
8. Keep v2 outbox/transaction artifacts from the failed attempt for manual reconciliation; do not replay them into v1 automatically.

Rollback trigger: canonical/queue divergence that cannot be reconciled unambiguously, authorization regression, sustained API errors, or outbox retries causing consumer harm.

## Remaining operational risks

- Filesystem atomic rename and `flock` assume a local POSIX filesystem with correct `fsync` behavior; network filesystems need separate validation.
- The API fast path is not a scheduler. Reliable retries require the separately deployed periodic `outbox-deliver` invocation.
- Legacy notify bodies are retained for compatibility; new adapters should consume full v2 outbox events.
- Manual edits to queue views become conflicts against canonical state and are preserved as evidence, not merged automatically.
- Event deduplication is an adapter responsibility keyed by `event_id`; Bridge guarantees at-least-once, not exactly-once.
- Outbox event files are not claimed or leased. Running multiple deliverers concurrently can duplicate delivery and race delivery-state updates; deploy exactly one deliverer until a claim/lease protocol is added.
