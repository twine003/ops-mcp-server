#!/usr/bin/env bash
# Roll the Alejandro gateway back.
#
#   sudo ./rollback_gateway.sh                 # stop + disable + unroute (keeps data, secrets, code)
#   sudo ./rollback_gateway.sh --to <stamp>    # restore code/unit from /var/backups/alejandro-gateway/<stamp>
#   sudo ./rollback_gateway.sh --purge         # also remove code, venv, unit, user (data kept in a tarball)
#
# Nothing else on the host depends on this service: rolling it back cannot break
# Traefik, Dokploy or MaxBot (the MaxBot plugin just logs "gateway poll failed").
set -euo pipefail
ROUTE=/etc/dokploy/traefik/dynamic/alejandro.yml
UNIT=/etc/systemd/system/alejandro-gateway.service
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
MODE="${1:-}"

if [ -f "$ROUTE" ]; then
  mkdir -p /var/backups/alejandro-gateway/removed-$STAMP
  mv "$ROUTE" /var/backups/alejandro-gateway/removed-$STAMP/
  echo "route removed (Traefik drops it by itself)"
fi

if [ "$MODE" = "--to" ]; then
  SRC="/var/backups/alejandro-gateway/${2:?stamp required}"
  [ -d "$SRC/src" ] && { rm -rf /opt/alejandro-gateway/src; cp -a "$SRC/src" /opt/alejandro-gateway/src; }
  [ -f "$SRC/alejandro-gateway.service" ] && cp -a "$SRC/alejandro-gateway.service" "$UNIT"
  [ -f "$SRC/alejandro.yml" ] && cp -a "$SRC/alejandro.yml" "$ROUTE"
  systemctl daemon-reload && systemctl restart alejandro-gateway
  echo "restored $SRC"; exit 0
fi

systemctl disable --now alejandro-gateway 2>/dev/null || true
echo "service stopped and disabled"

if [ "$MODE" = "--purge" ]; then
  tar czf /var/backups/alejandro-gateway/data-$STAMP.tgz -C /var/lib alejandro-gateway /etc/alejandro-gateway 2>/dev/null || true
  rm -f "$UNIT"; systemctl daemon-reload
  rm -rf /opt/alejandro-gateway
  echo "purged code and unit; data+secrets archived in /var/backups/alejandro-gateway/data-$STAMP.tgz"
  echo "(user alejandro-gw and /var/lib/alejandro-gateway left in place; remove by hand if wanted)"
fi
