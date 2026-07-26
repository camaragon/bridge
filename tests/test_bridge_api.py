from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from bridge_core.file_repository import FileBridgeRepository
from bridge_core.frontmatter import parse_frontmatter
from bridge_core.models import CreateHandoffInput
from bridge_core.service import BridgeService
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
                "BRIDGE_TOKEN_AGENT_A=token-agent-a",
                "BRIDGE_TOKEN_AGENT_B=token-agent-b",
                "BRIDGE_TOKEN_AGENT_C=token-agent-c",
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
        token="token-agent-a",
        body={
            "sender": "agent-c",
            "recipient": "agent-c",
            "issue_type": "task",
            "subject": "API flow",
            "requested_action": "Handle the request.",
            "minimal_context": "Server integration test.",
            "related_paths": ["/tmp/demo.md"],
        },
    )

    assert status == 201
    assert created["sender"] == "agent-a"
    assert created["recipient"] == "agent-c"
    handoff_id = str(created["handoff_id"])

    status, agent_a_list = _request(api_server["base_url"], "GET", "/v1/handoffs", token="token-agent-a")
    assert status == 200
    assert [item["handoff_id"] for item in agent_a_list["items"]] == [handoff_id]

    status, agent_c_list = _request(api_server["base_url"], "GET", "/v1/handoffs", token="token-agent-c")
    assert status == 200
    assert [item["handoff_id"] for item in agent_c_list["items"]] == [handoff_id]

    status, agent_b_list = _request(api_server["base_url"], "GET", "/v1/handoffs", token="token-agent-b")
    assert status == 200
    assert agent_b_list["items"] == []

    status, agent_a_get = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-agent-a")
    assert status == 200
    assert agent_a_get["handoff_id"] == handoff_id

    status, forbidden = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-agent-b")
    assert status == 403
    assert forbidden["error"] == "forbidden"

    status, acked = _request(api_server["base_url"], "POST", f"/v1/handoffs/{handoff_id}/ack", token="token-agent-c")
    assert status == 200
    assert acked["status"] == "acknowledged"
    assert acked["acknowledgment_source"] == "manual"
    assert acked["acknowledged_at"] != "none"

    status, blocked = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/block",
        token="token-agent-c",
        body={"outcome": "Waiting on a dependency."},
    )
    assert status == 200
    assert blocked["status"] == "blocked"
    assert blocked["resolution_summary"] == "Waiting on a dependency."

    status, in_progress = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/status",
        token="token-agent-c",
        body={"status": "in_progress"},
    )
    assert status == 200
    assert in_progress["status"] == "in_progress"

    status, closed = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/close",
        token="token-agent-c",
        body={"outcome": "Completed safely."},
    )
    assert status == 200
    assert closed["status"] == "closed"
    assert closed["resolution_summary"] == "Completed safely."

    status, active_items = _request(api_server["base_url"], "GET", "/v1/handoffs", token="token-agent-c")
    assert status == 200
    assert active_items["items"] == []

    status, all_items = _request(
        api_server["base_url"],
        "GET",
        "/v1/handoffs",
        token="token-agent-c",
        query={"active_only": "false"},
    )
    assert status == 200
    assert [item["status"] for item in all_items["items"]] == ["closed"]

    status, archived = _request(api_server["base_url"], "POST", f"/v1/handoffs/{handoff_id}/archive", token="token-agent-c")
    assert status == 200
    assert archived["status"] == "archived"
    assert archived["archive_path"].endswith(f"/archive/{handoff_id}")

    archived_file = api_server["bridge_root"] / "archive" / handoff_id / f"{handoff_id}.md"
    archived_data, archived_body = parse_frontmatter(archived_file.read_text(encoding="utf-8"))
    assert archived_data["status"] == "archived"
    assert archived_data["resolution_summary"] == "Completed safely."
    assert "## Outcome\nCompleted safely." in archived_body


def test_create_rejects_disallowed_route(api_server, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGE_ALLOWED_ROUTES", "agent-a:agent-c,agent-c:agent-a")
    status, payload = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-b",
        body={
            "recipient": "agent-c",
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
        extra_headers={"X-Bridge-Token": "token-agent-c"},
    )
    assert status == 200
    assert payload["actor"] == "agent-c"


def test_create_triggers_immediate_notify_when_recipient_endpoint_is_configured(api_server, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGE_WRAPPER_API_URL", api_server["base_url"])
    monkeypatch.setenv("BRIDGE_API_CONFIG", str(api_server["bridge_root"].parent / "config" / "bridge_api.env"))
    notify_server = bridge_intake_watch.build_notify_server(agent="agent-c", host="127.0.0.1", port=0)
    thread = threading.Thread(target=notify_server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_AGENT_C", f"http://127.0.0.1:{notify_server.server_port}/notify")
    try:
        status, created = _request(
            api_server["base_url"],
            "POST",
            "/v1/handoffs",
            token="token-agent-a",
            body={
                "recipient": "agent-c",
                "issue_type": "task",
                "subject": "Push me now",
                "requested_action": "Ack immediately.",
                "minimal_context": "Configured notify endpoint should fire on create.",
            },
        )
        assert status == 201
        handoff_id = str(created["handoff_id"])

        status, handoff = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-agent-c")
        assert status == 200
        assert handoff["status"] == "acknowledged"
        assert handoff["acknowledgment_source"] == "auto"
        assert handoff["acknowledged_at"] != "none"
    finally:
        notify_server.shutdown()
        thread.join(timeout=5)
        notify_server.server_close()


def test_create_keeps_open_handoff_when_immediate_notify_fails(api_server, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_AGENT_C", "http://127.0.0.1:1/notify")

    status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-a",
        body={
            "recipient": "agent-c",
            "issue_type": "task",
            "subject": "Fallback remains",
            "requested_action": "Polling backup should still work.",
            "minimal_context": "Push delivery can fail without breaking create.",
        },
    )

    assert status == 201
    handoff_id = str(created["handoff_id"])

    status, handoff = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-agent-c")
    assert status == 200
    assert handoff["status"] == "open"


def test_immediate_delivery_updates_loaded_event_without_rescanning(tmp_path: Path, monkeypatch) -> None:
    repository = FileBridgeRepository(tmp_path / "bridge")
    service = BridgeService(repository)
    created = service.create_handoff(
        CreateHandoffInput(
            sender="agent-a",
            recipient="agent-c",
            issue_type="task",
            subject="Single outbox scan",
            requested_action="Deliver once.",
            minimal_context="Regression coverage for API-triggered delivery.",
        )
    )
    context = bridge_api_server.ApiContext(
        bridge_root=repository.bridge_root,
        config_path=None,
        service=service,
    )
    original_list_events = repository.list_events
    scans = 0

    def counted_list_events(*, states=None):
        nonlocal scans
        scans += 1
        return original_list_events(states=states)

    monkeypatch.setattr(repository, "list_events", counted_list_events)
    monkeypatch.setattr(bridge_api_server, "_notify_agent", lambda *args, **kwargs: (True, "delivered"))

    bridge_api_server._deliver_pending_for_handoff(context, created.handoff_id, created.revision)

    assert scans == 1
    assert original_list_events()[0].delivery_state == "delivered"


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
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_AGENT_A", "http://notify.example/agent-a")

    status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-a",
        body={
            "recipient": "agent-c",
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
        token="token-agent-c",
        body={"outcome": "Completed safely."},
    )

    assert status == 200
    assert closed["status"] == "closed"
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request["url"] == "http://notify.example/agent-a"
    assert request["authorization"] == "Bearer token-agent-a"
    assert request["timeout"] == bridge_api_server.DEFAULT_NOTIFY_TIMEOUT_SECONDS
    assert request["body"] == {
        "trigger": "handoff_closed",
        "handoff_id": handoff_id,
        "sender": "agent-a",
        "recipient": "agent-c",
        "actor": "agent-c",
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
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_AGENT_A", "http://notify.example/agent-a")

    status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-a",
        body={
            "recipient": "agent-c",
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
        token="token-agent-c",
        body={"outcome": "Waiting on dependency."},
    )

    assert status == 200
    assert blocked["status"] == "blocked"
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request["url"] == "http://notify.example/agent-a"
    assert request["authorization"] == "Bearer token-agent-a"
    assert request["timeout"] == bridge_api_server.DEFAULT_NOTIFY_TIMEOUT_SECONDS
    assert request["body"] == {
        "trigger": "handoff_blocked",
        "handoff_id": handoff_id,
        "sender": "agent-a",
        "recipient": "agent-c",
        "actor": "agent-c",
        "status": "blocked",
        "subject": "Block push",
        "resolution_summary": "Waiting on dependency.",
    }


def test_close_keeps_status_update_when_sender_notify_fails(api_server, monkeypatch) -> None:
    def _failing_urlopen(request, timeout=0):
        raise OSError("notify down")

    monkeypatch.setattr(bridge_api_server, "urlopen", _failing_urlopen)
    monkeypatch.setenv("BRIDGE_NOTIFY_URL_AGENT_A", "http://notify.example/agent-a")

    status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-a",
        body={
            "recipient": "agent-c",
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
        token="token-agent-c",
        body={"outcome": "Completed despite notify failure."},
    )

    assert status == 200
    assert closed["status"] == "closed"
    assert closed["resolution_summary"] == "Completed despite notify failure."

    status, fetched = _request(api_server["base_url"], "GET", f"/v1/handoffs/{handoff_id}", token="token-agent-a")
    assert status == 200
    assert fetched["status"] == "closed"
    assert fetched["resolution_summary"] == "Completed despite notify failure."


def test_api_idempotency_conflict_returns_409(api_server) -> None:
    body: dict[str, object] = {
        "recipient": "agent-c",
        "issue_type": "task",
        "subject": "Idempotent API create",
        "requested_action": "Create once.",
        "minimal_context": "Concurrent-safe create.",
    }
    first_status, first = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-a",
        body=body,
        extra_headers={"Idempotency-Key": "api-create-1"},
    )
    duplicate_status, duplicate = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-a",
        body=body,
        extra_headers={"Idempotency-Key": "api-create-1"},
    )
    conflict_status, conflict = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-a",
        body={**body, "subject": "Different payload"},
        extra_headers={"Idempotency-Key": "api-create-1"},
    )

    assert first_status == duplicate_status == 201
    assert duplicate["handoff_id"] == first["handoff_id"]
    assert conflict_status == 409
    assert conflict["error"] == "idempotency_conflict"


def test_api_if_match_stale_revision_returns_409(api_server) -> None:
    _status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-a",
        body={
            "recipient": "agent-c",
            "issue_type": "task",
            "subject": "Revision API",
            "requested_action": "Exercise optimistic concurrency.",
            "minimal_context": "If-Match test.",
        },
    )
    handoff_id = str(created["handoff_id"])
    ack_status, acked = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/ack",
        token="token-agent-c",
        extra_headers={"If-Match": '"0"'},
    )
    stale_status, stale = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/block",
        token="token-agent-c",
        body={"outcome": "Stale update."},
        extra_headers={"If-Match": '"0"'},
    )

    assert ack_status == 200
    assert acked["revision"] == 1
    assert stale_status == 409
    assert stale["error"] == "revision_conflict"


def test_api_role_authorization_rejects_sender_close_and_allows_cancel(api_server) -> None:
    _status, created = _request(
        api_server["base_url"],
        "POST",
        "/v1/handoffs",
        token="token-agent-a",
        body={
            "recipient": "agent-c",
            "issue_type": "task",
            "subject": "Role matrix API",
            "requested_action": "Prove role-specific lifecycle rules.",
            "minimal_context": "Sender may cancel, not close.",
        },
    )
    handoff_id = str(created["handoff_id"])
    close_status, close_error = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/close",
        token="token-agent-a",
        body={"outcome": "Unauthorized close."},
    )
    cancel_status, cancelled = _request(
        api_server["base_url"],
        "POST",
        f"/v1/handoffs/{handoff_id}/cancel",
        token="token-agent-a",
        body={"outcome": "Withdrawn safely."},
    )

    assert close_status == 403
    assert close_error["error"] == "forbidden"
    assert cancel_status == 200
    assert cancelled["status"] == "cancelled"
