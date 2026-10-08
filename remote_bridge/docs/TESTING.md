# Pruebas

Estado al 2026-10-08. **Ejecutado** = corrió y pasó en esa fecha. **No ejecutado** = no se corrió;
no se declara como pasado.

## Cómo correrlas

```bash
# gateway + conector (Python ≥ 3.10)
python -m venv remote_bridge/.venv
remote_bridge/.venv/Scripts/python -m pip install -r requirements-bridge.txt pytest
remote_bridge/.venv/Scripts/python -m pytest remote_bridge/tests -q

# plugin de MaxBot (en el repo maxbot, rama feature/remote-bridge-plugin)
python -m venv .venv && .venv/Scripts/python -m pip install -e . pytest -r <ops-mcp-server>/requirements-bridge.txt
REMOTE_BRIDGE_PATH=<ops-mcp-server> .venv/Scripts/python -m pytest tests -q
```

Las pruebas levantan el gateway de verdad (uvicorn + WebSocket en puertos efímeros de
127.0.0.1) y el conector de verdad. Solo desktop-mcp y el certificado de Amazon son de prueba
(una CA propia emite un certificado con SAN `echo-api.amazon.com`).

## Resultados

| Suite | Entorno | Resultado |
|---|---|---|
| `remote_bridge/tests` (49) | Windows 11, Python 3.10.11 | **49/49**, 5 corridas seguidas |
| maxbot `tests` (11) | Windows 11, Python 3.10.11 | **11/11**, 5 corridas seguidas |
| Prueba de mutación | quitar la verificación de firma / el chequeo de aprobación en la PC | las pruebas correspondientes **fallaron** (detectan la regresión) |
| Integración real con desktop-mcp | PC Windows, desktop-mcp 1.0.0 en 127.0.0.1:8011, gateway local | ver abajo |
| Tarea programada real | instalar → conectar → reiniciar gateway → reconectar → desinstalar | ver abajo |
| Latencia de voz real | Claude CLI, perfil restringido | ver [ALEXA.md](ALEXA.md) |

### Cobertura por área

| Área | Prueba | Estado |
|---|---|---|
| **Autenticación** | credenciales válidas | ✅ ejecutado |
| | token inválido / dispositivo desconocido | ✅ ejecutado |
| | revocación (expulsa la conexión viva y bloquea la reconexión) | ✅ ejecutado |
| | rotación invalida el token anterior | ✅ ejecutado |
| | API interna sin credencial (401), con la del agente para aprobar/eventos (403), no servida en el puerto público (404) | ✅ ejecutado |
| **Conexión** | conexión inicial y estado | ✅ ejecutado |
| | caída del socket desde el gateway → reconexión | ✅ ejecutado |
| | dispositivo mudo → el watchdog lo desconecta | ✅ ejecutado |
| | conexión nueva reemplaza a la vieja | ✅ ejecutado |
| | timeout de herramienta (la PC corta en el plazo) | ✅ ejecutado |
| | frames duplicados (en curso y tardíos) → la herramienta corre una vez | ✅ ejecutado |
| | `request_id` idempotente | ✅ ejecutado |
| | frame malformado → cierre 4400 | ✅ ejecutado |
| | suspensión real del equipo | ❌ no ejecutado (la detección por salto de reloj está implementada, no probada con un sleep real) |
| **MCP** | herramienta permitida + error de ejecución propagado | ✅ ejecutado |
| | herramientas bloqueadas (no ofrecida, inexistente, nombre inválido) | ✅ ejecutado |
| | `deny` del servidor gana sobre `allow` de la PC | ✅ ejecutado |
| | la PC rechaza `confirm` sin aprobación aunque el gateway la pida | ✅ ejecutado |
| | aprobación: un solo uso, atada a argumentos, el agente no puede aprobar | ✅ ejecutado |
| | aprobación rechazada / vencida | ✅ ejecutado |
| | auditoría sin argumentos ni tokens | ✅ ejecutado |
| **MaxBot** | `run_oneshot` + persistencia de sesión por canal; la sesión de Telegram no se toca | ✅ ejecutado (CLI falso) |
| | perfil restringido llega al CLI (`--allowedTools`, `--disallowedTools`, `--strict-mcp-config`) | ✅ ejecutado (CLI falso) |
| | heartbeat delega en el camino compartido | ✅ ejecutado (CLI falso) |
| | la app arranca con el plugin (Telegram + handlers) y sin token (no se cae) | ✅ ejecutado |
| | el token aprobador sale del entorno de los subprocesos | ✅ ejecutado |
| | runner Codex real | ❌ no ejecutado |
| | Telegram real (mensaje de punta a punta en producción) | ❌ no ejecutado: requiere desplegar |
| **Alexa** | URL de certificado (6 inválidas, 5 válidas, incluido `..`) | ✅ ejecutado |
| | firma válida / alterada / sin cabeceras | ✅ ejecutado |
| | CA ajena, SAN incorrecto, certificado vencido | ✅ ejecutado |
| | timestamp fuera de 150 s, skill id ajeno, usuario no autorizado | ✅ ejecutado |
| | respuesta dentro del presupuesto; lenta → "continúa"; nadie escucha → a Telegram | ✅ ejecutado |
| | MaxBot caído; estado de PC sin modelo; stop / fin de sesión | ✅ ejecutado |
| | Echo real con el certificado real de Amazon | ❌ no ejecutado: requiere la skill en Amazon Developer |
| **Producción** | servicios existentes funcionando, puertos, consumo | ❌ no ejecutado: nada desplegado todavía |
| | secretos en los repositorios | ✅ ejecutado (búsqueda de patrones de tokens/llaves en ambos diffs: ninguno) |
| | gateway en Linux / Python 3.10 del servidor | ❌ no ejecutado (Docker local apagado); `install_gateway.sh --check` lo valida en el servidor |

### Integración real (PC de desarrollo, 2026-10-08)

Gateway local + conector con el desktop-mcp real (solo herramientas de lectura):

| Llamada | HTTP | Ida y vuelta | En la PC |
|---|---|---|---|
| `device.ping` | 200 | 41-49 ms | 0 ms |
| `device.status` | 200 | 95-211 ms | 94-202 ms |
| `desktop.status` | 200 | 45-60 ms | 30-46 ms |
| `desktop.list_monitors` | 200 | 24-49 ms | 14-30 ms |
| `desktop.screenshot` | 202 `approval_required` | — | no se ejecutó |
| `desktop.click` | 403 `tool_not_offered` | — | no se ejecutó |

Token guardado y leído con DPAPI; el archivo no contiene el token en claro.

Tarea programada `Alejandro-Connector`: registrada, conectó, **reconectó sola 6.6 s** después de
reiniciar el gateway, `desktop.status` a través de la tarea en 109 ms, desinstalación limpia (0
tareas). Esta prueba encontró un error real: PowerShell 5.1 escribe `config.json` con BOM y
Python lo rechazaba. Corregido en ambos lados.
