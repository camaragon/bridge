from __future__ import annotations

from pathlib import Path

import pytest

from bridge_core.file_repository import FileBridgeRepository
from bridge_core.frontmatter import parse_frontmatter
from bridge_core.models import CreateHandoffInput
from bridge_core.policy import StatusPolicyError
from bridge_core.service import BridgeService


def _build_service(tmp_path: Path) -> tuple[BridgeService, Path]:
    root = tmp_path / "agent-shared"
    bridge = root / "bridge"
    repo = FileBridgeRepository(bridge_root=bridge)
    return BridgeService(repository=repo), bridge


def test_create_handoff_writes_dual_queue_files_and_defaults(tmp_path: Path) -> None:
    service, bridge = _build_service(tmp_path)

    handoff = service.create_handoff(
        CreateHandoffInput(
            sender="hermes",
            recipient="jordan",
            issue_type="task",
            subject="Need follow-up",
            requested_action="Handle it",
            minimal_context="Only what is needed",
            related_paths=["/tmp/example.md"],
        )
    )

    outgoing = bridge / "outgoing" / "hermes" / f"{handoff.handoff_id}.md"
    incoming = bridge / "incoming" / "jordan" / f"{handoff.handoff_id}.md"
    assert outgoing.exists()
    assert incoming.exists()

    outgoing_data, outgoing_body = parse_frontmatter(outgoing.read_text(encoding="utf-8"))
    incoming_data, incoming_body = parse_frontmatter(incoming.read_text(encoding="utf-8"))
    assert outgoing_data == incoming_data
    assert outgoing_body == incoming_body
    assert handoff.status == "open"
    assert handoff.handoff_kind == "request"
    assert handoff.risk_level == "low"
    assert handoff.due_at == "none"
    assert handoff.approval_context == "none"
    assert handoff.resolution_summary == "pending"


def test_create_handoff_rejects_invalid_domain_values(tmp_path: Path) -> None:
    service, _bridge = _build_service(tmp_path)

    with pytest.raises(ValueError, match="invalid handoff_kind"):
        service.create_handoff(
            CreateHandoffInput(
                sender="hermes",
                recipient="jordan",
                issue_type="task",
                subject="Need follow-up",
                requested_action="Handle it",
                minimal_context="Only what is needed",
                handoff_kind="weird",
            )
        )

    with pytest.raises(ValueError, match="invalid priority"):
        service.create_handoff(
            CreateHandoffInput(
                sender="hermes",
                recipient="jordan",
                issue_type="task",
                subject="Need follow-up",
                requested_action="Handle it",
                minimal_context="Only what is needed",
                priority="p0",
            )
        )

    with pytest.raises(ValueError, match="invalid risk_level"):
        service.create_handoff(
            CreateHandoffInput(
                sender="hermes",
                recipient="jordan",
                issue_type="task",
                subject="Need follow-up",
                requested_action="Handle it",
                minimal_context="Only what is needed",
                risk_level="severe",
            )
        )


def test_create_handoff_sanitizes_frontmatter_injected_values(tmp_path: Path) -> None:
    service, bridge = _build_service(tmp_path)

    handoff = service.create_handoff(
        CreateHandoffInput(
            sender="hermes",
            recipient="jordan",
            issue_type="task",
            subject="hello\nstatus: archived",
            requested_action="Handle it",
            minimal_context="Only what is needed",
            related_paths=["/tmp/example.md\nstatus: archived"],
        )
    )

    outgoing = bridge / "outgoing" / "hermes" / f"{handoff.handoff_id}.md"
    data, _body = parse_frontmatter(outgoing.read_text(encoding="utf-8"))
    assert data["status"] == "open"
    assert data["subject"] == "hello status: archived"
    assert data["related_paths"] == ["/tmp/example.md status: archived"]


def test_list_handoffs_returns_all_or_only_active(tmp_path: Path) -> None:
    service, _bridge = _build_service(tmp_path)

    handoff = service.create_handoff(
        CreateHandoffInput(
            sender="hermes",
            recipient="jordan",
            issue_type="task",
            subject="Need follow-up",
            requested_action="Handle it",
            minimal_context="Only what is needed",
        )
    )
    service.set_status(handoff.handoff_id, actor="jordan", status="closed", outcome="Done.")

    assert service.list_handoffs("jordan") == []
    all_items = service.list_handoffs("jordan", active_only=False)
    assert len(all_items) == 1
    assert all_items[0].handoff_id == handoff.handoff_id
    assert all_items[0].status == "closed"


def test_set_status_updates_both_copies_and_archives_preserving_divergent_copy(tmp_path: Path) -> None:
    service, bridge = _build_service(tmp_path)
    handoff = service.create_handoff(
        CreateHandoffInput(
            sender="hermes",
            recipient="jordan",
            issue_type="task",
            subject="Lifecycle",
            requested_action="Handle it",
            minimal_context="Only what is needed",
        )
    )

    service.set_status(handoff.handoff_id, actor="jordan", status="acked", acknowledgment_source="auto")
    closed = service.set_status(
        handoff.handoff_id,
        actor="jordan",
        status="closed",
        outcome="Finished safely.",
    )
    assert closed.status == "closed"
    assert closed.resolution_summary == "Finished safely."

    outgoing = bridge / "outgoing" / "hermes" / f"{handoff.handoff_id}.md"
    incoming = bridge / "incoming" / "jordan" / f"{handoff.handoff_id}.md"
    incoming.write_text(incoming.read_text(encoding="utf-8") + "\n<!-- recipient note -->\n", encoding="utf-8")

    archive_dir = service.archive_handoff(handoff.handoff_id, actor="jordan")
    canonical = archive_dir / f"{handoff.handoff_id}.md"
    alternate = archive_dir / f"{handoff.handoff_id}.incoming.jordan.md"

    assert archive_dir.exists()
    assert canonical.exists()
    assert alternate.exists()
    assert not outgoing.exists()
    assert not incoming.exists()

    archived_data, archived_body = parse_frontmatter(canonical.read_text(encoding="utf-8"))
    assert archived_data["status"] == "archived"
    assert archived_data["acknowledgment_source"] == "auto"
    assert archived_data["acknowledged_at"] != "none"
    assert archived_data["resolution_summary"] == "Finished safely."
    assert "## Outcome\nFinished safely." in archived_body

    audit_lines = (bridge / "audit" / "handoff-log.md").read_text(encoding="utf-8").strip().splitlines()
    assert len(audit_lines) == 4
    assert " | open | Lifecycle" in audit_lines[0]
    assert " | acknowledged | Lifecycle | ack_source=auto acknowledged_at=" in audit_lines[1]
    assert " | closed | Lifecycle" in audit_lines[2]
    assert " | archived | Lifecycle" in audit_lines[3]


def test_set_status_rejects_unknown_acknowledgment_source(tmp_path: Path) -> None:
    service, _bridge = _build_service(tmp_path)
    handoff = service.create_handoff(
        CreateHandoffInput(
            sender="hermes",
            recipient="jordan",
            issue_type="task",
            subject="Bad ack source",
            requested_action="Handle it",
            minimal_context="Only what is needed",
        )
    )

    with pytest.raises(StatusPolicyError, match="acknowledgment_source must be auto or manual"):
        service.set_status(handoff.handoff_id, actor="jordan", status="acknowledged", acknowledgment_source="bot")


def test_set_status_uses_one_timestamp_and_preserves_sections_after_outcome(tmp_path: Path) -> None:
    service, bridge = _build_service(tmp_path)
    handoff = service.create_handoff(
        CreateHandoffInput(
            sender="hermes",
            recipient="jordan",
            issue_type="task",
            subject="Outcome preservation",
            requested_action="Handle it",
            minimal_context="Only what is needed",
        )
    )

    incoming = bridge / "incoming" / "jordan" / f"{handoff.handoff_id}.md"
    data, body = parse_frontmatter(incoming.read_text(encoding="utf-8"))
    body = body.rstrip() + "\n## Notes\nKeep this section.\n"
    incoming.write_text("---\n" + "\n".join(f"{k}: {v}" if not isinstance(v, list) else f"{k}:\n" + "\n".join(f"  - {item}" for item in v) for k, v in data.items()) + "\n---\n\n" + body.lstrip("\n"), encoding="utf-8")

    service.set_status(handoff.handoff_id, actor="jordan", status="closed", outcome="Finished safely.")

    outgoing = bridge / "outgoing" / "hermes" / f"{handoff.handoff_id}.md"
    outgoing_data, outgoing_body = parse_frontmatter(outgoing.read_text(encoding="utf-8"))
    incoming_data, incoming_body = parse_frontmatter(incoming.read_text(encoding="utf-8"))
    assert outgoing_data["updated_at"] == incoming_data["updated_at"]
    assert "## Notes\nKeep this section." in incoming_body
    assert "## Outcome\nFinished safely.\n## Notes\nKeep this section." in incoming_body
    assert outgoing_body.count("## Outcome") == 1


def test_archive_requires_closed_status(tmp_path: Path) -> None:
    service, _bridge = _build_service(tmp_path)
    handoff = service.create_handoff(
        CreateHandoffInput(
            sender="hermes",
            recipient="jarvy",
            issue_type="task",
            subject="Still open",
            requested_action="Handle it",
            minimal_context="Only what is needed",
        )
    )

    with pytest.raises(StatusPolicyError, match="only closed handoffs can be archived"):
        service.archive_handoff(handoff.handoff_id, actor="jarvy")
