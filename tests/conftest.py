from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / 'scripts'
for path in (ROOT_DIR, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

TEST_AGENTS = ('agent-a', 'agent-b', 'agent-c')


@pytest.fixture(autouse=True)
def bridge_sandbox(tmp_path, monkeypatch):
    import bridge_cli
    import bridge_audit_view

    root = tmp_path / 'bridge'
    bridge = root / 'bridge'
    for kind in ('incoming', 'outgoing'):
        for agent in TEST_AGENTS:
            (bridge / kind / agent).mkdir(parents=True, exist_ok=True)
    (bridge / 'archive').mkdir(parents=True, exist_ok=True)
    (bridge / 'audit').mkdir(parents=True, exist_ok=True)
    audit_file = bridge / 'audit' / 'handoff-log.md'
    audit_file.write_text('', encoding='utf-8')

    system_dir = tmp_path / 'agent-a' / 'System'
    system_dir.mkdir(parents=True, exist_ok=True)
    output = system_dir / 'Bridge Audit View.md'
    archive_output = system_dir / 'Bridge Archive Index.md'

    monkeypatch.setattr(bridge_cli, 'ROOT', root)
    monkeypatch.setattr(bridge_cli, 'BRIDGE', bridge)
    monkeypatch.setattr(bridge_cli, 'AUDIT', audit_file)

    monkeypatch.setattr(bridge_audit_view, 'ROOT', root)
    monkeypatch.setattr(bridge_audit_view, 'BRIDGE', bridge)
    monkeypatch.setattr(bridge_audit_view, 'OUTPUT', output)
    monkeypatch.setattr(bridge_audit_view, 'ARCHIVE_OUTPUT', archive_output)

    return {
        'root': root,
        'bridge': bridge,
        'audit_file': audit_file,
        'output': output,
        'archive_output': archive_output,
    }
