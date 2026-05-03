"""
scheduler_runner.py — Hilo daemon que dispara publicaciones agendadas.

Cada POLL_SECONDS consulta job_store.pop_due_scheduled() y lanza
un hilo por cada job due. Usa el mismo _run_job de api_server
para garantizar comportamiento idéntico (SQLite + webhook).
"""

import asyncio
import logging
import threading
import time
from datetime import datetime

from config import CONFIG, load_accounts, apply_group_filter
import job_store
import webhook
from account_manager_async import AsyncAccountManager

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
    image_paths: list[str] = job.get("image_paths") or []
    if not image_paths and job.get("image_path"):
        image_paths = [job["image_path"]]  # backward compat: job legacy con string
    group_ids = job.get("group_ids")
    callback_url = job.get("callback_url")
    account_filter = job.get("accounts")

    try:
        accounts = load_accounts()
    except ValueError as exc:
        logger.error("Job %s abortado — error cargando cuentas: %s", job_id, exc)
        job_store.mark_failed(job_id, str(exc))
        webhook.fire(callback_url, job_id, "failed", error_msg=str(exc))
        return

    if account_filter:
        accounts = [a for a in accounts if a.name in account_filter]

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
        mgr = AsyncAccountManager(
            accounts, CONFIG, text,
            image_paths=image_paths or None, callback_url=callback_url,
        )
        results = asyncio.run(mgr.run())
        AsyncAccountManager.print_summary(results)
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


def _loop(dispatch_fn=None) -> None:
    logger.info("Scheduler loop arrancando (poll=%ds)", POLL_SECONDS)
    while not _stop_event.is_set():
        try:
            due = job_store.pop_due_scheduled(datetime.now())
            for job in due:
                if dispatch_fn is not None:
                    # Modo biproceso: el worker controla concurrencia via su executor
                    dispatch_fn(_run_scheduled_job, job)
                else:
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


def start(dispatch_fn=None) -> threading.Thread:
    """Arranca el scheduler.

    dispatch_fn: si se proporciona, los jobs agendados se despachan a través
    de esta función en lugar de lanzar threads crudos. Usar en modo biproceso
    para que los scheduled jobs respeten MAX_CONCURRENT_WORKERS del worker.
    Ejemplo: scheduler_runner.start(dispatch_fn=executor.submit)
    """
    _stop_event.clear()
    t = threading.Thread(target=_loop, args=(dispatch_fn,), daemon=True, name="scheduler-runner")
    t.start()
    return t


def stop() -> None:
    """Señaliza al loop que debe salir. Idempotente."""
    _stop_event.set()
