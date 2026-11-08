"""Shared HTTP boundaries for the unrestricted host-control endpoints.

These checks protect the network entry points, not the capabilities of a
successfully connected MCP client.
"""
from __future__ import annotations

import ipaddress
import json
import os
from urllib.parse import urlsplit
from typing import Any

MAX_JSON_BODY_BYTES = 64 * 1024 * 1024
LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.", "127.0.0.1", "::1"})


class HTTPRequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def is_loopback_bind(host: str) -> bool:
    if host.lower() in LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_token_for_network_bind(host: str, token: str | None) -> None:
    if not is_loopback_bind(host) and not token:
        raise ValueError("binding host-control HTTP to a network interface requires an authentication token")


def request_origin_allowed(headers: Any, bind_host: str) -> bool:
    """Reject browser cross-site requests and DNS-rebinding Host headers.

    MCP clients without Origin keep working. Reverse proxies that preserve a
    public Host header can opt in to its exact hostname through
    HOST_SANDBOX_ALLOWED_HOSTS.
    """
    host = str(headers.get("Host", "")).strip().lower()
    if not host:
        return False
    try:
        parsed = urlsplit("http://" + host)
        hostname = parsed.hostname
        if not hostname or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            return False
        _ = parsed.port  # Reject malformed ports.
    except ValueError:
        return False
    if is_loopback_bind(bind_host):
        trusted = {s.strip().lower() for s in os.environ.get("HOST_SANDBOX_ALLOWED_HOSTS", "").split(",") if s.strip()}
        if hostname not in LOCAL_HOSTNAMES and hostname not in trusted:
            return False

    origin = headers.get("Origin")
    if origin is None:
        return True
    # Browser-originated requests must come from the same host and port.
    # Do not allow Origin: null (file:// or sandboxed documents).
    try:
        parsed_origin = urlsplit(str(origin))
        if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.hostname:
            return False
        return (
            parsed_origin.netloc.lower() == host
            and parsed_origin.username is None
            and parsed_origin.password is None
            and not parsed_origin.path
            and not parsed_origin.query
            and not parsed_origin.fragment
        )
    except ValueError:
        return False


def read_json_body(handler: Any) -> Any:
    content_type = str(handler.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPRequestError(415, "Content-Type must be application/json")
    try:
        n = int(handler.headers.get("Content-Length", ""))
    except ValueError as exc:
        raise HTTPRequestError(411, "Content-Length is required") from exc
    if n <= 0:
        raise HTTPRequestError(400, "Content-Length must be positive")
    if n > MAX_JSON_BODY_BYTES:
        raise HTTPRequestError(413, "JSON request body exceeds 64 MiB")
    handler.connection.settimeout(30)
    raw = handler.rfile.read(n)
    if len(raw) != n:
        raise HTTPRequestError(400, "incomplete JSON request body")
    return json.loads(raw)
