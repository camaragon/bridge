from __future__ import annotations

import json
import sys

import pytest

import bridge_agent


def test_generic_wrapper_create_uses_explicit_agent_identity(monkeypatch, capsys):
    captured = {}

    def fake_api_request(agent, method, path, *, payload=None):
        captured['agent'] = agent
        captured['method'] = method
        captured['path'] = path
        captured['payload'] = payload
        return {
            'handoff_id': 'HND-TEST-001',
            'sender': 'agent-a',
            'recipient': 'agent-b',
        }

    def fake_invoke(agent, args, *, api_call, cli_args):
        captured['invoke_agent'] = agent
        captured['cli_args'] = cli_args
        print(json.dumps(api_call()))

    monkeypatch.setattr(bridge_agent, 'api_request', fake_api_request)
    monkeypatch.setattr(bridge_agent, 'invoke', fake_invoke)
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'bridge_agent.py',
            '--agent', 'agent-a',
            'create',
            '--recipient', 'agent-b',
            '--issue-type', 'task',
            '--subject', 'API wrapper test',
            '--requested-action', 'Do the thing',
            '--minimal-context', 'Minimal context',
        ],
    )

    bridge_agent.main()
    payload = json.loads(capsys.readouterr().out)

    assert captured['agent'] == 'agent-a'
    assert captured['invoke_agent'] == 'agent-a'
    assert captured['method'] == 'POST'
    assert captured['path'] == '/v1/handoffs'
    assert 'sender' not in captured['payload']
    assert captured['payload']['recipient'] == 'agent-b'
    assert '--sender' in captured['cli_args']
    assert captured['cli_args'][captured['cli_args'].index('--sender') + 1] == 'agent-a'
    assert payload['handoff_id'] == 'HND-TEST-001'
    assert payload['outbox'].endswith('/outgoing/agent-a/HND-TEST-001.md')
    assert payload['inbox'].endswith('/incoming/agent-b/HND-TEST-001.md')


def test_generic_wrapper_refuses_cli_fallback_unless_enabled(monkeypatch):
    monkeypatch.setattr(bridge_agent, 'api_request', lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError('down')))
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'bridge_agent.py',
            '--agent', 'agent-a',
            'list-open',
        ],
    )

    with pytest.raises(SystemExit, match='bridge API unavailable'):
        bridge_agent.main()
