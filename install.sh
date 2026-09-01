#!/usr/bin/env bash
set -euo pipefail
PREFIX="${PREFIX:-$HOME/.local}"
REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$PREFIX/bin" "$PREFIX/lib/host-sandbox"
rm -rf "$PREFIX/lib/host-sandbox/host_sandbox"
cp -a "$REPO_DIR/host_sandbox" "$PREFIX/lib/host-sandbox/"
cat > "$PREFIX/bin/host-sandbox" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$PREFIX/lib/host-sandbox\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m host_sandbox.cli "\$@"
EOF
chmod +x "$PREFIX/bin/host-sandbox"
printf 'Installed %s\n' "$PREFIX/bin/host-sandbox"
case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *)
    printf 'For this shell, run: export PATH="%s/bin:$PATH"\n' "$PREFIX"
    if [[ "$PREFIX" == "$HOME/.local" ]]; then
      line='export PATH="$HOME/.local/bin:$PATH"'
      touch "$HOME/.bashrc"
      grep -Fqx "$line" "$HOME/.bashrc" || printf '\n%s\n' "$line" >> "$HOME/.bashrc"
      printf 'Also added ~/.local/bin to ~/.bashrc for future Bash shells.\n'
    fi
    ;;
esac
printf 'Local-only: host-sandbox --name "My Laptop" serve --open\n'
printf 'LAN/VPN:    host-sandbox --name "hub" serve --bind 0.0.0.0 --token <secret>\n'
