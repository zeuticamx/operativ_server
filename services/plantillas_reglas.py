"""
Reglas puras de las plantillas de WhatsApp de un negocio: validan lo que Meta
rechaza (nombre, variables posicionales, largos) ANTES de gastar una llamada a
NeuroAPI, y arman la lista `components` que espera `POST /messaging/templates`.
Sin BD ni red.
"""

import re
from typing import Any

NOMBRE_RE = re.compile(r"^[a-z0-9_]{1,512}$")
IDIOMA_RE = re.compile(r"^[a-z]{2}(_[A-Z]{2})?$")
VARIABLE_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")

CATEGORIAS = ("UTILITY", "MARKETING", "AUTHENTICATION")
ESTADOS = ("PENDING", "APPROVED", "REJECTED", "PAUSED", "DISABLED")

MAX_HEADER = 60
MAX_CUERPO = 1024
MAX_PIE = 60
MAX_BOTONES = 10
MAX_TEXTO_BOTON = 25
TIPOS_BOTON = ("QUICK_REPLY", "URL", "PHONE_NUMBER")


class PlantillaInvalida(ValueError):
    """La plantilla incumple una regla de Meta; el mensaje es apto para la UI."""


def validar_nombre(nombre: str) -> str:
    if not NOMBRE_RE.fullmatch(nombre or ""):
        raise PlantillaInvalida(
            "El nombre solo admite minúsculas, números y guion bajo (sin espacios ni tildes)"
        )
    return nombre


def validar_idioma(idioma: str) -> str:
    if not IDIOMA_RE.fullmatch(idioma or ""):
        raise PlantillaInvalida("El idioma debe verse como 'es' o 'es_MX'")
    return idioma


def variables_de(texto: str | None) -> list[int]:
    """Números de variable en orden de aparición, sin repetidos: '{{2}} {{1}} {{2}}' -> [2, 1]."""
    vistas: list[int] = []
    for m in VARIABLE_RE.finditer(texto or ""):
        n = int(m.group(1))
        if n not in vistas:
            vistas.append(n)
    return vistas


def _validar_variables(texto: str, parte: str, maximo: int | None = None) -> int:
    """Exige {{1}}..{{n}} consecutivas. Devuelve n."""
    numeros = variables_de(texto)
    if not numeros:
        return 0
    if sorted(numeros) != list(range(1, len(numeros) + 1)):
        raise PlantillaInvalida(
            f"Las variables del {parte} deben ser consecutivas desde {{{{1}}}} (sin saltos)"
        )
    if maximo is not None and len(numeros) > maximo:
        raise PlantillaInvalida(f"El {parte} admite como máximo {maximo} variable(s)")
    return len(numeros)


def _sin_variable_en_bordes(texto: str) -> None:
    limpio = texto.strip()
    if re.match(r"^\{\{\s*\d+\s*\}\}", limpio) or re.search(r"\{\{\s*\d+\s*\}\}$", limpio):
        raise PlantillaInvalida("El cuerpo no puede empezar ni terminar con una variable")


def _ejemplos(valores: list[str], esperadas: int, parte: str) -> list[str]:
    limpios = [v.strip() for v in valores]
    if len(limpios) != esperadas or any(not v for v in limpios):
        raise PlantillaInvalida(
            f"Hace falta un ejemplo no vacío por cada variable del {parte} ({esperadas})"
        )
    return limpios


def construir_componentes(
    *,
    cuerpo: str,
    ejemplos_cuerpo: list[str],
    encabezado: str | None = None,
    ejemplo_encabezado: str | None = None,
    pie: str | None = None,
    botones: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Valida y arma `components` (HEADER de texto, BODY, FOOTER, BUTTONS)."""
    componentes: list[dict[str, Any]] = []

    if encabezado and encabezado.strip():
        if len(encabezado) > MAX_HEADER:
            raise PlantillaInvalida(f"El encabezado admite hasta {MAX_HEADER} caracteres")
        n = _validar_variables(encabezado, "encabezado", maximo=1)
        header: dict[str, Any] = {"type": "HEADER", "format": "TEXT", "text": encabezado}
        if n:
            header["example"] = {
                "header_text": _ejemplos([ejemplo_encabezado or ""], 1, "encabezado")
            }
        componentes.append(header)

    if not cuerpo or not cuerpo.strip():
        raise PlantillaInvalida("El cuerpo es obligatorio")
    if len(cuerpo) > MAX_CUERPO:
        raise PlantillaInvalida(f"El cuerpo admite hasta {MAX_CUERPO} caracteres")
    n_cuerpo = _validar_variables(cuerpo, "cuerpo")
    body: dict[str, Any] = {"type": "BODY", "text": cuerpo}
    if n_cuerpo:
        _sin_variable_en_bordes(cuerpo)
        body["example"] = {"body_text": [_ejemplos(ejemplos_cuerpo, n_cuerpo, "cuerpo")]}
    componentes.append(body)

    if pie and pie.strip():
        if len(pie) > MAX_PIE:
            raise PlantillaInvalida(f"El pie admite hasta {MAX_PIE} caracteres")
        if variables_de(pie):
            raise PlantillaInvalida("El pie no admite variables")
        componentes.append({"type": "FOOTER", "text": pie})

    if botones:
        if len(botones) > MAX_BOTONES:
            raise PlantillaInvalida(f"Se admiten hasta {MAX_BOTONES} botones")
        componentes.append({"type": "BUTTONS", "buttons": [_boton(b) for b in botones]})

    return componentes


def _boton(b: dict[str, str]) -> dict[str, str]:
    tipo = b.get("tipo", "")
    texto = (b.get("texto") or "").strip()
    if tipo not in TIPOS_BOTON:
        raise PlantillaInvalida(f"Tipo de botón no válido: {tipo!r}")
    if not texto or len(texto) > MAX_TEXTO_BOTON:
        raise PlantillaInvalida(f"El texto del botón va de 1 a {MAX_TEXTO_BOTON} caracteres")
    if tipo == "QUICK_REPLY":
        return {"type": tipo, "text": texto}
    if tipo == "URL":
        url = (b.get("url") or "").strip()
        if not url.startswith("https://"):
            raise PlantillaInvalida("El botón URL necesita una dirección https://")
        return {"type": tipo, "text": texto, "url": url}
    telefono = (b.get("telefono") or "").strip()
    if not re.fullmatch(r"\+\d{8,15}", telefono):
        raise PlantillaInvalida("El botón de llamada necesita un teléfono como +593999999999")
    return {"type": tipo, "text": texto, "phone_number": telefono}


# ------------------------------------------------------------
# Lectura de lo que devuelve Meta/NeuroAPI (forma tolerante)
# ------------------------------------------------------------
def texto_de(componentes: list[dict[str, Any]], tipo: str) -> str | None:
    for c in componentes:
        if isinstance(c, dict) and str(c.get("type", "")).upper() == tipo:
            valor = c.get("text")
            return valor if isinstance(valor, str) else None
    return None


def numero_de_variables(componentes: list[dict[str, Any]]) -> int:
    """Mayor {{n}} del cuerpo: cuántos valores pide la plantilla al enviarse."""
    numeros = variables_de(texto_de(componentes, "BODY"))
    return max(numeros) if numeros else 0


def parametros_de_envio(
    componentes: list[dict[str, Any]],
    valores_cuerpo: list[str],
    valor_encabezado: str | None = None,
) -> list[dict[str, Any]]:
    """`template.components[]` de `POST /messaging/send`, validando que no falten valores."""
    salida: list[dict[str, Any]] = []
    if variables_de(texto_de(componentes, "HEADER")):
        if not (valor_encabezado or "").strip():
            raise PlantillaInvalida("Falta el valor de la variable del encabezado")
        salida.append(
            {"type": "header", "parameters": [{"type": "text", "text": valor_encabezado.strip()}]}
        )
    esperadas = numero_de_variables(componentes)
    if len(valores_cuerpo) != esperadas or any(not (v or "").strip() for v in valores_cuerpo):
        raise PlantillaInvalida(f"Hacen falta {esperadas} valor(es) no vacío(s) para el cuerpo")
    if esperadas:
        salida.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": v.strip()} for v in valores_cuerpo],
            }
        )
    return salida
