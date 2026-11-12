#!/usr/bin/env bash
# Install (or repair) the desktop MCP server (desktop tools only) as a systemd
# service on a Linux/X11 machine, inside a desktop user's graphical session.
#
#   sudo ./install_desktop_linux.sh --user escritorio [--display :0] [--port 8011] [--host 127.0.0.1]
#
# - Copies desktop/ + mcp_common.py to /opt/desktop-mcp/app and builds
#   /opt/desktop-mcp/venv (with --system-site-packages: python3-gi and tkinter
#   come from the distribution).
# - Installs the system packages it needs (python3-venv python3-tk python3-gi
#   gir1.2-atspi-2.0 at-spi2-core libatk-adaptor) when apt-get exists.
# - The API key is read from /etc/desktop-mcp/desktop-mcp.env
#   (DESKTOP_MCP_API_KEY=..., chmod 600, root only). If the file does not
#   exist a random key is generated and NOT printed: read it as root from that
#   file (or put your own there and restart the service).
# - The unit runs as --user with DISPLAY/XAUTHORITY of that session and keeps
#   retrying until the X server is up (autologin with LightDM/GDM/SDDM).
#
# Safe to re-run: it rewrites the code, the venv packages and the unit.

set -euo pipefail

DESK_USER=""
DISPLAY_NUM=":0"
PORT="8011"
HOST="127.0.0.1"
SERVICE="desktop-mcp"
PREFIX="/opt/desktop-mcp"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) DESK_USER="$2"; shift 2 ;;
        --display) DISPLAY_NUM="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --service-name) SERVICE="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

[[ "$(id -u)" -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ -n "$DESK_USER" ]] || { echo "--user <desktop user> is required" >&2; exit 1; }
id "$DESK_USER" >/dev/null 2>&1 || { echo "user $DESK_USER does not exist" >&2; exit 1; }
DESK_HOME="$(getent passwd "$DESK_USER" | cut -d: -f6)"
DESK_UID="$(id -u "$DESK_USER")"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
        python3-venv python3-tk python3-gi gir1.2-atspi-2.0 at-spi2-core libatk-adaptor x11-utils >/dev/null
fi

echo "== code -> $PREFIX/app"
mkdir -p "$PREFIX/app"
rm -rf "$PREFIX/app/desktop"
cp -r "$SRC/desktop" "$PREFIX/app/desktop"
rm -rf "$PREFIX/app/desktop/.venv" "$PREFIX/app/desktop/__pycache__" "$PREFIX/app/desktop/x11/__pycache__"
cp "$SRC/mcp_common.py" "$PREFIX/app/"

echo "== venv"
[[ -x "$PREFIX/venv/bin/python" ]] || python3 -m venv --system-site-packages "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip >/dev/null
"$PREFIX/venv/bin/pip" install -q -r "$SRC/requirements-desktop-linux.txt"
"$PREFIX/venv/bin/python" -c "import fastmcp, Xlib, mss, PIL, numpy, gi, tkinter" \
    || { echo "dependency check failed" >&2; exit 1; }

echo "== key"
mkdir -p /etc/desktop-mcp
ENV_FILE="/etc/desktop-mcp/$SERVICE.env"
if [[ ! -s "$ENV_FILE" ]]; then
    umask 077
    echo "DESKTOP_MCP_API_KEY=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)" > "$ENV_FILE"
    echo "   new key generated in $ENV_FILE (not shown)"
fi
chmod 600 "$ENV_FILE"; chown root:root "$ENV_FILE"

echo "== unit /etc/systemd/system/$SERVICE.service"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=desktop-mcp (screen, mouse, keyboard for an AI agent) on $DESK_USER's X session $DISPLAY_NUM
After=display-manager.service network-online.target
Wants=display-manager.service

[Service]
Type=simple
User=$DESK_USER
EnvironmentFile=$ENV_FILE
Environment=DISPLAY=$DISPLAY_NUM
Environment=XAUTHORITY=$DESK_HOME/.Xauthority
Environment=XDG_RUNTIME_DIR=/run/user/$DESK_UID
Environment=DESKTOP_MCP_HOST=$HOST
Environment=DESKTOP_MCP_PORT=$PORT
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$PREFIX/app
# wait for the autologin session's X server (up to 2 min), then run
ExecStartPre=/usr/bin/timeout 120 /bin/sh -c 'until xdpyinfo >/dev/null 2>&1; do sleep 1; done'
ExecStart=$PREFIX/venv/bin/python $PREFIX/app/desktop/serve.py --host $HOST --port $PORT
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=graphical.target
UNIT
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"
sleep 6
systemctl --no-pager --lines=5 status "$SERVICE" || true
HEALTH_HOST="$HOST"; [[ "$HOST" == "0.0.0.0" ]] && HEALTH_HOST="127.0.0.1"
curl -fsS "http://$HEALTH_HOST:$PORT/health" && echo || echo "health check failed: journalctl -u $SERVICE"
