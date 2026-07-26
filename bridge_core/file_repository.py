from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterator
import fcntl
import hashlib
import json
import os
import tempfile
import threading

from .events import LifecycleEvent
from .frontmatter import dump_document, parse_frontmatter
from .models import CURRENT_SCHEMA_VERSION, HandoffRecord, StoredHandoff, now_iso
from .repository import (
    BridgeRepository,
    HandoffNotFoundError,
    IdempotencyConflictError,
    RepositoryConflictError,
)


class FileBridgeRepository(BridgeRepository):
    """Crash-safe canonical records with backward-compatible queue views."""

    def __init__(
        self,
        bridge_root: Path | str,
        *,
        fault_injector: Callable[[str], None] | None = None,
        initialize: bool = True,
    ):
        self.bridge_root = Path(bridge_root)
        self.audit_file = self.bridge_root / "audit" / "handoff-log.md"
        self.records_root = self.bridge_root / "records"
        self.locks_root = self.bridge_root / ".locks"
        self.idempotency_root = self.bridge_root / "idempotency"
        self.events_root = self.bridge_root / "outbox" / "events"
        self.transactions_root = self.bridge_root / "transactions"
        self.conflicts_root = self.bridge_root / "audit" / "conflicts"
        self._fault_injector = fault_injector
        self._thread_locks: dict[str, threading.RLock] = {}
        self._thread_locks_guard = threading.Lock()
        self._local = threading.local()
        if initialize:
            self._ensure_layout()

    def _ensure_layout(self) -> None:
        for kind in ("incoming", "outgoing"):
            (self.bridge_root / kind).mkdir(parents=True, exist_ok=True)
        for path in (
            self.bridge_root / "archive",
            self.audit_file.parent,
            self.records_root,
            self.locks_root,
            self.idempotency_root,
            self.events_root,
            self.transactions_root,
            self.conflicts_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)
        if not self.audit_file.exists():
            self._atomic_write_bytes(self.audit_file, b"")

    def _queue_path(self, kind: str, agent: str, handoff_id: str) -> Path:
        return self.bridge_root / kind / agent / f"{handoff_id}.md"

    def _canonical_path(self, handoff_id: str) -> Path:
        return self.records_root / f"{handoff_id}.md"

    def _record_bytes(self, handoff: HandoffRecord) -> bytes:
        return dump_document(handoff.to_frontmatter(), handoff.body).encode("utf-8")

    def _fsync_dir(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write_bytes(self, path: Path, content: bytes, *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            path.chmod(mode)
            self._fsync_dir(path.parent)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temp_path.unlink(missing_ok=True)
            raise

    def _atomic_write_json(self, path: Path, payload: dict[str, object]) -> None:
        self._atomic_write_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    def _write_record(self, path: Path, handoff: HandoffRecord) -> None:
        self._atomic_write_bytes(path, self._record_bytes(handoff))

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _lock_for(self, key: str) -> threading.RLock:
        with self._thread_locks_guard:
            return self._thread_locks.setdefault(key, threading.RLock())

    @contextmanager
    def _file_lock(self, key: str) -> Iterator[None]:
        safe_key = hashlib.sha256(key.encode("utf-8")).hexdigest()
        thread_lock = self._lock_for(safe_key)
        with thread_lock:
            depths = getattr(self._local, "depths", {})
            depth = depths.get(safe_key, 0)
            depths[safe_key] = depth + 1
            self._local.depths = depths
            lock_handle = None
            try:
                if depth == 0:
                    if self.locks_root.exists():
                        lock_path = self.locks_root / f"{safe_key}.lock"
                        lock_handle = lock_path.open("a+b")
                        lock_path.chmod(0o600)
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                depths[safe_key] -= 1
                if depths[safe_key] == 0:
                    depths.pop(safe_key, None)
                    if lock_handle is not None:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                        lock_handle.close()

    def handoff_lock(self, handoff_id: str):
        return self._file_lock(f"handoff:{handoff_id}")

    def _idempotency_index_path(self, sender: str, key_hash: str) -> Path:
        return self.idempotency_root / sender / f"{key_hash}.json"

    def create(self, handoff: HandoffRecord, *, idempotency_key: str | None = None) -> HandoffRecord:
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest() if idempotency_key else ""
        create_lock = f"idempotency:{handoff.sender}:{key_hash}" if key_hash else f"create:{handoff.handoff_id}"
        with self._file_lock(create_lock):
            if key_hash:
                index_path = self._idempotency_index_path(handoff.sender, key_hash)
                if index_path.exists():
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    if index.get("request_hash") != handoff.canonical_request_hash:
                        raise IdempotencyConflictError(
                            f"idempotency key already used by {handoff.sender} with a different request payload"
                        )
                    original_id = str(index["handoff_id"])
                    self.reconcile_handoff(original_id)
                    return self.load_records(original_id)[0].record
                handoff.idempotency_key_hash = key_hash

            with self.handoff_lock(handoff.handoff_id):
                canonical = self._canonical_path(handoff.handoff_id)
                if canonical.exists():
                    raise RepositoryConflictError(f"handoff already exists: {handoff.handoff_id}")
                self._write_record(canonical, handoff)
                if key_hash:
                    self._atomic_write_json(
                        self._idempotency_index_path(handoff.sender, key_hash),
                        {
                            "schema_version": CURRENT_SCHEMA_VERSION,
                            "sender": handoff.sender,
                            "idempotency_key_hash": key_hash,
                            "request_hash": handoff.canonical_request_hash,
                            "handoff_id": handoff.handoff_id,
                            "created_at": handoff.created_at,
                        },
                    )
                outgoing = self._queue_path("outgoing", handoff.sender, handoff.handoff_id)
                incoming = self._queue_path("incoming", handoff.recipient, handoff.handoff_id)
                self._write_record(outgoing, handoff)
                self._fault("after_outgoing_write")
                self._write_record(incoming, handoff)
                self._fault("after_incoming_write")
                self.append_audit(handoff, handoff.status)
                return handoff

    def _load_path(self, path: Path) -> StoredHandoff:
        data, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        return StoredHandoff(path=path, record=HandoffRecord.from_mapping(data, body))

    def load_records(self, handoff_id: str) -> list[StoredHandoff]:
        canonical = self._canonical_path(handoff_id)
        matches: list[Path] = []
        if canonical.exists():
            matches.append(canonical)
        queue_matches = sorted(self.bridge_root.glob(f"incoming/*/{handoff_id}.md"))
        queue_matches += sorted(self.bridge_root.glob(f"outgoing/*/{handoff_id}.md"))
        archive_matches = sorted((self.bridge_root / "archive" / handoff_id).glob("*.md"))
        matches.extend(queue_matches)
        matches.extend(archive_matches)
        if not matches:
            raise HandoffNotFoundError("handoff not found")
        return [self._load_path(path) for path in matches]

    def list_incoming(self, agent: str) -> list[HandoffRecord]:
        return [self._load_path(path).record for path in sorted((self.bridge_root / "incoming" / agent).glob("*.md"))]

    def save_records(self, records: list[StoredHandoff]) -> None:
        if not records:
            return
        handoff_id = records[0].record.handoff_id
        with self.handoff_lock(handoff_id):
            canonical_record = next(
                (item.record for item in records if item.path == self._canonical_path(handoff_id)),
                records[0].record,
            )
            canonical_record.schema_version = CURRENT_SCHEMA_VERSION
            self._write_record(self._canonical_path(handoff_id), canonical_record)
            for stored in records:
                if stored.path == self._canonical_path(handoff_id) or "archive" in stored.path.parts:
                    continue
                stored.record.schema_version = CURRENT_SCHEMA_VERSION
                self._write_record(stored.path, stored.record)

    def archive_records(self, records: list[StoredHandoff]) -> Path:
        handoff_id = records[0].record.handoff_id
        with self.handoff_lock(handoff_id):
            archive_dir = self.bridge_root / "archive" / handoff_id
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_dir.chmod(0o700)
            canonical_record = next(
                (item.record for item in records if item.path == self._canonical_path(handoff_id)),
                records[0].record,
            )
            canonical_path = archive_dir / f"{handoff_id}.md"
            canonical_bytes = self._record_bytes(canonical_record)
            self._atomic_write_bytes(canonical_path, canonical_bytes)
            for stored in records:
                path = stored.path
                if path == self._canonical_path(handoff_id) or "archive" in path.parts:
                    continue
                record_bytes = self._record_bytes(stored.record)
                if record_bytes != canonical_bytes:
                    queue_label = path.parent.parent.name
                    suffix = stored.record.sender if queue_label == "outgoing" else stored.record.recipient
                    alternate = archive_dir / f"{handoff_id}.{queue_label}.{suffix}.md"
                    self._atomic_write_bytes(alternate, record_bytes)
                path.unlink(missing_ok=True)
                self._fsync_dir(path.parent)
            self._canonical_path(handoff_id).unlink(missing_ok=True)
            self._fsync_dir(self.records_root)
            return archive_dir

    def append_audit(self, handoff: HandoffRecord, status: str) -> None:
        metadata = ""
        if status == "acknowledged" and handoff.acknowledged_at != "none":
            metadata = f" | ack_source={handoff.acknowledgment_source} acknowledged_at={handoff.acknowledged_at}"
        metadata += f" | schema={handoff.schema_version} revision={handoff.revision}"
        line = (
            f"- {now_iso()} | {handoff.handoff_id} | {handoff.sender} -> {handoff.recipient} | "
            f"{status} | {handoff.subject}{metadata}\n"
        ).encode("utf-8")
        with self._file_lock("audit"):
            descriptor = os.open(self.audit_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, line)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def append_diagnostic_audit(self, handoff_id: str, event: str, detail: str) -> None:
        line = f"- {now_iso()} | {handoff_id} | system | {event} | {detail}\n".encode("utf-8")
        with self._file_lock("audit"):
            descriptor = os.open(self.audit_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, line)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _event_path(self, event_id: str) -> Path:
        return self.events_root / f"{event_id}.json"

    def prepare_event(self, event: LifecycleEvent) -> None:
        payload = {"phase": "prepared", "event": event.to_dict(), "prepared_at": now_iso()}
        self._atomic_write_json(self.transactions_root / f"{event.event_id}.json", payload)

    def complete_event(self, event_id: str) -> None:
        path = self.transactions_root / f"{event_id}.json"
        path.unlink(missing_ok=True)
        self._fsync_dir(self.transactions_root)

    def enqueue_event(self, event: LifecycleEvent) -> LifecycleEvent:
        with self._file_lock(f"event:{event.event_id}"):
            path = self._event_path(event.event_id)
            if path.exists():
                return LifecycleEvent.from_mapping(json.loads(path.read_text(encoding="utf-8")))
            self._atomic_write_json(path, event.to_dict())
            return event

    def list_events(self, *, states: set[str] | None = None) -> list[LifecycleEvent]:
        events = []
        for path in sorted(self.events_root.glob("*.json")):
            event = LifecycleEvent.from_mapping(json.loads(path.read_text(encoding="utf-8")))
            if states is None or event.delivery_state in states:
                events.append(event)
        return events

    def update_event(self, event: LifecycleEvent) -> LifecycleEvent:
        with self._file_lock(f"event:{event.event_id}"):
            self._atomic_write_json(self._event_path(event.event_id), event.to_dict())
        return event

    def replay_event(self, event_id: str) -> LifecycleEvent:
        with self._file_lock(f"event:{event_id}"):
            path = self._event_path(event_id)
            if not path.exists():
                raise HandoffNotFoundError(f"event not found: {event_id}")
            event = LifecycleEvent.from_mapping(json.loads(path.read_text(encoding="utf-8")))
            event.delivery_state = "pending"
            event.attempts = 0
            event.next_attempt_at = now_iso()
            event.last_error = ""
            self._atomic_write_json(path, event.to_dict())
            return event

    def recover_transactions(self) -> list[str]:
        recovered: list[str] = []
        for path in sorted(self.transactions_root.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            event = LifecycleEvent.from_mapping(dict(payload["event"]))
            try:
                record = self.load_records(event.handoff_id)[0].record
            except HandoffNotFoundError:
                continue
            if record.revision >= event.revision or record.status == event.status:
                self.enqueue_event(event)
                self.complete_event(event.event_id)
                recovered.append(event.event_id)
        return recovered

    def _preserve_conflict(self, path: Path, *, reason: str) -> Path:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        destination = self.conflicts_root / f"{path.stem}.{digest}.{path.parent.parent.name}.md"
        if not destination.exists():
            self._atomic_write_bytes(destination, path.read_bytes())
        self.append_diagnostic_audit(path.stem, "mirror_conflict", f"{reason}; evidence={destination}")
        return destination

    def reconcile_handoff(self, handoff_id: str, *, repair: bool = True) -> list[str]:
        issues: list[str] = []
        with self.handoff_lock(handoff_id):
            records = self.load_records(handoff_id)
            canonical_path = self._canonical_path(handoff_id)
            canonical = next((item for item in records if item.path == canonical_path), None)
            queue_records = [item for item in records if "incoming" in item.path.parts or "outgoing" in item.path.parts]
            if canonical is None:
                if not queue_records:
                    return [f"{handoff_id}: missing canonical record"]
                byte_variants = {item.path.read_bytes() for item in queue_records}
                if len(byte_variants) > 1:
                    if repair:
                        for item in queue_records:
                            self._preserve_conflict(item.path, reason="no canonical record and divergent queue copies")
                    return [f"{handoff_id}: ambiguous divergent v1 queue copies"]
                canonical = queue_records[0]
                if repair:
                    issues.append(f"{handoff_id}: missing canonical record")
                    canonical.record.schema_version = CURRENT_SCHEMA_VERSION
                    self._write_record(canonical_path, canonical.record)

            expected = self._record_bytes(canonical.record)
            expected_paths = [
                self._queue_path("outgoing", canonical.record.sender, handoff_id),
                self._queue_path("incoming", canonical.record.recipient, handoff_id),
            ]
            for path in expected_paths:
                if not path.exists():
                    issues.append(f"{handoff_id}: missing mirror {path.relative_to(self.bridge_root)}")
                    if repair:
                        self._atomic_write_bytes(path, expected)
                    continue
                item = self._load_path(path)
                if item.record.revision != canonical.record.revision:
                    issues.append(
                        f"{handoff_id}: stale revision {path.relative_to(self.bridge_root)}="
                        f"{item.record.revision} canonical={canonical.record.revision}"
                    )
                    if repair:
                        self._preserve_conflict(path, reason="stale mirror revision")
                        self._atomic_write_bytes(path, expected)
                elif path.read_bytes() != expected:
                    issues.append(f"{handoff_id}: divergent mirror {path.relative_to(self.bridge_root)}")
                    if repair:
                        self._preserve_conflict(path, reason="divergent mirror content")
                        self._atomic_write_bytes(path, expected)
            if issues and repair:
                self.append_diagnostic_audit(handoff_id, "reconciled", "; ".join(issues))
        return issues

    def diagnose(self, *, pending_age_seconds: float = 300.0) -> list[str]:
        issues: list[str] = []
        ids = {path.stem for path in self.records_root.glob("*.md")}
        ids.update(path.stem for path in self.bridge_root.glob("incoming/*/*.md"))
        ids.update(path.stem for path in self.bridge_root.glob("outgoing/*/*.md"))
        for handoff_id in sorted(ids):
            issues.extend(self.reconcile_handoff(handoff_id, repair=False))
        now = __import__("time").time()
        for event in self.list_events():
            path = self._event_path(event.event_id)
            if event.delivery_state in {"pending", "retrying"} and now - path.stat().st_mtime > pending_age_seconds:
                issues.append(f"{event.event_id}: pending outbox event beyond threshold")
            if event.delivery_state == "dead_letter":
                issues.append(f"{event.event_id}: dead-letter event")
        for path in sorted(self.idempotency_root.glob("*/*.json")):
            try:
                index = json.loads(path.read_text(encoding="utf-8"))
                record = self.load_records(str(index["handoff_id"]))[0].record
                if record.sender != index.get("sender") or record.canonical_request_hash != index.get("request_hash"):
                    issues.append(f"{path.relative_to(self.bridge_root)}: idempotency index inconsistency")
            except (OSError, ValueError, KeyError, HandoffNotFoundError, json.JSONDecodeError):
                issues.append(f"{path.relative_to(self.bridge_root)}: idempotency index inconsistency")
        for path in sorted(self.transactions_root.glob("*.json")):
            issues.append(f"{path.stem}: incomplete event transaction")
        return issues