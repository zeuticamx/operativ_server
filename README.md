# OperativAI — API del portal

Backend en FastAPI para el portal multitenant. Habla con la misma
`agente_db` que usa el workflow de n8n.

## Nombres que se confunden fácil

| Tabla | Qué contiene |
|---|---|
| `users` | Clientes finales que escriben por WhatsApp/IG/FB |
| `portal_users` | Dueños de negocio que entran al portal |

No son lo mismo. `users` ya existía del lado de n8n; `portal_users` es nuevo.

## Arranque

```bash
pip install -r requirements.txt
cp .env.example .env    # y llénalo
```

Genera el secreto de JWT:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Corre el SQL en `agente_db`:

```bash
psql $DATABASE_URL -f sql/01_portal.sql
```

Levanta:

```bash
uvicorn main:app --reload --port 8000
```

Docs interactivas en `http://localhost:8000/docs`.

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
POST   /api/auth/registro    Crea negocio + config de agente + usuario dueño
POST   /api/auth/login
POST   /api/auth/refresh
GET    /api/auth/yo
```

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
