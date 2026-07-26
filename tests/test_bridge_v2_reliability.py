from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json

import pytest

from bridge_core.file_repository import FileBridgeRepository
from bridge_core.frontmatter import dump_document, parse_frontmatter
from bridge_core.models import CreateHandoffInput, HandoffRecord
from bridge_core.policy import AccessPolicyError
from bridge_core.repository import IdempotencyConflictError, RepositoryConflictError, RevisionConflictError
from bridge_core.service import BridgeService


def _request(*, key: str = "create-1", subject: str = "Bridge v2") -> CreateHandoffInput:
    return CreateHandoffInput(
        sender="agent-a",
        recipient="agent-b",
        issue_type="task",
        subject=subject,
        requested_action="Perform the bounded work.",
        minimal_context="Reliability regression test.",
        idempotency_key=key,
        correlation_id="CORR-1",
        causation_id="CAUSE-1",
    )


def _service(tmp_path: Path, **repo_kwargs) -> tuple[BridgeService, FileBridgeRepository]:
    repository = FileBridgeRepository(tmp_path / "bridge", **repo_kwargs)
    return BridgeService(repository), repository


def _write_v1_handoff(root: Path, *, handoff_id: str = "HND-V1-0001") -> HandoffRecord:
    record = HandoffRecord(
        handoff_id=handoff_id,
        status="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        acknowledged_at="none",
        acknowledgment_source="none",
        sender="agent-a",
        recipient="agent-b",
        issue_type="task",
        subject="Legacy handoff",
        body="## Outcome\n- pending\n",
    )
    frontmatter = record.to_frontmatter()
    for key in (
        "schema_version",
        "revision",
        "correlation_id",
        "causation_id",
        "canonical_request_hash",
        "idempotency_key_hash",
    ):
        frontmatter.pop(key)
    document = dump_document(frontmatter, record.body)
    for path in (
        root / "outgoing" / "agent-a" / f"{handoff_id}.md",
        root / "incoming" / "agent-b" / f"{handoff_id}.md",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8")
    return record


def test_concurrent_identical_create_requests_produce_one_handoff(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(lambda _index: service.create_handoff(_request()), range(16)))

    assert len({record.handoff_id for record in records}) == 1
    assert len(list(repository.records_root.glob("*.md"))) == 1
    assert len(repository.list_events()) == 1


def test_same_idempotency_key_with_different_payload_conflicts(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    service.create_handoff(_request())

    with pytest.raises(IdempotencyConflictError, match="different request payload"):
        service.create_handoff(_request(subject="Materially different"))
    assert list(repository.transactions_root.glob("*.json")) == []


def test_crash_after_first_mirror_write_is_recoverable(tmp_path: Path) -> None:
    def fail_after_outgoing(stage: str) -> None:
        if stage == "after_outgoing_write":
            raise RuntimeError("simulated crash")

    root = tmp_path / "bridge"
    crashing_service = BridgeService(FileBridgeRepository(root, fault_injector=fail_after_outgoing))
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashing_service.create_handoff(_request())

    repository = FileBridgeRepository(root)
    recovered = BridgeService(repository).create_handoff(_request())
    outgoing = root / "outgoing" / "agent-a" / f"{recovered.handoff_id}.md"
    incoming = root / "incoming" / "agent-b" / f"{recovered.handoff_id}.md"
    assert outgoing.exists() and incoming.exists()
    recovered_event_ids = repository.recover_transactions()
    assert len(recovered_event_ids) == 1
    assert repository.list_events()[0].event_id == recovered_event_ids[0]


def test_retry_rebuilds_missing_idempotency_index_after_crash(tmp_path: Path, monkeypatch) -> None:
    service, repository = _service(tmp_path)
    original_atomic_write_json = repository._atomic_write_json
    failed = False

    def fail_first_index_write(path, payload):
        nonlocal failed
        if repository.idempotency_root in path.parents and not failed:
            failed = True
            raise RuntimeError("simulated index-write crash")
        return original_atomic_write_json(path, payload)

    monkeypatch.setattr(repository, "_atomic_write_json", fail_first_index_write)
    with pytest.raises(RuntimeError, match="simulated index-write crash"):
        service.create_handoff(_request())

    orphan_id = next(repository.records_root.glob("*.md")).stem
    assert list(repository.idempotency_root.glob("*/*.json")) == []
    monkeypatch.setattr(repository, "_atomic_write_json", original_atomic_write_json)

    recovered = service.create_handoff(_request())

    assert recovered.handoff_id == orphan_id
    assert len(list(repository.records_root.glob("*.md"))) == 1
    assert len(list(repository.idempotency_root.glob("*/*.json"))) == 1
    assert len(list(repository.transactions_root.glob("*.json"))) == 1
    recovered_event_ids = repository.recover_transactions()
    assert len(recovered_event_ids) == 1
    assert repository.list_events()[0].handoff_id == orphan_id


def test_recovery_does_not_enqueue_stale_prepared_event(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    created = service.create_handoff(_request())
    created_event = service.list_outbox()[0]
    repository.complete_event(created_event.event_id)
    event_path = repository._event_path(created_event.event_id)
    event_path.unlink()
    repository.prepare_event(created_event)

    service.set_status(created.handoff_id, actor="agent-b", status="acknowledged", expected_revision=0)

    assert repository.recover_transactions() == []
    assert not event_path.exists()
    assert (repository.transactions_root / f"{created_event.event_id}.json").exists()


def test_stale_transaction_can_be_explicitly_enqueued_with_resolution_evidence(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    created = service.create_handoff(_request())
    created_event = service.list_outbox()[0]
    repository.complete_event(created_event.event_id)
    event_path = repository._event_path(created_event.event_id)
    event_path.unlink()
    repository.prepare_event(created_event)
    service.set_status(created.handoff_id, actor="agent-b", status="acknowledged", expected_revision=0)

    resolved = service.resolve_transaction(
        created_event.event_id,
        action="enqueue",
        reason="Verified missing historical create delivery after canonical state advanced.",
    )

    assert resolved.event_id == created_event.event_id
    assert event_path.exists()
    assert not (repository.transactions_root / f"{created_event.event_id}.json").exists()
    evidence = repository.resolved_transactions_root / f"{created_event.event_id}.json"
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["action"] == "enqueue"
    assert payload["reason"].startswith("Verified missing historical")


def test_transaction_resolution_rejects_path_traversal_event_id(tmp_path: Path) -> None:
    _service_instance, repository = _service(tmp_path)

    with pytest.raises(ValueError, match="invalid event_id"):
        repository.resolve_transaction("../outside", action="discard", reason="invalid input")


def test_transaction_resolution_rejects_payload_event_id_mismatch(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    service.create_handoff(_request())
    event = service.list_outbox()[0]
    repository._event_path(event.event_id).unlink()
    repository.prepare_event(event)
    transaction_path = repository._transaction_path(event.event_id)
    payload = json.loads(transaction_path.read_text(encoding="utf-8"))
    payload["event"]["event_id"] = "EVT-substituted"
    transaction_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RepositoryConflictError, match="event_id mismatch"):
        service.resolve_transaction(event.event_id, action="enqueue", reason="must fail closed")
    with pytest.raises(RepositoryConflictError, match="event_id mismatch"):
        repository.recover_transactions()

    assert transaction_path.exists()
    assert not repository._event_path("EVT-substituted").exists()
    assert any("event_id mismatch" in issue for issue in repository.diagnose())


def test_prepared_transaction_can_be_discarded_with_resolution_evidence(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    service.create_handoff(_request())
    event = service.list_outbox()[0]
    repository._event_path(event.event_id).unlink()
    repository.prepare_event(event)

    resolved = service.resolve_transaction(
        event.event_id,
        action="discard",
        reason="Verified duplicate consumer delivery; no replay required.\n- not a second audit entry",
    )

    assert resolved.event_id == event.event_id
    assert not repository._event_path(event.event_id).exists()
    evidence = json.loads(repository._resolved_transaction_path(event.event_id).read_text(encoding="utf-8"))
    assert evidence["action"] == "discard"
    assert len(repository.audit_file.read_text(encoding="utf-8").splitlines()) == 2


def test_process_outbox_does_not_rescan_events_for_each_delivery(tmp_path: Path, monkeypatch) -> None:
    service, repository = _service(tmp_path)
    for index in range(4):
        service.create_handoff(_request(key=f"batch-{index}", subject=f"Batch {index}"))

    original_list_events = repository.list_events
    calls = 0

    def counted_list_events(*, states=None):
        nonlocal calls
        calls += 1
        return original_list_events(states=states)

    monkeypatch.setattr(repository, "list_events", counted_list_events)
    processed = service.process_outbox(lambda _event: (True, "ok"))

    assert len(processed) == 4
    assert calls == 1


def test_status_update_leaves_queue_views_at_same_revision(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    created = service.create_handoff(_request())
    updated = service.set_status(
        created.handoff_id,
        actor="agent-b",
        status="acknowledged",
        expected_revision=0,
    )

    records = repository.load_records(created.handoff_id)
    assert updated.revision == 1
    assert {stored.record.revision for stored in records} == {1}
    assert {stored.record.status for stored in records} == {"acknowledged"}


def test_concurrent_lifecycle_updates_have_one_revision_conflict(tmp_path: Path) -> None:
    service, _repository = _service(tmp_path)
    created = service.create_handoff(_request())

    def update(status: str):
        return service.set_status(
            created.handoff_id,
            actor="agent-b",
            status=status,
            outcome="Waiting." if status == "blocked" else "",
            expected_revision=0,
        )

    outcomes: list[object] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(update, status) for status in ("acknowledged", "blocked")]
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:  # asserted below
                outcomes.append(exc)

    assert sum(isinstance(item, HandoffRecord) for item in outcomes) == 1
    assert sum(isinstance(item, RevisionConflictError) for item in outcomes) == 1


def test_consumer_outage_keeps_event_and_recovery_reuses_event_id(tmp_path: Path) -> None:
    service, _repository = _service(tmp_path)
    created = service.create_handoff(_request())
    event = service.list_outbox(states={"pending"})[0]

    failed = service.process_outbox(
        lambda _event: (False, "consumer unavailable"),
        max_attempts=3,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )[0]
    assert failed.event_id == event.event_id
    assert failed.delivery_state == "retrying"

    delivered_ids: list[str] = []
    processed = service.process_outbox(
        lambda retry: (delivered_ids.append(retry.event_id) or True, "ok"),
    )
    assert processed[0].delivery_state == "delivered"
    assert delivered_ids == [event.event_id]
    assert created.handoff_id == processed[0].handoff_id


def test_replay_preserves_event_id_and_supports_consumer_deduplication(tmp_path: Path) -> None:
    service, _repository = _service(tmp_path)
    service.create_handoff(_request())
    event = service.list_outbox()[0]
    service.mark_event_delivered(event.event_id)

    replayed = service.replay_event(event.event_id)
    assert replayed.event_id == event.event_id
    seen: set[str] = {event.event_id}
    duplicate_effects: list[str] = []

    def deduplicating_consumer(item):
        if item.event_id not in seen:
            duplicate_effects.append(item.event_id)
            seen.add(item.event_id)
        return True, "deduplicated"

    service.process_outbox(deduplicating_consumer)
    assert duplicate_effects == []


def test_exhausted_retries_dead_letter_and_manual_replay(tmp_path: Path) -> None:
    service, _repository = _service(tmp_path)
    service.create_handoff(_request())
    event = service.list_outbox()[0]

    first = service.mark_event_failed(event.event_id, "down", max_attempts=2, base_delay_seconds=0, jitter_ratio=0)
    assert first.delivery_state == "retrying"
    second = service.mark_event_failed(event.event_id, "still down", max_attempts=2, base_delay_seconds=0, jitter_ratio=0)
    assert second.delivery_state == "dead_letter"
    replayed = service.replay_event(event.event_id)
    assert replayed.delivery_state == "pending"
    assert replayed.attempts == 0
    assert replayed.event_id == event.event_id


def test_role_specific_lifecycle_actions_fail_closed(tmp_path: Path) -> None:
    service, _repository = _service(tmp_path)
    created = service.create_handoff(_request())

    with pytest.raises(AccessPolicyError, match="not authorized"):
        service.set_status(created.handoff_id, actor="agent-a", status="closed", outcome="Not sender-owned.")
    with pytest.raises(AccessPolicyError, match="not authorized"):
        service.set_status(created.handoff_id, actor="agent-b", status="cancelled", outcome="Not recipient-owned.")

    cancelled = service.cancel_handoff(created.handoff_id, actor="agent-a", reason="Request withdrawn.")
    assert cancelled.status == "cancelled"


def test_existing_v1_handoff_loads_and_transitions_to_v2(tmp_path: Path) -> None:
    root = tmp_path / "bridge"
    repository = FileBridgeRepository(root)
    _write_v1_handoff(root)

    loaded = BridgeService(repository).get_handoff("HND-V1-0001")
    assert loaded.schema_version == 1
    assert loaded.revision == 0
    transitioned = BridgeService(repository).set_status(
        "HND-V1-0001",
        actor="agent-b",
        status="acknowledged",
        expected_revision=0,
    )
    assert transitioned.schema_version == 2
    assert transitioned.revision == 1
    assert repository._canonical_path("HND-V1-0001").exists()


def test_identical_v1_mirrors_are_not_divergence_and_migrate_without_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "bridge"
    repository = FileBridgeRepository(root)
    _write_v1_handoff(root)

    assert repository.reconcile_handoff("HND-V1-0001", repair=False) == []

    issues = repository.reconcile_handoff("HND-V1-0001", repair=True)
    canonical = repository._canonical_path("HND-V1-0001")
    outgoing = root / "outgoing" / "agent-a" / "HND-V1-0001.md"
    incoming = root / "incoming" / "agent-b" / "HND-V1-0001.md"

    assert issues == ["HND-V1-0001: missing canonical record"]
    assert canonical.exists()
    assert outgoing.read_bytes() == canonical.read_bytes()
    assert incoming.read_bytes() == canonical.read_bytes()
    assert list(repository.conflicts_root.glob("HND-V1-0001.*")) == []


def test_repository_diagnostics_detect_new_failure_states(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    created = service.create_handoff(_request())
    incoming = repository.bridge_root / "incoming" / "agent-b" / f"{created.handoff_id}.md"
    incoming.unlink()

    event = service.list_outbox()[0]
    service.mark_event_failed(event.event_id, "down", max_attempts=1)

    index_path = next(repository.idempotency_root.glob("*/*.json"))
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["request_hash"] = "bad-hash"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    stale = service.create_handoff(_request(key="create-stale", subject="Stale mirror"))
    stale_outgoing = repository.bridge_root / "outgoing" / "agent-a" / f"{stale.handoff_id}.md"
    stale_data, stale_body = parse_frontmatter(stale_outgoing.read_text(encoding="utf-8"))
    stale_data["revision"] = "99"
    stale_outgoing.write_text(dump_document(stale_data, stale_body), encoding="utf-8")

    divergent = service.create_handoff(_request(key="create-divergent", subject="Divergent mirror"))
    divergent_outgoing = repository.bridge_root / "outgoing" / "agent-a" / f"{divergent.handoff_id}.md"
    divergent_outgoing.write_text(divergent_outgoing.read_text(encoding="utf-8") + "\nlocal divergence\n", encoding="utf-8")

    issues = repository.diagnose(pending_age_seconds=0)
    assert any("missing mirror" in issue for issue in issues)
    assert any("stale revision" in issue for issue in issues)
    assert any("divergent mirror" in issue for issue in issues)
    assert any("pending outbox event beyond threshold" in issue for issue in issues)
    assert any("dead-letter event" in issue for issue in issues)
    assert any("idempotency index inconsistency" in issue for issue in issues)


def test_reconcile_preserves_divergent_evidence_before_repair(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    created = service.create_handoff(_request())
    incoming = repository.bridge_root / "incoming" / "agent-b" / f"{created.handoff_id}.md"
    incoming.write_text(incoming.read_text(encoding="utf-8") + "\nlocal note\n", encoding="utf-8")

    issues = repository.reconcile_handoff(created.handoff_id, repair=True)
    assert any("divergent mirror" in issue for issue in issues)
    assert list(repository.conflicts_root.glob(f"{created.handoff_id}.*.incoming.md"))
    canonical = repository._canonical_path(created.handoff_id)
    assert incoming.read_bytes() == canonical.read_bytes()
