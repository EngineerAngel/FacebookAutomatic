"""
v2_app.py — Ensambla la aplicación FastAPI con Flask montado como sub-app.

Arquitectura (cuando USE_FASTAPI=1):
  FastAPI  →  /v2/*   (endpoints nuevos con Pydantic, Swagger UI en /docs)
  Flask    →  /*      (todos los endpoints existentes, sin cambios)

Flask se monta con WSGIMiddleware de starlette — un único proceso,
un único puerto. OpenClaw puede apuntar a /v2/post o /post; ambos
responden de forma idéntica.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware

from v2_router import router


def create_app(flask_app) -> FastAPI:
    """
    Crea la FastAPI app y monta Flask en /.

    Args:
        flask_app: la instancia `app` de api_server.py

    Returns:
        FastAPI app lista para servir con uvicorn.
    """
    fastapi_app = FastAPI(
        title="Facebook Auto-Poster API",
        description=(
            "API para publicación automática en grupos de Facebook. "
            "Endpoints /v2/* son la versión moderna con validación estricta. "
            "Endpoints / son la versión Flask (compatibilidad con OpenClaw existente)."
        ),
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    fastapi_app.include_router(router)

    # Flask maneja todo lo que no matchea /v2/* ni /docs ni /redoc
    fastapi_app.mount("/", WSGIMiddleware(flask_app))

    return fastapi_app
