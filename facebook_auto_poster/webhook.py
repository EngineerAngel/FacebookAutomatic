"""
webhook.py — Dispara el callback hacia OpenClaw cuando un job termina.

Fire-and-forget: corre en su propio hilo daemon, no bloquea al worker.
3 reintentos con backoff exponencial (2s → 4s → 8s).
"""

import logging
import threading
import time
from datetime import datetime

import requests

logger = logging.getLogger("webhook")


def _build_payload(
    job_id: str,
    status: str,
    results: dict[str, dict[str, bool]] | None = None,
    error_msg: str | None = None,
) -> dict:
    """Construye el payload que recibirá OpenClaw."""
    total = succeeded = failed = 0

    if results:
        for group_results in results.values():
            for ok in group_results.values():
                total += 1
                if ok:
                    succeeded += 1
                else:
                    failed += 1

    payload: dict = {
        "job_id": job_id,
        "status": status,
        "finished_at": datetime.now().isoformat(),
        "results": results or {},
        "summary": {
            "total_groups": total,
            "succeeded": succeeded,
            "failed": failed,
        },
    }

    if error_msg:
        payload["error"] = error_msg

    return payload


def _fire_with_retry(url: str, payload: dict, max_retries: int = 3) -> None:
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info(
                "Webhook OK | job=%s → %s (%d)",
                payload.get("job_id"),
                url,
                resp.status_code,
            )
            return
        except Exception as exc:
            logger.warning(
                "Webhook intento %d/%d fallido | job=%s: %s",
                attempt,
                max_retries,
                payload.get("job_id"),
                exc,
            )
            if attempt < max_retries:
                time.sleep(2 ** attempt)  # 2s, 4s, 8s

    logger.error(
        "Webhook definitivamente fallido | job=%s → %s",
        payload.get("job_id"),
        url,
    )


def fire(
    url: str | None,
    job_id: str,
    status: str,
    results: dict[str, dict[str, bool]] | None = None,
    error_msg: str | None = None,
) -> None:
    """Dispara webhook en hilo daemon. Retorna inmediatamente."""
    if not url:
        return
    payload = _build_payload(job_id, status, results, error_msg)
    threading.Thread(
        target=_fire_with_retry,
        args=(url, payload),
        daemon=True,
        name=f"webhook-{job_id}",
    ).start()
