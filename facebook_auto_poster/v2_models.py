"""
v2_models.py — Pydantic v2 models para la API /v2 (Fase 3.2).

Cada modelo valida automáticamente los datos antes de que el endpoint
se ejecute. Cualquier campo inválido devuelve 422 con descripción exacta.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

class PostRequest(BaseModel):
    """Publicación inmediata en grupos de Facebook."""

    text: str = Field(..., min_length=1, max_length=63206)
    image_path: Optional[str] = Field(None)
    accounts: Optional[list[str]] = Field(
        None,
        description="Nombres de cuentas. Omitir para usar todas las activas.",
    )
    callback_url: Optional[str] = Field(None)

    @field_validator("accounts")
    @classmethod
    def accounts_not_empty(cls, v):
        if v is not None and len(v) == 0:
            raise ValueError("'accounts' no puede ser lista vacía; omite el campo para usar todas")
        return v


class ScheduleRequest(BaseModel):
    """Publicación agendada para una fecha/hora futura."""

    text: str = Field(..., min_length=1, max_length=63206)
    scheduled_for: datetime = Field(
        ...,
        description="Fecha y hora en ISO 8601, ej: 2026-05-15T10:30:00",
    )
    image_path: Optional[str] = Field(None)
    accounts: Optional[list[str]] = Field(None)
    callback_url: Optional[str] = Field(None)

    @field_validator("scheduled_for")
    @classmethod
    def must_be_future(cls, v: datetime) -> datetime:
        from datetime import timezone
        now = datetime.now(timezone.utc) if v.tzinfo else datetime.now()
        if v <= now:
            raise ValueError("'scheduled_for' debe ser una fecha futura")
        return v


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class PostAccepted(BaseModel):
    status: str = "accepted"
    job_id: str
    accounts: list[str]
    text_preview: str


class ScheduleCreated(BaseModel):
    status: str = "scheduled"
    job_id: str
    scheduled_for: str
    accounts: list[str] | str
    text_preview: str


class AccountOut(BaseModel):
    name: str
    email: str
    groups: list[str]
    last_login_at: Optional[str] = None
    last_published_at: Optional[str] = None


class AccountsListResponse(BaseModel):
    accounts: list[AccountOut]


class ScheduledJobOut(BaseModel):
    id: str
    text: str
    scheduled_for: str
    accounts: Optional[list[str]] = None
    callback_url: Optional[str] = None


class ScheduleListResponse(BaseModel):
    pending: list[dict]
    count: int
