"""
OperativAI — API del portal.

Arranque local:
    uvicorn main:app --reload --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import agente
import auth
import canales
import conversaciones
import eventos
import herramientas
import vendedores
from config import settings
from session import close_pool, init_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    await init_pool()
    yield
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

# Módulo de gestión de vendedores. Son tres routers y no uno porque las
# rutas cuelgan de tres raíces distintas (/vendedores, /tenants, /clientes).
app.include_router(vendedores.router_vendedores, prefix="/api")
app.include_router(vendedores.router_tenants, prefix="/api")
app.include_router(vendedores.router_clientes, prefix="/api")

# Lo llama n8n con X-Internal-Token, no el frontend.
app.include_router(eventos.router, prefix="/api")


@app.get("/api/salud")
async def salud():
    return {"ok": True}
