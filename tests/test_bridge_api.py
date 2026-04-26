from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from bridge_core.frontmatter import parse_frontmatter
import bridge_api_server
import bridge_intake_watch

@pytest.fixture
def api_server(tmp_path: Path):
    root = tmp_path / "agent-shared"
    bridge_root = root / "bridge"
    config_path = root / "config" / "bridge_api.env"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "BRIDGE_TOKEN_HERMES=token-hermes",
                "BRIDGE_TOKEN_JARVY=token-jarvy",
                "BRIDGE_TOKEN_JORDAN=token-jordan",
                "",
            ]
        ),
        encoding="utf-8",
    )

    server = bridge_api_server.build_server(
        host="127.0.0.1",
        port=0,
        bridge_root=bridge_root,
        config_path=config_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "bridge_root": bridge_root,
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, object] | None = None,
    query: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    url = f"{base_url}{path}"
    if query:
        url += f"?{urlencode(query)}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url, method=method, headers=headers, data=data)
    try:
        with urlopen(request, timeout=5) as response:
            payload = response.read().decode("utf-8")
            return response.status, json.loads(payload)
    except HTTPError as exc:
        payload = exc.read().decode("utf-8")
        return exc.code, json.loads(payload)


def test_health_endpoint_reports_ok(api_server) -> None:
    status, payload = _request(api_server["base_url"], "GET", "/v1/health")

    assert status == 200
    assert payload["ok"] is True
    assert payload["service"] == "bridge-api"


def test_create_list_get_and_lifecycle_flow(api_server) -> None:
    status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-hermes",
        body={
            "sender": "jordan",
            "recipient": "jordan",
            "issue_type": "task",
            "subject": "API flow",
            "requested_action": "Handle the request.",
            "minimal_context": "Server integration test.",
            "related_paths": ["/tmp/demo.md"],
        },
    )

    assert status == 201
    assert created["sender"] == "hermes"
    assert created["recipient"] == "jordan"
    handoff_id = str(created["handoff_id"])

    status, hermes_list = _request(api_server["base_url"], "GET", "/v1/handoffs", token="token-hermes")
    assert status == 200
    assert [item["handoff_id"] for item in hermes_list["items"]] == [handoff_id]

    status, jordan_list = _request(api_server["base_url"], "GET", "/v1/handoffs", token="token-jordan")
    assert status == 200
    assert [item["handoff_id"] for item in jordan_list["items"]] == [handoff_id]

    status, jarvy_list = _request(api_server["base_url"], "GET", "/v1/handoffs", token="token-jarvy")
    assert status == 200
    assert jarvy_list["items"] == []

    status, hermes_get = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-hermes")
    assert status == 200
    assert hermes_get["handoff_id"] == handoff_id

    status, forbidden = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-jarvy")
    assert status == 403
    assert forbidden["error"] == "forbidden"

    status, acked = _request(api_server["base_url"], "POST", f"/v1/handoffs/{handoff_id}/ack", token="token-jordan")
    assert status == 200
    assert acked["status"] == "acknowledged"
    assert acked["acknowledgment_source"] == "manual"
    assert acked["acknowledged_at"] != "none"

    status, blocked = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/block",
        token="token-jordan",
        body={"outcome": "Waiting on a dependency."},
    )
    assert status == 200
    assert blocked["status"] == "blocked"
    assert blocked["resolution_summary"] == "Waiting on a dependency."

    status, in_progress = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/status",
        token="token-jordan",
        body={"status": "in_progress"},
    )
    assert status == 200
    assert in_progress["status"] == "in_progress"

    status, closed = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/close",
        token="token-jordan",
        body={"outcome": "Completed safely."},
    )
    assert status == 200
    assert closed["status"] == "closed"
    assert closed["resolution_summary"] == "Completed safely."

    status, active_items = _request(api_server["base_url"], "GET", "/v1/handoffs", token="token-jordan")
    assert status == 200
    assert active_items["items"] == []

    status, all_items = _request(
        api_server["base_url"],
        "GET",
        "/v1/handoffs",
        token="token-jordan",
        query={"active_only": "false"},
    )
    assert status == 200
    assert [item["status"] for item in all_items["items"]] == ["closed"]

    status, archived = _request(api_server["base_url"], "POST", f"/v1/handoffs/{handoff_id}/archive", token="token-jordan")
    assert status == 200
    assert archived["status"] == "archived"
    assert archived["archive_path"].endswith(f"/archive/{handoff_id}")

    archived_file = api_server["bridge_root"] / "archive" / handoff_id / f"{handoff_id}.md"
    archived_data, archived_body = parse_frontmatter(archived_file.read_text(encoding="utf-8"))
    assert archived_data["status"] == "archived"
    assert archived_data["resolution_summary"] == "Completed safely."
    assert "## Outcome\nCompleted safely." in archived_body


def test_create_rejects_disallowed_route(api_server) -> None:
    status, payload = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-jarvy",
        body={
            "recipient": "jordan",
            "issue_type": "task",
            "subject": "Denied route",
            "requested_action": "Nope",
            "minimal_context": "Blocked by policy.",
        },
    )

    assert status == 403
    assert payload["error"] == "forbidden"
    assert "route not allowed" in str(payload["detail"])


def test_bad_auth_is_rejected(api_server) -> None:
    status, payload = _request(api_server["base_url"], "GET", "/v1/handoffs", token="wrong-token")
    assert status == 401
    assert payload["error"] == "unauthorized"

    status, payload = _request(
        api_server["base_url"],
        "GET",
        "/v1/handoffs",
        extra_headers={"X-Bridge-Token": "token-jordan"},
    )
    assert status == 200
    assert payload["actor"] == "jordan"


def test_create_triggers_immediate_notify_when_recipient_endpoint_is_configured(api_server, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGE_WRAPPER_API_URL", api_server["base_url"])
    monkeypatch.setenv("BRIDGE_API_CONFIG", str(api_server["bridge_root"].parent / "config" / "bridge_api.env"))
    notify_server = bridge_intake_watch.build_notify_server(agent="jordan", host="127.0.0.1", port=0)
    thread = threading.Thread(target=notify_server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_JORDAN", f"http://127.0.0.1:{notify_server.server_port}/notify")
    try:
        status, created = _request(
            api_server["base_url"],
            "POST",
            "/v1/handoffs",
            token="token-hermes",
            body={
                "recipient": "jordan",
                "issue_type": "task",
                "subject": "Push me now",
                "requested_action": "Ack immediately.",
                "minimal_context": "Configured notify endpoint should fire on create.",
            },
        )
        assert status == 201
        handoff_id = str(created["handoff_id"])

        status, handoff = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-jordan")
        assert status == 200
        assert handoff["status"] == "acknowledged"
        assert handoff["acknowledgment_source"] == "auto"
        assert handoff["acknowledged_at"] != "none"
    finally:
        notify_server.shutdown()
        thread.join(timeout=5)
        notify_server.server_close()


def test_create_keeps_open_handoff_when_immediate_notify_fails(api_server, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_JORDAN", "http://127.0.0.1:1/notify")

    status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-hermes",
        body={
            "recipient": "jordan",
            "issue_type": "task",
            "subject": "Fallback remains",
            "requested_action": "Polling backup should still work.",
            "minimal_context": "Push delivery can fail without breaking create.",
        },
    )

    assert status == 201
    handoff_id = str(created["handoff_id"])

    status, handoff = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-jordan")
    assert status == 200
    assert handoff["status"] == "open"


def test_close_notifies_sender_with_lifecycle_payload(api_server, monkeypatch) -> None:
    captured_requests: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _fake_urlopen(request, timeout=0):
        captured_requests.append(
            {
                "url": request.full_url,
                "authorization": request.headers.get("Authorization"),
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _FakeResponse()

    monkeypatch.setattr(bridge_api_server, "urlopen", _fake_urlopen)
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_HERMES", "http://notify.example/hermes")

    status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-hermes",
        body={
            "recipient": "jordan",
            "issue_type": "task",
            "subject": "Close push",
            "requested_action": "Notify sender on close.",
            "minimal_context": "Exercise close lifecycle push.",
        },
    )
    assert status == 201
    handoff_id = str(created["handoff_id"])
    captured_requests.clear()

    status, closed = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/close",
        token="token-jordan",
        body={"outcome": "Completed safely."},
    )

    assert status == 200
    assert closed["status"] == "closed"
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request["url"] == "http://notify.example/hermes"
    assert request["authorization"] == "Bearer token-hermes"
    assert request["timeout"] == bridge_api_server.DEFAULT_NOTIFY_TIMEOUT_SECONDS
    assert request["body"] == {
        "trigger": "handoff_closed",
        "handoff_id": handoff_id,
        "sender": "hermes",
        "recipient": "jordan",
        "actor": "jordan",
        "status": "closed",
        "subject": "Close push",
        "resolution_summary": "Completed safely.",
    }


def test_block_notifies_sender_with_lifecycle_payload(api_server, monkeypatch) -> None:
    captured_requests: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _fake_urlopen(request, timeout=0):
        captured_requests.append(
            {
                "url": request.full_url,
                "authorization": request.headers.get("Authorization"),
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _FakeResponse()

    monkeypatch.setattr(bridge_api_server, "urlopen", _fake_urlopen)
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_HERMES", "http://notify.example/hermes")

    status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-hermes",
        body={
            "recipient": "jordan",
            "issue_type": "task",
            "subject": "Block push",
            "requested_action": "Notify sender on block.",
            "minimal_context": "Exercise block lifecycle push.",
        },
    )
    assert status == 201
    handoff_id = str(created["handoff_id"])
    captured_requests.clear()

    status, blocked = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/block",
        token="token-jordan",
        body={"outcome": "Waiting on dependency."},
    )

    assert status == 200
    assert blocked["status"] == "blocked"
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request["url"] == "http://notify.example/hermes"
    assert request["authorization"] == "Bearer token-hermes"
    assert request["timeout"] == bridge_api_server.DEFAULT_NOTIFY_TIMEOUT_SECONDS
    assert request["body"] == {
        "trigger": "handoff_blocked",
        "handoff_id": handoff_id,
        "sender": "hermes",
        "recipient": "jordan",
        "actor": "jordan",
        "status": "blocked",
        "subject": "Block push",
        "resolution_summary": "Waiting on dependency.",
    }


def test_close_keeps_status_update_when_sender_notify_fails(api_server, monkeypatch) -> None:
    def _failing_urlopen(request, timeout=0):
        raise OSError("notify down")

    monkeypatch.setattr(bridge_api_server, "urlopen", _failing_urlopen)
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_HERMES", "http://notify.example/hermes")

    status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-hermes",
        body={
            "recipient": "jordan",
            "issue_type": "task",
            "subject": "Close fallback",
            "requested_action": "Do not fail close when notify fails.",
            "minimal_context": "Lifecycle notify should be best-effort.",
        },
    )
    assert status == 201
    handoff_id = str(created["handoff_id"])

    status, closed = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/close",
        token="token-jordan",
        body={"outcome": "Completed despite notify failure."},
    )

    assert status == 200
    assert closed["status"] == "closed"
    assert closed["resolution_summary"] == "Completed despite notify failure."

    status, fetched = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-hermes")
    assert status == 200
    assert fetched["status"] == "closed"
    assert fetched["resolution_summary"] == "Completed despite notify failure."
