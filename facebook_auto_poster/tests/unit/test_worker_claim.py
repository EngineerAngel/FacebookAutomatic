"""
tests/unit/test_worker_claim.py — Tests para claim_pending_job() (Fase 3.7).

Valida atomicidad, orden FIFO, y que solo se reclaman jobs inmediatos.
"""
from __future__ import annotations

import threading
from datetime import datetime
import pytest
import job_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_immediate_job(text: str = "test job") -> str:
    return job_store.create_job(
        text=text,
        accounts=["cuenta1"],
        image_path=None,
        callback_url=None,
        job_type="immediate",
    )


def _create_scheduled_job() -> str:
    return job_store.create_job(
        text="scheduled",
        accounts=["cuenta1"],
        image_path=None,
        callback_url=None,
        job_type="scheduled",
        scheduled_for=datetime(2099, 1, 1),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_claim_returns_none_when_no_pending(tmp_db):
    """Sin jobs pendientes, claim devuelve None."""
    result = job_store.claim_pending_job()
    assert result is None


def test_claim_marks_job_running(tmp_db):
    """Después del claim, el job tiene status='running'."""
    job_id = _create_immediate_job()
    result = job_store.claim_pending_job()

    assert result is not None
    assert result["id"] == job_id

    # Verificar que el status cambió en DB
    jobs = job_store.get_recent_jobs(limit=1)
    assert jobs[0]["status"] == "running"


def test_claim_returns_none_after_already_claimed(tmp_db):
    """Un segundo claim sin jobs nuevos devuelve None."""
    _create_immediate_job()
    first = job_store.claim_pending_job()
    second = job_store.claim_pending_job()

    assert first is not None
    assert second is None


def test_claim_ignores_scheduled_jobs(tmp_db):
    """claim_pending_job solo recoge type='immediate', no 'scheduled'."""
    _create_scheduled_job()
    result = job_store.claim_pending_job()
    assert result is None


def test_claim_fifo_order(tmp_db):
    """Los jobs se reclaman en orden FIFO (el más antiguo primero)."""
    id1 = _create_immediate_job("primero")
    id2 = _create_immediate_job("segundo")

    first_claim = job_store.claim_pending_job()
    second_claim = job_store.claim_pending_job()

    assert first_claim["id"] == id1
    assert second_claim["id"] == id2


def test_claim_atomic_two_threads(tmp_db):
    """Dos threads compitiendo: exactamente uno consigue el job."""
    job_id = _create_immediate_job()
    results: list[dict] = []
    lock = threading.Lock()

    def try_claim():
        r = job_store.claim_pending_job()
        if r is not None:
            with lock:
                results.append(r)

    t1 = threading.Thread(target=try_claim)
    t2 = threading.Thread(target=try_claim)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 1, f"Esperaba 1 claim, obtuvo {len(results)}"
    assert results[0]["id"] == job_id
