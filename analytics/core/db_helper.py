"""
Helper de base de datos para los scripts de analisis.

Usa la misma config que el harvester (.env en la raiz de produccion/).
Expone dos funciones principales:
  - get_df(sql, params)  → pandas DataFrame
  - get_scalar(sql, params) → valor unico
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import decimal

import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import os

# Cargar .env desde produccion/
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

logger = logging.getLogger("kawii.analytics.db")

_DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "dbname":   os.getenv("DB_NAME", "database_kawii"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "root"),
}


@contextmanager
def _get_conn():
    conn = psycopg2.connect(**_DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


def get_df(sql: str, params: tuple | dict | None = None) -> pd.DataFrame:
    """
    Ejecuta una consulta SELECT y devuelve un DataFrame con los resultados.
    Usa cursor nativo para evitar el DeprecationWarning de pandas con psycopg2.
    """
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    # Convertir Decimal (PostgreSQL numeric) a float para operar con numpy
    for col in df.select_dtypes(include="object").columns:
        if not df[col].empty and isinstance(df[col].dropna().iloc[0]
                                             if not df[col].dropna().empty else None,
                                             decimal.Decimal):
            df[col] = df[col].astype(float)
    # Convertir todas las columnas con Decimal directamente
    for col in df.columns:
        try:
            sample = df[col].dropna()
            if not sample.empty and isinstance(sample.iloc[0], decimal.Decimal):
                df[col] = pd.to_numeric(df[col], errors="coerce")
        except (TypeError, AttributeError):
            pass
    return df


def get_scalar(sql: str, params: tuple | dict | None = None) -> Any:
    """Ejecuta una consulta que devuelve un solo valor."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None
