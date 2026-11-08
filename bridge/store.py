from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys

from .config import Agent, agent_id, require_identity
from .frontmatter import dump_document, parse_frontmatter
from .policy import owes, require_change, STATUSES, TERMINAL

EVENT_NAME = re.compile(r"^(\d+)-([a-z0-9][a-z0-9_-]*)\.md$")
REQUEST_ID = re.compile(r"^\d{8}-\d{6}-[a-z0-9][a-z0-9_-]*$")


class Missing(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mkdir(path: Path) -> None:
    try:
        path.mkdir(mode=0o770)
    except FileExistsError:
        if not path.is_dir():
            raise
    else:
        path.chmod(0o770)


def create_file(path: Path, text: str, mode: int = 0o640) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


class Store:
    def __init__(self, root: Path, agents: dict[str, Agent]):
        self.root, self.agents = root, agents

    def locate(self, ident: str) -> Path:
        if not REQUEST_ID.fullmatch(ident):
            raise ValueError("invalid request id")
        for area in ("open", "done"):
            path = self.root / area / ident
            if path.is_dir():
                return path
        raise Missing(f"request not found: {ident}")

    def load(self, ident: str) -> dict:
        try:
            names = list(self.locate(ident).iterdir())
        except FileNotFoundError:
            names = list(self.locate(ident).iterdir())
        paths = sorted((p for p in names if EVENT_NAME.fullmatch(p.name)),
                       key=lambda p: (int(p.name.split("-", 1)[0]), p.name))
        if not paths:
            raise Missing(f"request has no events: {ident}")
        events = []
        for path in paths:
            # A close can move the whole directory during this read.
            try:
                text = path.read_bytes().decode("utf-8")
                uid = path.stat().st_uid
            except FileNotFoundError:
                path = self.locate(ident) / path.name
                text = path.read_bytes().decode("utf-8")
                uid = path.stat().st_uid
            meta, body = parse_frontmatter(text)
            if meta.get("id") != ident or meta.get("status") not in STATUSES:
                raise ValueError(f"invalid event: {ident}/{path.name}")
            author = meta.get("from", "")
            warnings = []
            if author not in self.agents or uid != self.agents[author].uid:
                warnings.append("file owner does not match declared author's account")
            if EVENT_NAME.fullmatch(path.name).group(2) != author:
                warnings.append("filename author does not match frontmatter")
            events.append({**meta, "body": body, "file": path.name,
                           "key": f"{ident}/{path.name}", "owner_uid": uid, "warnings": warnings})
        first = events[0]
        last = next((e for e in events if e["status"] in TERMINAL), events[-1])
        for event in events[events.index(last) + 1:]:
            event["warnings"].append("published after the request ended; ignored for status")
        if any(not first.get(key) for key in ("from", "to", "subject", "at")):
            raise ValueError(f"incomplete request header: {ident}")
        created = datetime.fromisoformat(first["at"])
        age = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
        return {"id": ident, "from": first["from"], "to": first["to"],
                "subject": first["subject"], "due": first.get("due"), "at": first["at"],
                "age_seconds": age, "status": last["status"],
                "owes": owes(last["status"], first["from"], first["to"], last["from"]),
                "events": events, "warnings": [f"{e['file']}: {w}" for e in events for w in e["warnings"]]}

    def requests(self) -> list[dict]:
        ids = set()
        for area in ("open", "done"):
            parent = self.root / area
            if parent.exists():
                ids.update(p.name for p in parent.iterdir() if p.is_dir() and REQUEST_ID.fullmatch(p.name))
        loaded = []
        for ident in sorted(ids):
            try:
                loaded.append(self.load(ident))
            except Missing:
                pass
        return loaded

    def ring(self, ident: str) -> None:
        path = self.root / "open" / ".bell"
        try:
            try:
                create_file(path, ident + "\n", 0o660)
            except FileExistsError:
                # Never replace the inode: a path watcher and shared ACL depend on it.
                with path.open("w", encoding="utf-8") as stream:
                    stream.write(ident + "\n")
        except OSError as exc:
            print(f"warning: event saved; .bell failed: {exc}", file=sys.stderr)

    def publish(self, folder: Path, meta: dict[str, str], body: str) -> str:
        text = dump_document(meta, body)
        sequence = max((int(p.name.split("-", 1)[0]) for p in folder.iterdir()
                        if EVENT_NAME.fullmatch(p.name)), default=-1) + 1
        # A common sequence name coordinates different authors too. Reservations
        # are permanent write-once event inodes, not locks needing stale recovery.
        while True:
            claim = folder / f".{sequence:02d}"
            try:
                create_file(claim, text)
                break
            except FileExistsError:
                sequence += 1
        # Publish only complete text; readers never see partially written headers.
        name = f"{sequence:02d}-{meta['from']}.md"
        os.link(claim, folder / name)
        self.ring(meta["id"])
        return name

    def new(self, actor: str, recipient: str, subject: str, body: str,
            due: str | None = None) -> str:
        require_identity(self.agents, actor)
        agent_id(recipient)
        if recipient not in self.agents or recipient == actor:
            raise ValueError("recipient must be a different configured agent")
        if not subject.strip():
            raise ValueError("subject is required")
        if due:
            datetime.strptime(due, "%Y-%m-%d")
        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:48] or "request"
        base = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + f"-{actor}-{slug}"
        # Validate before creating any request directory.
        meta = {"id": base, "from": actor, "to": recipient, "subject": subject,
                "status": "open", "at": now()}
        if due:
            meta["due"] = due
        dump_document(meta, body)
        for area in ("open", "done"):
            mkdir(self.root / area)
        suffix = 0
        while True:
            ident = base + (f"-{suffix}" if suffix else "")
            folder = self.root / "open" / ident
            if (self.root / "done" / ident).exists():
                suffix += 1
                continue
            try:
                folder.mkdir(mode=0o770)
                folder.chmod(0o770)
                break
            except FileExistsError:
                suffix += 1
        meta["id"] = ident
        self.publish(folder, meta, body)
        return ident

    def append(self, ident: str, actor: str, body: str, status: str | None = None) -> None:
        require_identity(self.agents, actor)
        request = self.load(ident)
        new_status = status or request["status"]
        require_change(request["status"], new_status, actor, request["from"],
                       request["to"], body, explicit=status is not None)
        folder = self.locate(ident)
        name = self.publish(folder, {"id": ident, "from": actor, "status": new_status, "at": now()}, body)
        events = self.load(ident)["events"]
        files = [e["file"] for e in events]
        ended = next((e["file"] for e in events if e["status"] in TERMINAL), name)
        if files.index(ended) < files.index(name):
            print(f"warning: event saved; request already ended by {ended}, so it was ignored for status",
                  file=sys.stderr)
            return
        if new_status in TERMINAL and folder.parent.name == "open":
            try:
                folder.rename(self.root / "done" / ident)
            except OSError as exc:
                # The terminal event is authoritative even if archiving is interrupted.
                print(f"warning: event saved; move to done failed: {exc}", file=sys.stderr)
