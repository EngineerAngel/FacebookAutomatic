"""
job_store.py — SQLite como fuente única de verdad para todos los jobs.

Reemplaza scheduler_store.py. Maneja:
  - Creación de jobs (inmediatos y agendados)
  - Transiciones de estado: pending → running → done | failed | cancelled
  - Resultados por cuenta/grupo
  - Consulta de jobs agendados pendientes (para scheduler_runner)

Base de datos: jobs.db (local, gitignored, nunca expuesto por API)
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "jobs.db"
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # lecturas concurrentes sin bloqueo
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Inicialización
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Crea las tablas si no existen. Llamar una vez al arrancar."""
    with _lock, _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                text          TEXT NOT NULL,
                accounts      TEXT,           -- JSON array or NULL (=all)
                image_path    TEXT,
                callback_url  TEXT,
                type          TEXT NOT NULL,  -- 'immediate' | 'scheduled'
                scheduled_for TEXT,           -- ISO 8601, only for scheduled
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    TEXT NOT NULL,
                started_at    TEXT,
                finished_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS job_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id       TEXT NOT NULL REFERENCES jobs(id),
                account_name TEXT NOT NULL,
                group_id     TEXT NOT NULL,
                success      INTEGER NOT NULL,  -- 1 | 0
                error_msg    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_scheduled ON jobs(scheduled_for)
                WHERE type = 'scheduled';
        """)


# ---------------------------------------------------------------------------
# Creación
# ---------------------------------------------------------------------------
def create_job(
    text: str,
    accounts: list[str] | None,
    image_path: str | None,
    callback_url: str | None,
    job_type: str = "immediate",
    scheduled_for: datetime | None = None,
) -> str:
    """Inserta un nuevo job y retorna su id."""
    job_id = uuid.uuid4().hex[:12]
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO jobs
               (id, text, accounts, image_path, callback_url,
                type, scheduled_for, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                text,
                json.dumps(accounts) if accounts else None,
                image_path,
                callback_url,
                job_type,
                scheduled_for.isoformat() if scheduled_for else None,
                "pending",
                datetime.now().isoformat(),
            ),
        )
    return job_id


# ---------------------------------------------------------------------------
# Transiciones de estado
# ---------------------------------------------------------------------------
def mark_running(job_id: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=?",
            (datetime.now().isoformat(), job_id),
        )


def mark_done(job_id: str, results: dict[str, dict[str, bool]]) -> None:
    """
    Guarda resultados y marca el job como done.
    results = {account_name: {group_id: True|False}}
    """
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=? WHERE id=?",
            (datetime.now().isoformat(), job_id),
        )
        for account_name, group_results in results.items():
            for group_id, success in group_results.items():
                conn.execute(
                    """INSERT INTO job_results
                       (job_id, account_name, group_id, success)
                       VALUES (?,?,?,?)""",
                    (job_id, account_name, group_id, int(success)),
                )


def mark_failed(job_id: str, error_msg: str = "") -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status='failed', finished_at=?, accounts=COALESCE(accounts, accounts) WHERE id=?",
            (datetime.now().isoformat(), job_id),
        )
        # Registrar el error como resultado especial
        conn.execute(
            """INSERT INTO job_results (job_id, account_name, group_id, success, error_msg)
               VALUES (?, '__system__', '__error__', 0, ?)""",
            (job_id, error_msg),
        )


def cancel_job(job_id: str) -> bool:
    """Cancela un job en estado pending/scheduled. Retorna True si existía."""
    with _lock, _connect() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=? AND status='pending'",
            (job_id,),
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Consultas para scheduler_runner
# ---------------------------------------------------------------------------
def pop_due_scheduled(now: datetime) -> list[dict]:
    """
    Retorna y marca como 'running' todos los scheduled jobs cuya hora ya pasó.
    Operación atómica para evitar doble disparo.
    """
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, text, accounts, image_path, callback_url, scheduled_for
               FROM jobs
               WHERE type='scheduled' AND status='pending'
                 AND scheduled_for <= ?""",
            (now.isoformat(),),
        ).fetchall()

        due = []
        for row in rows:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?",
                (now.isoformat(), row["id"]),
            )
            accounts = json.loads(row["accounts"]) if row["accounts"] else None
            due.append({
                "id": row["id"],
                "text": row["text"],
                "accounts": accounts,
                "image_path": row["image_path"],
                "callback_url": row["callback_url"],
                "scheduled_for": row["scheduled_for"],
            })
        return due


# ---------------------------------------------------------------------------
# Consultas para API
# ---------------------------------------------------------------------------
def list_pending_scheduled() -> list[dict]:
    """Lista jobs agendados aún pendientes (para GET /schedule)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, text, accounts, image_path, callback_url, scheduled_for, created_at
               FROM jobs WHERE type='scheduled' AND status='pending'
               ORDER BY scheduled_for ASC""",
        ).fetchall()
        return [
            {
                "id": r["id"],
                "text": r["text"],
                "accounts": json.loads(r["accounts"]) if r["accounts"] else None,
                "image_path": r["image_path"],
                "callback_url": r["callback_url"],
                "scheduled_for": r["scheduled_for"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
