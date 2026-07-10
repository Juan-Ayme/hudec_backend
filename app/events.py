"""
Auditoría por empresa: helper `log_event` que escribe en la tabla `event_log`.

Espíritu igual a `harvester.db.sync_finish`: persiste el evento Y emite un log
estructurado con el mismo contexto.

Garantías de diseño (críticas):
  - **Un fallo de auditoría NUNCA rompe el request del usuario.** El INSERT va
    dentro de un SAVEPOINT (`begin_nested`): si falla (p. ej. la tabla aún no
    existe en un deploy viejo), se revierte SOLO el savepoint —la transacción
    del caller sigue viva— y se loguea un `warning`. La excepción no se propaga.
  - **RLS consistente.** `event_log` tiene la misma policy tenant que el resto
    (`WITH CHECK (company_id = current_company_id())`). Para que el INSERT pase
    incluso en paths sin empresa activa (ej. `/auth/login`, que es público y no
    pasa por `get_current_company`), se hace `SET LOCAL app.current_company`
    con el `company_id` del evento antes de insertar.
  - **request_id** se toma del contextvar si no se pasa explícito.
  - Si `company_id` es `None` (evento global sin empresa, ej. logout sin header)
    se emite solo el log estructurado y se omite la fila (la columna es NOT NULL).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("kawii.events")


async def log_event(
    session: AsyncSession,
    *,
    company_id: int | None,
    event_type: str,
    actor_user_id: int | None = None,
    payload: dict[str, Any] | None = None,
    request_id: str | None = None,
    commit: bool = True,
) -> None:
    """Inserta una fila en `event_log` y emite un log estructurado.

    Args:
        session: sesión async del request (la misma del endpoint).
        company_id: empresa dueña del evento. `None` => solo log, sin fila.
        event_type: identificador del evento (ver catálogo, ej. "auth.login.success").
        actor_user_id: usuario que ejecuta la acción (si aplica).
        payload: dict JSON-serializable con detalle (NUNCA secretos).
        request_id: se toma del contextvar si no se pasa.
        commit: si True, commitea la fila (necesario en paths que no commitean,
            ej. login fallido). Poné False si el caller controla el commit.
    """
    payload = payload or {}
    if request_id is None:
        try:
            from app.middleware.logging import get_request_id

            request_id = get_request_id()
        except Exception:
            request_id = None

    # Evento global sin empresa: no hay fila (company_id es NOT NULL), pero
    # dejamos rastro estructurado.
    if company_id is None:
        logger.info(
            "event %s (sin empresa)",
            event_type,
            extra={
                "event_type": event_type,
                "evt_actor_user_id": actor_user_id,
            },
        )
        return

    try:
        # SAVEPOINT: aísla el fallo del INSERT de la transacción del caller.
        async with session.begin_nested():
            # RLS: garantiza current_company_id() para el WITH CHECK aun en
            # paths públicos. company_id ya es int validado => cast seguro.
            await session.execute(
                text(f"SET LOCAL app.current_company = '{int(company_id)}'")
            )
            await session.execute(
                text(
                    """
                    INSERT INTO event_log
                        (company_id, event_type, actor_user_id, request_id, payload)
                    VALUES
                        (:c, :t, :a, :r, CAST(:p AS jsonb))
                    """
                ),
                {
                    "c": int(company_id),
                    "t": event_type,
                    "a": actor_user_id,
                    "r": request_id,
                    "p": json.dumps(payload, ensure_ascii=False, default=str),
                },
            )
        if commit:
            await session.commit()
        logger.info(
            "event %s",
            event_type,
            extra={
                "event_type": event_type,
                "evt_company_id": int(company_id),
                "evt_actor_user_id": actor_user_id,
            },
        )
    except Exception as exc:
        # Auditoría best-effort: nunca romper el request por esto.
        logger.warning(
            "No se pudo registrar event_log %s (company=%s): %s",
            event_type,
            company_id,
            exc,
        )
