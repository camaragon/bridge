# Bridge

Bridge is a small shared-file CLI for requests between agents. Each request has
an author, a recipient, an event history, a status, and someone who owes the next
move. Every kind of agent uses the same commands, including chat-gateway agents
and remote agents connecting over SSH.

There is no server, token store, reminder patrol, or timer. Python 3.10+ on a
POSIX system is enough; runtime code uses only the standard library.

## Run it

Use `bin/bridge` from this checkout, or install the console command in your Python
environment with `python3 -m pip install .`. Development checks use
`python3 -m pip install -e '.[dev]'` and `python3 -m pytest -q`.

An administrator prepares a shared directory such as `/srv/bridge`, grants the
participating accounts access, and writes `agents.conf` there. See
[the permission model](docs/design.md#permissions) before deploying. This CLI
does not install services, change ACLs, create accounts, or migrate old inboxes.
Keep the data root separate from this repository's `bridge/` Python package.

```text
# agent  Unix-account  optional command with arguments
alice    svc-a
bob      svc-b          /srv/bridge-tools/notify-example.sh bob
ops      svc-a          /srv/bridge-tools/notify-example.sh ops
```

Commands are split using shell-style quoting, but run directly without a shell.
Accounts must already exist. Agent ids use lowercase letters, digits, `_` and
`-`, starting with a letter or digit. The configuration and notify executables
are administrator-managed trusted files.

Set `BRIDGE_ROOT=/srv/bridge`, or pass `--root /srv/bridge`. Options such as
`--root` and `--as` work before or after the subcommand. Each `--as` verifies
that the caller's real uid matches the configured Unix account, including on
read commands. Running as root does not bypass that check.

## Make and answer a request

Run the sender's command under `svc-a` and the recipient's commands under
`svc-b` in this example:

```sh
export BRIDGE_ROOT=/srv/bridge
bridge new --as alice --to bob --subject 'Review artifact' --due 2030-01-15 < request.md
# Prints an id such as 20300110-120000-alice-review-artifact
bridge show 20300110-120000-alice-review-artifact
bridge reply --as bob 20300110-120000-alice-review-artifact --status working < progress.md
bridge reply --as bob 20300110-120000-alice-review-artifact --status blocked < question.md
bridge reply --as alice 20300110-120000-alice-review-artifact < answer.md
bridge close --as bob 20300110-120000-alice-review-artifact < outcome.md
```

The recipient alone sets `working` or `blocked` and closes with a nonempty
outcome on stdin. The sender alone cancels:

```sh
bridge cancel --as alice 20300110-120000-alice-review-artifact < reason.md
```

Either participant may reply. A reply without `--status` preserves the status;
answering a blocked request does not automatically resume it. Active requests
can become `working`, `blocked`, `done`, or `cancelled`. Once working or blocked,
they cannot return to `open`. Terminal requests cannot receive further events.

| Status | Who owes the next move |
| --- | --- |
| `open`, `working` | Recipient |
| `blocked` | Sender, until the sender answers; then recipient |
| `done`, `cancelled` | Nobody |

The full UTF-8 event, including frontmatter, is limited to 64 KiB. Use LF line
endings; control characters other than LF and tab are refused. For larger
artifacts, put a readable file path in the Markdown body. A path is a reference,
not an attachment upload or an access grant.

## Find requests and unread events

```sh
bridge list                                  # All active requests
bridge list --as bob --mine                   # Active requests involving bob
bridge list --as bob --owed                   # Active requests bob owes
bridge list --done                           # All done and cancelled requests
bridge show 20300110-120000-alice-review-artifact --json
bridge unseen --as bob --json
bridge unseen --as bob --mark --json
```

`list` includes age and due date. `list`, `show`, and `unseen` support `--json`.
JSON warnings flag events whose file owner does not match the declared author's
account; text output displays those warnings too. Readers with filesystem access
can see all requests, including completed ones.

`unseen` lists individual events on requests the agent participates in, excluding
its own events. Without `--mark` it changes nothing. `--mark` returns and marks
the same snapshot. Seen keys are `request-id/event-file`, so a move from `open/`
to `done/` never makes a read event unread again. State defaults to
`$XDG_STATE_HOME/bridge/<agent>.seen`, or `~/.local/state/bridge/<agent>.seen`;
`--state-dir` overrides the directory. Keep that state local to the consumer.
Mark only after your consumer has handled the events; repeated delivery can be
deduplicated using each event's JSON `key`. With concurrent arrivals, process the
snapshot returned by `--mark` as well, rather than discarding its output.

## Storage and waking agents

```text
/srv/bridge/
  agents.conf
  open/.bell
  open/20300110-120000-alice-review-artifact/
    00-alice.md
    01-bob.md
  done/<completed-id>/...
```

Events are write-once Markdown files. Status and who owes a reply come from the
latest event. Closing or cancelling writes a terminal event and moves the folder
to `done/`. If the process stops between those actions, `list --done` still finds
it by its event status. Sequence allocation uses hidden write-once reservations
inside each request; see [the design](docs/design.md).

Every write rewrites `open/.bell` in place. The example
[systemd path unit](examples/bridge-wake.path) watches that one file, including
replies in nested request folders. Its paired [service](examples/bridge-wake.service)
checks `unseen` before waking an agent. No timer is needed.

After each event, the other participant's optional notify command runs as the
writing Unix account. It receives **one line on stdin** containing the request
id, author, subject, and `bridge show <id>` pointer. `BRIDGE_ROOT` is set in its
environment. The command can wake any runtime through a site-provided adapter;
Bridge does not depend on a chat transport or a particular agent framework.
It runs for at most ten seconds. Failure produces a warning but the saved event
remains available through `list`, `show`, and `unseen`.

[notify-example.sh](examples/notify-example.sh) shows both direct notifications
and an unseen-based path-unit wake. Customize the adapter and service account
before using the examples. The consumer reads and answers through Bridge; the
notification is only a doorbell. There are no automated reminders.

See [request](examples/handoffs/request.md) and
[closure](examples/handoffs/closure.md) for the event format. The previous API,
tokens, watchers, patrol, and transaction/outbox design remain in Git history.
