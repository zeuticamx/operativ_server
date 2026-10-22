"""
Plantillas de WhatsApp de los avisos de cita por negocio (spec 007).

Única fuente de los textos literales, sus ejemplos y las reglas puras para elegir
el nombre a crear y decidir si una plantilla ya existente se puede adoptar.
Sin I/O: ni base de datos, ni red, ni reloj.
"""

from typing import Any, Mapping

from services import plantillas_reglas as reglas

TIPO_CANCELACION = "cancelacion"
TIPO_RECORDATORIO = "recordatorio"
TIPOS = (TIPO_CANCELACION, TIPO_RECORDATORIO)

IDIOMA = "es_MX"
CATEGORIA = "UTILITY"

NOMBRE_BASE_CANCELACION = "notificacion_cancelacion"
NOMBRE_BASE_RECORDATORIO = "recordatorio_cita"

# Textos literales dados por el usuario (core-spec): no se tocan, ni un espacio.
CUERPO_CANCELACION = (
    "Hola! Lamentamos informarte que por causas de fuerza mayor nos vimos en la "
    "necesidad de cancelar tu cita {{1}} del dia {{2}}. Te pedimos nos escribas "
    "para poder agendar una nueva fecha, Muchas gracias."
)
CUERPO_RECORDATORIO = (
    "hola! te escribe el asistente de {{1}}  en relacion a tu cita registrada en "
    "nuestro sistema con fecha {{2}}, agradecemos tu confirmacion de cita. Te esperamos!"
)

# Qué significa cada {{n}} (coincide con variables_cancelacion/variables_recordatorio).
VARIABLES_CANCELACION = ("servicio", "fecha")
VARIABLES_RECORDATORIO = ("nombre_negocio", "fecha")

# Ejemplos que exige Meta al crear; la fecha va en dd-mm-aaaa, sin hora.
EJEMPLOS_CANCELACION = ("Corte de cabello", "20-10-2026")
EJEMPLOS_RECORDATORIO = ("Mi Negocio", "20-10-2026")

ESTADOS_ADOPTABLES = ("APPROVED", "PENDING")
CATEGORIAS_ADOPTABLES = ("UTILITY", "MARKETING")  # Meta puede reclasificar
MAX_SUFIJO = 9  # base, _v2 ... _v9

_BASES = {
    TIPO_CANCELACION: NOMBRE_BASE_CANCELACION,
    TIPO_RECORDATORIO: NOMBRE_BASE_RECORDATORIO,
}
_CUERPOS = {TIPO_CANCELACION: CUERPO_CANCELACION, TIPO_RECORDATORIO: CUERPO_RECORDATORIO}
_VARIABLES = {
    TIPO_CANCELACION: VARIABLES_CANCELACION,
    TIPO_RECORDATORIO: VARIABLES_RECORDATORIO,
}
_EJEMPLOS = {TIPO_CANCELACION: EJEMPLOS_CANCELACION, TIPO_RECORDATORIO: EJEMPLOS_RECORDATORIO}


def _validar_tipo(tipo: str) -> None:
    if tipo not in TIPOS:
        raise ValueError(f"tipo de aviso desconocido: {tipo!r}")


def nombre_base(tipo: str) -> str:
    _validar_tipo(tipo)
    return _BASES[tipo]


def cuerpo(tipo: str) -> str:
    _validar_tipo(tipo)
    return _CUERPOS[tipo]


def variables(tipo: str) -> tuple[str, ...]:
    _validar_tipo(tipo)
    return _VARIABLES[tipo]


def ejemplos(tipo: str) -> list[str]:
    _validar_tipo(tipo)
    return list(_EJEMPLOS[tipo])


def siguiente_nombre(base: str, usados: set[str] | frozenset[str] | list[str]) -> str | None:
    """
    Primer nombre libre entre `base`, `base_v2`, ... `base_v9`. Devuelve None si
    los 9 están usados (el llamador responde 409 `plantilla_existente_no_utilizable`).
    """
    ocupados = set(usados)
    if base not in ocupados:
        return base
    for n in range(2, MAX_SUFIJO + 1):
        candidato = f"{base}_v{n}"
        if candidato not in ocupados:
            return candidato
    return None


def adoptable(p: Mapping[str, Any], tipo: str) -> bool:
    """
    Una plantilla existente sirve si está en es_MX, es UTILITY/MARKETING y pide
    tantas variables como el aviso. `p` es la fila de `plantillas_whatsapp_tenant`
    (idioma, categoria, componentes). No mira el estado: ver `debe_adoptar`.
    """
    _validar_tipo(tipo)
    if p.get("idioma") != IDIOMA:
        return False
    if str(p.get("categoria") or "").upper() not in CATEGORIAS_ADOPTABLES:
        return False
    componentes = p.get("componentes")
    if not isinstance(componentes, list):
        return False
    return reglas.numero_de_variables(componentes) == len(_VARIABLES[tipo])


def debe_adoptar(p: Mapping[str, Any], tipo: str) -> bool:
    """APPROVED/PENDING y adoptable -> adoptar; REJECTED/PAUSED/DISABLED o no adoptable -> crear con sufijo."""
    estado = str(p.get("estado") or "").upper()
    return estado in ESTADOS_ADOPTABLES and adoptable(p, tipo)
