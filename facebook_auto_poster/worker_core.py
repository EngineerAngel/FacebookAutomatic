"""
worker_core.py — Lógica de ejecución de jobs compartida entre api_server y worker_main.

Contiene:
- Account locks (previene que dos jobs usen el mismo user_data_dir simultáneamente)
- _running_accounts: set actualizado en tiempo real durante la ejecución
- filter_rate_limited_accounts(): filtra cuentas que excedieron su tope hora/día
- run_job(): ejecuta un job completo (lock → rate-limit → AsyncAccountManager → webhook)

Tanto api_server (modo monoproceso) como worker_main (modo biproceso) importan desde aquí.
No importa nada de Flask.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from config import CONFIG
from account_manager_async import AsyncAccountManager
import job_store
import metrics
import webhook

logger = logging.getLogger("worker_core")

# ---------------------------------------------------------------------------
# Account locks — evita que dos jobs compitan por el mismo user_data_dir
# ---------------------------------------------------------------------------
_account_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_running_accounts: set[str] = set()
_running_accounts_lock = threading.Lock()


def _get_account_lock(name: str) -> threading.Lock:
    with _locks_guard:
        if name not in _account_locks:
            _account_locks[name] = threading.Lock()
        return _account_locks[name]


def running_accounts_snapshot() -> list[str]:
    """Retorna copia ordenada de cuentas actualmente en ejecución."""
    with _running_accounts_lock:
        return sorted(_running_accounts)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def filter_rate_limited_accounts(accounts):
    """Aparta cuentas que ya alcanzaron su tope por hora/día.

    Retorna (ok, skipped) donde skipped es lista de (account, reason).
    """
    max_h = CONFIG.get("max_posts_per_account_per_hour", 3)
    max_d = CONFIG.get("max_posts_per_account_per_day", 15)
    ok, skipped = [], []
    for a in accounts:
        h = job_store.account_recent_post_count(a.name, window_minutes=60)
        if h >= max_h:
            skipped.append((a, f"rate_limit_h={h}/{max_h}"))
            continue
        d = job_store.account_recent_post_count(a.name, window_minutes=24 * 60)
        if d >= max_d:
            skipped.append((a, f"rate_limit_d={d}/{max_d}"))
            continue
        ok.append(a)
    return ok, skipped


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

def run_job(
    job_id: str,
    accounts,
    text: str,
    image_path: str | None,
    callback_url: str | None,
) -> None:
    """Ejecuta un job completo: locks → rate-limit → AsyncAccountManager → webhook."""
    # Adquirir locks en orden lexicográfico para prevenir deadlocks
    accounts_sorted = sorted(accounts, key=lambda a: a.name)
    locks = [_get_account_lock(a.name) for a in accounts_sorted]
    for lk in locks:
        lk.acquire()
    for a in accounts_sorted:
        with _running_accounts_lock:
            _running_accounts.add(a.name)

    job_store.mark_running(job_id)
    try:
        runnable, skipped = filter_rate_limited_accounts(accounts)
        for acc, reason in skipped:
            logger.warning("Job %s: cuenta %s saltada (%s)", job_id, acc.name, reason)
        if not runnable:
            msg = f"Todas las cuentas excedieron su rate limit ({len(skipped)} saltadas)"
            logger.warning("Job %s: %s", job_id, msg)
            job_store.mark_failed(job_id, msg)
            metrics.inc_job("failed")
            webhook.fire(callback_url, job_id, "failed", error_msg=msg)
            return

        mgr = AsyncAccountManager(
            runnable, CONFIG, text,
            image_path=image_path, callback_url=callback_url,
        )
        results = asyncio.run(mgr.run())
        AsyncAccountManager.print_summary(results)
        job_store.mark_done(job_id, results)
        metrics.inc_job("done")
        webhook.fire(callback_url, job_id, "done", results)
    except Exception:
        logger.exception("Fallo en worker | job=%s", job_id)
        job_store.mark_failed(job_id, "Unhandled exception in worker")
        metrics.inc_job("failed")
        webhook.fire(callback_url, job_id, "failed", error_msg="Unhandled exception in worker")
    finally:
        for a in accounts_sorted:
            with _running_accounts_lock:
                _running_accounts.discard(a.name)
        for lk in reversed(locks):
            try:
                lk.release()
            except RuntimeError:
                pass
