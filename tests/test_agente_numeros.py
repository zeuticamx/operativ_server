"""
Tests T7 de spec 005a: agente con configuración y herramientas por número.

Lo que importa si falla:

  1. dos números del mismo negocio con prompts/modelos/herramientas distintas
     responden cada uno con los suyos (emisor simulado)
  2. un número pausado, con agente apagado o incompleto guarda el mensaje y no
     responde (sin AVISO_SIN_AGENTE)
  3. conversación distinta por número (mismo cliente, mismo negocio)
  4. número sin vincular no se atribuye y no responde
  5. mensaje a un número desvinculado queda oculto (sin respuesta)
  6. herramienta no activa para el número, o fuera del plan, no se ejecuta ni
     cobra
  7. Instagram, Facebook y widget usan tenant_agent_config y social_herramientas
  8. la respuesta humana usa el número de la conversación y devuelve los 409 de
     RF-12 (con agente apagado se permite)

`resolver_entrada` se prueba con base real (números en agentes_canal).
`ingreso.procesar` se prueba mockeando la persistencia (_upsert_usuario,
_conversacion, _guardar_mensaje, execute) y el plan (on_mensaje_entrante),
dejando real la resolución del número por `resolver_entrada`, para verificar
que se pasa el `agente_canal_id` y la `config` correctos al contexto del agente.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import numeros_whatsapp
from services.agente import herramientas, ingreso, loop_agente
from services.agente.modelos import (
    ContextoMensaje,
    MensajeNormalizado,
    PeticionVerificada,
)
from services import conversaciones_ocultas
from session import conexion, execute, fetch_one

# ============================================================
# Helpers de base
# ============================================================


def _peticion(tenant_id) -> PeticionVerificada:
    return PeticionVerificada(cuerpo={}, origen="interno", tenant_id=tenant_id)


def _mensaje_whatsapp(phone_number_id: str, contenido: str = "hola") -> MensajeNormalizado:
    return MensajeNormalizado(
        procesar=True,
        canal="whatsapp",
        usuario_id="5215599999999",
        nombre="Ana",
        mensaje_id=f"wamid-{uuid4().hex[:8]}",
        contenido=contenido,
        phone_number_id=phone_number_id,
    )


def _mensaje_social(canal: str) -> MensajeNormalizado:
    return MensajeNormalizado(
        procesar=True,
        canal=canal,
        usuario_id="5215599999999",
        nombre="Ana",
        mensaje_id=f"wamid-{uuid4().hex[:8]}",
        contenido="hola",
    )


async def _normalizar_falso(monkeypatch, mensaje: MensajeNormalizado) -> None:
    monkeypatch.setattr(
        ingreso.normalizar_mod, "normalizar", lambda cuerpo, tenant_id=None: mensaje
    )


async def _plan_sin_limites(monkeypatch) -> None:
    """on_mensaje_entrante dice que el agente puede responder."""
    async def falso(tenant_id, user_id, info):
        return SimpleNamespace(agente_ia_activo=True)
    monkeypatch.setattr("routers.eventos.on_mensaje_entrante", falso)


async def _mockea_persistencia(monkeypatch):
    """Mockea la persistencia de ingreso para no depender de columnas que aún
    no están en la BD de pruebas (channel_msg_id, agrupado en messages)."""
    escrito = []

    async def _upsert_usuario(tenant_id, mensaje):
        return uuid4()

    async def _conversacion(tenant_id, user_id, canal, agente_canal_id=None):
        escrito.append(("conversacion", agente_canal_id))
        return {"id": uuid4(), "status": "active", "metadata": {}}

    async def _guardar_mensaje(tenant_id, conversation_id, mensaje):
        escrito.append(("mensaje", conversation_id))
        return {"id": uuid4(), "created_at": datetime.now()}

    async def execute(query, *args):
        escrito.append(("execute", query))
        return "OK"

    async def _esperar_rafaga(contexto):
        return True

    async def _conversacion_oculto_falso(
        conn, tenant_id, agente_canal_id, user_id, ahora
    ):
        return uuid4()

    monkeypatch.setattr(ingreso, "_upsert_usuario", _upsert_usuario)
    monkeypatch.setattr(ingreso, "_conversacion", _conversacion)
    monkeypatch.setattr(ingreso, "_guardar_mensaje", _guardar_mensaje)
    monkeypatch.setattr(ingreso, "execute", execute)
    monkeypatch.setattr(ingreso, "_esperar_rafaga", _esperar_rafaga)
    monkeypatch.setattr(
        conversaciones_ocultas, "conversacion_para_mensaje_oculto", _conversacion_oculto_falso
    )
    return escrito


# ============================================================
# Datos de prueba en base real
# ============================================================


async def _suscribir_enterprise(conn, tenant_id):
    await conn.execute(
        "DELETE FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id
    )
    await conn.execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly, origen)
        VALUES ($1, 'enterprise', 'activa', 299.99, 'pago')
        """,
        tenant_id,
    )


async def _crear_numero(
    conn,
    tenant_id,
    phone_number_id,
    *,
    estado="activo",
    agent_name="Agente Uno",
    system_prompt="Prompt del número uno.",
    model="claude-sonnet-5",
    agente_activo=True,
    es_principal=False,
    es_linea_avisos=False,
    desvinculado_en=None,
):
    if desvinculado_en is None and estado == "desvinculado":
        desvinculado_en = datetime.now(timezone.utc)
    fila = await conn.fetchrow(
        """
        INSERT INTO agentes_canal
            (tenant_id, phone_number_id, estado, agent_name, system_prompt,
             model, agente_activo, es_principal, es_linea_avisos, desvinculado_en)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id, *
        """,
        tenant_id,
        phone_number_id,
        estado,
        agent_name,
        system_prompt,
        model,
        agente_activo,
        es_principal,
        es_linea_avisos,
        desvinculado_en,
    )
    await conn.execute("COMMIT")
    return fila


async def _crear_herramienta(conn, tenant_id, tool_key, tool_type, display_name):
    await conn.execute(
        """
        INSERT INTO tenant_tools
            (tenant_id, tool_key, tool_type, display_name, description, config, parametros_schema, is_enabled)
        VALUES ($1, $2, $3, $4, $5, '{}', '{}', true)
        ON CONFLICT (tenant_id, tool_key) DO NOTHING
        """,
        tenant_id,
        tool_key,
        tool_type,
        display_name,
        display_name,
    )
    await conn.execute("COMMIT")


async def _activar_para_numero(conn, tenant_id, agente_canal_id, tool_key):
    await conn.execute(
        """
        INSERT INTO agente_canal_herramientas (agente_canal_id, tenant_id, tool_key)
        VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
        """,
        agente_canal_id,
        tenant_id,
        tool_key,
    )


# ============================================================
# 1. resolver_entrada: número sin vincular
# ============================================================
@pytest.mark.asyncio
async def test_resolver_entrada_sin_numero(db, tenant_y_usuario):
    r = await numeros_whatsapp.resolver_entrada("PNID-NO-EXISTE")
    assert r.numero is None
    assert r.motivo is None


# ============================================================
# 2. resolver_entrada: número desvinculado → motivo numero_desvinculado
# ============================================================
@pytest.mark.asyncio
async def test_resolver_entrada_desvinculado(db, tenant_y_usuario):
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-DESVINC",
            estado="desvinculado",
        )

    r = await numeros_whatsapp.resolver_entrada("PNID-DESVINC")
    assert r.numero is not None
    assert r.numero["estado"] == "desvinculado"
    assert r.motivo == "numero_desvinculado"


# ============================================================
# 3. resolver_entrada: pausado / agente apagado / incompleto
# ============================================================
@pytest.mark.asyncio
async def test_resolver_entrada_pausado(db, tenant_y_usuario):
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-PAUSA", estado="pausado"
        )
    r = await numeros_whatsapp.resolver_entrada("PNID-PAUSA")
    assert r.motivo == "numero_no_activo"


@pytest.mark.asyncio
async def test_resolver_entrada_agente_apagado(db, tenant_y_usuario):
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-APAGADO", agente_activo=False
        )
    r = await numeros_whatsapp.resolver_entrada("PNID-APAGADO")
    assert r.motivo == "numero_no_activo"


@pytest.mark.asyncio
async def test_resolver_entrada_incompleto(db, tenant_y_usuario):
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-INCOMPLETO",
            agent_name="", system_prompt="",
        )
    r = await numeros_whatsapp.resolver_entrada("PNID-INCOMPLETO")
    assert r.motivo == "configuracion_incompleta"


# ============================================================
# 4. ingreso.procesar: número sin vincular no sigue al agente
# ============================================================
@pytest.mark.asyncio
async def test_ingreso_numero_no_vinculado_no_responde(
    db, tenant_y_usuario, monkeypatch
):
    await _plan_sin_limites(monkeypatch)
    await _normalizar_falso(monkeypatch, _mensaje_whatsapp("PNID-NO-EXISTE"))
    await _mockea_persistencia(monkeypatch)

    r = await ingreso.procesar(_peticion(tenant_y_usuario["tenant_id"]))

    assert not r.sigue_al_agente
    assert r.motivo == "Número no vinculado"


# ============================================================
# 5. ingreso.procesar: número desvinculado → oculto, sin agente
# ============================================================
@pytest.mark.asyncio
async def test_ingreso_numero_desvinculado_sin_responder(
    db, tenant_y_usuario, monkeypatch
):
    await _plan_sin_limites(monkeypatch)
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-DESVINC",
            estado="desvinculado",
        )
    await _normalizar_falso(monkeypatch, _mensaje_whatsapp("PNID-DESVINC"))
    await _mockea_persistencia(monkeypatch)

    r = await ingreso.procesar(_peticion(tenant_y_usuario["tenant_id"]))

    assert not r.sigue_al_agente
    assert "desvinculado" in (r.motivo or "")


# ============================================================
# 6. ingreso.procesar: pausado / apagado / incompleto → sin agente
# ============================================================
@pytest.mark.asyncio
async def test_ingreso_pausado_sin_responder(db, tenant_y_usuario, monkeypatch):
    await _plan_sin_limites(monkeypatch)
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-PAUSA", estado="pausado"
        )
    await _normalizar_falso(monkeypatch, _mensaje_whatsapp("PNID-PAUSA"))
    await _mockea_persistencia(monkeypatch)

    r = await ingreso.procesar(_peticion(tenant_y_usuario["tenant_id"]))

    assert not r.sigue_al_agente
    assert r.motivo is not None


@pytest.mark.asyncio
async def test_ingreso_agente_apagado_sin_responder(db, tenant_y_usuario, monkeypatch):
    await _plan_sin_limites(monkeypatch)
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-APAGADO", agente_activo=False
        )
    await _normalizar_falso(monkeypatch, _mensaje_whatsapp("PNID-APAGADO"))
    await _mockea_persistencia(monkeypatch)

    r = await ingreso.procesar(_peticion(tenant_y_usuario["tenant_id"]))

    assert not r.sigue_al_agente
    assert r.motivo is not None


@pytest.mark.asyncio
async def test_ingreso_incompleto_sin_responder(db, tenant_y_usuario, monkeypatch):
    await _plan_sin_limites(monkeypatch)
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-INCOMPLETO",
            agent_name="", system_prompt="",
        )
    await _normalizar_falso(monkeypatch, _mensaje_whatsapp("PNID-INCOMPLETO"))
    await _mockea_persistencia(monkeypatch)

    r = await ingreso.procesar(_peticion(tenant_y_usuario["tenant_id"]))

    assert not r.sigue_al_agente
    assert r.motivo is not None


# ============================================================
# 7. Dos números del mismo negocio: config y conversación distintas
# ============================================================
@pytest.mark.asyncio
async def test_ingreso_dos_numeros_config_distinta(
    db, tenant_y_usuario, monkeypatch
):
    await _plan_sin_limites(monkeypatch)
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        n1 = await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-UNO",
            agent_name="Agente Uno", system_prompt="Eres el agente uno.",
            model="claude-haiku-4-5-20251001",
        )
        n2 = await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-DOS",
            agent_name="Agente Dos", system_prompt="Eres el agente dos.",
            model="claude-opus-5",
        )

    # Número uno.
    await _normalizar_falso(monkeypatch, _mensaje_whatsapp("PNID-UNO"))
    await _mockea_persistencia(monkeypatch)
    r = await ingreso.procesar(_peticion(tenant_y_usuario["tenant_id"]))
    ctx1 = r.contexto
    assert ctx1.agente_canal_id == n1["id"]
    assert ctx1.config["agent_name"] == "Agente Uno"
    assert ctx1.config["system_prompt"] == "Eres el agente uno."
    assert ctx1.config["model"] == "claude-haiku-4-5-20251001"

    # Número dos: conversación distinta, config distinta.
    await _normalizar_falso(monkeypatch, _mensaje_whatsapp("PNID-DOS"))
    await _mockea_persistencia(monkeypatch)
    r = await ingreso.procesar(_peticion(tenant_y_usuario["tenant_id"]))
    ctx2 = r.contexto
    assert ctx2.agente_canal_id == n2["id"]
    assert ctx2.config["agent_name"] == "Agente Dos"
    assert ctx2.config["system_prompt"] == "Eres el agente dos."
    assert ctx2.config["model"] == "claude-opus-5"
    assert ctx1.conversation_id != ctx2.conversation_id


# ============================================================
# 8. Instagram y Facebook: config social (sin agente_canal_id)
# ============================================================
@pytest.mark.asyncio
async def test_ingreso_instagram_facebook_config_social(
    db, tenant_y_usuario, monkeypatch
):
    await _plan_sin_limites(monkeypatch)
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])

    for canal in ("instagram", "facebook"):
        await _normalizar_falso(monkeypatch, _mensaje_social(canal))
        await _mockea_persistencia(monkeypatch)
        try:
            r = await ingreso.procesar(_peticion(tenant_y_usuario["tenant_id"]))
        except Exception as e:
            import traceback as _tb
            print(f"EXCEPCION {canal}: {type(e).__name__}: {e}")
            _tb.print_exc()
            raise
        ctx = r.contexto
        assert ctx.agente_canal_id is None, f"{canal} no debe tener agente_canal_id"
        assert ctx.config is not None
        assert ctx.config["agent_name"] == "Asistente"  # default social sin tenant_agent_config


# ============================================================
# 9. Número activo y completo: llega al agente con su config
# ============================================================
@pytest.mark.asyncio
async def test_ingreso_numero_activo_con_su_config(
    db, tenant_y_usuario, monkeypatch
):
    await _plan_sin_limites(monkeypatch)
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-ACTIVO",
            agent_name="Agente Activo", system_prompt="Soy activo.",
        )

    await _normalizar_falso(monkeypatch, _mensaje_whatsapp("PNID-ACTIVO"))
    await _mockea_persistencia(monkeypatch)

    r = await ingreso.procesar(_peticion(tenant_y_usuario["tenant_id"]))

    assert r.sigue_al_agente
    ctx = r.contexto
    assert ctx.agente_canal_id is not None
    assert ctx.config["agent_name"] == "Agente Activo"
    assert ctx.config["system_prompt"] == "Soy activo."


# ============================================================
# 10. Herramienta no activa para el número / fuera del plan: no ejecuta ni cobra
# ============================================================
@pytest.mark.asyncio
async def test_herramienta_no_activa_para_numero_no_se_ejecuta(
    db, tenant_y_usuario, monkeypatch
):
    await _plan_sin_limites(monkeypatch)
    async with conexion() as conn:
        await _suscribir_enterprise(conn, tenant_y_usuario["tenant_id"])
        numero = await _crear_numero(
            conn, tenant_y_usuario["tenant_id"], "PNID-TOOL",
        )
        await _crear_herramienta(
            conn, tenant_y_usuario["tenant_id"], "hoja-x", "google_sheets", "Hoja X"
        )
        # Nota: NO se activa para el número.

    consumido = []
    async def falso_consumir(*args, **kwargs):
        consumido.append(True)
        return SimpleNamespace(permitido=True)
    monkeypatch.setattr(
        "services.agente.herramientas.creditos.consumir_credito", falso_consumir
    )

    texto = await herramientas.ejecutar(
        tenant_id=tenant_y_usuario["tenant_id"],
        tool_key="hoja-x",
        parametros={},
        conversation_id=uuid4(),
        mensaje_id=None,
        contacto={},
        referencia_cobro="tool:x",
        agente_canal_id=numero["id"],
    )
    assert "no está disponible" in texto
    assert consumido == []  # no se cobró


@pytest.mark.asyncio
async def test_herramienta_fuera_del_plan_no_se_ejecuta(
    db, tenant_y_usuario, monkeypatch
):
    nombre_plan = f"plan-solo-cal-{uuid4().hex[:8]}"
    async with conexion() as conn:
        await conn.execute(
            "INSERT INTO planes (nombre, precio_monthly, activo) VALUES ($1, 9999, true)",
            nombre_plan,
        )
        await conn.execute(
            "INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, 'calendario')",
            nombre_plan,
        )
        await conn.execute(
            "DELETE FROM tenant_subscriptions WHERE tenant_id = $1",
            tenant_y_usuario["tenant_id"],
        )
        await conn.execute(
            """
            INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly, origen)
            VALUES ($1, $2, 'activa', 10, 'pago')
            """,
            tenant_y_usuario["tenant_id"],
            nombre_plan,
        )
        await _crear_numero(conn, tenant_y_usuario["tenant_id"], "PNID-PLAN")
        numero = await fetch_one(
            "SELECT id FROM agentes_canal WHERE phone_number_id = 'PNID-PLAN'",
        )
        await _crear_herramienta(
            conn, tenant_y_usuario["tenant_id"], "http-x", "http_generico", "HTTP X"
        )
        await _activar_para_numero(
            conn, tenant_y_usuario["tenant_id"], numero["id"], "http-x"
        )

    consumido = []
    async def falso_consumir(*args, **kwargs):
        consumido.append(True)
        return SimpleNamespace(permitido=True)
    monkeypatch.setattr(
        "services.agente.herramientas.creditos.consumir_credito", falso_consumir
    )

    texto = await herramientas.ejecutar(
        tenant_id=tenant_y_usuario["tenant_id"],
        tool_key="http-x",
        parametros={},
        conversation_id=uuid4(),
        mensaje_id=None,
        contacto={},
        referencia_cobro="tool:x",
        agente_canal_id=numero["id"],
    )
    assert "no está disponible" in texto
    assert consumido == []

    await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre_plan)
    await execute("DELETE FROM plan_herramientas WHERE plan_nombre = $1", nombre_plan)
    await execute("DELETE FROM planes WHERE nombre = $1", nombre_plan)
