"""
test_job_store_concurrent.py — Verifica que job_store sin _lock global
no produce errores bajo escrituras concurrentes (Fase 3, ítem 3.5).

SQLite WAL + busy_timeout=5000 debe manejar la contención sin
necesidad del threading.Lock() de Python.
"""

import threading

import pytest


def test_concurrent_create_job_no_errors(tmp_db):
    """100 threads crean un job cada uno — sin errores ni IDs duplicados."""
    import job_store

    ids = []
    errors = []
    lock = threading.Lock()  # solo para recolectar resultados, no para la DB

    def worker(n: int) -> None:
        try:
            job_id = job_store.create_job(
                text=f"mensaje {n}", accounts=None, image_path=None, callback_url=None
            )
            with lock:
                ids.append(job_id)
        except Exception as exc:
            with lock:
                errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Errores en threads: {errors}"
    assert len(ids) == 100
    assert len(set(ids)) == 100, "Se generaron IDs duplicados"


def test_concurrent_mixed_reads_and_writes(tmp_db):
    """Readers y writers concurrentes — WAL permite lecturas mientras se escribe."""
    import job_store

    job_store.create_account("cuenta_test", "t@t.com", ["111"])
    errors = []
    lock = threading.Lock()

    def writer(n: int) -> None:
        try:
            job_store.create_job(
                text=f"write {n}", accounts=None, image_path=None, callback_url=None
            )
        except Exception as exc:
            with lock:
                errors.append(f"writer-{n}: {exc}")

    def reader(n: int) -> None:
        try:
            job_store.get_recent_jobs(limit=10)
            job_store.count_active_accounts()
        except Exception as exc:
            with lock:
                errors.append(f"reader-{n}: {exc}")

    threads = (
        [threading.Thread(target=writer, args=(i,)) for i in range(50)]
        + [threading.Thread(target=reader, args=(i,)) for i in range(50)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Errores bajo carga mixta: {errors}"


def test_no_database_locked_error_under_write_contention(tmp_db):
    """busy_timeout=5000 hace que escrituras concurrentes esperen en vez de
    lanzar OperationalError('database is locked')."""
    import job_store

    errors = []
    lock = threading.Lock()

    def batch_writer(batch_id: int) -> None:
        for i in range(10):
            try:
                job_store.create_job(
                    text=f"batch {batch_id} item {i}",
                    accounts=None, image_path=None, callback_url=None,
                )
            except Exception as exc:
                with lock:
                    errors.append(str(exc))

    threads = [threading.Thread(target=batch_writer, args=(b,)) for b in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    db_locked = [e for e in errors if "locked" in e.lower()]
    assert db_locked == [], f"'database is locked' bajo contención: {db_locked}"

    jobs = job_store.get_recent_jobs(limit=200)
    assert len(jobs) == 100, f"Esperados 100 jobs, encontrados {len(jobs)}"
