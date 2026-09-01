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

## Multi-computer setup

The normal topology uses foreground outbound agents:

```text
ChatGPT
   |
OpenAI Secure MCP Tunnel
   |
Orange Pi hub (always on)
   |-- hub itself
   |
   +-- foreground client connections from other computers
```

The Orange Pi keeps the ChatGPT-facing MCP endpoint on `127.0.0.1:8766`. A separate authenticated agent listener runs on port `8767` for computers you want to control.

On another computer, install `host-sandbox` and start:

```bash
host-sandbox connect
```

By default this connects to `http://10.8.0.9:8767` and registers under that computer's hostname. While the command is running, the computer appears dynamically in `list_projects` and ChatGPT can use the same Sandbox-style file/command tools on it. Press `Ctrl-C` (or close the program) and the computer is immediately unregistered; after an unclean crash the hub expires it after a short heartbeat lease.

Overrides are available when needed:

```bash
host-sandbox --name custom-name connect --hub http://other-hub:8767
```

The foreground client also starts a local activity dashboard at `http://127.0.0.1:8765/` unless `--no-dashboard` is supplied.

The agent connection requires a shared token. The hub service installer generates it in `~/.config/host-sandbox/router.env`. Copy that value once to `~/.config/host-sandbox/client.env` on the client (mode `0600`):

```text
HOST_SANDBOX_AGENT_TOKEN=...
```

After that, `host-sandbox connect` needs no arguments. Do not paste this token into ChatGPT.

Static SSH hosts remain supported as an optional fallback, but they are not required for the normal foreground-client workflow.

For ChatGPT, the aggregate MCP endpoint remains:

```text
http://127.0.0.1:8766/mcp
```

Create a **new** OpenAI tunnel and a **new** Host Sandbox plugin/app for this endpoint. The existing Development Sandbox tunnel stays independent.

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

The ChatGPT-facing router defaults to `127.0.0.1:8766`; the separate authenticated foreground-agent listener defaults to `0.0.0.0:8767`. The intended client address is `http://10.8.0.9:8767` over your private VPN.

## Local dashboard

`host-sandbox serve --open` opens the activity screen. It shows observable tool activity such as commands, file operations, jobs, exit states and output. It does not expose hidden model chain-of-thought.

## Current parity status

The initial version implements the core capabilities needed for coding and host administration. Development Sandbox-specific conveniences such as exact session/container lifecycle emulation, tmux interaction, Git patch/status helper calls, image-native MCP content and the remaining batch helper aliases are being mirrored separately so the public tool surface can converge on the existing Development Sandbox.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
