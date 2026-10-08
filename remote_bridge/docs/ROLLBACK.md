# Rollback

Cada pieza se revierte sola y en cualquier orden; ninguna es requisito de las demás.

## Gateway (servidor)

```bash
sudo /opt/alejandro-gateway/src/remote_bridge/deploy/rollback_gateway.sh            # detener + quitar ruta
sudo /opt/alejandro-gateway/src/remote_bridge/deploy/rollback_gateway.sh --to <stamp> # versión anterior
sudo /opt/alejandro-gateway/src/remote_bridge/deploy/rollback_gateway.sh --purge      # quitar código y unidad
```

`install_gateway.sh` deja copia de todo lo que reemplaza en `/var/backups/alejandro-gateway/<stamp>/`
(incluido `ufw status numbered` previo). La regla de ufw se quita con
`ufw delete allow from 172.16.0.0/12 to any port 8770 proto tcp`.
Quitar `alejandro.yml` hace que Traefik deje de enrutar al instante; no afecta a otros dominios.

## MaxBot

1. Sacar `"remote_bridge"` de `[plugins].enabled` en `bot.toml` (el `.bak` previo queda junto al archivo).
2. Volver el motor al commit anterior: `cd <MAXBOT_DIR> && git checkout main`.
   La rama solo agrega el plugin y `runners/oneshot.py`; heartbeat delega en él con la misma
   lógica, así que volver atrás no cambia datos ni sesiones.
3. Reiniciar la instancia.

`sessions.json` solo gana claves nuevas (`alexa_<chat>`); las existentes no se tocan.

## PC

```powershell
.\remote_bridge\windows\install_connector.ps1 -Remove          # quita la tarea, conserva datos
.\remote_bridge\windows\install_connector.ps1 -Remove -Purge   # además borra token, política y auditoría
```

La tarea `Desktop-MCP` y desktop-mcp no se tocan en ningún momento.

## Alexa

En la consola de Amazon: *Endpoint* vacío o borrar la skill. En el servidor: `ALEXA_SKILL_ID=` vacío
y reiniciar el gateway (la ruta `/alexa/v1` deja de existir).
