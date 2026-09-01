# host-sandbox

Host-native MCP tool bridge inspired by the Development Sandbox. It runs tools directly as your normal OS user instead of inside a container and includes a local audit dashboard.

## What it can do

- unrestricted command execution as the launching user
- text and binary file transfer
- file write/replace/copy/move/remove/chmod
- file search and path discovery
- durable command jobs and process signalling
- concurrent command execution
- localhost MCP HTTP endpoint and stdio MCP transport
- live browser dashboard for tool calls, jobs, output and errors
- central multi-computer routing over ordinary SSH

This deliberately has **no container boundary**. Anything the launching user can modify can also be modified through the tool bridge.

## Install on each computer

```bash
git clone https://github.com/mistrjirka/host-sandbox.git
cd host-sandbox
./install.sh
```

Then either run the local UI/MCP server:

```bash
host-sandbox --name laptop serve --open
```

`serve` deliberately binds to `127.0.0.1` by default. To expose a hub on your LAN or VPN, bind explicitly and use authentication:

```bash
host-sandbox --name hub serve --bind 0.0.0.0 --token 'a-long-random-secret'
```

Do not expose an unauthenticated host-control MCP endpoint to the public Internet.

or let the central router launch the stdio transport over SSH on demand. No extra daemon is required for that mode.

## Multi-computer setup

The recommended topology is:

```text
ChatGPT / MCP client
        |
        v
 central server
 host-sandbox router
   |      |      |
  SSH    SSH    SSH
   |      |      |
laptop grammetry server
```

Connect the machines with Tailscale/WireGuard or another private network and configure key-based SSH from the central server.

On the central server create `~/.config/host-sandbox/hosts.json` from `hosts.example.json`:

```json
{
  "hosts": [
    {"name": "hub", "local": true},
    {"name": "laptop", "ssh": "jirka@laptop"},
    {"name": "grammetry", "ssh": "jirka@grammetry"}
  ]
}
```

Start the router:

```bash
HOST_SANDBOX_ROUTER_TOKEN='use-a-long-random-token' \
  host-sandbox router --bind 127.0.0.1 --port 8766
```

For ChatGPT, use the single aggregate endpoint:

```text
http://127.0.0.1:8766/mcp
```

The router exposes configured computers as projects/logical sessions, similar to Development Sandbox. `hub` with `local: true` runs directly on the Orange Pi; remote computers are reached through SSH. Per-host `/mcp/<host>` endpoints remain available for debugging.

Create a **new** OpenAI tunnel and a **new** Host Sandbox plugin/app for this endpoint. The existing Development Sandbox tunnel stays independent and does not need to be modified.

### Wake computers on demand

A remote computer can stay powered off and be woken automatically on the first Host Sandbox tool call. Configure Wake-on-LAN on the hub:

```json
{
  "name": "rtx3090",
  "ssh": "jirka@192.168.50.123",
  "wake_on_lan": {
    "mac": "AA:BB:CC:DD:EE:FF",
    "broadcast": "192.168.50.255",
    "port": 9,
    "timeout_seconds": 120
  }
}
```

The hub first probes SSH. If the host is offline, it sends the standard Wake-on-LAN magic packet and waits until SSH becomes reachable before starting the remote MCP stdio session. `wake_host(project=...)` is also available for an explicit wake. `list_projects`/`/health` report whether a host is wake-capable without waking it.

Wake-on-LAN requires firmware/NIC support and generally works over the local Ethernet broadcast domain; do not use a Tailscale address as the broadcast target.

## Run as systemd user services

On a Linux hub, after `host-sandbox`, `hosts.json`, the tunnel-client binary, and the `host-sandbox` tunnel profile are configured:

```bash
# Keep the runtime key out of the repository. The setup script stores it as a 0600 file.
export TUNNELKEY='your-runtime-key'
./setup-services.sh

# Allow user services to start at boot before you log in.
sudo loginctl enable-linger "$USER"
```

The installer creates and starts:

- `host-sandbox-router.service`
- `host-sandbox-tunnel.service`

Check them with:

```bash
systemctl --user status host-sandbox-router host-sandbox-tunnel
journalctl --user -u host-sandbox-router -u host-sandbox-tunnel -f
```

The router service defaults to `127.0.0.1:8766`, which is sufficient for Secure MCP Tunnel. To intentionally expose it on the LAN/VPN, edit `~/.config/host-sandbox/router.env`, set `HOST_SANDBOX_ROUTER_BIND=0.0.0.0`, and restart the router. If exposing the MCP endpoint to other machines, protect it with a token/firewall/private network.

## Local dashboard

`host-sandbox serve --open` opens the activity screen. It shows observable tool activity such as commands, file operations, jobs, exit states and output. It does not expose hidden model chain-of-thought.

## Current parity status

The initial version implements the core capabilities needed for coding and host administration. Development Sandbox-specific conveniences such as exact session/container lifecycle emulation, tmux interaction, Git patch/status helper calls, image-native MCP content and the remaining batch helper aliases are being mirrored separately so the public tool surface can converge on the existing Development Sandbox.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
