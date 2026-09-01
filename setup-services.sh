#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/host-sandbox"
HOST_BIN="${HOST_SANDBOX_BIN:-$(command -v host-sandbox 2>/dev/null || true)}"
HOST_BIN="${HOST_BIN:-$HOME/.local/bin/host-sandbox}"
TUNNEL_BIN="${TUNNEL_CLIENT_BIN:-$HOME/programy/tunnel-client/tunnel-client}"
PROFILE="${TUNNEL_PROFILE:-host-sandbox}"

[[ -x "$HOST_BIN" ]] || { echo "host-sandbox not executable: $HOST_BIN" >&2; exit 1; }
[[ -x "$TUNNEL_BIN" ]] || { echo "tunnel-client not executable: $TUNNEL_BIN" >&2; exit 1; }
[[ -f "$HOME/.config/tunnel-client/$PROFILE.yaml" ]] || { echo "Tunnel profile missing: $HOME/.config/tunnel-client/$PROFILE.yaml" >&2; exit 1; }
[[ -f "$CONFIG_DIR/hosts.json" ]] || { echo "Host config missing: $CONFIG_DIR/hosts.json" >&2; exit 1; }

mkdir -p "$USER_UNIT_DIR" "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

# Router defaults to loopback because the OpenAI tunnel is local. Override in
# ~/.config/host-sandbox/router.env if LAN/VPN access is intentionally needed.
if [[ ! -f "$CONFIG_DIR/router.env" ]]; then
  cat > "$CONFIG_DIR/router.env" <<'ENV'
HOST_SANDBOX_ROUTER_BIND=127.0.0.1
HOST_SANDBOX_ROUTER_PORT=8766
HOST_SANDBOX_AGENT_BIND=0.0.0.0
HOST_SANDBOX_AGENT_PORT=8767
ENV
  chmod 600 "$CONFIG_DIR/router.env"
fi
if ! grep -q '^HOST_SANDBOX_AGENT_TOKEN=' "$CONFIG_DIR/router.env"; then
  agent_token="$(python3 - <<'PYTOKEN'
import secrets
print(secrets.token_urlsafe(32))
PYTOKEN
)"
  printf 'HOST_SANDBOX_AGENT_TOKEN=%s\n' "$agent_token" >> "$CONFIG_DIR/router.env"
  chmod 600 "$CONFIG_DIR/router.env"
fi

if [[ ! -f "$CONFIG_DIR/tunnel.env" ]]; then
  key="${CONTROL_PLANE_API_KEY:-${TUNNELKEY:-}}"
  if [[ -z "$key" ]]; then
    echo "No CONTROL_PLANE_API_KEY/TUNNELKEY is exported." >&2
    echo "Export it once, then rerun ./setup-services.sh; the key will be stored in $CONFIG_DIR/tunnel.env with mode 0600." >&2
    exit 1
  fi
  if [[ "$key" == *$'\n'* || "$key" == *$'\r'* ]]; then
    echo "Tunnel key unexpectedly contains a newline." >&2
    exit 1
  fi
  umask 077
  printf 'CONTROL_PLANE_API_KEY=%s\n' "$key" > "$CONFIG_DIR/tunnel.env"
  chmod 600 "$CONFIG_DIR/tunnel.env"
fi

# Generate the units with the paths detected on this machine.
sed \
  -e "s|%h/.local/bin/host-sandbox|$HOST_BIN|g" \
  "$REPO_DIR/systemd/host-sandbox-router.service" > "$USER_UNIT_DIR/host-sandbox-router.service"
sed \
  -e "s|%h/programy/tunnel-client/tunnel-client|$TUNNEL_BIN|g" \
  -e "s|--profile host-sandbox|--profile $PROFILE|g" \
  "$REPO_DIR/systemd/host-sandbox-tunnel.service" > "$USER_UNIT_DIR/host-sandbox-tunnel.service"

systemctl --user daemon-reload
systemctl --user enable --now host-sandbox-router.service
systemctl --user enable --now host-sandbox-tunnel.service

echo
echo "Installed and started user services:"
echo "  host-sandbox-router.service"
echo "  host-sandbox-tunnel.service"
echo
echo "Status:"
echo "  systemctl --user status host-sandbox-router host-sandbox-tunnel"
echo "Logs:"
echo "  journalctl --user -u host-sandbox-router -u host-sandbox-tunnel -f"
echo
echo "Foreground clients connect to port 8767 using the private token in:"
echo "  $CONFIG_DIR/router.env"
echo "Do not paste that token into ChatGPT."
echo
echo "For startup without an interactive login, enable lingering once:"
echo "  sudo loginctl enable-linger $(id -un)"
