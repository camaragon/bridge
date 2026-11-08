"""Lifecycle rules; a reply without a status preserves the current status."""

STATUSES = {"open", "working", "blocked", "done", "cancelled"}
TERMINAL = {"done", "cancelled"}
STATUS_TRANSITIONS = {
    "open": {"open", "working", "blocked", "done", "cancelled"},
    "working": {"working", "blocked", "done", "cancelled"},
    "blocked": {"blocked", "working", "done", "cancelled"},
    "done": set(),
    "cancelled": set(),
}


def require_change(current: str, status: str, actor: str, sender: str,
                   recipient: str, body: str, *, explicit: bool = True) -> None:
    if actor not in {sender, recipient}:
        raise ValueError("only sender or recipient may reply")
    if status not in STATUS_TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid transition: {current} -> {status}")
    if status == "cancelled" and actor != sender:
        raise ValueError("only the sender may cancel")
    if status == "done":
        if actor != recipient:
            raise ValueError("only the recipient may close")
        if not body.strip():
            raise ValueError("closing requires an outcome on stdin")
    if explicit and status in {"working", "blocked"} and actor != recipient:
        raise ValueError("only the recipient may set working or blocked")


def owes(status: str, sender: str, recipient: str) -> str | None:
    if status in TERMINAL:
        return None
    return sender if status == "blocked" else recipient
