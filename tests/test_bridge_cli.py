from __future__ import annotations

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

import bridge_cli


def _create_handoff(capsys):
    bridge_cli.create(
        Namespace(
            sender='agent-a',
            recipient='agent-c',
            issue_type='task',
            handoff_kind='request',
            priority='high',
            risk_level='medium',
            due_at='2026-04-24T18:00:00Z',
            approval_needed=False,
            approval_context='',
            subject='Lifecycle coverage',
            response_format='bullet list',
            related_path=['/tmp/example.md'],
            requested_action='Handle the request.',
            minimal_context='Only for tests.',
            constraints='- keep sandboxed',
        )
    )
    result = json.loads(capsys.readouterr().out)
    return result['handoff_id']


def test_validate_route_allows_agent_a_to_agent_c():
    bridge_cli.validate_route('agent-a', 'agent-c')


def test_validate_route_rejects_agent_b_to_agent_c(monkeypatch):
    monkeypatch.setenv('BRIDGE_ALLOWED_ROUTES', 'agent-a:agent-c,agent-c:agent-a')
    with pytest.raises(SystemExit, match='route not allowed'):
        bridge_cli.validate_route('agent-b', 'agent-c')


def test_handoff_lifecycle_persists_resolution_summary(bridge_sandbox, capsys):
    handoff_id = _create_handoff(capsys)
    bridge = bridge_sandbox['bridge']

    outgoing = bridge / 'outgoing' / 'agent-a' / f'{handoff_id}.md'
    incoming = bridge / 'incoming' / 'agent-c' / f'{handoff_id}.md'
    assert outgoing.exists()
    assert incoming.exists()

    outbox_data, _ = bridge_cli.parse_frontmatter(outgoing.read_text(encoding='utf-8'))
    inbox_data, _ = bridge_cli.parse_frontmatter(incoming.read_text(encoding='utf-8'))
    assert outbox_data['status'] == 'open'
    assert inbox_data['resolution_summary'] == 'pending'

    bridge_cli.set_status(Namespace(actor='agent-c', handoff_id=handoff_id, status='acknowledged', outcome=''))
    capsys.readouterr()

    for path in (outgoing, incoming):
        data, _ = bridge_cli.parse_frontmatter(path.read_text(encoding='utf-8'))
        assert data['acknowledgment_source'] == 'manual'
        assert data['acknowledged_at'] != 'none'

    bridge_cli.set_status(
        Namespace(
            actor='agent-c',
            handoff_id=handoff_id,
            status='closed',
            outcome='Handled safely and fully documented.',
        )
    )
    capsys.readouterr()

    for path in (outgoing, incoming):
        data, body = bridge_cli.parse_frontmatter(path.read_text(encoding='utf-8'))
        assert data['status'] == 'closed'
        assert data['resolution_summary'] == 'Handled safely and fully documented.'
        assert '## Outcome\nHandled safely and fully documented.' in body

    bridge_cli.archive(Namespace(actor='agent-c', handoff_id=handoff_id))
    archive_result = json.loads(capsys.readouterr().out)
    archive_dir = bridge / 'archive' / handoff_id

    assert archive_result['archive_dir'] == str(archive_dir)
    assert archive_dir.exists()
    assert not outgoing.exists()
    assert not incoming.exists()

    archived_file = archive_dir / f'{handoff_id}.md'
    assert archived_file.exists()
    archived_data, archived_body = bridge_cli.parse_frontmatter(archived_file.read_text(encoding='utf-8'))
    assert archived_data['status'] == 'archived'
    assert archived_data['resolution_summary'] == 'Handled safely and fully documented.'
    assert '## Outcome\nHandled safely and fully documented.' in archived_body

    audit_lines = bridge_sandbox['audit_file'].read_text(encoding='utf-8').strip().splitlines()
    assert len(audit_lines) == 4
    assert ' | open | Lifecycle coverage' in audit_lines[0]
    assert f' | {handoff_id} | ' in audit_lines[-1]
    assert ' | archived | Lifecycle coverage' in audit_lines[-1]


def test_archive_requires_closed_status(bridge_sandbox, capsys):
    handoff_id = _create_handoff(capsys)

    outgoing = bridge_sandbox['bridge'] / 'outgoing' / 'agent-a' / f'{handoff_id}.md'
    incoming = bridge_sandbox['bridge'] / 'incoming' / 'agent-c' / f'{handoff_id}.md'
    assert outgoing.exists()
    assert incoming.exists()

    with pytest.raises(SystemExit, match='only closed handoffs can be archived'):
        bridge_cli.archive(Namespace(actor='agent-c', handoff_id=handoff_id))


def test_relocated_bridge_cli_uses_repo_relative_root(tmp_path):
    source_root = Path(__file__).resolve().parents[1]
    relocated_root = tmp_path / 'bridge-copy'
    relocated_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '-R', str(source_root / 'bridge_core'), str(relocated_root / 'bridge_core')], check=True)
    subprocess.run(['cp', '-R', str(source_root / 'scripts'), str(relocated_root / 'scripts')], check=True)

    proc = subprocess.run(
        [
            sys.executable,
            str(relocated_root / 'scripts' / 'bridge_cli.py'),
            'create',
            '--sender', 'agent-a',
            '--recipient', 'agent-c',
            '--issue-type', 'task',
            '--subject', 'Relocated root test',
            '--requested-action', 'Verify repo-relative defaults',
            '--minimal-context', 'Copied repo should use its own bridge directory',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)

    expected_outbox = relocated_root / 'bridge' / 'outgoing' / 'agent-a' / f"{payload['handoff_id']}.md"
    expected_inbox = relocated_root / 'bridge' / 'incoming' / 'agent-c' / f"{payload['handoff_id']}.md"

    assert Path(payload['outbox']) == expected_outbox
    assert Path(payload['inbox']) == expected_inbox
    assert expected_outbox.exists()
    assert expected_inbox.exists()
