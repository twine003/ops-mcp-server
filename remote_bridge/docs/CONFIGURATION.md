# Configuración

## Gateway — `/etc/alejandro-gateway/gateway.env`

| Variable | Por defecto | Uso |
|---|---|---|
| `BRIDGE_DATA_DIR` | `/var/lib/alejandro-gateway` | `devices.json`, `audit.jsonl` |
| `BRIDGE_MAXBOT_TOKEN` | — (obligatorio, ≥32) | Credencial del plugin de MaxBot |
| `BRIDGE_AGENT_TOKEN` | — (obligatorio, ≥32, distinta) | Credencial del CLI del modelo |
| `ALEXA_SKILL_ID` | vacío = Alexa apagada | `amzn1.ask.skill…` |
| `ALEXA_ALLOWED_USER_IDS` | vacío = nadie | ids de cuenta de Amazon, separados por coma |
| `ALEXA_BUDGET_SECONDS` | `6.0` | Espera máxima por turno antes de "sigo trabajando" |
| `ALEXA_PROGRESSIVE` | `1` | Envía "Déjame pensarlo" por la API de Progressive Response |
| `ALEXA_ENROLL` | `0` | `1` = registra el id completo de cuentas desconocidas |
| `ASSISTANT_NAME` | `Alejandro` | Nombre en las frases de Alexa |
| `BRIDGE_LOG_LEVEL` | `INFO` | |

Argumentos de `remote_bridge.server`: `--public-host` (`172.17.0.1` en producción),
`--public-port` (8770), `--internal-port` (8771, siempre en 127.0.0.1).

Política del servidor por dispositivo: `admin policy <id> [--set herramienta=nivel]`.

## MaxBot — `[plugins.remote_bridge]` en `bot.toml`

| Opción | Por defecto | Uso |
|---|---|---|
| `gateway_url` | `http://127.0.0.1:8771` | API interna |
| `maxbot_token_env` | `BRIDGE_MAXBOT_TOKEN` | Nombre de la variable (se borra del entorno al leerla) |
| `owner_chat_id` | el chat emparejado | Quién recibe aprobaciones y respuestas de voz no escuchadas |
| `alexa` | `true` | Atender turnos de voz |
| `voice_model` | `claude` | Runner para voz |
| `voice_profile` | `restricted` | `restricted` = solo lectura impuesta por el CLI; `full` = mismos poderes que Telegram |
| `voice_allowed_tools` | `Read, Grep, Glob, WebSearch, WebFetch` | Herramientas del perfil restringido |
| `voice_cli_model` | vacío | Modelo solo para voz (medido: no reduce latencia) |
| `voice_timeout` | `120` | Corte del turno de voz (s) |

Variables que heredan los procesos del modelo: `BRIDGE_AGENT_TOKEN`, `BRIDGE_INTERNAL_URL`.

## PC — `%LOCALAPPDATA%\alejandro-connector\`

`config.json`:

```json
{"gateway_url": "wss://alejandro.example.com/bridge/v1/ws", "device_id": "pc-casa",
 "desktop_url": "http://127.0.0.1:8011/mcp", "max_backoff": 60, "auth_backoff": 300}
```

`policy.json` (se crea con los valores seguros; editarlo y reiniciar la tarea para cambiarlo):

```json
{"device.ping": "allow", "device.status": "allow", "desktop.status": "allow",
 "desktop.list_monitors": "allow", "desktop.screenshot": "confirm", "...": "..."}
```

Lo que no aparece es `deny`. Para habilitar una acción (p. ej. `desktop.launch_app`) hay que
ponerla en `confirm` aquí **y** en la política del servidor.
