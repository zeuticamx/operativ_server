"""
Caché local de las plantillas de WhatsApp de cada negocio (tabla
`plantillas_whatsapp_tenant`, sql/60). La fuente de verdad es Meta: aquí se
guarda lo que NeuroAPI devolvió al sincronizar, al crear, y los cambios de
estado que Meta empuja por `message_template_status_update` (sin polling).

Toda consulta filtra por `tenant_id`: una plantilla de otro negocio no existe
para quien pregunta (la ruta responde 404).
"""

import logging
from typing import Any
from uuid import UUID

from services import plantillas_reglas as reglas
from session import execute, fetch_all, fetch_one

log = logging.getLogger("operativai.plantillas_tenant")

# `event` de Meta -> estado de la caché. FLAGGED/PENDING_DELETION y otros no mueven nada.
ESTADO_POR_EVENTO = {
    "APPROVED": "APPROVED",
    "REINSTATED": "APPROVED",
    "REJECTED": "REJECTED",
    "DISABLED": "DISABLED",
    "PAUSED": "PAUSED",
    "PENDING": "PENDING",
}
CAMPO_ESTADO = "message_template_status_update"


# ------------------------------------------------------------
# Puras
# ------------------------------------------------------------
def normalizar_meta(crudo: dict[str, Any]) -> dict[str, Any] | None:
    """Forma de Meta (name/language/category/status/components/id) -> campos de la caché."""
    nombre = str(crudo.get("name") or "").strip()
    if not nombre:
        return None
    categoria = str(crudo.get("category") or "").upper()
    estado = str(crudo.get("status") or "PENDING").upper()
    lenguaje = crudo.get("language")
    if isinstance(lenguaje, dict):
        lenguaje = lenguaje.get("code")
    motivo = crudo.get("rejected_reason") or crudo.get("reason")
    componentes = crudo.get("components")
    return {
        "nombre": nombre,
        "idioma": str(lenguaje or "es_MX"),
        "categoria": categoria if categoria in reglas.CATEGORIAS else "UTILITY",
        "estado": estado if estado in reglas.ESTADOS else "PENDING",
        "motivo_rechazo": None if not motivo or str(motivo).upper() == "NONE" else str(motivo),
        "componentes": componentes if isinstance(componentes, list) else [],
        "meta_template_id": str(crudo["id"]) if crudo.get("id") else None,
    }


def a_salida(fila: dict[str, Any]) -> dict[str, Any]:
    comps = fila["componentes"]
    return {
        "nombre": fila["nombre"],
        "idioma": fila["idioma"],
        "categoria": fila["categoria"],
        "estado": fila["estado"],
        "motivo_rechazo": fila["motivo_rechazo"],
        "componentes": comps,
        "variables_cuerpo": reglas.numero_de_variables(comps),
        "variable_encabezado": bool(reglas.variables_de(reglas.texto_de(comps, "HEADER"))),
        "actualizada_en": fila["actualizada_en"],
        "sincronizada_en": fila["sincronizada_en"],
    }


def elegir_candidata(
    candidatas: list[dict[str, Any]], meta_id: str | None, waba_id: str | None
) -> dict[str, Any] | None:
    """
    Dueña de un evento de Meta, que solo trae WABA, nombre, idioma e id de
    plantilla. Prioridad: mismo `meta_template_id`; luego mismo `waba_id`; si
    ninguno decide, solo vale cuando hay UNA candidata. Con varios negocios
    homónimos y sin dato que desempate, no se adivina (se devuelve None).
    """
    if meta_id:
        por_id = [c for c in candidatas if c.get("meta_template_id") == meta_id]
        if len(por_id) == 1:
            return por_id[0]
    if waba_id:
        por_waba = [c for c in candidatas if c.get("waba_id") == waba_id]
        if len(por_waba) == 1:
            return por_waba[0]
    return candidatas[0] if len(candidatas) == 1 else None


def cambios_de_estado(evento: Any) -> list[dict[str, Any]]:
    """De un sobre de Meta saca cada `message_template_status_update` con estado conocido."""
    if not isinstance(evento, dict) or not isinstance(evento.get("entry"), list):
        return []
    salida: list[dict[str, Any]] = []
    for entrada in evento["entry"]:
        if not isinstance(entrada, dict) or not isinstance(entrada.get("changes"), list):
            continue
        for cambio in entrada["changes"]:
            if not isinstance(cambio, dict) or cambio.get("field") != CAMPO_ESTADO:
                continue
            v = cambio.get("value")
            if not isinstance(v, dict):
                continue
            nuevo = ESTADO_POR_EVENTO.get(str(v.get("event") or "").strip().upper())
            nombre = str(v.get("message_template_name") or "").strip()
            if nuevo is None or not nombre:
                continue
            motivo = v.get("reason")
            salida.append(
                {
                    "nombre": nombre,
                    "idioma": str(v.get("message_template_language") or "") or None,
                    "meta_id": str(v["message_template_id"]) if v.get("message_template_id") else None,
                    "waba_id": str(entrada["id"]) if entrada.get("id") else None,
                    "estado": nuevo,
                    "motivo": None if not motivo or str(motivo).upper() == "NONE" else str(motivo),
                }
            )
    return salida


# ------------------------------------------------------------
# BD
# ------------------------------------------------------------
async def listar(
    tenant_id: UUID, *, categoria: str | None = None, estado: str | None = None
) -> list[dict[str, Any]]:
    filas = await fetch_all(
        """
        SELECT * FROM plantillas_whatsapp_tenant
        WHERE tenant_id = $1
          AND ($2::text IS NULL OR categoria = $2)
          AND ($3::text IS NULL OR estado = $3)
        ORDER BY actualizada_en DESC, nombre
        """,
        tenant_id,
        categoria,
        estado,
    )
    return [dict(f) for f in filas]


async def obtener(tenant_id: UUID, nombre: str) -> dict[str, Any] | None:
    fila = await fetch_one(
        """
        SELECT * FROM plantillas_whatsapp_tenant
        WHERE tenant_id = $1 AND nombre = $2
        ORDER BY actualizada_en DESC LIMIT 1
        """,
        tenant_id,
        nombre,
    )
    return dict(fila) if fila else None


async def guardar(
    tenant_id: UUID, datos: dict[str, Any], *, creada_por: UUID | None = None, sincronizada: bool
) -> None:
    """Inserta o actualiza por (tenant, nombre, idioma)."""
    await execute(
        """
        INSERT INTO plantillas_whatsapp_tenant
            (tenant_id, nombre, idioma, categoria, estado, motivo_rechazo, componentes,
             meta_template_id, creada_por, sincronizada_en)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                CASE WHEN $10 THEN NOW() END)
        ON CONFLICT (tenant_id, nombre, idioma) DO UPDATE SET
            categoria = EXCLUDED.categoria,
            estado = EXCLUDED.estado,
            motivo_rechazo = EXCLUDED.motivo_rechazo,
            componentes = CASE WHEN jsonb_array_length(EXCLUDED.componentes) > 0
                               THEN EXCLUDED.componentes
                               ELSE plantillas_whatsapp_tenant.componentes END,
            meta_template_id = COALESCE(EXCLUDED.meta_template_id,
                                        plantillas_whatsapp_tenant.meta_template_id),
            actualizada_en = NOW(),
            sincronizada_en = COALESCE(EXCLUDED.sincronizada_en,
                                       plantillas_whatsapp_tenant.sincronizada_en)
        """,
        tenant_id,
        datos["nombre"],
        datos["idioma"],
        datos["categoria"],
        datos["estado"],
        datos.get("motivo_rechazo"),
        # list, no str: el códec jsonb del pool ya serializa.
        datos["componentes"],
        datos.get("meta_template_id"),
        creada_por,
        sincronizada,
    )


async def sincronizar(
    tenant_id: UUID, remotas: list[dict[str, Any]], *, lista_completa: bool
) -> int:
    """
    Vuelca el catálogo de Meta en la caché. Con `lista_completa` (NeuroAPI devolvió
    menos del límite pedido, o sea, todo) borra las filas locales que Meta ya no tiene.
    Devuelve cuántas plantillas quedaron sincronizadas.
    """
    normalizadas = [n for n in (normalizar_meta(r) for r in remotas) if n]
    for n in normalizadas:
        await guardar(tenant_id, n, sincronizada=True)
    if lista_completa:
        claves = [f"{n['nombre']}|{n['idioma']}" for n in normalizadas]
        await execute(
            """
            DELETE FROM plantillas_whatsapp_tenant
            WHERE tenant_id = $1 AND NOT ((nombre || '|' || idioma) = ANY($2::text[]))
            """,
            tenant_id,
            claves,
        )
    return len(normalizadas)


async def borrar_local(tenant_id: UUID, nombre: str) -> None:
    await execute(
        "DELETE FROM plantillas_whatsapp_tenant WHERE tenant_id = $1 AND nombre = $2",
        tenant_id,
        nombre,
    )


async def aplicar_evento_meta(evento: Any) -> list[tuple[UUID, str, str]]:
    """
    Aplica los `message_template_status_update` a la caché de los negocios.
    Devuelve `(tenant_id, nombre, estado_nuevo)` de lo que cambió. No crea filas:
    una plantilla que ningún negocio tiene en la caché (p. ej. las de la cuenta
    global) se ignora. Sin dueño único, se registra y se descarta.
    """
    aplicados: list[tuple[UUID, str, str]] = []
    for c in cambios_de_estado(evento):
        filas = await fetch_all(
            """
            SELECT tenant_id, nombre, idioma, estado, meta_template_id, waba_id
            FROM plantillas_whatsapp_tenant
            WHERE nombre = $1 AND ($2::text IS NULL OR idioma = $2)
            """,
            c["nombre"],
            c["idioma"],
        )
        if not filas:
            continue
        duenio = elegir_candidata([dict(f) for f in filas], c["meta_id"], c["waba_id"])
        if duenio is None:
            log.warning(
                "Evento de plantilla %r sin dueño único (%d candidatas): ignorado",
                c["nombre"],
                len(filas),
            )
            continue
        if duenio["estado"] == c["estado"]:
            continue
        await execute(
            """
            UPDATE plantillas_whatsapp_tenant
            SET estado = $4, motivo_rechazo = $5, actualizada_en = NOW(),
                meta_template_id = COALESCE(meta_template_id, $6),
                waba_id = COALESCE(waba_id, $7)
            WHERE tenant_id = $1 AND nombre = $2 AND idioma = $3
            """,
            duenio["tenant_id"],
            duenio["nombre"],
            duenio["idioma"],
            c["estado"],
            c["motivo"],
            c["meta_id"],
            c["waba_id"],
        )
        aplicados.append((duenio["tenant_id"], duenio["nombre"], c["estado"]))
    return aplicados
