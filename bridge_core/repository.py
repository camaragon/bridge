from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ContextManager

from .events import LifecycleEvent
from .models import HandoffRecord, StoredHandoff


class HandoffNotFoundError(FileNotFoundError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class RevisionConflictError(ValueError):
    pass


class RepositoryConflictError(RuntimeError):
    pass


class BridgeRepository(ABC):
    @abstractmethod
    def create(self, handoff: HandoffRecord, *, idempotency_key: str | None = None) -> HandoffRecord:
        raise NotImplementedError

    @abstractmethod
    def handoff_lock(self, handoff_id: str) -> ContextManager[None]:
        raise NotImplementedError

    @abstractmethod
    def load_records(self, handoff_id: str) -> list[StoredHandoff]:
        raise NotImplementedError

    @abstractmethod
    def list_incoming(self, agent: str) -> list[HandoffRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_records(self, records: list[StoredHandoff]) -> None:
        raise NotImplementedError

    @abstractmethod
    def archive_records(self, records: list[StoredHandoff]) -> Path:
        raise NotImplementedError

    @abstractmethod
    def append_audit(self, handoff: HandoffRecord, status: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def enqueue_event(self, event: LifecycleEvent) -> LifecycleEvent:
        raise NotImplementedError

    @abstractmethod
    def prepare_event(self, event: LifecycleEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    def complete_event(self, event_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_events(self, *, states: set[str] | None = None) -> list[LifecycleEvent]:
        raise NotImplementedError

    @abstractmethod
    def update_event(self, event: LifecycleEvent) -> LifecycleEvent:
        raise NotImplementedError

    @abstractmethod
    def replay_event(self, event_id: str) -> LifecycleEvent:
        raise NotImplementedError

    @abstractmethod
    def resolve_transaction(self, event_id: str, *, action: str, reason: str) -> LifecycleEvent:
        raise NotImplementedError
