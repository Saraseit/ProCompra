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
)

# ── CORS ──────────────────────────────────────────────
# Permite que el frontend React se comunique con el backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL, "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Rutas ─────────────────────────────────────────────
from app.api import proveedores, requerimientos, ordenes
app.include_router(proveedores.router, prefix="/api")
app.include_router(requerimientos.router, prefix="/api")
app.include_router(ordenes.router, prefix="/api")

# ── Health check ──────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "proyecto": "ProCompra"}