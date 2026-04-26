from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
import random

AGENTS = ("hermes", "jarvy", "jordan")
ACTIVE_STATUSES = {"open", "acknowledged", "in_progress", "blocked"}
ALL_STATUSES = ACTIVE_STATUSES | {"closed", "archived"}
HANDOFF_KINDS = {"incident", "request", "question", "result"}
PRIORITIES = {"low", "medium", "high", "urgent"}
RISK_LEVELS = {"low", "medium", "high"}
DEFAULT_RESPONSE_FORMAT = "concise status + action + blocker if any"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def generate_handoff_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choice("0123456789abcdef") for _ in range(4))
    return f"HND-{ts}-{suffix}"


@dataclass(slots=True)
class CreateHandoffInput:
    sender: str
    recipient: str
    issue_type: str
    subject: str
    requested_action: str
    minimal_context: str
    handoff_kind: str = "request"
    priority: str = "medium"
    risk_level: str = "low"
    due_at: str = "none"
    approval_needed: bool = False
    approval_context: str = "none"
    response_format: str = DEFAULT_RESPONSE_FORMAT
    related_paths: Sequence[str] = field(default_factory=list)
    constraints: str = "- none"

    def validate(self) -> None:
        if self.handoff_kind not in HANDOFF_KINDS:
            raise ValueError(f"invalid handoff_kind: {self.handoff_kind}")
        if self.priority not in PRIORITIES:
            raise ValueError(f"invalid priority: {self.priority}")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"invalid risk_level: {self.risk_level}")


@dataclass(slots=True)
class HandoffRecord:
    handoff_id: str
    status: str
    created_at: str
    updated_at: str
    acknowledged_at: str
    acknowledgment_source: str
    sender: str
    recipient: str
    issue_type: str
    handoff_kind: str = "request"
    priority: str = "medium"
    risk_level: str = "low"
    due_at: str = "none"
    approval_needed: str = "no"
    approval_context: str = "none"
    resolution_summary: str = "pending"
    subject: str = ""
    response_format: str = DEFAULT_RESPONSE_FORMAT
    related_paths: list[str] = field(default_factory=list)
    body: str = ""

    @classmethod
    def from_create(cls, data: CreateHandoffInput) -> "HandoffRecord":
        data.validate()
        ts = now_iso()
        return cls(
            handoff_id=generate_handoff_id(),
            status="open",
            created_at=ts,
            updated_at=ts,
            acknowledged_at="none",
            acknowledgment_source="none",
            sender=data.sender,
            recipient=data.recipient,
            issue_type=data.issue_type,
            handoff_kind=data.handoff_kind,
            priority=data.priority,
            risk_level=data.risk_level,
            due_at=data.due_at or "none",
            approval_needed="yes" if data.approval_needed else "no",
            approval_context=data.approval_context or "none",
            resolution_summary="pending",
            subject=data.subject,
            response_format=data.response_format,
            related_paths=list(data.related_paths),
            body=render_body(data.requested_action, data.minimal_context, data.constraints),
        )

    @classmethod
    def from_mapping(cls, mapping: dict[str, object], body: str) -> "HandoffRecord":
        return cls(
            handoff_id=str(mapping["handoff_id"]),
            status=str(mapping.get("status", "open")),
            created_at=str(mapping.get("created_at", "")),
            updated_at=str(mapping.get("updated_at", "")),
            acknowledged_at=str(mapping.get("acknowledged_at", "none") or "none"),
            acknowledgment_source=str(mapping.get("acknowledgment_source", "none") or "none"),
            sender=str(mapping.get("sender", "")),
            recipient=str(mapping.get("recipient", "")),
            issue_type=str(mapping.get("issue_type", "")),
            handoff_kind=str(mapping.get("handoff_kind", "request") or "request"),
            priority=str(mapping.get("priority", "medium") or "medium"),
            risk_level=str(mapping.get("risk_level", "low") or "low"),
            due_at=str(mapping.get("due_at", "none") or "none"),
            approval_needed=str(mapping.get("approval_needed", "no") or "no"),
            approval_context=str(mapping.get("approval_context", "none") or "none"),
            resolution_summary=str(mapping.get("resolution_summary", "pending") or "pending"),
            subject=str(mapping.get("subject", "")),
            response_format=str(mapping.get("response_format", DEFAULT_RESPONSE_FORMAT) or DEFAULT_RESPONSE_FORMAT),
            related_paths=[str(item) for item in mapping.get("related_paths", [])],
            body=body,
        )

    def to_frontmatter(self) -> dict[str, object]:
        return {
            "handoff_id": self.handoff_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "acknowledged_at": self.acknowledged_at,
            "acknowledgment_source": self.acknowledgment_source,
            "sender": self.sender,
            "recipient": self.recipient,
            "issue_type": self.issue_type,
            "handoff_kind": self.handoff_kind,
            "priority": self.priority,
            "risk_level": self.risk_level,
            "due_at": self.due_at,
            "approval_needed": self.approval_needed,
            "approval_context": self.approval_context,
            "resolution_summary": self.resolution_summary,
            "subject": self.subject,
            "response_format": self.response_format,
            "related_paths": list(self.related_paths),
        }


@dataclass(slots=True)
class StoredHandoff:
    path: Path
    record: HandoffRecord


def render_body(requested_action: str, minimal_context: str, constraints: str) -> str:
    return "\n".join(
        [
            "## Requested Action",
            requested_action.strip(),
            "",
            "## Minimal Context",
            minimal_context.strip(),
            "",
            "## Constraints",
            constraints.strip() if constraints.strip() else "- none",
            "",
            "## Outcome",
            "- pending",
            "",
        ]
    )
