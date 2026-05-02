"""
v2_router.py — Endpoints FastAPI /v2 para OpenClaw (Fase 3.2).

Contrato idéntico a los endpoints Flask de api_server.py:
  GET  /v2/accounts
  POST /v2/post
  POST /v2/schedule
  GET  /v2/schedule
  DELETE /v2/schedule/{job_id}

Reutiliza directamente las funciones internas de api_server.py
(_enqueue_job, _resolve_accounts, _safe_image_path) para no
duplicar lógica de negocio.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

import job_store
from config import load_accounts
from v2_deps import check_rate_limit
from v2_models import (
    AccountsListResponse,
    PostAccepted,
    PostRequest,
    ScheduleCreated,
    ScheduleListResponse,
    ScheduleRequest,
)

logger = logging.getLogger("v2_router")

router = APIRouter(prefix="/v2", tags=["OpenClaw v2"])


# ---------------------------------------------------------------------------
# GET /v2/accounts
# ---------------------------------------------------------------------------

@router.get(
    "/accounts",
    response_model=AccountsListResponse,
    summary="Listar cuentas activas y sus grupos",
    dependencies=[Depends(check_rate_limit)],
)
def list_accounts():
    try:
        accounts = load_accounts()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    db_info = {r["name"]: r for r in job_store.get_accounts_info()}

    return AccountsListResponse(
        accounts=[
            {
                "name": a.name,
                "email": a.email,
                "groups": a.groups,
                "last_login_at": db_info.get(a.name, {}).get("last_login_at"),
                "last_published_at": db_info.get(a.name, {}).get("last_published_at"),
            }
            for a in accounts
        ]
    )


# ---------------------------------------------------------------------------
# POST /v2/post
# ---------------------------------------------------------------------------

@router.post(
    "/post",
    response_model=PostAccepted,
    status_code=202,
    summary="Publicación inmediata en grupos de Facebook",
    dependencies=[Depends(check_rate_limit)],
)
def handle_post(body: PostRequest):
    # Importar aquí para evitar importación circular en el arranque
    from api_server import _enqueue_job, _filter_rate_limited_accounts, _safe_image_path

    # Validar image_path si se proporcionó
    image_path = None
    if body.image_path:
        image_path = _safe_image_path(body.image_path)
        if image_path is None:
            raise HTTPException(
                status_code=400,
                detail="image_path inválido o fuera del directorio permitido",
            )

    # Resolver cuentas
    try:
        accounts = load_accounts()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if body.accounts:
        accounts = [a for a in accounts if a.name in body.accounts]
        if not accounts:
            raise HTTPException(status_code=400, detail="Ninguna cuenta del filtro encontrada")

    # Filtrar cuentas con rate limit excedido
    runnable, skipped = _filter_rate_limited_accounts(accounts)
    for acc, reason in skipped:
        logger.warning("POST /v2/post: cuenta %s saltada (%s)", acc.name, reason)

    if not runnable:
        raise HTTPException(
            status_code=429,
            detail=f"Todas las cuentas excedieron su rate limit ({len(skipped)} saltadas)",
        )

    job_id = job_store.create_job(
        text=body.text,
        accounts=body.accounts,
        image_path=image_path,
        callback_url=body.callback_url,
        job_type="immediate",
    )

    logger.info("Job %s aceptado (v2) | cuentas=%s", job_id, [a.name for a in runnable])
    _enqueue_job(job_id, runnable, body.text, image_path, body.callback_url)

    return PostAccepted(
        job_id=job_id,
        accounts=[a.name for a in runnable],
        text_preview=body.text[:80],
    )


# ---------------------------------------------------------------------------
# POST /v2/schedule
# ---------------------------------------------------------------------------

@router.post(
    "/schedule",
    response_model=ScheduleCreated,
    status_code=201,
    summary="Agendar publicación para una fecha futura",
    dependencies=[Depends(check_rate_limit)],
)
def create_schedule(body: ScheduleRequest):
    # Validar que las cuentas existen (si se filtraron)
    if body.accounts:
        try:
            all_accounts = load_accounts()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        found = [a for a in all_accounts if a.name in body.accounts]
        if not found:
            raise HTTPException(status_code=400, detail="Ninguna cuenta del filtro encontrada")

    image_path = None
    if body.image_path:
        from api_server import _safe_image_path
        image_path = _safe_image_path(body.image_path)
        if image_path is None:
            raise HTTPException(
                status_code=400,
                detail="image_path inválido o fuera del directorio permitido",
            )

    job_id = job_store.create_job(
        text=body.text,
        accounts=body.accounts,
        image_path=image_path,
        callback_url=body.callback_url,
        job_type="scheduled",
        scheduled_for=body.scheduled_for,
    )

    logger.info("Job %s agendado para %s (v2)", job_id, body.scheduled_for.isoformat())

    return ScheduleCreated(
        job_id=job_id,
        scheduled_for=body.scheduled_for.isoformat(),
        accounts=body.accounts or "todas",
        text_preview=body.text[:80],
    )


# ---------------------------------------------------------------------------
# GET /v2/schedule
# ---------------------------------------------------------------------------

@router.get(
    "/schedule",
    response_model=ScheduleListResponse,
    summary="Listar publicaciones agendadas pendientes",
    dependencies=[Depends(check_rate_limit)],
)
def list_schedules():
    jobs = job_store.list_pending_scheduled()
    return ScheduleListResponse(pending=jobs, count=len(jobs))


# ---------------------------------------------------------------------------
# DELETE /v2/schedule/{job_id}
# ---------------------------------------------------------------------------

@router.delete(
    "/schedule/{job_id}",
    status_code=204,
    summary="Cancelar una publicación agendada",
    dependencies=[Depends(check_rate_limit)],
)
def cancel_schedule(job_id: str):
    if not job_store.cancel_job(job_id):
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' no encontrado o ya no está pendiente",
        )
