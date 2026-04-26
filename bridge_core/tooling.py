from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import parse_frontmatter
from .models import HandoffRecord


@dataclass(slots=True)
class LoadedHandoff:
    path: Path
    record: HandoffRecord
    body: str
    archive_file_count: int = 1
    archive_extra_files: list[str] = field(default_factory=list)


def load_handoff_path(path: Path) -> LoadedHandoff:
    data, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    return LoadedHandoff(path=path, record=HandoffRecord.from_mapping(data, body), body=body)


def summarize_handoffs(paths) -> list[LoadedHandoff]:
    return [load_handoff_path(path) for path in sorted(paths)]


def load_archive_entry(directory: Path) -> LoadedHandoff | None:
    files = sorted(directory.glob("*.md"))
    if not files:
        return None
    item = load_handoff_path(files[0])
    item.archive_file_count = len(files)
    item.archive_extra_files = [str(path) for path in files[1:]]
    return item
