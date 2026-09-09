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
```

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
- **WhatsApp**: los endpoints están pensados para Messenger/Instagram. El
  alta de WhatsApp usa Embedded Signup, que es otro flujo.
