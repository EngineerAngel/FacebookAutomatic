"""
scheduler_runner.py — Hilo daemon que dispara publicaciones agendadas.

Cada POLL_SECONDS consulta job_store.pop_due_scheduled() y lanza
un hilo por cada job due. Usa el mismo _run_job de api_server
para garantizar comportamiento idéntico (SQLite + webhook).
"""

import logging
import threading
import time
from datetime import datetime

from config import CONFIG, load_accounts, apply_group_filter
import job_store
import webhook
from account_manager import AccountManager

POLL_SECONDS = 30

logger = logging.getLogger("scheduler_runner")
logger.setLevel(logging.INFO)

_ch = logging.StreamHandler()
_ch.setFormatter(
    logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
)
logger.addHandler(_ch)

# Señal de parada para graceful shutdown
_stop_event = threading.Event()

# Purga diaria de eventos de rate limit viejos (retención 7 días)
_PURGE_INTERVAL_S = 24 * 3600
_last_purge_ts: float = 0.0


def _run_scheduled_job(job: dict) -> None:
    """Ejecuta un job agendado: mismo pipeline que POST /post."""
    job_id = job["id"]
    text = job["text"]
    image_paths = job.get("image_paths") or []
    callback_url = job.get("callback_url")
    account_filter = job.get("accounts")
    group_ids = job.get("group_ids")  # dict[str, list[str]] | None

    try:
        accounts = load_accounts()
    except ValueError as exc:
        logger.error("Job %s abortado — error cargando cuentas: %s", job_id, exc)
        job_store.mark_failed(job_id, str(exc))
        webhook.fire(callback_url, job_id, "failed", error_msg=str(exc))
        return

    if account_filter:
        accounts = [a for a in accounts if a.name in account_filter]

    # Aplicar filtro de grupos seleccionados por cuenta
    if group_ids is not None:
        accounts = apply_group_filter(accounts, group_ids)

    if not accounts:
        msg = "Sin cuentas válidas para este job"
        logger.warning("Job %s: %s", job_id, msg)
        job_store.mark_failed(job_id, msg)
        webhook.fire(callback_url, job_id, "failed", error_msg=msg)
        return

    logger.info("Disparando job agendado %s | cuentas=%s",
                job_id, [a.name for a in accounts])

    try:
        mgr = AccountManager(
            accounts, CONFIG, text,
            image_paths=image_paths, callback_url=callback_url,
        )
        results = mgr.run()
        mgr.print_summary(results)
        job_store.mark_done(job_id, results)
        webhook.fire(callback_url, job_id, "done", results)
    except Exception:
        logger.exception("Fallo ejecutando job %s", job_id)
        job_store.mark_failed(job_id, "Unhandled exception")
        webhook.fire(callback_url, job_id, "failed",
                     error_msg="Unhandled exception")


def _maybe_purge_rate_limits() -> None:
    """Purga eventos de rate limit viejos una vez al día."""
    global _last_purge_ts
    now = time.time()
    if now - _last_purge_ts < _PURGE_INTERVAL_S:
        return
    try:
        deleted = job_store.purge_old_rate_limit_events(days=7)
        if deleted:
            logger.info("Purge rate_limit_events: %d filas eliminadas", deleted)
    except Exception:
        logger.exception("Error en purge de rate_limit_events")
    _last_purge_ts = now


def _loop() -> None:
    logger.info("Scheduler loop arrancando (poll=%ds)", POLL_SECONDS)
    while not _stop_event.is_set():
        try:
            due = job_store.pop_due_scheduled(datetime.now())
            for job in due:
                threading.Thread(
                    target=_run_scheduled_job,
                    args=(job,),
                    daemon=True,
                    name=f"scheduled-{job['id']}",
                ).start()
            _maybe_purge_rate_limits()
        except Exception:
            logger.exception("Error en el loop del scheduler")
        # Espera interrumpible — permite stop() inmediato
        _stop_event.wait(timeout=POLL_SECONDS)
    logger.info("Scheduler loop detenido")


def start() -> threading.Thread:
    _stop_event.clear()
    t = threading.Thread(target=_loop, daemon=True, name="scheduler-runner")
    t.start()
    return t


def stop() -> None:
    """Señaliza al loop que debe salir. Idempotente."""
    _stop_event.set()
