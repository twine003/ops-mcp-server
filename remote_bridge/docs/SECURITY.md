# Seguridad

## Credenciales

| Credencial | Dónde está | Qué permite | Cómo se revoca |
|---|---|---|---|
| Token de dispositivo | PC: `%LOCALAPPDATA%\alejandro-connector\token.bin` (DPAPI, usuario actual). Servidor: solo su SHA-256 en `devices.json` | Conectarse como ESE dispositivo | `admin revoke <id>` (corta la conexión viva en ≤5 s) o `admin rotate <id>` |
| `BRIDGE_MAXBOT_TOKEN` | `/etc/alejandro-gateway/gateway.env` (0640) y `.env` de la instancia de MaxBot | Eventos, **aprobar**, respuestas de Alexa | Cambiarlo en ambos archivos y reiniciar los dos servicios |
| `BRIDGE_AGENT_TOKEN` | Igual; visible a los procesos del modelo | Listar dispositivos, **pedir** llamadas, leer estado de una aprobación | Igual |
| Clave de desktop-mcp | Variable de usuario `DESKTOP_MCP_API_KEY` (no cambia) | Usar desktop-mcp en 127.0.0.1 | Ver README de `desktop/` |
| Firma de Alexa | Certificado de Amazon (no es un secreto nuestro) | — | — |

Ningún token aparece en los repositorios, en la auditoría ni en los logs. Las pruebas lo
verifican (`test_audit_log_never_contains_arguments`).

## Superficie expuesta

- Público, por Traefik y TLS: `/bridge/v1/ws`, `/alexa/v1` y `/health`. El resto de las rutas
  del host no llegan al gateway.
- El puerto 8770 escucha en `172.17.0.1` (puente de Docker), no en la IP pública. El firewall
  lo abre solo a `172.16.0.0/12`.
- La API interna escucha en `127.0.0.1:8771` y además rechaza todo cliente que no sea loopback.
- desktop-mcp sigue en `127.0.0.1:8011` de la PC. **No se publica.**

## Política de herramientas (inicial)

| Nivel | Herramientas |
|---|---|
| `allow` | `device.ping`, `device.status`, `desktop.status`, `desktop.list_monitors` |
| `confirm` (una aprobación por llamada) | `desktop.screenshot`, `desktop.list_windows`, `desktop.ui_tree`, `desktop.find_element`, `desktop.read_value`, `desktop.audit_tail` |
| `deny` (todo lo demás) | `click`, `type_text`, `key`, `drag`, `scroll`, `launch_app`, `focus_window`, `move_window`, `set_value`, `invoke`, `select`, `set_option`, `pause`, `resume`, … |

Una aprobación vale para **una** llamada: mismo dispositivo, misma herramienta, mismos
argumentos (hash SHA-256), dentro de 120 s. No existe "aprobar siempre". Solo el dueño
(chat emparejado + usuario dueño) puede tocar el botón.

Lectura de pantalla va en `confirm`: `list_windows`, `status` y `audit_tail` devuelven títulos
de ventana, que pueden contener URLs y datos personales.

## Amenazas y mitigaciones

| Amenaza | Mitigación | Límite honesto |
|---|---|---|
| Alguien llama al endpoint de Alexa | Firma de Amazon + ventana de 150 s + skill id + lista de usuarios | — |
| Alguien en la casa le habla al Echo | Voz en solo lectura (lo impone el CLI) | Puede **leer** lo que el bot lee, incluida la memoria del `workspace` |
| Token de una PC robado | Atado a un `device_id`; revocación inmediata; limitador de fallos por IP | Mientras no se revoque, permite lo que la política de esa PC permita |
| Gateway comprometido | La PC aplica su propia política; `deny` no corre, `confirm` exige aprobación | Un atacante en el gateway podría fabricar el campo `approval`: las herramientas `confirm` quedan expuestas a él (las `deny` no) |
| Inyección de instrucciones vía contenido de pantalla | CONTEXT.md: lo que devuelve una PC es dato; acciones peligrosas están en `deny` | El modelo podría igual intentar pedir una `confirm`: la ve el dueño antes |
| El modelo se auto-aprueba | El token del agente no puede aprobar; el de MaxBot se borra del entorno | **MaxBot corre como root con `Bash(*)`**: el modelo puede leer `.env` o `gateway.env` del disco. La separación real exige correr MaxBot con un usuario sin acceso a esos archivos (pendiente, ver abajo) |
| Repetición de mensajes | UUID por llamada, caché de resultados en la PC, `request_id` idempotente | — |
| PC dormida / red caída | Latido de la aplicación + ping de WebSocket; detección de salto de reloj | Durante la suspensión la PC aparece desconectada (correcto) |

## Recomendaciones para el despliegue

1. **Correr MaxBot con un usuario sin privilegios** y sin acceso de lectura a
   `/etc/alejandro-gateway/` ni al `.env` donde vive `BRIDGE_MAXBOT_TOKEN`. Si MaxBot corre como
   root con herramientas de shell, la frontera aprobador/agente es de configuración, no de
   sistema operativo.
2. Mantener `allowed_tools` de la instancia tan corto como se pueda: lo que el modelo puede
   hacer por Telegram también lo puede hacer con lo que devuelva una PC.
3. Respaldar `/var/lib/alejandro-gateway/devices.json` (perderlo obliga a re-inscribir las PCs;
   no contiene secretos utilizables).
