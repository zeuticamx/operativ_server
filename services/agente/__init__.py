"""
Agente de IA por chat: el reemplazo en código de los workflows de n8n.

Hasta ahora el agente vivía en tres workflows de n8n
(`entrada-canal-universal`, `ejecutar-herramienta-tenant`, `escalar-humano`)
que hablaban con este backend por HTTP (`/api/eventos/*`). Este paquete trae
esa lógica al mismo proceso, que es donde ya vive todo lo que el agente
necesita: el calendario, los créditos, la asignación a humanos y los avisos
en tiempo real.

Qué hace cada módulo, en el orden en que corre un mensaje entrante:

    firma.py       Autentica la petición: HMAC de Meta por remitente, token
                   interno del proxy de NeuroAPI, o widget_key + Origin del
                   widget web.
    normalizar.py  Convierte el sobre de cada canal (WhatsApp / Instagram /
                   Facebook / widget) en un `MensajeNormalizado`.
    ingreso.py     Resuelve el tenant, revisa plan y créditos, persiste
                   usuario / conversación / mensaje, agrupa ráfagas y decide
                   si el agente contesta o la conversación está en manos de
                   una persona.
    bajas.py       Quién pidió no recibir más mensajes: reconoce BAJA / ALTA
                   en el mensaje entrante y responde si se puede escribir.
    modelo.py      Qué parámetros acepta cada modelo del catálogo.
    prompt.py      Arma los bloques de system y el historial como turnos.
    loop_agente.py Bucle de tool-calling contra la API de Anthropic.
    herramientas.py Ejecuta las herramientas dinámicas del tenant.
    escalar.py     Pasa la conversación a una persona.
    envio.py       Entrega la respuesta por el canal que corresponda.

El agente tiene exactamente dos herramientas: `ejecutar_herramienta`, cuya
descripción lleva incrustado el catálogo del tenant, y `escalar_humano`. Ni
el tenant ni la conversación viajan en sus esquemas: los pone el servidor al
ejecutar, porque un modelo que pudiera elegirlos podría pedir datos de la
conversación de otro cliente.

`medir-uso-tokens` no se migra: desaparece. Existía porque en n8n el
`tokenUsage` del modelo quedaba en una salida que ningún nodo hermano podía
leer, así que un job aparte sondeaba la API de ejecuciones cada 2 minutos
para recuperarlo. Acá viene en la respuesta de cada llamada y se registra
donde siempre (`loop_agente._registrar_consumo`).

Cuatro decisiones que vienen de la auditoría del 15/10/2026 y que acá quedan
resueltas por construcción, no por un parche:

  - Los tokens de canal NUNCA viajan dentro de un dict de datos. Se leen de
    `get_channel_credentials` y se usan solo al armar la cabecera del envío.
    En n8n quedaban guardados en el historial de cada ejecución.
  - No se loguea el contenido de los mensajes ni la respuesta del modelo.
    Solo identificadores y conteos de tokens.
  - `META_VERIFY_TOKEN` falla cerrado: sin la variable, el handshake de Meta
    se rechaza. En n8n había un valor de respaldo escrito en el código.
  - Hay lista de bajas (`bajas.py`, tabla en sql/55). La plantilla de
    reapertura la consulta antes de salir, y es el único mensaje del flujo
    que puede empezar una conversación: el resto va dentro de la ventana de
    24 h, o sea contestando al cliente. En n8n esa plantilla salía sin
    comprobar nada.

El proxy de caché que había delante de Anthropic queda RETIRADO. Existía
porque los nodos de Anthropic en n8n no tenían caché de prompt nativo, así
que había que interponer algo que la hiciera. Acá se llama al SDK directo
(`loop_agente._cliente`, sin `base_url`) y el caché se declara en la propia
petición, con dos cortes: el bloque estable de `system` y el último turno del
historial. Ese docstring explica los dos y los mínimos por modelo.

Durante la migración los dos caminos conviven: `AGENTE_EN_CODIGO` decide si
un mensaje entrante lo atiende este paquete o se sigue reenviando a n8n
(ver services/entrada_mensajes.py). Mientras esté apagado, nada de esto
corre en producción.

Esto cubre WhatsApp por Meta directo, Instagram, Facebook y el widget, que
entran por `routers/agente_webhook.py` y deciden el camino por la cabecera
que traen. WhatsApp por NeuroAPI es distinto: entra por
`routers/canales_neuroapi_webhook.py`, que reenvía el mensaje *de vuelta*
hacia este mismo backend a través de `entrada_mensajes.destino_para`.
`AGENTE_EN_CODIGO` también gobierna ese reenvío, pero en un archivo distinto:
si se enciende la bandera sin que `entrada_mensajes.py` sepa del cambio,
WhatsApp por NeuroAPI —el canal con más tráfico real— seguiría yendo a n8n
aunque el agente esté encendido. Dos interruptores separados para la misma
bandera: al tocar uno, revisar el otro.
"""
