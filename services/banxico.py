"""
Tipo de cambio USD/MXN para el margen del panel de plataforma (ver
routers/gerencia.py y services/gerencia.py).

Fuente preferida: el SIE API de Banxico, serie SF43718 ("Tipo de cambio
para solventar obligaciones... FIX"). Es el dato oficial de pesos por
dólar que Banxico publica una vez al día (~18:00 hora CDMX).

Se cachea en memoria con un TTL de unas horas: pedirlo en cada request del
panel no traería un dato más fresco, solo carga extra contra el SIE API.
Si la llamada falla (token vencido, Banxico caído, sin red), se sirve el
último valor en caché aunque esté vencido — un margen con el FIX de ayer
es mejor que uno que desaparece por una caída momentánea del servicio. Si
nunca hubo un valor en caché, cae al TIPO_CAMBIO_USD manual de settings.

Nunca lanza: un fallo acá no puede tumbar /gerencia/resumen ni el listado
de negocios, que tienen mucho más para mostrar que el margen.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

import httpx

from config import settings

log = logging.getLogger("operativai.banxico")

SERIE_FIX = "SF43718"
BASE_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1/series"

# Banxico publica el FIX una vez al día; unas horas de caché alcanzan para
# no pegarle al SIE API en cada petición del panel sin arriesgarse a
# servir un valor de varios días si el backend queda prendido sin tráfico
# (el fallback a caché vencida de abajo ya cubre ese caso igual).
CACHE_TTL = timedelta(hours=6)

FuenteTipoCambio = Literal["moneda_usd", "banxico", "manual", "ninguno"]


@dataclass
class TipoCambio:
    valor: Decimal | None
    fuente: FuenteTipoCambio


@dataclass
class _Cache:
    valor: Decimal | None = None
    obtenido_en: datetime | None = None

    def vigente(self, ahora: datetime) -> bool:
        return (
            self.valor is not None
            and self.obtenido_en is not None
            and ahora - self.obtenido_en < CACHE_TTL
        )


_cache = _Cache()
# Serializa los refrescos: sin esto, N peticiones concurrentes con la
# caché vencida dispararían N llamadas simultáneas al SIE API por el
# mismo dato.
_lock = asyncio.Lock()


async def _pedir_fix() -> Decimal:
    """Trae el dato más reciente de la serie. Lanza en cualquier fallo."""
    url = f"{BASE_URL}/{SERIE_FIX}/datos/oportuno"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"Bmx-Token": settings.BANXICO_TOKEN})
        r.raise_for_status()
        cuerpo = r.json()

    try:
        dato = cuerpo["bmx"]["series"][0]["datos"][0]["dato"]
        valor = Decimal(dato.replace(",", ""))
    except (KeyError, IndexError, InvalidOperation, AttributeError) as e:
        raise ValueError(f"Respuesta inesperada del SIE API: {cuerpo}") from e

    if valor <= 0:
        raise ValueError(f"El SIE API devolvió un FIX no positivo: {valor}")
    return valor


async def _tipo_cambio_banxico() -> Decimal | None:
    """
    Valor cacheado si sigue vigente; si no, lo refresca. Devuelve el
    último valor conocido (aunque esté vencido) si el refresco falla, o
    None si nunca se pudo obtener nada.
    """
    ahora = datetime.now(timezone.utc)
    if _cache.vigente(ahora):
        return _cache.valor

    async with _lock:
        # Otra corrutina pudo haber refrescado mientras se esperaba el lock.
        ahora = datetime.now(timezone.utc)
        if _cache.vigente(ahora):
            return _cache.valor

        try:
            _cache.valor = await _pedir_fix()
            _cache.obtenido_en = ahora
        except Exception as e:
            log.warning(
                "No se pudo obtener el FIX de Banxico (se usa el último valor en "
                "caché si hay uno): %s",
                e,
            )

    return _cache.valor


async def obtener_tipo_cambio() -> TipoCambio:
    """
    Decide la fuente y devuelve el tipo de cambio final, en ese orden:

    1. Moneda de cobro ya es USD → 1, sin llamar a nadie.
    2. Moneda de cobro es MXN y hay BANXICO_TOKEN → el FIX (con su caché
       y su fallback a caché vencida).
    3. TIPO_CAMBIO_USD manual, si está configurado.
    4. Ninguno: el panel muestra el margen como no disponible.
    """
    if settings.moneda_cobro == "usd":
        return TipoCambio(valor=Decimal(1), fuente="moneda_usd")

    if settings.banxico_configurado:
        valor = await _tipo_cambio_banxico()
        if valor is not None:
            return TipoCambio(valor=valor, fuente="banxico")

    manual = settings.tipo_cambio_usd_manual
    if manual is not None:
        return TipoCambio(valor=manual, fuente="manual")

    return TipoCambio(valor=None, fuente="ninguno")


def _reiniciar_cache_para_tests() -> None:
    """Solo para tests: la caché es de módulo y sobrevive entre casos."""
    _cache.valor = None
    _cache.obtenido_en = None
