from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from .config import load_agents, require_identity
from .frontmatter import dump_document
from .policy import TERMINAL
from .store import create_file, mkdir, Store


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    for flag, options in (
        ("--root", {"help": "shared root (or BRIDGE_ROOT)"}),
        ("--as", {"dest": "actor", "help": "configured agent identity"}),
        ("--state-dir", {"help": "seen-list directory (default: XDG_STATE_HOME/bridge)"}),
        ("--json", {"action": "store_true", "help": "JSON output for read commands"}),
    ):
        common.add_argument(flag, default=argparse.SUPPRESS, **options)
    result = argparse.ArgumentParser(description="Shared-file requests between agents", parents=[common])
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("new", "reply", "close", "cancel", "list", "show", "unseen"):
        command = commands.add_parser(name, parents=[common])
        if name == "new":
            command.add_argument("--to", required=True)
            command.add_argument("--subject", required=True)
            command.add_argument("--due", metavar="YYYY-MM-DD")
        if name in {"reply", "close", "cancel", "show"}:
            command.add_argument("id")
        if name == "reply":
            command.add_argument("--status", choices=["working", "blocked"])
        if name == "list":
            filters = command.add_mutually_exclusive_group()
            for flag in ("--mine", "--owed", "--done"):
                filters.add_argument(flag, action="store_true")
        if name == "unseen":
            command.add_argument("--mark", action="store_true", help="mark the returned events seen")
    return result


def unseen(store: Store, actor: str, state: Path, mark: bool) -> list[dict]:
    seen_file = state / f"{actor}.seen"
    seen = set(seen_file.read_text(encoding="utf-8").splitlines()) if seen_file.exists() else set()
    found = []
    for request in store.requests():
        if actor not in {request["from"], request["to"]}:
            continue
        for event in request["events"]:
            if event["from"] != actor and event["key"] not in seen:
                found.append({**event, "subject": request["subject"], "request_status": request["status"]})
    if mark and found:
        missing = []
        parent = state
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            mkdir(directory)
        # Append-only marking avoids losing another invocation's marks.
        try:
            create_file(seen_file, "")
        except FileExistsError:
            pass
        with seen_file.open("a", encoding="utf-8") as stream:
            stream.write("".join(event["key"] + "\n" for event in found))
    return found


def notify(store: Store, ident: str, actor: str) -> None:
    request = store.load(ident)
    target = request["to"] if actor == request["from"] else request["from"]
    command = store.agents[target].notify
    if not command:
        return
    pointer = f"bridge {ident} from {actor}: {request['subject']} - run bridge show {ident}\n"
    try:
        subprocess.run(command, input=pointer, text=True, check=True, timeout=10,
                       env={**os.environ, "BRIDGE_ROOT": str(store.root)},
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"warning: event saved; notify {target} failed: {exc}", file=sys.stderr)


def summary(request: dict) -> dict:
    return {key: value for key, value in request.items() if key != "events"}


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    root = getattr(args, "root", None) or os.environ.get("BRIDGE_ROOT")
    if not root:
        arguments.error("set --root or BRIDGE_ROOT")
    actor = getattr(args, "actor", None)
    try:
        store = Store(Path(root).expanduser().resolve(), load_agents(Path(root).expanduser()))
        if actor:
            require_identity(store.agents, actor)
        command = args.command
        if command in {"new", "reply", "close", "cancel", "unseen"} and not actor:
            raise ValueError(f"{command} requires --as")
        if command in {"new", "reply", "close", "cancel"}:
            if getattr(args, "json", False):
                raise ValueError("--json is available on read commands")
            # Decode bytes ourselves to reject CRLF instead of stdin's newline conversion.
            body = sys.stdin.buffer.read(64 * 1024 + 1).decode("utf-8")
            if command == "new":
                ident = store.new(actor, args.to, args.subject, body, args.due)
            else:
                ident = args.id
                status = {"close": "done", "cancel": "cancelled"}.get(command, getattr(args, "status", None))
                store.append(ident, actor, body, status)
            notify(store, ident, actor)
            print(ident)
            return 0
        if command == "show":
            result = store.load(args.id)
            if getattr(args, "json", False):
                print(json.dumps(result))
            else:
                print(f"{result['id']}  {result['status']}  owes={result['owes'] or '-'}")
                for event in result["events"]:
                    print(f"\n# {event['file']}")
                    for warning in event["warnings"]:
                        print(f"WARNING: {warning}")
                    meta = {key: value for key, value in event.items()
                            if key in {"id", "from", "to", "subject", "status", "due", "at"}}
                    print(dump_document(meta, event["body"]))
            return 0
        if command == "unseen":
            default = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "bridge"
            result = unseen(store, actor, Path(getattr(args, "state_dir", default)).expanduser(), args.mark)
        else:
            if (args.mine or args.owed) and not actor:
                raise ValueError("--mine and --owed require --as")
            result = [summary(r) for r in store.requests()
                      if (r["status"] in TERMINAL) == args.done
                      and (not args.mine or actor in {r["from"], r["to"]})
                      and (not args.owed or r["owes"] == actor)]
        if getattr(args, "json", False):
            print(json.dumps(result))
        else:
            for item in result:
                if command == "unseen":
                    print(f"{item['key']}  from={item['from']}  {item['subject']}")
                else:
                    print(f"{item['id']}  {item['status']}  owes={item['owes'] or '-'}  "
                          f"age={item['age_seconds'] // 3600}h  due={item['due'] or '-'}  {item['subject']}")
                for warning in item["warnings"]:
                    print(f"  WARNING: {warning}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"bridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
