from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
import hashlib
import json
import random

CURRENT_SCHEMA_VERSION = 2
ACTIVE_STATUSES = {"open", "acknowledged", "in_progress", "blocked"}
ALL_STATUSES = ACTIVE_STATUSES | {"closed", "cancelled", "archived"}
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
    idempotency_key: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    def validate(self) -> None:
        if self.handoff_kind not in HANDOFF_KINDS:
            raise ValueError(f"invalid handoff_kind: {self.handoff_kind}")
        if self.priority not in PRIORITIES:
            raise ValueError(f"invalid priority: {self.priority}")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"invalid risk_level: {self.risk_level}")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")

    def canonical_request_hash(self) -> str:
        payload = {
            "sender": self.sender,
            "recipient": self.recipient,
            "issue_type": self.issue_type,
            "subject": self.subject,
            "requested_action": self.requested_action,
            "minimal_context": self.minimal_context,
            "handoff_kind": self.handoff_kind,
            "priority": self.priority,
            "risk_level": self.risk_level,
            "due_at": self.due_at or "none",
            "approval_needed": bool(self.approval_needed),
            "approval_context": self.approval_context or "none",
            "response_format": self.response_format,
            "related_paths": list(self.related_paths),
            "constraints": self.constraints or "- none",
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


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
    schema_version: int = CURRENT_SCHEMA_VERSION
    revision: int = 0
    correlation_id: str | None = None
    causation_id: str | None = None
    canonical_request_hash: str = ""
    idempotency_key_hash: str = ""

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
            schema_version=CURRENT_SCHEMA_VERSION,
            revision=0,
            correlation_id=data.correlation_id,
            causation_id=data.causation_id,
            canonical_request_hash=data.canonical_request_hash(),
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
            schema_version=int(str(mapping.get("schema_version", 1) or 1)),
            revision=int(str(mapping.get("revision", 0) or 0)),
            correlation_id=_optional_string(mapping.get("correlation_id")),
            causation_id=_optional_string(mapping.get("causation_id")),
            canonical_request_hash=str(mapping.get("canonical_request_hash", "") or ""),
            idempotency_key_hash=str(mapping.get("idempotency_key_hash", "") or ""),
        )

    def to_frontmatter(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
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
            "correlation_id": self.correlation_id or "none",
            "causation_id": self.causation_id or "none",
            "canonical_request_hash": self.canonical_request_hash,
            "idempotency_key_hash": self.idempotency_key_hash,
        }


@dataclass(slots=True)
class StoredHandoff:
    path: Path
    record: HandoffRecord


def _optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return None if normalized in {"", "none"} else normalized


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
