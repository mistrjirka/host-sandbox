# host-sandbox

Host-native MCP tool bridge inspired by the Development Sandbox. It runs tools directly as your normal OS user instead of inside a container and includes a local audit dashboard.

## What it can do

- unrestricted command execution as the launching user
- separate paginated stdout/stderr durable jobs, persistent recovery, termination, deletion and cleanup
- text reads/writes plus native MCP binary resources and native image content (up to 16 MiB)
- batched `read_files`, `search_many`, and concurrent `exec_commands`
- Git patch validation/application and bounded status/diff inspection
- persistent interactive tmux terminal reads, text input and control/navigation keys
- host-scoped durable resource locks such as `gpu:all`, with an independent bounded lock-wait timeout
- process listing/signalling and repository/path discovery
- foreground outbound clients that appear only while `host-sandbox connect` is running
- local activity dashboard plus an always-on Orange Pi router/OpenAI tunnel
- optional static SSH hosts as a fallback transport

This deliberately has **no container boundary**. Anything the launching user can modify can also be modified through the tool bridge.

### Durable job lifecycle

`exec_command` and `exec_commands` keep long work in durable host-side runners. `timeout_seconds` limits execution **after** resource locks are acquired; `resource_lock_wait_seconds` independently limits how long a job may wait for locks such as `gpu:all` (30 seconds by default). Locks remain held for the complete job lifetime, not merely until the initial MCP response returns.

Job metadata and output live under the configured state directory, so a restarted `host-sandbox` controller can recover and terminate jobs that are still running. When a job finishes, times out, or is cancelled, descendants that detached into a new process group are also cleaned up.

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

The foreground client also starts a local activity dashboard at `http://127.0.0.1:8765/` unless `--no-dashboard` is supplied. Interactive terminal tools require `tmux` on that controlled computer; all other tools remain usable without tmux.

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

Version 0.5.1 exposes the same 30 public tool names as the current Development Sandbox, including `search_many`, `read_files`, native `read_binary_file` / `view_image`, Git helpers, separate stdout/stderr job pagination, job cleanup, resource locks, and tmux terminal controls. Binary and image payloads are returned as native MCP `EmbeddedResource` / `ImageContent` rather than duplicated inside structured JSON.

The intentional architectural difference is session lifecycle: Development Sandbox sessions are isolated containers attached to persistent `/workspace`; Host Sandbox sessions are lightweight logical handles selecting a real computer and execute with the permissions of the user who started `host-sandbox connect`. Stopping that foreground client removes the computer from the hub. Static SSH transport remains optional for machines where that behavior is desired.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
