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
