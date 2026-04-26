from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import bridge_patrol
from bridge_core.frontmatter import dump_document
from bridge_core.models import HandoffRecord


class _NotifyCaptureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int]):
        super().__init__(server_address, _NotifyCaptureHandler)
        self.requests: list[dict[str, object]] = []


class _NotifyCaptureHandler(BaseHTTPRequestHandler):
    server: _NotifyCaptureServer

    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode('utf-8') or '{}')
        self.server.requests.append(payload)
        data = json.dumps({'ok': True}).encode('utf-8')
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def _setup_patrol_root(tmp_path: Path, monkeypatch):
    root = tmp_path / 'agent-shared'
    bridge = root / 'bridge'
    for kind in ('incoming', 'outgoing'):
        for agent in ('agent-a', 'agent-b', 'agent-c'):
            (bridge / kind / agent).mkdir(parents=True, exist_ok=True)
    (bridge / 'archive').mkdir(parents=True, exist_ok=True)
    (bridge / 'audit').mkdir(parents=True, exist_ok=True)
    (root / 'scripts').mkdir(parents=True, exist_ok=True)
    (root / 'docs').mkdir(parents=True, exist_ok=True)
    (root / 'examples').mkdir(parents=True, exist_ok=True)
    audit_file = bridge / 'audit' / 'handoff-log.md'
    audit_file.write_text('', encoding='utf-8')
    audit_file.chmod(0o600)

    config = root / 'config' / 'bridge_api.env'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        '\n'.join([
            'BRIDGE_API_HOST=127.0.0.1',
            'BRIDGE_API_PORT=8427',
            'BRIDGE_TOKEN_AGENT_C=token-agent-c',
            '',
        ]),
        encoding='utf-8',
    )

    for path in [
        root,
        bridge,
        bridge / 'incoming',
        bridge / 'outgoing',
        bridge / 'archive',
        bridge / 'audit',
        root / 'scripts',
        root / 'docs',
        root / 'examples',
        bridge / 'incoming' / 'agent-a',
        bridge / 'incoming' / 'agent-b',
        bridge / 'incoming' / 'agent-c',
        bridge / 'outgoing' / 'agent-a',
        bridge / 'outgoing' / 'agent-b',
        bridge / 'outgoing' / 'agent-c',
        config.parent,
    ]:
        path.chmod(0o700)
    config.chmod(0o600)

    monkeypatch.setattr(bridge_patrol, 'ROOT', root)
    monkeypatch.setattr(bridge_patrol, 'BRIDGE', bridge)
    monkeypatch.setattr(bridge_patrol, 'CONFIG', config)
    monkeypatch.setattr(bridge_patrol, 'PATROL_STATE_PATH', bridge / 'audit' / 'patrol-reminders.json')
    monkeypatch.setattr(bridge_patrol, 'bridge_systemd_unit', lambda: 'bridge-api.service')
    return root, bridge, config


def _write_handoff(
    bridge: Path,
    *,
    handoff_id: str,
    recipient: str = 'agent-c',
    updated_at: str,
    status: str = 'open',
    acknowledged_at: str = 'none',
    resolution_summary: str = 'pending',
) -> Path:
    record = HandoffRecord(
        handoff_id=handoff_id,
        status=status,
        created_at=updated_at,
        updated_at=updated_at,
        acknowledged_at=acknowledged_at,
        acknowledgment_source='none' if acknowledged_at == 'none' else 'auto',
        sender='agent-a',
        recipient=recipient,
        issue_type='task',
        handoff_kind='request',
        priority='medium',
        risk_level='low',
        due_at='none',
        approval_needed='no',
        approval_context='none',
        resolution_summary=resolution_summary,
        subject='Patrol reminder target',
        response_format='concise status + action + blocker if any',
        related_paths=[],
        body='## Requested Action\nAck it\n\n## Minimal Context\nTest\n\n## Constraints\n- none\n\n## Outcome\n- pending\n',
    )
    path = bridge / 'incoming' / recipient / f'{handoff_id}.md'
    path.write_text(dump_document(record.to_frontmatter(), record.body), encoding='utf-8')
    path.chmod(0o600)
    return path


def test_patrol_warns_when_service_or_health_check_fail(tmp_path: Path, monkeypatch, capsys) -> None:
    root, bridge, config = _setup_patrol_root(tmp_path, monkeypatch)

    monkeypatch.setattr(bridge_patrol, 'check_systemd_service', lambda unit: ('warning', f'{unit} not active (inactive)'))
    monkeypatch.setattr(bridge_patrol, 'check_api_health', lambda url: ('warning', f'bridge API health failed at {url} (connection refused)'))
    monkeypatch.setattr('sys.argv', ['bridge_patrol.py'])

    bridge_patrol.main()
    out = capsys.readouterr().out
    assert 'status: warning' in out
    assert 'bridge-api.service not active (inactive)' in out
    assert 'bridge API health failed at http://127.0.0.1:8427/v1/health' in out


def test_patrol_ok_when_service_and_health_are_ok(tmp_path: Path, monkeypatch, capsys) -> None:
    root, bridge, config = _setup_patrol_root(tmp_path, monkeypatch)

    monkeypatch.setattr(bridge_patrol, 'check_systemd_service', lambda unit: ('ok', f'{unit} active'))
    monkeypatch.setattr(bridge_patrol, 'check_api_health', lambda url: ('ok', f'bridge API health ok at {url}'))
    monkeypatch.setattr('sys.argv', ['bridge_patrol.py'])

    bridge_patrol.main()
    out = capsys.readouterr().out
    assert 'status: ok' in out
    assert 'bridge-api.service active' in out
    assert 'bridge API health ok at http://127.0.0.1:8427/v1/health' in out


def test_patrol_sends_unacknowledged_reminder_once_and_persists_state(tmp_path: Path, monkeypatch, capsys) -> None:
    root, bridge, config = _setup_patrol_root(tmp_path, monkeypatch)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    _write_handoff(bridge, handoff_id='HND-TEST-0001', updated_at=old_ts)

    notify_server = _NotifyCaptureServer(('127.0.0.1', 0))
    thread = threading.Thread(target=notify_server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('BRIDGE_NOTIFY_URL_AGENT_C', f'http://127.0.0.1:{notify_server.server_port}/notify')
    monkeypatch.setattr(bridge_patrol, 'check_systemd_service', lambda unit: ('ok', f'{unit} active'))
    monkeypatch.setattr(bridge_patrol, 'check_api_health', lambda url: ('ok', f'bridge API health ok at {url}'))
    try:
        monkeypatch.setattr('sys.argv', ['bridge_patrol.py', '--reminder-after-hours', '0.25', '--reminder-repeat-hours', '2', '--escalate-after-hours', '12'])
        bridge_patrol.main()
        out = capsys.readouterr().out
        assert 'reminder sent for HND-TEST-0001 to agent-c' in out
        assert len(notify_server.requests) == 1
        assert notify_server.requests[0]['trigger'] == 'handoff_reminder'
        state_path = bridge / 'audit' / 'patrol-reminders.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        assert state['HND-TEST-0001']['reminder_count'] == 1

        monkeypatch.setattr('sys.argv', ['bridge_patrol.py', '--reminder-after-hours', '0.25', '--reminder-repeat-hours', '2', '--escalate-after-hours', '12'])
        bridge_patrol.main()
        capsys.readouterr()
        assert len(notify_server.requests) == 1
    finally:
        notify_server.shutdown()
        thread.join(timeout=5)
        notify_server.server_close()


def test_patrol_escalates_old_unacknowledged_handoff_without_repeat(tmp_path: Path, monkeypatch, capsys) -> None:
    root, bridge, config = _setup_patrol_root(tmp_path, monkeypatch)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=8)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    _write_handoff(bridge, handoff_id='HND-TEST-0002', updated_at=old_ts)

    monkeypatch.setattr(bridge_patrol, 'check_systemd_service', lambda unit: ('ok', f'{unit} active'))
    monkeypatch.setattr(bridge_patrol, 'check_api_health', lambda url: ('ok', f'bridge API health ok at {url}'))

    monkeypatch.setattr('sys.argv', ['bridge_patrol.py', '--reminder-after-hours', '99', '--escalate-after-hours', '6', '--escalate-repeat-hours', '24'])
    bridge_patrol.main()
    out = capsys.readouterr().out
    assert 'status: warning' in out
    assert 'escalation: unresolved open handoff for agent-c: HND-TEST-0002 age=8.0h' in out

    state_path = bridge / 'audit' / 'patrol-reminders.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['HND-TEST-0002']['escalation_count'] == 1

    monkeypatch.setattr('sys.argv', ['bridge_patrol.py', '--reminder-after-hours', '99', '--escalate-after-hours', '6', '--escalate-repeat-hours', '24'])
    bridge_patrol.main()
    out = capsys.readouterr().out
    assert out.count('escalation: unresolved open handoff for agent-c: HND-TEST-0002') == 0


def test_patrol_escalates_old_acknowledged_handoff_without_resolution(tmp_path: Path, monkeypatch, capsys) -> None:
    root, bridge, config = _setup_patrol_root(tmp_path, monkeypatch)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=7)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    _write_handoff(
        bridge,
        handoff_id='HND-TEST-0003',
        updated_at=old_ts,
        status='acknowledged',
        acknowledged_at=old_ts,
    )

    monkeypatch.setattr(bridge_patrol, 'check_systemd_service', lambda unit: ('ok', f'{unit} active'))
    monkeypatch.setattr(bridge_patrol, 'check_api_health', lambda url: ('ok', f'bridge API health ok at {url}'))
    monkeypatch.setattr('sys.argv', ['bridge_patrol.py', '--reminder-after-hours', '99', '--escalate-after-hours', '6', '--escalate-repeat-hours', '24'])

    bridge_patrol.main()
    out = capsys.readouterr().out

    assert 'status: warning' in out
    assert 'escalation: unresolved acknowledged handoff for agent-c: HND-TEST-0003 age=7.0h' in out


def test_patrol_reminds_on_in_progress_handoff(tmp_path: Path, monkeypatch, capsys) -> None:
    root, bridge, config = _setup_patrol_root(tmp_path, monkeypatch)
    updated_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    acknowledged_at = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    _write_handoff(
        bridge,
        handoff_id='HND-TEST-0004',
        updated_at=updated_at,
        status='in_progress',
        acknowledged_at=acknowledged_at,
        resolution_summary='Investigating; no stable fix yet.',
    )

    notify_server = _NotifyCaptureServer(('127.0.0.1', 0))
    thread = threading.Thread(target=notify_server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('BRIDGE_NOTIFY_URL_AGENT_C', f'http://127.0.0.1:{notify_server.server_port}/notify')
    monkeypatch.setattr(bridge_patrol, 'check_systemd_service', lambda unit: ('ok', f'{unit} active'))
    monkeypatch.setattr(bridge_patrol, 'check_api_health', lambda url: ('ok', f'bridge API health ok at {url}'))
    try:
        monkeypatch.setattr('sys.argv', ['bridge_patrol.py', '--reminder-after-hours', '0.25', '--reminder-repeat-hours', '2', '--escalate-after-hours', '12'])
        bridge_patrol.main()
        out = capsys.readouterr().out

        assert 'reminder sent for HND-TEST-0004 to agent-c status=in_progress age=1.0h count=1' in out
        assert len(notify_server.requests) == 1
        assert notify_server.requests[0]['reason'] == 'unresolved_in_progress_handoff'
        assert notify_server.requests[0]['status'] == 'in_progress'
    finally:
        notify_server.shutdown()
        thread.join(timeout=5)
        notify_server.server_close()


def test_patrol_warns_on_active_handoff_older_than_alert_threshold_without_actionable_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    root, bridge, config = _setup_patrol_root(tmp_path, monkeypatch)
    updated_at = (datetime.now(timezone.utc) - timedelta(minutes=45)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    acknowledged_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    _write_handoff(
        bridge,
        handoff_id='HND-TEST-0005',
        updated_at=updated_at,
        status='acknowledged',
        acknowledged_at=acknowledged_at,
        resolution_summary='pending',
    )

    monkeypatch.setattr(bridge_patrol, 'check_systemd_service', lambda unit: ('ok', f'{unit} active'))
    monkeypatch.setattr(bridge_patrol, 'check_api_health', lambda url: ('ok', f'bridge API health ok at {url}'))
    monkeypatch.setattr('sys.argv', ['bridge_patrol.py', '--active-alert-hours', '0.5', '--reminder-after-hours', '99', '--escalate-after-hours', '99'])

    bridge_patrol.main()
    out = capsys.readouterr().out

    assert 'status: warning' in out
    assert 'active unresolved handoff for agent-c: HND-TEST-0005 age=0.8h status=acknowledged summary=pending' in out



def test_patrol_skips_active_alert_when_summary_is_actionable(tmp_path: Path, monkeypatch, capsys) -> None:
    root, bridge, config = _setup_patrol_root(tmp_path, monkeypatch)
    updated_at = (datetime.now(timezone.utc) - timedelta(minutes=45)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    acknowledged_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    _write_handoff(
        bridge,
        handoff_id='HND-TEST-0006',
        updated_at=updated_at,
        status='acknowledged',
        acknowledged_at=acknowledged_at,
        resolution_summary='Root cause verified. Stable target is bluebubbles:+17209719704. Safe retry only after fresh approval.',
    )

    monkeypatch.setattr(bridge_patrol, 'check_systemd_service', lambda unit: ('ok', f'{unit} active'))
    monkeypatch.setattr(bridge_patrol, 'check_api_health', lambda url: ('ok', f'bridge API health ok at {url}'))
    monkeypatch.setattr('sys.argv', ['bridge_patrol.py', '--active-alert-hours', '0.5', '--reminder-after-hours', '99', '--escalate-after-hours', '99'])

    bridge_patrol.main()
    out = capsys.readouterr().out

    assert 'active unresolved handoff for agent-c: HND-TEST-0006' not in out
    assert 'status: ok' in out
