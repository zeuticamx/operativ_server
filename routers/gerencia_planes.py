"""
Catálogo de planes (tabla `planes`), administrado desde el panel de
plataforma.

Es la misma tabla que lee `GET /api/pagos/catalogo` para pintar la
pantalla de suscripción del portal — aquella filtra `WHERE activo` porque
es lo que un tenant ve al contratar; acá gerencia necesita ver y tocar
también los planes apagados, así que no hay ese filtro.

Separado de routers/gerencia.py por tamaño, mismo nivel de acceso
(gerencia_users) y misma convención de bitácora: crear o actualizar un
plan deja asiento en gerencia_auditoria dentro de la misma transacción.

`nombre` es el identificador de la URL, no `id`: es UNIQUE en la tabla
(igual que en 11_mercado_pago.sql) y es lo único que el resto del sistema
usa para referenciar un plan — `tenant_subscriptions.plan` es FK a
planes(nombre) desde 25_planes_herramientas.sql (antes era un CHECK con los
tres nombres de fábrica, y por eso un plan creado acá no se podía
contratar). El renombre sigue sin exponerse (ver PlanActualizarIn), aunque
la FK ya lo haría seguro con su ON UPDATE CASCADE.

Además de precios y límites, cada plan dice qué herramientas del portal
incluye (`*_activo`): es la matriz que evalúa services/acceso_plan.py. También
qué modelos y cuánto historial puede dar al agente (`modelos_permitidos`,
`historial_maximo`: services/limites_agente.py). Bajar esos topes recorta de
inmediato la config de los negocios que ya tienen el plan (trigger en
45_limites_agente_plan.sql) y el número queda en la auditoría.

`stripe_price_id` es el Price recurrente creado en el panel de Stripe: lo
que de verdad se cobra cada mes (ver services/stripe_suscripciones.py). Sin
él, el plan no se puede contratar en línea.
"""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from deps import UsuarioActual, gerencia_plataforma_actual
from schemas import PlanActualizarIn, PlanCrearIn, PlanGerenciaOut
from services.gerencia import registrar_auditoria
from services.limites_agente import (
    HISTORIAL_MAX,
    HISTORIAL_MIN,
    MODELOS_AGENTE,
    contar_fuera_de_limite,
)
from session import fetch_all, transaccion

router = APIRouter(
    prefix="/gerencia/planes",
    tags=["gerencia"],
    dependencies=[Depends(gerencia_plataforma_actual)],
)

# alias a los nombres de columna que espera PlanGerenciaOut.
_COLUMNAS = """
    nombre, descripcion, precio_monthly, precio_annual,
    max_vendedores, max_proveedores, max_leads_mensuales, creditos_incluidos_mensual,
    agente_ia_activo, gestion_vendedores_activo,
    herramientas_activo, crm_campo_activo, calendario_activo, activo, orden,
    stripe_price_id, modelos_permitidos, historial_maximo,
    max_numeros_whatsapp, coexistencia_activa,
    created_at AS creado_en, updated_at AS actualizado_en
"""

# Familias del catálogo base de herramientas del agente (P5, sql/57).
_FAMILIAS_BASE = ("calendario", "sql_lectura", "http", "google_sheets", "google_docs")
_OTRAS_FAMILIAS = ("sql_lectura", "http", "google_sheets", "google_docs")


def _cupo_no_valido() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "codigo": "cupo_no_valido",
            "mensaje": "El cupo va en números enteros: null no cambia, 0 quita el tope y un entero mayor o igual que 1 lo fija.",
        },
    )


def _normalizar_cupo(valor: int | float | None) -> int | None:
    """None = sin tope; 0 = sin tope; entero >= 1 = tope. El resto es 400."""
    if valor is None:
        return None
    if isinstance(valor, bool):
        raise _cupo_no_valido()
    if isinstance(valor, int):
        entero = valor
    elif isinstance(valor, float):
        if not valor.is_integer():
            raise _cupo_no_valido()
        entero = int(valor)
    else:
        raise _cupo_no_valido()
    if entero == 0:
        return None
    if entero >= 1:
        return entero
    raise _cupo_no_valido()


def _lista_desde_flags(herramientas_activo: bool, calendario_activo: bool) -> list[str]:
    lista = [f for f in _FAMILIAS_BASE if (f == "calendario" and calendario_activo) or (f != "calendario" and herramientas_activo)]
    return lista


def _flags_desde_lista(lista: list[str]) -> tuple[bool, bool]:
    conjunto = set(lista)
    return (any(f in conjunto for f in _OTRAS_FAMILIAS), "calendario" in conjunto)


async def _leer_herramientas(conn, nombre: str) -> list[str]:
    filas = await conn.fetch(
        "SELECT familia FROM plan_herramientas WHERE plan_nombre = $1 ORDER BY familia", nombre
    )
    return [f["familia"] for f in filas]


def _out(f, herramientas: list[str] | None = None) -> PlanGerenciaOut:
    datos = dict(f)
    datos["herramientas_plan"] = sorted(herramientas) if herramientas is not None else []
    return PlanGerenciaOut(**datos)


@router.get("", response_model=list[PlanGerenciaOut])
async def listar_planes():
    """
    Todos los planes, activos e inactivos — a diferencia de
    /api/pagos/catalogo, que solo trae los que un tenant puede contratar
    hoy. Gerencia necesita ver los apagados también, para poder
    reactivarlos o confirmar que de verdad están fuera.
    """
    from session import fetch_all as _fetch_all

    filas = await fetch_all(f"SELECT {_COLUMNAS} FROM planes ORDER BY orden, precio_monthly")
    listas = await _fetch_all("SELECT plan_nombre, familia FROM plan_herramientas ORDER BY familia")
    por_plan: dict[str, list[str]] = {}
    for r in listas:
        por_plan.setdefault(r["plan_nombre"], []).append(r["familia"])
    return [_out(f, por_plan.get(dict(f)["nombre"], [])) for f in filas]


@router.get("/modelos-agente")
async def modelos_agente():
    """
    Catálogo de modelos que un plan puede permitir (barato → caro) y el rango
    del historial, para que el formulario del panel no los repita a mano.
    """
    return {
        "modelos": list(MODELOS_AGENTE),
        "historial_min": HISTORIAL_MIN,
        "historial_max": HISTORIAL_MAX,
    }


@router.get("/familias-herramientas")
async def familias_herramientas():
    """Catálogo base de familias de herramientas del agente (P5), para el panel."""
    return {"familias": list(_FAMILIAS_BASE)}


@router.post("", response_model=PlanGerenciaOut, status_code=status.HTTP_201_CREATED)
async def crear_plan(
    datos: PlanCrearIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    cupo = _normalizar_cupo(datos.max_numeros_whatsapp)
    if datos.herramientas_plan is None:
        lista = _lista_desde_flags(datos.herramientas_activo, datos.calendario_activo)
        herramientas_activo = datos.herramientas_activo
        calendario_activo = datos.calendario_activo
    else:
        lista = sorted(set(datos.herramientas_plan))
        herramientas_activo, calendario_activo = _flags_desde_lista(lista)
    async with transaccion() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO planes
                    (nombre, descripcion, precio_monthly, precio_annual,
                     max_vendedores, max_leads_mensuales, creditos_incluidos_mensual,
                     agente_ia_activo, gestion_vendedores_activo,
                     herramientas_activo, crm_campo_activo, calendario_activo,
                     activo, orden, stripe_price_id, max_proveedores,
                     modelos_permitidos, historial_maximo, max_numeros_whatsapp,
                     coexistencia_activa)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                        $17, $18, $19, $20)
                """,
                datos.nombre,
                datos.descripcion,
                datos.precio_monthly,
                datos.precio_annual,
                datos.max_vendedores,
                datos.max_leads_mensuales,
                datos.creditos_incluidos_mensual,
                datos.agente_ia_activo,
                datos.gestion_vendedores_activo,
                herramientas_activo,
                datos.crm_campo_activo,
                calendario_activo,
                datos.activo,
                datos.orden,
                datos.stripe_price_id,
                datos.max_proveedores,
                datos.modelos_permitidos,
                datos.historial_maximo,
                cupo,
                datos.coexistencia_activa,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ya existe un plan con ese nombre",
            )

        for familia in lista:
            await conn.execute(
                "INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                datos.nombre,
                familia,
            )

        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="plan_creado",
            tenant_id=None,
            detalle={
                "nombre": datos.nombre,
                "precio_monthly": str(datos.precio_monthly),
                "activo": datos.activo,
                "max_numeros_whatsapp": cupo,
                "coexistencia_activa": datos.coexistencia_activa,
                "herramientas_plan": lista,
            },
            conn=conn,
        )

        fila = await conn.fetchrow(f"SELECT {_COLUMNAS} FROM planes WHERE nombre = $1", datos.nombre)

    return _out(fila, lista)


@router.patch("/{nombre}", response_model=PlanGerenciaOut)
async def actualizar_plan(
    nombre: str,
    datos: PlanActualizarIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """
    Apagar un plan (`activo=false`) no toca a los negocios que ya lo
    tienen contratado: la FK de `tenant_subscriptions` solo exige que la
    fila exista, no que esté activa, así que una suscripción vigente sigue
    vigente. Solo deja de ofrecerse a
    quien contrate desde cero (`/api/pagos/catalogo` filtra por `activo`).
    """
    async with transaccion() as conn:
        anterior = await conn.fetchrow(f"SELECT {_COLUMNAS} FROM planes WHERE nombre = $1", nombre)
        if anterior is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Plan no encontrado",
            )
        lista_anterior = await _leer_herramientas(conn, nombre)
        # El cupo se valida antes de tocar nada: un 400 no cambia el plan.
        nuevo_cupo = (
            anterior["max_numeros_whatsapp"]
            if datos.max_numeros_whatsapp is None
            else _normalizar_cupo(datos.max_numeros_whatsapp)
        )
        if datos.herramientas_plan is None:
            nueva_lista = lista_anterior
            herramientas_activo = (
                anterior["herramientas_activo"]
                if datos.herramientas_activo is None
                else datos.herramientas_activo
            )
            calendario_activo = (
                anterior["calendario_activo"]
                if datos.calendario_activo is None
                else datos.calendario_activo
            )
        else:
            nueva_lista = sorted(set(datos.herramientas_plan))
            herramientas_activo, calendario_activo = _flags_desde_lista(nueva_lista)

        # None = no tocar ese campo (ver el docstring de PlanActualizarIn).
        nuevo = {
            "descripcion": anterior["descripcion"] if datos.descripcion is None else datos.descripcion,
            "precio_monthly": (
                anterior["precio_monthly"] if datos.precio_monthly is None else datos.precio_monthly
            ),
            "precio_annual": (
                anterior["precio_annual"] if datos.precio_annual is None else datos.precio_annual
            ),
            "max_vendedores": (
                anterior["max_vendedores"] if datos.max_vendedores is None else datos.max_vendedores
            ),
            "max_proveedores": (
                anterior["max_proveedores"] if datos.max_proveedores is None else datos.max_proveedores
            ),
            "max_leads_mensuales": (
                anterior["max_leads_mensuales"]
                if datos.max_leads_mensuales is None
                else datos.max_leads_mensuales
            ),
            "creditos_incluidos_mensual": (
                anterior["creditos_incluidos_mensual"]
                if datos.creditos_incluidos_mensual is None
                else datos.creditos_incluidos_mensual
            ),
            "agente_ia_activo": (
                anterior["agente_ia_activo"]
                if datos.agente_ia_activo is None
                else datos.agente_ia_activo
            ),
            "gestion_vendedores_activo": (
                anterior["gestion_vendedores_activo"]
                if datos.gestion_vendedores_activo is None
                else datos.gestion_vendedores_activo
            ),
            "herramientas_activo": herramientas_activo,
            "crm_campo_activo": (
                anterior["crm_campo_activo"]
                if datos.crm_campo_activo is None
                else datos.crm_campo_activo
            ),
            "calendario_activo": calendario_activo,
            # None = no tocar; [] / 0 = quitar el límite (ver PlanActualizarIn).
            "modelos_permitidos": (
                anterior["modelos_permitidos"]
                if datos.modelos_permitidos is None
                else (datos.modelos_permitidos or None)
            ),
            "historial_maximo": (
                anterior["historial_maximo"]
                if datos.historial_maximo is None
                else (datos.historial_maximo or None)
            ),
            "max_numeros_whatsapp": nuevo_cupo,
            "coexistencia_activa": (
                anterior["coexistencia_activa"]
                if datos.coexistencia_activa is None
                else datos.coexistencia_activa
            ),
            "activo": anterior["activo"] if datos.activo is None else datos.activo,
            "orden": anterior["orden"] if datos.orden is None else datos.orden,
            # None = no tocar; "" = borrarlo (ver PlanActualizarIn).
            "stripe_price_id": (
                anterior["stripe_price_id"]
                if datos.stripe_price_id is None
                else (datos.stripe_price_id or None)
            ),
        }

        # Cuántos negocios van a quedar recortados por los topes nuevos. Se
        # cuenta ANTES del UPDATE: el trigger de 45_limites_agente_plan.sql
        # los recorta en cuanto cambia la fila.
        recortados = await contar_fuera_de_limite(
            conn, nombre, nuevo["modelos_permitidos"], nuevo["historial_maximo"]
        )

        await conn.execute(
            """
            UPDATE planes SET
                descripcion = $2,
                precio_monthly = $3,
                precio_annual = $4,
                max_vendedores = $5,
                max_leads_mensuales = $6,
                creditos_incluidos_mensual = $7,
                agente_ia_activo = $8,
                gestion_vendedores_activo = $9,
                herramientas_activo = $10,
                crm_campo_activo = $11,
                calendario_activo = $12,
                activo = $13,
                orden = $14,
                stripe_price_id = $15,
                max_proveedores = $16,
                modelos_permitidos = $17,
                historial_maximo = $18,
                max_numeros_whatsapp = $19,
                coexistencia_activa = $20,
                updated_at = NOW()
            WHERE nombre = $1
            """,
            nombre,
            nuevo["descripcion"],
            nuevo["precio_monthly"],
            nuevo["precio_annual"],
            nuevo["max_vendedores"],
            nuevo["max_leads_mensuales"],
            nuevo["creditos_incluidos_mensual"],
            nuevo["agente_ia_activo"],
            nuevo["gestion_vendedores_activo"],
            nuevo["herramientas_activo"],
            nuevo["crm_campo_activo"],
            nuevo["calendario_activo"],
            nuevo["activo"],
            nuevo["orden"],
            nuevo["stripe_price_id"],
            nuevo["max_proveedores"],
            nuevo["modelos_permitidos"],
            nuevo["historial_maximo"],
            nuevo["max_numeros_whatsapp"],
            nuevo["coexistencia_activa"],
        )

        if datos.herramientas_plan is not None and set(nueva_lista) != set(lista_anterior):
            await conn.execute("DELETE FROM plan_herramientas WHERE plan_nombre = $1", nombre)
            for familia in nueva_lista:
                await conn.execute(
                    "INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    nombre,
                    familia,
                )

        cambios = datos.model_dump(exclude_none=True)
        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="plan_actualizado",
            tenant_id=None,
            detalle={
                "nombre": nombre,
                # Decimal no es serializable a JSON tal cual: se pasa a
                # string, mismo criterio que el resto de las auditorías de
                # este módulo (ver AjusteCreditosIn en routers/gerencia.py).
                "cambios": {
                    k: (str(v) if hasattr(v, "quantize") else v) for k, v in cambios.items()
                },
                # Negocios cuyo agente se recortó al nuevo límite (modelo o historial).
                "negocios_recortados": recortados,
            },
            conn=conn,
        )
        if anterior["max_numeros_whatsapp"] != nuevo["max_numeros_whatsapp"]:
            await registrar_auditoria(
                actor_email=gerente.email,
                actor_portal_user_id=gerente.id,
                accion="cupo_cambiado",
                tenant_id=None,
                detalle={
                    "nombre": nombre,
                    "anterior": anterior["max_numeros_whatsapp"],
                    "nuevo": nuevo["max_numeros_whatsapp"],
                },
                conn=conn,
            )
        if anterior["coexistencia_activa"] != nuevo["coexistencia_activa"]:
            await registrar_auditoria(
                actor_email=gerente.email,
                actor_portal_user_id=gerente.id,
                accion="coexistencia_del_plan_cambiada",
                tenant_id=None,
                detalle={
                    "nombre": nombre,
                    "anterior": anterior["coexistencia_activa"],
                    "nuevo": nuevo["coexistencia_activa"],
                },
                conn=conn,
            )
        if str(anterior["precio_monthly"]) != str(nuevo["precio_monthly"]):
            await registrar_auditoria(
                actor_email=gerente.email,
                actor_portal_user_id=gerente.id,
                accion="precio_del_plan_cambiado",
                tenant_id=None,
                detalle={
                    "nombre": nombre,
                    "anterior": str(anterior["precio_monthly"]),
                    "nuevo": str(nuevo["precio_monthly"]),
                },
                conn=conn,
            )
        if set(nueva_lista) != set(lista_anterior):
            await registrar_auditoria(
                actor_email=gerente.email,
                actor_portal_user_id=gerente.id,
                accion="herramientas_del_plan_cambiadas",
                tenant_id=None,
                detalle={
                    "nombre": nombre,
                    "anteriores": sorted(lista_anterior),
                    "nuevas": sorted(nueva_lista),
                },
                conn=conn,
            )
        activos = await conn.fetchval("SELECT COUNT(*) FROM planes WHERE activo")
        if not activos:
            await registrar_auditoria(
                actor_email=gerente.email,
                actor_portal_user_id=gerente.id,
                accion="plan_sin_tope_por_falta_de_plan_activo",
                tenant_id=None,
                detalle={"nombre": nombre},
                conn=conn,
            )

        fila = await conn.fetchrow(f"SELECT {_COLUMNAS} FROM planes WHERE nombre = $1", nombre)
        lista_final = await _leer_herramientas(conn, nombre)

    return _out(fila, lista_final)
