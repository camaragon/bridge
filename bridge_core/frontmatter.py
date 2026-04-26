from __future__ import annotations

from collections.abc import Mapping


class FrontmatterError(ValueError):
    pass


def _sanitize_scalar(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    parts = normalized.split("\n---\n", 1)
    if len(parts) != 2:
        raise FrontmatterError("invalid frontmatter block")
    raw, body = parts
    lines = raw.splitlines()[1:]
    data: dict[str, object] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.endswith(":") and ": " not in line:
            key = line[:-1]
            values: list[str] = []
            index += 1
            while index < len(lines) and lines[index].startswith("  - "):
                values.append(lines[index][4:])
                index += 1
            data[key] = values
            continue
        if ": " not in line:
            index += 1
            continue
        key, value = line.split(": ", 1)
        data[key] = value
        index += 1
    return data, body


def render_frontmatter(data: Mapping[str, object]) -> str:
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_sanitize_scalar(item)}")
        else:
            lines.append(f"{key}: {_sanitize_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def dump_document(data: Mapping[str, object], body: str) -> str:
    return render_frontmatter(data) + "\n\n" + body.lstrip("\n")
