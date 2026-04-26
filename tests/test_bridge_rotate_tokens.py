from __future__ import annotations

from pathlib import Path

import bridge_rotate_tokens


def test_rotate_lines_updates_only_selected_agents() -> None:
    lines = [
        'BRIDGE_TOKEN_AGENT_A=old-agent-a',
        'BRIDGE_TOKEN_AGENT_B=old-agent-b',
        'BRIDGE_TOKEN_AGENT_C=old-agent-c',
        'BRIDGE_API_PORT=8427',
        '',
    ]
    rotated = bridge_rotate_tokens.rotate_lines(lines, {'agent-a', 'agent-c'})
    values = {line.split('=', 1)[0]: line.split('=', 1)[1] for line in rotated if line.startswith('BRIDGE_TOKEN_')}
    assert values['BRIDGE_TOKEN_AGENT_B'] == 'old-agent-b'
    assert values['BRIDGE_TOKEN_AGENT_A'] != 'old-agent-a'
    assert values['BRIDGE_TOKEN_AGENT_C'] != 'old-agent-c'


def test_rotate_tokens_dry_run_does_not_modify_file(tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / 'bridge_api.env'
    original = '\n'.join([
        'BRIDGE_TOKEN_AGENT_A=old-agent-a',
        'BRIDGE_TOKEN_AGENT_B=old-agent-b',
        'BRIDGE_TOKEN_AGENT_C=old-agent-c',
        '',
    ])
    config.write_text(original, encoding='utf-8')

    monkeypatch.setattr('sys.argv', ['bridge_rotate_tokens.py', '--config', str(config), '--agents', 'agent-a', '--dry-run'])
    bridge_rotate_tokens.main()
    out = capsys.readouterr().out

    assert 'dry_run: yes' in out
    assert config.read_text(encoding='utf-8') == original
