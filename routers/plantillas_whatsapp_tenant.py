"""
Plantillas de WhatsApp de cada negocio, administradas contra la API de
plantillas de NeuroAPI (Meta Graph). Distinto de `/gerencia/plantillas-whatsapp`
(las dos plantillas globales de los avisos de cita, solo plataforma).

Leer (listar, detalle) es de `negocio_actual`: owner, superadmin y member.
Crear, eliminar y enviar pruebas es de `gerencia_actual`. vendedor y proveedor
reciben 403. `/api/plantillas-whatsapp` está en `RUTAS_SENSIBLES`: desde "ver
como" se puede mirar, pero no crear ni borrar, ni con permiso de edición.

La tabla local es una caché del catálogo de Meta: `GET ?sincronizar=true` la
refresca desde NeuroAPI y el webhook (`message_template_status_update`) mueve
el estado sin polling. Una plantilla de otro negocio responde 404.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from deps import UsuarioActual, gerencia_actual, negocio_actual
from schemas import (
    CategoriaPlantilla,
    EstadoPlantillaMeta,
    PlantillaCrearIn,
    PlantillaPruebaIn,
    PlantillaTenantOut,
)
from services import plantillas_reglas as reglas
from services import plantillas_tenant as cache
from services.neuroapi_plantillas import NeuroApiTemplateService, PlantillaApiError, a_http
from services.whatsapp import ProveedorWhatsAppError
from services.whatsapp import a_http as a_http_envio

router = APIRouter(prefix="/plantillas-whatsapp", tags=["plantillas-whatsapp"])

LIMITE_SINCRONIZACION = 100


def _no_encontrada() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plantilla no encontrada")


def _invalida(e: reglas.PlantillaInvalida) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


def _tenant(usuario: UsuarioActual):
    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )
    return usuario.tenant_id


@router.get("", response_model=list[PlantillaTenantOut])
async def listar_plantillas(
    categoria: CategoriaPlantilla | None = None,
    estado: EstadoPlantillaMeta | None = None,
    sincronizar: bool = False,
    usuario: UsuarioActual = Depends(negocio_actual),
):
    tenant_id = _tenant(usuario)
    if sincronizar:
        try:
            servicio = await NeuroApiTemplateService.desde_tenant(tenant_id)
            remotas = await servicio.listar(limite=LIMITE_SINCRONIZACION)
        except PlantillaApiError as e:
            raise a_http(e)
        await cache.sincronizar(
            tenant_id, remotas, lista_completa=len(remotas) < LIMITE_SINCRONIZACION
        )
    filas = await cache.listar(tenant_id, categoria=categoria, estado=estado)
    return [PlantillaTenantOut(**cache.a_salida(f)) for f in filas]


@router.get("/{nombre}", response_model=PlantillaTenantOut)
async def detalle_plantilla(nombre: str, usuario: UsuarioActual = Depends(negocio_actual)):
    fila = await cache.obtener(_tenant(usuario), nombre)
    if fila is None:
        raise _no_encontrada()
    return PlantillaTenantOut(**cache.a_salida(fila))


@router.post("", response_model=PlantillaTenantOut, status_code=status.HTTP_201_CREATED)
async def crear_plantilla(
    datos: PlantillaCrearIn, usuario: UsuarioActual = Depends(gerencia_actual)
):
    tenant_id = _tenant(usuario)
    try:
        nombre = reglas.validar_nombre(datos.nombre)
        idioma = reglas.validar_idioma(datos.idioma)
        componentes = reglas.construir_componentes(
            cuerpo=datos.cuerpo,
            ejemplos_cuerpo=datos.ejemplos_cuerpo,
            encabezado=datos.encabezado,
            ejemplo_encabezado=datos.ejemplo_encabezado,
            pie=datos.pie,
            botones=[b.model_dump() for b in datos.botones],
        )
    except reglas.PlantillaInvalida as e:
        raise _invalida(e)

    existente = await cache.obtener(tenant_id, nombre)
    if existente is not None and existente["idioma"] == idioma:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe una plantilla con ese nombre e idioma",
        )

    try:
        servicio = await NeuroApiTemplateService.desde_tenant(tenant_id)
        respuesta = await servicio.crear(
            nombre=nombre, idioma=idioma, categoria=datos.categoria, componentes=componentes
        )
    except PlantillaApiError as e:
        raise a_http(e)

    guardada = cache.normalizar_meta({"name": nombre, "language": idioma, **respuesta}) or {}
    await cache.guardar(
        tenant_id,
        {
            **guardada,
            "nombre": nombre,
            "idioma": idioma,
            "categoria": respuesta.get("category") or datos.categoria,
            # Meta contesta PENDING al crear; lo demás lo empuja el webhook.
            "estado": guardada.get("estado", "PENDING"),
            "componentes": componentes,
        },
        creada_por=usuario.id,
        sincronizada=False,
    )
    fila = await cache.obtener(tenant_id, nombre)
    return PlantillaTenantOut(**cache.a_salida(fila))


@router.delete("/{nombre}", status_code=status.HTTP_204_NO_CONTENT)
async def eliminar_plantilla(
    nombre: str,
    confirmar: bool = Query(default=False, description="Debe ser true: el borrado es permanente"),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    tenant_id = _tenant(usuario)
    if not confirmar:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "codigo": "confirmacion_requerida",
                "mensaje": "Eliminar una plantilla es permanente; repite con confirmar=true",
            },
        )
    if await cache.obtener(tenant_id, nombre) is None:
        raise _no_encontrada()
    try:
        servicio = await NeuroApiTemplateService.desde_tenant(tenant_id)
        await servicio.eliminar(nombre)
    except PlantillaApiError as e:
        # Si Meta ya no la tiene, solo falta limpiar la caché.
        if e.status_code != 404:
            raise a_http(e)
    # Spec 007 RF-6: si era la plantilla vigente de un aviso de cita activo, la función se apaga y se alerta.
    await cache.quitar(tenant_id, nombre)


@router.post("/{nombre}/prueba", status_code=status.HTTP_202_ACCEPTED)
async def enviar_prueba(
    nombre: str,
    datos: PlantillaPruebaIn,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    tenant_id = _tenant(usuario)
    fila = await cache.obtener(tenant_id, nombre)
    if fila is None:
        raise _no_encontrada()
    if fila["estado"] != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se pueden enviar plantillas aprobadas por Meta",
        )
    try:
        componentes = reglas.parametros_de_envio(
            fila["componentes"], datos.valores_cuerpo, datos.valor_encabezado
        )
    except reglas.PlantillaInvalida as e:
        raise _invalida(e)
    try:
        proveedor = await NeuroApiTemplateService.proveedor_envio(tenant_id)
        resultado = await proveedor.enviar_plantilla_con_componentes(
            datos.telefono, nombre, fila["idioma"], componentes
        )
    except ProveedorWhatsAppError as e:
        raise a_http_envio(e)
    return {"id_mensaje": resultado.id_mensaje, "estado": resultado.estado}
