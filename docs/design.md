# Design

Bridge keeps requests in a shared filesystem and private seen lists with each
consumer. The CLI has no running service. All participating agents, including
chat-gateway agents, file, receive, and answer requests through Bridge.

## Events and lifecycle

A request id is `YYYYMMDD-HHMMSS-<sender>-<subject-slug>` in UTC. If a name is
already used, creation adds a numeric suffix. `open/<id>/00-<agent>.md` contains
`id`, `from`, `to`, `subject`, `status`, optional `due`, and `at` frontmatter, then
Markdown. Subsequent events contain `id`, `from`, `status`, and `at`.
The frontmatter is a flat `key: value` format, not general YAML.

Events use `O_CREAT|O_EXCL` and explicit mode `0640`. Different authors must
compete for the same sequence number, so a writer exclusively creates a hidden
`.NN` reservation, retrying the next number on collision. It writes and flushes
the complete event there, then hard-links it as `NN-<agent>.md`. Both names refer
to the same write-once inode. Reservations stay in place; there is no lock,
lease, journal, or lock-recovery process. An interrupted unpublished reservation
leaves a harmless gap. Readers use only the published `.md` names and sort
sequence numbers numerically. The filesystem must support hard links and atomic
same-filesystem directory rename; keep `open/` and `done/` on the same filesystem.

The latest event determines status. `open` and `working` owe the recipient;
`blocked` owes the sender; `done` and `cancelled` owe nobody. Only the recipient
sets working/blocked or closes, and closing needs a nonempty outcome. Only the
sender cancels. Either may reply without changing status. Terminal requests
cannot reopen. A close or cancel publishes its event before renaming the folder
to `done/`. Readers search both locations and classify by status, so a crash
between publication and rename still shows a terminal request under `list --done`.

## Permissions

Use access and default ACLs to share the root with existing Unix accounts such
as `svc-a` and `svc-b`. No new group or session restart is required. An
administrator provisions the root, `agents.conf`, and ACLs before use; Bridge
never changes host permissions or installs that policy itself.

For the root, `open/`, `done/`, and descendants, grant every participating account
`rwx` access and default directory ACLs, with no access for other users. Do not
set the sticky bit: either participant must be able to move a request directory.
New CLI directories are explicitly `0770` and new event files `0640`, even under
a restrictive umask. A default ACL entry for a peer is limited by the `0640`
file's read-only group-class mask; on a `0770` directory it retains `rwx`.
This allows reading each other's events and adding events without editing files
owned by another account. An optional setgid bit on provisioned directories can
preserve an existing group; a new group is unnecessary.

Provision `open/.bell` once as `0660` with write access for every participating
account. If absent, the first CLI write creates it as `0660`. Subsequent writes
open/write/close the existing inode, preserving its ownership and ACL. Never
replace it atomically, because a path watcher must keep observing it.
Provision `agents.conf` as administrator-controlled and readable by writers.
Seen lists are per-agent consumer state and are never stored in the shared data
root. Their CLI-created directories and files also use `0770` and `0640`.

These modes express cooperation, not tamper-proof isolation: shared directory
write access permits unlinking or replacing a peer's files. Write-once behavior
is a CLI convention, not a filesystem immutability guarantee. Keep backups if
you need protection from deletion.

## Identity and trust

`agents.conf` maps an agent id to a Unix account and optional executable/arguments.
`--as` compares the caller's real uid to that account; there is no root override.
Readers compare every event's file uid with its declared author's configured
account and flag mismatches, including unknown authors. That is account-level
provenance, not signatures. Multiple agents under one account can declare each
other's names; the `from` field is self-declared within that account.

Do not sync request files between machines in a way that rewrites file ownership.
Remote agents can run the CLI over SSH as their configured account instead.
Treat received Markdown as request data subject to the recipient's own authority
rules. An event does not grant permission to execute its contents.

## Wake model

Every event write updates `open/.bell`, including replies and terminal events.
A systemd `.path`/`.service` pair can wake a consumer when this file changes. A
watch on `open/` alone misses nested reply writes. Path notifications can coalesce;
consumers query `unseen`, which excludes their own events and uses location-free
keys. Seen marks survive movement to `done/`. Consumers should process returned
snapshots and tolerate repeated delivery.

The other participant's configured notify command is an additional doorbell.
It executes without a shell as the writer, receives a one-line pointer on stdin,
and inherits `BRIDGE_ROOT`. A site adapter can forward that pointer to any local,
remote, or chat-gateway agent runtime. It must have the needed authority already;
Bridge never switches users or provisions credentials. Failure or timeout warns
without rolling back the event. The examples leave the actual runtime adapter
to the deployment. There is no server, chat dependency, timer, or reminder patrol.
