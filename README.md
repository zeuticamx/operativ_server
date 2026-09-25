# OperativAI — API del portal

Backend en FastAPI para el portal multitenant. Habla con la misma
`agente_db` que usa el workflow de n8n.

## Nombres que se confunden fácil

| Tabla | Qué contiene |
|---|---|
| `users` | Clientes finales que escriben por WhatsApp/IG/FB |
| `portal_users` | Dueños de negocio que entran al portal |
| `vendedores` | Personal de venta del tenant (mini-CRM) |

No son lo mismo. `users` ya existía del lado de n8n; `portal_users` es
nuevo. Un `vendedor` puede tener cuenta de portal (`portal_user_id`) o no
tenerla y existir solo como destinatario de leads.

## Arranque

```bash
pip install -r requirements.txt
cp .env.example .env    # y llénalo
```

Genera el secreto de JWT:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Estructura

```
sql/          migraciones NN_*.sql, en orden
services/     lógica de negocio sin HTTP (asignación, CRM, correo, geocerca,
              Google, Meta, avisos, pipeline). La importan los routers.
routers/      un APIRouter por recurso; lo único que sabe de FastAPI aparte
              de deps.py
config.py, deps.py, security.py, session.py, schemas.py, main.py
              infraestructura transversal: la usan tanto routers/ como
              services/, así que se queda en la raíz y no en ninguna
              de las dos carpetas de arriba.
```

## Migraciones

No hay Alembic: son archivos `NN_*.sql` numerados en `sql/` que se aplican
en orden con `aplicar_sql.py`, que lee el **mismo** `DATABASE_URL` que la
app (así lo que se aplica cae sí o sí en la base que n8n lee).

```bash
python aplicar_sql.py --revisar              # solo lectura: qué falta y en qué base
python aplicar_sql.py sql/06_vendedores.sql  # aplicar (el nombre suelto también funciona)
```

Cada archivo va en una transacción: si algo falla no queda a medio aplicar.
Todo es `IF NOT EXISTS`, así que reaplicar no rompe nada.

Desde cero:

```bash
python aplicar_sql.py sql/01_portal.sql sql/02_token_expires_tz.sql \
                      sql/03_users_username.sql sql/04_verificacion_email.sql \
                      sql/05_herramientas.sql sql/06_vendedores.sql \
                      sql/07_crm_campo.sql
```

Levanta:

```bash
uvicorn main:app --reload --port 8000
```

Docs interactivas en `http://localhost:8000/docs`.

## Tests

```bash
pip install -r requirements.txt
pytest              # desde backend/
pytest -v           # con el nombre de cada caso
```

No necesitan base de datos ni servidor: cubren la máquina de estados, las
reglas de reparto y la lógica condicional del mensaje entrante, que son las
partes que deciden algo. La SQL no se prueba acá.

## Prerequisito de la BD

Las funciones de credenciales usan `current_setting('app.cred_key')`. Si aún
no lo configuraste:

```sql
ALTER DATABASE agente_db SET app.cred_key = 'TU_LLAVE_MAESTRA';
```

Reconéctate después: el parámetro aplica en sesiones nuevas.

## Endpoints

### Auth
```
POST   /api/auth/registro          Paso 1: manda un código de 6 dígitos al correo
POST   /api/auth/verificar         Paso 2: código ok → crea negocio + agente + dueño
POST   /api/auth/reenviar-codigo   Código nuevo para un alta pendiente
POST   /api/auth/login
POST   /api/auth/refresh
GET    /api/auth/yo
```

## Alta con verificación de correo

`/registro` no crea la cuenta: guarda el alta en `email_verifications` y
manda un código al correo. Devuelve **202 y ningún token**. La cuenta nace
recién en `/verificar`.

```
Frontend                         Backend                        Correo
   |                                |                              |
   |-- POST /auth/registro -------> |                              |
   |                                |  guarda alta pendiente       |
   |                                |-- código de 6 dígitos -----> |
   |<-- 202 {expira_en_minutos} --- |                              |
   |                                |                              |
   |-- POST /auth/verificar ------> |                              |
   |                                |  crea tenant + config + user |
   |<-- 201 {access, refresh} ----- |                              |
```

Por qué una tabla aparte y no un `email_verified` en `portal_users`: si el
alta sin confirmar ya ocupara una fila de `portal_users`, cualquiera podría
registrar el correo de otro y dejarlo bloqueado por el `UNIQUE` de email sin
haber probado nunca que le pertenece.

Defensas del código:

| Qué | Dónde se ajusta |
|---|---|
| Se guarda hasheado con argon2, no en claro | — |
| Vence a los 10 minutos | `CODIGO_VIGENCIA_MINUTOS` |
| 5 intentos fallidos y el alta se descarta | `CODIGO_MAX_INTENTOS` |
| 60 s mínimo entre envíos | `CODIGO_REENVIO_SEGUNDOS` |

**Sin SMTP configurado la app arranca igual y escribe los códigos en el
log** en vez de enviarlos. Es cómodo en local, pero en producción significa
que nadie recibe nada: llena `SMTP_HOST` y `SMTP_FROM`. El arranque avisa
con un WARNING si faltan.

### Canales
```
GET    /api/canales                  Canales conectados
POST   /api/canales/meta/conectar    code → user token de larga duración
GET    /api/canales/meta/paginas     Páginas que el usuario autorizó
POST   /api/canales/meta/activar     Guarda tokens + suscribe webhooks
POST   /api/canales/whatsapp/conectar  Registra la línea de Kontesta del tenant
DELETE /api/canales/{tipo}           Desconecta
```

### Agente
```
GET    /api/agente           Config actual
PUT    /api/agente           Actualiza prompt, modelo, parámetros
GET    /api/agente/modelos   Lista blanca de modelos
```

### Conversaciones
```
GET    /api/conversaciones                Listado con filtros
GET    /api/conversaciones/metricas
GET    /api/conversaciones/{id}           Detalle con mensajes
POST   /api/conversaciones/{id}/mensajes       Respuesta manual (handoff humano)
POST   /api/conversaciones/{id}/volver-a-ia    Le devuelve el control a la IA
```

Gerencia de plataforma tiene los mismos dos últimos (más los dos GET) bajo
`/api/gerencia/tenants/{tenant_id}/conversaciones/...`, con el `tenant_id`
explícito en la ruta en vez de salir del JWT de quien llama — es lo que le
permite "filtrar por tenant_id" sin pasar por la impersonación de solo
lectura (`/gerencia/tenants/{id}/impersonar`), que bloquea cualquier POST a
propósito.

**Excepción deliberada a la regla del CLAUDE.md raíz** ("el backend solo
posee sus propias tablas; nunca toca directamente tablas propiedad de
n8n"): `enviar_mensaje_humano` y `volver_a_ia` (`services/conversaciones.py`)
sí escriben en
`conversations`/`messages`. Es segura porque `entrada-canal-universal`
(el workflow que de verdad conversa con el cliente) ya relee
`conversations.status` en cada mensaje entrante y ya suprime la IA
mientras vale `'transferred'` — el backend solo pone ese valor en
`'active'` otra vez; no hace falta coordinar nada más con n8n. El envío
real al cliente (WhatsApp/Instagram/Facebook) replica el mismo mecanismo
que usa ese workflow (`get_channel_credentials` + Graph API directo), no
`services/whatsapp.py` (Kontesta): ese proveedor no es el que de verdad
entrega los mensajes de la conversación con IA, así que una respuesta
manual que lo usara le llegaría al cliente por un canal distinto al que
ya tiene abierto.

### Gestión de vendedores (mini-CRM)

Módulo **opcional por tenant** e independiente del agente. Un negocio puede
tener solo el agente, solo vendedores, o los dos. El interruptor está en
`tenant_servicios`; todos los endpoints de abajo responden 409 si
`gestion_vendedores_activo` está en false.

```
GET    /api/tenants/{id}/servicios          Flags de servicio
PATCH  /api/tenants/{id}/servicios          Enciende/apaga módulos (gerencia)

GET    /api/tenants/{id}/config-vendedores  Estrategia de reparto
PATCH  /api/tenants/{id}/config-vendedores  carga | round_robin | manual (gerencia)

POST   /api/vendedores                      Alta
GET    /api/tenants/{id}/vendedores         Listado (?activo=true) — sin exigir el flag
PATCH  /api/vendedores/{id}                 Activar / desactivar / editar
GET    /api/vendedores/{id}/pipeline        Su embudo
POST   /api/vendedores/{id}/reasignar-pendientes   Reparte sus leads abiertos

POST   /api/pipeline/{user_id}/asignar      Asignar o reasignar
PATCH  /api/pipeline/{user_id}/estado       Mover de etapa
GET    /api/pipeline/{user_id}/historial    Bitácora del lead

GET    /api/tenants/{id}/pipeline           Vista de gerencia
GET    /api/tenants/{id}/metricas           Conversión, tiempos, ranking
```

Con `tenant_id` en la URL se valida contra el JWT: si no es el tuyo da
**404, no 403** — un 403 confirmaría que ese negocio existe.

#### Embudo

```
nuevo ──> contactado ──> en_seguimiento ──> cotizado ──> negociacion ──> ganado
  │            │                │              │              │
  └────────────┴────────────────┴──────────────┴──────────────┴──> perdido
                                                                      │
                          (reapertura) contactado <────────────────────┘
```

`ganado` es terminal. `perdido` no: se reabre hacia `contactado`. Las
transiciones viven en `pipeline_estados.py` y el `CHECK` de
`client_pipeline.estado` lista los mismos estados — si se agrega uno hay
que tocar los dos lados.

Un `PATCH .../estado` inválido responde **409 con las transiciones
posibles**, no con un mensaje genérico:

```json
{
  "detail": {
    "mensaje": "No se puede pasar de 'ganado' a 'contactado'",
    "estado_actual": "ganado",
    "transiciones_permitidas": [],
    "es_terminal": true
  }
}
```

#### Reparto de leads

La estrategia sale siempre de `tenant_vendedor_config`, nunca del código
del endpoint:

| Estrategia | Cómo elige |
|---|---|
| `carga` (default) | Menos leads abiertos. Empata → el que lleva más tiempo sin recibir → el más antiguo |
| `round_robin` | El siguiente de la rueda, con el puntero en `ultimo_vendedor_asignado_id` |
| `manual` | Devuelve `None` siempre: lo elige una persona |

Sin vendedores activos **no es un error**: el lead se crea con
`vendedor_id = NULL` y sale en el panel como pendiente de asignar.

Desactivar a un vendedor **no** reparte su cartera: deja de recibir leads
nuevos y nada más. Mover los que ya tiene es una llamada aparte y explícita
a `POST /vendedores/{id}/reasignar-pendientes`, que solo toca los estados
no cerrados — un `ganado` o un `perdido` son historia de quien los cerró y
cambiarles el dueño falsearía el ranking.

#### Integración con n8n

```
POST /api/eventos/mensaje-entrante     Cabecera: X-Internal-Token
```

n8n lo llama justo después de resolver el tenant. **No decide si el agente
contesta**: gestiona el embudo y devuelve los flags, y el propio workflow
elige con `agente_ia_activo` si sigue hacia el nodo del agente. La decisión
vive en n8n para no partir el control del flujo en dos lugares. Los
workflows existentes (`canal-entrada-universal`, `escalar-humano`,
`ejecutar-herramienta-tenant`) no cambian.

```
                        ┌─ módulo apagado ──> responde los flags y sale
mensaje entrante ──> ───┤
                        └─ módulo activo ───> asegura el lead en el embudo
                                              sin vendedor? ──> reparte
                                              agente apagado? ──> avisa
```

Se autentica con secreto compartido y no con el JWT del portal porque n8n
es una máquina y no tiene sesión de portal. **Sin `N8N_INTERNAL_TOKEN` el
endpoint responde 503**: falla cerrado a propósito, para que olvidar la
variable no deje abierto un endpoint que crea leads y que, probando UUIDs,
diría cuáles existen.

El aviso al vendedor (`notificaciones.py`) es un **stub**: registra en el
log y devuelve False. Queda por enganchar al sub-workflow de envío.

### CRM de campo (clientes, visitas, tareas)

Segundo módulo sobre el mismo equipo de `vendedores`, para venta en calle.
**No es lo mismo que el embudo de arriba** y por eso no comparten URL:

| | Qué es un "cliente" | Dónde vive |
|---|---|---|
| Embudo de chat | Un contacto que escribió por WhatsApp/IG (`users`) | `/api/pipeline/...` |
| CRM de campo | Un negocio físico que se visita (`clientes`) | `/api/clientes/...` |

```
GET    /api/clientes                    Cartera (estado, prioridad, buscar)
GET    /api/clientes/{id}
POST   /api/clientes                    Alta            (gerencia)
PUT    /api/clientes/{id}               Edición parcial (gerencia)
POST   /api/clientes/{id}/desasignar    Quitar vendedor (gerencia)

POST   /api/visitas/checkin             Check-in con geocerca
POST   /api/visitas/sync                Cola offline, idempotente
GET    /api/visitas                     Historial filtrable
GET    /api/visitas/{id}

GET    /api/tareas                      Agenda
POST   /api/tareas
PUT    /api/tareas/{id}
POST   /api/tareas/{id}/completar       Sella completado_en
POST   /api/tareas/{id}/reabrir
DELETE /api/tareas/{id}

GET    /api/reportes/actividad          Visitas y tareas por vendedor (gerencia)
```

#### Rol `vendedor`

`portal_users.role` es `VARCHAR(50)` **sin CHECK**, así que `'vendedor'`
entró como un valor más junto a owner/member/superadmin — sin migración y
sin sistema de permisos paralelo.

El JWT **no cambió**: sigue llevando solo `sub`. Ni el rol ni el
`vendedor_id` viajan en el token. `deps.vendedor_actual` relee de BD en
cada petición, igual que ya hacía `usuario_actual`, y rechaza con 403 si
la ficha no existe o si `vendedores.activo = false`. Desactivar a alguien
surte efecto en la siguiente petición y no cuando expire su token — que
con `ACCESS_TOKEN_MINUTES=60` sería hasta una hora de check-ins de quien
ya no trabaja ahí.

#### Alcance por rol

`crm.acceso_crm` resuelve quién llama y qué puede ver:

| Rol | Ve | Escribe |
|---|---|---|
| `vendedor` | Solo su cartera y sus visitas/tareas | Check-ins y sus tareas |
| `owner` / `superadmin` | Todo el tenant | Alta y edición de clientes, reportes |
| `member` | Todo el tenant | Nada |

Un `vendedor_id` en el query string se **ignora** cuando quien llama es un
vendedor: el filtro se fuerza a su propio id, así que el parámetro no
sirve para asomarse a la cartera de un compañero.

Dos errores distintos a propósito:

- **404** el cliente no existe, o es de otro tenant. Iguales para que
  probar UUIDs no revele qué negocios existen en otras cuentas.
- **403** existe en este mismo tenant pero es de otro vendedor. Acá sí se
  puede decir la verdad: es gente de la misma empresa, y un 404 mandaría
  al vendedor a reportar como perdido un cliente que solo no es suyo.

#### Geocerca

El veredicto lo calcula el servidor con las coordenadas guardadas del
cliente (`geo.evaluar_geocerca`); `dentro_de_geocerca` **no existe en el
esquema de entrada**, así que el teléfono no puede mandarlo.

La distancia y el veredicto se **persisten**. Si después mueven el pin del
cliente o cambian su radio, las visitas ya registradas conservan el
resultado que tuvieron: un reporte de productividad no puede cambiar
retroactivamente.

Un check-in fuera de la geocerca **se guarda y responde 201**. No es un
error del cliente, es un hecho que gerencia necesita ver; lo que cambia es
`dentro_de_geocerca` y el mensaje dice cuántos metros se pasó.

La misma fórmula existe dos veces —`geo.py` para el check-in (probable sin
BD) y la función SQL `distancia_metros` para reportes y consultas ad-hoc—.
Ambas usan 6 371 000 m de radio terrestre y hay una comprobación que las
compara punto por punto para que no se separen.

#### Sincronización offline

`POST /visitas/sync` es idempotente por `cliente_uuid_offline`, que genera
la app antes de salir a la calle. Reenviar el mismo lote no crea visitas
nuevas: devuelve las que ya estaban con `duplicada: true` y su `visita_id`
real, para que la app sepa qué borrar de su cola.

La idempotencia tiene dos capas, porque los duplicados llegan de dos
sitios:

1. **Dentro del lote** — una cola local puede traer el mismo elemento dos
   veces si la app reintentó y guardó de más. Lo resuelve `dedupe_lote`.
2. **Contra la base** — el índice único parcial sobre
   `cliente_uuid_offline`. El `ON CONFLICT ... WHERE cliente_uuid_offline
   IS NOT NULL` lleva el mismo predicado del índice a propósito: sin él
   Postgres no lo reconoce como árbitro del conflicto.

Responde **200 aunque haya elementos rechazados**, con el resultado por
elemento. Un 4xx global obligaría a la app a descartar el lote entero por
un solo check-in malo. Y cada elemento va en su propia transacción: un
cliente borrado mientras el teléfono estaba sin señal no puede costar el
resto de la cola.

### Panel de plataforma (nivel gerencia)

`/api/gerencia/*` — todo el router exige **nivel gerencia**, que no es el
rol `owner`/`superadmin` de un tenant. Son dos cosas distintas y conviene
no mezclarlas:

| | `gerencia_actual` (deps.py) | `gerencia_plataforma_actual` (deps.py) |
|---|---|---|
| De dónde sale | `portal_users.role` ∈ {owner, superadmin} | el correo está en `gerencia_users` |
| Alcance | **su** tenant | todos los tenants |
| Para qué | configurar su negocio | administrar el servicio |

La **primera** persona se da de alta por SQL (no hay nadie que pueda
hacerlo desde el panel todavía); de ahí en adelante, desde `/gerencia/equipo`:

```sql
INSERT INTO gerencia_users (email, full_name, cargo)
VALUES ('alguien@operativai.com.mx', 'Nombre Apellido', 'Operaciones');
```

Nadie puede quitarse el nivel a sí mismo, así que la tabla nunca queda
vacía desde el panel.

Se resuelve por `LOWER(email)` contra el `portal_users` de la sesión, así
que quien entra sigue autenticándose con su cuenta de siempre (o con
Google). Sacar a alguien de la tabla le quita el nivel en la **siguiente
petición**, no cuando expire su token: `deps.usuario_actual` relee el LEFT
JOIN en cada request.

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/gerencia/resumen?dias=` | KPIs: negocios por estado, MRR, cobrado, consumo, "pagan y no usan" |
| GET | `/gerencia/tenants?dias=&q=&estado=&orden=&limite=&offset=` | Listado con estado, plan y consumo agregado |
| GET | `/gerencia/tenants/{id}` | Ficha de un negocio |
| GET | `/gerencia/tenants/{id}/transacciones` | Sus cobros (existe aparte de `/api/pagos` porque aquel sale del tenant del JWT) |
| PATCH | `/gerencia/tenants/{id}/estado` | activo / prueba / suspendido / baja |
| PATCH | `/gerencia/tenants/{id}/servicios` | Enciende o apaga agente y módulo de vendedores |
| POST | `/gerencia/tenants/{id}/creditos` | Ajuste manual de saldo (positivo suma, negativo resta) |
| GET | `/gerencia/consumo?dias=&tenant_id=` | Serie diaria de tokens y desglose por modelo |
| GET | `/gerencia/auditoria?tenant_id=&accion=&limite=` | Bitácora, solo lectura |
| GET | `/gerencia/salud` | Problemas operativos de ahora + alertas de plataforma abiertas |
| PATCH | `/gerencia/alertas/{id}/revisar` | Cierra una alerta de plataforma |
| GET/POST | `/gerencia/usuarios` | Equipo de plataforma (`gerencia_users`) |
| DELETE | `/gerencia/usuarios/{id}` | Quita el nivel (nunca a uno mismo) |
| GET | `/gerencia/cohortes?meses=` | Retención mensual por mes de alta |
| POST | `/gerencia/tenants/{id}/impersonar` | Token de "ver como el negocio", solo lectura |
| GET/POST | `/gerencia/planes` | Catálogo de planes (activos e inactivos) |
| PATCH | `/gerencia/planes/{nombre}` | Edita un plan (no se puede renombrar) |

`orden=margen` ordena del peor margen al mejor.

`TenantGerenciaOut.email` es el correo de un solo portal_user del negocio
(owner primero, después superadmin, después el resto por antigüedad — mismo
criterio de prioridad que `/impersonar`), no una lista completa. `None` si
el negocio no tiene ningún usuario activo.

### Catálogo de planes

`/gerencia/planes` administra la misma tabla `planes` que lee
`GET /api/pagos/catalogo` para la pantalla de suscripción del portal.
Aquel filtra `WHERE activo`; esto no, porque gerencia necesita ver también
los planes apagados.

`nombre` es el identificador de la URL (`PATCH /gerencia/planes/{nombre}`),
no `id`: es UNIQUE en la tabla y es lo único que el resto del sistema usa
para referenciar un plan — `tenant_subscriptions.plan` lo guarda como texto
plano, sin FK. Por eso **no se puede renombrar un plan** desde el endpoint
de update: haría huérfanas las suscripciones que ya lo referencian sin que
nada avise. Para "renombrar", dar de alta uno nuevo y apagar el viejo
(`activo=false`) — apagarlo no toca a quien ya lo tiene contratado.

`PlanActualizarIn` es parcial: un campo en `null` significa "no tocar", no
"borrar el valor" — mismo criterio que `CambiarServiciosTenantIn`. No hay
forma de vaciar `precio_annual` una vez puesto desde el endpoint; es SQL
directo para ese caso raro.

Las tres acciones que cambian algo escriben en `gerencia_auditoria` **dentro
de la misma transacción** que el cambio.

**La suspensión tiene dientes.** `tenant_estado_plataforma.estado` en
`suspendido` o `baja` hace que `services/acceso_pagos.py` devuelva
`permitido=False`, y de ahí sale el `agente_ia_activo=false` que recibe n8n
en `/api/eventos/mensaje-entrante`. No es una etiqueta: el agente deja de
contestar aunque el negocio tenga plan vigente y créditos. `prueba` no
bloquea nada.

`tenants` es de n8n, así que el estado vive en su propia tabla del portal
(`tenant_estado_plataforma`) en vez de como columna — mismo criterio que
`tenant_servicios`.

**Ojo con `SQL_AGENTE_OPERANDO`** (routers/gerencia.py): es el gate de
pagos escrito en SQL para resolver la página entera de una vez en lugar de
una consulta por fila. Duplica la regla de `services/acceso_pagos.py`, que
sigue siendo la fuente de verdad. `tests/test_gerencia.py` compara las dos
implementaciones sobre una matriz de casos justamente para que no se
separen en silencio; si cambias una, cambia la otra.

### Ver como el negocio (impersonación)

`POST /gerencia/tenants/{id}/impersonar` con `{"motivo": "..."}` devuelve un
access token para ver el portal como lo ve el dueño del negocio. Es la única
pieza del panel que da acceso a datos de un cliente con otra identidad, así
que cada garantía vive en un lugar concreto:

| Garantía | Dónde |
|---|---|
| Solo lectura: todo lo que no sea GET/HEAD/OPTIONS → 403 | `deps._validar_impersonacion`, dentro de `usuario_actual` (lo usan todos los endpoints; no hay lista de permitidos que olvidar) |
| Si sacan al gerente de `gerencia_users`, muere en la siguiente petición | la misma función relee la tabla |
| Desde el "ver como" no se entra a `/gerencia` | `es_gerencia_plataforma` forzado a `False` |
| Dura `IMPERSONACION_MINUTOS` (30) y no se estira | sin refresh token; `/auth/refresh` solo acepta `type: refresh` |
| No marca alertas leídas por el socket | `realtime.marcar_leida` ignora sids de solo lectura |
| Queda registrado con motivo | `gerencia_auditoria`, acción `impersonacion` |

Entra como el `owner` (si no hay, `superadmin` o `member`), nunca como
`vendedor`. En el portal el token va a `sessionStorage`: solo afecta a esa
pestaña y la sesión del gerente en `localStorage` no se toca.

### Salud y alertas de plataforma

`/gerencia/salud` corre los detectores de `services/gerencia_salud.py`
(agente bloqueado por pago, consumo anómalo, token de Meta por vencer,
suscripción vencida sin pausar, cobros fallidos, pagos trabados en
pendiente, canales silenciosos). Un detector que falla no tumba la
pantalla: aparece como `detector_fallido`.

El consumo anómalo además tiene job propio (`jobs/gerencia_background.py`,
cada `CONSUMO_ANOMALO_INTERVALO_HORAS`): abre una alerta en
`gerencia_alertas` y avisa por correo a todo `gerencia_users`. Anómalo =
tokens de las últimas 24 h ≥ `CONSUMO_ANOMALO_FACTOR` × el promedio diario
de los 7 días previos, con ese promedio nunca por debajo de
`CONSUMO_ANOMALO_PISO_TOKENS`. Hay una sola alerta abierta por negocio (índice
único parcial), así que un pico que dura todo el día manda un correo, no 24.

### Margen por negocio

El costo de modelos está en USD y los planes se cobran en la moneda de la
pasarela (`STRIPE_CURRENCY`, MXN por defecto). Hace falta un tipo de
cambio para restarlos. `services/banxico.py` decide la fuente, en orden:

1. La moneda de cobro ya es USD → 1, sin llamar a nadie.
2. La moneda de cobro es MXN y hay `BANXICO_TOKEN` → el FIX oficial del
   día (SIE API de Banxico, serie `SF43718`), cacheado 6 h en memoria. Si
   Banxico falla, se sirve el último valor cacheado aunque esté vencido
   antes de rendirse.
3. `TIPO_CAMBIO_USD` manual, si está configurado.
4. Ninguno de los anteriores → `margen` y `costo_moneda` salen `null` en
   vez de calcularse con un número inventado.

La respuesta trae `tipo_cambio_fuente` (`moneda_usd` | `banxico` | `manual`
| `ninguno`) para que el panel muestre de dónde salió el número, no solo
si hay uno. El token se pide gratis en
`https://www.banxico.org.mx/SieAPIRest/service/v1/token`.

Ingreso del período = suscripción activa prorrateada a la ventana
(`precio_monthly × días / 30`) + créditos cobrados en la ventana. Es una
estimación a propósito: lo cobrado real cae a saltos (un plan anual entra
entero un día) y en ventanas cortas daría márgenes absurdos.

### Consumo de tokens (lo escribe n8n)

`POST /api/eventos/uso-tokens` — con `X-Internal-Token`, igual que
`/mensaje-entrante`. Se llama **después** de que el modelo respondió: lo que
se mide es el consumo real, no el estimado.

```json
{
  "tenant_id": "…", "conversation_id": "…",
  "origen": "agente",            // agente | herramienta | resumen | otro
  "modelo": "gpt-4o-mini",
  "tokens_entrada": 1200, "tokens_salida": 340,
  "costo_usd": "0.012000",
  "idempotency_key": "{{$execution.id}}:agente"
}
```

Va suelto y no colgado de `/mensaje-entrante` porque una sola respuesta
puede ser varias llamadas al modelo (el agente más cada herramienta), y
cada una tiene su propio consumo.

`idempotency_key` es opcional pero muy recomendable: hay un índice único
parcial sobre esa columna, así que un nodo reintentado no cuenta dos veces
— la respuesta trae `duplicado: true` y n8n puede seguir. Sin la llave no
hay candado, que es lo correcto: dos llamadas iguales al modelo son dos
consumos reales.

`tenant_token_usage` es el libro mayor crudo del costo del proveedor. **No
reemplaza a `tenant_credits`**, que es la unidad que se le cobra al cliente
y tiene su propio libro (`credit_transactions`). Están separados a propósito:
el día que cambie la equivalencia token→crédito, el histórico de consumo no
se toca.

## Flujo de conexión con Meta

```
Frontend                    Backend                     Meta
   |                           |                          |
   |-- FB.login() ------------------------------------->  |
   |<-- code -------------------------------------------  |
   |                           |                          |
   |-- POST /meta/conectar --> |                          |
   |                           |-- code → token corto --> |
   |                           |-- token corto → largo -> |
   |                           |-- debug_token ---------> |
   |                           |   guarda cifrado en BD   |
   |<-- {conectado: true} ---- |                          |
   |                           |                          |
   |-- GET /meta/paginas ----> |                          |
   |                           |-- /me/accounts --------> |
   |<-- [páginas] ------------ |                          |
   |                           |                          |
   |-- POST /meta/activar ---> |                          |
   |                           |-- subscribed_apps -----> |
   |                           |   set_channel_credentials|
   |<-- {resultados} --------- |                          |
```

El App Secret nunca sale del backend. El frontend solo maneja el `code`,
que sin el secret no sirve para nada.

## Antes del App Review

Mientras la app de Meta no tenga Acceso Avanzado aprobado, `/me/accounts`
solo va a devolver páginas de usuarios con rol en la app. El código no
cambia cuando se apruebe — simplemente empiezan a llegar más páginas.

Se puede desarrollar y probar todo hoy con las páginas propias.

## Pendientes conocidos

- **Renovación de tokens**: el user token de larga duración vence a los ~60
  días. Falta un job que revise `meta_connections.token_expires_at` y avise
  o renueve antes de que expire.
- **Revocación**: si el usuario quita el acceso desde Facebook, se entera
  hasta que falla un envío con error 190. Meta tiene un webhook de
  desautorización que valdría la pena escuchar.
- **Multi-usuario por tenant**: el schema lo soporta (varios `portal_users`
  con el mismo `tenant_id`), pero no hay endpoints de invitación todavía.
- **WhatsApp**: el alta pasa hoy por Kontesta (`services/whatsapp.py`), que
  es un intermediario temporal mientras la app de Meta no tenga Acceso
  Avanzado aprobado. La cuenta de Kontesta es una sola, la de OperativAI:
  el número de cada negocio se da de alta como línea dentro de ella *fuera
  del portal*, y `POST /api/canales/whatsapp/conectar` solo registra qué
  línea es de qué tenant. Falta: verificar contra Kontesta que la línea
  exista y sea del negocio (su API no documenta cómo consultarlas), y un
  índice único parcial sobre `channel_credentials.phone_number_id` que
  cierre del todo la carrera que hoy solo cubre un SELECT previo — es tabla
  del lado de n8n, así que le toca a quien sea dueño de ese esquema. El
  alta nativa de Meta (Embedded Signup) sigue siendo otro flujo, para
  cuando se migre a `MetaProvider`. `WHATSAPP_PROVIDER=neuroapi`
  (`NeuroApiProvider`) es una tercera opción ya implementada contra el BSP
  NeuroAPI/NeuroChat, con el mismo alcance y las mismas limitaciones que
  Kontesta hasta que se decida cuál usar en producción.
