"""Valores por defecto de la cookie de refresh (spec 006)."""

from config import settings


def test_valores_por_defecto_de_la_cookie():
    assert settings.COOKIE_REFRESH_NOMBRE == "op_refresh"
    # Portal y API en dominios distintos (spec 006, D1): SameSite=None
    # exige Secure, y es el default de producción.
    assert settings.COOKIE_SAMESITE == "none"
    assert settings.COOKIE_SECURE is True
    # Ventana de migración abierta por defecto: un cliente viejo con el
    # refresh en localStorage sigue pudiendo mandarlo en el body.
    assert settings.REFRESH_BODY_LEGACY is True
