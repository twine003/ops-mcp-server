#!/usr/bin/env bash
# Install (or repair) the MCP Server as a systemd service on Linux.
# Run as root (or via sudo).
#
# Safe to re-run any time on an already-installed machine: it re-checks and
# repairs each step rather than assuming a fresh install (kills a stuck
# process still holding the port, re-verifies server_linux.py isn't
# corrupted before touching the service, rewrites+reloads the systemd unit,
# and reprints the API key that's already configured if you don't pass one).
#
# Usage:
#   sudo ./install_linux.sh [--port 8001] [--api-key KEY] [--web-service NAME]
#
# --web-service is optional: the systemd unit that fronts the web app
# (nginx, gunicorn, ...), restarted by the service_restart tool / full_deploy.
# Leave it unset if you don't want that wired up yet.
#
# Note on network mounts: unlike the Windows installer, this script does
# NOT need to relocate itself off a network share. Windows services run as
# SYSTEM, which cannot see per-user SMB drive mappings (net use) - but on
# Linux, NFS/CIFS mounts set up in /etc/fstab (or autofs) are system-wide,
# so a systemd service (running as root) can read them the same as any
# local path. If you copied this folder from a share, it's fine to run it
# from there directly - though copying to a local path first is still
# slightly more robust against the share being briefly unavailable at boot.

set -uo pipefail

SERVICE_NAME="ops-mcp"
PORT="8001"
API_KEY=""
WEB_SERVICE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --api-key) API_KEY="$2"; shift 2 ;;
        --web-service) WEB_SERVICE="$2"; shift 2 ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SERVER_SCRIPT="$SCRIPT_DIR/server_linux.py"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
VENV_DIR="$SCRIPT_DIR/venv"
ENV_DIR="/etc/ops-mcp"
ENV_FILE="$ENV_DIR/mcp.env"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

c_cyan='\033[36m'; c_green='\033[32m'; c_yellow='\033[33m'; c_red='\033[31m'; c_reset='\033[0m'
info()  { echo -e "${c_cyan}$1${c_reset}"; }
ok()    { echo -e "${c_green}$1${c_reset}"; }
warn()  { echo -e "${c_yellow}$1${c_reset}"; }
error() { echo -e "${c_red}$1${c_reset}"; }

echo ""
info "=== Ops MCP Server Installation / Repair (Linux) ==="
echo ""
echo "Project Root: $PROJECT_ROOT"
echo "Port: $PORT"
echo ""

if [[ "$(id -u)" -ne 0 ]]; then
    error "ERROR: this script must be run as root (use sudo)."
    exit 1
fi

# =============================================================================
# Python detection / install
# =============================================================================

info "Checking for Python 3..."
if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 not found - installing via the system package manager..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q python3 python3-pip
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q python3 python3-pip
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --noconfirm python python-pip
    else
        error "ERROR: no known package manager found (apt-get/dnf/yum/pacman)."
        error "Install Python 3 manually and re-run this script."
        exit 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        error "ERROR: python3 still not found after attempted install."
        exit 1
    fi
fi
ok "Found Python: $(python3 --version)"
echo ""

# =============================================================================
# Virtualenv + dependencies
# =============================================================================
# A venv avoids the "externally-managed-environment" pip error on modern
# Debian/Ubuntu, and keeps fastmcp isolated from the system Python.

info "Setting up virtualenv at $VENV_DIR..."
if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
    python3 -m venv "$VENV_DIR" || {
        warn "python3-venv module missing - trying to install it..."
        if command -v apt-get >/dev/null 2>&1; then
            apt-get install -y -qq python3-venv
        fi
        python3 -m venv "$VENV_DIR"
    }
fi
PYTHON="$VENV_DIR/bin/python3"

if [[ ! -x "$PYTHON" ]]; then
    error "ERROR: could not create the virtualenv at $VENV_DIR."
    exit 1
fi
ok "Virtualenv ready."
echo ""

info "Installing Python dependencies (fastmcp, python-dotenv)..."
"$PYTHON" -m pip install --quiet --upgrade pip
if ! "$PYTHON" -m pip install --quiet -r "$REQUIREMENTS_FILE"; then
    error "ERROR: pip install failed. Check internet access on this machine."
    exit 1
fi
ok "Dependencies installed."
echo ""

# =============================================================================
# Sanity-check server_linux.py BEFORE touching the service
# =============================================================================
# Catches a corrupted/incomplete file (e.g. an interrupted deploy) with a
# clear error instead of installing a service that crash-loops.

info "Checking server_linux.py compiles cleanly..."
if ! "$PYTHON" -m py_compile "$SERVER_SCRIPT"; then
    error "ERROR: server_linux.py failed to compile - it looks corrupted or incomplete."
    error "Re-copy this project folder from its known-good source and re-run this installer."
    exit 1
fi
ok "OK: server_linux.py compiles cleanly."
echo ""

# =============================================================================
# Firewall
# =============================================================================
# Best-effort, idempotent: only opens $PORT, doesn't touch other rules.

info "Opening firewall port $PORT..."
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow "${PORT}/tcp" comment "$SERVICE_NAME" >/dev/null
    ok "ufw rule added/confirmed for ${PORT}/tcp."
elif command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
    firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null
    firewall-cmd --reload >/dev/null
    ok "firewalld rule added/confirmed for ${PORT}/tcp."
else
    warn "No active ufw/firewalld detected - skipping (open the port manually if you have another firewall)."
fi
echo ""

# =============================================================================
# API key: use --api-key if given, otherwise keep the existing one from the
# env file, otherwise generate a new one. Either way, print it clearly -
# re-running this script with no --api-key is the supported way to find out
# what key is already set.
# =============================================================================

mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"

EXISTING_KEY=""
if [[ -f "$ENV_FILE" ]]; then
    EXISTING_KEY="$(grep -m1 '^MCP_API_KEY=' "$ENV_FILE" | cut -d= -f2- || true)"
fi

if [[ -n "$API_KEY" ]]; then
    FINAL_KEY="$API_KEY"
    ok "API key set from --api-key argument."
elif [[ -n "$EXISTING_KEY" ]]; then
    FINAL_KEY="$EXISTING_KEY"
    ok "API key already configured on this machine (unchanged)."
else
    if command -v openssl >/dev/null 2>&1; then
        FINAL_KEY="$(openssl rand -hex 24)"
    else
        FINAL_KEY="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(24))')"
    fi
    warn "No API key existed on this machine - generated a new one."
fi

cat > "$ENV_FILE" <<EOF
MCP_API_KEY=${FINAL_KEY}
MCP_PORT=${PORT}
MCP_HOST=0.0.0.0
WEB_SERVICE_NAME=${WEB_SERVICE}
EOF
chmod 600 "$ENV_FILE"
echo ""

# =============================================================================
# Repair: kill anything already holding the port
# =============================================================================

info "Checking for a stuck process on port $PORT..."
if command -v fuser >/dev/null 2>&1; then
    fuser -k "${PORT}/tcp" 2>/dev/null || true
elif command -v lsof >/dev/null 2>&1; then
    PIDS="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
    if [[ -n "$PIDS" ]]; then
        echo "$PIDS" | xargs -r kill -9
    fi
fi
sleep 1

# =============================================================================
# systemd unit
# =============================================================================

info "Writing systemd unit $UNIT_FILE..."
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Ops MCP Server
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_ROOT}
EnvironmentFile=${ENV_FILE}
ExecStart=${PYTHON} ${SERVER_SCRIPT} --port ${PORT} --host 0.0.0.0
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"
ok "Service '$SERVICE_NAME' (re)started."
echo ""

# =============================================================================
# Verify it's actually up
# =============================================================================

HEALTH_URL="http://127.0.0.1:${PORT}/health"
info "Waiting for the server to come up on port $PORT..."
UP=false
for i in $(seq 1 20); do
    sleep 3
    if curl -s -o /dev/null -w "" --max-time 5 "$HEALTH_URL" 2>/dev/null; then
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" 2>/dev/null)"
        if [[ "$code" == "200" ]]; then
            UP=true
            break
        fi
    fi
done

echo ""
if $UP; then
    ok "SUCCESS: server is up and responding at $HEALTH_URL"
else
    warn "WARNING: could not confirm the server is up at $HEALTH_URL after 60s."
    warn "Check: systemctl status $SERVICE_NAME"
    warn "Logs:  journalctl -u $SERVICE_NAME -n 50 --no-pager"
    warn "This script is safe to re-run - it will repair a stuck process, a"
    warn "corrupted server_linux.py (report only, no auto-fix without a source"
    warn "copy to restore from), and a wrong/uncollected API key."
fi
echo ""

REACHABLE_IPS="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^127\.' | grep -v '^$' | sort -u || true)"

echo "========================================================"
echo " API KEY: ${FINAL_KEY}"
echo "========================================================"
echo " Use this in .mcp.json as the Bearer token for this server."
echo ""
echo " Reachable at (from another machine on the LAN):"
if [[ -n "$REACHABLE_IPS" ]]; then
    while IFS= read -r ip; do
        echo "   http://${ip}:${PORT}/mcp"
    done <<< "$REACHABLE_IPS"
else
    echo "   (no non-loopback IPv4 address found - check network interfaces)"
fi
echo ""

if $UP; then
    info "Available tools:"
    "$PYTHON" - "$HEALTH_URL" "$FINAL_KEY" <<'PYEOF'
import sys, json, urllib.request

health_url = sys.argv[1]
api_key = sys.argv[2]
mcp_url = health_url.replace("/health", "/mcp")

def post(payload):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(mcp_url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8", errors="replace")

try:
    post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "installer", "version": "1.0"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    body = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    data_line = next((l for l in body.splitlines() if l.startswith("data:")), None)
    tools = json.loads(data_line[5:].strip())["result"]["tools"] if data_line else []
    for t in sorted(tools, key=lambda t: t["name"]):
        desc = t.get("description", "")
        if len(desc) > 78:
            desc = desc[:75] + "..."
        print(f"  {t['name']:<20} {desc}")
except Exception as e:
    print(f"(could not list tools: {e})")
PYEOF
    echo ""
fi

echo "Commands:"
echo "  systemctl status $SERVICE_NAME             - Check status"
echo "  systemctl restart $SERVICE_NAME            - Restart"
echo "  systemctl stop $SERVICE_NAME               - Stop"
echo "  journalctl -u $SERVICE_NAME -f              - Follow logs"
echo "  systemctl disable --now $SERVICE_NAME       - Remove from boot + stop"
echo ""
echo "Re-running this script any time (no arguments needed) re-checks and"
echo "repairs the install, and reprints the current API key above."
