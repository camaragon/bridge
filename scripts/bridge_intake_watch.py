#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bridge_core.auth import AuthenticationError, require_agent_token
from bridge_core.runtime import env_key_for_agent, normalize_agent_id
from bridge_wrapper_common import api_request, token_config_path


DEFAULT_POLL_INTERVAL = 60.0
DEFAULT_NOTIFY_HOST = '127.0.0.1'
DEFAULT_NOTIFY_PORT = 0
DEFAULT_NOTIFY_PATH = '/notify'
LIFECYCLE_NOTIFY_TRIGGERS = {'handoff_closed', 'handoff_blocked'}
EVENT_COMMAND_TIMEOUT_SECONDS = 30.0


def _event_command_env_var(agent: str) -> str:
    return env_key_for_agent('BRIDGE_NOTIFY_EVENT_COMMAND_', agent)


def _intake_event_command_env_var(agent: str) -> str:
    return env_key_for_agent('BRIDGE_INTAKE_EVENT_COMMAND_', agent)


class IntakeNotifyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        agent: str,
        dry_run: bool,
        event_command: str | None = None,
        intake_event_command: str | None = None,
    ):
        super().__init__(server_address, IntakeNotifyHandler)
        self.agent = agent
        self.dry_run = dry_run
        self.event_command = event_command or None
        self.intake_event_command = intake_event_command or None


class IntakeNotifyHandler(BaseHTTPRequestHandler):
    server: IntakeNotifyServer
    protocol_version = 'HTTP/1.1'

    def do_GET(self) -> None:
        if self.path.rstrip('/') == '/health':
            self._send_json(HTTPStatus.OK, {'ok': True, 'service': 'bridge-intake-watch', 'agent': self.server.agent})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {'error': 'not_found', 'detail': 'unknown endpoint'})

    def do_POST(self) -> None:
        if self.path.rstrip('/') != DEFAULT_NOTIFY_PATH:
            self._send_json(HTTPStatus.NOT_FOUND, {'error': 'not_found', 'detail': 'unknown endpoint'})
            return
        try:
            self._authenticate()
            event = self._read_json_body()
            actions = handle_notify_event(
                self.server.agent,
                event,
                dry_run=self.server.dry_run,
                event_command=self.server.event_command,
                intake_event_command=self.server.intake_event_command,
            )
        except AuthenticationError as exc:
            self._send_json(HTTPStatus.UNAUTHORIZED, {'error': 'unauthorized', 'detail': str(exc)})
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {'error': 'bad_request', 'detail': str(exc)})
            return
        except SystemExit as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {'error': 'intake_failed', 'detail': str(exc)})
            return
        payload = {'agent': self.server.agent, 'actions': actions, 'trigger': 'notify', 'event': event}
        _emit(payload)
        self._send_json(HTTPStatus.OK, payload)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authenticate(self) -> None:
        token = self.headers.get('Authorization', '')
        if token.startswith('Bearer '):
            token = token[len('Bearer ') :].strip()
        elif token:
            raise AuthenticationError('unsupported authorization scheme')
        else:
            token = self.headers.get('X-Bridge-Token', '').strip()
        require_agent_token(self.server.agent, token, token_config_path())

    def _read_json_body(self) -> dict[str, Any]:
        length_header = self.headers.get('Content-Length', '').strip()
        if not length_header:
            return {}
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ValueError('invalid content length') from exc
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('invalid json body') from exc
        if not isinstance(payload, dict):
            raise ValueError('json body must be an object')
        return payload

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, sort_keys=True).encode('utf-8')
        self.send_response(int(status))
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)


def build_notify_server(
    *,
    agent: str,
    host: str = DEFAULT_NOTIFY_HOST,
    port: int = DEFAULT_NOTIFY_PORT,
    dry_run: bool = False,
    event_command: str | None = None,
    intake_event_command: str | None = None,
) -> IntakeNotifyServer:
    return IntakeNotifyServer(
        (host, port),
        agent=agent,
        dry_run=dry_run,
        event_command=event_command,
        intake_event_command=intake_event_command,
    )


def list_active_handoffs(agent: str) -> list[dict[str, Any]]:
    payload = api_request(agent, 'GET', '/v1/handoffs?active_only=true')
    items = payload.get('items', [])
    if not isinstance(items, list):
        raise SystemExit('bridge API returned invalid handoff list payload')
    normalized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized.append(item)
    return normalized


def acknowledge_open_handoff(agent: str, handoff_id: str) -> dict[str, Any]:
    payload = api_request(agent, 'POST', f'/v1/handoffs/{handoff_id}/ack', payload={'ack_source': 'auto'})
    if not isinstance(payload, dict):
        raise SystemExit(f'bridge API returned invalid ack payload for {handoff_id}')
    return payload


def _resolve_event_command(agent: str, event_command: str | None = None) -> str | None:
    if event_command:
        command = event_command.strip()
        return command or None
    scoped = os.getenv(_event_command_env_var(agent), '').strip()
    if scoped:
        return scoped
    generic = os.getenv('BRIDGE_NOTIFY_EVENT_COMMAND', '').strip()
    return generic or None


def _resolve_intake_event_command(agent: str, intake_event_command: str | None = None) -> str | None:
    if intake_event_command:
        command = intake_event_command.strip()
        return command or None
    scoped = os.getenv(_intake_event_command_env_var(agent), '').strip()
    if scoped:
        return scoped
    generic = os.getenv('BRIDGE_INTAKE_EVENT_COMMAND', '').strip()
    return generic or None


def _run_event_command(
    agent: str,
    event: dict[str, Any],
    *,
    event_command: str,
    env_prefix: str = 'BRIDGE_NOTIFY',
    action: str = 'event_command',
    raise_on_failure: bool = True,
) -> dict[str, Any]:
    handoff_id = str(event.get('handoff_id', '') or '')
    trigger = str(event.get('trigger', '') or '')
    argv = shlex.split(event_command)
    if not argv:
        raise SystemExit('event command resolved to empty argv')
    env = os.environ.copy()
    env.update({
        f'{env_prefix}_AGENT': agent,
        f'{env_prefix}_TRIGGER': trigger,
        f'{env_prefix}_HANDOFF_ID': handoff_id,
        f'{env_prefix}_EVENT_JSON': json.dumps(event, sort_keys=True),
    })
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(event, sort_keys=True),
            text=True,
            capture_output=True,
            timeout=EVENT_COMMAND_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        detail = f'timed out after {EVENT_COMMAND_TIMEOUT_SECONDS:g}s'
        if raise_on_failure:
            raise SystemExit(f'event command failed for {trigger or "unknown"}: {detail}') from exc
        return {
            'agent': agent,
            'handoff_id': handoff_id,
            'trigger': trigger,
            'action': action,
            'exit_code': None,
            'error': detail,
        }
    if completed.returncode != 0:
        stderr = (completed.stderr or '').strip()
        stdout = (completed.stdout or '').strip()
        detail = stderr or stdout or f'exit {completed.returncode}'
        if raise_on_failure:
            raise SystemExit(f'event command failed for {trigger or "unknown"}: {detail}')
        return {
            'agent': agent,
            'handoff_id': handoff_id,
            'trigger': trigger,
            'action': action,
            'exit_code': completed.returncode,
            'error': detail,
        }
    return {
        'agent': agent,
        'handoff_id': handoff_id,
        'trigger': trigger,
        'action': action,
        'exit_code': completed.returncode,
    }


def _build_acknowledged_event(agent: str, source: dict[str, Any], acked: dict[str, Any]) -> dict[str, Any]:
    return {
        'trigger': 'handoff_acknowledged',
        'handoff_id': str(acked.get('handoff_id', '') or source.get('handoff_id', '') or ''),
        'sender': str(acked.get('sender', '') or source.get('sender', '') or ''),
        'recipient': str(acked.get('recipient', '') or source.get('recipient', '') or agent),
        'actor': agent,
        'status': str(acked.get('status', '') or 'acknowledged'),
        'subject': str(acked.get('subject', '') or source.get('subject', '') or ''),
        'acknowledgment_source': str(acked.get('acknowledgment_source', '') or 'auto'),
    }


def _run_intake_event_command(agent: str, source: dict[str, Any], acked: dict[str, Any], *, intake_event_command: str) -> dict[str, Any]:
    event = _build_acknowledged_event(agent, source, acked)
    return _run_event_command(
        agent,
        event,
        event_command=intake_event_command,
        env_prefix='BRIDGE_INTAKE',
        action='intake_event_command',
        raise_on_failure=False,
    )



def handle_notify_event(
    agent: str,
    event: dict[str, Any],
    *,
    dry_run: bool = False,
    event_command: str | None = None,
    intake_event_command: str | None = None,
) -> list[dict[str, Any]]:
    trigger = str(event.get('trigger', '') or '')
    if trigger in LIFECYCLE_NOTIFY_TRIGGERS:
        resolved_command = _resolve_event_command(agent, event_command)
        if not resolved_command:
            return []
        if dry_run:
            return [{
                'agent': agent,
                'handoff_id': str(event.get('handoff_id', '') or ''),
                'trigger': trigger,
                'action': 'would_run_event_command',
            }]
        return [_run_event_command(agent, event, event_command=resolved_command)]
    return intake_once(
        agent,
        dry_run=dry_run,
        intake_event_command=intake_event_command,
    )


def intake_once(agent: str, *, dry_run: bool = False, intake_event_command: str | None = None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    resolved_intake_command = _resolve_intake_event_command(agent, intake_event_command)
    for item in list_active_handoffs(agent):
        handoff_id = str(item.get('handoff_id', '') or '')
        if not handoff_id:
            continue
        if str(item.get('recipient', '') or '') != agent:
            continue
        if str(item.get('status', '') or '') != 'open':
            continue
        subject = str(item.get('subject', '') or '')
        if dry_run:
            actions.append({
                'agent': agent,
                'handoff_id': handoff_id,
                'status': 'open',
                'subject': subject,
                'action': 'would_acknowledge',
            })
            if resolved_intake_command:
                actions.append({
                    'agent': agent,
                    'handoff_id': handoff_id,
                    'trigger': 'handoff_acknowledged',
                    'action': 'would_run_intake_event_command',
                })
            continue
        acked = acknowledge_open_handoff(agent, handoff_id)
        actions.append({
            'agent': agent,
            'handoff_id': handoff_id,
            'status': str(acked.get('status', '')),
            'ack_source': str(acked.get('acknowledgment_source', '')),
            'subject': subject,
            'action': 'acknowledged',
        })
        if resolved_intake_command:
            actions.append(_run_intake_event_command(agent, item, acked, intake_event_command=resolved_intake_command))
    return actions

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Poll the Bridge API and promptly acknowledge new handoffs for one agent.')
    parser.add_argument('--agent', required=True)
    parser.add_argument('--poll-interval', type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument('--once', action='store_true', help='run one intake pass and exit')
    parser.add_argument('--dry-run', action='store_true', help='report open handoffs without acknowledging them')
    parser.add_argument('--listen', action='store_true', help='serve an immediate notify endpoint in addition to the polling loop')
    parser.add_argument('--host', default=DEFAULT_NOTIFY_HOST, help='host to bind the notify endpoint to')
    parser.add_argument('--port', type=int, default=DEFAULT_NOTIFY_PORT, help='port to bind the notify endpoint to')
    parser.add_argument(
        '--event-command',
        default='',
        help='optional command to run for handoff_closed/handoff_blocked lifecycle events; receives event JSON on stdin',
    )
    parser.add_argument(
        '--intake-event-command',
        default='',
        help='optional command to run after auto-acknowledging a new handoff; receives handoff_acknowledged JSON on stdin',
    )
    return parser.parse_args()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _run_poll_loop(
    agent: str,
    *,
    dry_run: bool,
    once: bool,
    poll_interval: float,
    intake_event_command: str | None = None,
) -> None:
    while True:
        actions = intake_once(agent, dry_run=dry_run, intake_event_command=intake_event_command)
        _emit({
            'agent': agent,
            'checked_at': int(time.time()),
            'actions': actions,
        })
        if once:
            return
        time.sleep(poll_interval)


def main() -> None:
    args = parse_args()
    args.agent = normalize_agent_id(args.agent)
    poll_interval = max(args.poll_interval, 1.0)
    event_command = _resolve_event_command(args.agent, args.event_command)
    intake_event_command = _resolve_intake_event_command(args.agent, args.intake_event_command)
    if args.listen:
        server = build_notify_server(
            agent=args.agent,
            host=args.host,
            port=args.port,
            dry_run=args.dry_run,
            event_command=event_command,
            intake_event_command=intake_event_command,
        )
        _emit({
            'agent': args.agent,
            'notify_url': f'http://{args.host}:{server.server_port}{DEFAULT_NOTIFY_PATH}',
            'health_url': f'http://{args.host}:{server.server_port}/health',
            'mode': 'notify',
            'event_command_configured': bool(event_command),
            'intake_event_command_configured': bool(intake_event_command),
        })
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return
        finally:
            server.server_close()
        return
    _run_poll_loop(
        args.agent,
        dry_run=args.dry_run,
        once=args.once,
        poll_interval=poll_interval,
        intake_event_command=intake_event_command,
    )


if __name__ == '__main__':
    main()

