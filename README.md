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
POST   /api/auth/refresh
GET    /api/auth/yo
POST   /api/auth/invitacion/revisar     A qué negocio y con qué rol invita un enlace
POST   /api/auth/invitacion/aceptar     Enlace + contraseña → crea la cuenta (TokenOut)
```

### Roles dentro de un negocio

`portal_users.role`. El JWT solo lleva `sub`: el rol se relee de BD en cada
petición (`deps.usuario_actual`), así que cambiarlo o desactivar a alguien
surte efecto en su siguiente llamada.

| Rol | Cómo nace | Ve | Configura |
|---|---|---|---|
| `owner` | `/auth/registro` (o Google) | Todo el negocio | Todo |
| `superadmin` | a mano | Todo el negocio | Todo |
| `member` ("colaborador") | invitación | Conversaciones, embudo, agenda, métricas | Nada (agente, canales, herramientas, equipo, pagos) |
| `vendedor` | invitación ligada a su ficha | **Solo lo suyo**: su ficha, sus leads, su cartera de campo | Nada |
| `proveedor` | invitación ligada a su ficha del calendario | **Solo lo suyo**: su agenda y las conversaciones de sus clientes | Sus descansos y días libres |

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
segundo del cambio sigue valiendo.

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
POST   /api/conversaciones/{id}/tomar          Un humano toma el control (status='transferred', sin acuse)
POST   /api/conversaciones/{id}/volver-a-ia    Le devuelve el control a la IA
POST   /api/conversaciones/{id}/adjuntos       Imagen/documento por WhatsApp (multipart: archivo, leyenda?)
GET    /api/conversaciones/{id}/adjuntos/{aid} Contenido de un adjunto (JWT, filtrado por tenant)
GET    /api/media/{token}                      Público: lo descargan NeuroAPI/Meta (token aleatorio)
POST   /api/eventos/adjunto-entrante           n8n asocia a un mensaje el archivo que mandó el cliente
```

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

El agente de n8n usa el calendario a través de cinco filas de `tenant_tools`
(`consultar_servicios`, `consultar_proveedores`, `consultar_disponibilidad`,
`crear_reserva`, `cancelar_reserva`) que apuntan a `/api/eventos/calendario/*`
con `X-Internal-Token`. Las crea `services/herramientas_calendario.py` al
encender el calendario (dueño, gerencia o una prueba de plan) y las pausa al
apagarlo; al arrancar, `sincronizar_todos()` se las da a quien ya lo tenía
encendido y refresca URL (`BASE_URL_BACKEND`) y token (`N8N_INTERNAL_TOKEN`,
guardado con `set_tool_credentials`). No pisa textos editados por el dueño ni
reactiva una que él pausó. No se pueden eliminar desde `/herramientas` (409).

Si cambias el contrato de esos endpoints, actualiza también `definiciones()`:
es lo que lee el workflow `ejecutar-herramienta-tenant`.

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
