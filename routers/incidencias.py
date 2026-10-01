"""
Reporte de incidencias desde el portal: el botón "Reportar un problema".

Cualquier usuario autenticado puede reportar, sea cual sea su rol y su plan:
quien tiene el plan vencido o la cuenta bloqueada por cobro es justamente
quien más puede necesitar avisar de que algo falla, así que esta ruta no
cuelga de `requiere_herramienta`. En "ver como" (impersonación)
deps.usuario_actual ya corta las escrituras con 403: un reporte saldría con
la identidad del cliente y no la de quien está mirando.

Multipart (no JSON) porque lleva una imagen opcional.
"""

from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)

from config import settings
from deps import UsuarioActual, usuario_actual
from schemas import ReporteCreadoOut
from services import incidencias as svc

router = APIRouter(prefix="/incidencias", tags=["incidencias"])


@router.post("", response_model=ReporteCreadoOut, status_code=status.HTTP_201_CREATED)
async def crear_incidencia(
    request: Request,
    tareas: BackgroundTasks,
    resumen: str = Form(...),
    descripcion: str = Form(...),
    contexto: str | None = Form(None),
    adjunto: UploadFile | None = File(None),
    usuario: UsuarioActual = Depends(usuario_actual),
) -> ReporteCreadoOut:
    """
    Guarda el reporte y avisa al equipo de plataforma por correo (en
    background: el reporte ya quedó guardado).

      422 -> resumen o descripción fuera de rango, o adjunto dañado
      413 -> el adjunto pesa de más
      415 -> el adjunto no es PNG/JPG (se decide por el contenido)
      429 -> ya mandó REPORTES_MAX_POR_HORA en la última hora

    El usuario y el negocio salen del token; el formulario no puede
    cambiarlos. `contexto` es JSON que arma el navegador y solo se conservan
    las claves conocidas.
    """
    try:
        resumen_ok = svc.limpiar_texto(
            resumen, svc.RESUMEN_MIN, svc.RESUMEN_MAX, "Resumen", una_linea=True
        )
        descripcion_ok = svc.limpiar_texto(
            descripcion, svc.DESCRIPCION_MIN, svc.DESCRIPCION_MAX, "Descripción", una_linea=False
        )

        archivo = None
        if adjunto is not None and adjunto.filename:
            # Un byte más que el tope: si llega a leerlo, excede, sin tener
            # que cargar entero algo enorme en memoria.
            bytes_ = await adjunto.read(settings.REPORTE_ADJUNTO_MAX_BYTES + 1)
            archivo = svc.validar_adjunto(bytes_, adjunto.filename)
    except svc.ReporteInvalido as e:
        raise HTTPException(status_code=e.status_code, detail=e.mensaje)

    datos_contexto = svc.limpiar_contexto(contexto)
    # La cabecera es lo que el servidor vio de verdad: respaldo si el
    # navegador no mandó (o falseó) su propio user agent.
    if "user_agent" not in datos_contexto:
        cabecera = request.headers.get("user-agent", "").strip()
        if cabecera:
            datos_contexto["user_agent"] = cabecera[: svc.CLAVES_CONTEXTO["user_agent"]]

    try:
        reporte_id: UUID = await svc.crear_reporte(
            portal_user_id=usuario.id,
            tenant_id=usuario.tenant_id,
            email=usuario.email,
            resumen=resumen_ok,
            descripcion=descripcion_ok,
            contexto=datos_contexto,
            adjunto=archivo,
        )
    except svc.LimiteDeReportes as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Ya enviaste {settings.REPORTES_MAX_POR_HORA} reportes en la última hora. "
                "Inténtalo de nuevo más tarde."
            ),
            headers={"Retry-After": str(e.reintentar_en)},
        )

    tareas.add_task(svc.notificar_reporte, reporte_id)
    return ReporteCreadoOut(id=reporte_id)
