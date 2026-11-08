from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import threading

import pytest

from bridge.cli import notify, unseen
from bridge.config import load_agents, require_identity
from bridge.frontmatter import dump_document, parse_frontmatter
from bridge.policy import require_change, TERMINAL
from bridge.store import Store

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    account = pwd.getpwuid(os.getuid()).pw_name
    (tmp_path / 'agents.conf').write_text(''.join(f'{agent} {account}\n' for agent in ('alice', 'bob', 'ops')))
    return Store(tmp_path, load_agents(tmp_path))


def run(store, *args, body='', extra_env=None):
    return subprocess.run([sys.executable, str(REPO / 'bin/bridge'), '--root', str(store.root), *args],
                          input=body.encode(), capture_output=True,
                          env={**os.environ, **(extra_env or {})})


def test_lifecycle_and_owes(store):
    ident = store.new('alice', 'bob', 'Review artifact', 'Inputs: /srv/artifacts/input.md\n', '2030-01-01')
    first = store.load(ident)
    assert first['status'] == 'open' and first['owes'] == 'bob'
    original = (store.root / 'open' / ident / '00-alice.md').read_bytes()
    store.append(ident, 'bob', 'Checking.', 'working')
    store.append(ident, 'bob', 'Which revision?', 'blocked')
    assert store.load(ident)['owes'] == 'alice'
    store.append(ident, 'alice', 'Revision 2.')
    answered = store.load(ident)
    assert answered['status'] == 'blocked' and answered['owes'] == 'bob'
    store.append(ident, 'bob', 'Resuming.', 'working')
    store.append(ident, 'bob', 'Reviewed; result: /srv/artifacts/result.md', 'done')
    request = store.load(ident)
    assert request['status'] == 'done' and request['owes'] is None
    assert request['due'] == '2030-01-01'
    assert len(request['events']) == 6
    assert (store.root / 'done' / ident / '00-alice.md').read_bytes() == original
    assert not (store.root / 'open' / ident).exists()
    cancelled = store.new('alice', 'bob', 'Withdraw request', '')
    store.append(cancelled, 'alice', 'No longer needed.', 'cancelled')
    assert store.load(cancelled)['status'] == 'cancelled'


@pytest.mark.parametrize('actor,status,body', [
    ('alice', 'done', 'Result'), ('bob', 'cancelled', 'No'),
    ('ops', None, 'Hello'), ('alice', 'working', 'Start'),
    ('alice', 'blocked', 'Question'), ('bob', 'done', ' \n'),
    ('bob', 'unknown', 'Bad status'),
])
def test_role_refusals(store, actor, status, body):
    ident = store.new('alice', 'bob', 'Task', 'Input')
    with pytest.raises(ValueError):
        store.append(ident, actor, body, status)
    assert len(store.load(ident)['events']) == 1


@pytest.mark.parametrize('current', ['open', 'working', 'blocked', 'done', 'cancelled'])
@pytest.mark.parametrize('target', ['open', 'working', 'blocked', 'done', 'cancelled'])
def test_transition_table(current, target):
    allowed = current not in TERMINAL and (target != 'open' or current == 'open')
    actor = 'alice' if target in {'open', 'cancelled'} else 'bob'
    if allowed:
        require_change(current, target, actor, 'alice', 'bob', 'Outcome')
    else:
        with pytest.raises(ValueError):
            require_change(current, target, actor, 'alice', 'bob', 'Outcome')


def test_no_replies_after_terminal(store):
    ident = store.new('alice', 'bob', 'Task', '')
    store.append(ident, 'bob', 'Complete.', 'done')
    with pytest.raises(ValueError, match='transition'):
        store.append(ident, 'alice', 'Reopen?')


def test_concurrent_writers_different_authors(store, monkeypatch):
    ident = store.new('alice', 'bob', 'Concurrent', 'Input')
    # Force every writer to observe the same starting sequence before publication.
    barrier = threading.Barrier(12)
    original_load = store.load
    def synchronized_load(value):
        request = original_load(value)
        barrier.wait(timeout=10)
        return request
    monkeypatch.setattr(store, 'load', synchronized_load)
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(store.append, ident, 'alice' if index % 2 else 'bob', f'Reply {index}')
                   for index in range(12)]
        for future in futures:
            future.result()
    events = original_load(ident)['events']
    assert len(events) == 13
    assert [int(event['file'].split('-')[0]) for event in events] == list(range(13))
    assert len({event['body'] for event in events}) == 13


def test_modes_and_bell_inode(store):
    old_umask = os.umask(0o077)
    try:
        ident = store.new('alice', 'bob', 'Modes', 'Input')
        bell = store.root / 'open/.bell'
        inode = bell.stat().st_ino
        store.append(ident, 'bob', 'Working.', 'working')
        assert bell.stat().st_ino == inode
        assert bell.stat().st_mode & 0o777 == 0o660
        for area in ('open', 'done', f'open/{ident}'):
            assert (store.root / area).stat().st_mode & 0o777 == 0o770
        for event in (store.root / 'open' / ident).iterdir():
            assert event.stat().st_mode & 0o777 == 0o640
    finally:
        os.umask(old_umask)


def test_identity_refusal_and_owner_warning(store):
    ident = store.new('alice', 'bob', 'Provenance', '')
    store.agents['alice'] = replace(store.agents['alice'], uid=os.getuid() + 1)
    with pytest.raises(ValueError, match='requires Unix account'):
        store.new('alice', 'bob', 'Impersonation', '')
    assert 'file owner' in store.load(ident)['events'][0]['warnings'][0]
    with pytest.raises(ValueError, match='unknown agent'):
        require_identity(store.agents, 'stranger')


def test_seen_excludes_self_and_survives_move(store, tmp_path):
    state = tmp_path / 'state'
    ident = store.new('alice', 'bob', 'Task', 'Input')
    assert unseen(store, 'alice', state, False) == []
    first = unseen(store, 'bob', state, True)
    assert len(first) == 1
    assert not unseen(store, 'bob', state, False)
    store.append(ident, 'bob', 'Question?', 'blocked')
    before = unseen(store, 'alice', state, True)
    assert len(before) == 1
    store.append(ident, 'bob', 'Result.', 'done')
    after = unseen(store, 'alice', state, False)
    assert len(after) == 1 and after[0]['status'] == 'done'
    assert not unseen(store, 'bob', state, False)
    assert first[0]['key'] == store.load(ident)['events'][0]['key']
    assert (state / 'alice.seen').stat().st_mode & 0o777 == 0o640


def test_notify_is_generic_and_failure_keeps_event(store, tmp_path, capsys):
    output = tmp_path / 'notification'
    code = 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read()); sys.exit(3)'
    store.agents['bob'] = replace(store.agents['bob'], notify=(sys.executable, '-c', code, str(output)))
    ident = store.new('alice', 'bob', 'Wake recipient', 'Input')
    notify(store, ident, 'alice')
    assert ident in output.read_text() and 'from alice' in output.read_text()
    assert output.read_text().count('\n') == 1
    assert 'event saved; notify bob failed' in capsys.readouterr().err
    assert store.load(ident)['status'] == 'open'
    store.agents['alice'] = replace(store.agents['alice'], notify=(sys.executable, '-c', code, str(output)))
    store.append(ident, 'bob', 'Result.', 'done')
    notify(store, ident, 'bob')
    assert 'from bob' in output.read_text()


def test_interrupted_close_is_listed_as_done(store, monkeypatch):
    ident = store.new('alice', 'bob', 'Crash before move', 'Input')
    def crash(*args):
        raise OSError('simulated interrupted rename')
    monkeypatch.setattr(Path, 'rename', crash)
    store.append(ident, 'bob', 'Finished.', 'done')
    assert (store.root / 'open' / ident).exists()
    assert store.requests()[0]['status'] == 'done'
    closed = run(store, 'list', '--done', '--json')
    assert closed.returncode == 0 and json.loads(closed.stdout)[0]['id'] == ident
    assert json.loads(run(store, 'list', '--json').stdout) == []


@pytest.mark.parametrize('body', ['Bad\r\ntext', 'Bad\x00text', 'Bad\x1btext', 'x' * 65536, 'é' * 33000])
def test_invalid_input_leaves_no_request(store, body):
    with pytest.raises(ValueError):
        store.new('alice', 'bob', 'Task', body)
    assert not (store.root / 'open').exists()


def test_frontmatter_and_path_rejections(store):
    text = dump_document({'id': 'example', 'subject': 'Task: review'}, 'Body\n')
    assert parse_frontmatter(text) == ({'id': 'example', 'subject': 'Task: review'}, 'Body\n')
    with pytest.raises(ValueError):
        store.new('alice', 'bob', 'Injected\nstatus: done', 'Body')
    with pytest.raises(ValueError):
        store.new('alice', 'alice', 'Self', '')
    with pytest.raises(ValueError):
        store.load('../../agents.conf')


def test_cli_commands_json_and_filters(store, tmp_path):
    created = run(store, 'new', '--as', 'alice', '--to', 'bob', '--subject', 'CLI task', body='Input\n')
    assert created.returncode == 0, created.stderr
    ident = created.stdout.decode().strip()
    listed = json.loads(run(store, '--as', 'bob', 'list', '--owed', '--json').stdout)
    assert listed[0]['id'] == ident and listed[0]['owes'] == 'bob'
    assert json.loads(run(store, 'list', '--mine', '--as', 'ops', '--json').stdout) == []
    assert run(store, 'reply', ident, '--as', 'bob', '--status', 'working', body='Started').returncode == 0
    shown = json.loads(run(store, 'show', ident, '--json').stdout)
    assert shown['status'] == 'working' and len(shown['events']) == 2
    assert 'Started' in run(store, 'show', ident).stdout.decode()
    state = str(tmp_path / 'seen')
    marked = run(store, 'unseen', '--as', 'bob', '--state-dir', state, '--mark', '--json')
    assert len(json.loads(marked.stdout)) == 1
    assert json.loads(run(store, 'unseen', '--as', 'bob', '--state-dir', state, '--json').stdout) == []
    assert run(store, 'close', ident, '--as', 'bob', body='Outcome: complete').returncode == 0
    assert json.loads(run(store, 'list', '--json').stdout) == []
    assert json.loads(run(store, 'list', '--done', '--json').stdout)[0]['status'] == 'done'
    cancelled = run(store, 'new', '--as', 'alice', '--to', 'bob', '--subject', 'Cancel me').stdout.decode().strip()
    assert run(store, 'cancel', cancelled, '--as', 'alice', body='Withdrawn').returncode == 0
    assert run(store, 'list', '--mine').returncode == 1
    assert run(store, 'unseen').returncode == 1
    assert run(store, 'new', '--as', 'alice', '--to', 'bob', '--subject', 'CRLF', body='Bad\r\n').returncode == 1


def test_config_rejections_and_quoted_command(store):
    path = store.root / 'agents.conf'
    account = pwd.getpwuid(os.getuid()).pw_name
    path.write_text(f'alice {account} /usr/bin/printf "quoted argument" # comment\n')
    assert load_agents(store.root)['alice'].notify == ('/usr/bin/printf', 'quoted argument')
    for text in (f'alice {account}\nalice {account}\n', 'bad/id account\n', 'alice\n', 'alice no-such-bridge-account\n'):
        path.write_text(text)
        with pytest.raises(ValueError):
            load_agents(store.root)


def test_notify_command_from_configuration_and_root_environment(store, tmp_path):
    account = pwd.getpwuid(os.getuid()).pw_name
    output = tmp_path / 'pointer'
    code = 'import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(os.environ["BRIDGE_ROOT"] + "\\n" + sys.stdin.read())'
    import shlex
    command = shlex.join([sys.executable, '-c', code, str(output)])
    (store.root / 'agents.conf').write_text(f'alice {account}\nbob {account} {command}\n')
    result = subprocess.run([str(REPO / 'bin/bridge'), 'new', '--as', 'alice', '--to', 'bob', '--subject', 'Notify'],
                            input=b'Body', capture_output=True,
                            env={**os.environ, 'BRIDGE_ROOT': str(store.root)})
    assert result.returncode == 0, result.stderr
    assert output.read_text().startswith(str(store.root) + '\nbridge ')
    assert result.stdout.decode().strip() in output.read_text()


def test_unpublished_claim_is_skipped(store):
    ident = store.new('alice', 'bob', 'Interrupted writer', '')
    (store.root / 'open' / ident / '.01').write_text('unfinished')
    store.append(ident, 'bob', 'This event survives.')
    files = [event['file'] for event in store.load(ident)['events']]
    assert files == ['00-alice.md', '02-bob.md']


def test_reply_racing_close_cannot_reopen(store):
    ident = store.new('alice', 'bob', 'Race', 'Input')
    stale = store.load(ident)
    store.append(ident, 'bob', 'Finished.', 'done')
    folder = store.root / 'done' / ident
    store.publish(folder, {'id': ident, 'from': 'alice', 'status': stale['status'], 'at': '2030-01-01T00:00:00+00:00'}, 'Late.')
    request = store.load(ident)
    assert request['status'] == 'done' and request['owes'] is None
    assert any(w.startswith('02-alice.md: published after') for w in request['warnings'])
    with pytest.raises(ValueError, match='transition'):
        store.append(ident, 'alice', 'Again?')
    assert json.loads(run(store, 'list', '--json').stdout) == []


def test_listing_skips_unpublished_and_moved_folders(store, tmp_path, monkeypatch):
    ident = store.new('alice', 'bob', 'Published', 'Input')
    (store.root / 'open' / '20300101-000000-alice-empty').mkdir()
    assert [r['id'] for r in store.requests()] == [ident]
    assert len(unseen(store, 'bob', tmp_path / 'state', False)) == 1
    assert json.loads(run(store, 'list', '--json').stdout)[0]['id'] == ident
    moving = store.new('alice', 'bob', 'Moving', 'Input')
    original_iterdir = Path.iterdir
    def iterdir_then_move(path):
        if path.name == moving and path.parent.name == 'open':
            path.rename(store.root / 'done' / moving)
        return original_iterdir(path)
    monkeypatch.setattr(Path, 'iterdir', iterdir_then_move)
    assert moving in [r['id'] for r in store.requests()]
