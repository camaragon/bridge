from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .models import CURRENT_SCHEMA_VERSION, HandoffRecord, now_iso

DELIVERY_STATES = {"pending", "retrying", "delivered", "dead_letter"}


@dataclass(slots=True)
class LifecycleEvent:
    event_id: str
    handoff_id: str
    event_type: str
    revision: int
    occurred_at: str
    target_agent: str
    sender: str
    recipient: str
    actor: str
    status: str
    subject: str
    resolution_summary: str
    correlation_id: str | None = None
    causation_id: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION
    attempts: int = 0
    next_attempt_at: str = field(default_factory=now_iso)
    last_error: str = ""
    delivery_state: str = "pending"
    external_correlation: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_handoff(
        cls,
        handoff: HandoffRecord,
        *,
        event_type: str,
        actor: str,
        target_agent: str,
        event_id: str | None = None,
    ) -> "LifecycleEvent":
        return cls(
            event_id=event_id or f"EVT-{uuid4().hex}",
            handoff_id=handoff.handoff_id,
            event_type=event_type,
            revision=handoff.revision,
            occurred_at=now_iso(),
            target_agent=target_agent,
            sender=handoff.sender,
            recipient=handoff.recipient,
            actor=actor,
            status=handoff.status,
            subject=handoff.subject,
            resolution_summary=handoff.resolution_summary,
            correlation_id=handoff.correlation_id,
            causation_id=handoff.causation_id,
        )

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "LifecycleEvent":
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        values = {key: value for key, value in mapping.items() if key in allowed}
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def due(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        due_at = datetime.fromisoformat(self.next_attempt_at.replace("Z", "+00:00"))
        return self.delivery_state in {"pending", "retrying"} and due_at <= current