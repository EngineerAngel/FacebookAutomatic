"""
v2_deps.py — Dependencias FastAPI para /v2 (Fase 3.2).

Reutiliza la lógica de autenticación y rate limiting ya existente
en api_server.py — no duplica código, solo lo adapta al patrón Depends.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Depends, Header, HTTPException, Request

import job_store

OPENCLAW_API_KEY = os.getenv("OPENCLAW_API_KEY", "").strip()

_RATE_LIMIT  = 10   # máx requests por ventana
_RATE_WINDOW = 60   # ventana en segundos


def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    """Verifica X-API-Key — equivalente al decorador @openclaw_required de Flask."""
    if not OPENCLAW_API_KEY:
        raise HTTPException(status_code=503, detail="OPENCLAW_API_KEY no configurado en .env")
    if not secrets.compare_digest(x_api_key.strip(), OPENCLAW_API_KEY):
        raise HTTPException(status_code=401, detail="API key inválida o ausente")


def check_rate_limit(request: Request, _: None = Depends(verify_api_key)) -> None:
    """Rate limit por IP — persistente en SQLite, igual que en Flask."""
    ip = request.client.host if request.client else "unknown"
    endpoint = request.url.path
    if job_store.is_rate_limited(ip, endpoint, _RATE_LIMIT, _RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Demasiadas peticiones, espera un momento")
