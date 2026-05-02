"""
worker_main.py — Entry point del proceso Worker (Fase 3.7, SPLIT_PROCESSES=1).

Responsabilidades:
- Orphan recovery al arrancar (jobs 'running' de crash previo → 'interrupted')
- Sincronizar cuentas desde .env/DB
- ThreadPoolExecutor con max_concurrent_workers (límite de Chromes simultáneos)
- WorkerLoop: polling de jobs inmediatos via claim_pending_job() cada N segundos
- Scheduler: jobs agendados dispatched al mismo executor (fix Bug B1)
- Graceful shutdown en SIGTERM/SIGINT

Uso:
    SPLIT_PROCESSES=1 python worker_main.py
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import CONFIG, load_accounts
from logging_config import setup_logging
import job_store
import scheduler_runner
import worker_core

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent / "logs"
setup_logging(structured=CONFIG.get("structured_logging", False), log_dir=LOG_DIR)
logger = logging.getLogger("worker_main")

# ---------------------------------------------------------------------------
# Estado del proceso
# ---------------------------------------------------------------------------
_stop = threading.Event()


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------
async def _poll_loop(executor: ThreadPoolExecutor) -> None:
    poll_s = CONFIG.get("worker_poll_interval", 5)
    logger.info("Worker poll loop arrancando (interval=%ds)", poll_s)
    while not _stop.is_set():
        job = job_store.claim_pending_job()
        if job:
            # Deserializar filtro de cuentas
            import json
            account_filter = None
            raw_accounts = job.get("accounts")
            if raw_accounts:
                try:
                    account_filter = json.loads(raw_accounts)
                except Exception:
                    pass

            try:
                all_accounts = load_accounts()
            except ValueError as exc:
                logger.error("Job %s abortado — error cargando cuentas: %s", job["id"], exc)
                job_store.mark_failed(job["id"], str(exc))
                continue

            accounts = (
                [a for a in all_accounts if a.name in account_filter]
                if account_filter else all_accounts
            )
            if not accounts:
                msg = "Sin cuentas válidas para este job"
                logger.warning("Job %s: %s", job["id"], msg)
                job_store.mark_failed(job["id"], msg)
                continue

            logger.info("Job %s reclamado | cuentas=%s", job["id"], [a.name for a in accounts])
            executor.submit(
                worker_core.run_job,
                job["id"],
                accounts,
                job["text"],
                job.get("image_path"),
                job.get("callback_url"),
            )
        else:
            await asyncio.sleep(poll_s)
    logger.info("Worker poll loop detenido")


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------
def _install_signal_handlers(executor: ThreadPoolExecutor) -> None:
    def _handler(signum, _frame):
        if _stop.is_set():
            return
        _stop.set()
        logger.warning("Señal %s recibida — iniciando shutdown graceful del worker", signum)
        try:
            scheduler_runner.stop()
            executor.shutdown(wait=False, cancel_futures=True)
            n = job_store.mark_running_as_interrupted()
            if n:
                logger.info("Marcados %d jobs 'running' → 'interrupted'", n)
        except Exception:
            logger.exception("Error durante shutdown del worker")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    job_store.init_db()

    orphans = job_store.mark_running_as_interrupted()
    if orphans:
        logger.warning(
            "Orphan recovery: %d jobs 'running' de shutdown previo → 'interrupted'", orphans
        )

    n = job_store.upsert_accounts(load_accounts())
    logger.info("Sincronizadas %d cuentas en DB", n)

    executor = ThreadPoolExecutor(
        max_workers=CONFIG.get("max_concurrent_workers", 2),
        thread_name_prefix="fb-worker",
    )

    # Scheduler con dispatch_fn → scheduled jobs respetan max_concurrent_workers
    scheduler_runner.start(dispatch_fn=executor.submit)
    logger.info("Scheduler arrancado (dispatch via executor)")

    _install_signal_handlers(executor)

    logger.info(
        "Worker arrancado | max_workers=%d | poll=%ds",
        CONFIG.get("max_concurrent_workers", 2),
        CONFIG.get("worker_poll_interval", 5),
    )
    asyncio.run(_poll_loop(executor))


if __name__ == "__main__":
    main()
