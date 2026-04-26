from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import bridge_api_server
import bridge_intake_watch


@pytest.fixture
def api_server(tmp_path: Path):
    root = tmp_path / 'agent-shared'
    bridge_root = root / 'bridge'
    config_path = root / 'config' / 'bridge_api.env'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        '\n'.join([
            'BRIDGE_TOKEN_AGENT_A=token-agent-a',
            'BRIDGE_TOKEN_AGENT_B=token-agent-b',
            'BRIDGE_TOKEN_AGENT_C=token-agent-c',
            '',
        ]),
        encoding='utf-8',
    )

    server = bridge_api_server.build_server(
        host='127.0.0.1',
        port=0,
        bridge_root=bridge_root,
        config_path=config_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            'base_url': f'http://127.0.0.1:{server.server_port}',
            'bridge_root': bridge_root,
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
    token: str,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    data = None
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}',
    }
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = Request(f'{base_url}{path}', method=method, headers=headers, data=data)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8'))


def _configure_watch_env(monkeypatch, bridge_root: Path, base_url: str) -> None:
    monkeypatch.setenv('BRIDGE_WRAPPER_API_URL', base_url)
    monkeypatch.setenv('BRIDGE_API_CONFIG', str(bridge_root.parent / 'config' / 'bridge_api.env'))


def _notify_request(
    notify_url: str,
    *,
    token: str,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    request = Request(
        notify_url,
        method='POST',
        headers={
            'Accept': 'application/json',
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        data=json.dumps(body or {}).encode('utf-8'),
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8'))


def test_intake_once_acknowledges_open_handoff(api_server, monkeypatch) -> None:
    _configure_watch_env(monkeypatch, api_server['bridge_root'], api_server['base_url'])

    status, created = _request(
        api_server['base_url'],
        'POST',
        '/v1/handoffs',
        token='token-agent-a',
        body={
            'recipient': 'agent-c',
            'issue_type': 'task',
            'subject': 'Needs prompt ack',
            'requested_action': 'Acknowledge receipt.',
            'minimal_context': 'Watcher should catch this quickly.',
        },
    )
    assert status == 201
    handoff_id = str(created['handoff_id'])

    actions = bridge_intake_watch.intake_once('agent-c')

    assert actions == [
        {
            'agent': 'agent-c',
            'handoff_id': handoff_id,
            'status': 'acknowledged',
            'ack_source': 'auto',
            'subject': 'Needs prompt ack',
            'action': 'acknowledged',
        }
    ]

    status, payload = _request(api_server['base_url'], 'GET', f'/v1/handoffs/{handoff_id}', token='token-agent-c')
    assert status == 200
    assert payload['status'] == 'acknowledged'
    assert payload['acknowledgment_source'] == 'auto'
    assert payload['acknowledged_at'] != 'none'


def test_intake_once_ignores_non_open_handoffs(api_server, monkeypatch) -> None:
    _configure_watch_env(monkeypatch, api_server['bridge_root'], api_server['base_url'])

    status, created = _request(
        api_server['base_url'],
        'POST',
        '/v1/handoffs',
        token='token-agent-a',
        body={
            'recipient': 'agent-c',
            'issue_type': 'task',
            'subject': 'Already acked',
            'requested_action': 'Nothing to do.',
            'minimal_context': 'Should not be acked twice by intake pass.',
        },
    )
    assert status == 201
    handoff_id = str(created['handoff_id'])

    status, acked = _request(api_server['base_url'], 'POST', f'/v1/handoffs/{handoff_id}/ack', token='token-agent-c', body={})
    assert status == 200
    assert acked['status'] == 'acknowledged'

    actions = bridge_intake_watch.intake_once('agent-c')

    assert actions == []


def test_intake_once_dry_run_reports_without_mutating(api_server, monkeypatch) -> None:
    _configure_watch_env(monkeypatch, api_server['bridge_root'], api_server['base_url'])

    status, created = _request(
        api_server['base_url'],
        'POST',
        '/v1/handoffs',
        token='token-agent-a',
        body={
            'recipient': 'agent-c',
            'issue_type': 'task',
            'subject': 'Dry run only',
            'requested_action': 'Do not mutate.',
            'minimal_context': 'Visibility check only.',
        },
    )
    assert status == 201
    handoff_id = str(created['handoff_id'])

    actions = bridge_intake_watch.intake_once('agent-c', dry_run=True)

    assert actions == [
        {
            'agent': 'agent-c',
            'handoff_id': handoff_id,
            'status': 'open',
            'subject': 'Dry run only',
            'action': 'would_acknowledge',
        }
    ]

    status, payload = _request(api_server['base_url'], 'GET', f'/v1/handoffs/{handoff_id}', token='token-agent-c')
    assert status == 200
    assert payload['status'] == 'open'


def test_notify_server_acknowledges_open_handoff(api_server, monkeypatch) -> None:
    _configure_watch_env(monkeypatch, api_server['bridge_root'], api_server['base_url'])
    notify_server = bridge_intake_watch.build_notify_server(agent='agent-c', host='127.0.0.1', port=0)
    thread = threading.Thread(target=notify_server.serve_forever, daemon=True)
    thread.start()
    try:
        status, created = _request(
            api_server['base_url'],
            'POST',
            '/v1/handoffs',
            token='token-agent-a',
            body={
                'recipient': 'agent-c',
                'issue_type': 'task',
                'subject': 'Notify now',
                'requested_action': 'Immediate acknowledge.',
                'minimal_context': 'Push endpoint should handle this.',
            },
        )
        assert status == 201
        handoff_id = str(created['handoff_id'])

        notify_url = f'http://127.0.0.1:{notify_server.server_port}/notify'
        status, payload = _notify_request(notify_url, token='token-agent-c')

        assert status == 200
        assert payload['agent'] == 'agent-c'
        assert payload['trigger'] == 'notify'
        assert payload['actions'] == [
            {
                'agent': 'agent-c',
                'handoff_id': handoff_id,
                'status': 'acknowledged',
                'ack_source': 'auto',
                'subject': 'Notify now',
                'action': 'acknowledged',
            }
        ]

        status, handoff = _request(api_server['base_url'], 'GET', f'/v1/handoffs/{handoff_id}', token='token-agent-c')
        assert status == 200
        assert handoff['status'] == 'acknowledged'
        assert handoff['acknowledgment_source'] == 'auto'
    finally:
        notify_server.shutdown()
        thread.join(timeout=5)
        notify_server.server_close()


def test_notify_server_rejects_wrong_token(api_server, monkeypatch) -> None:
    _configure_watch_env(monkeypatch, api_server['bridge_root'], api_server['base_url'])
    notify_server = bridge_intake_watch.build_notify_server(agent='agent-c', host='127.0.0.1', port=0)
    thread = threading.Thread(target=notify_server.serve_forever, daemon=True)
    thread.start()
    try:
        status, created = _request(
            api_server['base_url'],
            'POST',
            '/v1/handoffs',
            token='token-agent-a',
            body={
                'recipient': 'agent-c',
                'issue_type': 'task',
                'subject': 'Bad token',
                'requested_action': 'Should stay open.',
                'minimal_context': 'Unauthorized notify must not mutate.',
            },
        )
        assert status == 201
        handoff_id = str(created['handoff_id'])

        notify_url = f'http://127.0.0.1:{notify_server.server_port}/notify'
        status, payload = _notify_request(notify_url, token='wrong-token')

        assert status == 401
        assert payload['error'] == 'unauthorized'

        status, handoff = _request(api_server['base_url'], 'GET', f'/v1/handoffs/{handoff_id}', token='token-agent-c')
        assert status == 200
        assert handoff['status'] == 'open'
    finally:
        notify_server.shutdown()
        thread.join(timeout=5)
        notify_server.server_close()


def test_notify_server_exposes_lifecycle_events_without_mutating_handoff(api_server, monkeypatch) -> None:
    _configure_watch_env(monkeypatch, api_server['bridge_root'], api_server['base_url'])
    notify_server = bridge_intake_watch.build_notify_server(agent='agent-a', host='127.0.0.1', port=0)
    thread = threading.Thread(target=notify_server.serve_forever, daemon=True)
    thread.start()
    try:
        status, created = _request(
            api_server['base_url'],
            'POST',
            '/v1/handoffs',
            token='token-agent-a',
            body={
                'recipient': 'agent-c',
                'issue_type': 'task',
                'subject': 'Lifecycle echo',
                'requested_action': 'Return lifecycle event immediately.',
                'minimal_context': 'Notify listener should expose pushed lifecycle payload.',
            },
        )
        assert status == 201
        handoff_id = str(created['handoff_id'])

        notify_url = f'http://127.0.0.1:{notify_server.server_port}/notify'
        event = {
            'trigger': 'handoff_closed',
            'handoff_id': handoff_id,
            'sender': 'agent-a',
            'recipient': 'agent-c',
            'actor': 'agent-c',
            'status': 'closed',
            'subject': 'Lifecycle echo',
            'resolution_summary': 'Done now.',
        }
        status, payload = _notify_request(notify_url, token='token-agent-a', body=event)

        assert status == 200
        assert payload['agent'] == 'agent-a'
        assert payload['trigger'] == 'notify'
        assert payload['event'] == event
        assert payload['actions'] == []

        status, handoff = _request(api_server['base_url'], 'GET', f'/v1/handoffs/{handoff_id}', token='token-agent-a')
        assert status == 200
        assert handoff['status'] == 'open'
    finally:
        notify_server.shutdown()
        thread.join(timeout=5)
        notify_server.server_close()


def test_notify_server_runs_lifecycle_event_command(api_server, monkeypatch, tmp_path: Path) -> None:
    _configure_watch_env(monkeypatch, api_server['bridge_root'], api_server['base_url'])
    capture_path = tmp_path / 'captured-event.json'
    script_path = tmp_path / 'capture_event.py'
    script_path.write_text(
        """import json, pathlib, sys\npathlib.Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')\n""",
        encoding='utf-8',
    )
    notify_server = bridge_intake_watch.build_notify_server(
        agent='agent-a',
        host='127.0.0.1',
        port=0,
        event_command=f'{sys.executable} {script_path} {capture_path}',
    )
    thread = threading.Thread(target=notify_server.serve_forever, daemon=True)
    thread.start()
    try:
        status, created = _request(
            api_server['base_url'],
            'POST',
            '/v1/handoffs',
            token='token-agent-a',
            body={
                'recipient': 'agent-c',
                'issue_type': 'task',
                'subject': 'Lifecycle forward',
                'requested_action': 'Forward lifecycle payload to command.',
                'minimal_context': 'Listener should run command on close/block events.',
            },
        )
        assert status == 201
        handoff_id = str(created['handoff_id'])

        notify_url = f'http://127.0.0.1:{notify_server.server_port}/notify'
        event = {
            'trigger': 'handoff_closed',
            'handoff_id': handoff_id,
            'sender': 'agent-c',
            'recipient': 'agent-a',
            'actor': 'agent-c',
            'status': 'closed',
            'subject': 'Lifecycle forward',
            'resolution_summary': 'Done now.',
        }
        status, payload = _notify_request(notify_url, token='token-agent-a', body=event)

        assert status == 200
        assert payload['actions'] == [
            {
                'agent': 'agent-a',
                'handoff_id': handoff_id,
                'trigger': 'handoff_closed',
                'action': 'event_command',
                'exit_code': 0,
            }
        ]
        assert json.loads(capture_path.read_text(encoding='utf-8')) == event

        status, handoff = _request(api_server['base_url'], 'GET', f'/v1/handoffs/{handoff_id}', token='token-agent-a')
        assert status == 200
        assert handoff['status'] == 'open'
    finally:
        notify_server.shutdown()
        thread.join(timeout=5)
        notify_server.server_close()
