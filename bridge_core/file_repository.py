from __future__ import annotations

from pathlib import Path
import shutil

from .frontmatter import dump_document, parse_frontmatter
from .models import HandoffRecord, StoredHandoff, now_iso
from .repository import BridgeRepository, HandoffNotFoundError


class FileBridgeRepository(BridgeRepository):
    def __init__(self, bridge_root: Path | str):
        self.bridge_root = Path(bridge_root)
        self.audit_file = self.bridge_root / "audit" / "handoff-log.md"
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for kind in ("incoming", "outgoing"):
            for agent in ("hermes", "jarvy", "jordan"):
                (self.bridge_root / kind / agent).mkdir(parents=True, exist_ok=True)
        (self.bridge_root / "archive").mkdir(parents=True, exist_ok=True)
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.audit_file.exists():
            self.audit_file.write_text("", encoding="utf-8")

    def _queue_path(self, kind: str, agent: str, handoff_id: str) -> Path:
        return self.bridge_root / kind / agent / f"{handoff_id}.md"

    def _write_record(self, path: Path, handoff: HandoffRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_document(handoff.to_frontmatter(), handoff.body), encoding="utf-8")
        path.chmod(0o600)

    def create(self, handoff: HandoffRecord) -> HandoffRecord:
        outgoing = self._queue_path("outgoing", handoff.sender, handoff.handoff_id)
        incoming = self._queue_path("incoming", handoff.recipient, handoff.handoff_id)
        self._write_record(outgoing, handoff)
        shutil.copy2(outgoing, incoming)
        incoming.chmod(0o600)
        self.append_audit(handoff, handoff.status)
        return handoff

    def load_records(self, handoff_id: str) -> list[StoredHandoff]:
        def sort_key(path: Path) -> tuple[int, str]:
            parts = path.parts
            if "outgoing" in parts:
                rank = 0
            elif "incoming" in parts:
                rank = 1
            elif "archive" in parts:
                rank = 2
            else:
                rank = 3
            return rank, str(path)

        matches = sorted(self.bridge_root.glob(f"**/{handoff_id}.md"), key=sort_key)
        if not matches:
            raise HandoffNotFoundError("handoff not found")
        records: list[StoredHandoff] = []
        for path in matches:
            data, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            records.append(StoredHandoff(path=path, record=HandoffRecord.from_mapping(data, body)))
        return records

    def list_incoming(self, agent: str) -> list[HandoffRecord]:
        records: list[HandoffRecord] = []
        for path in sorted((self.bridge_root / "incoming" / agent).glob("*.md")):
            data, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            records.append(HandoffRecord.from_mapping(data, body))
        return records

    def save_records(self, records: list[StoredHandoff]) -> None:
        for stored in records:
            self._write_record(stored.path, stored.record)

    def archive_records(self, records: list[StoredHandoff]) -> Path:
        handoff_id = records[0].record.handoff_id
        archive_dir = self.bridge_root / "archive" / handoff_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.chmod(0o700)
        canonical_path = archive_dir / f"{handoff_id}.md"
        for stored in records:
            path = stored.path
            record_text = dump_document(stored.record.to_frontmatter(), stored.record.body)
            if not canonical_path.exists():
                canonical_path.write_text(record_text, encoding="utf-8")
                canonical_path.chmod(0o600)
                if path.exists():
                    path.unlink()
                continue
            canonical_text = canonical_path.read_text(encoding="utf-8")
            if canonical_text == record_text:
                if path.exists():
                    path.unlink()
                continue
            queue_label = path.parent.parent.name
            suffix = stored.record.sender if queue_label == "outgoing" else stored.record.recipient
            alternate_path = archive_dir / f"{handoff_id}.{queue_label}.{suffix}.md"
            alternate_path.write_text(record_text, encoding="utf-8")
            alternate_path.chmod(0o600)
            if path.exists():
                path.unlink()
        return archive_dir

    def append_audit(self, handoff: HandoffRecord, status: str) -> None:
        metadata = ""
        if status == "acknowledged" and handoff.acknowledged_at != "none":
            metadata = f" | ack_source={handoff.acknowledgment_source} acknowledged_at={handoff.acknowledged_at}"
        line = (
            f"- {now_iso()} | {handoff.handoff_id} | {handoff.sender} -> {handoff.recipient} | "
            f"{status} | {handoff.subject}{metadata}\n"
        )
        with self.audit_file.open("a", encoding="utf-8") as handle:
            handle.write(line)
