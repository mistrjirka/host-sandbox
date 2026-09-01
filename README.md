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

It exposes one MCP path per computer:

```text
http://127.0.0.1:8766/mcp/laptop
http://127.0.0.1:8766/mcp/grammetry
```

A reverse proxy or OpenAI-supported tunnel can publish only the central router; individual computers need no public inbound port.

## Local dashboard

`host-sandbox serve --open` opens the activity screen. It shows observable tool activity such as commands, file operations, jobs, exit states and output. It does not expose hidden model chain-of-thought.

## Current parity status

The initial version implements the core capabilities needed for coding and host administration. Development Sandbox-specific conveniences such as exact session/container lifecycle emulation, tmux interaction, Git patch/status helper calls, image-native MCP content and the remaining batch helper aliases are being mirrored separately so the public tool surface can converge on the existing Development Sandbox.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
