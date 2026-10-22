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
POST   /api/auth/recuperar/solicitar    Código de recuperación al correo (siempre 202)
POST   /api/auth/recuperar/restablecer  Código + contraseña nueva (204; cierra sesiones)
POST   /api/auth/login
POST   /api/auth/refresh           Cookie (web) o body (app móvil) → TokenOut, rota la sesión
POST   /api/auth/logout            Cierra la sesión de refresh actual (siempre 204)
GET    /api/auth/yo
POST   /api/auth/invitacion/revisar     A qué negocio y con qué rol invita un enlace
POST   /api/auth/invitacion/aceptar     Enlace + contraseña → crea la cuenta (TokenOut)
```

### Sesión y refresh token (spec 006, `sql/62_refresh_sesiones.sql`)

El refresh token del portal web no va en el cuerpo de la respuesta ni se
guarda en `localStorage`: viaja en una cookie HttpOnly
(`COOKIE_REFRESH_NOMBRE`, por defecto `op_refresh`), puesta por
`/auth/login`, `/verificar`, `/google` e `/invitacion/aceptar`
(`routers.auth._emitir_sesion`). `TokenOut.refresh_token` sale `null` para
el portal.

| Atributo | Valor | Por qué |
|---|---|---|
| `HttpOnly` | sí | JS (y un XSS) no puede leerlo |
| `Secure` | `COOKIE_SECURE` (default `true`) | en local sobre HTTP plano (sin `dev:https`) hay que bajarlo a `false`, si no el navegador descarta la cookie |
| `SameSite` | `COOKIE_SAMESITE` (default `none`) | portal y API en dominios distintos: `Lax` no viajaría en la petición cross-site a `/auth/refresh` |
| `Path` | `/api/auth` | no sale en ninguna otra petición |
| `Max-Age` | `REFRESH_TOKEN_DAYS` días | igual que antes |

**App móvil** (fuera de este repo, no puede usar cookies de navegador):
manda `X-Cliente: movil` y sigue recibiendo/mandando el refresh en el
cuerpo, sin cookie ni Origin.

**Despliegue con portal y API en dominios distintos.** La cookie de
terceros la bloquean Safari y algunos Chrome, y el refresco fallaría sin
aviso. Para evitarlo el portal puede hacer de proxy: construir el frontend
con el build arg `NEXT_PUBLIC_API_PROXY=true` (más `NEXT_PUBLIC_API_URL`
apuntando al backend). Entonces el navegador llama a `/api/*` en el origen
del portal, `next.config.ts` lo reenvía al backend y la cookie es de primera
parte. En el backend: `COOKIE_SAMESITE=lax`, `COOKIE_SECURE=true` y el
origen del portal en `FRONTEND_ORIGINS` (el navegador lo manda como `Origin`
en cada POST). Los sockets (`/socket.io`) siguen yendo directo al backend,
así que `FRONTEND_ORIGINS` y el CSP del portal los siguen necesitando.

**CSRF.** Con `SameSite=None` la cookie viaja en cualquier petición
cross-site, así que `/auth/refresh` y `/auth/logout` exigen `Origin` en
`FRONTEND_ORIGINS` (403 si falta o no está en la lista); la app móvil
queda exenta porque no manda Origin ni usa la cookie.

**Rotación y reuso.** Cada refresh es de un solo uso: `refresh_sesiones`
(una fila por sesión/dispositivo) lo marca `usado_en` al canjearlo por uno
nuevo. Reusar uno ya canjeado es 401; si pasaron más de 30 segundos
(`_REUSO_GRACIA_SEGUNDOS`, la carrera normal de dos pestañas refrescando
casi a la vez) se revocan **todas** las sesiones del usuario — la señal de
que alguien más también lo tenía. Un refresh emitido antes de esta spec
(sin `jti`) se rechaza una vez; el siguiente login ya deja uno rotable.

**Migración.** `REFRESH_BODY_LEGACY` (default `true`) deja que un cliente
web viejo, con el refresh todavía en su `localStorage`, lo siga mandando en
el body de `/auth/refresh`; apagarla una vez desplegado el frontend nuevo
(`frontend/lib/auth.ts` ya no lo guarda ahí, solo lo reenvía una vez si lo
encuentra y lo borra).

### Roles dentro de un negocio

`portal_users.role`. El JWT solo lleva `sub`: el rol se relee de BD en cada
petición (`deps.usuario_actual`), así que cambiarlo o desactivar a alguien
surte efecto en su siguiente llamada.

| Rol | Cómo nace | Ve | Configura |
|---|---|---|---|
| `owner` | `/auth/registro` (o Google) | Todo el negocio | Todo |
| `superadmin` | a mano | Todo el negocio | Todo |
| `member` ("colaborador") | invitación | Conversaciones, embudo, agenda, métricas y la configuración de los números de WhatsApp (agente y herramientas por número), sin credenciales de herramientas | Nada (agente, canales, herramientas, equipo, pagos): si intenta editar, 403 |
| `vendedor` | invitación ligada a su ficha | **Solo lo suyo**: su ficha, sus leads, su cartera de campo y las conversaciones que le asignaron (o de sus leads sin dueño) | Nada |
| `proveedor` | invitación ligada a su ficha del calendario | **Solo lo suyo**: su agenda y las conversaciones que le asignaron (o de sus clientes sin dueño) | Sus descansos y días libres |

Las guardas, de la más amplia a la más estrecha:

- `deps.negocio_actual`: lista blanca `ROLES_NEGOCIO` (owner/superadmin/member).
  Va a nivel de router en `agente`, `canales`, `herramientas`,
  `conversaciones` y `calendario`, y por endpoint en las vistas completas del
  embudo (`/tenants/{id}/pipeline`, `/metricas*`, `/vendedores`,
  `/config-vendedores`) y en `/pagos/suscripcion|historial|catalogo`.
  `/pagos/acceso` queda abierto a todos: el portal reducido también lo usa.
- `deps.gerencia_actual` (owner/superadmin): toda escritura de configuración
  (agente, canales, herramientas, alta/edición de vendedores, servicios,
  reparto, etapas, agenda, equipo).
- Vendedor: `deps.vendedor_actual` / `crm.acceso_crm` lo limitan a su ficha.
  En el embudo, `GET /vendedores/{id}/pipeline|historial`,
  `PATCH /pipeline/{user_id}/estado` y `GET /pipeline/{user_id}/historial`
  responden **403** si el id o el lead es de un compañero (403 y no 404: es
  del mismo negocio). Asignar leads no puede.
- Proveedor: `deps.permitir_a_proveedor(...)` va a nivel de router en
  `calendario` y `conversaciones`. Deja pasar a ROLES_NEGOCIO a todo y al
  proveedor solo a los endpoints nombrados (lista blanca por nombre de
  función: uno nuevo queda cerrado hasta que se lo sume). Cada uno de esos
  endpoints lo acota con `deps.proveedor_actual` (ver "Proveedor con
  cuenta" abajo).
- WebSocket (`realtime.py`): vendedor y proveedor no entran a la room del
  negocio, solo a la suya (`usuario_{id}`), y solo marcan leídas sus alertas
  personales. Al proveedor le llegan copias personales de lo suyo
  (`avisar_a_proveedor`): reserva nueva o cancelada de sus citas, chat de su
  cliente que pide una persona, y los mensajes en vivo de esos chats.

`tests/test_roles.py` y `tests/test_proveedor_cuenta.py` recorren esta
matriz por HTTP.

### Proveedor con cuenta (`sql/37_proveedor_cuenta.sql`)

El dueño le da acceso desde Calendario › Proveedores (invitación con
`role: proveedor` y `proveedor_id`; al aceptarla se llena
`proveedores.portal_user_id`). Con su cuenta:

| Puede | No puede |
|---|---|
| Ver solo su columna de la agenda: `/calendario/proveedores` le devuelve su ficha y `/reservas` le fuerza `proveedor_id` | Crear, reprogramar o cambiar de barbero una cita |
| Cancelar sus citas y marcarlas completada / no asistió | Tocar citas de otro proveedor (403) |
| Leer su horario base | Cambiar su horario base, ni poner un "horario especial" (excepción con `disponible=true`) |
| Poner y quitar sus descansos y días libres | Ver el corte diario, la bitácora, el cupo, otros proveedores |
| Ver, responder, tomar y devolver a la IA las conversaciones de sus clientes | Ver métricas de conversaciones ni editar el contacto |

**De quién es una conversación**: del proveedor de la cita **más reciente**
(por `reservas.creado_en`, cancelada incluida) de ese cliente
(`services/conversaciones._PROVEEDOR_DEL_CLIENTE`). Un cliente que reservó
con dos proveedores es solo del último: una conversación nunca tiene dos
dueños. Un cliente que nunca reservó no es de ningún proveedor.

**Cupo**: `planes.max_proveedores` limita las fichas de proveedor
**activas**, igual que `max_vendedores` (402 `cupo_proveedores` al dar de alta
o reactivar; `GET /tenants/{id}/calendario/proveedores/cupo`). Todos los
planes quedan en NULL (sin tope) hasta que gerencia de plataforma lo defina en
`/gerencia/planes`.

**Citas vencidas**: `jobs/reservas_background.py` (cada
`RESERVAS_VENCIDAS_INTERVALO_MINUTOS`, 15) da por `no_asistio` toda cita
`confirmada` cuya hora de fin pasó hace más de
`RESERVAS_VENCIDAS_GRACIA_MINUTOS` (0). Deja el renglón en
`reserva_auditoria` con `origen = 'sistema'`. Si en realidad sí vino, se
corrige a `completada` (con su cobro) desde el portal: el cambio de estado no
restringe desde qué estado se llega.

### Equipo: cuentas por invitación
```
GET    /api/equipo/usuarios                    Cuentas del negocio (gerencia)
PATCH  /api/equipo/usuarios/{id}               {activo}: quitar o devolver el acceso
GET    /api/equipo/invitaciones                Sin aceptar (marca las vencidas)
POST   /api/equipo/invitaciones                {email, role: member|vendedor|proveedor, vendedor_id?, proveedor_id?}
POST   /api/equipo/invitaciones/{id}/reenviar  Enlace nuevo; el anterior muere
DELETE /api/equipo/invitaciones/{id}           Revocar
```

El dueño nunca ve ni pone contraseñas: al invitado le llega un enlace
(`{FRONTEND_ORIGINS[0]}/invitacion#t=<token>`) y la elige él. El token va en
el **fragmento** para que no quede en logs del portal, y viaja al backend en
el body. En la base solo queda su SHA-256 (`sql/36_invitaciones_equipo.sql`);
el enlace en claro sale una sola vez, en la respuesta de crear/reenviar, por
si el correo no llega y el dueño lo manda por WhatsApp (`correo_enviado`).

- Rol y tenant salen de la invitación, nunca del invitado.
- Vigencia 7 días, un solo uso. Una sola invitación viva por correo y por
  ficha en cada negocio: invitar de nuevo revoca la anterior.
- `role = vendedor` exige `vendedor_id`, y `role = proveedor` exige
  `proveedor_id` (ficha activa, del negocio, sin cuenta). Al aceptar, en la
  misma transacción se crea el `portal_user`, se llena el `portal_user_id`
  de la ficha y se cierra la invitación; si la ficha dejó de estar
  disponible, 409 y no se crea nada.
- Cualquier enlace que no sirva (no existe, usado, revocado, vencido) da el
  mismo **410**.
- El correo de `portal_users` es único en toda la plataforma: invitar uno que
  ya tiene cuenta es 409.
- Quitar el acceso (`activo: false`) no toca la ficha: el vendedor sigue en
  el reparto con sus leads y el proveedor en la agenda recibiendo citas. Para
  sacarlos se desactiva la ficha.
  Al dueño no se le quita el acceso desde acá.

`tests/test_equipo.py` cubre el flujo completo.

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

### Recuperación de contraseña

Pantalla `/recuperar` del portal (enlace "¿Olvidaste tu contraseña?" en el
login). Lógica en `services/recuperacion_password.py`, migración
`sql/32_recuperacion_password.sql` (tabla `password_resets`, un código
activo por `portal_user`, y `portal_users.credenciales_cambiadas_en`).

| Regla | Cómo |
|---|---|
| Vigencia **estricta de 5 minutos** | Constante `VIGENCIA_MINUTOS`, **no** variable de entorno. Emisión (`NOW() + 5 min`) y comprobación (`expires_at > NOW()`) usan el reloj de Postgres; en el segundo 300 ya no vale |
| Un solo uso | El canje borra la fila en la misma transacción que cambia la contraseña, condicionado a que siga vigente y sea el mismo código (dos canjes simultáneos: gana uno) |
| Intentos | `CODIGO_MAX_INTENTOS` (5); agotados, el código se descarta |
| Reenvío | `CODIGO_REENVIO_SEGUNDOS` (60). Un código nuevo anula el anterior |
| Hashing | `security.hash_password` (argon2id) para el código y la contraseña nueva; sin cambios al algoritmo |
| Cuentas solo-Google (`password_hash` NULL) | Reciben código y pueden definir una contraseña: el código prueba que leen el correo |
| Cuentas desactivadas | No reciben código |

**Anti-enumeración.** `/solicitar` responde siempre `202` con el mismo
cuerpo y hace todo el trabajo (buscar la cuenta, argon2, SMTP) en una
`BackgroundTask` después de responder: ni el contenido ni el tiempo de la
respuesta dicen si el correo existe. Por lo mismo, un fallo de SMTP no es un
502, solo queda en el log. `/restablecer` da **un único 400** ("incorrecto o
ya venció") para código errado, vencido, usado o agotado — distinguirlos
diría si ese correo tiene un código pendiente — y gasta un argon2 contra un
hash señuelo cuando no hay fila, para que tarde lo mismo. El "venció" que ve
el usuario lo pone el portal con su propia cuenta regresiva.

**Cierre de sesiones.** Restablecer escribe `credenciales_cambiadas_en =
NOW()`; `deps.usuario_actual` y `/auth/refresh` rechazan (401) los tokens
con `iat` anterior. Sin esto una sesión robada sobrevivía 30 días al cambio
(refresh token). `iat` va en segundos enteros: un token emitido en el mismo
segundo del cambio sigue valiendo. Además borra las filas de
`refresh_sesiones` del usuario (spec 006): ya no podrían canjearse de
todos modos, pero así no quedan ocupando la tabla hasta su `expira_en`.

### Aceptación de Términos y Condiciones

Ninguna cuenta nace sin evidencia de que el dueño aceptó las Condiciones del
servicio y el Aviso de privacidad (`/condiciones` y `/privacidad` del
portal). La evidencia es `portal_users.terminos_aceptados_en` (TIMESTAMPTZ
escrito por el servidor, NULL = no hay) y `terminos_version` (qué texto
estaba vigente: `TERMINOS_VERSION` en `config.py`, hoy la fecha de "Última
actualización" de esas páginas; súbela cuando cambien los términos).
Migración: `sql/28_aceptacion_terminos.sql`.

| Camino | Regla |
|---|---|
| `POST /auth/registro` | `acepta_terminos` es **obligatorio y debe ser el booleano `true`**. Ausente, `false`, `null`, `"true"` o `1` → **422** y ni siquiera se guarda el alta pendiente. La aceptación se guarda en `email_verifications` |
| `POST /auth/verificar` | Copia la evidencia del alta pendiente a `portal_users`. Una pendiente sin evidencia (anterior a la migración) no crea cuenta (400) |
| `POST /auth/google`, correo **nuevo** | Sin `acepta_terminos: true` → **428** `{"codigo": "terminos_requeridos", "cuenta_nueva": true}` y **no se crea nada** (ni tenant ni usuario) |
| `POST /auth/google` y `POST /auth/login`, cuenta **existente sin evidencia** | **428** hasta que se reenvíe con `acepta_terminos: true`; entonces se guarda. En `/login` el 428 va *después* de validar la contraseña, así no sirve para confirmar si un correo existe |
| Cuenta que ya aceptó | Entra como siempre; la evidencia previa no se toca |

`_crear_cuenta` recibe la evidencia como argumento obligatorio, así que no
puede existir un camino nuevo que cree cuentas sin ella. El frontend
reconoce el 428 y abre un paso intermedio ("Antes de continuar") que
reenvía la misma petición con la aceptación; para Google reenvía el mismo
`credential`, que dura pocos minutos (si venció, se pide volver a pulsar el
botón).

Las cuentas anteriores quedan con la evidencia en NULL y aceptan en su
próximo ingreso con `/login` o `/google`. Una sesión ya abierta sigue
funcionando hasta que su refresh token venza: `/auth/refresh` no pide
aceptación.

### Reporte de problemas (incidencias)

Botón "Reportar un problema" al pie del sidebar del portal (modal, sin
recargar ni navegar). Migración `sql/33_reportes_incidencia.sql`
(`reportes_incidencia` y `reportes_incidencia_adjuntos`). Se llama
"incidencias" en rutas y archivos porque `/reportes` ya es el de los
reportes analíticos del embudo (`routers/reportes.py`).

```
POST   /api/incidencias                    multipart: resumen, descripcion, contexto (JSON), adjunto (opcional)
GET    /api/gerencia/incidencias           lista (?estado=abierto|en_revision|resuelto)
GET    /api/gerencia/incidencias/{id}      detalle
GET    /api/gerencia/incidencias/{id}/adjunto   la captura
PATCH  /api/gerencia/incidencias/{id}      cambia el estado (deja asiento en gerencia_auditoria)
```

| Regla | Detalle |
|---|---|
| Quién puede reportar | Cualquier usuario autenticado, de cualquier rol y **aunque el plan esté vencido** (no cuelga de `requiere_herramienta`). En "ver como" es 403 como toda escritura |
| Quién reporta | Sale del JWT (usuario, correo, negocio). El formulario no puede cambiarlo |
| Campos | `resumen` 5–120 (una sola línea: va en el asunto del correo), `descripcion` 10–4000. Rango fuera → 422 |
| Contexto técnico | Lo arma el navegador (`frontend/lib/contexto-reporte.ts`): ruta sin hash ni tokens, navegador, SO, resolución, ventana, idioma, zona horaria. El backend solo conserva esas claves, recortadas; un contexto ilegible queda `{}` y no rompe el envío |
| Adjunto | Una imagen PNG/JPG, máx. `REPORTE_ADJUNTO_MAX_BYTES` (5 MB) y `REPORTE_ADJUNTO_MAX_PX` (8192). El tipo se decide por la firma de bytes (`services/imagen.py`), no por el nombre. 413 pesa de más, 415 no es imagen, 422 dañada |
| Límite | `REPORTES_MAX_POR_HORA` (3) por usuario → 429 con `Retry-After`. Conteo e INSERT en una transacción con candado por usuario |
| Aviso al equipo | Correo a cada `gerencia_users` en background (el reporte ya está guardado; un SMTP caído no le llega al usuario como error). Todo lo escrito por el usuario va escapado en el HTML |
| Dónde se ven | `/gerencia/incidencias` en el portal. No se usa `gerencia_alertas`: permite una sola alerta abierta por (tipo, negocio) y escondería el segundo reporte |

### Perfil del usuario y recordatorios

`/api/perfil` es siempre el del usuario de la sesión (no hay `{id}` en la
ruta): cualquier rol edita el suyo desde `/preferencias` del portal. El alta
no pide nada de esto. Migración: `sql/29_perfil_usuario.sql`.

```
GET    /api/perfil          datos + completo + faltantes + días de recordatorio
PUT    /api/perfil          guarda el perfil entero (se puede a medias)
PUT    /api/perfil/foto     multipart, campo "archivo"
GET    /api/perfil/foto     la foto propia (con token; el portal la muestra como blob)
DELETE /api/perfil/foto
```

**Completo** = `nombres`, `apellido_paterno`, `fecha_nacimiento` y `genero`
(`femenino` / `masculino` / `prefiero_no_decirlo`). Opcionales:
`apellido_materno`, `empresa` y la foto. Los nombres solo admiten letras
(con acentos), espacios, guion, apóstrofo y punto; hay que tener 18 años.
`perfil_completado_en` lo fija el servidor al completarse y vuelve a NULL si
se vacía un obligatorio.

**Foto:** solo JPEG/PNG, máximo 642×642 px (`PERFIL_FOTO_MAX_PX`) y 2 MB
(`PERFIL_FOTO_MAX_BYTES`). El formato se decide por la firma de los bytes y
la extensión tiene que coincidir; las dimensiones se leen del encabezado
(`services/imagen.py`, sin Pillow). El backend **rechaza** lo que excede
(415 formato, 413 peso, 422 dimensiones); el portal redimensiona con canvas
antes de subir. Se guarda en `portal_user_fotos` (BYTEA): el contenedor no
tiene volumen persistente.

### Cuestionario de bienvenida (`sql/42_onboarding.sql`)

Al crear una cuenta, el dueño contesta un cuestionario sobre su negocio y
el sistema le arma una configuración de arranque. `services/onboarding.py`,
`routers/onboarding.py`; en el portal, `/bienvenida`.

```
GET  /api/onboarding           estado + respuestas + recomendación (si hay respuestas)
PUT  /api/onboarding           guarda respuestas, devuelve la vista previa; no aplica nada
POST /api/onboarding/aplicar   {sobrescribir_prompt}: prompt + prueba + módulos
POST /api/onboarding/omitir    204
GET  /api/gerencia/tenants/{id}/onboarding   lo que contestó (solo lectura, gerencia de plataforma)
```

- **Quién:** solo owner/superadmin (`gerencia_actual`). Sin `requiere_herramienta`
  a propósito: la cuenta nueva no tiene plan y esto es lo que se lo recomienda.
  `/api/onboarding` está en `RUTAS_SENSIBLES`: "ver como" puede mirar, nunca
  escribir (aplicar otorga una prueba).
- **Pendiente:** `_crear_cuenta` (alta por correo y por Google) inserta la fila
  de `tenant_onboarding`; `GET /auth/yo` devuelve `onboarding_pendiente` (sin
  completar ni omitir, rol de gerencia, no en "ver como") y el portal manda a
  `/bienvenida` al aterrizar en `/dashboard`, una vez por pestaña. Los negocios
  anteriores no tienen fila: no se les impone, lo abren desde `/agente`
  ("Configuración guiada").
- **Prompt:** plantillas fijas por giro (`PLANTILLAS_GIRO`, mismas claves que
  `schemas.GiroNegocio`) + tono + tareas por módulo; sin LLM. El texto libre
  (`descripcion`, `horario`) se deja en una línea y con tope de largo.
  `PROMPT_INICIAL` vive en este servicio. **No pisa un prompt editado a mano**
  (distinto del inicial y del último que escribió el onboarding,
  `prompt_generado`) sin `sobrescribir_prompt`.
- **Plan recomendado:** el más barato de `planes WHERE activo` (orden de la
  grilla) que incluye los módulos sugeridos y cuyo `max_vendedores` /
  `max_proveedores` alcanza para el equipo declarado (un cupo solo cuenta si se
  usa ese módulo). Si ninguno cubre todo, el que deja menos afuera, con
  `cubre_todo=false` y `faltantes`. Lee la matriz de la tabla, no una copia.
- **Prueba:** al aplicar, el plan recomendado por `DIAS_PRUEBA` (14) días: fila
  de `tenant_subscriptions` con `origen='prueba'`, `otorgada_por='onboarding'`,
  créditos del plan y asiento `prueba_onboarding` en `gerencia_auditoria`. Una
  sola vez (`prueba_otorgada_en`) y **solo a quien nunca tuvo fila en
  `tenant_subscriptions`** (ni suspendido/baja): pagar, cancelar y volver a
  contestar no da otra. Nunca cobra: el resto se contrata en `/suscripcion`.
- **Módulos:** después del commit, enciende (nunca apaga) los sugeridos que el
  plan vigente permite — `gestion_vendedores_activo`, `calendario_activo` (con
  sus `tenant_tools`); los que no, vuelven en `modulos_pendientes`.
- Las respuestas las ve gerencia de plataforma; está declarado en el aviso de
  privacidad del portal (`frontend/app/privacidad`).

### Gestionar suscripción y borrar cuenta (solo el dueño)

```
POST   /api/pagos/portal-cliente   URL de una sesión del Customer Portal de Stripe
GET    /api/cuenta/eliminacion     si se puede borrar, por qué no, y qué se pierde
POST   /api/cuenta/eliminar        {confirmacion: "ELIMINAR", password | google_credential}
```

**Portal de Stripe** (`services/stripe_portal.py`): el Customer sale de
`tenant_subscriptions.stripe_customer_id` por el tenant del JWT, nunca del
body. 403 si no es `owner`, 409 sin Customer, 502 si Stripe falla (el caso
típico: el portal sin configurar en Dashboard → Settings → Billing →
Customer portal). Lo que se cancela allá vuelve por el webhook de siempre
(`customer.subscription.updated` → `cancela_al_vencer`).

**Borrar cuenta** (`services/eliminacion_cuenta.py`): borra el negocio entero
con el mismo `DELETE FROM tenants` en cascada que gerencia
(`services/eliminacion_tenant.py`, `modo="propietario"`). Orden: rol `owner`
(403), escribir `ELIMINAR` (400), contraseña actual, o credencial de Google
del mismo `google_id` en cuentas sin contraseña (403; 5 fallos → 429 durante
15 min, `sql/35_eliminacion_cuenta.sql`). Después, los bloqueos (409):
suscripción que todavía cobra (Stripe sin cancelar, Mercado Pago activa),
facturas `open` en Stripe, o Stripe que no contesta (se falla cerrado). Una
suscripción ya cancelada con días pagados **no** bloquea: se devuelve
`pagado_hasta` y el portal advierte que esos días se pierden. En la bitácora
queda UUID, plan y Customer de Stripe, sin correos ni nombres. El Customer de
Stripe no se borra (historial de facturas). Las sesiones abiertas dan 401 en
su siguiente petición porque `deps.usuario_actual` relee al usuario.

**Recordatorios** (`jobs/perfil_background.py`, cada
`PERFIL_RECORDATORIO_INTERVALO_HORAS`): mientras el perfil esté incompleto,
un correo + una alerta personal en la campana cada 24 h durante los primeros
`PERFIL_RECORDATORIO_DIAS` (20) días naturales desde `created_at` — 20 en
total. Solo roles `owner`/`superadmin`/`member` activos; las cuentas creadas
hace más de 20 días no entran. Cesan al completar el perfil o al terminar la
ventana. El envío se reserva con un `UPDATE … RETURNING`, así dos pasadas
simultáneas no duplican.

**Alertas personales:** `alertas.portal_user_id` NULL = del negocio (como
siempre); con valor = solo de ese usuario. No van a la room del tenant sino
a `usuario_{id}`, no aparecen en el historial ni en las estadísticas de sus
compañeros, solo su dueño las marca leídas, y el resumen diario a gerencia
las excluye.

### Canales
```
GET    /api/canales                  Canales conectados
POST   /api/canales/meta/conectar    code → user token de larga duración
GET    /api/canales/meta/paginas     Páginas que el usuario autorizó
POST   /api/canales/meta/activar     Guarda tokens + suscribe webhooks
POST   /api/canales/whatsapp/conectar  Registra la línea de Kontesta del tenant (un número más; sujeto al cupo del plan)
POST   /api/canales/instagram/neuroapi/iniciar  Abre la Connect Session de Instagram Direct (ver "Instagram Direct vía NeuroAPI")
GET    /api/canales/instagram/neuroapi/estado   Resultado de la última sesión de Instagram del negocio
DELETE /api/canales/{tipo}           Desconecta (Meta). Para WhatsApp, `DELETE /api/canales/whatsapp` responde 409 `usar_numeros`:
                                     se desvincula número a número (ver "Varios números de WhatsApp por negocio")
```

### Agente
```
GET    /api/agente           Config actual + `limites_plan` (modelos e historial que deja el plan)
PUT    /api/agente           Actualiza prompt, modelo, parámetros (402 si excede el plan)
GET    /api/agente/modelos   Catálogo completo de modelos (lo que deja cada plan va en `limites_plan`)
```
Ver "Límites del agente por plan".

### Conversaciones
```
GET    /api/conversaciones                Listado con filtros (?asignada=mias|sin_asignar|<uuid>)
GET    /api/conversaciones/metricas
GET    /api/conversaciones/{id}           Detalle con mensajes
POST   /api/conversaciones/{id}/mensajes       Respuesta manual (handoff humano)
POST   /api/conversaciones/{id}/tomar          Un humano toma el control y se la queda (status='transferred', sin acuse)
POST   /api/conversaciones/{id}/volver-a-ia    Le devuelve el control a la IA (y termina la asignación)
POST   /api/conversaciones/{id}/limpiar-contexto  Solo el dueño: el agente deja de usar lo anterior (no borra mensajes)
PUT    /api/conversaciones/{id}/asignacion     Owner/superadmin asigna o reasigna {asignado_a, nota?}
DELETE /api/conversaciones/{id}/asignacion     Deja la conversación sin asignar (owner, o el asignado la suya)
GET    /api/conversaciones/asignables          Cuentas a las que se les puede asignar (solo gerencia)
POST   /api/conversaciones/{id}/adjuntos       Imagen/documento por WhatsApp (multipart: archivo, leyenda?)
GET    /api/conversaciones/{id}/adjuntos/{aid} Contenido de un adjunto (JWT, filtrado por tenant)
GET    /api/media/{token}                      Público: lo descargan NeuroAPI/Meta (token aleatorio)
POST   /api/eventos/adjunto-entrante           n8n asocia a un mensaje el archivo que mandó el cliente
POST   /api/eventos/conversacion-transferida   n8n avisa que escaló; el backend asigna según `area`
POST   /api/eventos/mensaje-conversacion-asignada  n8n avisa al asignado que el cliente escribió
```

### Asignación de conversaciones (`sql/41_conversaciones_asignacion.sql`)

Una conversación transferida a un humano tiene como máximo **un responsable**:
`conversations.asignado_a` → `portal_users.id` (la cuenta, no la ficha de
vendedor / proveedor: un rol nuevo no necesita migración, solo sumarse a
`deps.ROLES_ASIGNABLES`). Cada cambio queda en `conversacion_asignaciones`.
Asignar apaga la IA (`status='transferred'`).

| Acción | Quién |
|---|---|
| Ver todas, con quién la tiene | owner, superadmin, member |
| Asignar / reasignar / quitar | owner, superadmin (`gerencia_actual`) |
| Tomar | quien la vea, **si está sin asignar**; si ya es de otro, 409. Solo el owner se la quita |
| Soltar la propia | el asignado (`DELETE .../asignacion`) |
| Contestar y `volver-a-ia` | el asignado o la gerencia (`puede_operar`); sin asignar, quien la vea |

- **Alcance de vendedor y proveedor** (`conversaciones._visible_acotado`): lo
  que les asignaron, más lo que su ficha les hace propio **mientras nadie lo
  tenga** (proveedor: cita más reciente con él; vendedor: lead de su cartera).
  Asignada a otro, deja de ser suya. Ajena del mismo negocio: 403; de otro
  negocio o inexistente: 404. `permitir_a_roles_acotados(...)` es lista blanca
  por nombre de endpoint, como `permitir_a_proveedor`.
- **Exclusión mutua**: `tomar` / `asignar` / `soltar` bloquean la fila
  (`SELECT … FOR UPDATE`), así que dos personas que la toman a la vez no se
  pisan: gana una y la otra recibe 409 con el nombre de quien la tiene.
- **Agente de n8n** (`services/asignacion_conversaciones.py`): `escalar_humano`
  manda `area` (`ventas` | `agenda` | `otro`) a `/eventos/conversacion-transferida`.
  El LLM solo **sugiere**; `elegir_destino` (pura, con tests) decide: `agenda`
  → proveedor de la cita más reciente; `ventas` → vendedor del lead, o el que
  toque por `tenant_vendedor_config`; `otro` o un valor inventado → owner. Todo
  se valida (ficha y cuenta activas, módulo encendido). **Si no hay a quién,
  cae en el owner** con `asignacion_nota` y una alerta `asignacion_fallida`
  para que reasigne. Es idempotente: si ya tenía dueño (reintento de n8n, o un
  humano se adelantó) no pisa nada, y una falla de asignación nunca tumba el
  aviso.
- **Mensajes del cliente**: n8n llama `/eventos/mensaje-conversacion-asignada`
  cuando el cliente escribe en una transferida; el asignado recibe una alerta
  personal (una sin leer por conversación, para no llenarlo).
- **Quien se queda sin acceso** (desactivar la cuenta en `/equipo`, o la ficha
  de vendedor / proveedor): sus conversaciones pasan al owner con una nota
  (`reasignar_de_cuenta`); si no, nadie más podría tomarlas.
- Una conversación cerrada que el cliente reabre es una conversación nueva en
  n8n: **no hereda** el asignado.
- `alertas.tipo` es `VARCHAR(30)`: los tipos nuevos (`conversacion_asignada`,
  `asignacion_fallida`, `mensaje_conversacion_asignada`) caben justo.
- Escribe en `conversations` (tabla de n8n) con la misma excepción deliberada
  del handoff; las columnas son nullables, así que los INSERT de n8n no cambian.

**Adjuntos de WhatsApp.** Solo JPG/PNG (tope `WA_IMAGEN_MAX_BYTES`, 5 MB) y
PDF/DOCX (`WA_DOCUMENTO_MAX_BYTES`, 16 MB). El tipo se decide por la firma de
los bytes y la extensión tiene que coincidir (`services/adjuntos.py`): 413
pesa de más, 415 tipo no permitido, 422 vacío. Se guardan en
`message_attachments` (`sql/34_mensajes_adjuntos.sql`, BYTEA; `messages` no se
toca porque es de n8n). NeuroAPI y Meta reciben una `link` HTTPS a
`/api/media/{token}`, por eso `BASE_URL_BACKEND` tiene que ser pública y HTTPS
(si no, 409). El mensaje se inserta antes de enviar y se borra si el envío
falla. Facebook/Instagram no soportan archivos (400).

Gerencia de plataforma tiene los mismos tres últimos (más los dos GET) bajo
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

POST   /api/vendedores                      Alta (gerencia; respeta el cupo del plan)
GET    /api/tenants/{id}/vendedores         Listado (?activo=true) — sin exigir el flag
GET    /api/tenants/{id}/vendedores/cupo    Activos contra planes.max_vendedores
PATCH  /api/vendedores/{id}                 Activar / desactivar / editar (gerencia)
GET    /api/vendedores/yo                   La ficha del vendedor que llama (rol vendedor)
GET    /api/vendedores/{id}/pipeline        Su embudo (un vendedor, solo el suyo)
POST   /api/vendedores/{id}/reasignar-pendientes   Reparte sus leads abiertos

POST   /api/pipeline                        Alta manual de un lead en el embudo
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

**Cupo del plan.** `planes.max_vendedores` (None = sin tope) limita las
fichas **activas**, tengan o no cuenta: lo que el plan vende es gente
recibiendo leads. Dar de alta o reactivar más allá del tope responde
**402** con `detail.codigo = "cupo_vendedores"` (y `mensaje`, `maximo`,
`activos`) — sin `herramienta`, así el portal muestra el mensaje y no el
modal de plan. Desactivar libera el lugar. El conteo va con un
`pg_advisory_xact_lock` por tenant dentro de la transacción del alta, para
que dos altas simultáneas no pasen las dos (`acceso_plan.exigir_cupo_vendedor`).
Invitar a una ficha que ya existe no consume cupo.

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
GET    /api/tareas/{id}/reprogramaciones  Bitácora de cambios de fecha (ver "Agenda de ventas")

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
| `vendedor` | Solo su cartera y sus visitas/tareas | Check-ins, sus tareas y **alta de clientes** |
| `owner` / `superadmin` | Todo el tenant | Alta, edición y reasignación de clientes, reportes |
| `member` | Todo el tenant | Nada (ver "Roles dentro de un negocio") |

Cualquier otro rol (`proveedor` del calendario, o uno desconocido) recibe
**403**: `acceso_crm` es lista blanca (`ROLES_NEGOCIO` + vendedor), igual que
`deps.negocio_actual`.

Un `vendedor_id` en el query string se **ignora** cuando quien llama es un
vendedor: el filtro se fuerza a su propio id, así que el parámetro no
sirve para asomarse a la cartera de un compañero.

Lo mismo en el alta: un vendedor puede dar de alta un cliente (lo captura
en campo, con su pin de GPS) y le queda asignado a él — el `vendedor_id`
del body se ignora, porque si se respetara serviría para colgarle cartera
a un compañero. Gerencia sí elige a quién se lo asigna, y es la única que
puede **editar, reasignar y desasignar** (`exigir_gerencia_crm`): mover el
pin o cambiar el radio de una geocerca cambia cómo se validan las visitas
futuras de todo el equipo, no solo las de quien lo capturó.

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

### Agenda de ventas (`sql/39_agenda.sql`)

Un calendario que junta las **tareas** del CRM de campo y los **seguimientos**
de los leads del embudo. Lo usan `/vendedores/agenda` (negocio) y
`/mi-cartera` (vendedor) en el portal; arrastrar un pendiente a otro día lo
reprograma.

```
GET  /api/agenda?desde&hasta[&vendedor_id]           Pendientes del rango, de las dos fuentes
PUT  /api/agenda/seguimientos/{user_id}              Poner / mover / quitar (fecha null) el seguimiento de un lead
GET  /api/agenda/seguimientos/{user_id}/reprogramaciones
```

- **Cada módulo por su lado.** `GET /agenda` no responde 402 si falta uno:
  trae tareas si el plan incluye `crm_campo`, y seguimientos si incluye
  `vendedores` **y** el módulo está encendido. `tareas_disponibles` /
  `seguimientos_disponibles` dicen cuál entró. Rango máximo: 100 días.
- **Un lead tiene un solo próximo seguimiento**: `client_pipeline.proximo_seguimiento`
  (+ `seguimiento_nota`). Agendarlo **no** toca `actualizado_en`, que es
  "cuándo se movió de etapa" y alimenta la alerta de lead sin actividad. No
  se puede agendar uno en un lead ganado/perdido (409). Va aparte de
  `/api/pipeline/*` para no tocar el contrato de la app de vendedores;
  `PipelineOut` solo suma los dos campos, opcionales.
- **Mover una tarea sigue siendo `PUT /tareas/{id}`.** Si cambia la fecha y
  no viene `estado`, se recalcula: futura → `pendiente`, pasada → `vencida`;
  una `completada` se queda completada. Un job (`jobs/tareas_background.py`,
  cada `TAREAS_VENCIDAS_INTERVALO_MINUTOS`, 15 por defecto) marca `vencida`
  la pendiente cuya fecha pasó — antes ese estado existía pero nadie lo
  escribía.
- **Bitácora**: `agenda_reprogramaciones`, un renglón por cambio de fecha de
  una tarea o de un seguimiento (quién, de qué fecha a cuál), en la misma
  transacción que el UPDATE. Editar solo el título no escribe nada.
- **Aviso personal al vendedor** (alerta `agenda_reprogramada`, a su room de
  usuario) cuando otro le mueve, le asigna o le agenda un pendiente. Si se
  lo mueve él mismo, o no tiene cuenta del portal, no hay aviso. Es
  best-effort, después del commit.

Permisos: los de `acceso_crm`. El vendedor ve y mueve solo lo suyo (403 con
lo de un compañero, 404 si el lead no es del negocio); el `vendedor_id` del
query se ignora para él. Reasignar una tarea sigue siendo de gerencia.

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
| POST | `/gerencia/tenants/{id}/prueba` | Otorga un plan activo del catálogo como prueba (días, semanas o fecha; máx. 3 meses) |
| POST | `/gerencia/tenants/{id}/prueba/revocar` | Termina la prueba ya (queda `cancelada`) |
| GET | `/gerencia/tenants/{id}/eliminacion` | Si se puede eliminar, por qué no, y qué se perdería |
| POST | `/gerencia/tenants/{id}/eliminar` | Borrado definitivo (`{confirmacion: <nombre exacto>}`) |
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

**Eliminar un negocio** (`services/eliminacion_tenant.py`): borrado duro,
`DELETE FROM tenants` con todo lo que cuelga en cascada (usuarios del
portal, conversaciones, CRM, calendario, historial de cobros). Dos pasos:
primero se da de baja con `PATCH .../estado` (la inactivación de siempre) y
después se elimina. 409 con `{codigo, mensaje, bloqueos}` si:

- `cuenta_activa`: el estado no es `baja` (tampoco vale `suspendido`);
- `suscripcion_vigente`: `activa`, `fecha_renovacion` futura, una
  Subscription de Stripe sin `cancelada_en` (aunque figure `pausada`, Stripe
  la sigue reintentando) o una de Mercado Pago no cancelada;
- `cuenta_propia`: algún usuario del negocio tiene nivel gerencia.

Los créditos sin usar no bloquean: se advierten en la confirmación. Las
filas de estado y suscripción se toman `FOR UPDATE`, así que un webhook no
puede reactivar la suscripción entre la revisión y el borrado. Queda una
entrada `eliminar_tenant` en la bitácora con la foto de lo borrado (nombre,
correos, `stripe_customer_id`, total cobrado); el cliente de Stripe no se
toca. Si en producción alguna tabla apunta a `tenants` sin cascada, el
DELETE falla, se revierte todo y responde 409 `referencias_pendientes`.

**Plan de prueba** (`services/pruebas.py`, `sql/26_plan_prueba.sql`): no es
un mecanismo aparte, es una fila de `tenant_subscriptions` `activa` con
`origen='prueba'`, `precio_monthly=0` (no infla el MRR) y
`fecha_renovacion` = fin de la prueba. Por eso el gate por plan, el de pagos
y el job de vencimiento la tratan igual que a un plan pagado. Reglas: tope de
3 meses calendario, solo planes `activo`, no pisa un plan pagado vigente
(409) pero sí otra prueba o una suscripción pausada/cancelada, y un pago
aprobado la convierte en `origen='pago'`. Revocar la deja `cancelada` en vez
de borrar la fila: sin fila el negocio contaría como "nunca pagó" y
`acceso_pagos` dejaría al agente contestando.

Al vencer, `acceso_plan` corta las herramientas del portal **al instante**
(lee una `activa` con `fecha_renovacion` pasada como `pausada`, para toda
suscripción, no solo pruebas). El agente de n8n (`acceso_pagos` y su espejo
`SQL_AGENTE_OPERANDO`) sigue esperando al job, hasta
`SUSCRIPCION_REVISION_INTERVALO_HORAS` (1 h por defecto).

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
para referenciar un plan — `tenant_subscriptions.plan` es **FK a
`planes(nombre)`** desde `25_planes_herramientas.sql`. Antes era un `CHECK`
con los tres nombres de fábrica, así que un plan creado desde el panel no se
podía contratar; ahora cualquier plan dado de alta se puede contratar, y uno
inexistente no. El nombre va de 2 a 50 caracteres (el largo de
`tenant_subscriptions.plan`). El endpoint de update sigue sin permitir
renombrar; para "renombrar", dar de alta uno nuevo y apagar el viejo
(`activo=false`) — apagarlo no toca a quien ya lo tiene contratado, y la FK
impide borrar un plan que alguien tiene.

Cada plan dice además **qué herramientas del portal incluye** (ver "Control
de acceso por plan" abajo): `agente_ia_activo`, `gestion_vendedores_activo`,
`herramientas_activo`, `crm_campo_activo`, `calendario_activo`.

**Los precios son netos.** `precio_monthly`/`precio_annual` se guardan y se
muestran tal cual, sin ningún impuesto sumado. Con Stripe, lo que de verdad
se cobra cada mes es el **Price recurrente** del plan (`stripe_price_id`,
editable desde `/gerencia/planes`; ver "Suscripción recurrente con Stripe"
abajo): si se cambia `precio_monthly`, hay que crear el Price nuevo en
Stripe y pegarlo en el plan, o el portal mostrará un precio y Stripe cobrará
otro. El monto real de cada cobro queda en `tenant_transactions.monto`.
Los paquetes de créditos sí se cobran con el precio de la tabla.

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

### Herramientas del agente para el calendario

El agente de n8n usa el calendario a través de nueve filas de `tenant_tools`
(`consultar_servicios`, `consultar_proveedores`, `consultar_disponibilidad`,
`crear_reserva`, `cancelar_reserva`, `consultar_cancelacion_reserva` e
`historial_citas_cliente`, `consultar_citas_proximas` y `reagendar_reserva`)
que apuntan a `/api/eventos/calendario/*`
con `X-Internal-Token`. Las crea `services/herramientas_calendario.py` al
encender el calendario (dueño, gerencia o una prueba de plan) y las pausa al
apagarlo; al arrancar, `sincronizar_todos()` se las da a quien ya lo tenía
encendido y refresca URL (`BASE_URL_BACKEND`) y token (`N8N_INTERNAL_TOKEN`,
guardado con `set_tool_credentials`). No pisa textos editados por el dueño ni
reactiva una que él pausó. No se pueden eliminar desde `/herramientas` (409).

Si cambias el contrato de esos endpoints, actualiza también `definiciones()`:
es lo que lee el workflow `ejecutar-herramienta-tenant`. Desde la regla de
cancelación se suma `consultar_cancelacion_reserva` (ver abajo) y, con el
historial de citas, `historial_citas_cliente`, y con re-agendar
`consultar_citas_proximas` y `reagendar_reserva`: hoy son **nueve**.

### Regla de cancelación de citas (`sql/43_regla_cancelacion_citas.sql`)

Cada servicio puede exigir una **antelación mínima para cancelar**
(`servicios.antelacion_cancelacion_horas`, entero 0-720, `NULL` = sin regla). La
configuran owner/superadmin (`gerencia_actual`) al crear o editar el servicio; en
`PATCH` omitir el campo no lo toca y mandar `null` lo borra. Editarla no
reescribe las cancelaciones pasadas: la marca se guarda en la reserva al cancelar.

- **Tardía** = cancelación del **cliente** (por el agente, endpoint de
  `/api/eventos/calendario/reservas/cancelar`) hecha con menos de N horas exactas
  para la cita. Justo N horas no es tardía. La cancelación desde el portal es
  `cancelada_por='equipo'` y nunca es tardía. El origen lo decide el endpoint, no
  el cuerpo. Una tardía **no se bloquea**: se cancela, se marca
  (`cancelacion_tardia`, `antelacion_aplicada_horas`, `cancelada_por`,
  `cancelada_en`), se audita (evento `cancelada` con origen y segundos) y la alerta
  al negocio dice cuánto faltaba.
- **409 con `detail` objeto** `{codigo, mensaje, estado?}` (no texto): 
  `reserva_no_confirmada` (solo se cancela una `confirmada`) y `cita_ya_iniciada`
  (el cliente no puede cancelar una cita que ya empezó; el equipo sí hasta que
  el job la pase a `no_asistio`). También `calendario_inactivo` en los dos
  endpoints del agente. El portal muestra el mensaje fijo y recarga.
  `evaluar_cancelacion` (`services/cancelacion_citas.py`, pura, reloj inyectable)
  decide todo con estado primero, luego hora de inicio.
- **Consulta previa**: `POST /api/eventos/calendario/reservas/consultar-cancelacion`
  (solo lectura, sin auditoría, `X-Internal-Token`) devuelve `puede_cancelar`,
  `motivo_no_cancelable`, `antelacion_minima_horas`, `segundos_restantes` y
  `cancelacion_seria_tardia`. La expone la herramienta del agente
  `consultar_cancelacion_reserva`, para que avise al cliente antes de cancelar.
  Es informativa: la marca final la decide la cancelación real.
- **Reporte de plataforma**: `GET /api/gerencia/cancelaciones-tardias` (solo
  gerencia de plataforma; owner/member y "ver como" reciben 403) devuelve por
  tenant, servicio y periodo `tardias` y `total_cliente` sin datos del cliente
  final. Filtros `desde`/`hasta` (por defecto 30 días, máximo 366, si no 422),
  `granularidad=dia|mes`, `tenant_id`, `servicio_id`; el periodo usa la zona
  horaria de cada tenant. Pantalla: `/gerencia/cancelaciones`.
- **Despliegue**: aplicar primero la migración 43 (aditiva). Al arrancar,
  `sincronizar_todos()` crea `consultar_cancelacion_reserva` en los negocios con
  calendario encendido. **Revisión manual del workflow de n8n**: los 409 de
  cancelar ahora traen `detail` como objeto; el workflow y el prompt deben leer
  `detail.mensaje`/`detail.codigo` (si lo tratan como texto el agente verá
  "[object Object]") y llamar a la consulta antes de cancelar.
- **Límite conocido (R32)**: `_encender` no pisa descripciones ya guardadas, así
  que el texto nuevo de `cancelar_reserva` solo llega a negocios que activen el
  calendario después; los existentes conservan la descripción vieja y dependen de
  la de la herramienta nueva.

Tests: `tests/test_cancelacion_citas.py` (puro), `test_calendario.py`,
`test_herramientas_calendario.py` y `test_gerencia_operacion.py`.

### Historial de citas del cliente (spec 002)

Muestra las **10 citas pasadas o canceladas más recientes** del cliente de una
conversación, en el portal (panel "Historial de citas" bajo la asignación) y como
herramienta del agente. Solo lectura: no modifica citas ni escribe auditoría. La
regla (elegibilidad, orden, tope, UUID válido, fecha local) vive en
`services/historial_citas.py` (pura, sin I/O); la consulta, en
`services/calendario.py::historial_citas_de_conversacion`.

- **Quién es el cliente**: se deduce de la conversación (`conversations.user_id`),
  nunca de un dato que mande el llamante, y solo cuentan las citas con
  `reservas.user_id` igual: no se une por teléfono ni nombre. Solo conversaciones
  de `whatsapp`, `instagram` o `facebook` (lista blanca; el widget web no cuenta).
- **Citas elegibles**: `cancelada`, `completada` y `no_asistio` siempre; una
  `confirmada` solo si ya empezó (`hora_inicio <= ahora`). Orden por inicio
  descendente (empate por `creado_en`), tope 10, `hay_mas` = hay más de 10
  elegibles. Fechas ISO 8601 con el desfase de la **zona del negocio**, no la del
  navegador.
- **Portal**: `GET /api/conversaciones/{id}/historial-citas`
  (`routers/conversaciones_historial.py`). Respuesta `{citas, hay_mas}`; cada cita:
  `fecha_hora_inicio`, `hora_fin`, `servicio_nombre`, `proveedor_nombre`, `estado`,
  `notas`, `cancelada_por` (`cliente|equipo|null`), `cancelacion_tardia`. Nunca
  identificador de cita, precio, pago, motivo ni contacto.
  Orden de verificación: 401 → ruta con UUID mal formado 422 → vendedor 403 → plan
  sin acceso a conversaciones 402 → proveedor sin ficha o desactivado 403 →
  calendario apagado 409 `{codigo: "calendario_inactivo", mensaje}` / plan sin
  calendario, vencido o cuenta suspendida 402 → conversación no visible para el
  proveedor 404 → conversación inexistente, de otro negocio o de canal no admitido
  404 (`Conversación no encontrada`, idéntico en todos los casos).
- **Roles**: owner, superadmin y member ven todas las citas del cliente en el
  negocio; el **proveedor** solo las suyas (tope y `hay_mas` sobre ese subconjunto)
  y solo si la conversación le es visible (si no, 404, no 403); el vendedor, 403.
  El endpoint se llama `historial_citas` porque `permitir_a_proveedor` filtra por
  nombre de función.
- **Agente**: `POST /api/eventos/calendario/historial-cliente` con
  `X-Internal-Token` y cuerpo `{tenant_id, conversation_id}` (el resto se ignora).
  Respuesta `{citas, hay_mas}` con solo `fecha_hora_inicio`, `servicio_nombre`,
  `proveedor_nombre` y `estado`: **nunca** identificador de cita, notas, quién
  canceló, tardía, precio, pago, motivo ni teléfono. Orden: 401 → `tenant_id` o
  `conversation_id` ausente 422 → calendario apagado 409 con `codigo` / pagos del
  chat agotados 402 → conversación inexistente, ajena, de widget o
  `conversation_id` inválido (nulo, número, sin forma de UUID con guiones) 404
  `Conversación no encontrada`. Ve el historial completo del cliente (sin filtro de
  proveedor).
- **Herramienta** `historial_citas_cliente` (`http_post_json`, sin parámetros del
  modelo, sin contacto, no idempotente): la crea `services/herramientas_calendario.py`
  junto con las demás al encender el calendario. El dueño puede pausarla, no
  borrarla.
- **Compuerta `HISTORIAL_CITAS_N8N_PUBLICADO`** (`false` por defecto; ver
  `.env.example`). La herramienta solo puede estar activa si n8n ya envía el
  `conversation_id` **real** de la conversación; si no, el modelo podría pasar el
  de otra y leer el historial de otro cliente. Con `false`: la fila se crea
  siempre pero **desactivada** (marcada `pausada_por_sistema`, salvo que el dueño
  ya la hubiera pausado), `PATCH /api/herramientas/{tool_key}` con
  `is_enabled=true` da 409 `Esta herramienta aún no está disponible para tu
  negocio.` (rechaza todo el PATCH; 404 si la fila no existe) y el arranque deja un
  aviso en el log con los negocios afectados. Con `true`: se comporta como las
  demás (se reactiva una pausada por el sistema; una pausa del dueño se respeta).
  Cambiar la variable exige reiniciar el backend; al arrancar `sincronizar_todos()`
  aplica el nuevo valor.
- **Orden de despliegue** (no alterar):
  1. Desplegar backend y portal con la variable en `false` (no hay migración SQL).
  2. En n8n, flujo `ejecutar-herramienta-tenant`, nodo "Prepara HTTP POST": añadir
     `conversation_id` (el real, el que recibe el nodo de inicio) al body, **después**
     de los parámetros del modelo para que prevalezca; no tocar la clave de
     repetición. **Publicar** el flujo (un cambio solo en borrador no cuenta).
     Estado: publicado el 2026-10-12 (versión `34d6da8f` -> `0cc2a050`); falta la
     prueba manual del paso 3.
  3. Prueba manual (criterio 30) en un negocio de pruebas: en una conversación real
     de WhatsApp/Instagram/Facebook, con el modelo instruido para pasar otro
     `conversation_id`, la petición que recibe el backend lleva el de la
     conversación real y el historial es el de ese cliente. Guardar la ejecución y
     la petición.
  4. Solo con la prueba superada: `HISTORIAL_CITAS_N8N_PUBLICADO=true` y reiniciar.
     Después el dueño activa la herramienta desde `/herramientas`.
- **Reversa**: poner la variable en `false` y reiniciar; al arrancar
  `sincronizar_todos()` desactiva la herramienta en los negocios con calendario
  encendido y el dueño no puede reactivarla. Si basta con parar un negocio, el
  dueño la pausa desde `/herramientas`. El panel del portal no depende de la
  variable.

Tests: `tests/test_historial_citas.py` (puro), `test_historial_citas_api.py` (portal
y agente), `test_herramientas_calendario.py` (herramienta y compuerta) y
`test_pruebas.py`.

### Re-agendar una cita desde el agente

Antes el agente "re-agendaba" creando una cita nueva con `crear_reserva` y la
anterior quedaba confirmada, ocupando horario. Ahora hay dos herramientas más (en
total **nueve**, `services/herramientas_calendario.py`) y una regla en el prompt de
n8n.

- **`consultar_citas_proximas`** → `POST /api/eventos/calendario/citas-proximas`,
  cuerpo `{tenant_id, conversation_id}`. Citas **futuras y `confirmada`s** del
  cliente de la conversación, de la más cercana a la más lejana (tope 10 +
  `hay_mas`), cada una con `reserva_id`, `servicio_id`, `proveedor_id`,
  `fecha_hora_inicio` (zona del negocio), nombres, `antelacion_minima_horas` y
  `cancelacion_seria_tardia`. Es lo único que le da al agente un `reserva_id` sin que
  el cliente lo tenga (el historial **no** lo incluye, a propósito). Mismo
  criterio de cliente y mismo 404 uniforme que el historial (conversación ajena,
  inexistente, de widget o con id inválido).
- **`reagendar_reserva`** → `POST /api/eventos/calendario/reservas/reagendar`,
  cuerpo `{tenant_id, conversation_id, reserva_id, hora_inicio, idempotency_key}`
  (la herramienta recibe `fecha` + `hora` y "Prepara HTTP POST" los convierte).
  `services/calendario.py::reagendar_reserva` hace **en una sola transacción**:
  bloquea la cita (`FOR UPDATE`), la cancela como cancelación **del cliente**
  y crea otra con el mismo servicio, proveedor, cliente y notas. Si el horario nuevo
  no cabe (ocupado, fuera de jornada, proveedor o servicio inactivos), no cambia
  nada y la respuesta es `200 {reagendada: false, motivo_rechazo}`.
- **Qué se decidió**: se mantienen servicio y proveedor (solo cambia la hora);
  fuera de la antelación del servicio **se acepta**, pero la anterior queda con
  `cancelacion_tardia=true` (cuenta en el reporte de plataforma y avisa al negocio,
  igual que cancelar); una cita que no está `confirmada` o ya empezó da 409
  `{codigo: reserva_no_confirmada | cita_ya_iniciada}` (mismos códigos que
  cancelar). Mover la cita a un horario que se solapa con la propia funciona, porque
  la anterior se cancela primero dentro de la transacción.
- **La cita debe ser del cliente de la conversación**: se compara
  `reservas.user_id` con el de `conversation_id`; la ajena y la inexistente dan el
  mismo 404 (el modelo no puede mover la cita de otro cliente aunque conozca su id).
  Esa es la razón de que `reagendar_reserva` dependa del `conversation_id` real.
- **Idempotencia**: `idempotency_key` se guarda en la cita **nueva**; un reintento
  con la misma clave devuelve `duplicado=true` con esa cita y **no** cancela otra vez.
- **Auditoría y avisos**: la anterior registra `cancelada` (datos con
  `reagendada_a`, y `antelacion_aplicada_horas`/`segundos_restantes` si fue tardía),
  la nueva `creada` (con `reagendada_de`); sin migración. El negocio recibe las
  alertas ya existentes `reserva_cancelada` ("...porque el cliente la reagendó") y
  `reserva_creada`, y la del proveedor si tiene cuenta.
- **Compuerta**: las dos herramientas comparten `HISTORIAL_CITAS_N8N_PUBLICADO` con
  el historial (`herramientas_calendario.CON_COMPUERTA`); con `false` quedan creadas
  pero **desactivadas**. Mientras la compuerta esté cerrada el agente solo tiene
  `cancelar_reserva`, y la regla de respaldo del prompt le pide **no** agendar encima
  de la anterior: cancelar primero (si tiene el `reserva_id`) o escalar a una persona.
- **n8n** (`entrada-canal-universal`, nodo "Arma prompt"): párrafo "RE-AGENDAR UNA
  CITA": usar `consultar_citas_proximas`, preguntar cuál si hay varias, conservar
  servicio y proveedor, **avisar que se cancelará la cita actual** (y que quedará
  como tardía si `cancelacion_seria_tardia`), pedir confirmación explícita y solo
  entonces `reagendar_reserva`. Si el horario ya no está libre, la original sigue
  vigente. Publicado el 2026-10-12.
- **Límite conocido**: como en R32 de la regla de cancelación, `_encender` no pisa
  descripciones editadas; los negocios existentes reciben las dos filas nuevas al
  arrancar (`sincronizar_todos`), con la descripción de este módulo.

Tests: `tests/test_reagendar_citas.py` (atomicidad, tardía, idempotencia, cita ajena,
endpoints) y `test_herramientas_calendario.py` (las nueve filas y la compuerta).

### Limpiar el contexto del agente (spec 003, `sql/47_conversacion_contexto_cortes.sql`)

Cuando el agente responde mal, el dueño pulsa "Limpiar contexto del agente" en la
conversación y el agente deja de usar lo anterior. **No se borra ni se modifica
ningún mensaje**: el portal sigue mostrando todo el historial. Lo único que se hace
es registrar un **corte** (una fila nueva en `conversacion_contexto_cortes`); n8n lee
solo los mensajes posteriores al último corte de la conversación. No cambia el
estado, la asignación ni la IA de la conversación, no genera alertas y no escribe en
`gerencia_auditoria` (su bitácora es la propia tabla). La lógica vive en
`services/contexto_conversacion.py` (`puede_limpiar_contexto` pura;
`conversacion_de_cliente`, `registrar_corte` y `ultimo_corte` con SQL crudo y
`tenant_id` en el WHERE).

- **Endpoint**: `POST /api/conversaciones/{conversacion_id}/limpiar-contexto` → 201
  `{conversacion_id, contexto_limpiado_en}`. Códigos, **en este orden** (responde la
  primera condición que falla):

  | Código | Cuándo |
  |---|---|
  | 403 | No es dueño con sesión propia: member, vendedor, proveedor, superadmin o "ver como" (aunque tenga escritura concedida). No mira el id ni busca la conversación. |
  | 404 | Conversación inexistente, de otro negocio o de canal que no es `whatsapp`/`instagram`/`facebook` (`Conversación no encontrada`, idéntico en los casos). |
  | 409 | `CONTEXTO_CONVERSACION_N8N_PUBLICADO` en `false`: `Limpiar el contexto del agente todavía no está disponible.` |
  | 402 | El plan no incluye el agente (vencido, cancelado, sin plan) o cuenta suspendida; mismo cuerpo que `volver-a-ia`. |
  | 422 | `conversacion_id` sin forma de UUID. |

  `conversacion_id` se declara `str` y no `UUID` a propósito: con `UUID` FastAPI
  respondería 422 antes de ejecutar la función y un id mal formado ganaría al 409 y
  al 402. Así, con el flag apagado un id malformado da 409, con el flag activo y plan
  sin agente da 402, y solo con todo en regla da 422.
- **Guarda `owner_sin_impersonar`** (`deps.py`): acepta solo el rol `owner` y rechaza
  la sesión de "ver como". No sirve `gerencia_actual`, porque admite también a
  `superadmin`; ni `herramientas._solo_owner`, que compara el rol pero no mira la
  impersonación. No declara parámetros de ruta, así que su 403 no depende del id y no
  revela si la conversación existe. Vendedor y proveedor quedan cerrados por ella: el
  endpoint **no** se añade a `permitir_a_roles_acotados` ni a `permitir_a_proveedor`.
  Un intento de "ver como" con escritura concedida lo registra como
  `impersonacion_escritura` la dependencia existente (`_validar_impersonacion`) antes
  de este 403; no se añade otra fila.
- **Router propio sin dependencias**: el endpoint cuelga de `router_contexto`
  (`routers/conversaciones.py`, incluido en `main.py` con prefijo `/api`), no de
  `router`. Este último tiene `requiere_herramienta("agente", lectura_sin_plan=True)`
  a nivel de router, que se evalúa antes que las del endpoint y respondería 402 a un
  member o a un superadmin con plan vencido antes de que la guarda dé el 403. El plan
  se comprueba dentro del endpoint con `exigir_herramienta(tenant_id, "agente")`
  (el mismo 402 que `volver-a-ia`), después del 409. Está cubierto por un test de
  dependencias del router.
- **Detalle**: `GET /api/conversaciones/{id}` devuelve `contexto_limpiado_en` (el
  `creado_en` del último corte o `null`) y `puede_limpiar_contexto`, que es `true`
  solo si a la vez: flag activo, dueño con sesión propia, canal de cliente y plan con
  agente. El portal muestra el botón solo con ese dato. El detalle nunca lanza 402
  (con plan vencido sigue en lectura y el dato es `false`). La pantalla de gerencia
  no monta el botón.
- **Tabla `conversacion_contexto_cortes`**: `id`, `tenant_id` (CASCADE),
  `conversation_id` (CASCADE), `creado_en TIMESTAMPTZ NOT NULL DEFAULT now()` (el
  instante lo fija la BD, no el navegador ni el servidor de aplicación) y
  `realizado_por` (`portal_users`, `ON DELETE SET NULL`; no se guarda el correo).
  Índice `(conversation_id, creado_en DESC)`. Cada pulsación inserta una fila y los
  cortes anteriores se conservan; vale el más reciente. Idempotente (`IF NOT EXISTS`).
- **Compuerta `CONTEXTO_CONVERSACION_N8N_PUBLICADO`** (`false` por defecto; ver
  `.env.example`; mismo parseo que `HISTORIAL_CITAS_N8N_PUBLICADO`). Sin el cambio de
  lectura en n8n, el botón prometería un efecto que no ocurre. Con `false`: el POST
  responde 409, `puede_limpiar_contexto` es `false` y el portal no muestra el botón.
  **Los cortes ya registrados siguen vigentes**: apagar el flag solo oculta el botón y
  bloquea cortes nuevos; n8n los respeta mientras lea la tabla. Cambiar la variable
  exige reiniciar el backend.
- **Cambio requerido en n8n** (fuera de este repo; flujo `entrada-canal-universal`,
  nodo "Carga historial"): hoy lee `SELECT role, content, created_at FROM messages
  WHERE conversation_id = $1 AND id NOT IN (...) ORDER BY created_at DESC LIMIT $2`
  (`$2` = `history_window`, 30 por defecto). Hay que leer el último corte
  (`SELECT creado_en FROM conversacion_contexto_cortes WHERE conversation_id = $1
  ORDER BY creado_en DESC LIMIT 1`) y añadir al `WHERE` (**antes del `LIMIT`**, o el
  tope se llenaría con mensajes viejos) `AND created_at > (<corte.creado_en> AT TIME
  ZONE '<huso de agente_db>')`. Motivo: `messages.created_at` es `TIMESTAMP` sin
  huso, con la hora de reloj de la sesión que lo insertó, mientras el corte es un
  instante absoluto; hay que convertirlo al huso de la base. El huso es el valor de
  `SHOW timezone` de `agente_db`, en formato IANA (no una abreviatura, para que el
  horario de verano se resuelva bien); no se fija en el repo. Si la tabla no existe
  (migración 47 sin aplicar) o la consulta falla, n8n trata el caso como "sin corte",
  deja un aviso en la ejecución y responde como hoy.
- **Orden de despliegue** (no alterar):
  1. Aplicar `python aplicar_sql.py sql/47_conversacion_contexto_cortes.sql`
     (`--revisar` la lista como pendiente; aplicarla dos veces no da error).
  2. Desplegar backend y portal con la variable en `false`.
  3. En n8n, publicar el cambio de lectura de "Carga historial" (tolerando tabla
     ausente y fallo de consulta). Un cambio solo en borrador no cuenta.
  4. Prueba manual (criterio A9): `SHOW timezone` igual en la conexión de n8n y en la
     del backend, y `NOW() AT TIME ZONE '<huso>' - created_at` de un mensaje de prueba
     de segundos (si da horas, no se publica); tras un corte en una conversación real,
     el historial de la ejecución de n8n no contiene mensajes con `created_at`
     anterior o igual al corte; con la tabla ausente o la consulta fallando, la
     respuesta sale igual y queda el aviso. Guardar la ejecución.
  5. Solo con la prueba superada: `CONTEXTO_CONVERSACION_N8N_PUBLICADO=true` y
     reiniciar el backend.
- **Advertencia**: no poner la variable en `true` antes de superar el paso 4 (A9). Si
  el huso no coincide, el corte no surte efecto y el dueño creería haber corregido al
  agente cuando no es así. Mientras tanto el sistema no registra cortes ni muestra el
  botón.
- **Casos aceptados**: un mensaje que el cliente envía justo mientras el dueño
  confirma, o una respuesta que n8n ya armó con el contexto viejo, salen con ese
  contexto (el diálogo del portal lo avisa); dos pulsaciones dejan dos filas. No hay
  forma de deshacer un corte: el siguiente solo mueve el instante hacia adelante. Una
  conversación cerrada que el cliente reabre es una conversación nueva en n8n y no
  hereda el corte. Quedan fuera el widget web, "ver como" y la acción masiva.

Tests: `tests/test_contexto_conversacion.py` (flag, migración, guarda, lógica pura,
servicio con BD, precedencia de códigos del endpoint y campos del detalle).

### Avisos de cita y notificaciones por correo (spec 004, `sql/48` a `sql/51`)

> **Reemplazo parcial (spec 007).** El envío de avisos de cita ya **no sale por la cuenta global**: cada negocio usa
> sus propias plantillas desde su número principal activo (ver "Avisos de cita por tenant"). De este apartado siguen
> vigentes el correo espejo, el registro `avisos_cita`, las reglas de teléfono y el contexto del agente. Las plantillas
> globales y la respuesta fija de la cuenta global quedan como código sin uso para avisos (spec 007, A4).

Tres cosas independientes, todas con la bandera apagada o inocuas por defecto.

**Correo espejo.** Cada alerta que `realtime.py` emite por Socket.IO sale también por
correo (SMTP, `services/correo_espejo.py` + `correo.enviar_notificacion_negocio`, con
escape HTML y versión de texto) a las cuentas activas que la recibirían en el panel.
Lo cubren también `sin_actividad` y `nuevo_lead`; solo quedan fuera `cuota_excedida`,
`perfil_incompleto` y `solicitud_escritura` (`TIPOS_SIN_ESPEJO`). Si una misma persona
recibe la transferida y la asignada de una conversación, llega un solo correo
(`excluir_correo_a`). Un SMTP caído no lanza ni quita la alerta del panel.

**Plantillas globales (sin uso para avisos desde spec 007).** `notificacion_cancelacion` y `recordatorio_cita` (idioma
`es_MX`) son plantillas de Meta únicas para todos los negocios, creadas **a mano** en
Meta Business Manager (NeuroAPI no tiene API de plantillas). Su estado vive en
`plantillas_whatsapp_globales` (`no_solicitada`, `pendiente_aprobacion`, `aprobada`,
`rechazada`, `pausada`) y solo la plataforma lo gestiona:
`GET /api/gerencia/plantillas-whatsapp`, `PATCH /{nombre}` y `POST /{nombre}/reintentar`
(solo `gerencia_users`; cada cambio queda en `gerencia_auditoria`). El estado también
cambia solo: por el evento `message_template_status_update` que NeuroAPI manda al
webhook de canales (`APPROVED`, `REJECTED`/`DISABLED`, `PAUSED`, `PENDING`) o por un
error de envío con código de Meta 132015 (`pausada`) o 132016 (`rechazada`).

**Avisos al cliente por WhatsApp** (`services/avisos_cita.py`, reglas puras en
`services/avisos_cita_reglas.py`). Desde spec 007 salen por el **número principal activo del
propio negocio** con sus plantillas (`services/avisos_cita_envio.py`), no por la cuenta global.
- *Cancelación*: al cancelar una cita el owner, el superadmin o el proveedor (no el
  agente ni una reagenda), tarea en segundo plano tras el `commit`; plantilla
  `notificacion_cancelacion` con servicio y fecha `dd-mm-aaaa` en la zona del negocio.
- *Recordatorio*: `jobs/recordatorios_cita_background.py`, cada 5 min
  (`max_instances=1`, `coalesce=True`); avisa a las citas `confirmada` que entran en la
  ventana de 5 h antes del inicio, con la plantilla de recordatorio del negocio (negocio y fecha). Una cita
  creada ya dentro de la ventana recibe el aviso en la siguiente pasada.
- *Teléfono*: el de la reserva, normalizado a E.164 (10 dígitos = México `+52`, `00` =
  `+`, E.164 válido tal cual; vacío = `sin_telefono`, otro = `telefono_invalido`). El
  widget web queda `canal_no_soportado`; sin conversación se envía igual.
- *Registro*: `avisos_cita`, única por `(reserva_id, tipo, hora_inicio)`; `en_curso` →
  proveedor → `enviado`/`omitido`/`fallido` con motivo. Un `en_curso` de una ejecución
  muerta pasa a `fallido resultado_desconocido`. `GET
  /api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/avisos` (owner y superadmin) los
  lista. Una cancelación que no se pudo avisar genera la alerta personal
  `aviso_cita_no_enviado` para cada owner y superadmin activo (no member).
- *Contexto del agente*: un aviso `enviado` con conversación deja una fila en
  `contexto_agente_eventos` (`cita_cancelada_por_negocio` / `recordatorio_cita_enviado`)
  aunque el agente esté apagado.
- **Regla fija: los avisos NUNCA usan texto libre**, solo plantilla aprobada del negocio
  (`avisos_cita_envio.enviar_plantilla_tenant`). `enviar_texto_global` existe solo para la
  respuesta fija de abajo.

**Banderas** (`config.py`, todas apagadas o vacías por defecto):

| Variable | Efecto |
|---|---|
| `AVISOS_CITA_WHATSAPP_ACTIVOS` | Interruptor maestro de plataforma (spec 007). `false`: no se envía ningún aviso. Al encenderla, las citas que **ya están** en ventana SÍ reciben su recordatorio (RF-10). Ya **no** exige `AVISOS_CUENTA_GLOBAL_TENANT_ID`. |
| `AVISOS_CUENTA_GLOBAL_TENANT_ID` | Tenant de la línea global (`a383f05c-7028-4d5f-8c5d-30232a80faf3`). Solo la respuesta fija y la línea de avisos de conversación lo usan. |
| `AVISOS_CUENTA_GLOBAL_WABA_ID` | WABA de Meta de la cuenta global (spec 007). El handler global de `message_template_status_update` **solo aplica** los eventos de ese WABA (o de un `meta_template_id` global) a `plantillas_whatsapp_globales`; **si está vacía no aplica ningún evento** a las globales, para que una plantilla de un negocio con el mismo nombre no cambie su estado. |
| `AVISOS_CUENTA_GLOBAL_RESPUESTA_ACTIVA` | Enciende la respuesta fija (siguiente apartado). Sigue exigiendo `AVISOS_CUENTA_GLOBAL_TENANT_ID`. |
| `AVISOS_CITA_REVISION_MINUTOS`, `AVISOS_EJECUCION_VIVA_MINUTOS` | 5 y 5. |

**Respuesta fija de la cuenta global** (`services/respuesta_cuenta_global.py`, gancho en
`canales_neuroapi_webhook._reenviar_mensaje`, antes de `tenant_por_numero`). La línea es
también el agente demo de su tenant, así que un cliente de otro negocio que responda a
un aviso entraría a ese agente. Con la bandera encendida, el mensaje se **desvía** (no se
reenvía a n8n, no crea evento de contexto) solo si: (1) el receptor es la línea global;
(2) el remitente (`messages[].from`; se tratan igual `521…` y `52…` en México) tiene un
aviso `enviado` en las últimas 24 h (`avisos_cita.telefono_destino`); (3) no es ya un
`users` del tenant de la línea (`users.whatsapp_id`, solo lectura). Entonces recibe
`Este mensaje es con fines informativos para mayor informacion comunicate al numero donde
realizaste tu cita` (constante `TEXTO_RESPUESTA_CUENTA_GLOBAL`, ortografía literal) como
máximo una vez cada 24 h (`cuenta_global_respuestas`, `puede_responder`). Si no se
cumplen las tres, o falla cualquier consulta del gancho, el flujo es exactamente el de
siempre. Si el envío de la respuesta falla, el mensaje igualmente no llega al agente y
no se registra la respuesta (el siguiente mensaje reintenta).

**Riesgos conocidos.** Unicidad de los avisos probada solo con fakes (no hay BD de
pruebas); una cancelación entre la respuesta del cliente y el registro puede avisar de
una cita ya cancelada; el correo espejo no tiene métrica ni límite diario; un número de
10 dígitos se toma como de México; quien no respondió dentro de 24 h de un aviso y
escribe a la línea cae en el agente del tenant (como hoy); la respuesta fija no se da a
quien nunca recibió un aviso. Lo más limpio sería una línea dedicada sin agente.

**Activación (en este orden).** (Desde spec 007 los pasos 2 y 3 ya no aplican a los avisos de cita; ver "Avisos de cita por tenant".)
1. Aplicar `sql/48` a `sql/51` y `sql/65` (`python aplicar_sql.py --revisar`).
2. `AVISOS_CUENTA_GLOBAL_TENANT_ID` solo hace falta para la respuesta fija.
3. Cada negocio crea y aprueba sus plantillas desde Calendario (reemplaza las plantillas globales a mano).
4. Con `AVISOS_CITA_WHATSAPP_ACTIVOS=false` no se envía ni se registra nada; al encenderla, las citas ya en ventana reciben su recordatorio.
5. Probar cancelar una cita con teléfono válido y comprobar el correo espejo y el aviso a
   gerencia (owner sí, member no).
6. Con `AVISOS_CUENTA_GLOBAL_RESPUESTA_ACTIVA=true`, responder a un aviso desde el teléfono
   de prueba: llega el texto fijo, no aparece conversación nueva en el tenant de la línea
   y un segundo mensaje dentro de 24 h no recibe nada.

**Pendientes del usuario.**
- `[PENDIENTE usuario: webhook NeuroAPI cuenta global]` verificar en la práctica que los
  mensajes entrantes llegan con `entry[].changes[]` (`messages[].from` y
  `metadata.phone_number_id`) y si `POST /messaging/webhooks/api` reemplaza el destino
  de las demás líneas (comparten API key). Mientras tanto
  `AVISOS_CUENTA_GLOBAL_RESPUESTA_ACTIVA` queda apagada.
- `[PENDIENTE usuario: ¿la línea del tenant a383... atiende también a clientes propios?]`
- Publicar el cambio de lectura en n8n (lee `contexto_agente_eventos` por
  `conversation_id`, tolerante a fallo) para que el agente vea los avisos.
- (Reemplazado por spec 007: ya no se crean plantillas globales a mano; las crea cada negocio.)

Tests: `test_correo_espejo.py`, `test_cuenta_global.py`, `test_avisos_cita_reglas.py`,
`test_avisos_cita_servicio.py`, `test_avisos_cita_api.py`, `test_avisos_cita_jobs.py`,
`test_plantillas_whatsapp*.py`, `test_avisos_sql.py`.

### Avisos de cita por tenant (spec 007, `sql/65`)

Cada negocio usa **sus propias plantillas** de WhatsApp (UTILITY, `es_MX`, solo cuerpo) para el recordatorio y la
cancelación de citas, enviadas desde su **número principal activo**. La cuenta global se retiró de los avisos de cita
y no hay fallback: sin plantilla propia `APPROVED` no se envía. Specs: `specs/007-plantillas-avisos-cita-tenant/`.

- **Modelo.** `avisos_cita_config` (una fila por tenant; fila ausente = oferta `pendiente`, ambas funciones apagadas,
  nombres base): `oferta` (`pendiente|aceptada|rechazada`), `recordatorio_activo`, `cancelacion_activo`,
  `nombre_plantilla_recordatorio` / `nombre_plantilla_cancelacion` (nombre **vigente**), `decidido_en/por`. Las plantillas
  viven en `plantillas_whatsapp_tenant` y se enlazan por nombre, sin FK. Textos y variables en
  `services/avisos_cita_plantillas.py` (única fuente; el front los recibe en `GET /config`). Si una plantilla cae se recrea
  con sufijo: `siguiente_nombre(base, usados)` da `base`, `base_v2`, `base_v3`... (hasta `_v9`).
- **Emisor (D6).** El principal activo (`agentes_canal.es_principal`, `estado='activo'`, config completa, nunca la línea de
  avisos) **y** la credencial del canal (`get_channel_credentials`) con el mismo `phone_number_id`. Si no coinciden:
  409 `sin_numero_emisor` al aceptar/activar, y `omitido sin_numero_emisor` al enviar. `numero_emisor_ok` lo refleja.
- **Oferta.** El owner/superadmin con `tenant_servicios.calendario_activo` y oferta `pendiente` ve un modal (nunca en sesión
  "ver como"; `/auth/yo` devuelve `oferta_avisos_cita_pendiente`, un solo JOIN). `aceptar` crea/adopta las 2 plantillas (con
  `pg_advisory_xact_lock`) y activa ambas; `rechazar` apaga ambas (no borra plantillas), devuelve `mensaje` y deja alerta.
  Reabrir una rechazada es `aceptar`. Adopción: una plantilla existente `es_MX`, UTILITY/MARKETING, con el nº de variables
  correcto y `APPROVED`/`PENDING` se reutiliza; si no, se crea con sufijo.
- **Endpoints** (`routers/avisos_cita_config.py`; `/api/avisos-cita` está en `RUTAS_SENSIBLES`): `GET /config` (owner,
  superadmin, member; sincroniza con NeuroAPI antes de responder y, si falla la red, responde con caché),
  `POST /oferta {accion}` y `PATCH /config {recordatorio_activo?, cancelacion_activo?}` (`gerencia_actual`; `{}` -> 422).
  `vendedor`/`proveedor` 403. Errores: 409 `calendario_inactivo`, `sin_numero_emisor`, `plantilla_existente_no_utilizable`
  (solo si se agota el sufijo); los de NeuroAPI pasan por `a_http`.
- **Reacción al estado de Meta** (`plantillas_tenant.reaccion_estado`, único punto para webhook, sincronización y DELETE):
  plantilla `REJECTED/PAUSED/DISABLED` o fila quitada con la función activa -> `activa=false` + **una** alerta
  `avisos_cita_plantilla_inutilizable` (si la función ya estaba apagada, sin alerta). `PENDING` ("esperando aprobación")
  nunca alerta ni registra. `marcar_estado_por_error` aplica lo mismo cuando un envío devuelve `plantilla_no_disponible`.
- **D7: función apagada, sin registro.** El job de recordatorios filtra en SQL por función activa y `calendario_activo`; lo
  apagado ni es candidato ni deja fila `omitido`. Al activar después, las citas ya en ventana SÍ reciben recordatorio
  (RF-10). Las cancelaciones con la función apagada u oferta pendiente no se avisan ni se registran (sin reenvío retroactivo).
  Sin emisor: `omitido sin_numero_emisor`; plantilla ausente/no aprobada: `omitido plantilla_no_aprobada`.
- **Alertas nuevas** (personales al owner, con correo espejo automático): `avisos_cita_oferta_rechazada`,
  `avisos_cita_plantilla_inutilizable`, y `aviso_cita_no_enviado` reescrito para el tenant (una variante por motivo:
  `plantilla_no_aprobada`, `plantilla_no_disponible`, `sin_numero_emisor`, `error_proveedor`; solo en cancelaciones).
  `sql/65` ensancha `alertas.tipo` a `VARCHAR(60)` y rehace `alertas_tipo_check` desde la lista de `sql/60`; también amplía
  el CHECK de `avisos_cita.motivo` con `sin_numero_emisor`.
- **Handler global de Meta.** `canales_neuroapi_webhook` ya no aplica a las plantillas globales un evento de un negocio con
  el mismo nombre: `plantillas_whatsapp.evento_es_de_cuenta_global` exige el WABA de `AVISOS_CUENTA_GLOBAL_WABA_ID` (vacía =
  no aplica ninguno).
- **Aislamiento bidireccional (T10, RF-11/D9).** El handler de negocio (`plantillas_tenant.aplicar_evento_meta`) ignora las `entry` del WABA de `AVISOS_CUENTA_GLOBAL_WABA_ID` (`entradas_de_negocio`, por `entry`) y `elegir_candidata` ya no da por dueña a la candidata única si su `waba_id`/`meta_template_id` guardados contradicen al evento (se descarta con `warning`; NULL no contradice). Con `AVISOS_CUENTA_GLOBAL_WABA_ID` vacío no se descarta nada por «ser global», rige solo la contradicción y cada evento aplicado deja un `warning`. **Riesgo residual de D9:** con `AVISOS_CUENTA_GLOBAL_WABA_ID` vacío, una plantilla de negocio cuyo `meta_template_id` y `waba_id` aún no se han aprendido puede aceptar un evento global homónimo si es la única candidata, y entonces los ids del evento global quedan aprendidos en esa fila. El WABA solo se aprende por eventos, y el `meta_template_id` solo si Meta lo devolvió al crear la plantilla. Mitigación: configura `AVISOS_CUENTA_GLOBAL_WABA_ID` en producción.
- **Calendario y roles.** Si el negocio sale de calendario las plantillas y la config se conservan; `GET /config` da 200 con
  `calendario_activo=false` y las escrituras 409. Frontend: `components/modal-oferta-avisos-cita.tsx` (una vez por pestaña) y
  `components/avisos-cita-panel.tsx` en `/calendario` (`member` solo lectura).

Tests: `test_avisos_cita_tenant.py`, `test_avisos_cita_cancelacion_tenant.py`, `test_avisos_cita_estado_meta.py`,
`test_avisos_cita_jobs.py`, `test_plantillas_whatsapp_webhook.py`.

### Control de acceso por plan

`services/acceso_plan.py` decide qué **herramientas del portal** puede usar
un negocio. No reemplaza a `acceso_pagos.py`: ese sigue decidiendo si el
agente de n8n contesta (modelo híbrido, con créditos alcanza) y no cambió.

| Herramienta | APIs | starter | pro | enterprise |
|---|---|---|---|---|
| `agente` | `/agente`, `/conversaciones`, `/canales` | ✓ | ✓ | ✓ |
| `vendedores` | `/vendedores`, `/pipeline`, `/tenants/{id}/pipeline…`, `pipeline-config` | ✓ | ✓ | ✓ |
| `herramientas` | `/herramientas` | — | ✓ | ✓ |
| `crm_campo` | `/clientes`, `/visitas`, `/tareas`, `/reportes` | — | ✓ | ✓ |
| `calendario` | `/tenants/{id}/calendario/…` | — | ✓ | ✓ |

La matriz vive en la tabla `planes` y se edita desde `/gerencia/planes`.

| Estado de la cuenta | Qué puede usar |
|---|---|
| suscripción `activa` | lo que incluya su plan |
| `pausada` / `cancelada` / nunca contrató | nada — los créditos **no** dan herramientas |
| `tenant_estado_plataforma = 'prueba'` | todo (piloto, demos, desarrollo) |
| `suspendido` / `baja` | nada, aunque el plan esté al día |

Un tenant que nunca contrató **queda bloqueado en el portal** (el agente de
n8n sigue contestando igual). Para un negocio de demo o desarrollo, ponerlo
en `prueba` desde `/gerencia/tenants/{id}/estado`.

Todo rechazo es **402** con `detail` estructurado — `codigo`
(`plan_insuficiente` | `plan_requerido` | `cuenta_suspendida`),
`herramienta`, `estado`, `plan_actual`, `planes_que_la_incluyen`, `mensaje`.
No 403, que ya significa "tu rol no puede". Dónde se aplica:

- `deps.requiere_herramienta(...)` como dependency de router (agente,
  conversaciones, canales, herramientas, CRM de campo). `conversaciones`
  lleva `lectura_sin_plan=True`: con el plan vencido los GET pasan (el
  historial es del negocio); contestar o devolver a la IA, no.
- `_exigir_modulo` (vendedores) y `verificar_calendario_activo`: el **409**
  de módulo apagado sigue yendo antes que el 402. Las rutas de calendario
  de n8n (`/eventos/calendario/*`) usan `desde_chat=True` y conservan el gate
  de pagos de siempre.
- `PATCH /tenants/{id}/servicios`: el dueño no puede **encender** un módulo
  que su plan no incluye (apagarlo siempre puede). El de plataforma
  (`/gerencia/tenants/{id}/servicios`) no pasa por acá.

`GET /pagos/acceso` (nunca 402) devuelve el estado, las herramientas
permitidas y la matriz de los planes contratables; con eso el portal pone el
candado en el menú y la vista de mejora de plan.

Los tests de módulos que no prueban el cobro (calendario, reportes, canales)
activan el fixture `plan_enterprise` de `conftest.py`.

**Ojo con `SQL_AGENTE_OPERANDO`** (routers/gerencia.py): es el gate de
pagos escrito en SQL para resolver la página entera de una vez en lugar de
una consulta por fila. Duplica la regla de `services/acceso_pagos.py`, que
sigue siendo la fuente de verdad. `tests/test_gerencia.py` compara las dos
implementaciones sobre una matriz de casos justamente para que no se
separen en silencio; si cambias una, cambia la otra.

### Límites del agente por plan (`sql/45_limites_agente_plan.sql`)

El plan decide qué **modelos** puede elegir el negocio para su agente y
cuántos **mensajes de historial** (`tenant_agent_config.history_window`, la
"ventana de contexto": se cuenta en mensajes, no en tokens) puede darle.
Vive en `planes` y se edita desde `/gerencia/planes`:

| Columna | Significado |
|---|---|
| `modelos_permitidos TEXT[]` | `NULL` = sin límite. El **primero** (orden del catálogo, barato → caro) es el modelo por defecto del plan |
| `historial_maximo INT` | `NULL` = sin límite; si hay valor, 5 a 200 |

De fábrica: `starter` = Haiku y 50; `pro` = Haiku + Sonnet y 100; `enterprise` =
`NULL`/`NULL` (todos los modelos, 200, y entran solos los que se agreguen). El
seed solo corre si nadie tocó el plan (`updated_at = created_at`).

`services/limites_agente.py` tiene el catálogo de modelos (`MODELOS_AGENTE`,
única lista: se suma un modelo ahí **y** en n8n) y la regla pura. Dos capas:

1. **API** — `PUT /api/agente` responde **402** con `codigo`
   `modelo_no_incluido_en_plan` o `historial_excede_plan` (más `plan_actual`,
   `modelos_permitidos` / `historial_maximo`). Sin modelo elegido, y con el
   plan limitando modelos, guarda el por defecto del plan (`model = NULL`
   dejaría a n8n usar el suyo, que puede no estar en el plan).
2. **Triggers** — el agente de la plataforma lee la configuración directo de
   la base (`agentes_canal` por número y `tenant_agent_config` para Instagram,
   Facebook y el widget), sin pasar por la API, así que las filas tienen que
   *estar* siempre dentro del plan.
   `recortar_agente_a_plan(tenant)` baja el modelo (al por defecto del plan, si
   el actual no cabe o es `NULL`) y el historial (`LEAST`), nunca sube nada, y
   la disparan: alta o cambio de plan en `tenant_subscriptions`, un cambio de
   `tenant_estado_plataforma.estado` (salir de `prueba`) y bajar los topes en
   `planes`. Así cubre Stripe, piloto, onboarding, el panel y SQL a mano sin
   acordarse de llamarlo en cada uno.

**Qué plan aplica (spec 005a, sustituye a "el piloto y un negocio sin
suscripción no tienen topes").** Se evalúa en este orden y gana el primero:

| Situación | Límites de modelo/historial, lista de herramientas y cupo de números |
|---|---|
| Prueba vigente (cualquier suscripción de origen `prueba` que no terminó, incluida la de 14 días del cuestionario y el piloto sin fecha de fin; una prueba pausada o cancelada no es vigente) | Los del plan asociado a la prueba; si no tiene plan, sin límites y catálogo base completo. **Nunca tope de números** |
| Suscripción vigente (`activa` de pago, y el periodo pagado de una cancelada hasta su fin) | Los de su plan, aunque el plan esté desactivado en el catálogo |
| Sin suscripción vigente (vencida, pausada, prueba terminada sin pagar, o nunca contrató) | Los del **plan más bajo activo**: menor `precio_monthly`; empate: menor cupo (sin tope cuenta como mayor); luego menor id |
| Ningún plan activo | Sin topes ni límites y catálogo base completo; queda en `gerencia_auditoria` (`plan_sin_tope_por_falta_de_plan_activo`) |

Antes de la spec 005a un negocio sin suscripción y el piloto quedaban sin
topes; ahora el primero se recorta al plan más bajo activo, y una prueba con
plan asociado usa los límites de ese plan. Recortar nunca sube un valor; un
modelo retirado pasa al modelo por defecto del plan y eso no deja el número
incompleto. Lo implementa `limites_efectivos` (SQL) con paridad probada contra
el núcleo puro `services/numeros_reglas.py`.

`GET /gerencia/planes/modelos-agente` da el catálogo y el rango al formulario
del panel. En `PATCH /gerencia/planes/{nombre}`: `null` = no tocar,
`modelos_permitidos: []` / `historial_maximo: 0` = quitar el límite. Bajar un
tope recorta de inmediato a los negocios que ya tienen el plan, y
`gerencia_auditoria` (`plan_actualizado`) guarda `negocios_recortados`.

Aplicar la migración **recorta de una vez** a los negocios existentes con
`starter`/`pro` que tuvieran más de lo que ahora da su plan.

Tests: `tests/test_limites_agente.py` y el portal en
`app/(privado)/agente/__tests__/limites-plan.test.tsx`.

### Probar el agente (`sql/68_agente_pruebas_uso.sql`)

`POST /api/agente/probar[?numero_id=<uuid>]` deja al dueño chatear con su agente desde `/agente` sin escribirle a un
cliente real. El body lleva el **borrador** del formulario (`agent_name`, `system_prompt`, `model`, `temperature`,
`history_window`) y la conversación de prueba (`mensajes`, máx. 40 turnos de 4000 caracteres; el último debe ser del
usuario). Responde `{texto, modelo, transferida, herramientas, tokens_*, restantes_hoy}`.

- **Mismo agente, modo simulación.** `services/agente_tester.py` arma un `ContextoMensaje` sintético y llama a
  `loop_agente.responder(simulacion=True, historial_previo=...)`: mismo prompt, mismas herramientas del número
  (`numero_id`; sin él, las de Instagram/Facebook/widget) y mismos parámetros que en producción. Sin `numero_id` ajeno:
  404 `numero_no_encontrado`; la línea de avisos de plataforma no se prueba (409).
- **Sin huella.** No lee ni escribe `messages`, no registra consumo ni `usage_events`, no cobra créditos, no transfiere.
  La conversación vive en el navegador. El `conversation_id` es un uuid que nunca existió, así que ninguna herramienta
  puede alcanzar datos de un cliente real.
- **Herramientas.** Corren de verdad solo `sql_lectura`, `google_sheets`, `google_docs` y `http_generico` con GET
  (`herramientas.se_ejecuta_en_prueba`, lista cerrada: un tipo nuevo se simula por defecto). Todo lo demás —el calendario
  entero, también sus consultas, que necesitan una conversación real—, `http_post_json` y `escalar_humano` devuelve un texto
  `[SIMULADO]`. La respuesta incluye qué herramientas pidió el modelo.
- **Cupo.** 20 mensajes por negocio y día (`LIMITE_DIARIO`, tabla `agente_pruebas_uso`, día en America/Mexico_City,
  UPSERT atómico). Los tokens los absorbe la plataforma. Agotado: 429 `cupo_pruebas_agente`. Si el modelo no responde
  (sin `ANTHROPIC_API_KEY`, API caída) es 503 y el mensaje se reintegra.
- **Quién.** Solo owner/superadmin (`gerencia_actual`). `/api/agente/probar` está en `RUTAS_SENSIBLES`: "ver como" no
  puede usarlo ni con concesión de escritura. Límites del plan: 400 modelo desconocido, 402 modelo/historial fuera del plan.

Tests: `tests/test_agente_tester.py`.

### Varios números de WhatsApp por negocio (spec 005a, `sql/56` a `sql/59`)

Estado: documentado en la tarea T0, antes del código. Hasta que se apliquen las
migraciones y las tareas T1 a T9 de `specs/005a-varios-numeros-whatsapp/tasks.md`,
el comportamiento real sigue siendo un número por negocio.

Un negocio vincula varios números de WhatsApp. Cada uno tiene su configuración
de agente (nombre, system prompt, modelo, temperatura, ventana de historial y
agente encendido o apagado) y su lista de herramientas. **La plataforma decide
por número si el agente responde** (principio 2.5): responde si el número está
activo, el agente encendido y la configuración completa (system prompt no vacío
ni solo espacios, y nombre de agente), con la configuración y las herramientas
de ese número y nunca las de otro.

| Concepto | Regla |
|---|---|
| Tabla | `agentes_canal`: una fila por número, estado `activo`/`pausado`/`desvinculado`, marcas `es_principal` y `es_linea_avisos`. `phone_number_id` vinculado en un solo negocio. Instagram, Facebook y el widget siguen en `tenant_agent_config` + `social_herramientas`, fuera del cupo |
| Cupo | `planes.max_numeros_whatsapp` (NULL = sin tope, `0` en el panel lo quita, entero >= 1 lo fija; negativo o no entero = 400 `cupo_no_valido`). Plazas = números activos, salvo la línea de avisos. Lo edita cualquier cuenta de `gerencia_users`, sea cual sea su cargo; la gerencia del negocio recibe 403 |
| Herramientas por plan | `plan_herramientas`, por familia: `calendario`, `sql_lectura`, `http`, `google_sheets`, `google_docs`. Los flags `herramientas_activo` y `calendario_activo` pasan a derivarse de la lista |
| Principal | Número activo con configuración completa que sirve de fuente de copia para los nuevos. Lo elige la gerencia del negocio; si no hay, pasa a serlo el activo completo más antiguo (desempate: menor id). Nunca es la línea de avisos ni uno incompleto |
| Pausa por cupo | Si los activos superan el cupo se pausan, sin borrar, los más recientes primero (el principal también). Reactivar es siempre manual y sujeto al cupo; subir el cupo no reactiva nada |
| Línea de avisos | La línea de `AVISOS_CUENTA_GLOBAL_TENANT_ID` (spec 004) es un número vinculado de ese negocio, marcado; no cuenta para el cupo ni puede ser principal. La gerencia del negocio no puede pausarla, desvincularla ni elegirla principal (403) |
| Mensajes que no se responden | Número pausado, con agente apagado o incompleto: el mensaje se guarda sin respuesta y, al volver a responder, no se contesta a los anteriores. Número sin vincular: no se atribuye a ningún negocio |
| Desvincular | Libera la plaza. Las conversaciones quedan ocultas, asignadas al owner que creó el negocio (o su relevo), legibles en solo lectura por owner y superadmin; nadie las responde. Se conservan 12 meses. Un número revinculado no recupera las anteriores |
| Bitácoras | Negocio: `numeros_bitacora` (motivo, número y autor). Plataforma: `gerencia_auditoria` (`cupo_cambiado`, `precio_del_plan_cambiado`, `herramientas_del_plan_cambiadas`, `plan_sin_tope_por_falta_de_plan_activo`, `alta_ajena_rechazada`, `altas_bloqueadas`) |

**Alta, en este orden (el primero que aplica decide):** 429
`demasiados_intentos` (5 altas ajenas rechazadas en 24 h, ventana deslizante;
con la hora de liberación, también en `Retry-After`) -> 402
`cupo_numeros_whatsapp` (cupo lleno, sea cual sea el número) -> 409
`numero_ya_vinculado` (propio, activo o pausado) -> 404 `numero_no_encontrado`
(vinculado a otro negocio; cuenta para el bloqueo) -> alta libre como activo.
El 404 de alta ajena revela que el número está tomado: riesgo residual aceptado
(D13 de la spec, tensión abierta con la constitución 3.4).

**Endpoints** (guarda: `negocio_actual` = owner, superadmin y member;
`gerencia_actual` = owner y superadmin):

| Ruta | Guarda | Respuestas |
|---|---|---|
| `GET /api/canales/whatsapp/numeros` | `negocio_actual` | 200 con `numeros[]`, `tope`, `plazas_usadas`, `plazas_libres`; 403 vendedor y proveedor |
| `POST /api/canales/whatsapp/conectar` (Kontesta) | `gerencia_actual` | 201; 429, 402, 409, 404 como el orden del alta |
| `POST /api/canales/whatsapp/neuroapi/iniciar` | `gerencia_actual` | Comprueba 429 y 402 antes de abrir la sesión; el resultado del webhook en `GET .../neuroapi/estado` |
| `POST /api/canales/whatsapp/numeros/{id}/pausar` | `gerencia_actual` | 200 (`pausa_manual`, sin aviso; se permite pausar el último); 403 línea de avisos; 404 |
| `POST /api/canales/whatsapp/numeros/{id}/reactivar` | `gerencia_actual` | 200; 402 `cupo_numeros_whatsapp`; 404 |
| `DELETE /api/canales/whatsapp/numeros/{id}` | `gerencia_actual` | 204; 403 línea de avisos; 404 |
| `PUT /api/canales/whatsapp/principal` | `gerencia_actual` | 200; 409 `numero_no_activo`; 409 `configuracion_incompleta`; 403 línea de avisos; 404 |
| `DELETE /api/canales/whatsapp` | `gerencia_actual` | 409 `usar_numeros` |
| `GET/PUT /api/agente/numeros/{id}` | lectura `negocio_actual`, escritura `gerencia_actual` | 400 `temperatura_fuera_de_rango` (0 y 1 se aceptan); 402 `modelo_no_incluido_en_plan` / `historial_excede_plan`; 404; member lee sin credenciales y no edita (403) |
| `GET/PUT /api/herramientas/numeros/{id}` y `/api/herramientas/social` | lectura `negocio_actual`, escritura `gerencia_actual` | 402 `herramienta_no_incluida_en_plan`; 404 |
| `GET /api/conversaciones/ocultas[/{id}]` | `gerencia_actual` | Solo lectura; 404 si no es del negocio |
| `POST /api/conversaciones/{id}/mensajes` | como hoy | Añade 409 `numero_desvinculado`, `numero_no_activo`, `configuracion_incompleta` (en ese orden; con agente apagado la respuesta humana se permite) |
| `POST/PATCH /api/gerencia/planes` | `gerencia_plataforma_actual` | Añade `max_numeros_whatsapp` y `herramientas_plan`; 400 `cupo_no_valido` |

Códigos 403: sin código propio para rol sin permiso o sesión de soporte sin
concesión; `linea_avisos_gestionada_por_plataforma` para la línea de avisos.
Precedencia: primero el permiso del rol y del tipo de acción (403), después la
pertenencia (404, idéntico para número ajeno, ajeno pausado e inexistente).

**Sesiones de soporte ("ver como").** Pueden leer la configuración de los
números sin concesión. Con concesión editan la configuración de agente, las
herramientas y la de Instagram y Facebook (con auditoría), pero no vinculan,
pausan, reactivan, desvinculan ni cambian el principal (`/api/canales` sigue en
`RUTAS_SENSIBLES`) ni editan credenciales de herramientas
(`RUTAS_CREDENCIALES_CERRADAS`, restricción nueva).

**Reevaluación del cupo (RF-11).** Siete casos: cambio de plan a menos cupo,
fin de suscripción por cancelación o vencimiento (al terminar el periodo
pagado; una suscripción o prueba pausada o cancelada, en el acto), prueba
terminada sin pago, cambio del cupo del plan contratado, recálculo continuo
cuando cambia el plan más bajo activo (para los negocios sin suscripción
vigente), prueba que pasa a pago (`pausa_por_pago_de_prueba`) y despliegue
(`pausa_por_despliegue`, solo si el plan más bajo activo tiene tope). Cada
pausa avisa una vez a owner y superadmin (portal y correo, agrupado), salvo la
pausa manual y la desvinculación pedida. Los negocios suspendidos o de baja no
se reevalúan hasta reactivarse. Un job (`jobs/numeros_background.py`) drena la
cola de reevaluación y aplica la retención de 12 meses.

**Al desplegar:** un número existente con configuración completa pasa a
principal sin cambios; uno sin configuración completa deja de responder y avisa;
los planes existentes arrancan sin tope; las pruebas con plan asociado pasan a
usar sus límites y su lista de herramientas.

Tests (cuando se implementen): `tests/test_numeros_reglas.py` (puro),
`test_numeros_sql.py`, `test_numeros_servicio.py`, `test_numeros_planes.py`,
`test_numeros_endpoints.py`, `test_agente_numeros.py`, `test_numeros_jobs.py`.

### Ver como el negocio (impersonación)

`POST /gerencia/tenants/{id}/impersonar` con `{"motivo": "..."}` devuelve un
access token para ver el portal como lo ve el dueño del negocio. Es la única
pieza del panel que da acceso a datos de un cliente con otra identidad, así
que cada garantía vive en un lugar concreto:

| Garantía | Dónde |
|---|---|
| Solo lectura por defecto: todo lo que no sea GET/HEAD/OPTIONS → 403, salvo que el dueño haya autorizado escribir (abajo) | `deps._validar_impersonacion`, dentro de `usuario_actual` (lo usan todos los endpoints; no hay lista de permitidos que olvidar) |
| Si sacan al gerente de `gerencia_users`, muere en la siguiente petición | la misma función relee la tabla |
| Desde el "ver como" no se entra a `/gerencia` | `es_gerencia_plataforma` forzado a `False` |
| Dura `IMPERSONACION_MINUTOS` (30) y no se estira | sin refresh token; `/auth/refresh` solo acepta `type: refresh` |
| No marca alertas leídas por el socket | `realtime.marcar_leida` ignora sids de solo lectura |
| Queda registrado con motivo | `gerencia_auditoria`, acción `impersonacion` |

#### Escritura autorizada por el dueño

Para corregir algo por el cliente, soporte pide permiso de edición (botón del
banner de "ver como"; solo cargos de `ACCESO_SOPORTE_CARGOS`, por defecto
`gerencia,developer,ti`, con motivo obligatorio). El dueño (owner/superadmin)
recibe una alerta personal `solicitud_escritura` y un correo, y responde desde
el portal o desde el enlace del correo (`/acceso-soporte#t=...`, token de un
solo uso; en BD solo el SHA-256):

| Endpoint | Quién |
|---|---|
| `GET /acceso-soporte/estado`, `POST /acceso-soporte/solicitar`, `POST /acceso-soporte/cancelar` | sesión de "ver como" (las dos escrituras son las únicas que `deps` deja pasar en solo lectura) |
| `GET /acceso-soporte/solicitudes`, `POST /acceso-soporte/{id}/aprobar` (15/30/60 min), `/rechazar`, `/revocar` | owner/superadmin, nunca una sesión de "ver como" |
| `POST /acceso-soporte/enlace/leer`, `/enlace/aprobar`, `/enlace/rechazar` | públicos; token en el body |

Reglas: una solicitud viva (pendiente o aprobada) por gerente y negocio;
pendiente vive `ACCESO_SOPORTE_VIGENCIA_MINUTOS` (30); aprobar/rechazar es
idempotente (una ya resuelta o vencida devuelve su estado, sin efecto); un id
de otro negocio es 404. La concesión se relee de BD en cada petición, no viaja
en el token: revocar o vencerse cierra la escritura en la siguiente llamada.
Con concesión siguen cerradas las rutas de `services.acceso_soporte.RUTAS_SENSIBLES`
(contraseña, equipo, pagos, credenciales de canales, perfil, onboarding, borrar cuenta), y
las credenciales de herramientas (`RUTAS_CREDENCIALES_CERRADAS`, spec 005a), y cada
escritura queda en `gerencia_auditoria` (`impersonacion_escritura`, más las acciones
`acceso_escritura_*`). Tests: `tests/test_acceso_soporte.py`.

Entra como el `owner` (si no hay, `superadmin` o `member`), nunca como
`vendedor`. En el portal el token va a `sessionStorage`: solo afecta a esa
pestaña y la sesión del gerente en `localStorage` no se toca.

### Vista de gerencia a las conversaciones de un número

Todo `gerencia_users` abre en `/gerencia/conversaciones` (sin `tenant_id`) las
conversaciones del número de WhatsApp cuyo `phone_number_id` de Meta está en
`GERENCIA_NUMERO_PHONE_NUMBER_ID` (vacío = función apagada). Es un atajo sobre las
rutas por tenant que ya existían: `GET /gerencia/numero-conversaciones` resuelve el
número (`services/vista_gerencia.py`, `agentes_canal` activo o pausado) y el portal
llama después a `/gerencia/tenants/{id}/conversaciones?numero_id=...`, así que ver,
tomar, responder y devolver a la IA funcionan igual que allí.

El dueño (owner/superadmin, nunca una sesión de «ver como»; `/api/vista-gerencia` está
en `RUTAS_SENSIBLES`) puede **revocar** esa vista desde Canales
(`GET /vista-gerencia`, `POST /vista-gerencia/revocar|restablecer`,
`sql/67_vista_gerencia_conversaciones.sql`). Con la vista revocada, todas las rutas
`/gerencia/tenants/{id}/conversaciones*` de ese negocio y `numero-conversaciones`
responden 403 `vista_revocada_por_dueno`. Revocar y restablecer quedan en
`gerencia_auditoria`. El panel solo aparece en el negocio dueño del número
(`aplica`). Tests: `tests/test_vista_gerencia.py`.

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

### Suscripción recurrente con Stripe

Contratar un plan abre un Checkout en **`mode=subscription`** sobre el Price
recurrente del plan (`planes.stripe_price_id`, `27_stripe_suscripciones.sql`).
Stripe cobra solo cada mes y avisa por webhook
(`POST /api/pagos/stripe/webhook`); `services/stripe_suscripciones.py`
traduce cada evento. La compra de créditos no cambió: es un cargo único.

**Dónde van los Price IDs:** en la base, no en variables de entorno. Se
cargan por plan desde `/gerencia/planes` (campo "Price de Stripe",
`price_...`; el Product `prod_...` se rechaza con 422). Un plan sin Price
existe y sirve para pruebas de gerencia, pero `crear-pago` responde 409.

`crear-pago` también responde **409** si el tenant ya tiene una Subscription
viva (`stripe_subscription_id` sin `cancelada_en`): otra encima cobraría dos
veces. El cambio de plan se hace desde el panel de Stripe; la factura
siguiente trae el Price nuevo y el plan se actualiza solo. Si el tenant ya
fue cliente, se reusa su Customer (`stripe_customer_id`).

| Evento | Qué hace | Aviso a gerencia |
|---|---|---|
| `checkout.session.completed` (mode=subscription) | Marca la transacción; **no** activa el plan (lo hace la factura, que sabe hasta cuándo quedó pagado) | — |
| `invoice.payment_succeeded` (`subscription_create`) | Completa la transacción de `crear-pago` y activa el plan hasta el fin del período cobrado | correo "Nueva suscripción" |
| `invoice.payment_succeeded` (renovación) | Una transacción por factura, extiende `fecha_renovacion`, pone `intentos_fallidos` en 0. Reactiva si estaba pausada | correo "Suscripción renovada" |
| `invoice.payment_failed` | Cuenta el intento (`intentos_fallidos` = `attempt_count`), guarda `fecha_proximo_intento`. **No pausa**: conserva los días pagados | correo + alerta |
| `customer.subscription.updated` | `cancela_al_vencer` = true/false (cancelación programada o revertida). Sigue activa | correo (+ alerta si se programó) |
| `customer.subscription.deleted` | `cancelada_en`. Con días restantes sigue activa hasta `fecha_renovacion`; sin días, se pausa ya | correo + alerta |

La pausa diferida (cobro fallido o baja con días restantes) la hace
`jobs/pagos_background.py` cuando pasa `fecha_renovacion`, y en esos dos
casos avisa "Suscripción pausada" (correo + alerta). Una prueba de gerencia
que vence se pausa igual, pero sin aviso.

**Idempotencia:** Stripe reintenta y no garantiza el orden. Una factura es
una sola fila (`idx_tenant_transactions_stripe_invoice`); un intento fallido
repetido no avanza `intentos_fallidos`; una cancelación solo se aplica si el
estado cambia. Solo se avisa cuando la escritura ocurrió de verdad. Si la
escritura falla, el webhook responde 500 y Stripe reintenta.

**Avisos** (`services/notificaciones_gerencia.py`): correo a cada
`gerencia_users` con negocio, correo del dueño, plan, monto, intento y
próximo reintento, vigencia, motivo, qué hizo el sistema, ids de Stripe y
enlace a `/gerencia/tenants/{id}`. Lo que pide acción abre además una alerta
en `gerencia_alertas` (una abierta por tipo y negocio; un segundo intento
fallido la actualiza). Van en background: un SMTP caído no afecta al cobro.

Los lectores aceptan el formato de Invoice anterior y el de la API
2025-03-31 "basil" (`parent.subscription_details`), así que da igual qué
versión tenga el endpoint.

#### Eventos que tiene que tener el endpoint del webhook

En el panel de Stripe → Developers → Webhooks → el endpoint
`{BASE_URL_BACKEND}/api/pagos/stripe/webhook`:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`
- `checkout.session.async_payment_failed`
- `checkout.session.expired`
- `charge.refunded`
- `invoice.payment_succeeded`
- `invoice.payment_failed`
- `customer.subscription.updated`
- `customer.subscription.deleted`

Los primeros cinco ya se usaban (créditos y la transacción del checkout);
los últimos cuatro son los del ciclo de vida. Cualquier otro evento se
contesta 200 y se ignora.

#### Prueba manual paso a paso (modo test)

Automatizado: `pytest tests/test_stripe_suscripciones.py -v`. Contra Stripe
de verdad:

1. Aplicar la migración: `python aplicar_sql.py sql/27_stripe_suscripciones.sql`.
2. En `/gerencia/planes`, pegar el Price de prueba (`price_...`) del plan.
3. `stripe listen --forward-to localhost:8000/api/pagos/stripe/webhook` y
   poner el `whsec_...` que imprime en `STRIPE_WEBHOOK_SECRET`.
4. **Alta:** en `/suscripcion`, "Contratar ahora" con la tarjeta
   `4242 4242 4242 4242`. Esperado: `tenant_subscriptions` en `activa` con
   `stripe_subscription_id`, y en el log del backend
   `Aviso a gerencia: alta` (sin SMTP el cuerpo del correo sale en el log).
5. **Renovación:** crear la suscripción sobre un Customer con
   [Test Clock](https://docs.stripe.com/billing/testing/test-clocks) y
   adelantar el reloj un mes. Esperado: una transacción "Renovación ...",
   `fecha_renovacion` extendida y aviso `renovacion`.
6. **Cobro fallido:** en el panel de Stripe, cambiar el método de pago del
   Customer por `4000 0000 0000 0341` (se asocia bien, pero el cobro falla)
   y adelantar el Test Clock al fin del período. Esperado:
   `intentos_fallidos = 1`, estado sigue `activa`, alerta
   "Cobro de suscripción fallido" en `/gerencia/salud`. Adelantar hasta
   pasada `fecha_renovacion` y esperar al job (o llamar a
   `job_pausar_suscripciones_vencidas`): queda `pausada` con aviso.
7. **Cancelación programada:** en el panel, "Cancel subscription" → "At end
   of period". Esperado: `cancela_al_vencer = true`, sigue `activa`, aviso.
8. **Baja inmediata con días restantes:** "Cancel immediately". Esperado:
   `cancelada_en` con valor, sigue `activa` hasta `fecha_renovacion`, aviso
   `cancelada`; al vencer, el job la pausa y avisa.

`stripe trigger invoice.payment_failed` y parecidos crean objetos sin
nuestra metadata ni un tenant enlazado: el webhook los contesta 200 y no
hace nada. Sirven para ver que la firma pasa, no para probar el flujo.

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

Quien lo llama es el workflow `medir-uso-tokens`, no `entrada-canal-universal`:
el `tokenUsage` del sub-nodo "Anthropic Chat Model" sale por la salida
`ai_languageModel`, que un Code node de la misma ejecución no puede leer. Por
eso un workflow programado (cada 2 min) lee por la API de n8n las ejecuciones
ya terminadas, suma cada llamada al modelo (una por vuelta del agente) y
reporta con `idempotency_key = exec:<id>:agente`. Solo si el backend devuelve
`registrado: true` escribe además la fila `mensaje_procesado` en `usage_events`
(tabla de n8n que alimenta el límite de tokens del plan). Así se usa este
endpoint como candado para no duplicar.

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

### Créditos por llamada a herramienta

Regla: **1 crédito = 1 llamada a herramienta** ejecutada por el agente
(`services/creditos.py`, `sql/31_creditos_herramientas.sql`). `escalar_humano`
no cobra: sin saldo, quien pide una persona tiene que poder llegar a ella.

Dos bolsas en `tenant_credits`:

| Columna | Qué es | Se gasta |
|---|---|---|
| `creditos_plan` (+ `creditos_plan_vence`) | Cuota del ciclo: `planes.creditos_incluidos_mensual`. Se **reinicia** (no acumula) en cada activación o renovación | primero, y solo mientras no venza |
| `creditos_disponibles` | Comprados (paquetes) o ajustados por gerencia. No vencen | cuando la del plan se acaba |

La bolsa del plan **no** cuenta en `acceso_pagos`: solo los comprados dejan
operar sin suscripción (modelo híbrido), así un plan vencido no sigue
funcionando con lo que le sobró del mes.

Cuándo se carga la cuota (siempre en la misma transacción que activa el
plan, con asiento `asignacion_plan` en `credit_transactions`):

| Disparador | `referencia` (idempotencia) |
|---|---|
| Pago aprobado (Mercado Pago, `procesar_pago_aprobado`) | `tx:<transaccion_id>` |
| Factura pagada de Stripe (alta y cada renovación) | `stripe_invoice:<id>` |
| Prueba otorgada por gerencia | `prueba:<uuid>` (cada otorgamiento reinicia) |

Revocar una prueba o que el job pause un plan vencido deja la bolsa en 0
(asiento `expiracion_plan`). Los tenants sin plan (demos, internos) no
tienen cuota: se les da saldo con el ajuste manual de gerencia
(`POST /api/gerencia/tenants/{id}/creditos`), que va a `creditos_disponibles`.

`POST /api/eventos/creditos/consumir` — con `X-Internal-Token`. Lo llama el
sub-workflow `ejecutar-herramienta-tenant` de n8n después de validar que la
herramienta existe (una inexistente no cobra) y antes de ejecutarla:

```json
{ "tenant_id": "…", "herramienta": "crear_reserva", "conversation_id": "…",
  "idempotency_key": "{{$execution.id}}" }
→ { "permitido": true,  "saldo_restante": "41.00", "bolsa": "plan", "duplicado": false }
→ { "permitido": false, "motivo": "sin_creditos", "saldo_restante": "0.00" }
```

- Sin saldo responde **200 con `permitido: false`**, no 402: n8n tiene que
  distinguirlo de un token inválido o un backend caído. En ese caso n8n no
  ejecuta la herramienta y le devuelve al agente `SIN_CREDITOS: …`; con
  error HTTP tampoco la ejecuta (falla cerrado).
- **Atómico**: la fila de `tenant_credits` se bloquea (`FOR UPDATE`) durante
  el descuento, así que llamadas en paralelo del mismo tenant se serializan
  y nunca gastan más de lo que hay; los `CHECK >= 0` son el segundo candado.
- La misma `idempotency_key` no cobra dos veces (`duplicado: true`).
- Cada descuento deja un asiento `gasto`: fecha, tenant, herramienta,
  conversación, bolsa y saldo anterior/nuevo.
- Se cobra al validar, antes de ejecutar: una herramienta que después
  falla por un problema técnico (API externa caída) ya consumió su crédito.

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
- **Mensajes entrantes de NeuroAPI**: llegan a la misma `webhook_url` de la
  Connect Session (`/api/canales/whatsapp/neuroapi/webhook`). El backend
  valida la firma con `NEUROAPI_CONNECT_WEBHOOK_SECRET`, resuelve el tenant
  por el `phone_number_id` receptor y reenvía el cuerpo intacto a
  `N8N_WEBHOOK_ENTRADA_URL` (`services/entrada_mensajes.py`), con
  `X-Tenant-Id`, `X-Canal`, `X-Proveedor` y `X-Internal-Token`. Si n8n no lo
  recibe responde 502/503 para que NeuroAPI reintente. El enrutamiento por
  tenant a otro destino se agrega en `entrada_mensajes.destino_para`.
- **Varios números (spec 005a)**: el alta y la atribución del mensaje pasan a la
  tabla `agentes_canal` (índice único parcial por `phone_number_id` vinculado, que
  cierra la carrera del alta pendiente arriba) y la plataforma responde por número;
  `channel_credentials` queda como credencial por negocio. Los puntos de abajo sobre
  n8n describen el flujo anterior y quedan obsoletos para WhatsApp cuando se
  implemente la spec (ver "Varios números de WhatsApp por negocio").
- **Respuestas por NeuroAPI**: las manda n8n, no el backend. Al vincular, el
  backend guarda la línea con `set_whatsapp_neuroapi`
  (`sql/30_whatsapp_neuroapi_credenciales.sql`): `bsp_provider='neuroapi'` y
  `NEUROAPI_API_KEY` cifrada como `access_token`, que n8n lee con
  `get_channel_credentials` y manda en `x-api-key`. Las líneas vinculadas
  antes de esa migración quedaron con token NULL y `bsp_provider='meta'`;
  se reparan con `python reparar_whatsapp_neuroapi.py --aplicar` (sin
  `--aplicar` solo las lista). Ese script no sirve para rotar la API key
  (solo toca líneas sin token o con otro proveedor): tras rotarla hay que
  reconectar cada línea.
- **Desconectar una línea de NeuroAPI**: NeuroAPI no expone endpoint de baja
  (su API pública solo tiene connect/sessions, messaging/send,
  messaging/webhooks/api y messaging/logs). `DELETE /api/canales/whatsapp`
  desactiva la línea en nuestra base, y con eso basta para que el proxy
  descarte lo que siga llegando y n8n no conteste. Devuelve
  `"proveedor": "neuroapi"` y el portal le pide al negocio que retire el
  acceso de la app en su Meta Business Manager, que es lo que la desvincula
  del todo.

## Instagram Direct vía NeuroAPI (`sql/69_instagram_neuroapi.sql`)

NeuroAPI es el proveedor principal de Instagram mientras Meta no lo reemplace
(junto con Kontesta y NeuroAPI para WhatsApp). El OAuth de Meta de "Flujo de
conexión con Meta" sigue en el código, pero el portal ya no lo ofrece para
Instagram: la fila de Instagram en Canales muestra "Vincular Instagram Direct".

```
POST /api/canales/instagram/neuroapi/iniciar   gerencia_actual: abre la Connect Session (service_type instagram_messaging)
GET  /api/canales/instagram/neuroapi/estado    La última sesión de Instagram del negocio (404 si no hay)
```

- **Key propia, webhook compartido.** La sesión se abre con `NEUROAPI_INSTA_KEY`
  (scope `instagram_api`), pero su `webhook_url` es la misma de WhatsApp
  (`/api/canales/whatsapp/neuroapi/webhook`) y la firma el mismo
  `NEUROAPI_CONNECT_WEBHOOK_SECRET`. `neuroapi_connect_sessions.canal` dice de qué
  canal es cada sesión: un `instagram.connected` sobre una sesión de WhatsApp no
  activa nada. Sin `NEUROAPI_INSTA_KEY` (o sin el secreto) `iniciar` responde 503.
- **Alta** (`services/instagram_neuroapi.py`). `instagram.connected` trae
  `data.accounts[].instagramAccountId` (y `username`). Mismas reglas que un número
  (`numeros_reglas.evaluar_alta`), **sin cupo por plan**:
  cuenta activa de otro negocio → `ajeno`, `cuenta_instagram_no_encontrada`, no se
  revela y cuenta para el bloqueo de cinco altas ajenas en 24 h, que es **del negocio**
  (comparte `intentos_alta_ajena` con WhatsApp; `iniciar` responde 429
  `demasiados_intentos`); la propia ya vinculada por NeuroAPI → `propio`,
  `cuenta_instagram_ya_vinculada`, sin tocar nada; libre → `set_instagram_neuroapi`:
  `bsp_provider='neuroapi'`, `ig_user_id`, `NEUROAPI_INSTA_KEY` cifrada como
  `access_token` y **`page_id` en NULL**. Un negocio tiene una sola cuenta: vincular
  otra la reemplaza, y una que antes se conectó por OAuth de Meta se migra (sin
  `page_id` deja de resolverse por el webhook de Meta, así que no hay respuestas
  dobles; "Obtener nombre" en esas conversaciones responde 409 porque usa la página).
  El resultado queda en `neuroapi_connect_sessions.detalle`.
- **Retorno.** `return_url` = `/canales?instagram_neuroapi=retorno` (NeuroAPI agrega
  `status`). El portal no confía en `status=success`: consulta `.../estado` hasta que
  la sesión deja de estar pendiente (90 s). Solo un `status` distinto de `success`
  se muestra enseguida como cancelado.
- **Entrantes.** El webhook enruta por `object`: `instagram` → la cuenta receptora
  es `entry[].id` (o `messaging[].recipient.id`), se resuelve con
  `resuelve_tenant('instagram', …)` y el cuerpo se reenvía intacto con
  `X-Canal: instagram`. La forma anidada `entry[].entry[].messaging[]` se aplana
  (`normalizar.aplanar_entradas`). Los ecos (`is_echo`) los descarta `normalizar`.
- **Envío** (`services/mensajeria.py`, el adaptador por proveedor):
  `POST /neuroapi/instagram/send` con `x-api-key` y
  `{recipient_id: <sender.id>, type: "text", text: {body}}`. La respuesta de una
  persona desde el portal lleva `tag: "HUMAN_AGENT"` (Meta deja contestar hasta
  7 días tras el último mensaje del cliente; el detalle de conversación lo expone
  en `minutos_restantes_ventana_humana`); la del agente no la lleva. Solo el
  **agente en código** contesta estas cuentas: n8n envía Instagram por Graph con
  `page_id`, que aquí es NULL (aviso de arranque si `AGENTE_EN_CODIGO` está apagada).
- **Media entrante.** El `media_id` del adjunto (en el adjunto o en su `payload`;
  NeuroAPI no lo documenta) se baja por `GET /neuroapi/instagram/media/{id}` con
  `NEUROAPI_INSTA_KEY` (`adjuntos.descargar_media_instagram`: id de ruta validado,
  sin redirecciones, con tope de tamaño) y se guarda en `message_attachments` si
  es JPG/PNG/PDF/DOCX. La URL de CDN de Meta del webhook nunca se descarga. Un
  fallo deja el mensaje solo con su texto.
- **Fuera de alcance**: enviar imágenes o archivos por Instagram, y mover WhatsApp
  y Facebook al adaptador de `mensajeria.py`.

Ver `tests/test_instagram_neuroapi.py`.

## Plantillas de WhatsApp del negocio

Cada negocio crea, consulta y elimina sus plantillas por la API de plantillas de NeuroAPI
(`/messaging/templates`, scope `whatsapp_cloud_api`), con la `x-api-key` de su línea activa
(`bsp_provider='neuroapi'`; cae a `NEUROAPI_API_KEY`). No confundir con las dos plantillas **globales** de
avisos de cita (`/api/gerencia/plantillas-whatsapp`, spec 004), que no se tocan. Las plantillas de los avisos de cita de cada negocio (spec 007) viven en esta misma caché pero se gestionan desde Calendario ("Avisos de cita por tenant").

| Endpoint | Rol | Notas |
|---|---|---|
| `GET /api/plantillas-whatsapp?categoria=&estado=&sincronizar=` | `negocio_actual` | Lee la caché; `sincronizar=true` la refresca desde NeuroAPI |
| `GET /api/plantillas-whatsapp/{nombre}` | `negocio_actual` | Detalle para la vista previa |
| `POST /api/plantillas-whatsapp` | `gerencia_actual` | Valida (slug, `{{n}}` consecutivas, largos) antes de llamar a Meta; queda `PENDING` |
| `DELETE /api/plantillas-whatsapp/{nombre}?confirmar=true` | `gerencia_actual` | Permanente en Meta; sin `confirmar` es 400 |
| `POST /api/plantillas-whatsapp/{nombre}/prueba` | `gerencia_actual` | Solo `APPROVED`; `template.components[].parameters` |

- Tabla `plantillas_whatsapp_tenant` (`sql/63`): **caché**, la fuente de verdad es Meta. Una plantilla de otro
  negocio es 404. `/api/plantillas-whatsapp` está en `RUTAS_SENSIBLES` (en "ver como" solo se lee).
- Errores de NeuroAPI: 400 → 422 con el motivo de Meta; 403 → 502 `scope_whatsapp_cloud_api_faltante`;
  404 → 404; 429 → 429; el resto → 502.
- Webhook: `message_template_status_update` se aplica también a esta caché
  (`plantillas_tenant.aplicar_evento_meta`). El evento no trae tenant: se resuelve por `meta_template_id`,
  luego `waba_id` y, si no, solo cuando el nombre es de un único negocio; con homónimos se ignora. Cuando la resolución cae en la rama por `waba_id` o en la de candidata única, se descarta el evento si el `waba_id` o el `meta_template_id` ya guardados de la candidata contradicen los del evento (un dato guardado vacío o ausente en el evento no contradice); la rama por `meta_template_id` no pasa por esa comprobación (si el id coincide, la candidata es la dueña aunque el `waba_id` difiera). Los eventos del WABA global (`AVISOS_CUENTA_GLOBAL_WABA_ID`) se ignoran en este handler de negocio (`entradas_de_negocio`, por `entry`); con esa variable vacía no se descarta ninguno por «ser global».
- Código: `services/neuroapi_plantillas.py`, `services/plantillas_reglas.py` (puras),
  `services/plantillas_tenant.py`, `routers/plantillas_whatsapp_tenant.py`; tests en
  `tests/test_plantillas_tenant.py`. Frontend: `/plantillas` y `/plantillas/nueva`.
