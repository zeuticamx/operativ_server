"""
Tests del historial que se le manda al modelo (services/agente/prompt.py).

Cubre sobre todo los cortes de contexto (spec 003): el dueño pulsa "limpiar
el contexto" en el portal y a partir de ahí el agente deja de ver lo
anterior, pero los mensajes NO se borran — el historial del portal sigue
completo.

Lo que importa si falla:

  1. sin corte, el agente ve la conversación entera (hasta la ventana)
  2. con corte, solo lo posterior
  3. si el dueño limpia dos veces, manda el corte más reciente
  4. el corte de otra conversación no afecta a esta
  5. los mensajes de esta misma pasada no se duplican como historial

Usa la base real, como el resto de tests del repo.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from services.agente.prompt import historial
from services.agente.modelos import ContextoMensaje
from session import execute, fetch_value

# Las horas las calcula Postgres con `now()`, no Python: las dos columnas que
# entran en juego tienen tipos distintos (`messages.created_at` es timestamp
# sin zona, `conversacion_contexto_cortes.creado_en` es timestamptz) y
# mezclar el reloj y la zona de la máquina con los de la base haría que el
# test midiera eso en vez de la regla del corte.


@pytest.fixture
async def conversacion(tenant_y_usuario):
    """
    Una conversación de WhatsApp con cliente, lista para colgarle mensajes.

    El tenant lo borra la fixture de la que depende, y el CASCADE se lleva
    usuario, conversación, mensajes y cortes.
    """
    tenant_id = tenant_y_usuario["tenant_id"]

    user_id = await fetch_value(
        """
        INSERT INTO users (tenant_id, whatsapp_id, display_name)
        VALUES ($1, $2, 'Ana')
        RETURNING id
        """,
        tenant_id,
        f"52155{uuid4().hex[:8]}",
    )
    conversation_id = await fetch_value(
        """
        INSERT INTO conversations (tenant_id, user_id, channel_type, status)
        VALUES ($1, $2, 'whatsapp', 'active')
        RETURNING id
        """,
        tenant_id,
        user_id,
    )
    return {"tenant_id": tenant_id, "user_id": user_id, "conversation_id": conversation_id}


async def agregar(conversacion: dict, role: str, content: str, hace_minutos: int) -> UUID:
    """Un mensaje fechado hace N minutos, para poder ordenarlos en el test."""
    return await fetch_value(
        """
        INSERT INTO messages (conversation_id, tenant_id, role, content, created_at)
        VALUES ($1, $2, $3, $4, now()::timestamp - make_interval(mins => $5))
        RETURNING id
        """,
        conversacion["conversation_id"],
        conversacion["tenant_id"],
        role,
        content,
        hace_minutos,
    )


async def cortar(conversacion: dict, hace_minutos: int) -> None:
    """Lo que hace el botón 'limpiar el contexto' del portal."""
    await execute(
        """
        INSERT INTO conversacion_contexto_cortes (tenant_id, conversation_id, creado_en)
        VALUES ($1, $2, now() - make_interval(mins => $3))
        """,
        conversacion["tenant_id"],
        conversacion["conversation_id"],
        hace_minutos,
    )


def contexto_de(conversacion: dict, ids_actuales: list[UUID]) -> ContextoMensaje:
    return ContextoMensaje(
        tenant_id=conversacion["tenant_id"],
        conversation_id=conversacion["conversation_id"],
        user_id=conversacion["user_id"],
        canal="whatsapp",
        usuario_id="5215599999999",
        nombre="Ana",
        contenido="¿y el precio?",
        tipo="text",
        mensaje_id="wamid.actual",
        db_message_id=ids_actuales[0],
        # `historial()` no lo usa (filtra por ids_agrupados); va por completar
        # la estructura.
        db_message_created_at=datetime.now(timezone.utc),
        ids_agrupados=ids_actuales,
    )


# ============================================================
async def test_sin_corte_ve_toda_la_conversacion(conversacion):
    await agregar(conversacion, "user", "hola", 30)
    await agregar(conversacion, "assistant", "¡Hola! ¿En qué te ayudo?", 29)
    actual = await agregar(conversacion, "user", "¿y el precio?", 0)

    turnos = await historial(contexto_de(conversacion, [actual]), 30)

    assert [t["content"] for t in turnos] == ["hola", "¡Hola! ¿En qué te ayudo?"]


async def test_con_corte_solo_ve_lo_posterior(conversacion):
    """El caso que el botón existe para resolver: la conversación se enredó."""
    await agregar(conversacion, "user", "pregunta vieja y confusa", 60)
    await agregar(conversacion, "assistant", "respuesta equivocada", 59)
    await cortar(conversacion, 30)
    await agregar(conversacion, "user", "empecemos de nuevo", 20)
    actual = await agregar(conversacion, "user", "¿y el precio?", 0)

    turnos = await historial(contexto_de(conversacion, [actual]), 30)

    assert [t["content"] for t in turnos] == ["empecemos de nuevo"]


async def test_los_mensajes_no_se_borran(conversacion):
    """
    El corte solo cambia lo que ve el modelo. El historial del portal sigue
    completo: si esto fallara, el negocio perdería la conversación.
    """
    await agregar(conversacion, "user", "pregunta vieja", 60)
    await cortar(conversacion, 30)
    actual = await agregar(conversacion, "user", "¿y el precio?", 0)

    turnos = await historial(contexto_de(conversacion, [actual]), 30)
    assert turnos == []

    total = await fetch_value(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = $1",
        conversacion["conversation_id"],
    )
    assert total == 2


async def test_manda_el_corte_mas_reciente(conversacion):
    """Cada pulsación añade una fila; vale la última."""
    await agregar(conversacion, "user", "muy viejo", 90)
    await cortar(conversacion, 60)
    await agregar(conversacion, "user", "viejo", 50)
    await cortar(conversacion, 30)
    await agregar(conversacion, "user", "reciente", 20)
    actual = await agregar(conversacion, "user", "¿y el precio?", 0)

    turnos = await historial(contexto_de(conversacion, [actual]), 30)

    assert [t["content"] for t in turnos] == ["reciente"]


async def test_el_corte_de_otra_conversacion_no_afecta(conversacion, tenant_y_usuario):
    """El corte es por conversación, no por negocio."""
    otra = await fetch_value(
        """
        INSERT INTO conversations (tenant_id, user_id, channel_type, status)
        VALUES ($1, $2, 'whatsapp', 'active')
        RETURNING id
        """,
        conversacion["tenant_id"],
        conversacion["user_id"],
    )
    await execute(
        """
        INSERT INTO conversacion_contexto_cortes (tenant_id, conversation_id, creado_en)
        VALUES ($1, $2, now() - make_interval(mins => 1))
        """,
        conversacion["tenant_id"],
        otra,
    )

    await agregar(conversacion, "user", "sigue visible", 30)
    actual = await agregar(conversacion, "user", "¿y el precio?", 0)

    turnos = await historial(contexto_de(conversacion, [actual]), 30)

    assert [t["content"] for t in turnos] == ["sigue visible"]


# ============================================================
async def test_los_mensajes_de_esta_pasada_no_se_duplican(conversacion):
    """
    Los mensajes que se acaban de guardar viajan como la pregunta; incluirlos
    también en el historial se los mandaría al modelo dos veces.
    """
    await agregar(conversacion, "user", "hola", 30)
    uno = await agregar(conversacion, "user", "oye", 1)
    dos = await agregar(conversacion, "user", "¿y el precio?", 0)

    turnos = await historial(contexto_de(conversacion, [uno, dos]), 30)

    assert [t["content"] for t in turnos] == ["hola"]


async def test_la_ventana_recorta_por_los_mas_recientes(conversacion):
    for i in range(10):
        await agregar(conversacion, "user", f"mensaje {i}", 30 - i)
    actual = await agregar(conversacion, "user", "¿y el precio?", 0)

    turnos = await historial(contexto_de(conversacion, [actual]), 3)

    # Los tres últimos, en orden cronológico.
    assert [t["content"] for t in turnos] == ["mensaje 7", "mensaje 8", "mensaje 9"]
