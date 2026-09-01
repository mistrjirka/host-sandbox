from __future__ import annotations

import argparse
import os
import platform
import signal
import threading
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .audit import AuditLog
from .core import HostTools
from .http_server import HostHTTPServer, run_http
from .mcp import MCPServer, run_stdio
from .ssh_router import SSHRouter
from .agent_transport import AgentRegistry
from .agent_server import AgentHTTPServer, run_agent_http
from .agent_client import run_connected_agent
from .router_server import RouterHTTPServer, run_router_http


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base).expanduser() / "host-sandbox" if base else Path.home() / ".local/state/host-sandbox"



def default_client_env() -> Path:
    return Path.home() / ".config/host-sandbox/client.env"


def client_agent_token(cli_token: str | None) -> str:
    if cli_token:
        return cli_token
    env_token = os.environ.get("HOST_SANDBOX_AGENT_TOKEN")
    if env_token:
        return env_token
    path = default_client_env()
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "HOST_SANDBOX_AGENT_TOKEN":
                value = value.strip().strip('\"').strip("'")
                if value:
                    return value
    except FileNotFoundError:
        pass
    return ""

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="host-sandbox", description="Unrestricted host-native MCP bridge with a live audit dashboard")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--name", default=os.environ.get("HOST_SANDBOX_NAME") or platform.node() or "computer", help="human-readable computer name")
    p.add_argument("--cwd", default=os.environ.get("HOST_SANDBOX_CWD") or str(Path.home()), help="default working directory for relative paths")
    p.add_argument("--state-dir", default=os.environ.get("HOST_SANDBOX_STATE_DIR") or str(default_state_dir()))
    sub = p.add_subparsers(dest="mode")
    h = sub.add_parser("serve", help="serve MCP over HTTP and show a local dashboard (default)")
    h.add_argument("--bind", default=os.environ.get("HOST_SANDBOX_BIND", "127.0.0.1"))
    h.add_argument("--port", type=int, default=int(os.environ.get("HOST_SANDBOX_PORT", "8765")))
    h.add_argument("--token", default=os.environ.get("HOST_SANDBOX_TOKEN"), help="optional bearer token for /mcp")
    h.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    sub.add_parser("stdio", help="serve MCP over stdio for local MCP clients")
    r = sub.add_parser("router", help="central multi-computer router with one aggregate MCP endpoint")
    r.add_argument("--config", default=os.environ.get("HOST_SANDBOX_HOSTS", str(Path.home()/".config/host-sandbox/hosts.json")))
    r.add_argument("--bind", default=os.environ.get("HOST_SANDBOX_ROUTER_BIND", "127.0.0.1"))
    r.add_argument("--port", type=int, default=int(os.environ.get("HOST_SANDBOX_ROUTER_PORT", "8766")))
    r.add_argument("--token", default=os.environ.get("HOST_SANDBOX_ROUTER_TOKEN"))
    r.add_argument("--agent-bind", default=os.environ.get("HOST_SANDBOX_AGENT_BIND", "0.0.0.0"))
    r.add_argument("--agent-port", type=int, default=int(os.environ.get("HOST_SANDBOX_AGENT_PORT", "8767")))
    r.add_argument("--agent-token", default=os.environ.get("HOST_SANDBOX_AGENT_TOKEN"))
    c = sub.add_parser("connect", help="foreground host agent; control exists only while this command runs")
    c.add_argument("--hub", default=os.environ.get("HOST_SANDBOX_HUB", "http://10.8.0.9:8767"), help="Orange Pi agent endpoint (default: http://10.8.0.9:8767)")
    c.add_argument("--token", default=os.environ.get("HOST_SANDBOX_AGENT_TOKEN"), help="shared agent token")
    c.add_argument("--dashboard-bind", default="127.0.0.1")
    c.add_argument("--dashboard-port", type=int, default=8765)
    c.add_argument("--no-dashboard", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.mode is None: args.mode = "serve"; args.bind = "127.0.0.1"; args.port = 8765; args.token = os.environ.get("HOST_SANDBOX_TOKEN"); args.open = False
    if args.mode == "router":
        registry = AgentRegistry()
        router = SSHRouter(args.config, registry)
        server = RouterHTTPServer((args.bind, args.port), router, args.token, str(Path(args.config).expanduser().with_name("sessions.json")))
        agent_server = None
        agent_thread = None
        if args.agent_token:
            agent_server = AgentHTTPServer((args.agent_bind, args.agent_port), registry, args.agent_token)
            agent_thread = threading.Thread(target=run_agent_http, args=(agent_server,), name="host-sandbox-agent-listener", daemon=True)
            agent_thread.start()
        print(f"Host Sandbox router: http://{args.bind}:{args.port}", flush=True)
        print("Aggregate MCP endpoint: /mcp", flush=True)
        if agent_server is not None:
            print(f"Foreground agent endpoint: http://{args.agent_bind}:{args.agent_port}", flush=True)
        else:
            print("Foreground agent endpoint: disabled (set HOST_SANDBOX_AGENT_TOKEN)", flush=True)
        try:
            run_router_http(server); return 0
        except KeyboardInterrupt:
            return 0
        finally:
            if agent_server is not None:
                agent_server.shutdown(); agent_server.server_close()

    state = Path(args.state_dir).expanduser().resolve()
    if args.mode == "connect":
        return run_connected_agent(hub=args.hub, token=client_agent_token(args.token), name=args.name, cwd=args.cwd, state_dir=state, dashboard_bind=args.dashboard_bind, dashboard_port=args.dashboard_port, dashboard=not args.no_dashboard)

    audit = AuditLog(state); tools = HostTools(audit, state, args.cwd); mcp = MCPServer(tools, audit)
    # Put the identity in every startup audit trail and system_info response context.
    original_system_info = tools.system_info
    def system_info():
        value = original_system_info(); value.update({"host_name": args.name}); return value
    tools.system_info = system_info  # type: ignore[method-assign]
    def stop(*_: object) -> None:
        audit.add("server", "Stopping host-sandbox", status="ok"); tools.close(); raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    audit.add("server", "Host Sandbox starting", status="ok", host_name=args.name, cwd=tools.cwd, pid=os.getpid(), host_mode=True, sandboxed=False)
    try:
        if args.mode == "stdio": return run_stdio(mcp)
        server = HostHTTPServer((args.bind, args.port), mcp, tools, audit, args.name, args.token)
        url = f"http://{args.bind}:{args.port}/"
        print(f"Host Sandbox [{args.name}] — HOST MODE / NOT CONTAINERIZED", flush=True)
        print(f"Dashboard: {url}", flush=True); print(f"MCP:       http://{args.bind}:{args.port}/mcp", flush=True)
        print(f"Working directory: {tools.cwd}", flush=True)
        if args.bind not in {"127.0.0.1", "localhost", "::1"}: print("WARNING: non-loopback bind exposes host-control API to the network.", file=sys.stderr, flush=True)
        if args.open: webbrowser.open(url)
        run_http(server); return 0
    except KeyboardInterrupt: return 0
    finally: tools.close()


if __name__ == "__main__": raise SystemExit(main())
