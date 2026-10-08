"""
Cuestionario de bienvenida del dueño (services/onboarding.py).

Solo gerencia del negocio (owner/superadmin): es configuración. Sin
`requiere_herramienta` a propósito: una cuenta recién creada no tiene plan,
y justamente esto es lo que se lo recomienda (y le da la prueba).

/api/onboarding está en acceso_soporte.RUTAS_SENSIBLES: desde "ver como"
se puede mirar (GET), pero guardar, aplicar u omitir no, ni con permiso de
edición — aplicar otorga un plan de prueba y eso lo decide el dueño.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from deps import UsuarioActual, gerencia_actual, tenant_actual
from schemas import (
    OnboardingAplicadoOut,
    OnboardingAplicarIn,
    OnboardingOut,
    OnboardingRespuestasIn,
)
from services import onboarding

router = APIRouter(
    prefix="/onboarding",
    tags=["onboarding"],
    dependencies=[Depends(gerencia_actual)],
)


@router.get("", response_model=OnboardingOut)
async def obtener(tenant_id: UUID = Depends(tenant_actual)):
    """Estado, respuestas guardadas y la recomendación que sale de ellas."""
    return await onboarding.obtener(tenant_id)


@router.put("", response_model=OnboardingOut)
async def guardar(datos: OnboardingRespuestasIn, tenant_id: UUID = Depends(tenant_actual)):
    """Guarda las respuestas y devuelve la vista previa. No aplica nada."""
    return await onboarding.guardar_respuestas(tenant_id, datos)


@router.post("/aplicar", response_model=OnboardingAplicadoOut)
async def aplicar(
    datos: OnboardingAplicarIn,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """
    Escribe el prompt, otorga la prueba (si corresponde) y enciende los
    módulos que el plan vigente permite.

      409 todavía no hay respuestas guardadas
    """
    return await onboarding.aplicar(
        tenant_id,
        sobrescribir_prompt=datos.sobrescribir_prompt,
        actor_email=usuario.email,
        actor_id=usuario.id,
    )


@router.post("/omitir", status_code=status.HTTP_204_NO_CONTENT)
async def omitir(tenant_id: UUID = Depends(tenant_actual)) -> None:
    await onboarding.omitir(tenant_id)
