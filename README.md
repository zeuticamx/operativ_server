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

## Migraciones

No hay Alembic: son archivos `NN_*.sql` numerados que se aplican en orden
con `aplicar_sql.py`, que lee el **mismo** `DATABASE_URL` que la app (así lo
que se aplica cae sí o sí en la base que n8n lee).

```bash
python aplicar_sql.py --revisar          # solo lectura: qué falta y en qué base
python aplicar_sql.py 06_vendedores.sql  # aplicar
```

Cada archivo va en una transacción: si algo falla no queda a medio aplicar.
Todo es `IF NOT EXISTS`, así que reaplicar no rompe nada.

Desde cero:

```bash
python aplicar_sql.py 01_portal.sql 02_token_expires_tz.sql \
                      03_users_username.sql 04_verificacion_email.sql \
                      05_herramientas.sql 06_vendedores.sql
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
GET    /api/tenants/{id}/vendedores         Listado (?activo=true)
PATCH  /api/vendedores/{id}                 Activar / desactivar / editar
GET    /api/vendedores/{id}/clientes        Su cartera
POST   /api/vendedores/{id}/reasignar-pendientes   Reparte sus leads abiertos

POST   /api/clientes/{user_id}/asignar      Asignar o reasignar
PATCH  /api/clientes/{user_id}/estado       Mover de etapa
GET    /api/clientes/{user_id}/historial    Bitácora del lead

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
