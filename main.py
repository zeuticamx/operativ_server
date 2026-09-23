"""
OperativAI — API del portal.

Arranque local:
    uvicorn main:socket_app --reload --port 8000

`socket_app` y no `app`: el WebSocket de alertas (realtime.py) va montado
encima de la app de FastAPI, y uvicorn tiene que arrancar esa capa de afuera
para que /socket.io/* llegue a python-socketio en vez de a FastAPI.
"""

from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import (
    agente,
    alertas,
    auth,
    canales,
    clientes,
    conversaciones,
    eventos,
    gerencia,
    gerencia_operacion,
    gerencia_planes,
    herramientas,
    pagos,
    pagos_stripe,
    pipeline_config,
    reportes,
    tareas,
    vendedores,
    visitas,
)

from config import settings
from jobs.alertas_background import iniciar_scheduler
from session import close_pool, init_pool
from realtime import sio


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    await init_pool()
    scheduler = iniciar_scheduler()
    yield
    scheduler.shutdown()
    await close_pool()

app = FastAPI(
    title="OperativAI API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(canales.router, prefix="/api")
app.include_router(agente.router, prefix="/api")
app.include_router(conversaciones.router, prefix="/api")
app.include_router(herramientas.router, prefix="/api")

# Módulo de gestión de vendedores (embudo de chat). Son cuatro routers y no
# uno porque las rutas cuelgan de cuatro raíces distintas.
app.include_router(vendedores.router_vendedores, prefix="/api")
app.include_router(vendedores.router_tenants, prefix="/api")
app.include_router(vendedores.router_pipeline, prefix="/api")
app.include_router(vendedores.router_clientes, prefix="/api")
app.include_router(pipeline_config.router, prefix="/api")
app.include_router(alertas.router, prefix="/api")

# Cobros. La pasarela activa la decide PAYMENT_PROVIDER: hoy Stripe, con
# Mercado Pago inhabilitado pero sin borrar. Cada una tiene su webhook, y
# son los únicos endpoints del módulo sin JWT: se autentican por firma HMAC.
#   Stripe       -> /api/pagos/stripe/webhook
#   Mercado Pago -> /api/pagos/webhook  (503 mientras no sea el proveedor)
app.include_router(pagos.router, prefix="/api")
app.include_router(pagos_stripe.router, prefix="/api")

# CRM de campo: cartera, visitas con geocerca, seguimientos y reportes.
# Convive con el embudo de chat de arriba; comparten la tabla vendedores.
app.include_router(clientes.router, prefix="/api")
app.include_router(visitas.router, prefix="/api")
app.include_router(tareas.router, prefix="/api")
app.include_router(reportes.router, prefix="/api")

# Panel de plataforma: nivel gerencia (tabla gerencia_users), no el rol
# owner de un tenant. Ve todos los negocios, así que no filtra por tenant.
app.include_router(gerencia.router, prefix="/api")
app.include_router(gerencia_operacion.router, prefix="/api")
app.include_router(gerencia_planes.router, prefix="/api")

# Lo llama n8n con X-Internal-Token, no el frontend.
app.include_router(eventos.router, prefix="/api")


@app.get("/api/salud")
async def salud():
    return {"ok": True}

# El WebSocket de alertas vive en su propia capa ASGI, montada encima de
# FastAPI: /socket.io/* lo atiende python-socketio, todo lo demás sigue
# yendo a `app` sin cambios. Ver realtime.py para la autenticación y los
# eventos.
socket_app = socketio.ASGIApp(sio, other_asgi_app=app, socketio_path="socket.io")