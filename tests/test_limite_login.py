"""Tope de intentos fallidos de login (services/limite_login.py). Puro, sin BD."""

import pytest

from services import limite_login as ll


@pytest.fixture(autouse=True)
def _limpio():
    ll.reiniciar()
    yield
    ll.reiniciar()


def test_bloquea_al_quinto_fallo_por_correo():
    for _ in range(ll.MAX_FALLOS_POR_CORREO - 1):
        ll.registrar_fallo("a@x.com", "1.1.1.1", ahora=100)
    assert ll.segundos_de_espera("a@x.com", "1.1.1.1", ahora=100) == 0
    ll.registrar_fallo("a@x.com", "1.1.1.1", ahora=100)
    assert ll.segundos_de_espera("a@x.com", "1.1.1.1", ahora=100) > 0


def test_el_correo_no_distingue_mayusculas():
    for _ in range(ll.MAX_FALLOS_POR_CORREO):
        ll.registrar_fallo("A@X.com", None, ahora=100)
    assert ll.segundos_de_espera("a@x.COM", None, ahora=100) > 0


def test_otro_correo_desde_otra_ip_no_se_ve_afectado():
    for _ in range(ll.MAX_FALLOS_POR_CORREO):
        ll.registrar_fallo("a@x.com", "1.1.1.1", ahora=100)
    assert ll.segundos_de_espera("b@x.com", "2.2.2.2", ahora=100) == 0


def test_la_ip_se_bloquea_aunque_roten_los_correos():
    for i in range(ll.MAX_FALLOS_POR_IP):
        ll.registrar_fallo(f"u{i}@x.com", "9.9.9.9", ahora=100)
    assert ll.segundos_de_espera("nuevo@x.com", "9.9.9.9", ahora=100) > 0


def test_el_bloqueo_expira_con_la_ventana():
    for _ in range(ll.MAX_FALLOS_POR_CORREO):
        ll.registrar_fallo("a@x.com", None, ahora=100)
    assert ll.segundos_de_espera("a@x.com", None, ahora=100 + ll.VENTANA_SEGUNDOS + 1) == 0


def test_login_correcto_limpia_los_fallos_del_correo():
    for _ in range(ll.MAX_FALLOS_POR_CORREO):
        ll.registrar_fallo("a@x.com", None, ahora=100)
    ll.limpiar_correo("a@x.com")
    assert ll.segundos_de_espera("a@x.com", None, ahora=100) == 0
