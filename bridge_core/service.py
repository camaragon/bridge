from __future__ import annotations

from pathlib import Path
import re

from .models import CreateHandoffInput, HandoffRecord, StoredHandoff, now_iso
from .policy import (
    StatusPolicyError,
    is_active_status,
    normalize_status,
    require_actor_access,
    require_route,
    require_status_transition,
)
from .repository import BridgeRepository


class BridgeService:
    def __init__(self, repository: BridgeRepository):
        self.repository = repository

    def create_handoff(self, request: CreateHandoffInput) -> HandoffRecord:
        require_route(request.sender, request.recipient)
        handoff = HandoffRecord.from_create(request)
        return self.repository.create(handoff)

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
    ) -> HandoffRecord:
        records = self.repository.load_records(handoff_id)
        primary = records[0].record
        require_actor_access(actor, primary.sender, primary.recipient)
        outcome_text = outcome.strip()
        has_resolution_summary = bool(outcome_text or primary.resolution_summary != "pending")
        normalized_status = require_status_transition(primary.status, status, has_resolution_summary=has_resolution_summary)
        ack_source = _normalize_acknowledgment_source(acknowledgment_source, status=normalized_status)
        updated = self._mutate_records(records, normalized_status, outcome_text, acknowledgment_source=ack_source)
        self.repository.save_records(records)
        self.repository.append_audit(updated, updated.status)
        return updated

    def archive_handoff(self, handoff_id: str, *, actor: str) -> Path:
        records = self.repository.load_records(handoff_id)
        primary = records[0].record
        require_actor_access(actor, primary.sender, primary.recipient)
        if {normalize_status(item.record.status) for item in records} != {"closed"}:
            raise StatusPolicyError("only closed handoffs can be archived")
        archived = self._mutate_records(records, "archived", "", acknowledgment_source=None)
        archive_dir = self.repository.archive_records(records)
        self.repository.append_audit(archived, archived.status)
        return archive_dir

    def _mutate_records(
        self,
        records: list[StoredHandoff],
        status: str,
        outcome: str,
        *,
        acknowledgment_source: str | None,
    ) -> HandoffRecord:
        updated: HandoffRecord | None = None
        transition_time = now_iso()
        for stored in records:
            stored.record.status = status
            stored.record.updated_at = transition_time
            if status == "acknowledged" and stored.record.acknowledged_at == "none":
                stored.record.acknowledged_at = transition_time
                stored.record.acknowledgment_source = acknowledgment_source or "manual"
            if outcome:
                stored.record.resolution_summary = outcome
                stored.record.body = self._replace_outcome(stored.record.body, outcome)
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
