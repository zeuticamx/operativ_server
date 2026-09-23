"""
Panel de plataforma, segunda parte: operar el servicio más que mirarlo.

Separado de routers/gerencia.py solo por tamaño; mismo prefijo, mismo
nivel de acceso (gerencia_users) y la misma regla de que toda acción que
cambia algo deja asiento en gerencia_auditoria dentro de su transacción.

  /gerencia/salud                         qué está roto ahora mismo
  /gerencia/alertas/{id}/revisar          cerrar una alerta de plataforma
  /gerencia/usuarios                      quién es del equipo de plataforma
  /gerencia/cohortes                      retención por mes de alta
  /gerencia/tenants/{id}/impersonar       ver el portal como el negocio
"""

from datetime import date
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from config import settings
from deps import UsuarioActual, gerencia_plataforma_actual
from schemas import (
    AlertaGerenciaOut,
    CohorteOut,
    CohortesOut,
    GerenciaUsuarioIn,
    GerenciaUsuarioOut,
    ImpersonarIn,
    ImpersonarOut,
    ProblemaSaludOut,
    SaludOut,
)
from security import crear_token_impersonacion
from services.gerencia import registrar_auditoria
from services.gerencia_salud import problemas_de_salud
from session import fetch_all, fetch_value, transaccion

router = APIRouter(
    prefix="/gerencia",
    tags=["gerencia"],
    dependencies=[Depends(gerencia_plataforma_actual)],
)

MESES_COHORTE_POR_DEFECTO = 12
MESES_COHORTE_MAXIMO = 24


# ============================================================
# Salud
# ============================================================
_SELECT_ALERTAS_ABIERTAS = """
    SELECT a.id, a.tipo, a.tenant_id, t.name AS tenant_nombre, a.titulo,
           a.detalle, a.creada_en, a.revisada_en, a.revisada_por
    FROM gerencia_alertas a
    LEFT JOIN tenants t ON t.id = a.tenant_id
    WHERE a.revisada_en IS NULL
    ORDER BY a.creada_en DESC
"""


def _alerta_out(f) -> AlertaGerenciaOut:
    return AlertaGerenciaOut(
        id=f["id"],
        tipo=f["tipo"],
        tenant_id=f["tenant_id"],
        tenant_nombre=f["tenant_nombre"],
        titulo=f["titulo"],
        detalle=f["detalle"],
        creada_en=f["creada_en"],
        revisada_en=f["revisada_en"],
        revisada_por=f["revisada_por"],
    )


@router.get("/salud", response_model=SaludOut)
async def salud():
    """
    Todo lo que hoy está roto o a punto de romperse, de todos los negocios,
    ordenado por severidad. Ver services/gerencia_salud.py para qué mira
    cada detector y por qué.
    """
    problemas = await problemas_de_salud()
    alertas = await fetch_all(_SELECT_ALERTAS_ABIERTAS)
    ahora = await fetch_value("SELECT NOW()")

    return SaludOut(
        generado_en=ahora,
        problemas=[
            ProblemaSaludOut(
                tipo=p.tipo,
                severidad=p.severidad,
                tenant_id=p.tenant_id,
                tenant_nombre=p.tenant_nombre,
                detalle=p.detalle,
                fecha=p.fecha,
                datos=p.datos,
            )
            for p in problemas
        ],
        alertas_abiertas=[_alerta_out(f) for f in alertas],
    )


@router.patch("/alertas/{alerta_id}/revisar", response_model=AlertaGerenciaOut)
async def revisar_alerta(
    alerta_id: UUID,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """
    Cierra una alerta. Al cerrarla, el índice único parcial la suelta y un
    pico nuevo del mismo negocio puede volver a abrir otra — que es lo que
    se quiere: "ya lo miré" no es "no me avises más".
    """
    async with transaccion() as conn:
        fila = await conn.fetchrow(
            """
            UPDATE gerencia_alertas
               SET revisada_en = NOW(), revisada_por = $2
             WHERE id = $1 AND revisada_en IS NULL
            RETURNING id, tipo, tenant_id, titulo, detalle, creada_en,
                      revisada_en, revisada_por,
                      (SELECT name FROM tenants WHERE id = gerencia_alertas.tenant_id)
                          AS tenant_nombre
            """,
            alerta_id,
            gerente.email,
        )
        if fila is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Alerta no encontrada o ya revisada",
            )

        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="alerta_revisada",
            tenant_id=fila["tenant_id"],
            detalle={"alerta": fila["titulo"], "tipo": fila["tipo"]},
            conn=conn,
        )

    return _alerta_out(fila)


# ============================================================
# Equipo de plataforma
# ============================================================
_SELECT_USUARIOS = """
    SELECT gu.id, gu.email, gu.full_name, gu.cargo, gu.created_at,
           pu.id IS NOT NULL AS tiene_cuenta_portal,
           pu.last_login_at::timestamptz AS ultimo_acceso
    FROM gerencia_users gu
    LEFT JOIN portal_users pu ON LOWER(pu.email) = LOWER(gu.email)
"""


def _usuario_out(f, yo: UsuarioActual) -> GerenciaUsuarioOut:
    return GerenciaUsuarioOut(
        id=f["id"],
        email=f["email"],
        full_name=f["full_name"],
        cargo=f["cargo"],
        creado_en=f["created_at"],
        tiene_cuenta_portal=f["tiene_cuenta_portal"],
        ultimo_acceso=f["ultimo_acceso"],
        es_yo=f["email"].lower() == yo.email.lower(),
    )


@router.get("/usuarios", response_model=list[GerenciaUsuarioOut])
async def listar_usuarios(gerente: UsuarioActual = Depends(gerencia_plataforma_actual)):
    filas = await fetch_all(_SELECT_USUARIOS + " ORDER BY gu.created_at")
    return [_usuario_out(f, gerente) for f in filas]


@router.post(
    "/usuarios",
    response_model=GerenciaUsuarioOut,
    status_code=status.HTTP_201_CREATED,
)
async def agregar_usuario(
    datos: GerenciaUsuarioIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """
    Da nivel gerencia a un correo. No crea cuenta de portal: la persona
    entra con la suya (correo o Google). Si todavía no tiene, el alta queda
    esperando y se activa sola el día que se registre con ese correo —
    `tiene_cuenta_portal` lo deja a la vista.
    """
    email = datos.email.lower()
    async with transaccion() as conn:
        try:
            nuevo_id = await conn.fetchval(
                """
                INSERT INTO gerencia_users (email, full_name, cargo)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                email,
                datos.full_name.strip(),
                datos.cargo.strip(),
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ese correo ya tiene nivel gerencia",
            )

        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="gerencia_alta",
            tenant_id=None,
            detalle={"email": email, "cargo": datos.cargo.strip()},
            conn=conn,
        )

        fila = await conn.fetchrow(_SELECT_USUARIOS + " WHERE gu.id = $1", nuevo_id)

    return _usuario_out(fila, gerente)


@router.delete("/usuarios/{usuario_id}", status_code=status.HTTP_204_NO_CONTENT)
async def quitar_usuario(
    usuario_id: UUID,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """
    Quita el nivel gerencia. Surte efecto en la siguiente petición de esa
    persona (deps.usuario_actual relee la tabla), no cuando expire su token.

    No se puede quitar a uno mismo. Además de evitar el "me saqué sin
    querer", garantiza que siempre quede al menos una persona con nivel
    gerencia: quien borra sigue estando, así que la tabla nunca queda vacía
    por esta vía — y una tabla vacía solo se arregla con SQL a mano.
    """
    async with transaccion() as conn:
        fila = await conn.fetchrow(
            "SELECT email, cargo FROM gerencia_users WHERE id = $1 FOR UPDATE",
            usuario_id,
        )
        if fila is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Usuario de gerencia no encontrado",
            )

        if fila["email"].lower() == gerente.email.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No puedes quitarte el nivel gerencia a ti mismo",
            )

        await conn.execute("DELETE FROM gerencia_users WHERE id = $1", usuario_id)

        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="gerencia_baja",
            tenant_id=None,
            detalle={"email": fila["email"], "cargo": fila["cargo"]},
            conn=conn,
        )


# ============================================================
# Cohortes
# ============================================================
@router.get("/cohortes", response_model=CohortesOut)
async def cohortes(
    meses: int = Query(MESES_COHORTE_POR_DEFECTO, ge=1, le=MESES_COHORTE_MAXIMO),
):
    """
    Retención mensual por mes de alta.

    "Activo" en un mes = al menos un mensaje ese mes. Se mide sobre
    mensajes y no sobre pagos a propósito: un negocio que paga y no usa ya
    está perdido aunque todavía no haya cancelado, y es justo lo que esta
    pantalla tiene que mostrar antes que la de ingresos.

    Recorre `messages` de los últimos `meses` meses para toda la plataforma.
    Con el volumen de hoy es instantáneo; si algún día pesa, el siguiente
    paso es una tabla de actividad mensual por tenant que se llene de a
    poco, no cachear esta respuesta.
    """
    filas_base = await fetch_all(
        """
        SELECT date_trunc('month', created_at)::date AS mes, COUNT(*) AS tamano
        FROM tenants
        WHERE created_at >= date_trunc('month', NOW()) - make_interval(months => $1 - 1)
        GROUP BY 1
        ORDER BY 1
        """,
        meses,
    )

    filas_actividad = await fetch_all(
        """
        WITH base AS (
            SELECT id AS tenant_id, date_trunc('month', created_at)::date AS cohorte
            FROM tenants
            WHERE created_at >= date_trunc('month', NOW()) - make_interval(months => $1 - 1)
        ),
        actividad AS (
            SELECT DISTINCT m.tenant_id, date_trunc('month', m.created_at)::date AS mes
            FROM messages m
            JOIN base b ON b.tenant_id = m.tenant_id
            WHERE m.created_at >= b.cohorte
        )
        SELECT
            b.cohorte,
            ((EXTRACT(YEAR FROM a.mes) - EXTRACT(YEAR FROM b.cohorte)) * 12
              + EXTRACT(MONTH FROM a.mes) - EXTRACT(MONTH FROM b.cohorte))::int AS desfase,
            COUNT(*) AS activos
        FROM base b
        JOIN actividad a ON a.tenant_id = b.tenant_id
        GROUP BY 1, 2
        """,
        meses,
    )

    mes_actual: date = await fetch_value("SELECT date_trunc('month', NOW())::date")

    activos_por: dict[tuple[date, int], int] = {
        (f["cohorte"], f["desfase"]): f["activos"] for f in filas_actividad
    }

    resultado: list[CohorteOut] = []
    for f in filas_base:
        mes: date = f["mes"]
        tamano: int = f["tamano"]
        # Columnas hasta el mes actual inclusive: la cohorte de hace tres
        # meses tiene cuatro (0, 1, 2, 3). El mes en curso va incompleto, y
        # la pantalla lo aclara; no se descarta porque es el que más
        # interesa en una cohorte nueva.
        columnas = (mes_actual.year - mes.year) * 12 + (mes_actual.month - mes.month) + 1
        activos = [activos_por.get((mes, i), 0) for i in range(columnas)]
        resultado.append(
            CohorteOut(
                mes=mes,
                tamano=tamano,
                activos=activos,
                retencion=[round(100.0 * a / tamano, 1) if tamano else 0.0 for a in activos],
            )
        )

    return CohortesOut(meses=meses, cohortes=resultado)


# ============================================================
# Ver como el negocio
# ============================================================
@router.post("/tenants/{tenant_id}/impersonar", response_model=ImpersonarOut)
async def impersonar(
    tenant_id: UUID,
    datos: ImpersonarIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """
    Token corto para ver el portal exactamente como lo ve el negocio, en
    solo lectura. Es para soporte: reproducir lo que el cliente dice que ve
    sin pedirle la contraseña ni una captura.

    Se entra como el dueño (owner; si no hay, el superadmin o un member
    activo, en ese orden). Nunca como un vendedor: su pantalla es la app de
    campo, no el portal.

    Las garantías viven en otros lados y conviene saber dónde:
      - solo lectura y revalidación del gerente: deps.usuario_actual
      - sin refresh: el token no viene con uno y /refresh no acepta access
      - el socket de alertas no deja marcarlas leídas: realtime.marcar_leida
      - el motivo queda en gerencia_auditoria: acá abajo

    Sirve también con negocios suspendidos, a propósito: es justo cuando
    más hace falta ver qué ve el cliente.
    """
    async with transaccion() as conn:
        tenant_nombre = await conn.fetchval("SELECT name FROM tenants WHERE id = $1", tenant_id)
        if tenant_nombre is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Negocio no encontrado",
            )

        usuario = await conn.fetchrow(
            """
            SELECT id, email, role
            FROM portal_users
            WHERE tenant_id = $1 AND is_active AND role IN ('owner', 'superadmin', 'member')
            ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'superadmin' THEN 1 ELSE 2 END,
                     created_at
            LIMIT 1
            """,
            tenant_id,
        )
        if usuario is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El negocio no tiene ningún usuario activo con acceso al portal",
            )

        token, expira = crear_token_impersonacion(
            usuario["id"],
            tenant_id,
            usuario["role"],
            gerente.id,
            settings.IMPERSONACION_MINUTOS,
        )

        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="impersonacion",
            tenant_id=tenant_id,
            detalle={
                "como": usuario["email"],
                "motivo": datos.motivo.strip(),
                "minutos": settings.IMPERSONACION_MINUTOS,
            },
            conn=conn,
        )

    return ImpersonarOut(
        access_token=token,
        expira_en=expira,
        email_usuario=usuario["email"],
        tenant_nombre=tenant_nombre,
    )
