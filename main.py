"""
main.py
-------
Punto de entrada del servidor FastAPI.
Ejecutar con: uvicorn main:app --reload
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings

app = FastAPI(
    title="ProCompra API",
    description="Sistema de órdenes de compra — Minimal 4.0",
    version="1.0.0",
    redirect_slashes=False,
)

# ── CORS ──────────────────────────────────────────────
# Permite que el frontend React se comunique con el backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "https://pro-compra-front.vercel.app",
        settings.FRONTEND_URL,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Rutas ─────────────────────────────────────────────
from app.api import proveedores, requerimientos, ordenes, usuarios
app.include_router(proveedores.router, prefix="/api")
app.include_router(requerimientos.router, prefix="/api")
app.include_router(ordenes.router, prefix="/api")
app.include_router(usuarios.router, prefix="/api")

# ── Health check ──────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "proyecto": "ProCompra"}