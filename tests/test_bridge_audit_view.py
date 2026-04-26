from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import bridge_audit_view


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _write_handoff(path: Path, **frontmatter):
    lines = ['---']
    for key, value in frontmatter.items():
        lines.append(f'{key}: {value}')
    lines += ['---', '', '## Outcome', frontmatter.get('resolution_summary', 'pending'), '']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines), encoding='utf-8')


def test_audit_view_reports_core_signals_and_writes_archive_index(bridge_sandbox, monkeypatch):
    monkeypatch.setenv('BRIDGE_ALLOWED_ROUTES', 'agent-c:agent-a,agent-a:agent-c,agent-b:agent-a,agent-a:agent-b')
    bridge = bridge_sandbox['bridge']
    now = datetime.now(timezone.utc)
    stale_time = _iso(now - timedelta(hours=30))
    fresh_time = _iso(now - timedelta(hours=2))
    archived_time = _iso(now - timedelta(hours=6))

    _write_handoff(
        bridge / 'incoming' / 'agent-a' / 'HND-ACTIVE-001.md',
        handoff_id='HND-ACTIVE-001',
        status='open',
        created_at=stale_time,
        updated_at=stale_time,
        sender='agent-c',
        recipient='agent-a',
        issue_type='task',
        handoff_kind='request',
        priority='medium',
        risk_level='low',
        due_at='none',
        approval_needed='no',
        approval_context='none',
        resolution_summary='pending',
        subject='Stale inbox item',
        response_format='bullet list',
    )
    _write_handoff(
        bridge / 'incoming' / 'agent-a' / 'HND-BLOCKED-001.md',
        handoff_id='HND-BLOCKED-001',
        status='blocked',
        created_at=fresh_time,
        updated_at=fresh_time,
        sender='agent-b',
        recipient='agent-a',
        issue_type='incident',
        handoff_kind='incident',
        priority='high',
        risk_level='medium',
        due_at='none',
        approval_needed='no',
        approval_context='none',
        resolution_summary='Waiting on approval',
        subject='Blocked inbox item',
        response_format='bullet list',
    )
    _write_handoff(
        bridge / 'incoming' / 'agent-c' / 'HND-VIOLATION-001.md',
        handoff_id='HND-VIOLATION-001',
        status='open',
        created_at=fresh_time,
        updated_at=fresh_time,
        sender='agent-b',
        recipient='agent-c',
        issue_type='question',
        handoff_kind='question',
        priority='low',
        risk_level='low',
        due_at='none',
        approval_needed='no',
        approval_context='none',
        resolution_summary='pending',
        subject='Route violation item',
        response_format='bullet list',
    )
    archive_dir = bridge / 'archive' / 'HND-ARCH-001'
    _write_handoff(
        archive_dir / 'HND-ARCH-001.md',
        handoff_id='HND-ARCH-001',
        status='archived',
        created_at=stale_time,
        updated_at=archived_time,
        sender='agent-a',
        recipient='agent-c',
        issue_type='result',
        handoff_kind='result',
        priority='urgent',
        risk_level='high',
        due_at='none',
        approval_needed='no',
        approval_context='none',
        resolution_summary='Resolved and archived',
        subject='Archived handoff',
        response_format='bullet list',
    )
    _write_handoff(
        archive_dir / 'HND-ARCH-001.outgoing.agent-a.md',
        handoff_id='HND-ARCH-001',
        status='archived',
        created_at=stale_time,
        updated_at=archived_time,
        sender='agent-a',
        recipient='agent-c',
        issue_type='result',
        handoff_kind='result',
        priority='urgent',
        risk_level='high',
        due_at='none',
        approval_needed='no',
        approval_context='none',
        resolution_summary='Resolved and archived (preserved copy)',
        subject='Archived handoff',
        response_format='bullet list',
    )

    bridge_audit_view.main()

    audit_text = bridge_sandbox['output'].read_text(encoding='utf-8')
    archive_text = bridge_sandbox['archive_output'].read_text(encoding='utf-8')

    assert '- Total active handoffs: **3**' in audit_text
    assert '- `agent-a` inbox active: **2**' in audit_text
    assert '- `agent-c` inbox active: **1**' in audit_text
    assert '- Blocked handoffs: **1**' in audit_text
    assert '- Stale active handoffs (>24h): **1**' in audit_text
    assert '- Route violations: **1**' in audit_text

    assert '## Needs Attention' in audit_text
    assert 'HND-BLOCKED-001' in audit_text
    assert 'HND-ACTIVE-001' in audit_text
    assert 'HND-VIOLATION-001' in audit_text
    assert '## Route Violations' in audit_text
    assert 'agent-b → agent-c' in audit_text
    assert '## Stale Active Handoffs (>24h)' in audit_text
    assert 'Stale inbox item' in audit_text

    assert '# Bridge Archive Index' in archive_text
    assert '## Recent Closures' in archive_text
    assert '## By Route' in archive_text
    assert 'HND-ARCH-001' in archive_text
    assert 'Resolved and archived' in archive_text
    assert 'agent-a → agent-c' in archive_text
    assert 'preserved copies: 2' in archive_text
    assert 'preserved copies: 2' in audit_text
