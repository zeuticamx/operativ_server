"""
Reevaluación del cupo, los límites y las herramientas de un negocio (spec 005a,
RF-11, T8), mantenimiento de los números (RF-14, RF-2) y despliegue (RF-22).

Qué hace `reevaluar_negocio`, en una transacción y con el candado del negocio
(el mismo `pg_advisory_xact_lock` que toman las acciones de `numeros_whatsapp`):

  1. Salta los negocios suspendidos o de baja (P8): se reevalúan al reactivarse.
  2. Lee las reglas vigentes (`limites_efectivos`: prueba, suscripción, sin
     suscripción vigente, sin plan activo).
  3. Recorta los límites de modelo e historial de los números, activos o pausados
     (`recortar_agente_a_plan`), comparando la configuración antes y después para
     saber qué números avisar y anotar en la bitácora (`recorte_por_plan`).
  4. Retira las herramientas cuya familia el plan ya no incluye.
  5. Pausa los números que sobran del cupo, del más reciente al más antiguo
     (`numeros_reglas.numeros_a_pausar`), con el motivo del caso de RF-11, y aplica
     la regla única del principal. Nunca reactiva nada (RF-15). Un cambio de
     límites o de lista de herramientas no toca el cupo (`herramientas_del_plan`).
  6. Sin plan activo no pausa nada y lo deja en la bitácora de la plataforma
     (CL-20, A36).
  7. Da por procesadas las entradas pendientes de la cola de ese negocio.

Los avisos (RF-20) salen DESPUÉS de confirmar, uno por acción y agrupado: pausa,
recorte y número que un cambio de plan mantiene incompleto. Por eso las funciones
públicas reciben una conexión SIN transacción abierta (`session.conexion()`).

Quién lo llama (P6): las vías de Python que cambian la suscripción justo después
de confirmar (`reevaluar_tras_cambio`, efecto inmediato), el trabajo de un minuto
(`drenar_cola` y `reevaluar_vencidos`, red de seguridad para lo que no pasa por
Python o lo que ocurre solo por el paso del reloj) y el despliegue.

Todo es idempotente: reevaluar dos veces seguidas no repite pausas, bitácora ni
avisos, y dos reevaluaciones a la vez se serializan con el candado.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

import asyncpg

from services import conversaciones_ocultas, herramientas_plan, numeros_avisos, numeros_whatsapp
from services.gerencia import registrar_auditoria
from services.numeros_bitacora import SISTEMA, ultimos_digitos, registrar
from services.numeros_reglas import configuracion_incompleta, numeros_a_pausar
from session import conexion

log = logging.getLogger("operativai.numeros_reevaluacion")

# Marca de la tarea de una sola vez de RF-22 en `numeros_despliegue`.
CLAVE_DESPLIEGUE = "despliegue_005a"

# Motivo de la cola (sql/59) -> motivo de la bitácora cuando la reevaluación pausa
# (RF-11, siete casos). `reactivacion_negocio` no tiene motivo propio en la lista
# cerrada de la spec: un negocio que vuelve se reevalúa como un cambio de plan.
MOTIVO_PAUSA: dict[str, str] = {
    "cambio_de_plan": "pausa_por_cambio_de_plan",
    "fin_de_suscripcion": "pausa_por_fin_de_suscripcion",
    "fin_de_prueba": "pausa_por_fin_de_prueba",
    "cupo_plataforma": "pausa_por_cupo_bajado_por_plataforma",
    "plan_mas_bajo": "pausa_por_plan_mas_bajo_recalculado",
    "pago_de_prueba": "pausa_por_pago_de_prueba",
    "reactivacion_negocio": "pausa_por_cambio_de_plan",
    "despliegue": "pausa_por_despliegue",
}

# Un cambio de límites o de lista de herramientas reevalúa límites y herramientas,
# no el cupo (RF-11, caso 5).
MOTIVOS_SIN_CUPO: frozenset[str] = frozenset({"herramientas_del_plan"})

MOTIVOS_VALIDOS: frozenset[str] = frozenset(MOTIVO_PAUSA) | MOTIVOS_SIN_CUPO

# Cambios de la situación comercial del propio negocio: si dejan un número activo
# incompleto, se avisa de nuevo (CL-10). Un recálculo del plan más bajo o del cupo
# por la plataforma no repite ese aviso.
MOTIVOS_AVISAN_INCOMPLETO: frozenset[str] = frozenset(
    {"cambio_de_plan", "pago_de_prueba", "fin_de_prueba", "fin_de_suscripcion"}
)

# Etiqueta con la que Instagram y Facebook salen en los avisos (no son un número).
ETIQUETA_SOCIAL = "Instagram y Facebook"

# Cuánto se conserva un intento de alta ajena (P9): la ventana es de 24 h.
HORAS_RETENCION_INTENTOS = 25

# La bitácora de la plataforma no se llena de lo mismo cada minuto (CL-20).
HORAS_SIN_REPETIR_SIN_PLAN_ACTIVO = 24

LIMITE_POR_PASADA = 200


@dataclass(frozen=True)
class ResultadoReevaluacion:
    tenant_id: UUID
    # Motivo de la cola con el que se reevaluó (None si se salió antes de decidirlo).
    motivo: Optional[str] = None
    # El negocio es suspendido, de baja o ya no existe (P8): no se hizo nada.
    omitido: bool = False
    # Pasada de la cola sin nada pendiente: otra pasada ya lo atendió.
    sin_trabajo: bool = False
    pausados: list[dict[str, Any]] = field(default_factory=list)
    recortados: list[dict[str, Any]] = field(default_factory=list)
    herramientas_retiradas: list[tuple[Optional[UUID], str]] = field(default_factory=list)
    # Números activos que siguen incompletos tras un cambio de plan (CL-10).
    incompletos: list[dict[str, Any]] = field(default_factory=list)
    sin_plan_activo: bool = False


@dataclass(frozen=True)
class ResultadoDespliegue:
    negocios_reevaluados: int
    numeros_pausados: int
    negocios_con_aviso_incompleto: int
    errores: int


def _ahora(ahora: Optional[datetime]) -> datetime:
    return ahora if ahora is not None else datetime.now(timezone.utc)


def elegir_motivo(pendientes: Sequence[str], por_defecto: str) -> str:
    """
    El motivo con el que se reevalúa: el primero pendiente en la cola que pausa por
    cupo (o, si solo hay cambios de herramientas, ese); sin pendientes, el que
    llega por parámetro. Pura: la prueban las tablas de casos.
    """
    validos = [m for m in pendientes if m in MOTIVOS_VALIDOS]
    for motivo in validos:
        if motivo not in MOTIVOS_SIN_CUPO:
            return motivo
    if validos:
        return validos[0]
    return por_defecto


# ============================================================
# Reevaluación de un negocio
# ============================================================
async def reevaluar_negocio(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    motivo: Optional[str] = None,
    *,
    por_defecto: str = "cambio_de_plan",
    ahora: Optional[datetime] = None,
    avisar: bool = True,
    solo_si_pendiente: bool = False,
) -> ResultadoReevaluacion:
    """
    Reevalúa el cupo, los límites y las herramientas de UN negocio (ver el módulo).

    `motivo` explícito manda. Sin él se toma de las entradas pendientes de la cola
    del negocio (las que dejaron los triggers de sql/59 al escribir la suscripción) y,
    si no hay, `por_defecto`. `solo_si_pendiente` es para la pasada de la cola: si
    otra reevaluación ya atendió al negocio, no hace nada.

    Pasa una conexión sin transacción abierta: los avisos salen al terminar.
    `ahora` solo afecta a las reglas leídas de `limites_efectivos`; el recorte de
    límites de sql/58 usa el reloj de la base.
    """
    for candidato in (motivo, por_defecto):
        if candidato is not None and candidato not in MOTIVOS_VALIDOS:
            raise ValueError(f"Motivo de reevaluación desconocido: {candidato!r}")
    ahora = _ahora(ahora)

    async with conn.transaction():
        resultado = await _aplicar(conn, tenant_id, motivo, por_defecto, ahora, solo_si_pendiente)
    if avisar:
        await _avisar(conn, resultado)
    return resultado


async def reevaluar_tras_cambio(
    tenant_id: UUID, motivo: str = "cambio_de_plan", *, forzar: bool = False
) -> None:
    """
    Disparo inmediato de las vías de Python que acaban de confirmar un cambio en la
    suscripción (RF-11, P6): mismo efecto que la cola, en la misma petición. Con
    `forzar` el motivo es el que se pasa; sin él manda el que dejó el trigger.

    NUNCA propaga un error: el cambio de suscripción ya está confirmado y la
    entrada de la cola sigue pendiente, así que el trabajo de un minuto lo reintenta.
    """
    try:
        async with conexion() as conn:
            if forzar:
                await reevaluar_negocio(conn, tenant_id, motivo)
            else:
                await reevaluar_negocio(conn, tenant_id, None, por_defecto=motivo)
    except Exception:
        log.exception("No se pudo reevaluar el negocio %s tras un cambio de suscripción", tenant_id)


async def _aplicar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    motivo: Optional[str],
    por_defecto: str,
    ahora: datetime,
    solo_si_pendiente: bool,
) -> ResultadoReevaluacion:
    await numeros_whatsapp._bloquear_negocio(conn, tenant_id)

    pendientes = await conn.fetch(
        "SELECT id, motivo FROM numeros_reevaluacion_cola WHERE tenant_id = $1 AND procesada_en IS NULL ORDER BY id",
        tenant_id,
    )
    ids_pendientes = [f["id"] for f in pendientes]
    if solo_si_pendiente and not ids_pendientes:
        return ResultadoReevaluacion(tenant_id, sin_trabajo=True)
    motivo = motivo or elegir_motivo([f["motivo"] for f in pendientes], por_defecto)

    async def dar_por_procesadas() -> None:
        if ids_pendientes:
            await conn.execute(
                "UPDATE numeros_reevaluacion_cola SET procesada_en = NOW() WHERE id = ANY($1::bigint[])",
                ids_pendientes,
            )

    existe = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM tenants WHERE id = $1)", tenant_id)
    estado_plataforma = await conn.fetchval(
        "SELECT estado FROM tenant_estado_plataforma WHERE tenant_id = $1", tenant_id
    )
    if not existe or estado_plataforma in ("suspendido", "baja"):
        await dar_por_procesadas()
        return ResultadoReevaluacion(tenant_id, motivo, omitido=True)

    reglas = await numeros_whatsapp.reglas_del_negocio(conn, tenant_id, ahora)

    # --- Recorte de límites (RF-10): antes y después, para avisar a quien toca. ---
    recortados = await _recortar(conn, tenant_id)

    # --- Herramientas que el plan ya no incluye (RF-9). ---
    retiradas = await herramientas_plan.retirar_herramientas_por_plan(
        conn, tenant_id, familias=reglas.familias
    )

    # --- Cupo (RF-11). ---
    pausados: list[dict[str, Any]] = []
    if motivo not in MOTIVOS_SIN_CUPO:
        pausados = await _pausar_sobrantes(conn, tenant_id, motivo, reglas, ahora)

    # --- Sin plan activo: no se pausa nada y queda registrado (CL-20, A36). ---
    if reglas.sin_plan_activo:
        await _registrar_sin_plan_activo(conn, tenant_id, motivo)

    # --- Números que un cambio de plan mantiene incompletos (CL-10). ---
    incompletos: list[dict[str, Any]] = []
    if motivo in MOTIVOS_AVISAN_INCOMPLETO:
        vinculados = await numeros_whatsapp._vinculados(conn, tenant_id)
        incompletos = [
            dict(f)
            for f in vinculados
            if f["estado"] == "activo" and configuracion_incompleta(f["agent_name"], f["system_prompt"])
        ]

    await dar_por_procesadas()
    return ResultadoReevaluacion(
        tenant_id,
        motivo,
        pausados=pausados,
        recortados=recortados,
        herramientas_retiradas=retiradas,
        incompletos=incompletos,
        sin_plan_activo=reglas.sin_plan_activo,
    )


async def _recortar(conn: asyncpg.Connection, tenant_id: UUID) -> list[dict[str, Any]]:
    """
    Llama a `recortar_agente_a_plan` y devuelve qué cambió: cada número (activo o
    pausado) y, aparte, la configuración de Instagram y Facebook. Deja la bitácora
    `recorte_por_plan` de cada uno.

    El recorte lo deja anotado la propia función (`numeros_recortes_pendientes`,
    sql/61), también cuando lo hizo un trigger antes de llegar aquí: se consumen
    todas las filas del negocio y, por destino, se compara el primer "antes" con el
    último "después" (un recorte que no cambia nada neto no se avisa).
    """
    await conn.execute("SELECT recortar_agente_a_plan($1)", tenant_id)

    pendientes = await conn.fetch(
        """
        DELETE FROM numeros_recortes_pendientes
        WHERE tenant_id = $1
        RETURNING id, agente_canal_id, modelo_antes, modelo_despues, historial_antes, historial_despues
        """,
        tenant_id,
    )
    por_destino: dict[Optional[UUID], list[Mapping[str, Any]]] = {}
    for fila in sorted(pendientes, key=lambda f: f["id"]):
        por_destino.setdefault(fila["agente_canal_id"], []).append(fila)

    numeros = {
        f["id"]: dict(f)
        for f in await conn.fetch(
            """
            SELECT id, estado, telefono_visible, phone_number_id, model, history_window
            FROM agentes_canal
            WHERE tenant_id = $1 AND estado IN ('activo', 'pausado')
            """,
            tenant_id,
        )
    }

    recortados: list[dict[str, Any]] = []
    for destino, filas in por_destino.items():
        primera, ultima = filas[0], filas[-1]
        detalle = _diferencias(
            {"model": primera["modelo_antes"], "history_window": primera["historial_antes"]},
            {"model": ultima["modelo_despues"], "history_window": ultima["historial_despues"]},
        )
        if not detalle:
            continue
        if destino is None:
            await registrar(
                conn, tenant_id, None, "recorte_por_plan", SISTEMA, {**detalle, "canal": "instagram_facebook"}
            )
            recortados.append(_fila_social())
            continue
        numero = numeros.get(destino)
        if numero is None:
            continue
        await registrar(conn, tenant_id, destino, "recorte_por_plan", SISTEMA, detalle)
        recortados.append(numero)
    return recortados


def _diferencias(antes: Mapping[str, Any], despues: Mapping[str, Any]) -> dict[str, Any]:
    detalle: dict[str, Any] = {}
    if antes["model"] != despues["model"]:
        detalle["modelo"] = [antes["model"], despues["model"]]
    if antes["history_window"] != despues["history_window"]:
        detalle["historial"] = [antes["history_window"], despues["history_window"]]
    return detalle


def _fila_social() -> dict[str, Any]:
    """Instagram y Facebook con la forma de una fila de número, para el aviso agrupado."""
    return {"id": None, "telefono_visible": ETIQUETA_SOCIAL, "phone_number_id": None, "estado": "activo"}


async def _pausar_sobrantes(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    motivo: str,
    reglas: Any,
    ahora: datetime,
) -> list[dict[str, Any]]:
    """RF-11: pausa los activos que sobran del cupo, los más recientes primero (D15)."""
    vinculados = await numeros_whatsapp._vinculados(conn, tenant_id)
    sobrantes = numeros_a_pausar([numeros_whatsapp._a_numero_canal(f) for f in vinculados], reglas.tope)
    if not sobrantes:
        return []

    motivo_bitacora = MOTIVO_PAUSA[motivo]
    principal_previo = next((f["id"] for f in vinculados if f["es_principal"] and f["id"] in sobrantes), None)
    filas = await conn.fetch(
        """
        UPDATE agentes_canal
        SET es_principal = false, estado = 'pausado', pausado_en = $2,
            motivo_pausa = $3, updated_at = NOW()
        WHERE id = ANY($1::uuid[]) AND estado = 'activo'
        RETURNING *
        """,
        sobrantes,
        ahora,
        motivo_bitacora,
    )
    filas = sorted(filas, key=lambda f: (f["vinculado_en"], f["id"]))
    for fila in filas:
        await registrar(
            conn, tenant_id, fila["id"], motivo_bitacora, SISTEMA,
            {
                "plan": reglas.plan,
                "tope": reglas.tope,
                "ultimos_digitos": ultimos_digitos(fila["phone_number_id"]),
            },
        )
    await numeros_whatsapp.aplicar_regla_principal(conn, tenant_id, SISTEMA, principal_previo=principal_previo)
    return [dict(f) for f in filas]


async def _registrar_sin_plan_activo(conn: asyncpg.Connection, tenant_id: UUID, motivo: str) -> None:
    ya = await conn.fetchval(
        """
        SELECT 1 FROM gerencia_auditoria
        WHERE tenant_id = $1 AND accion = 'plan_sin_tope_por_falta_de_plan_activo'
          AND created_at > NOW() - make_interval(hours => $2)
        LIMIT 1
        """,
        tenant_id,
        HORAS_SIN_REPETIR_SIN_PLAN_ACTIVO,
    )
    if ya is not None:
        return
    await registrar_auditoria(
        actor_email="sistema",
        actor_portal_user_id=None,
        accion="plan_sin_tope_por_falta_de_plan_activo",
        tenant_id=tenant_id,
        detalle={"motivo": motivo},
        conn=conn,
    )


async def _avisar(conn: asyncpg.Connection, resultado: ResultadoReevaluacion) -> None:
    """Un aviso agrupado por acción (RF-20, D29), tras confirmar. Nunca lanza."""
    if resultado.omitido or resultado.sin_trabajo or resultado.motivo is None:
        return
    tenant_id = resultado.tenant_id
    try:
        if resultado.pausados:
            await numeros_avisos.avisar_pausa(conn, tenant_id, resultado.pausados, MOTIVO_PAUSA[resultado.motivo])
        if resultado.recortados:
            await numeros_avisos.avisar_recorte(conn, tenant_id, resultado.recortados)
        if resultado.incompletos:
            await numeros_avisos.avisar_incompleto(conn, tenant_id, resultado.incompletos, "plan")
    except Exception:
        log.exception("No se pudieron enviar los avisos de la reevaluación de %s", tenant_id)


# ============================================================
# Trabajo de un minuto: cola y suscripciones vencidas
# ============================================================
async def drenar_cola(
    conn: asyncpg.Connection, *, ahora: Optional[datetime] = None, limite: int = LIMITE_POR_PASADA
) -> int:
    """
    Reevalúa los negocios con entradas pendientes en `numeros_reevaluacion_cola`
    (las que dejaron los triggers de sql/59), una vez por negocio. Un fallo de un
    negocio se registra y no frena a los demás; su entrada sigue pendiente y se
    reintenta en la pasada siguiente. Devuelve cuántos negocios atendió.
    """
    filas = await conn.fetch(
        """
        SELECT tenant_id
        FROM numeros_reevaluacion_cola
        WHERE procesada_en IS NULL
        GROUP BY tenant_id
        ORDER BY MIN(id)
        LIMIT $1
        """,
        limite,
    )
    atendidos = 0
    for fila in filas:
        try:
            resultado = await reevaluar_negocio(
                conn, fila["tenant_id"], None, ahora=ahora, solo_si_pendiente=True
            )
            if not resultado.sin_trabajo:
                atendidos += 1
        except Exception:
            log.exception("Falló la reevaluación de la cola del negocio %s", fila["tenant_id"])
    return atendidos


async def reevaluar_vencidos(
    conn: asyncpg.Connection, *, ahora: Optional[datetime] = None, limite: int = LIMITE_POR_PASADA
) -> int:
    """
    Suscripciones y pruebas 'activas' cuya fecha de renovación ya pasó: el reloj las
    dejó sin vigencia sin que nadie escribiera la fila, así que ningún trigger lo
    avisó (RF-11, casos 2 y 3). El job de pagos las pasa a 'pausada' cada hora; esto
    reevalúa el cupo en cuanto vencen.

    Para no repetirse cada minuto, deja en la cola una entrada ya procesada con el
    motivo (`fin_de_suscripcion` o `fin_de_prueba`) como constancia: no se vuelve a
    reevaluar una vencida con una constancia posterior a su fecha de renovación.
    Devuelve cuántos negocios reevaluó.
    """
    ahora = _ahora(ahora)
    filas = await conn.fetch(
        """
        SELECT s.tenant_id,
               CASE WHEN s.origen = 'prueba' THEN 'fin_de_prueba' ELSE 'fin_de_suscripcion' END AS motivo
        FROM tenant_subscriptions s
        WHERE s.estado = 'activa'
          AND s.fecha_renovacion IS NOT NULL
          AND s.fecha_renovacion < $1
          AND NOT EXISTS (
              SELECT 1 FROM numeros_reevaluacion_cola q
              WHERE q.tenant_id = s.tenant_id
                AND q.motivo = CASE WHEN s.origen = 'prueba' THEN 'fin_de_prueba' ELSE 'fin_de_suscripcion' END
                AND q.creada_en >= s.fecha_renovacion
          )
        ORDER BY s.fecha_renovacion
        LIMIT $2
        """,
        ahora,
        limite,
    )
    reevaluados = 0
    for fila in filas:
        try:
            await reevaluar_negocio(conn, fila["tenant_id"], fila["motivo"], ahora=ahora)
            await conn.execute(
                "INSERT INTO numeros_reevaluacion_cola (tenant_id, motivo, procesada_en) VALUES ($1, $2, NOW())",
                fila["tenant_id"],
                fila["motivo"],
            )
            reevaluados += 1
        except Exception:
            log.exception("Falló la reevaluación del negocio %s con la suscripción vencida", fila["tenant_id"])
    return reevaluados


# ============================================================
# Mantenimiento: intentos de alta ajena y retención de ocultas
# ============================================================
async def limpiar_intentos(conn: asyncpg.Connection, ahora: Optional[datetime] = None) -> int:
    """P9: borra los intentos de alta ajena con más de 25 horas (la ventana es de 24). Devuelve cuántos."""
    ahora = _ahora(ahora)
    borrados = await conn.fetchval(
        """
        WITH borrados AS (
            DELETE FROM intentos_alta_ajena
            WHERE creado_en < $1::timestamptz - make_interval(hours => $2)
            RETURNING 1
        )
        SELECT count(*) FROM borrados
        """,
        ahora,
        HORAS_RETENCION_INTENTOS,
    )
    return int(borrados or 0)


async def aplicar_retencion(conn: asyncpg.Connection, ahora: Optional[datetime] = None) -> tuple[int, int]:
    """
    RF-14, P10, A43: elimina las conversaciones de los números desvinculados hace más
    de 12 meses (sus mensajes caen en cascada) y las filas de esos números que se
    quedaron sin conversaciones. Devuelve (conversaciones, números). Idempotente.
    """
    return await conversaciones_ocultas.eliminar_vencidas(conn, _ahora(ahora))


# ============================================================
# Despliegue de una sola vez (RF-22, RF-11 caso 7)
# ============================================================
async def ejecutar_despliegue_una_vez(
    conn: asyncpg.Connection, *, ahora: Optional[datetime] = None
) -> Optional[ResultadoDespliegue]:
    """
    Tarea de una sola vez al desplegar la spec 005a (RF-22, P19, D38):

      1. Reevalúa con el motivo `despliegue` a los negocios SIN suscripción vigente
         (pausa los sobrantes solo si el plan más bajo activo tiene tope, y aplica
         sus límites y su lista de herramientas) y a los de PRUEBA (limites y lista
         del plan asociado; una prueba no tiene tope). Los negocios con suscripción
         vigente conservan el cupo y los límites de su plan: no se tocan.
      2. Evalúa la configuración incompleta (RF-8) de todos los números activos, la
         línea de avisos incluida, y la de Instagram y Facebook: bitácora
         `configuracion_incompleta` (origen `despliegue`) y un aviso agrupado por
         negocio (A63, A73).

    Devuelve None si ya se ejecutó o si otra instancia la está ejecutando (candado de
    sesión). Cada negocio se procesa por su cuenta; si alguno falla se registra y la
    marca de `numeros_despliegue` NO se escribe, así que la siguiente pasada del
    trabajo reintenta (todo es idempotente: ni pausas ni avisos se repiten).
    """
    if await _despliegue_hecho(conn):
        return None
    if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", CLAVE_DESPLIEGUE):
        return None
    try:
        if await _despliegue_hecho(conn):
            return None
        ahora = _ahora(ahora)

        reevaluados = pausados = errores = 0
        negocios = await conn.fetch(
            """
            SELECT t.id
            FROM tenants t
            WHERE COALESCE((SELECT ep.estado FROM tenant_estado_plataforma ep WHERE ep.tenant_id = t.id), 'activo')
                  NOT IN ('suspendido', 'baja')
              AND (SELECT sc.situacion FROM situacion_comercial(t.id) sc) IN ('sin_suscripcion', 'prueba')
            ORDER BY t.id
            """
        )
        for negocio in negocios:
            try:
                r = await reevaluar_negocio(conn, negocio["id"], "despliegue", ahora=ahora)
                reevaluados += 1
                pausados += len(r.pausados)
            except Exception:
                errores += 1
                log.exception("Falló el despliegue (reevaluación) del negocio %s", negocio["id"])

        con_aviso = 0
        candidatos = await conn.fetch(
            """
            SELECT x.tenant_id
            FROM (
                SELECT tenant_id FROM agentes_canal
                WHERE estado = 'activo' AND configuracion_incompleta(agent_name, system_prompt)
                UNION
                SELECT tenant_id FROM channel_credentials
                WHERE channel_type IN ('instagram', 'facebook') AND is_active
            ) x
            WHERE COALESCE((SELECT ep.estado FROM tenant_estado_plataforma ep WHERE ep.tenant_id = x.tenant_id), 'activo')
                  NOT IN ('suspendido', 'baja')
            ORDER BY x.tenant_id
            """
        )
        for candidato in candidatos:
            try:
                if await _evaluar_incompletos_despliegue(conn, candidato["tenant_id"]):
                    con_aviso += 1
            except Exception:
                errores += 1
                log.exception("Falló el despliegue (configuración incompleta) del negocio %s", candidato["tenant_id"])

        if errores == 0:
            await conn.execute(
                "INSERT INTO numeros_despliegue (clave) VALUES ($1) ON CONFLICT (clave) DO NOTHING",
                CLAVE_DESPLIEGUE,
            )
        else:
            log.error("El despliegue 005a terminó con %s errores: se reintentará en la siguiente pasada", errores)
        return ResultadoDespliegue(reevaluados, pausados, con_aviso, errores)
    finally:
        await conn.fetchval("SELECT pg_advisory_unlock(hashtext($1))", CLAVE_DESPLIEGUE)


async def _despliegue_hecho(conn: asyncpg.Connection) -> bool:
    return await conn.fetchval("SELECT 1 FROM numeros_despliegue WHERE clave = $1", CLAVE_DESPLIEGUE) is not None


async def _evaluar_incompletos_despliegue(conn: asyncpg.Connection, tenant_id: UUID) -> bool:
    """
    RF-22, A63, A73 para UN negocio: anota y avisa de los números activos incompletos
    (la línea de avisos incluida) y de Instagram y Facebook si están activos y su
    configuración no está completa. No repite lo que una pasada anterior ya anotó.
    Devuelve si avisó.
    """
    afectados: list[Mapping[str, Any]] = []
    async with conn.transaction():
        await numeros_whatsapp._bloquear_negocio(conn, tenant_id)
        numeros = await conn.fetch(
            """
            SELECT a.* FROM agentes_canal a
            WHERE a.tenant_id = $1 AND a.estado = 'activo'
              AND configuracion_incompleta(a.agent_name, a.system_prompt)
              AND NOT EXISTS (
                  SELECT 1 FROM numeros_bitacora b
                  WHERE b.tenant_id = a.tenant_id AND b.agente_canal_id = a.id
                    AND b.motivo = 'configuracion_incompleta' AND b.detalle->>'origen' = 'despliegue'
              )
            ORDER BY a.vinculado_en, a.id
            """,
            tenant_id,
        )
        for numero in numeros:
            await registrar(conn, tenant_id, numero["id"], "configuracion_incompleta", SISTEMA, {"origen": "despliegue"})
            afectados.append(numero)

        social_activo = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM channel_credentials "
            "WHERE tenant_id = $1 AND channel_type IN ('instagram', 'facebook') AND is_active)",
            tenant_id,
        )
        social_incompleto = social_activo and not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM tenant_agent_config c "
            "WHERE c.tenant_id = $1 AND NOT configuracion_incompleta(c.agent_name, c.system_prompt))",
            tenant_id,
        )
        ya_social = social_incompleto and await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM numeros_bitacora b
                WHERE b.tenant_id = $1 AND b.agente_canal_id IS NULL
                  AND b.motivo = 'configuracion_incompleta'
                  AND b.detalle->>'origen' = 'despliegue' AND b.detalle->>'canal' = 'instagram_facebook'
            )
            """,
            tenant_id,
        )
        if social_incompleto and not ya_social:
            await registrar(
                conn, tenant_id, None, "configuracion_incompleta", SISTEMA,
                {"origen": "despliegue", "canal": "instagram_facebook"},
            )
            afectados.append(_fila_social())

    if not afectados:
        return False
    await numeros_avisos.avisar_incompleto(conn, tenant_id, afectados, "despliegue")
    return True
