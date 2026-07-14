"""
Endpoints de autenticación y gestión de usuarios (multi-tenant).

- POST /auth/login          (público) body {username, password}
                            → setea cookie + devuelve user + companies del user
- POST /auth/logout         (autenticado) → limpia cookie
- GET  /auth/me             (autenticado) → user + companies del user

Endpoints de gestión de usuarios DENTRO de una empresa.
Requieren header X-Company-Id y rol 'admin' en esa empresa:
- GET    /auth/users          → lista miembros de la empresa activa
- POST   /auth/users          → crea usuario + le da membresía en la empresa
- PATCH  /auth/users/{id}     → actualiza role en la empresa, password global
- DELETE /auth/users/{id}     → quita la MEMBRESÍA del user en la empresa
                                (el user sigue existiendo para otras empresas)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    CurrentCompany,
    CurrentUser,
    UserRole,
    create_access_token,
    get_current_user,
    hash_password,
    require_admin,
    set_session_cookie,
    clear_session_cookie,
    verify_password,
)
from app.database import get_db
from app.events import log_event

logger = logging.getLogger("kawii.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


# ──────────────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────────────


class LoginBody(BaseModel):
    username: str
    password: str


class CompanyMembership(BaseModel):
    """Una empresa donde el usuario tiene membresía + el rol en esa empresa."""

    id: int
    name: str
    slug: str
    role: UserRole
    # Sucursales a las que el user está limitado en esta empresa.
    # [] = sin restricción (ve todas). El frontend usa esto para acotar el
    # selector de sucursal.
    allowed_office_ids: list[int] = []


class UserOut(BaseModel):
    """Usuario visto desde dentro de una empresa: el role es el de esa empresa."""

    id: int
    username: str
    role: UserRole
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None
    # Sucursales asignadas en esta empresa ([] = todas).
    office_ids: list[int] = []


class CreateUserBody(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=6)
    role: UserRole  # rol en la empresa activa
    # Sucursales permitidas en la empresa activa. None/[] = acceso a todas.
    office_ids: list[int] | None = None


class UpdateUserBody(BaseModel):
    role: UserRole | None = None  # cambia el rol en la empresa activa
    is_active: bool | None = None  # bandera GLOBAL del usuario
    password: str | None = Field(default=None, min_length=6)
    # None = no tocar las sucursales; [] = limpiar (pasa a ver todas);
    # [ids…] = restringir a ese conjunto.
    office_ids: list[int] | None = None


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


async def _list_user_companies(
    db: AsyncSession, user_id: int
) -> list[CompanyMembership]:
    """Devuelve las empresas activas en las que el user tiene membresía."""
    rows = (
        await db.execute(
            text(
                """
                SELECT c.id, c.name, c.slug, uc.role,
                       COALESCE(ARRAY(
                           SELECT uo.bsale_office_id FROM user_offices uo
                           WHERE uo.user_id = uc.user_id AND uo.company_id = c.id
                           ORDER BY uo.bsale_office_id
                       ), '{}') AS allowed_office_ids
                FROM user_companies uc
                JOIN companies c ON c.id = uc.company_id
                WHERE uc.user_id = :uid AND c.is_active
                ORDER BY c.name
                """
            ),
            {"uid": user_id},
        )
    ).mappings().all()
    return [
        CompanyMembership(
            id=r["id"], name=r["name"], slug=r["slug"], role=r["role"],
            allowed_office_ids=list(r["allowed_office_ids"] or []),
        )
        for r in rows
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Login / logout / me
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/login")
async def login(
    body: LoginBody,
    response: Response,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Valida credenciales, setea cookie httpOnly y devuelve user + companies."""
    client_ip = request.client.host if request.client else None
    row = (
        await db.execute(
            text(
                "SELECT id, username, password_hash, is_active "
                "FROM app_users WHERE username = :u"
            ),
            {"u": body.username},
        )
    ).mappings().first()
    if not row or not verify_password(body.password, row["password_hash"]):
        logger.warning(
            "Login fallido (credenciales inválidas): username=%s ip=%s",
            body.username, client_ip,
        )
        # Login es pre-empresa: no hay company activa → solo log estructurado.
        await log_event(
            db, company_id=None, event_type="auth.login.failure",
            payload={"username": body.username, "reason": "bad_credentials", "ip": client_ip},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos",
        )
    if not row["is_active"]:
        logger.warning(
            "Login rechazado (usuario desactivado): username=%s user_id=%s ip=%s",
            row["username"], row["id"], client_ip,
        )
        await log_event(
            db, company_id=None, event_type="auth.login.failure",
            actor_user_id=row["id"],
            payload={"username": row["username"], "reason": "disabled", "ip": client_ip},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Usuario desactivado"
        )

    token = create_access_token(row["id"], row["username"])
    set_session_cookie(response, token)

    companies = await _list_user_companies(db, row["id"])

    try:
        await db.execute(
            text("UPDATE app_users SET last_login_at = NOW() WHERE id = :id"),
            {"id": row["id"]},
        )
        await db.commit()
    except Exception as exc:
        # No es crítico: el login sigue siendo válido aunque no se actualice
        # el timestamp. Antes se tragaba en silencio; ahora deja rastro.
        logger.warning(
            "No se pudo actualizar last_login_at para user_id=%s: %s",
            row["id"], exc,
        )

    logger.info(
        "Login exitoso: username=%s user_id=%s empresas=%d ip=%s",
        row["username"], row["id"], len(companies), client_ip,
    )
    # company_id=None: en login aún no hay empresa activa (se elige después vía
    # X-Company-Id). El evento queda como log estructurado con el actor.
    await log_event(
        db, company_id=None, event_type="auth.login.success",
        actor_user_id=row["id"],
        payload={
            "username": row["username"],
            "ip": client_ip,
            "companies": [c.id for c in companies],
        },
    )

    return {
        "ok": True,
        "user": {
            "id": row["id"],
            "username": row["username"],
            "is_active": row["is_active"],
        },
        "companies": [c.model_dump() for c in companies],
    }


@router.post("/logout")
async def logout(
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Limpia la cookie de sesión."""
    clear_session_cookie(response)
    logger.info("Logout: username=%s user_id=%s", user.username, user.id)
    # company_id=None: logout no está scopeado a empresa → solo log estructurado.
    await log_event(
        db, company_id=None, event_type="auth.logout",
        actor_user_id=user.id, payload={"username": user.username},
    )
    return {"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/me")
async def me(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Devuelve el usuario actual + sus empresas. NO requiere X-Company-Id:
    se usa justamente para que el frontend descubra qué empresas mostrar
    en el selector."""
    companies = await _list_user_companies(db, user.id)
    return {
        "id": user.id,
        "username": user.username,
        "is_active": user.is_active,
        "companies": [c.model_dump() for c in companies],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Gestión de usuarios DENTRO de una empresa (admin only)
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/users")
async def list_users(
    company: CurrentCompany = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lista los miembros de la empresa activa con sus roles en ella."""
    rows = (
        await db.execute(
            text(
                """
                SELECT u.id, u.username, uc.role, u.is_active,
                       u.created_at, u.last_login_at,
                       COALESCE(ARRAY(
                           SELECT uo.bsale_office_id FROM user_offices uo
                           WHERE uo.user_id = u.id AND uo.company_id = uc.company_id
                           ORDER BY uo.bsale_office_id
                       ), '{}') AS office_ids
                FROM user_companies uc
                JOIN app_users u ON u.id = uc.user_id
                WHERE uc.company_id = :cid
                ORDER BY u.username
                """
            ),
            {"cid": company.company_id},
        )
    ).mappings().all()
    return {
        "total": len(rows),
        "users": [
            UserOut(
                id=r["id"],
                username=r["username"],
                role=r["role"],
                is_active=r["is_active"],
                created_at=r["created_at"],
                last_login_at=r["last_login_at"],
                office_ids=list(r["office_ids"] or []),
            ).model_dump(mode="json")
            for r in rows
        ],
    }


@router.get("/offices")
async def list_company_offices(
    company: CurrentCompany = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Sucursales de la empresa activa, para poblar el selector al crear/editar
    usuarios (asignar acceso por sucursal). Solo admin."""
    rows = (
        await db.execute(
            text(
                "SELECT bsale_office_id AS id, name, is_active "
                "FROM offices WHERE company_id = :c ORDER BY name"
            ),
            {"c": company.company_id},
        )
    ).mappings().all()
    return {"total": len(rows), "offices": [dict(r) for r in rows]}


@router.post("/users", status_code=201)
async def create_user(
    body: CreateUserBody,
    actor: CurrentUser = Depends(get_current_user),
    company: CurrentCompany = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Crea un usuario global y le da membresía en la empresa activa.
    Falla si el username ya existe (no une al usuario existente — para eso
    habrá un endpoint separado `add member` en el futuro)."""
    try:
        user_row = (
            await db.execute(
                text(
                    "INSERT INTO app_users (username, password_hash) "
                    "VALUES (:u, :p) RETURNING id, created_at"
                ),
                {
                    "u": body.username.strip(),
                    "p": hash_password(body.password),
                },
            )
        ).mappings().one()

        await db.execute(
            text(
                "INSERT INTO user_companies (user_id, company_id, role) "
                "VALUES (:uid, :cid, :r)"
            ),
            {"uid": user_row["id"], "cid": company.company_id, "r": body.role},
        )
        # Sucursales permitidas (si se especificaron). Sin filas = ve todas.
        for oid in body.office_ids or []:
            await db.execute(
                text(
                    "INSERT INTO user_offices (user_id, company_id, bsale_office_id) "
                    "VALUES (:uid, :cid, :oid)"
                ),
                {"uid": user_row["id"], "cid": company.company_id, "oid": oid},
            )
        # Auditoría en la MISMA transacción (commit=False): la persiste el commit
        # de abajo. Sin secretos: solo actor, target y rol.
        await log_event(
            db, company_id=company.company_id, event_type="auth.user.created",
            actor_user_id=actor.id,
            payload={
                "actor": actor.username,
                "target_user_id": user_row["id"],
                "target_username": body.username.strip(),
                "role": body.role,
            },
            commit=False,
        )
        await db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No se pudo crear el usuario: {exc}",
        )
    logger.info(
        "Usuario creado: target_id=%s target=%s role=%s por actor=%s en company=%s",
        user_row["id"], body.username.strip(), body.role, actor.username, company.company_id,
    )
    return {
        "ok": True,
        "id": user_row["id"],
        "username": body.username,
        "role": body.role,
        "company_id": company.company_id,
        "created_at": user_row["created_at"].isoformat(),
    }


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    body: UpdateUserBody,
    user: CurrentUser = Depends(get_current_user),
    company: CurrentCompany = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Actualiza el rol del usuario EN LA EMPRESA ACTIVA, su estado activo
    GLOBAL o su password GLOBAL. Solo los campos pasados se cambian."""
    if not (
        body.role
        or body.is_active is not None
        or body.password
        or body.office_ids is not None
    ):
        raise HTTPException(status_code=400, detail="Nada para actualizar")

    # Prevenir auto-degradación / auto-desactivación.
    if user_id == user.id:
        if body.role and body.role != "admin":
            raise HTTPException(
                status_code=400,
                detail="No podés quitarte tu propio rol de admin en esta empresa",
            )
        if body.is_active is False:
            raise HTTPException(
                status_code=400, detail="No podés desactivarte a vos mismo"
            )

    # role → user_companies (por empresa)
    if body.role is not None:
        result = await db.execute(
            text(
                "UPDATE user_companies SET role = :r "
                "WHERE user_id = :uid AND company_id = :cid RETURNING user_id"
            ),
            {"r": body.role, "uid": user_id, "cid": company.company_id},
        )
        if not result.first():
            raise HTTPException(
                status_code=404,
                detail=f"El usuario {user_id} no es miembro de esta empresa",
            )

    # is_active y password → app_users (global)
    global_fields, global_params = [], {"id": user_id}
    if body.is_active is not None:
        global_fields.append("is_active = :a")
        global_params["a"] = body.is_active
    if body.password is not None:
        global_fields.append("password_hash = :p")
        global_params["p"] = hash_password(body.password)
    if global_fields:
        result = await db.execute(
            text(
                f"UPDATE app_users SET {', '.join(global_fields)} "
                f"WHERE id = :id RETURNING id"
            ),
            global_params,
        )
        if not result.first():
            raise HTTPException(
                status_code=404, detail=f"Usuario {user_id} no existe"
            )

    # Sucursales permitidas: None = no tocar; [] o [ids] = reemplazar el set.
    # Se valida contra la empresa activa (FK compuesta a offices).
    if body.office_ids is not None:
        await db.execute(
            text(
                "DELETE FROM user_offices "
                "WHERE user_id = :uid AND company_id = :cid"
            ),
            {"uid": user_id, "cid": company.company_id},
        )
        for oid in body.office_ids:
            await db.execute(
                text(
                    "INSERT INTO user_offices (user_id, company_id, bsale_office_id) "
                    "VALUES (:uid, :cid, :oid)"
                ),
                {"uid": user_id, "cid": company.company_id, "oid": oid},
            )

    # Auditoría: qué campos cambiaron (NUNCA el password, solo la bandera de que
    # se cambió). Se persiste con el commit de abajo (commit=False).
    changed = []
    if body.role is not None:
        changed.append("role")
    if body.is_active is not None:
        changed.append("is_active")
    if body.password is not None:
        changed.append("password")
    if body.office_ids is not None:
        changed.append("office_ids")
    await log_event(
        db, company_id=company.company_id, event_type="auth.user.updated",
        actor_user_id=user.id,
        payload={
            "actor": user.username,
            "target_user_id": user_id,
            "changed": changed,
            "role": body.role,
            "is_active": body.is_active,
        },
        commit=False,
    )
    await db.commit()
    logger.info(
        "Usuario actualizado: target_id=%s cambios=%s por actor=%s en company=%s",
        user_id, changed, user.username, company.company_id,
    )
    return {"ok": True, "id": user_id, "company_id": company.company_id}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    user: CurrentUser = Depends(get_current_user),
    company: CurrentCompany = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Quita la membresía del usuario en la empresa activa.
    El usuario sigue existiendo y mantiene sus membresías en otras empresas.

    No permite que un admin se quite a sí mismo. Si es el último admin
    activo de la empresa, bloquea para no dejarla sin administración."""
    if user_id == user.id:
        raise HTTPException(
            status_code=400,
            detail="No podés quitarte a vos mismo de la empresa",
        )

    # Si es admin y queda como último, bloquear.
    target_role = await db.scalar(
        text(
            "SELECT role FROM user_companies "
            "WHERE user_id = :uid AND company_id = :cid"
        ),
        {"uid": user_id, "cid": company.company_id},
    )
    if target_role == "admin":
        remaining = await db.scalar(
            text(
                """
                SELECT COUNT(*) FROM user_companies uc
                JOIN app_users u ON u.id = uc.user_id
                WHERE uc.company_id = :cid AND uc.role = 'admin'
                  AND u.is_active AND uc.user_id != :uid
                """
            ),
            {"cid": company.company_id, "uid": user_id},
        )
        if not remaining:
            raise HTTPException(
                status_code=400,
                detail="Es el último admin activo de la empresa, no se puede quitar",
            )

    result = await db.execute(
        text(
            "DELETE FROM user_companies "
            "WHERE user_id = :uid AND company_id = :cid RETURNING user_id"
        ),
        {"uid": user_id, "cid": company.company_id},
    )
    if not result.first():
        raise HTTPException(
            status_code=404,
            detail=f"El usuario {user_id} no es miembro de esta empresa",
        )
    await log_event(
        db, company_id=company.company_id, event_type="auth.user.deleted",
        actor_user_id=user.id,
        payload={
            "actor": user.username,
            "target_user_id": user_id,
            "target_role": target_role,
        },
        commit=False,
    )
    await db.commit()
    logger.info(
        "Membresía removida: target_id=%s por actor=%s en company=%s",
        user_id, user.username, company.company_id,
    )
    return {
        "ok": True,
        "removed_user_id": user_id,
        "company_id": company.company_id,
    }
