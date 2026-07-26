from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
import random
import re

from .events import LifecycleEvent
from .models import CURRENT_SCHEMA_VERSION, CreateHandoffInput, HandoffRecord, StoredHandoff, now_iso
from .policy import (
    AccessPolicyError,
    StatusPolicyError,
    is_active_status,
    normalize_status,
    require_actor_access,
    require_lifecycle_role,
    require_route,
    require_status_transition,
)
from .repository import BridgeRepository, IdempotencyConflictError, RevisionConflictError


class BridgeService:
    def __init__(self, repository: BridgeRepository):
        self.repository = repository

    def create_handoff(self, request: CreateHandoffInput) -> HandoffRecord:
        require_route(request.sender, request.recipient)
        request.validate()
        proposed = HandoffRecord.from_create(request)
        event = LifecycleEvent.from_handoff(
            proposed,
            event_type="handoff_created",
            actor=request.sender,
            target_agent=request.recipient,
        )
        self.repository.prepare_event(event)
        try:
            created = self.repository.create(proposed, idempotency_key=request.idempotency_key)
        except IdempotencyConflictError:
            self.repository.complete_event(event.event_id)
            raise
        except BaseException:
            # A prepared transaction is intentionally retained if canonical state was
            # committed before a crash; recovery can decide whether to enqueue it.
            raise
        if created.handoff_id != proposed.handoff_id:
            self.repository.complete_event(event.event_id)
            return created
        self.repository.enqueue_event(event)
        self.repository.complete_event(event.event_id)
        return created

    def get_handoff(self, handoff_id: str, *, actor: str | None = None) -> HandoffRecord:
        records = self.repository.load_records(handoff_id)
        primary = records[0].record
        if actor is not None:
            require_actor_access(actor, primary.sender, primary.recipient)
        return primary

    def list_handoffs(self, agent: str, *, active_only: bool = True) -> list[HandoffRecord]:
        records = self.repository.list_incoming(agent)
        if active_only:
            return [record for record in records if is_active_status(record.status)]
        return records

    def list_open_handoffs(self, agent: str) -> list[HandoffRecord]:
        return self.list_handoffs(agent, active_only=True)

    def set_status(
        self,
        handoff_id: str,
        *,
        actor: str,
        status: str,
        outcome: str = "",
        acknowledgment_source: str | None = None,
        expected_revision: int | None = None,
        maintenance: bool = False,
    ) -> HandoffRecord:
        with self.repository.handoff_lock(handoff_id):
            records = self.repository.load_records(handoff_id)
            primary = records[0].record
            normalized_requested = normalize_status(status)
            require_lifecycle_role(
                actor=actor,
                sender=primary.sender,
                recipient=primary.recipient,
                new_status=normalized_requested,
                maintenance=maintenance,
            )
            outcome_text = outcome.strip()
            if expected_revision is not None and expected_revision != primary.revision:
                if self._is_safe_same_state_retry(primary, normalized_requested, outcome_text):
                    return primary
                raise RevisionConflictError(
                    f"stale revision for {handoff_id}: expected {expected_revision}, current {primary.revision}"
                )
            if self._is_safe_same_state_retry(primary, normalized_requested, outcome_text):
                return primary
            has_resolution_summary = bool(outcome_text or primary.resolution_summary != "pending")
            normalized_status = require_status_transition(
                primary.status,
                normalized_requested,
                has_resolution_summary=has_resolution_summary,
            )
            ack_source = _normalize_acknowledgment_source(acknowledgment_source, status=normalized_status)
            next_revision = primary.revision + 1
            updated = self._mutate_records(
                records,
                normalized_status,
                outcome_text,
                acknowledgment_source=ack_source,
                revision=next_revision,
            )
            target_agent = updated.recipient if normalized_status == "cancelled" else updated.sender
            event = LifecycleEvent.from_handoff(
                updated,
                event_type=f"handoff_{normalized_status}",
                actor=actor,
                target_agent=target_agent,
            )
            self.repository.prepare_event(event)
            self.repository.save_records(records)
            self.repository.append_audit(updated, updated.status)
            self.repository.enqueue_event(event)
            self.repository.complete_event(event.event_id)
            return updated

    def cancel_handoff(
        self,
        handoff_id: str,
        *,
        actor: str,
        reason: str,
        expected_revision: int | None = None,
    ) -> HandoffRecord:
        if not reason.strip():
            raise StatusPolicyError("cancelling a handoff requires a reason")
        return self.set_status(
            handoff_id,
            actor=actor,
            status="cancelled",
            outcome=reason,
            expected_revision=expected_revision,
        )

    def archive_handoff(
        self,
        handoff_id: str,
        *,
        actor: str,
        expected_revision: int | None = None,
        maintenance: bool = False,
    ) -> Path:
        with self.repository.handoff_lock(handoff_id):
            records = self.repository.load_records(handoff_id)
            primary = records[0].record
            require_lifecycle_role(
                actor=actor,
                sender=primary.sender,
                recipient=primary.recipient,
                new_status="archived",
                maintenance=maintenance,
            )
            if expected_revision is not None and expected_revision != primary.revision:
                raise RevisionConflictError(
                    f"stale revision for {handoff_id}: expected {expected_revision}, current {primary.revision}"
                )
            statuses = {normalize_status(item.record.status) for item in records}
            if not statuses <= {"closed", "cancelled"}:
                raise StatusPolicyError("only closed handoffs can be archived (or cancelled handoffs via maintenance recovery)")
            archived = self._mutate_records(
                records,
                "archived",
                "",
                acknowledgment_source=None,
                revision=primary.revision + 1,
            )
            event = LifecycleEvent.from_handoff(
                archived,
                event_type="handoff_archived",
                actor=actor,
                target_agent=archived.sender,
            )
            self.repository.prepare_event(event)
            archive_dir = self.repository.archive_records(records)
            self.repository.append_audit(archived, archived.status)
            self.repository.enqueue_event(event)
            self.repository.complete_event(event.event_id)
            return archive_dir

    def list_outbox(self, *, states: set[str] | None = None) -> list[LifecycleEvent]:
        return self.repository.list_events(states=states)

    def replay_event(self, event_id: str) -> LifecycleEvent:
        return self.repository.replay_event(event_id)

    def process_outbox(
        self,
        deliver: Callable[[LifecycleEvent], tuple[bool, str]],
        *,
        limit: int = 100,
        max_attempts: int = 8,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 300.0,
    ) -> list[LifecycleEvent]:
        processed: list[LifecycleEvent] = []
        due = [event for event in self.list_outbox(states={"pending", "retrying"}) if event.due()]
        for event in due[: max(0, limit)]:
            ok, detail = deliver(event)
            if ok:
                processed.append(self.mark_event_delivered(event.event_id))
            else:
                processed.append(
                    self.mark_event_failed(
                        event.event_id,
                        detail,
                        max_attempts=max_attempts,
                        base_delay_seconds=base_delay_seconds,
                        max_delay_seconds=max_delay_seconds,
                    )
                )
        return processed

    def mark_event_delivered(self, event_id: str) -> LifecycleEvent:
        event = self._event(event_id)
        event.delivery_state = "delivered"
        event.last_error = ""
        return self.repository.update_event(event)

    def mark_event_failed(
        self,
        event_id: str,
        error: str,
        *,
        max_attempts: int = 8,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 300.0,
        jitter_ratio: float = 0.2,
    ) -> LifecycleEvent:
        event = self._event(event_id)
        event.attempts += 1
        event.last_error = error.strip()[:1000]
        if event.attempts >= max(1, max_attempts):
            event.delivery_state = "dead_letter"
            event.next_attempt_at = now_iso()
        else:
            event.delivery_state = "retrying"
            base = min(max_delay_seconds, base_delay_seconds * (2 ** (event.attempts - 1)))
            jitter = random.uniform(0.0, max(0.0, base * jitter_ratio))
            next_attempt = datetime.now(timezone.utc) + timedelta(seconds=base + jitter)
            event.next_attempt_at = next_attempt.isoformat().replace("+00:00", "Z")
        return self.repository.update_event(event)

    def _event(self, event_id: str) -> LifecycleEvent:
        for event in self.repository.list_events():
            if event.event_id == event_id:
                return event
        raise FileNotFoundError(f"event not found: {event_id}")

    @staticmethod
    def _is_safe_same_state_retry(primary: HandoffRecord, status: str, outcome: str) -> bool:
        if normalize_status(primary.status) != status:
            return False
        if not outcome:
            return True
        return primary.resolution_summary == outcome

    def _mutate_records(
        self,
        records: list[StoredHandoff],
        status: str,
        outcome: str,
        *,
        acknowledgment_source: str | None,
        revision: int,
    ) -> HandoffRecord:
        updated: HandoffRecord | None = None
        transition_time = now_iso()
        for stored in records:
            stored.record.status = status
            stored.record.schema_version = CURRENT_SCHEMA_VERSION
            stored.record.revision = revision
            stored.record.updated_at = transition_time
            if status == "acknowledged" and stored.record.acknowledged_at == "none":
                stored.record.acknowledged_at = transition_time
                stored.record.acknowledgment_source = acknowledgment_source or "manual"
            if outcome:
                stored.record.resolution_summary = outcome
                stored.record.body = self._replace_outcome(stored.record.body, outcome)
            if updated is None:
                updated = stored.record
        assert updated is not None
        return updated

    @staticmethod
    def _replace_outcome(body: str, outcome: str) -> str:
        replacement = f"## Outcome\n{outcome}\n"
        pattern = r"(?ms)^## Outcome\n.*?(?=^## |\Z)"
        if re.search(pattern, body):
            return re.sub(pattern, replacement, body, count=1)
        return body.rstrip() + f"\n\n{replacement}"


def _normalize_acknowledgment_source(value: str | None, *, status: str) -> str | None:
    if status != "acknowledged":
        return None
    normalized = str(value or "manual").strip().lower()
    if normalized not in {"manual", "auto"}:
        raise StatusPolicyError("acknowledgment_source must be auto or manual")
    return normalized