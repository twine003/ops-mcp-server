# Instalación

Orden: **servidor → DNS + ruta → MaxBot → PC → Alexa.** Cada paso se puede probar solo.

## 1. Gateway en el servidor

```bash
sudo git clone https://github.com/twine003/ops-mcp-server.git /opt/alejandro-bridge-src
cd /opt/alejandro-bridge-src && git checkout feature/alejandro-remote-bridge
sudo remote_bridge/deploy/install_gateway.sh --check     # valida sin tocar nada
sudo remote_bridge/deploy/install_gateway.sh             # instala y arranca (sin ruta pública)
```

Crea: usuario `alejandro-gw`, `/opt/alejandro-gateway/{src,venv}`, `/etc/alejandro-gateway/gateway.env`
(tokens generados, 0640), `/var/lib/alejandro-gateway/` (0700), unidad `alejandro-gateway`.
No toca ningún servicio existente.

## 2. DNS y ruta pública

1. Cloudflare: registro **A** `alejandro.example.com → <IP del servidor>`, **sin proxy** (nube gris):
   el desafío HTTP-01 de Let's Encrypt entra por el 80.
2. `sudo remote_bridge/deploy/install_gateway.sh --route --domain alejandro.example.com` → agrega la regla de ufw (solo redes
   Docker → 8770) y `/etc/dokploy/traefik/dynamic/alejandro.yml`. Traefik la carga solo.
3. `curl https://alejandro.example.com/health` → `{"status":"ok",…}`.

## 3. MaxBot

```bash
cd <MAXBOT_DIR> && git fetch && git checkout feature/remote-bridge-plugin
```

En el `.env` de la instancia de MaxBot (copiar los valores de `/etc/alejandro-gateway/gateway.env`):

```
BRIDGE_MAXBOT_TOKEN=…
BRIDGE_AGENT_TOKEN=…
```

En `bot.toml`:

```toml
[plugins]
enabled = ["tasks", "heartbeat", "banking", "remote_bridge"]

[plugins.remote_bridge]
gateway_url = "http://127.0.0.1:8771"
alexa = true
voice_profile = "restricted"
```

Reiniciar la instancia (su `restart.sh` o `systemctl restart <servicio-maxbot>`). En Telegram: `/pc`.

## 4. PC Windows

En el servidor:

```bash
sudo -u alejandro-gw sh -c 'cd /opt/alejandro-gateway/src && \
  BRIDGE_DATA_DIR=/var/lib/alejandro-gateway /opt/alejandro-gateway/venv/bin/python \
  -m remote_bridge.admin add pc-casa --label "PC de casa"'
```

Imprime el token **una vez**. En la PC (requiere desktop-mcp instalado, `install_service.ps1 -Desktop`):

```powershell
cd <ruta>\ops-mcp-server
git checkout feature/alejandro-remote-bridge
.\remote_bridge\windows\install_connector.ps1 -SetToken          # pegar el token (oculto)
.\remote_bridge\windows\install_connector.ps1 -DeviceId pc-casa -GatewayUrl wss://alejandro.example.com/bridge/v1/ws
.\remote_bridge\windows\install_connector.ps1 -Status
```

Crea la tarea `Alejandro-Connector` (al iniciar sesión, en tu sesión, reinicio cada minuto) con el
venv de desktop-mcp. No modifica la tarea `Desktop-MCP`.

## 5. Alexa

Ver [ALEXA.md](ALEXA.md).
