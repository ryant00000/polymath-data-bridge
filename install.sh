#!/usr/bin/env bash
# Install the bridge for the current user.  Does not start it.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bindir="${HOME}/.local/bin"
cfgdir="${HOME}/.config/polymath-data-bridge"
unitdir="${HOME}/.config/systemd/user"

install -d -m 755 "$bindir" "$cfgdir" "$unitdir"
install -m 755 "$here/polymath_data_bridge.py" "$bindir/polymath-data-bridge"

if [[ -f "$cfgdir/config.json" ]]; then
  echo "Keeping existing config: $cfgdir/config.json"
else
  install -m 600 "$here/config.example.json" "$cfgdir/config.json"
  echo "Installed example config: $cfgdir/config.json"
fi

install -m 644 "$here/polymath-data-bridge.service" "$unitdir/polymath-data-bridge.service"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload || true
fi

cat <<EOF

Installed:
  $bindir/polymath-data-bridge
  $cfgdir/config.json
  $unitdir/polymath-data-bridge.service

Next:
  1. Edit $cfgdir/config.json (set the URLs and uncomment at least one tile).
  2. Test without Polymath:
       $bindir/polymath-data-bridge --config $cfgdir/config.json --dry-run --once
  3. First real run (approve the Polymath prompt):
       $bindir/polymath-data-bridge --config $cfgdir/config.json --once
  4. Start at login (systemd):
       systemctl --user enable --now polymath-data-bridge.service
  5. Follow logs:
       journalctl --user -u polymath-data-bridge -f

EOF
