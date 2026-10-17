"""
Números de WhatsApp de un negocio (spec 005a): alta, pausa, reactivación,
principal, baja y configuración del agente de cada número.

La lógica de decisión está en `services/numeros_reglas.py` (pura). Aquí solo el
acceso a datos (asyncpg, SQL crudo) y la aplicación de esas decisiones, en el
mismo estilo que `asignacion_conversaciones.py`:

  - Cada función recibe la CONEXIÓN de quien la llama (no usa el pool), abre su
    propia transacción (un punto de guardado si ya había una) y toma el
    candado `pg_advisory_xact_lock(hashtext(tenant_id))` del negocio: las
    acciones concurrentes sobre un mismo negocio se resuelven en orden de
    llegada, cada una contra el estado vigente (D30). El mismo candado lo toma
    la reevaluación de T8.
  - Pasa una conexión SIN transacción abierta (`session.conexion()`): los avisos a
    la gerencia salen al terminar la función, es decir, tras confirmar. Con una
    transacción externa saldrían antes de confirmar.
  - Los errores son `HTTPException` con `detail={"codigo", "mensaje"}` (como
    `limites_agente`), con los códigos de la sección "Contrato de códigos".
  - Todo hecho de RF-21 queda en `numeros_bitacora` (services/numeros_bitacora.py)
    en la misma transacción que el cambio; los rechazos de altas ajenas van a
    `gerencia_auditoria` (bitácora de la plataforma).

El ORDEN en que se quita el papel de principal importa: sql/56 tiene CHECKs que
impiden que un principal esté pausado, desvinculado, sea la línea de avisos o
tenga la configuración incompleta. Por eso cada acción le quita `es_principal`
en el mismo UPDATE (o antes) y después aplica la regla única de asignación
(`aplicar_regla_principal`).
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, NoReturn, Optional, Sequence
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from config import settings
from session import conexion
from services import conversaciones_ocultas, herramientas_plan, numeros_avisos
from services.gerencia import registrar_auditoria
from services.limites_agente import LimitesAgente, exigir_dentro_del_plan
from services.numeros_bitacora import SISTEMA, Autor, registrar, ultimos_digitos
from services.numeros_reglas import (
    VENTANA_INTENTOS,
    MAX_INTENTOS_ALTA_AJENA,
    NumeroCanal,
    ReglasNegocio,
    ResultadoAlta,
    configuracion_incompleta,
    evaluar_alta,
    permite_activar,
    principal_resultante,
    recorte,
)

# Campos de la configuración del agente de un número (RF-5) que se editan.
CAMPOS_CONFIGURACION: tuple[str, ...] = (
    "agent_name",
    "system_prompt",
    "model",
    "temperature",
    "history_window",
    "agente_activo",
)

_MENSAJES = {
    "numero_no_encontrado": "Número no encontrado.",
    "numero_ya_vinculado": "Ese número ya está vinculado a tu negocio. Si está pausado, reactívalo.",
    "cupo_numeros_whatsapp": "Tu plan no tiene plazas libres para otro número de WhatsApp activo. Mejora tu plan o pausa uno.",
    "demasiados_intentos": "Demasiados intentos de vincular números. Inténtalo de nuevo más tarde.",
    "linea_avisos_gestionada_por_plataforma": "Esta es la línea de avisos de la plataforma: la gestiona la plataforma.",
    "numero_no_activo": "El número está pausado.",
    "configuracion_incompleta": "El número tiene la configuración incompleta: falta el nombre del agente o sus instrucciones.",
    "temperatura_fuera_de_rango": "La temperatura debe estar entre 0 y 1.",
    "numero_no_valido": "El identificador del número no es válido.",
}


def error_http(estado: int, codigo: str, mensaje: Optional[str] = None, **extra: Any) -> HTTPException:
    """`HTTPException` con el cuerpo `{codigo, mensaje}` del contrato de códigos de la spec."""
    headers = extra.pop("headers", None)
    detail = {"codigo": codigo, "mensaje": mensaje or _MENSAJES.get(codigo, codigo), **extra}
    return HTTPException(status_code=estado, detail=detail, headers=headers)


def _ahora(ahora: Optional[datetime]) -> datetime:
    return ahora if ahora is not None else datetime.now(timezone.utc)


async def _bloquear_negocio(conn: asyncpg.Connection, tenant_id: UUID) -> None:
    """Candado del negocio hasta el fin de la transacción (D30). Reentrante en la misma sesión."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1::text))", str(tenant_id))


def _a_numero_canal(fila: Mapping[str, Any]) -> NumeroCanal:
    return NumeroCanal(
        id=fila["id"],
        estado=fila["estado"],
        vinculado_en=fila["vinculado_en"],
        es_principal=fila["es_principal"],
        es_linea_avisos=fila["es_linea_avisos"],
        agent_name=fila["agent_name"],
        system_prompt=fila["system_prompt"],
        agente_activo=fila["agente_activo"],
    )


async def _vinculados(conn: asyncpg.Connection, tenant_id: UUID) -> list[asyncpg.Record]:
    """Los números vinculados (activos y pausados) del negocio, del más antiguo al más reciente."""
    return await conn.fetch(
        """
        SELECT * FROM agentes_canal
        WHERE tenant_id = $1 AND estado IN ('activo', 'pausado')
        ORDER BY vinculado_en, id
        """,
        tenant_id,
    )


def _plazas(filas: Sequence[Mapping[str, Any]]) -> int:
    """Plazas usadas: números activos que cuentan para el cupo (la línea de avisos no)."""
    return sum(1 for f in filas if f["estado"] == "activo" and not f["es_linea_avisos"])


@dataclass(frozen=True)
class ResultadoEntrada:
    """Decisión sobre un mensaje entrante de WhatsApp (RF-12, RF-16).

    `numero` es la fila de `agentes_canal` (o None si no hay ninguno).
    `motivo` es None cuando el número puede responder; si no, explica por
    qué no (`numero_no_vinculado`, `numero_desvinculado`,
    `numero_no_activo`, `configuracion_incompleta`).
    """

    numero: Optional[asyncpg.Record] = None
    motivo: Optional[str] = None


async def resolver_entrada(
    phone_number_id: str,
    *,
    ahora: Optional[datetime] = None,
) -> ResultadoEntrada:
    """
    Resuelve a qué número pertenece un mensaje entrante de WhatsApp y si
    el agente debe responder (RF-12, RF-16).

    Busca el número vinculado (activo o pausado); si no hay ninguno, el
    último desvinculado. La línea de avisos se trata como cualquier otro
    número vinculado.
    """
    identificador = (phone_number_id or "").strip()
    ahora = _ahora(ahora)
    if not identificador:
        return ResultadoEntrada()
    async with conexion() as conn:
        fila = await conn.fetchrow(
            """
            SELECT *
              FROM (
                SELECT *
                  FROM agentes_canal
                 WHERE phone_number_id = $1
                   AND estado IN ('activo', 'pausado')
                UNION ALL
                SELECT *
                  FROM agentes_canal
                 WHERE phone_number_id = $1
                   AND estado = 'desvinculado'
              ) x
             ORDER BY
                 CASE WHEN estado = 'desvinculado' THEN 1 ELSE 0 END,
                 vinculado_en DESC, id
             LIMIT 1
            """,
            identificador,
        )
    if fila is None:
        return ResultadoEntrada()
    if fila["estado"] == "desvinculado":
        return ResultadoEntrada(numero=fila, motivo="numero_desvinculado")
    if fila["estado"] != "activo" or not fila["agente_activo"]:
        return ResultadoEntrada(numero=fila, motivo="numero_no_activo")
    if configuracion_incompleta(fila["agent_name"], fila["system_prompt"]):
        return ResultadoEntrada(numero=fila, motivo="configuracion_incompleta")
    return ResultadoEntrada(numero=fila)


async def numero_del_negocio(
    conn: asyncpg.Connection, tenant_id: UUID, numero_id: UUID, *, bloquear: bool = False
) -> asyncpg.Record:
    """
    El número vinculado (activo o pausado) del negocio, o 404 `numero_no_encontrado`.
    Igual para uno ajeno, uno desvinculado y uno inexistente (RF-4, A5).
    """
    fila = await conn.fetchrow(
        f"""
        SELECT * FROM agentes_canal
        WHERE id = $1 AND tenant_id = $2 AND estado IN ('activo', 'pausado')
        {"FOR UPDATE" if bloquear else ""}
        """,
        numero_id,
        tenant_id,
    )
    if fila is None:
        raise error_http(404, "numero_no_encontrado")
    return fila


async def reglas_del_negocio(
    conn: asyncpg.Connection, tenant_id: UUID, ahora: Optional[datetime] = None
) -> ReglasNegocio:
    """Cupo, límites y familias que rigen al negocio hoy (RF-9, RF-10, RF-11), de `limites_efectivos`."""
    fila = await conn.fetchrow("SELECT * FROM limites_efectivos($1, $2)", tenant_id, _ahora(ahora))
    return ReglasNegocio(
        plan=fila["plan"],
        tope=fila["tope"],
        modelos=tuple(fila["modelos"]) if fila["modelos"] else None,
        historial_maximo=fila["historial"],
        familias=frozenset(fila["familias"]),
        sin_plan_activo=bool(fila["sin_plan_activo"]),
    )


# ============================================================
# Lectura
# ============================================================
@dataclass(frozen=True)
class ListaNumeros:
    numeros: list[dict[str, Any]]
    # `tope`: cupo del plan (None = sin tope). `plazas_usadas`: activos que cuentan.
    tope: Optional[int]
    plazas_usadas: int
    # None cuando no hay tope.
    plazas_libres: Optional[int]


async def listar(conn: asyncpg.Connection, tenant_id: UUID) -> ListaNumeros:
    """Los números vinculados con su estado, marcas y herramientas activas, y las plazas (P17)."""
    filas = await conn.fetch(
        """
        SELECT a.*, configuracion_incompleta(a.agent_name, a.system_prompt) AS configuracion_incompleta,
               ARRAY(SELECT h.tool_key FROM agente_canal_herramientas h
                      WHERE h.agente_canal_id = a.id ORDER BY h.tool_key) AS herramientas
        FROM agentes_canal a
        WHERE a.tenant_id = $1 AND a.estado IN ('activo', 'pausado')
        ORDER BY a.vinculado_en, a.id
        """,
        tenant_id,
    )
    reglas = await reglas_del_negocio(conn, tenant_id)
    usadas = _plazas(filas)
    libres = None if reglas.tope is None else max(reglas.tope - usadas, 0)
    return ListaNumeros([dict(f) for f in filas], reglas.tope, usadas, libres)


# ============================================================
# Principal y referencia
# ============================================================
async def refrescar_referencia(conn: asyncpg.Connection, agente_canal_id: UUID) -> None:
    """
    Reescribe la configuración de referencia del negocio con la del número, que
    tiene que ser su principal (si no lo es, no hace nada: la referencia nunca
    viene de la línea de avisos ni de un número cualquiera). Incluye el conjunto
    de herramientas.
    """
    await conn.execute(
        """
        INSERT INTO config_referencia_negocio
            (tenant_id, agent_name, system_prompt, model, temperature, history_window,
             agente_activo, herramientas, updated_at)
        SELECT a.tenant_id, a.agent_name, a.system_prompt, a.model, a.temperature,
               a.history_window, a.agente_activo,
               COALESCE((SELECT array_agg(h.tool_key ORDER BY h.tool_key)
                           FROM agente_canal_herramientas h
                          WHERE h.agente_canal_id = a.id), '{}'),
               NOW()
        FROM agentes_canal a
        WHERE a.id = $1 AND a.es_principal
        ON CONFLICT (tenant_id) DO UPDATE SET
            agent_name = EXCLUDED.agent_name,
            system_prompt = EXCLUDED.system_prompt,
            model = EXCLUDED.model,
            temperature = EXCLUDED.temperature,
            history_window = EXCLUDED.history_window,
            agente_activo = EXCLUDED.agente_activo,
            herramientas = EXCLUDED.herramientas,
            updated_at = NOW()
        """,
        agente_canal_id,
    )


async def aplicar_regla_principal(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    autor: Autor = SISTEMA,
    *,
    principal_previo: Optional[UUID] = None,
) -> Optional[UUID]:
    """
    Regla única de asignación del principal (D34, D33; `numeros_reglas.principal_resultante`).
    Se llama después de cualquier evento que cambie el conjunto de números activos con
    configuración completa: vinculación, reactivación, configuración que pasa a completa,
    y salida del principal por pausa, baja o configuración que pasa a incompleta.

    `principal_previo` es el principal que la acción acaba de quitar en este mismo
    paso (por eso la base ya no lo tiene): si no queda ninguno, se anota
    `principal_sin_numero_activo` (RF-15). Sin ese dato, no se repite la anotación en
    cada evento de un negocio que ya estaba sin principal.

    Devuelve el principal resultante. Cuando cambia, reescribe la referencia del negocio.
    Cuando no queda ninguno, la referencia se conserva (RF-5).
    """
    async with conn.transaction():
        await _bloquear_negocio(conn, tenant_id)
        filas = await _vinculados(conn, tenant_id)
        actual = next((f["id"] for f in filas if f["es_principal"]), None)
        nuevo = principal_resultante([_a_numero_canal(f) for f in filas], actual)
        antes = actual or principal_previo

        if nuevo == actual:
            if nuevo is None and principal_previo is not None:
                await registrar(
                    conn, tenant_id, None, "principal_sin_numero_activo", autor,
                    {"anterior": str(principal_previo)},
                )
            return nuevo

        if actual is not None:
            await conn.execute(
                "UPDATE agentes_canal SET es_principal = false, updated_at = NOW() WHERE id = $1", actual
            )
        if nuevo is None:
            await registrar(
                conn, tenant_id, None, "principal_sin_numero_activo", autor,
                {"anterior": str(antes) if antes else None},
            )
            return None

        await conn.execute(
            "UPDATE agentes_canal SET es_principal = true, updated_at = NOW() WHERE id = $1", nuevo
        )
        await refrescar_referencia(conn, nuevo)
        await registrar(
            conn, tenant_id, nuevo, "cambio_de_principal", autor,
            {"anterior": str(antes) if antes else None, "automatico": True},
        )
        return nuevo


async def elegir_principal(
    conn: asyncpg.Connection, tenant_id: UUID, numero_id: UUID, autor: Autor
) -> asyncpg.Record:
    """
    RF-15, D4: la gerencia elige el principal entre los números activos con
    configuración completa. 404 si no es del negocio; 403
    `linea_avisos_gestionada_por_plataforma` si es la línea de avisos; 409
    `numero_no_activo` si está pausado (CL-15, CL-21); 409 `configuracion_incompleta`
    si está incompleto (D41). Reescribe la referencia del negocio.
    """
    async with conn.transaction():
        await _bloquear_negocio(conn, tenant_id)
        numero = await numero_del_negocio(conn, tenant_id, numero_id, bloquear=True)
        if numero["es_linea_avisos"]:
            raise error_http(403, "linea_avisos_gestionada_por_plataforma")
        if numero["estado"] != "activo":
            raise error_http(409, "numero_no_activo")
        if configuracion_incompleta(numero["agent_name"], numero["system_prompt"]):
            raise error_http(409, "configuracion_incompleta")

        if not numero["es_principal"]:
            anterior = await conn.fetchval(
                "UPDATE agentes_canal SET es_principal = false, updated_at = NOW() "
                "WHERE tenant_id = $1 AND es_principal RETURNING id",
                tenant_id,
            )
            await conn.execute(
                "UPDATE agentes_canal SET es_principal = true, updated_at = NOW() WHERE id = $1", numero_id
            )
            await refrescar_referencia(conn, numero_id)
            await registrar(
                conn, tenant_id, numero_id, "cambio_de_principal", autor,
                {"anterior": str(anterior) if anterior else None, "automatico": False},
            )
        return await conn.fetchrow("SELECT * FROM agentes_canal WHERE id = $1", numero_id)


# ============================================================
# Línea de avisos de la plataforma (P11)
# ============================================================
def tenant_de_avisos() -> Optional[UUID]:
    """El negocio que aloja la línea de avisos (`AVISOS_CUENTA_GLOBAL_TENANT_ID`), o None."""
    valor = (settings.AVISOS_CUENTA_GLOBAL_TENANT_ID or "").strip()
    if not valor:
        return None
    try:
        return UUID(valor)
    except ValueError:
        return None


async def sincronizar_linea_avisos(
    conn: asyncpg.Connection, tenant_id: Optional[UUID] = None, autor: Autor = SISTEMA
) -> Optional[UUID]:
    """
    Marca como línea de avisos el número del negocio de avisos cuyo `phone_number_id`
    coincide con el de su fila de `channel_credentials` (la que usa
    `cuenta_global_wa`), y desmarca cualquier otra marca. Se llama al arrancar la
    aplicación y tras cada alta de ese negocio. Devuelve el id de la línea, o None
    si no hay negocio configurado o la línea aún no está vinculada.

    La migración de datos (sql/59) no puede leer la variable de entorno y puede
    haber dejado la línea como principal de su negocio. Un CHECK impide que el
    principal sea la línea de avisos, así que primero se le quita `es_principal`
    (en el mismo UPDATE que la marca) y después se aplica la regla única. Si no
    queda ningún principal se borra la referencia del negocio: venía de la línea
    de avisos y la referencia nunca puede venir de ella (el negocio vuelve a
    usar su configuración de Instagram y Facebook como base).
    """
    tenant = tenant_id or tenant_de_avisos()
    if tenant is None:
        return None
    async with conn.transaction():
        await _bloquear_negocio(conn, tenant)
        pnid = await conn.fetchval(
            """
            SELECT phone_number_id FROM channel_credentials
            WHERE tenant_id = $1 AND channel_type = 'whatsapp' AND NULLIF(phone_number_id, '') IS NOT NULL
            """,
            tenant,
        )
        # Una marca vieja (cambió la variable de entorno o el número) deja de valer.
        otras = await conn.fetch(
            """
            UPDATE agentes_canal SET es_linea_avisos = false, updated_at = NOW()
            WHERE es_linea_avisos
              AND NOT (tenant_id = $1 AND phone_number_id IS NOT DISTINCT FROM $2)
            RETURNING tenant_id
            """,
            tenant,
            pnid,
        )
        for ajeno in {f["tenant_id"] for f in otras}:
            await aplicar_regla_principal(conn, ajeno, autor)

        if pnid is None:
            return None
        fila = await conn.fetchrow(
            """
            SELECT id, es_principal, es_linea_avisos FROM agentes_canal
            WHERE tenant_id = $1 AND phone_number_id = $2 AND estado IN ('activo', 'pausado')
            FOR UPDATE
            """,
            tenant,
            pnid,
        )
        if fila is None:
            return None
        if not fila["es_linea_avisos"] or fila["es_principal"]:
            await conn.execute(
                "UPDATE agentes_canal SET es_principal = false, es_linea_avisos = true, updated_at = NOW() WHERE id = $1",
                fila["id"],
            )
            if fila["es_principal"]:
                principal = await aplicar_regla_principal(conn, tenant, autor, principal_previo=fila["id"])
                if principal is None:
                    await conn.execute("DELETE FROM config_referencia_negocio WHERE tenant_id = $1", tenant)
        return fila["id"]


# ============================================================
# Alta (RF-2)
# ============================================================
async def _fuente_de_copia(
    conn: asyncpg.Connection, tenant_id: UUID
) -> tuple[dict[str, Any], list[str]]:
    """
    Configuración y herramientas con las que nace un número nuevo (RF-5, RF-6, P4):
    la del principal; si no hay, la configuración de referencia del negocio; si
    tampoco, `tenant_agent_config` (así el cuestionario de bienvenida sigue
    sirviendo) con las herramientas habilitadas del negocio. Nunca viene de la
    línea de avisos (no puede ser principal ni referencia).
    """
    principal = await conn.fetchrow(
        """
        SELECT id, agent_name, system_prompt, model, temperature, history_window, agente_activo
        FROM agentes_canal
        WHERE tenant_id = $1 AND es_principal AND estado = 'activo'
        """,
        tenant_id,
    )
    if principal is not None:
        claves = await herramientas_plan.herramientas_de_numero(conn, tenant_id, principal["id"])
        return dict(principal), claves

    referencia = await conn.fetchrow(
        """
        SELECT agent_name, system_prompt, model, temperature, history_window, agente_activo, herramientas
        FROM config_referencia_negocio WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if referencia is not None:
        return dict(referencia), list(referencia["herramientas"] or [])

    base = await conn.fetchrow(
        """
        SELECT agent_name, system_prompt, model, temperature, history_window, is_active AS agente_activo
        FROM tenant_agent_config WHERE tenant_id = $1
        """,
        tenant_id,
    )
    claves = [
        f["tool_key"]
        for f in await conn.fetch(
            "SELECT tool_key FROM tenant_tools WHERE tenant_id = $1 AND is_enabled = true ORDER BY tool_key",
            tenant_id,
        )
    ]
    if base is not None:
        return dict(base), claves
    return (
        {
            "agent_name": "",
            "system_prompt": "",
            "model": None,
            "temperature": Decimal("0.70"),
            "history_window": 30,
            "agente_activo": True,
        },
        claves,
    )


async def _insertar_numero(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    phone_number_id: str,
    telefono_visible: Optional[str],
    bsp_provider: str,
    ahora: datetime,
    reglas: ReglasNegocio,
) -> asyncpg.Record:
    """Da de alta el número como activo, con la copia de configuración y herramientas de RF-5 y RF-6."""
    config, claves = await _fuente_de_copia(conn, tenant_id)

    modelo = config["model"]
    if reglas.modelos and modelo is not None and modelo not in reglas.modelos:
        modelo = reglas.modelos[0]
    historial = recorte(int(config["history_window"]), reglas.historial_maximo)

    nuevo = await conn.fetchrow(
        """
        INSERT INTO agentes_canal
            (tenant_id, phone_number_id, telefono_visible, bsp_provider, estado, vinculado_en,
             agent_name, system_prompt, model, temperature, history_window, agente_activo)
        VALUES ($1, $2, $3, $4, 'activo', $5, $6, $7, $8, $9, $10, $11)
        RETURNING *
        """,
        tenant_id,
        phone_number_id,
        telefono_visible,
        bsp_provider,
        ahora,
        config["agent_name"] or "",
        config["system_prompt"] or "",
        modelo,
        config["temperature"],
        historial,
        bool(config["agente_activo"]),
    )

    # Solo herramientas que existen, están habilitadas y que el plan incluye (RF-6).
    if claves:
        habilitadas = await conn.fetch(
            "SELECT tool_key, tool_type FROM tenant_tools "
            "WHERE tenant_id = $1 AND is_enabled = true AND tool_key = ANY($2::text[])",
            tenant_id,
            claves,
        )
        validas = [
            h["tool_key"]
            for h in habilitadas
            if (fam := herramientas_plan.familia_de(h["tool_type"], h["tool_key"])) is not None
            and permite_activar(fam, reglas.familias)
        ]
        if validas:
            await conn.execute(
                "INSERT INTO agente_canal_herramientas (agente_canal_id, tenant_id, tool_key) "
                "SELECT $1, $2, unnest($3::text[]) ON CONFLICT DO NOTHING",
                nuevo["id"],
                tenant_id,
                validas,
            )
    return nuevo


async def _registrar_intento_ajeno(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    phone_number_id: str,
    autor: Autor,
    ahora: datetime,
    intentos_previos: int,
) -> None:
    """RF-2 paso 4, P9: cuenta el intento y lo deja en la bitácora de la plataforma (4 últimos dígitos)."""
    await conn.execute(
        "INSERT INTO intentos_alta_ajena (tenant_id, creado_en) VALUES ($1, $2)", tenant_id, ahora
    )
    detalle = {"ultimos_digitos": ultimos_digitos(phone_number_id)}
    actor = dict(
        actor_email=autor.email or "desconocido",
        actor_portal_user_id=autor.portal_user_id,
        tenant_id=tenant_id,
        conn=conn,
    )
    await registrar_auditoria(accion="alta_ajena_rechazada", detalle=detalle, **actor)
    # "cada bloqueo de altas que empieza": el intento que completa los cinco.
    if intentos_previos + 1 == MAX_INTENTOS_ALTA_AJENA:
        await registrar_auditoria(
            accion="altas_bloqueadas",
            detalle={"intentos": MAX_INTENTOS_ALTA_AJENA, **detalle},
            **actor,
        )


async def _datos_alta(
    conn: asyncpg.Connection, tenant_id: UUID, ahora: datetime
) -> tuple[ReglasNegocio, int, list[datetime]]:
    """Reglas, plazas usadas e intentos recientes para evaluar un alta (RF-2)."""
    reglas = await reglas_del_negocio(conn, tenant_id, ahora)
    vinculados = await _vinculados(conn, tenant_id)
    intentos = [
        f["creado_en"]
        for f in await conn.fetch(
            "SELECT creado_en FROM intentos_alta_ajena WHERE tenant_id = $1 AND creado_en > $2",
            tenant_id,
            ahora - VENTANA_INTENTOS,
        )
    ]
    return reglas, _plazas(vinculados), intentos


def lanzar_rechazo_alta(decision: ResultadoAlta, ahora: datetime) -> NoReturn:
    """Lanza el 429 (con `libera_en` y `Retry-After`), 402, 409 o 404 de un alta."""
    if decision.estado_http == 429 and decision.libera_en is not None:
        espera = max(1, math.ceil((decision.libera_en - ahora).total_seconds()))
        raise error_http(
            429,
            "demasiados_intentos",
            libera_en=decision.libera_en.isoformat(),
            headers={"Retry-After": str(espera)},
        )
    raise error_http(decision.estado_http, str(decision.codigo))


async def comprobar_alta_previa(
    conn: asyncpg.Connection, tenant_id: UUID, *, ahora: Optional[datetime] = None
) -> None:
    """
    RF-2 pasos 1 y 2 para `iniciar_neuroapi_connect`: responde 429
    (`demasiados_intentos`) y 402 (`cupo_numeros_whatsapp`) ANTES de abrir la
    sesión. El resultado final del alta llega por el webhook (T6).
    """
    ahora = _ahora(ahora)
    reglas, plazas, intentos = await _datos_alta(conn, tenant_id, ahora)
    decision = evaluar_alta(
        intentos_recientes=intentos,
        ahora=ahora,
        plazas_usadas=plazas,
        tope=reglas.tope,
        relacion_numero="libre",
    )
    if not decision.permitido:
        lanzar_rechazo_alta(decision, ahora)


@dataclass(frozen=True)
class ResultadoProveedor:
    """Lo que pasó con un alta por el webhook (sin autor humano: SISTEMA)."""

    resultado: Literal["vinculado", "propio", "ajeno", "rechazado"]
    codigo: str | None
    estado_http: int | None
    phone_number_id: str
    libera_en: Optional[datetime] = None


async def vincular_desde_proveedor(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    phone_number_id: str,
    *,
    telefono_visible: Optional[str] = None,
    bsp_provider: str = "neuroapi",
    ahora: Optional[datetime] = None,
) -> ResultadoProveedor:
    """
    Alta por el webhook de NeuroAPI (T6): un número libre se vincula; uno
    propio se ignora (sin duplicar ni reactivar); uno ajeno se rechaza sin
    revelar (404 que cuenta para los intentos, RF-2 paso 4). Cupo lleno o
    bloqueo quedan como `rechazado` con su código. Nunca lanza.
    """
    identificador = (phone_number_id or "").strip()
    ahora = _ahora(ahora)
    if not identificador:
        return ResultadoProveedor("rechazado", "numero_no_valido", 400, phone_number_id or "")
    try:
        await vincular(
            conn,
            tenant_id,
            identificador,
            SISTEMA,
            ahora=ahora,
            telefono_visible=telefono_visible,
            bsp_provider=bsp_provider,
        )
    except HTTPException as e:
        codigo = e.detail.get("codigo") if isinstance(e.detail, dict) else None
        if e.status_code == 409 and codigo == "numero_ya_vinculado":
            return ResultadoProveedor("propio", codigo, 409, identificador)
        if e.status_code == 404 and codigo == "numero_no_encontrado":
            return ResultadoProveedor("ajeno", codigo, 404, identificador)
        libera = None
        if isinstance(e.detail, dict) and e.detail.get("libera_en"):
            try:
                libera = datetime.fromisoformat(str(e.detail["libera_en"]))
            except ValueError:
                libera = None
        return ResultadoProveedor("rechazado", codigo, e.status_code, identificador, libera)
    return ResultadoProveedor("vinculado", None, 201, identificador)


async def vincular(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    phone_number_id: str,
    autor: Autor,
    *,
    ahora: Optional[datetime] = None,
    telefono_visible: Optional[str] = None,
    bsp_provider: str = "neuroapi",
) -> asyncpg.Record:
    """
    La gerencia vincula un número. Evalúa en el orden de RF-2 (A68; el primero que
    aplica decide, `numeros_reglas.evaluar_alta`):

      429 `demasiados_intentos`  (con `libera_en` y la cabecera Retry-After)
      402 `cupo_numeros_whatsapp`
      409 `numero_ya_vinculado`  (propio, activo o pausado; no se duplica ni se reactiva)
      404 `numero_no_encontrado` (ajeno; cuenta como intento y queda en gerencia_auditoria)
      alta como activo           (libre, incluido uno que la plataforma no conoce)

    El bloqueo por negocio hace que dos altas simultáneas con una plaza solo
    completen una (CL-5). Si dos negocios se disputan el mismo número a la vez, el
    índice único de sql/56 decide y el perdedor recibe el 404 de alta ajena.
    Tras el alta: sincroniza la línea de avisos si el negocio es el de avisos,
    aplica la regla única del principal y anota `vinculacion`.
    """
    identificador = (phone_number_id or "").strip()
    if not identificador:
        raise error_http(400, "numero_no_valido")
    ahora = _ahora(ahora)

    nuevo: Optional[asyncpg.Record] = None
    rechazo: Optional[ResultadoAlta] = None
    async with conn.transaction():
        await _bloquear_negocio(conn, tenant_id)
        reglas, plazas, intentos = await _datos_alta(conn, tenant_id, ahora)
        dueno = await conn.fetchval(
            "SELECT tenant_id FROM agentes_canal WHERE phone_number_id = $1 AND estado IN ('activo', 'pausado')",
            identificador,
        )
        relacion = "libre" if dueno is None else ("propio" if dueno == tenant_id else "ajeno")

        decision = evaluar_alta(
            intentos_recientes=intentos,
            ahora=ahora,
            plazas_usadas=plazas,
            tope=reglas.tope,
            relacion_numero=relacion,
        )

        if decision.permitido:
            try:
                async with conn.transaction():
                    nuevo = await _insertar_numero(
                        conn, tenant_id, identificador, telefono_visible, bsp_provider, ahora, reglas
                    )
            except asyncpg.UniqueViolationError:
                # Otro negocio lo vinculó justo antes: para este es un número ajeno.
                decision = ResultadoAlta(False, 404, "numero_no_encontrado", cuenta_como_intento=True)

        if not decision.permitido:
            rechazo = decision
            if decision.cuenta_como_intento:
                await _registrar_intento_ajeno(conn, tenant_id, identificador, autor, ahora, len(intentos))
        else:
            assert nuevo is not None
            await registrar(
                conn, tenant_id, nuevo["id"], "vinculacion", autor,
                {"ultimos_digitos": ultimos_digitos(identificador)},
            )
            if tenant_id == tenant_de_avisos():
                await sincronizar_linea_avisos(conn, tenant_id, autor)
            await aplicar_regla_principal(conn, tenant_id, autor)
            nuevo = await conn.fetchrow("SELECT * FROM agentes_canal WHERE id = $1", nuevo["id"])

    if rechazo is not None:
        lanzar_rechazo_alta(rechazo, ahora)
    assert nuevo is not None
    return nuevo


# ============================================================
# Pausa, reactivación y baja (RF-14, RF-15)
# ============================================================
@dataclass(frozen=True)
class ResultadoPausa:
    numero: asyncpg.Record
    # La pausa dejó al negocio sin números activos: la confirmación advierte que
    # no responderá por WhatsApp (D5, A44). La línea de avisos no cuenta.
    sin_numero_activo: bool


async def pausar_manual(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    numero_id: UUID,
    autor: Autor,
    *,
    ahora: Optional[datetime] = None,
) -> ResultadoPausa:
    """
    RF-15, D5: pausa a mano un número activo. Libera la plaza, anota `pausa_manual` y
    NO genera aviso. Se permite pausar el último activo. 404 si no es del negocio;
    403 `linea_avisos_gestionada_por_plataforma` si es la línea de avisos. Si ya
    estaba pausado no hace nada. Si era el principal, lo sustituye la regla única.
    """
    ahora = _ahora(ahora)
    async with conn.transaction():
        await _bloquear_negocio(conn, tenant_id)
        numero = await numero_del_negocio(conn, tenant_id, numero_id, bloquear=True)
        if numero["es_linea_avisos"]:
            raise error_http(403, "linea_avisos_gestionada_por_plataforma")
        if numero["estado"] == "activo":
            await conn.execute(
                """
                UPDATE agentes_canal
                SET es_principal = false, estado = 'pausado', pausado_en = $2,
                    motivo_pausa = 'pausa_manual', updated_at = NOW()
                WHERE id = $1
                """,
                numero_id,
                ahora,
            )
            await registrar(conn, tenant_id, numero_id, "pausa_manual", autor, {})
            await aplicar_regla_principal(
                conn, tenant_id, autor, principal_previo=numero_id if numero["es_principal"] else None
            )
        fila = await conn.fetchrow("SELECT * FROM agentes_canal WHERE id = $1", numero_id)
        quedan = _plazas(await _vinculados(conn, tenant_id))
    return ResultadoPausa(fila, quedan == 0)


async def reactivar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    numero_id: UUID,
    autor: Autor,
    *,
    ahora: Optional[datetime] = None,
) -> asyncpg.Record:
    """
    RF-15: acción manual de la gerencia. Solo si cabe en el cupo vigente; si no, 402
    `cupo_numeros_whatsapp` y el número sigue pausado (CL-3). Conserva su antigüedad
    y su configuración. Si queda con configuración incompleta pasa a activo
    incompleto (cuenta para el cupo, no responde) y la gerencia recibe el aviso (A21).
    Aplica la regla única del principal (un reactivado no recupera el papel si el
    negocio ya tiene principal). Un número ya activo no cambia.
    """
    ahora = _ahora(ahora)
    incompleto = False
    async with conn.transaction():
        await _bloquear_negocio(conn, tenant_id)
        numero = await numero_del_negocio(conn, tenant_id, numero_id, bloquear=True)
        if numero["estado"] == "pausado":
            reglas = await reglas_del_negocio(conn, tenant_id, ahora)
            usadas = _plazas(await _vinculados(conn, tenant_id))
            if not numero["es_linea_avisos"] and reglas.tope is not None and usadas >= reglas.tope:
                raise error_http(402, "cupo_numeros_whatsapp")
            await conn.execute(
                """
                UPDATE agentes_canal
                SET estado = 'activo', pausado_en = NULL, motivo_pausa = NULL, updated_at = NOW()
                WHERE id = $1
                """,
                numero_id,
            )
            await registrar(conn, tenant_id, numero_id, "reactivacion_manual", autor, {})
            incompleto = configuracion_incompleta(numero["agent_name"], numero["system_prompt"])
            if incompleto:
                await registrar(
                    conn, tenant_id, numero_id, "configuracion_incompleta", autor, {"origen": "reactivacion"}
                )
            await aplicar_regla_principal(conn, tenant_id, autor)
        fila = await conn.fetchrow("SELECT * FROM agentes_canal WHERE id = $1", numero_id)

    if incompleto:
        await numeros_avisos.avisar_incompleto(conn, tenant_id, [fila], "reactivacion")
    return fila


async def desvincular(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    numero_id: UUID,
    autor: Autor,
    *,
    ahora: Optional[datetime] = None,
) -> int:
    """
    RF-14: la plaza queda libre y la pertenencia termina. Las conversaciones del
    número quedan ocultas y asignadas a la cuenta owner creadora del negocio (o su
    relevo, D23; `conversaciones_ocultas`). Anota `desvinculacion`; NO genera aviso
    (la pidió la gerencia). 404 si no es del negocio; 403
    `linea_avisos_gestionada_por_plataforma` si es la línea de avisos. Si era el
    principal, lo sustituye la regla única. Devuelve cuántas conversaciones ocultó.
    No toca `channel_credentials` (T6). El número puede vincularse de nuevo; sus
    conversaciones viejas no reaparecen.
    """
    ahora = _ahora(ahora)
    async with conn.transaction():
        await _bloquear_negocio(conn, tenant_id)
        numero = await numero_del_negocio(conn, tenant_id, numero_id, bloquear=True)
        if numero["es_linea_avisos"]:
            raise error_http(403, "linea_avisos_gestionada_por_plataforma")
        await conn.execute(
            """
            UPDATE agentes_canal
            SET es_principal = false, estado = 'desvinculado', desvinculado_en = $2, updated_at = NOW()
            WHERE id = $1
            """,
            numero_id,
            ahora,
        )
        ocultas = await conversaciones_ocultas.asignar_al_responsable(conn, tenant_id, numero_id, ahora)
        await registrar(
            conn, tenant_id, numero_id, "desvinculacion", autor,
            {"ultimos_digitos": ultimos_digitos(numero["phone_number_id"]), "conversaciones_ocultas": ocultas},
        )
        await aplicar_regla_principal(
            conn, tenant_id, autor, principal_previo=numero_id if numero["es_principal"] else None
        )
    return ocultas


# ============================================================
# Configuración del agente de un número (RF-5, RF-7, RF-8)
# ============================================================
def validar_temperatura(valor: Any) -> Decimal:
    """RF-5, A11: entre 0 y 1, ambos incluidos. Fuera de rango (o no numérica): 400 `temperatura_fuera_de_rango`."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
        raise error_http(400, "temperatura_fuera_de_rango")
    if isinstance(valor, float) and not math.isfinite(valor):
        raise error_http(400, "temperatura_fuera_de_rango")
    numero = Decimal(str(valor))
    if numero < 0 or numero > 1:
        raise error_http(400, "temperatura_fuera_de_rango")
    return numero


async def editar_configuracion(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    numero_id: UUID,
    cambios: Mapping[str, Any],
    autor: Autor,
    *,
    ahora: Optional[datetime] = None,
) -> asyncpg.Record:
    """
    RF-5: edita solo los campos presentes en `cambios` (`CAMPOS_CONFIGURACION`) de un
    número activo o pausado, sin afectar a los demás. Errores: 400
    `temperatura_fuera_de_rango`; 402 `modelo_no_incluido_en_plan` /
    `historial_excede_plan` (los límites que rigen al negocio, RF-10); 404.
    Un modelo `None` significa "el default del plan" (D32) y siempre cabe.

    Bitácora (RF-7, RF-21): `prompt_cambiado` (con las longitudes, nunca el
    texto), `agente_encendido` / `agente_apagado`, `configuracion_incompleta` y
    `configuracion_completada`. Si un número ACTIVO pasa a incompleto, la gerencia
    recibe el aviso (RF-20); un pausado no. Un principal que pasa a incompleto deja
    de serlo (CL-31); un número que pasa a completo dispara la regla única. Si el
    principal sigue siéndolo, se reescribe la referencia del negocio.
    """
    desconocidos = set(cambios) - set(CAMPOS_CONFIGURACION)
    if desconocidos:
        raise ValueError(f"Campos no editables: {sorted(desconocidos)}")
    cambios = dict(cambios)
    if "temperature" in cambios:
        cambios["temperature"] = validar_temperatura(cambios["temperature"])
    ahora = _ahora(ahora)

    aviso_incompleto = False
    async with conn.transaction():
        await _bloquear_negocio(conn, tenant_id)
        numero = await numero_del_negocio(conn, tenant_id, numero_id, bloquear=True)
        nuevos = {campo: cambios.get(campo, numero[campo]) for campo in CAMPOS_CONFIGURACION}

        if "model" in cambios or "history_window" in cambios:
            reglas = await reglas_del_negocio(conn, tenant_id, ahora)
            limites = LimitesAgente(
                plan=reglas.plan, modelos=reglas.modelos, historial_maximo=reglas.historial_maximo
            )
            exigir_dentro_del_plan(
                limites,
                nuevos["model"] if nuevos["model"] is not None else limites.modelo_por_defecto,
                int(nuevos["history_window"]),
            )

        era_incompleta = configuracion_incompleta(numero["agent_name"], numero["system_prompt"])
        es_incompleta = configuracion_incompleta(nuevos["agent_name"], nuevos["system_prompt"])
        pierde_principal = bool(numero["es_principal"]) and es_incompleta

        await conn.execute(
            """
            UPDATE agentes_canal
            SET agent_name = $3, system_prompt = $4, model = $5, temperature = $6,
                history_window = $7, agente_activo = $8,
                es_principal = es_principal AND NOT $9::boolean,
                updated_at = NOW()
            WHERE id = $1 AND tenant_id = $2
            """,
            numero_id,
            tenant_id,
            nuevos["agent_name"],
            nuevos["system_prompt"],
            nuevos["model"],
            nuevos["temperature"],
            int(nuevos["history_window"]),
            bool(nuevos["agente_activo"]),
            pierde_principal,
        )

        if nuevos["system_prompt"] != numero["system_prompt"]:
            await registrar(
                conn, tenant_id, numero_id, "prompt_cambiado", autor,
                {
                    "longitud_anterior": len(numero["system_prompt"] or ""),
                    "longitud_nueva": len(nuevos["system_prompt"] or ""),
                },
            )
        if bool(nuevos["agente_activo"]) != bool(numero["agente_activo"]):
            await registrar(
                conn, tenant_id, numero_id,
                "agente_encendido" if nuevos["agente_activo"] else "agente_apagado", autor, {},
            )
        if es_incompleta and not era_incompleta:
            await registrar(conn, tenant_id, numero_id, "configuracion_incompleta", autor, {"origen": "edicion"})
            aviso_incompleto = numero["estado"] == "activo"
        elif era_incompleta and not es_incompleta:
            await registrar(conn, tenant_id, numero_id, "configuracion_completada", autor, {})

        if pierde_principal or (era_incompleta and not es_incompleta):
            await aplicar_regla_principal(
                conn, tenant_id, autor, principal_previo=numero_id if pierde_principal else None
            )
        elif numero["es_principal"]:
            await refrescar_referencia(conn, numero_id)

        fila = await conn.fetchrow("SELECT * FROM agentes_canal WHERE id = $1", numero_id)

    if aviso_incompleto:
        await numeros_avisos.avisar_incompleto(conn, tenant_id, [fila], "edicion")
    return fila
