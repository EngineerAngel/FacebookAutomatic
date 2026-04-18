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

from config import CONFIG, load_accounts
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


def _run_scheduled_job(job: dict) -> None:
    """Ejecuta un job agendado: mismo pipeline que POST /post."""
    job_id = job["id"]
    text = job["text"]
    image_path = job.get("image_path")
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

    if not accounts:
        msg = "Sin cuentas válidas para este job"
        logger.warning("Job %s: %s", job_id, msg)
        job_store.mark_failed(job_id, msg)
        webhook.fire(callback_url, job_id, "failed", error_msg=msg)
        return

    logger.info("Disparando job agendado %s | cuentas=%s",
                job_id, [a.name for a in accounts])

    try:
        mgr = AccountManager(accounts, CONFIG, text, image_path=image_path)
        results = mgr.run()
        mgr.print_summary(results)
        job_store.mark_done(job_id, results)
        webhook.fire(callback_url, job_id, "done", results)
    except Exception:
        logger.exception("Fallo ejecutando job %s", job_id)
        job_store.mark_failed(job_id, "Unhandled exception")
        webhook.fire(callback_url, job_id, "failed",
                     error_msg="Unhandled exception")


def _loop() -> None:
    logger.info("Scheduler loop arrancando (poll=%ds)", POLL_SECONDS)
    while True:
        try:
            due = job_store.pop_due_scheduled(datetime.now())
            for job in due:
                threading.Thread(
                    target=_run_scheduled_job,
                    args=(job,),
                    daemon=True,
                    name=f"scheduled-{job['id']}",
                ).start()
        except Exception:
            logger.exception("Error en el loop del scheduler")
        time.sleep(POLL_SECONDS)


def start() -> threading.Thread:
    t = threading.Thread(target=_loop, daemon=True, name="scheduler-runner")
    t.start()
    return t
