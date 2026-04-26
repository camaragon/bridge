from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from .models import HandoffRecord, StoredHandoff


class HandoffNotFoundError(FileNotFoundError):
    pass


class BridgeRepository(ABC):
    @abstractmethod
    def create(self, handoff: HandoffRecord) -> HandoffRecord:
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
