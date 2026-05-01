"""
logging_config.py — Configuración centralizada de logging (Fase 3.3a).

Flag `structured_logging` en CONFIG:
  False (default) → texto clásico, comportamiento idéntico al original.
  True            → JSON por línea vía structlog. Facilita grep, jq y Loki.

API pública:
  setup_logging(structured, log_dir, file_level, console_level)
  get_formatter() → logging.Formatter  (para handlers per-account en FacebookPoster)
  bind_account(account_name)           (llama al inicio del hilo de la cuenta)
  unbind_account()                     (llama al cerrar el poster)
"""

from __future__ import annotations

import logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Estado interno
# ---------------------------------------------------------------------------
_TEXT_FMT = "%(asctime)s - [%(name)s] - %(levelname)s - %(message)s"
_structured: bool = False


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def setup_logging(
    structured: bool,
    log_dir: Path,
    file_level: int = logging.DEBUG,
    console_level: int = logging.INFO,
) -> None:
    """Configura el root logger. Llamar una sola vez al arranque (main.py).

    structured=False → texto clásico, sin dependencias extra.
    structured=True  → JSON por línea vía structlog (requiere structlog instalado).
    """
    global _structured
    _structured = structured

    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # Limpiar handlers previos para evitar duplicados si se llama más de una vez
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_dir / "main.log", encoding="utf-8")
    fh.setLevel(file_level)

    ch = logging.StreamHandler()
    ch.setLevel(console_level)

    if structured:
        fmt = _build_structlog_formatter()
    else:
        fmt = logging.Formatter(_TEXT_FMT)

    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(ch)


def get_formatter() -> logging.Formatter:
    """Devuelve el formatter activo (texto o JSON) para handlers per-account."""
    if _structured:
        return _build_structlog_formatter()
    return logging.Formatter(_TEXT_FMT)


def bind_account(account_name: str) -> None:
    """Añade `account` a todos los logs del thread actual (structlog contextvars).

    Si structured_logging está OFF es un no-op — cero overhead.
    Llamar al inicio de login() / publish_to_all_groups() en el hilo del worker.
    """
    if _structured:
        import structlog
        structlog.contextvars.bind_contextvars(account=account_name)


def unbind_account() -> None:
    """Limpia los contextvars del thread actual. Llamar en FacebookPoster.close()."""
    if _structured:
        import structlog
        structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _build_structlog_formatter() -> logging.Formatter:
    """Construye el ProcessorFormatter de structlog para stdlib logging."""
    import structlog

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    return structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )
