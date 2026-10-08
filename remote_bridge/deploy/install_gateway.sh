#!/usr/bin/env bash
# Install / update the Alejandro gateway on an Ubuntu host behind Dokploy's Traefik.
#
#   sudo ./install_gateway.sh            # install or update code + unit, start it
#   sudo ./install_gateway.sh --route --domain alejandro.example.com   # also the Traefik route (DNS first)
#   sudo ./install_gateway.sh --check    # only validate, change nothing
#
# Idempotent. Never touches existing services. Every file it replaces is backed up
# to /var/backups/alejandro-gateway/<timestamp>/ first. Rollback: deploy/rollback_gateway.sh
set -euo pipefail

SRC_REPO="$(cd "$(dirname "$0")/../.." && pwd)"
APP=/opt/alejandro-gateway
ETC=/etc/alejandro-gateway
DATA=/var/lib/alejandro-gateway
UNIT=/etc/systemd/system/alejandro-gateway.service
ROUTE=/etc/dokploy/traefik/dynamic/alejandro.yml
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP=/var/backups/alejandro-gateway/$STAMP
PY="${PYTHON:-python3}"
WITH_ROUTE=0; CHECK=0; DOMAIN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --route) WITH_ROUTE=1 ;;
    --check) CHECK=1 ;;
    --domain) DOMAIN="${2:?--domain needs a value}"; shift ;;
    *) echo "unknown arg $1"; exit 2 ;;
  esac
  shift
done
if [ "$WITH_ROUTE" = 1 ] && ! printf '%s' "$DOMAIN" | grep -Eq '^[a-z0-9.-]+\.[a-z]{2,}$'; then
  echo "--route needs --domain <fqdn>"; exit 2
fi

say() { printf '\n== %s\n' "$*"; }
backup() { [ -e "$1" ] && { mkdir -p "$BACKUP"; cp -a "$1" "$BACKUP/"; echo "backup: $1 -> $BACKUP/"; } || true; }

say "preflight"
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), sys.version'
ip -4 addr show docker0 | grep -q '172.17.0.1/' || { echo "docker0 is not 172.17.0.1 — adjust the unit"; exit 1; }
# A port already held by our own service (re-run / update) is fine; anything else is not.
OWN_PID="$(systemctl show -p MainPID --value alejandro-gateway 2>/dev/null || echo 0)"
for p in 8770 8771; do
  if ss -ltnH "sport = :$p" | grep -q .; then
    if [ "${OWN_PID:-0}" = 0 ] || ! ss -ltnpH "sport = :$p" | grep -q "pid=$OWN_PID,"; then
      echo "port $p is taken by something else"; exit 1
    fi
  fi
done
"$PY" -m py_compile "$SRC_REPO"/remote_bridge/*.py
echo "ok: python, docker0, ports, syntax"
[ "$CHECK" = 1 ] && { echo "check only: nothing changed"; exit 0; }

say "user and directories"
id alejandro-gw >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin alejandro-gw
install -d -o root -g root -m 0755 "$APP"
install -d -o root -g alejandro-gw -m 0750 "$ETC"
install -d -o alejandro-gw -g alejandro-gw -m 0700 "$DATA"

say "code"
[ -d "$APP/src" ] && { mkdir -p "$BACKUP"; cp -a "$APP/src" "$BACKUP/src"; }
rm -rf "$APP/src.new"; mkdir -p "$APP/src.new"
cp -a "$SRC_REPO/remote_bridge" "$APP/src.new/"
rm -rf "$APP/src.new/remote_bridge/.venv" "$APP/src.new/remote_bridge/tests" "$APP/src.new/remote_bridge/__pycache__"
cp "$SRC_REPO/requirements-bridge.txt" "$APP/src.new/"
rm -rf "$APP/src"; mv "$APP/src.new" "$APP/src"
chown -R root:root "$APP/src"

say "venv"
[ -x "$APP/venv/bin/python" ] || "$PY" -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/src/requirements-bridge.txt"

say "secrets"
if [ ! -f "$ETC/gateway.env" ]; then
  umask 027
  sed -e "s|^BRIDGE_MAXBOT_TOKEN=.*|BRIDGE_MAXBOT_TOKEN=$("$PY" -c 'import secrets;print(secrets.token_urlsafe(32))')|" \
      -e "s|^BRIDGE_AGENT_TOKEN=.*|BRIDGE_AGENT_TOKEN=$("$PY" -c 'import secrets;print(secrets.token_urlsafe(32))')|" \
      "$SRC_REPO/remote_bridge/deploy/gateway.env.example" > "$ETC/gateway.env"
  chown root:alejandro-gw "$ETC/gateway.env"; chmod 0640 "$ETC/gateway.env"
  echo "generated $ETC/gateway.env (tokens not printed)"
else
  echo "keeping existing $ETC/gateway.env"
fi

say "systemd unit"
backup "$UNIT"
install -m 0644 "$SRC_REPO/remote_bridge/deploy/alejandro-gateway.service" "$UNIT"
systemd-analyze verify "$UNIT" 2>&1 | grep -v 'docker.service' || true
systemctl daemon-reload
systemctl enable alejandro-gateway >/dev/null
systemctl restart alejandro-gateway
for i in $(seq 1 20); do
  curl -fsS http://172.17.0.1:8770/health >/dev/null 2>&1 && break; sleep 0.5
done
curl -fsS http://172.17.0.1:8770/health && echo
curl -fsS -o /dev/null -w 'internal from loopback without token: %{http_code} (expect 401)\n' \
  http://127.0.0.1:8771/internal/v1/devices || true

if [ "$WITH_ROUTE" = 1 ]; then
  say "firewall (Traefik container -> host:8770 only)"
  mkdir -p "$BACKUP"; ufw status numbered > "$BACKUP/ufw-status-before.txt" 2>&1 || true
  # Same pattern as the host's other proxied services: only Docker networks may reach it.
  # 8770 listens on 172.17.0.1, so it is not reachable on the public IP regardless.
  ufw status | grep -q '8770/tcp.*172.16.0.0/12' || \
    ufw allow from 172.16.0.0/12 to any port 8770 proto tcp comment 'alejandro-gateway via traefik'
  say "traefik route"
  backup "$ROUTE"
  sed "s/alejandro\.example\.com/$DOMAIN/g" "$SRC_REPO/remote_bridge/deploy/traefik-alejandro.yml" > "$ROUTE.tmp"
  chmod 0644 "$ROUTE.tmp" && mv "$ROUTE.tmp" "$ROUTE"
  echo "installed $ROUTE for $DOMAIN (Traefik reloads it by itself)"
fi

say "done"
echo "backups (if any): $BACKUP"
echo "next: python -m remote_bridge.admin add <device-id>   (as alejandro-gw, see docs/INSTALLATION.md)"
