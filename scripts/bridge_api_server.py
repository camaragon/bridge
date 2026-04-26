#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bridge_core.auth import AuthenticationError, load_agent_tokens, resolve_agent_from_token
from bridge_core.file_repository import FileBridgeRepository
from bridge_core.frontmatter import parse_frontmatter
from bridge_core.models import CreateHandoffInput, DEFAULT_RESPONSE_FORMAT, HandoffRecord
from bridge_core.policy import AccessPolicyError, BridgePolicyError, RoutePolicyError, StatusPolicyError, is_active_status, require_actor_access
from bridge_core.repository import HandoffNotFoundError
from bridge_core.service import BridgeService

DEFAULT_ROOT = Path(os.environ.get("BRIDGE_PROJECT_ROOT", str(SCRIPT_ROOT)))
DEFAULT_BRIDGE_ROOT = DEFAULT_ROOT / "bridge"
DEFAULT_CONFIG_PATH = DEFAULT_ROOT / "config" / "bridge_api.env"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8427
DEFAULT_NOTIFY_TIMEOUT_SECONDS = 1.0


@dataclass(slots=True)
class ApiContext:
    bridge_root: Path
    config_path: Path | None
    service: BridgeService


class BridgeApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], context: ApiContext):
        super().__init__(server_address, BridgeApiHandler)
        self.context = context


class BridgeApiHandler(BaseHTTPRequestHandler):
    server: BridgeApiServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if method == "GET" and path == "/v1/health":
                self._send_json(HTTPStatus.OK, {"ok": True, "service": "bridge-api"})
                return

            actor = self._authenticate()
            if method == "POST" and path == "/v1/handoffs":
                body = self._read_json_body()
                self._handle_create(actor, body)
                return
            if method == "GET" and path == "/v1/handoffs":
                query = parse_qs(parsed.query)
                active_only = _parse_active_only(query)
                self._handle_list(actor, active_only=active_only)
                return

            route = _match_handoff_route(path)
            if route is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "unknown endpoint")

            handoff_id, action = route
            if method == "GET" and action is None:
                self._handle_get(actor, handoff_id)
                return
            if method == "POST" and action in {"ack", "block", "close", "archive", "status"}:
                body = self._read_json_body(required=False)
                self._handle_action(actor, handoff_id, action, body)
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "unknown endpoint")
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.error, "detail": exc.detail})
        except Exception as exc:  # pragma: no cover - defensive guard
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "detail": str(exc)})

    def _authenticate(self) -> str:
        token = self.headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[len("Bearer ") :].strip()
        elif token:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthorized", "unsupported authorization scheme")
        else:
            token = self.headers.get("X-Bridge-Token", "").strip()
        try:
            return resolve_agent_from_token(token, self.server.context.config_path)
        except AuthenticationError as exc:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthorized", str(exc)) from exc

    def _read_json_body(self, *, required: bool = True) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            if required:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "request body is required")
            return {}
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "invalid content length") from exc
        raw = self.rfile.read(length)
        if not raw:
            if required:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "request body is required")
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "invalid json body") from exc
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "json body must be an object")
        return payload

    def _handle_create(self, actor: str, body: dict[str, Any]) -> None:
        sender = str(body.get("sender", actor) or actor)
        if sender != actor:
            body = dict(body)
            body["sender"] = actor
        try:
            request = CreateHandoffInput(
                sender=actor,
                recipient=str(_require_field(body, "recipient")),
                issue_type=str(_require_field(body, "issue_type")),
                subject=str(_require_field(body, "subject")),
                requested_action=str(_require_field(body, "requested_action")),
                minimal_context=str(_require_field(body, "minimal_context")),
                handoff_kind=str(body.get("handoff_kind", "request") or "request"),
                priority=str(body.get("priority", "medium") or "medium"),
                risk_level=str(body.get("risk_level", "low") or "low"),
                due_at=str(body.get("due_at", "none") or "none"),
                approval_needed=bool(body.get("approval_needed", False)),
                approval_context=str(body.get("approval_context", "none") or "none"),
                response_format=str(body.get("response_format") or DEFAULT_RESPONSE_FORMAT),
                related_paths=_coerce_string_list(body.get("related_paths", []), field_name="related_paths"),
                constraints=str(body.get("constraints", "- none") or "- none"),
            )
            record = self.server.context.service.create_handoff(request)
        except RoutePolicyError as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, "forbidden", str(exc)) from exc
        except (BridgePolicyError, ValueError, TypeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", str(exc)) from exc
        _notify_agent(
            self.server.context,
            target_agent=record.recipient,
            payload={
                "handoff_id": record.handoff_id,
                "recipient": record.recipient,
                "sender": record.sender,
                "subject": record.subject,
                "trigger": "handoff_created",
            },
        )
        self._send_json(HTTPStatus.CREATED, _record_to_dict(record))

    def _handle_list(self, actor: str, *, active_only: bool) -> None:
        items = [_record_to_dict(record) for record in _list_visible_handoffs(self.server.context, actor=actor, active_only=active_only)]
        self._send_json(HTTPStatus.OK, {"actor": actor, "items": items})

    def _handle_get(self, actor: str, handoff_id: str) -> None:
        try:
            record = self.server.context.service.get_handoff(handoff_id, actor=actor)
        except HandoffNotFoundError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", str(exc)) from exc
        except AccessPolicyError as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, "forbidden", str(exc)) from exc
        self._send_json(HTTPStatus.OK, _record_to_dict(record))

    def _handle_action(self, actor: str, handoff_id: str, action: str, body: dict[str, Any]) -> None:
        try:
            if action == "archive":
                archive_dir = self.server.context.service.archive_handoff(handoff_id, actor=actor)
                record = self.server.context.service.get_handoff(handoff_id, actor=actor)
                payload = _record_to_dict(record)
                payload["archive_path"] = str(archive_dir)
                self._send_json(HTTPStatus.OK, payload)
                return
            if action == "status":
                status_name = str(_require_field(body, "status"))
            else:
                status_name = {
                    "ack": "acknowledged",
                    "block": "blocked",
                    "close": "closed",
                }[action]
            outcome = str(body.get("outcome", "") or "")
            acknowledgment_source = None
            if status_name == "acknowledged":
                acknowledgment_source = str(body.get("ack_source") or body.get("acknowledgment_source") or "manual")
            record = self.server.context.service.set_status(
                handoff_id,
                actor=actor,
                status=status_name,
                outcome=outcome,
                acknowledgment_source=acknowledgment_source,
            )
        except HandoffNotFoundError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", str(exc)) from exc
        except AccessPolicyError as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, "forbidden", str(exc)) from exc
        except StatusPolicyError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", str(exc)) from exc
        if action in {"close", "block"}:
            _notify_agent(
                self.server.context,
                target_agent=record.sender,
                payload={
                    "trigger": f"handoff_{record.status}",
                    "handoff_id": record.handoff_id,
                    "sender": record.sender,
                    "recipient": record.recipient,
                    "actor": actor,
                    "status": record.status,
                    "subject": record.subject,
                    "resolution_summary": record.resolution_summary,
                },
            )
        self._send_json(HTTPStatus.OK, _record_to_dict(record))

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, error: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


def _match_handoff_route(path: str) -> tuple[str, str | None] | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) == 3 and parts[:2] == ["v1", "handoffs"]:
        return parts[2], None
    if len(parts) == 4 and parts[:2] == ["v1", "handoffs"]:
        return parts[2], parts[3]
    return None


def _parse_active_only(query: dict[str, list[str]]) -> bool:
    raw = (query.get("active_only") or ["true"])[0].strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _require_field(body: dict[str, Any], field_name: str) -> Any:
    value = body.get(field_name)
    if value is None:
        raise ValueError(f"missing required field: {field_name}")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"missing required field: {field_name}")
    return value


def _coerce_string_list(value: Any, *, field_name: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return [str(item) for item in value]


def _record_to_dict(record: HandoffRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["related_paths"] = list(record.related_paths)
    return payload


def _list_visible_handoffs(context: ApiContext, *, actor: str, active_only: bool) -> list[HandoffRecord]:
    candidates: dict[str, HandoffRecord] = {}
    visible_roots = [
        context.bridge_root / "incoming" / actor,
        context.bridge_root / "outgoing" / actor,
    ]
    for root in visible_roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md")):
            record = _record_from_path(path)
            if active_only and not is_active_status(record.status):
                continue
            candidates.setdefault(record.handoff_id, record)
    if not active_only:
        archive_root = context.bridge_root / "archive"
        if archive_root.exists():
            for path in sorted(archive_root.glob("*/*.md")):
                record = _record_from_path(path)
                try:
                    require_actor_access(actor, record.sender, record.recipient)
                except AccessPolicyError:
                    continue
                candidates.setdefault(record.handoff_id, record)
    return sorted(candidates.values(), key=lambda record: (record.updated_at, record.handoff_id), reverse=True)


def _record_from_path(path: Path) -> HandoffRecord:
    data, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    return HandoffRecord.from_mapping(data, body)


def _notify_agent(context: ApiContext, *, target_agent: str, payload: dict[str, Any]) -> None:
    notify_url = os.environ.get(f"BRIDGE_NOTIFY_URL_{target_agent.upper()}", "").strip()
    if not notify_url:
        return
    token = load_agent_tokens(context.config_path).get(target_agent, "")
    if not token:
        return
    request = Request(
        notify_url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=DEFAULT_NOTIFY_TIMEOUT_SECONDS):
            return
    except Exception:
        return


def build_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    bridge_root: Path | str = DEFAULT_BRIDGE_ROOT,
    config_path: Path | str | None = DEFAULT_CONFIG_PATH,
) -> BridgeApiServer:
    repository = FileBridgeRepository(bridge_root=Path(bridge_root))
    context = ApiContext(
        bridge_root=repository.bridge_root,
        config_path=Path(config_path) if config_path is not None else None,
        service=BridgeService(repository),
    )
    return BridgeApiServer((host, port), context)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge Phase 3 local stdlib API server")
    parser.add_argument("--host", default=os.environ.get("BRIDGE_API_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BRIDGE_API_PORT", str(DEFAULT_PORT))))
    parser.add_argument(
        "--bridge-root",
        default=os.environ.get("BRIDGE_ROOT", str(DEFAULT_BRIDGE_ROOT)),
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("BRIDGE_API_CONFIG", str(DEFAULT_CONFIG_PATH)),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = build_server(
        host=args.host,
        port=args.port,
        bridge_root=args.bridge_root,
        config_path=args.config,
    )
    print(json.dumps({
        "host": args.host,
        "port": server.server_port,
        "bridge_root": str(server.context.bridge_root),
        "config": str(server.context.config_path) if server.context.config_path is not None else None,
    }))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
