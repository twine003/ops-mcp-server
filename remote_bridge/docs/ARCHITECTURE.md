# Arquitectura — Alejandro

Alejandro es **MaxBot extendido**, no un asistente nuevo. Telegram sigue igual; se suman
dos entradas (voz por Alexa y PCs remotas) a través de un **gateway** aparte.

```
 Amazon Echo ──► Alexa Custom Skill ──HTTPS──┐
                                             │      Servidor (VPS)
 PC Windows                                  ▼      ┌──────────────────────────────────────────┐
 ┌─────────────────────────┐   WSS saliente   Traefik (Dokploy, :443, Let's Encrypt)          │
 │ desktop-mcp 127.0.0.1:8011│◄─┐  ──────────────►│   │ solo /bridge/, /alexa/, /health          │
 │ Alejandro Connector      │──┘                 │   ▼                                         │
 │  (tarea al iniciar sesión)│                   │ alejandro-gateway  (usuario alejandro-gw)  │
 └─────────────────────────┘                    │   público : 172.17.0.1:8770               │
                                                │   interno : 127.0.0.1:8771 (nunca proxeado)│
                                                │        ▲ long-poll HTTP (sin puertos nuevos)│
                                                │        │                                   │
                                                │ MaxBot (instancia existente)               │
                                                │   plugin remote_bridge ── run_oneshot ──► Claude/Codex
                                                │   Telegram (igual que antes)               │
                                                └──────────────────────────────────────────┘
```

## Componentes

| Pieza | Dónde | Qué hace |
|---|---|---|
| `remote_bridge/gateway.py` + `server.py` | servidor, servicio `alejandro-gateway` | Borde público: verifica Alexa, recibe los WebSocket de las PCs. API interna en loopback para MaxBot. |
| `hub.py` | gateway | Conexiones vivas, llamadas en curso (UUID + plazo), deduplicación, idempotencia, expulsión de revocados. |
| `registry.py` | gateway | Dispositivos inscritos: SHA-256 del token, revocación, política del lado servidor. |
| `policy.py` | ambos lados | Niveles `allow / confirm / deny`; aprobaciones de un solo uso atadas a la llamada exacta. |
| `alexa.py` | gateway | Verificación de firma de Amazon, intents, presupuesto de 8 s, métricas de latencia. |
| `windows_connector.py` | PC | Conexión saliente WSS, latido, reconexión, política local, auditoría. Reenvía a desktop-mcp. |
| `maxbot/plugins/remote_bridge` | MaxBot | Ejecuta turnos de voz, pide aprobaciones por Telegram, `/pc`, CLI para el modelo. |
| `maxbot/runners/oneshot.py` | MaxBot | Turno completo sin streaming (antes vivía dentro de heartbeat); admite perfil restringido. |

## Decisiones

- **Gateway aparte, MaxBot como cliente.** MaxBot no abre puertos ni suma dependencias
  (usa `httpx`, que ya trae python-telegram-bot). Si el gateway cae, MaxBot y Telegram siguen
  intactos; el plugin solo reintenta.
- **WebSocket sobre TLS, iniciado por la PC.** No hay puertos entrantes en el router ni
  desktop-mcp publicado. TLS lo termina Traefik con el certificado de Let's Encrypt, como el
  resto de los servicios del host.
- **Dos credenciales internas.** `BRIDGE_MAXBOT_TOKEN` (aprobar, eventos, Alexa) y
  `BRIDGE_AGENT_TOKEN` (listar y pedir). El plugin borra la primera del entorno al arrancar,
  así los procesos del modelo no la heredan.
- **Política en los dos extremos.** Manda la más estricta. La de la PC vive en la PC y el
  servidor no puede ampliarla: aunque el servidor se comprometa, una herramienta `deny` en
  la PC no corre y una `confirm` exige una aprobación presente en la llamada.
- **Voz restringida por defecto.** Cualquiera cerca del Echo puede hablarle. El turno de voz
  corre con `--allowedTools Read Grep Glob WebSearch WebFetch`, `--disallowedTools Bash Edit
  Write …` y `--strict-mcp-config` (sin servidores MCP): lo hace cumplir el CLI, no el prompt.
- **No se reutiliza `run_turn`.** Está atado a la mensajería de Telegram. Se extrajo el camino
  "un turno, solo el texto final" que ya existía en heartbeat a `runners/oneshot.py`, y
  heartbeat ahora delega en él.

## Identidad, contexto y memoria

| Capa | Qué es | Dónde vive | Quién la ve |
|---|---|---|---|
| **Identidad** | `system_instruction` + manifiesto de plugins | `bot.toml` de la instancia | Todos los canales (mismo runner, mismo texto) |
| **Memoria persistente** | Hechos durables, notas de sesión | El `workspace` del bot (p. ej. un repo de memoria con `CLAUDE.md` / `AGENTS.md`) | Todos los canales; en voz, solo lectura |
| **Historial de sesión** | La conversación del CLI | `sessions.json`, una clave por canal: el chat de Telegram, `alexa_<dueño>`, `heartbeat_<tarea>` | Solo ese canal |
| **Contexto temporal** | Lo que se manda en el turno (prefijo de voz, texto) | Nada; muere con el turno | Ese turno |

- Voz y Telegram **no comparten historial** a propósito: dos turnos simultáneos sobre el mismo
  hilo se pisan (el cliente de Codex admite un solo manejador por hilo) y la voz necesita
  respuestas cortas. Comparten identidad y memoria persistente.
- **La memoria de ChatGPT/Claude.ai no se sincroniza con MaxBot.** MaxBot solo sabe lo que está
  en su `workspace` y en sus sesiones del CLI.

## Flujos

**Herramienta de PC:** modelo → `cli call pc-x desktop.status` → API interna → hub (política
servidor ∩ PC) → frame `call` con UUID y plazo → conector (política local, deduplicación) →
desktop-mcp → `result` → hub → CLI → modelo.

**Herramienta `confirm`:** igual, pero el hub responde `202 approval_required`, publica el
evento, el plugin manda *Aprobar / Rechazar* al dueño. Al aprobar, el gateway ejecuta esa llamada
(una vez) y guarda el resultado; el CLI lo estaba esperando.

**Voz:** Echo → Alexa → `/alexa/v1` (firma, hora, skill, usuario) → evento `alexa_turn` → el
plugin corre el turno → `answer` → si alguien espera, Alexa lo dice; si no, va a Telegram.
Detalle y límites en [ALEXA.md](ALEXA.md).
