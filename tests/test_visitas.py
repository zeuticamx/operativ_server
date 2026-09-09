"""
Check-in y sincronización de la cola offline.

Se sustituyen las dos funciones que tocan la base (`cargar_cliente` e
`insertar_visita`) y la transacción, igual que en test_eventos.py: lo que
se prueba acá es el flujo y la idempotencia, no la SQL.

La idempotencia real tiene dos capas y las dos se cubren:
  - dentro del lote  -> `dedupe_lote`, puro
  - contra la base   -> el ON CONFLICT del índice único parcial, simulado
                        aquí con un almacén falso y verificado contra
                        Postgres de verdad en la comprobación de esquema.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from routers import visitas
from deps import VendedorActual
from schemas import CheckinIn, CheckinSyncIn, SyncIn

# Zócalo de la CDMX: el punto donde vive el cliente de prueba.
LAT_CLIENTE, LON_CLIENTE = 19.432608, -99.133209
# ~50 m al norte: dentro de la tolerancia de 120 m.
LAT_CERCA, LON_CERCA = 19.433057, -99.133209
# Guadalajara: sin discusión, fuera.
LAT_LEJOS, LON_LEJOS = 20.676667, -103.347222

TENANT = uuid4()
VENDEDOR = uuid4()
CLIENTE = uuid4()
OTRO_CLIENTE = uuid4()


def vendedor_prueba() -> VendedorActual:
    return VendedorActual(
        id=VENDEDOR,
        tenant_id=TENANT,
        nombre="Ana",
        portal_user_id=uuid4(),
        email="ana@ejemplo.com",
    )


def fila_cliente(radio: int = 120, cliente_id: UUID = CLIENTE) -> dict:
    return {
        "id": cliente_id,
        "tenant_id": TENANT,
        "vendedor_id": VENDEDOR,
        "nombre_negocio": "Abarrotes La Esquina",
        "latitud": LAT_CLIENTE,
        "longitud": LON_CLIENTE,
        "radio_tolerancia_metros": radio,
    }


class BaseFalsa:
    """
    Almacén en memoria que imita el ON CONFLICT del índice único parcial
    sobre `cliente_uuid_offline`.
    """

    def __init__(self, radio: int = 120):
        self.radio = radio
        self.visitas: list[dict] = []
        self.por_uuid: dict[UUID, dict] = {}
        self.clientes_validos = {CLIENTE, OTRO_CLIENTE}

    async def cargar_cliente(self, cliente_id, acceso, conn=None):
        if cliente_id not in self.clientes_validos:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        return fila_cliente(self.radio, cliente_id)

    async def insertar_visita(
        self, conn, tenant_id, vendedor_id, cliente_id, datos, resultado
    ):
        uuid_offline = datos.cliente_uuid_offline

        # El índice es parcial: solo choca cuando el uuid no es NULL.
        if uuid_offline is not None and uuid_offline in self.por_uuid:
            return self.por_uuid[uuid_offline], False

        fila = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "vendedor_id": vendedor_id,
            "vendedor_nombre": "Ana",
            "cliente_id": cliente_id,
            "cliente_nombre_negocio": "Abarrotes La Esquina",
            "latitud": datos.latitud,
            "longitud": datos.longitud,
            "accuracy_metros": datos.accuracy_metros,
            "distancia_calculada_metros": resultado.distancia_metros,
            "dentro_de_geocerca": resultado.dentro,
            "foto_url": datos.foto_url,
            "comentario": datos.comentario,
            "timestamp_dispositivo": datos.timestamp_dispositivo,
            "timestamp_servidor": datetime.now(timezone.utc),
            "cliente_uuid_offline": uuid_offline,
            "creado_en": datetime.now(timezone.utc),
        }
        self.visitas.append(fila)
        if uuid_offline is not None:
            self.por_uuid[uuid_offline] = fila
        return fila, True


@pytest.fixture
def base(monkeypatch):
    bd = BaseFalsa()

    @asynccontextmanager
    async def fake_transaccion():
        yield object()

    monkeypatch.setattr(visitas, "cargar_cliente", bd.cargar_cliente)
    monkeypatch.setattr(visitas, "insertar_visita", bd.insertar_visita)
    monkeypatch.setattr(visitas, "transaccion", fake_transaccion)
    return bd


# ============================================================
# CHECK-IN dentro de la geocerca
# ============================================================
async def test_checkin_dentro_de_la_geocerca(base):
    r = await visitas.checkin(
        CheckinIn(cliente_id=CLIENTE, latitud=LAT_CERCA, longitud=LON_CERCA),
        vendedor_prueba(),
    )

    assert r.dentro_de_geocerca is True
    assert r.visita.dentro_de_geocerca is True
    assert r.distancia_metros < 120
    assert r.exceso_metros == 0.0
    assert "validada" in r.mensaje


async def test_checkin_en_el_punto_exacto(base):
    r = await visitas.checkin(
        CheckinIn(cliente_id=CLIENTE, latitud=LAT_CLIENTE, longitud=LON_CLIENTE),
        vendedor_prueba(),
    )
    assert r.dentro_de_geocerca is True
    assert r.distancia_metros == pytest.approx(0.0, abs=0.1)


async def test_la_visita_guarda_el_veredicto_calculado_en_servidor(base):
    """
    El teléfono no manda `dentro_de_geocerca`: no está en el esquema de
    entrada. El valor sale del cálculo contra las coordenadas guardadas.
    """
    await visitas.checkin(
        CheckinIn(cliente_id=CLIENTE, latitud=LAT_CERCA, longitud=LON_CERCA),
        vendedor_prueba(),
    )
    guardada = base.visitas[0]
    assert guardada["dentro_de_geocerca"] is True
    assert guardada["distancia_calculada_metros"] > 0
    assert "dentro_de_geocerca" not in CheckinIn.model_fields


# ============================================================
# CHECK-IN fuera de la geocerca
# ============================================================
async def test_checkin_fuera_de_la_geocerca(base):
    r = await visitas.checkin(
        CheckinIn(cliente_id=CLIENTE, latitud=LAT_LEJOS, longitud=LON_LEJOS),
        vendedor_prueba(),
    )

    assert r.dentro_de_geocerca is False
    assert r.visita.dentro_de_geocerca is False
    assert r.exceso_metros > 400_000
    assert "FUERA" in r.mensaje


async def test_una_visita_fuera_se_guarda_igual(base):
    """
    No es un error del cliente: es un hecho que gerencia necesita ver en
    el reporte. Se registra y se responde 201.
    """
    await visitas.checkin(
        CheckinIn(cliente_id=CLIENTE, latitud=LAT_LEJOS, longitud=LON_LEJOS),
        vendedor_prueba(),
    )
    assert len(base.visitas) == 1
    assert base.visitas[0]["dentro_de_geocerca"] is False


async def test_el_radio_del_cliente_decide_el_veredicto(base):
    """La misma lectura, con un cliente de radio amplio, sí valida."""
    base.radio = 1000
    lejos_pero_dentro = (19.436000, -99.133209)  # ~375 m
    r = await visitas.checkin(
        CheckinIn(
            cliente_id=CLIENTE,
            latitud=lejos_pero_dentro[0],
            longitud=lejos_pero_dentro[1],
        ),
        vendedor_prueba(),
    )
    assert r.radio_metros == 1000
    assert r.dentro_de_geocerca is True


async def test_cliente_inexistente_da_404(base):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await visitas.checkin(
            CheckinIn(cliente_id=uuid4(), latitud=LAT_CERCA, longitud=LON_CERCA),
            vendedor_prueba(),
        )
    assert exc.value.status_code == 404


# ============================================================
# Coordenadas fuera de rango (las corta Pydantic, no el endpoint)
# ============================================================
@pytest.mark.parametrize(
    "lat, lon",
    [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)],
)
def test_coordenadas_imposibles_no_pasan_validacion(lat, lon):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CheckinIn(cliente_id=CLIENTE, latitud=lat, longitud=lon)


def test_accuracy_negativa_no_pasa_validacion():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CheckinIn(
            cliente_id=CLIENTE,
            latitud=LAT_CERCA,
            longitud=LON_CERCA,
            accuracy_metros=-5,
        )


# ============================================================
# dedupe_lote (puro)
# ============================================================
def checkin_sync(uuid_offline: UUID, lat=LAT_CERCA, lon=LON_CERCA) -> CheckinSyncIn:
    return CheckinSyncIn(
        cliente_id=CLIENTE,
        latitud=lat,
        longitud=lon,
        cliente_uuid_offline=uuid_offline,
    )


def test_dedupe_conserva_la_primera_aparicion():
    u1, u2 = uuid4(), uuid4()
    lote = [checkin_sync(u1), checkin_sync(u2), checkin_sync(u1)]
    unicas, repetidas = visitas.dedupe_lote(lote)

    assert [v.cliente_uuid_offline for v in unicas] == [u1, u2]
    assert [v.cliente_uuid_offline for v in repetidas] == [u1]


def test_dedupe_sin_repetidos_no_cambia_nada():
    lote = [checkin_sync(uuid4()) for _ in range(5)]
    unicas, repetidas = visitas.dedupe_lote(lote)
    assert len(unicas) == 5
    assert repetidas == []


def test_dedupe_lote_entero_repetido():
    u = uuid4()
    unicas, repetidas = visitas.dedupe_lote([checkin_sync(u) for _ in range(4)])
    assert len(unicas) == 1
    assert len(repetidas) == 3


# ============================================================
# SYNC — idempotencia
# ============================================================
async def test_sync_crea_las_visitas_del_lote(base):
    lote = SyncIn(visitas=[checkin_sync(uuid4()) for _ in range(3)])
    r = await visitas.sync(lote, vendedor_prueba())

    assert r.recibidas == 3
    assert r.creadas == 3
    assert r.duplicadas == 0
    assert r.rechazadas == 0
    assert len(base.visitas) == 3
    assert all(x.aceptada for x in r.resultados)


async def test_reenviar_el_mismo_lote_no_duplica(base):
    """
    El caso que justifica el `cliente_uuid_offline`: la app sube su cola,
    se corta antes de recibir la respuesta y reintenta el lote entero.
    """
    lote = SyncIn(visitas=[checkin_sync(uuid4()) for _ in range(3)])

    primera = await visitas.sync(lote, vendedor_prueba())
    segunda = await visitas.sync(lote, vendedor_prueba())

    assert primera.creadas == 3
    assert segunda.creadas == 0
    assert segunda.duplicadas == 3
    # Lo importante: en la base sigue habiendo tres, no seis.
    assert len(base.visitas) == 3


async def test_al_reenviar_se_devuelven_los_ids_originales(base):
    """La app necesita el id real para borrar el elemento de su cola."""
    lote = SyncIn(visitas=[checkin_sync(uuid4()) for _ in range(2)])

    primera = await visitas.sync(lote, vendedor_prueba())
    segunda = await visitas.sync(lote, vendedor_prueba())

    ids_1 = {r.cliente_uuid_offline: r.visita_id for r in primera.resultados}
    ids_2 = {r.cliente_uuid_offline: r.visita_id for r in segunda.resultados}
    assert ids_1 == ids_2
    assert all(v is not None for v in ids_2.values())


async def test_lote_parcialmente_nuevo(base):
    """Reintento con elementos ya subidos más otros nuevos."""
    viejos = [checkin_sync(uuid4()) for _ in range(2)]
    await visitas.sync(SyncIn(visitas=viejos), vendedor_prueba())

    nuevos = [checkin_sync(uuid4()) for _ in range(3)]
    r = await visitas.sync(SyncIn(visitas=viejos + nuevos), vendedor_prueba())

    assert r.recibidas == 5
    assert r.creadas == 3
    assert r.duplicadas == 2
    assert len(base.visitas) == 5


async def test_repetido_dentro_del_mismo_lote(base):
    """Una cola local puede traer el mismo elemento dos veces."""
    u = uuid4()
    r = await visitas.sync(
        SyncIn(visitas=[checkin_sync(u), checkin_sync(u)]), vendedor_prueba()
    )

    assert r.recibidas == 2
    assert r.creadas == 1
    assert r.duplicadas == 1
    assert len(base.visitas) == 1
    # Los dos resultados apuntan a la misma visita, para que la app borre
    # ambos elementos de su cola.
    ids = {x.visita_id for x in r.resultados}
    assert len(ids) == 1


async def test_sync_conserva_el_veredicto_de_cada_visita(base):
    dentro = checkin_sync(uuid4(), LAT_CERCA, LON_CERCA)
    fuera = checkin_sync(uuid4(), LAT_LEJOS, LON_LEJOS)

    r = await visitas.sync(SyncIn(visitas=[dentro, fuera]), vendedor_prueba())

    por_uuid = {x.cliente_uuid_offline: x for x in r.resultados}
    assert por_uuid[dentro.cliente_uuid_offline].dentro_de_geocerca is True
    assert por_uuid[fuera.cliente_uuid_offline].dentro_de_geocerca is False


async def test_un_elemento_malo_no_tumba_el_lote(base):
    """
    Cada elemento va en su propia transacción. Un cliente borrado mientras
    la app estaba sin señal no puede costar el resto de la cola.
    """
    bueno1 = checkin_sync(uuid4())
    malo = CheckinSyncIn(
        cliente_id=uuid4(),  # no existe
        latitud=LAT_CERCA,
        longitud=LON_CERCA,
        cliente_uuid_offline=uuid4(),
    )
    bueno2 = checkin_sync(uuid4())

    r = await visitas.sync(SyncIn(visitas=[bueno1, malo, bueno2]), vendedor_prueba())

    assert r.creadas == 2
    assert r.rechazadas == 1
    assert len(base.visitas) == 2

    rechazado = next(x for x in r.resultados if not x.aceptada)
    assert rechazado.cliente_uuid_offline == malo.cliente_uuid_offline
    assert rechazado.visita_id is None
    assert "no encontrado" in (rechazado.error or "").lower()


async def test_el_uuid_offline_es_obligatorio_al_sincronizar():
    """Sin él no hay forma de saber si el elemento ya se procesó."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CheckinSyncIn(cliente_id=CLIENTE, latitud=LAT_CERCA, longitud=LON_CERCA)


def test_el_lote_tiene_tope():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SyncIn(visitas=[checkin_sync(uuid4()) for _ in range(201)])

    with pytest.raises(ValidationError):
        SyncIn(visitas=[])


# ============================================================
# Aislamiento entre vendedores
# ============================================================
async def test_el_vendedor_y_el_tenant_salen_del_jwt_no_del_body(base):
    """
    Ni `vendedor_id` ni `tenant_id` existen en el esquema de entrada: no
    hay forma de que el teléfono los mande.
    """
    assert "vendedor_id" not in CheckinIn.model_fields
    assert "tenant_id" not in CheckinIn.model_fields

    await visitas.checkin(
        CheckinIn(cliente_id=CLIENTE, latitud=LAT_CERCA, longitud=LON_CERCA),
        vendedor_prueba(),
    )
    guardada = base.visitas[0]
    assert guardada["vendedor_id"] == VENDEDOR
    assert guardada["tenant_id"] == TENANT
