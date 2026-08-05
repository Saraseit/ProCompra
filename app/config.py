"""
app/config.py
-------------
Lee las variables del archivo .env y las expone
como un objeto settings disponible en todo el proyecto.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SUPABASE_URL: str
    SUPABASE_KEY: str
    SUPABASE_ANON_KEY: str        # <- agregar esta línea
    RESEND_API_KEY: str = ""
    JWT_SECRET: str = ""
    FRONTEND_URL: str = "http://localhost:5174"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Instancia global — se importa en cualquier archivo así:
# from app.config import settings
settings = Settings()