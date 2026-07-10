"""
Autenticación de la API: bcrypt + JWT en cookies httpOnly + multi-tenant.

Modelo:
- `app_users` es GLOBAL: un usuario único (por username) en todo el sistema.
- `user_companies` es el pivote N-a-N: a qué empresas pertenece cada usuario
  y con qué rol en cada una. Un mismo usuario puede ser 'admin' en una
  empresa y 'viewer' en otra.

Tres niveles de protección, ordenados de más permisivo a más estricto:
- `get_current_user(request, db)` — exige cookie de sesión válida. Cualquier
  usuario activo pasa. Devuelve el usuario (sin role — el role es por empresa).
- `get_current_company(request, user, db)` — exige header `X-Company-Id`
  que apunte a una empresa donde el usuario tenga membresía. Devuelve
  `(company_id, role)`.
- `require_operador_or_admin` / `require_admin` — encima de
  `get_current_company`, validan que el role en ESA empresa esté en la lista.

Bootstrap: si la tabla `app_users` está vacía al arrancar, se siembra UN
admin con membresía 'admin' en la empresa id=1 (Hudec), usando
`ADMIN_USERNAME` / `ADMIN_PASSWORD` del .env.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

import bcrypt
import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

logger = logging.getLogger("kawii.auth")


# ──────────────────────────────────────────────────────────────────────────────
# Configuración (vía .env)
# ──────────────────────────────────────────────────────────────────────────────

JWT_SECRET: str = os.environ.get("JWT_SECRET") or secrets.token_urlsafe(64)
if not os.environ.get("JWT_SECRET"):
    logger.warning(
        "JWT_SECRET no está definido en .env — usando uno aleatorio. "
        "Definilo en producción para que los tokens sobrevivan a reinicios."
    )

JWT_ALGORITHM: str = "HS256"
JWT_EXPIRES_DAYS: int = int(os.environ.get("JWT_EXPIRES_DAYS", "7"))
COOKIE_NAME: str = "kawii_session"
COOKIE_SAMESITE: Literal["lax", "strict", "none"] = os.environ.get(
    "COOKIE_SAMESITE", "lax"
).lower()  # type: ignore[assignment]
COOKIE_SECURE: bool = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

UserRole = Literal["admin", "operador", "viewer"]

# Header donde el frontend manda la empresa activa.
COMPANY_HEADER: str = "X-Company-Id"


# ──────────────────────────────────────────────────────────────────────────────
# Hashing
# ──────────────────────────────────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    """Hashea con bcrypt (cost 12 por defecto). Devuelve el hash codificado."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Comparación timing-safe entre password y hash bcrypt."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ──────────────────────────────────────────────────────────────────────────────
# JWT
# ──────────────────────────────────────────────────────────────────────────────


def create_access_token(user_id: int, username: str) -> str:
    """Crea un JWT con el ID + username del usuario.

    El JWT NO lleva company_id ni role: ambos se resuelven por request
    a partir del header X-Company-Id + la tabla user_companies. Esto
    permite cambiar de empresa sin re-emitir el token.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=JWT_EXPIRES_DAYS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """Decodifica y valida un JWT. Devuelve None si está expirado o inválido."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Cookies
# ──────────────────────────────────────────────────────────────────────────────


def set_session_cookie(response: Response, token: str) -> None:
    """Setea la cookie httpOnly de sesión."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=JWT_EXPIRES_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Borra la cookie de sesión (logout)."""
    response.delete_cookie(key=COOKIE_NAME, path="/")


# ──────────────────────────────────────────────────────────────────────────────
# Dependencies
# ──────────────────────────────────────────────────────────────────────────────


class CurrentUser(BaseModel):
    """Representa al usuario autenticado en una request.

    No lleva role: el role es POR EMPRESA y se resuelve en
    `get_current_company`.
    """

    id: int
    username: str
    is_active: bool


class CurrentCompany(BaseModel):
    """Representa la empresa activa + el role del usuario en ella."""

    company_id: int
    role: UserRole


async def get_current_user(
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Valida la cookie de sesión y devuelve el usuario actual.

    Levanta 401 si no hay cookie, el JWT es inválido/expirado, el usuario
    fue eliminado o desactivado."""
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No hay sesión activa",
            headers={"WWW-Authenticate": "Cookie"},
        )
    payload = decode_access_token(session)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión inválida o expirada",
            headers={"WWW-Authenticate": "Cookie"},
        )
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Token corrupto")

    row = (
        await db.execute(
            text(
                "SELECT id, username, is_active "
                "FROM app_users WHERE id = :id"
            ),
            {"id": user_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=401, detail="Usuario no existe")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Usuario desactivado")
    return CurrentUser(
        id=row["id"],
        username=row["username"],
        is_active=row["is_active"],
    )


async def get_current_company(
    user: CurrentUser = Depends(get_current_user),
    x_company_id: str | None = Header(default=None, alias=COMPANY_HEADER),
    db: AsyncSession = Depends(get_db),
) -> CurrentCompany:
    """Resuelve la empresa activa y el rol del usuario en ella.

    El frontend manda el ID de la empresa activa en el header
    `X-Company-Id`. Este resolver:
      1. Lo parsea a int.
      2. Verifica que el usuario tenga membresía en esa empresa.
      3. Devuelve (company_id, role).

    Levanta:
      - 400 si el header falta o no es un entero válido.
      - 403 si el usuario no es miembro de esa empresa.
    """
    if not x_company_id:
        raise HTTPException(
            status_code=400,
            detail=f"Falta el header {COMPANY_HEADER}",
        )
    try:
        company_id = int(x_company_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{COMPANY_HEADER} debe ser un entero",
        )

    row = (
        await db.execute(
            text(
                "SELECT role "
                "FROM user_companies "
                "WHERE user_id = :u AND company_id = :c"
            ),
            {"u": user.id, "c": company_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(
            status_code=403,
            detail="El usuario no pertenece a esta empresa",
        )

    # ★ Multi-tenant: activa el filtro RLS de Postgres para esta transacción.
    # Todas las queries subsecuentes ven SOLO las filas de esta empresa
    # gracias a `CREATE POLICY tenant_isolation ... USING (company_id = current_company_id())`.
    # Cast a text es seguro porque company_id ya fue validado como int.
    await db.execute(text(f"SET LOCAL app.current_company = '{int(company_id)}'"))
    return CurrentCompany(company_id=company_id, role=row["role"])


def require_role(*allowed: UserRole):
    """Factory de dependency: exige que el rol del usuario EN LA EMPRESA ACTIVA
    esté en `allowed`. Depende de `get_current_company` (no de get_current_user)
    porque el rol es por empresa.
    """

    async def _checker(
        company: CurrentCompany = Depends(get_current_company),
    ) -> CurrentCompany:
        if company.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Acción permitida solo para roles: {list(allowed)}",
            )
        return company

    return _checker


require_admin = require_role("admin")
require_operador_or_admin = require_role("admin", "operador")


# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap: siembra un admin inicial si la tabla está vacía
# ──────────────────────────────────────────────────────────────────────────────


async def bootstrap_first_admin(db: AsyncSession) -> None:
    """Si `app_users` está vacía, crea el primer admin + su membresía 'admin'
    en la empresa id=1 (Hudec).

    Vars de entorno:
    - ADMIN_USERNAME (default: 'admin')
    - ADMIN_PASSWORD (default: 'admin' — CAMBIAR EN PRODUCCIÓN)
    """
    n = await db.scalar(text("SELECT COUNT(*) FROM app_users"))
    if n and n > 0:
        return
    username = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
    password = os.environ.get("ADMIN_PASSWORD", "admin")
    if password == "admin":
        logger.warning(
            "Sembrando admin inicial con password 'admin'. "
            "CAMBIAR INMEDIATAMENTE despues del primer login."
        )

    # 1) Crear usuario global
    user_id = await db.scalar(
        text(
            "INSERT INTO app_users (username, password_hash) "
            "VALUES (:u, :p) RETURNING id"
        ),
        {"u": username, "p": hash_password(password)},
    )

    # 2) Darle membresía 'admin' en la empresa id=1 (Hudec).
    #    Si en el futuro hay más empresas, esto se gestiona desde la UI.
    await db.execute(
        text(
            "INSERT INTO user_companies (user_id, company_id, role) "
            "VALUES (:u, 1, 'admin')"
        ),
        {"u": user_id},
    )

    await db.commit()
    logger.info("Admin inicial creado: username=%s, company_id=1, role=admin", username)
