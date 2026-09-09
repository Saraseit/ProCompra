"""
Prepara el entorno mínimo para importar la app en las pruebas.

Estas pruebas solo ejercitan modelos y constantes, nunca la base. Se
sustituye app.database por un doble antes de importar los routers, para que
no haga falta una clave de Supabase válida ni exista forma de tocar datos
reales por accidente. app.config sí se importa, así que las variables que
solo valida como texto se rellenan con marcadores.
"""

import os
import sys
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

os.environ.setdefault("SUPABASE_URL", "https://ejemplo.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "clave-de-prueba")
os.environ.setdefault("SUPABASE_ANON_KEY", "clave-anon-de-prueba")


def _sin_base(*_args, **_kwargs):
    raise AssertionError("Las pruebas no deben consultar la base de datos")


_doble = types.ModuleType("app.database")
_doble.supabase = property(_sin_base)
_doble.una_fila = _sin_base
sys.modules.setdefault("app.database", _doble)
