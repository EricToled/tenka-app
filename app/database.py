"""
database.py — Configuración de conexión a PostgreSQL con SQLAlchemy 2.x.

Variables de entorno requeridas (o valores por defecto para desarrollo):
    DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "tenka_db")

# Railway (y otros PaaS) proveen DATABASE_URL completa; si existe, usarla.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
)

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """Generador de sesión para uso con FastAPI Depends."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
