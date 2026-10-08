# Operación

## Estado

| Qué | Cómo |
|---|---|
| Gateway vivo | `systemctl status alejandro-gateway` · `curl -s http://172.17.0.1:8770/health` |
| Desde fuera | `curl -s https://alejandro.example.com/health` |
| PCs conectadas | Telegram `/pc` · `/pc_estado <id>` |
| MaxBot conectado al gateway | `/health` → `"maxbot_polling": true` |
| PC (local) | `install_connector.ps1 -Status` · `%USERPROFILE%\.alejandro-connector\state.json` |

## Logs y auditoría

- Gateway: `journalctl -u alejandro-gateway -f`. Auditoría JSONL: `/var/lib/alejandro-gateway/audit.jsonl`
  (eventos: `device_connected/disconnected`, `device_auth_failed`, `call_done/refused/timeout`,
  `approval_requested/decided`, `alexa_turn`, `alexa_answer`, `alexa_rejected`).
- Latencias de Alexa: `jq 'select(.event=="alexa_turn")' audit.jsonl` (verify_ms, total_ms,
  runner_ms, answer_ms_since_publish, answered).
- MaxBot: `journalctl -u <servicio-maxbot> | grep remote_bridge`.
- PC: `connector.log` (rotado, 3×2 MB) y `audit.jsonl` en `%USERPROFILE%\.alejandro-connector\`.

Ningún log guarda argumentos ni tokens: solo nombres de claves, hash y resultado.

## Tareas habituales

```bash
G='sudo -u alejandro-gw sh -c "cd /opt/alejandro-gateway/src && BRIDGE_DATA_DIR=/var/lib/alejandro-gateway /opt/alejandro-gateway/venv/bin/python -m remote_bridge.admin'
eval "$G list\""                 # dispositivos
eval "$G revoke pc-casa\""   # corta la conexión en ≤5 s; no vuelve a entrar
eval "$G rotate pc-casa\""   # token nuevo (luego -SetToken en la PC)
```

## Recuperación

| Síntoma | Causa probable | Acción |
|---|---|---|
| `/pc` dice "No pude consultar el gateway" | gateway caído | `systemctl restart alejandro-gateway` (Telegram no se ve afectado) |
| PC "desconectada" con la PC encendida | tarea detenida, token revocado, sin red | `-Status`; ver `last_error` y `last_close_code` (4401/4403 = credencial) |
| Alexa: "no está disponible" | MaxBot no está haciendo long-poll | `journalctl -u <servicio-maxbot> | grep remote_bridge` |
| Alexa: siempre "sigo trabajando" | latencia del CLI (ver ALEXA.md) | esperado con el diseño actual; decir "continúa" |
| Alexa no responde nada | cert/DNS/ruta | `curl -I https://alejandro.example.com/health`; logs de Traefik |

Si la tarea no arranca: `crash.log` en esa misma carpeta (con `pythonw` no hay consola).
La carpeta NO está en `%LOCALAPPDATA%` a propósito: los procesos lanzados desde apps empaquetadas
(MSIX) escriben ahí en una copia virtual que la tarea programada no ve.

La PC tolera suspensión: detecta el salto de reloj y reconecta sola al despertar (reintento
con espera exponencial hasta 60 s; 5 min si la credencial fue rechazada).
