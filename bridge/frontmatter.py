"""A small flat frontmatter format, not a YAML implementation."""
from __future__ import annotations

from collections.abc import Mapping

MAX_BYTES = 64 * 1024


def validate_text(text: str) -> str:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("event exceeds 64 KiB; pass a readable file path instead")
    if any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) < 160 for char in text):
        raise ValueError("text must use LF line endings and contain no control characters")
    return text


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    validate_text(text)
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("invalid frontmatter block")
    header, body = text[4:].split("\n---\n", 1)
    data = {}
    for line in header.splitlines():
        if ": " not in line:
            raise ValueError("expected key: value in frontmatter")
        key, value = line.split(": ", 1)
        if not key or key in data:
            raise ValueError("empty or duplicate frontmatter key")
        data[key] = value
    return data, body.lstrip("\n")


def dump_document(data: Mapping[str, str], body: str) -> str:
    for key, value in data.items():
        if any(char in key + value for char in "\r\n"):
            raise ValueError("frontmatter values must be single lines")
    header = "\n".join(f"{key}: {value}" for key, value in data.items())
    return validate_text(f"---\n{header}\n---\n\n{body}")
