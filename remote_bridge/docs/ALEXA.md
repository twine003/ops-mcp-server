# Alexa — configuración y límites

## Límites verificados de una Custom Skill (documentación de Amazon, revisada 2026-10-08)

- **~8 segundos** para responder cada solicitud. Las *progressive responses* (hasta 5 por
  solicitud) **no** amplían ese plazo: se reproducen solo si llegan antes que la respuesta final.
- Modelo **solicitud/respuesta**: no hay audio bidireccional continuo, la skill no puede hablar
  por iniciativa propia más tarde y el usuario no puede interrumpir un proceso nuestro.
- La sesión sigue abierta si `shouldEndSession=false`; Alexa escucha unos segundos más
  (con `reprompt`). Si nadie habla, la sesión termina.
- Firma obligatoria: cabeceras `SignatureCertChainUrl` y `Signature-256`, certificado de
  `s3.amazonaws.com/echo.api/` con SAN `echo-api.amazon.com`, tolerancia de **150 s**.
- Endpoint HTTPS en el 443 con certificado de una CA de confianza (Let's Encrypt sirve).

## Latencia medida

Turno de voz real con Claude CLI (perfil restringido), medido en la PC de desarrollo el
2026-10-08:

| Pregunta | Modelo por defecto | Haiku 4.5 |
|---|---|---|
| "¿Cuánto es 17 por 23?" | 13.7 s (frío), 6.5 s | 7.6 s |
| "Dime qué día es hoy" | 7.7 s, 7.6 s | 6.9 s (**dijo "miércoles": era jueves**) |
| "¿Capital de Honduras?" | — | 6.6 s |

El cuello de botella es **arrancar el CLI en cada turno** (5-6 s), no el modelo: cambiar a un
modelo más chico no ayuda y empeora las respuestas.

Componentes del gateway (pruebas automáticas, loopback): verificación + enrutado < 50 ms; ida
y vuelta del long-poll hacia MaxBot ~100 ms. La parte de Amazon (reconocimiento de voz y
síntesis) no es visible desde aquí: `request.timestamp` tiene resolución de segundos.

**Conclusión:** con el diseño actual la mayoría de las preguntas no entra en el primer turno.
Por eso el flujo es:

1. "Alexa, pídele a Alejandro que …" → el gateway espera hasta `ALEXA_BUDGET_SECONDS` (6 s).
2. Si la respuesta está: Alexa la dice y pregunta "¿Algo más?".
3. Si no: "Sigo trabajando en eso. Di *continúa* en unos segundos." La sesión sigue abierta.
4. "Continúa" → otra espera de hasta 6 s por la **misma** respuesta.
5. Si el usuario no vuelve, la respuesta llega a **Telegram** (marcada 🔊).

`¿Está conectada mi computadora?` (`EstadoPCIntent`) no pasa por el modelo: responde en
milisegundos.

### Alternativas para bajar la latencia (no implementadas)

| Opción | Ganancia esperada | Costo |
|---|---|---|
| Proceso Claude **persistente** para voz (`--input-format stream-json`) | quita el arranque en frío: ~2-4 s | Proceso extra en RAM (el host ya usa swap) |
| Segundo `codex app-server` con sandbox `read-only` solo para voz | Codex ya es persistente | ~150-300 MB más; otra sesión de Codex |
| Alexa solo como "disparador": responde "te lo mando por Telegram" | latencia percibida 0 | Pierde la respuesta hablada |

## Pasos manuales en Amazon Developer (los hace el dueño)

1. <https://developer.amazon.com/alexa/console/ask> → *Create Skill*.
   - Nombre: **Alejandro**. Idioma: **Spanish (US)** (o el del Echo: es-MX / es-ES).
   - Tipo: **Other → Custom**. Hosting: **Provision your own**.
2. *Interaction Model → JSON Editor*: pegar `remote_bridge/skill/interaction_model.es-US.json`.
   *Save* + *Build*.
3. *Endpoint*: **HTTPS**, Default Region = `https://alejandro.example.com/alexa/v1`,
   certificado: *"My development endpoint has a certificate from a trusted certificate
   authority"*.
4. Copiar el **Skill ID** (`amzn1.ask.skill…`) a `ALEXA_SKILL_ID` en
   `/etc/alejandro-gateway/gateway.env`.
5. Inscribir tu cuenta de Amazon:
   - Poner `ALEXA_ENROLL=1`, reiniciar el gateway.
   - *Test* (pestaña de la consola, modo *Development*) o tu Echo: "Alexa, abre Alejandro".
     Responderá "no está autorizada".
   - `sudo grep alexa_unauthorized_user /var/lib/alejandro-gateway/audit.jsonl | tail -1` →
     copiar el `user_id` a `ALEXA_ALLOWED_USER_IDS`, volver `ALEXA_ENROLL=0`, reiniciar.
6. La skill queda en modo *Development*: solo funciona en los Echo de tu cuenta. **No hace
   falta publicarla** (y no conviene).

Frases: "Alexa, abre Alejandro" · "Alexa, pídele a Alejandro que me diga qué tengo pendiente" ·
"continúa" · "¿está conectada mi computadora?" · "para".
