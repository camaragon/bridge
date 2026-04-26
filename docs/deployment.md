# Deployment

## Expected deployment pieces
- repo checkout, for example `/opt/bridge`
- env file, for example `/etc/bridge/bridge.env`
- runtime directory referenced by `BRIDGE_ROOT`
- optional systemd user or system services

## Runtime prep
```bash
cp config/bridge_api.example.env config/bridge_api.env
mkdir -p bridge/incoming bridge/outgoing bridge/archive bridge/audit
```

## Start local API
```bash
python3 scripts/bridge_api_server.py
```

## Start recipient listener
```bash
python3 scripts/bridge_intake_watch.py --agent agent-b --listen --port 8522
```

## Systemd templates
Generic examples live in [`../deploy/systemd/`](../deploy/systemd/):
- `bridge-api.service`
- `bridge-intake-watch@.service`
- `example-agent.env`

Template-specific notes remain in [`../deploy/systemd/README.md`](../deploy/systemd/README.md).

## Event adapters
Use `BRIDGE_NOTIFY_URL_<AGENT_ID>` for immediate recipient notifications.
Use `BRIDGE_NOTIFY_EVENT_COMMAND_<AGENT_ID>` for read-only lifecycle consumers.

Adapters should stay outside `bridge_core/`.
