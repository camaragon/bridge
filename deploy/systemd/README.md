# Example deployment units

These units are templates, not drop-in copies of Cameron's live host-specific services.

## Files
- `bridge-api.service` — local Bridge API server
- `bridge-intake-watch@.service` — recipient notify listener template (`%i` = agent name)
- `example-agent.env` — per-agent listener settings consumed by `bridge-intake-watch@.service`

## Expected layout
This example assumes the repo is deployed at `/opt/bridge` and runtime config lives at `/etc/bridge/bridge.env`.
Adjust paths for your host.

## Lifecycle event consumers
`bridge-intake-watch.py` supports per-agent read-only event commands via env vars:
- `BRIDGE_NOTIFY_EVENT_COMMAND_HERMES`
- `BRIDGE_NOTIFY_EVENT_COMMAND_JARVY`
- `BRIDGE_NOTIFY_EVENT_COMMAND_JORDAN`

Each command receives the raw lifecycle JSON event on stdin.

Bridge core should emit/store lifecycle state.
UI/chat-specific notification adapters should live outside Bridge core.
