"""
Tests del aplanado de documentos de Google Docs (services/google_tools.py).

La API no devuelve el texto de corrido: el cuerpo es un árbol de elementos
estructurales y el texto vive dentro de los `textRun` de cada párrafo. En n8n
esto lo resolvía el nodo de Google Docs; acá hay que recorrerlo.

Lo que importa si falla: la herramienta `google_docs` del agente le devuelve
al modelo un documento incompleto —sin las tablas, o sin la mitad del
texto— y el modelo contesta con datos que faltan sin saber que faltan.

Sin red: se prueba el recorrido con respuestas de ejemplo de la API.
"""

from services.google_tools import _texto_de_contenido


def parrafo(*textos: str) -> dict:
    return {"paragraph": {"elements": [{"textRun": {"content": t}} for t in textos]}}


def test_parrafos_simples():
    assert _texto_de_contenido([
        parrafo("Horario de atención\n"),
        parrafo("Lunes a viernes de 9 a 18.\n"),
    ]) == "Horario de atención\nLunes a viernes de 9 a 18.\n"


def test_un_parrafo_partido_en_varios_textrun():
    """
    Docs parte un párrafo en varios `textRun` cuando cambia el formato: una
    palabra en negrita es su propio trozo. Concatenarlos mal se come texto.
    """
    assert _texto_de_contenido([
        parrafo("El corte cuesta ", "200", " pesos.\n")
    ]) == "El corte cuesta 200 pesos.\n"


def test_tabla():
    """Las tablas anidan más elementos; sin recorrerlas se pierde el contenido."""
    tabla = {
        "table": {
            "tableRows": [
                {"tableCells": [
                    {"content": [parrafo("Servicio\n")]},
                    {"content": [parrafo("Precio\n")]},
                ]},
                {"tableCells": [
                    {"content": [parrafo("Corte\n")]},
                    {"content": [parrafo("200\n")]},
                ]},
            ]
        }
    }
    assert _texto_de_contenido([tabla]) == "Servicio | Precio\nCorte | 200\n"


def test_indice_tambien_anida():
    toc = {"tableOfContents": {"content": [parrafo("1. Servicios\n")]}}
    assert _texto_de_contenido([toc]) == "1. Servicios\n"


def test_elementos_sin_texto_se_ignoran():
    """
    Saltos de sección, saltos de página, líneas horizontales e imágenes no
    aportan texto; tropezar con ellos no puede romper la lectura.
    """
    assert _texto_de_contenido([
        {"sectionBreak": {"sectionStyle": {}}},
        parrafo("Hola\n"),
        {"paragraph": {"elements": [
            {"pageBreak": {}},
            {"horizontalRule": {}},
            {"inlineObjectElement": {"inlineObjectId": "kix.abc"}},
            {"textRun": {"content": "Adiós\n"}},
        ]}},
    ]) == "Hola\nAdiós\n"


def test_documento_vacio():
    assert _texto_de_contenido([]) == ""
    assert _texto_de_contenido(None) == ""


def test_parrafo_sin_elements():
    """Un párrafo vacío llega sin la clave `elements`."""
    assert _texto_de_contenido([{"paragraph": {}}]) == ""


def test_documento_realista_completo():
    cuerpo = [
        {"sectionBreak": {}},
        parrafo("Barbería El Corte\n"),
        parrafo("\n"),
        parrafo("Precios ", "vigentes", ":\n"),
        {"table": {"tableRows": [
            {"tableCells": [
                {"content": [parrafo("Corte\n")]},
                {"content": [parrafo("$200\n")]},
            ]},
        ]}},
        parrafo("Pagos en efectivo o tarjeta.\n"),
    ]
    texto = _texto_de_contenido(cuerpo)

    assert "Barbería El Corte" in texto
    assert "Precios vigentes:" in texto
    assert "Corte | $200" in texto
    assert "Pagos en efectivo o tarjeta." in texto
