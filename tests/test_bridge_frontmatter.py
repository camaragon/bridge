from __future__ import annotations

from bridge_core.frontmatter import parse_frontmatter, render_frontmatter


def test_parse_frontmatter_returns_plain_body_when_no_frontmatter() -> None:
    data, body = parse_frontmatter("hello\nworld\n")
    assert data == {}
    assert body == "hello\nworld\n"


def test_parse_frontmatter_normalizes_crlf_input() -> None:
    text = "---\r\nhandoff_id: HND-1\r\nstatus: open\r\n---\r\n\r\nBody\r\n"
    data, body = parse_frontmatter(text)
    assert data["handoff_id"] == "HND-1"
    assert data["status"] == "open"
    assert body == "\nBody\n"


def test_parse_frontmatter_ignores_invalid_non_mapping_lines() -> None:
    text = "---\nstatus: open\nthis is junk\nrecipient: jordan\n---\n\nBody\n"
    data, body = parse_frontmatter(text)
    assert data["status"] == "open"
    assert data["recipient"] == "jordan"
    assert body == "\nBody\n"


def test_render_frontmatter_sanitizes_newlines_in_scalar_and_list_values() -> None:
    rendered = render_frontmatter(
        {
            "status": "open\narchived",
            "related_paths": ["/tmp/a\nstatus: archived"],
        }
    )
    assert "status: open archived" in rendered
    assert "status: archived" not in rendered.splitlines()[1:]
    assert "  - /tmp/a status: archived" in rendered
