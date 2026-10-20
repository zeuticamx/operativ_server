"""`jti` en el refresh token (spec 006): lo que permite rotarlo y revocarlo
sin esperar a su `exp`. Ver routers/auth.py para cómo se usa."""

from uuid import UUID, uuid4

from security import crear_refresh_token, decodificar_token


def test_el_refresh_trae_jti():
    user_id = uuid4()
    jti = uuid4()
    token = crear_refresh_token(user_id, jti)

    payload = decodificar_token(token, "refresh")
    assert payload["jti"] == str(jti)
    assert payload["sub"] == str(user_id)
    assert payload["type"] == "refresh"


def test_sin_jti_explicito_genera_uno_nuevo():
    token = crear_refresh_token(uuid4())
    payload = decodificar_token(token, "refresh")
    # No revienta, y el valor es un UUID válido.
    UUID(payload["jti"])
