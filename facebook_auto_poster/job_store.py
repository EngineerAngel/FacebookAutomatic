"""
job_store.py — SQLite como fuente única de verdad para todos los jobs y cuentas.

Tablas:
  accounts     — cuentas activas (nombre, email, grupos, historial)
  group_tags   — etiquetas amigables por grupo (default 'generico')
  login_events — historial de intentos de login
  jobs         — cola de trabajos (inmediatos y agendados)
  job_results  — resultados por cuenta/grupo

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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Inicialización y migraciones
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Crea las tablas si no existen y aplica migraciones seguras."""
    with _lock, _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                name              TEXT PRIMARY KEY,
                email             TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                last_login_at     TEXT,
                last_published_at TEXT
            );

            CREATE TABLE IF NOT EXISTS group_tags (
                group_id  TEXT PRIMARY KEY,
                tag       TEXT NOT NULL DEFAULT 'generico'
            );

            CREATE TABLE IF NOT EXISTS login_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                logged_in_at TEXT NOT NULL,
                success      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                text          TEXT NOT NULL,
                accounts      TEXT,
                image_path    TEXT,
                callback_url  TEXT,
                type          TEXT NOT NULL,
                scheduled_for TEXT,
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
                group_tag    TEXT NOT NULL DEFAULT 'generico',
                success      INTEGER NOT NULL,
                error_msg    TEXT
            );

            CREATE TABLE IF NOT EXISTS account_cookies (
                email      TEXT PRIMARY KEY,
                cookies    TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gemini_usage (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                used_at      TEXT NOT NULL,
                group_id     TEXT,
                success      INTEGER NOT NULL DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_scheduled ON jobs(scheduled_for)
                WHERE type = 'scheduled';
            CREATE INDEX IF NOT EXISTS idx_login_events_account ON login_events(account_name);
            CREATE INDEX IF NOT EXISTS idx_gemini_usage_account_date
                ON gemini_usage(account_name, used_at);
        """)

        # Migraciones seguras — no fallan si la columna ya existe
        for stmt in [
            "ALTER TABLE job_results ADD COLUMN group_tag TEXT NOT NULL DEFAULT 'generico'",
            "ALTER TABLE accounts ADD COLUMN groups TEXT",
            "ALTER TABLE accounts ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE accounts ADD COLUMN timezone TEXT NOT NULL DEFAULT 'America/Mexico_City'",
            "ALTER TABLE accounts ADD COLUMN active_hours TEXT NOT NULL DEFAULT '[7, 23]'",
            "ALTER TABLE accounts ADD COLUMN fingerprint_json TEXT",
        ]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass


# ---------------------------------------------------------------------------
# Cuentas — sincronización desde .env
# ---------------------------------------------------------------------------
def upsert_accounts(accounts) -> int:
    """
    Sincroniza la lista de AccountConfig desde .env a la tabla accounts.
    Guarda groups como JSON. Preserva last_login_at y last_published_at.
    Retorna el número de cuentas sincronizadas.
    """
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        for acc in accounts:
            conn.execute(
                """INSERT INTO accounts (name, email, groups, created_at, is_active)
                   VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT(name) DO UPDATE SET
                       email    = excluded.email,
                       groups   = excluded.groups,
                       is_active = 1""",
                (acc.name, acc.email, json.dumps(acc.groups), now),
            )
    return len(accounts)


def get_accounts_info() -> list[dict]:
    """Retorna metadatos de cuentas activas (para GET /accounts de OpenClaw)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT name, email, groups, last_login_at, last_published_at
               FROM accounts WHERE is_active=1"""
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Cuentas — CRUD desde UI admin
# ---------------------------------------------------------------------------
def list_accounts_full() -> list[dict]:
    """Lista completa de cuentas activas con todos sus campos."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT name, email, groups, timezone, active_hours, fingerprint_json,
                      created_at, last_login_at, last_published_at
               FROM accounts WHERE is_active=1
               ORDER BY name ASC"""
        ).fetchall()
        return [dict(r) for r in rows]


def create_account(name: str, email: str, groups: list[str],
                   fingerprint_json: str | None = None) -> None:
    """Crea una cuenta nueva o reactiva una eliminada."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO accounts (name, email, groups, fingerprint_json, created_at, is_active)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(name) DO UPDATE SET
                   email          = excluded.email,
                   groups         = excluded.groups,
                   fingerprint_json = COALESCE(excluded.fingerprint_json, accounts.fingerprint_json),
                   is_active      = 1""",
            (name, email, json.dumps(groups), fingerprint_json, now),
        )


def save_fingerprint(account_name: str, fingerprint_json: str) -> None:
    """Persiste el fingerprint asignado a una cuenta."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE accounts SET fingerprint_json=? WHERE name=?",
            (fingerprint_json, account_name),
        )


def update_account(name: str, email: str, groups: list[str]) -> bool:
    """Actualiza email y grupos de una cuenta. Retorna True si existía."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE accounts SET email=?, groups=? WHERE name=? AND is_active=1",
            (email, json.dumps(groups), name),
        )
        return cur.rowcount > 0


def rename_account(old_name: str, new_name: str, email: str, groups: list[str]) -> bool:
    """
    Renombra una cuenta cambiando su PK. Copia timestamps, elimina la vieja.
    Retorna True si old_name existía.
    """
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT created_at, last_login_at, last_published_at FROM accounts WHERE name=? AND is_active=1",
            (old_name,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            """INSERT INTO accounts (name, email, groups, created_at, last_login_at, last_published_at, is_active)
               VALUES (?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(name) DO UPDATE SET
                   email=excluded.email, groups=excluded.groups, is_active=1""",
            (new_name, email, json.dumps(groups), row["created_at"],
             row["last_login_at"], row["last_published_at"]),
        )
        conn.execute("UPDATE accounts SET is_active=0 WHERE name=?", (old_name,))
        # Actualizar historial de logins para que refleje el nuevo nombre
        conn.execute(
            "UPDATE login_events SET account_name=? WHERE account_name=?",
            (new_name, old_name),
        )
        return True


def delete_account(name: str) -> bool:
    """Soft-delete: marca is_active=0. Retorna True si existía."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE accounts SET is_active=0 WHERE name=? AND is_active=1",
            (name,),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Login events
# ---------------------------------------------------------------------------
def record_login(account_name: str, success: bool) -> None:
    """Registra un intento de login y actualiza last_login_at si fue exitoso."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO login_events (account_name, logged_in_at, success) VALUES (?,?,?)",
            (account_name, now, int(success)),
        )
        if success:
            conn.execute(
                "UPDATE accounts SET last_login_at=? WHERE name=?",
                (now, account_name),
            )


def get_recent_logins(limit: int = 50) -> list[dict]:
    """Retorna los últimos N eventos de login."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT account_name, logged_in_at, success
               FROM login_events
               ORDER BY logged_in_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Gemini usage — cuota diaria por cuenta
# ---------------------------------------------------------------------------
def record_gemini_use(
    account_name: str,
    group_id: str | None = None,
    success: bool = True,
) -> None:
    """Inserta un evento de uso de Gemini para esta cuenta."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO gemini_usage (account_name, used_at, group_id, success)
               VALUES (?,?,?,?)""",
            (account_name, now, group_id, int(success)),
        )


def count_gemini_uses_today(account_name: str) -> int:
    """Cuenta usos exitosos del día actual (zona local) para esta cuenta."""
    with _lock, _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM gemini_usage
               WHERE account_name=?
                 AND success=1
                 AND date(used_at)=date('now','localtime')""",
            (account_name,),
        ).fetchone()
        return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Group tags
# ---------------------------------------------------------------------------
def get_group_tag(group_id: str) -> str:
    """Retorna el tag del grupo, o 'generico' si no tiene uno asignado."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT tag FROM group_tags WHERE group_id=?", (group_id,)
        ).fetchone()
        return row["tag"] if row else "generico"


def set_group_tag(group_id: str, tag: str) -> None:
    """Asigna o actualiza el tag de un grupo."""
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO group_tags (group_id, tag) VALUES (?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET tag = excluded.tag",
            (group_id, tag),
        )


def list_group_tags() -> list[dict]:
    """Lista todos los grupos con tags asignados."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT group_id, tag FROM group_tags ORDER BY tag ASC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Creación de jobs
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
    Almacena group_tag (snapshot) y actualiza last_published_at.
    """
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=? WHERE id=?",
            (now, job_id),
        )
        for account_name, group_results in results.items():
            for group_id, success in group_results.items():
                row = conn.execute(
                    "SELECT tag FROM group_tags WHERE group_id=?", (group_id,)
                ).fetchone()
                tag = row["tag"] if row else "generico"

                conn.execute(
                    """INSERT INTO job_results
                       (job_id, account_name, group_id, group_tag, success)
                       VALUES (?,?,?,?,?)""",
                    (job_id, account_name, group_id, tag, int(success)),
                )
                conn.execute(
                    "UPDATE accounts SET last_published_at=? WHERE name=?",
                    (now, account_name),
                )


def mark_failed(job_id: str, error_msg: str = "") -> None:
    with _lock, _connect() as conn:
        # No sobreescribir si ya terminó correctamente
        conn.execute(
            "UPDATE jobs SET status='failed', finished_at=? WHERE id=? AND status NOT IN ('done','cancelled')",
            (datetime.now().isoformat(), job_id),
        )
        conn.execute(
            """INSERT INTO job_results (job_id, account_name, group_id, group_tag, success, error_msg)
               VALUES (?, '__system__', '__error__', 'generico', 0, ?)""",
            (job_id, error_msg),
        )


def cancel_job(job_id: str) -> bool:
    """Cancela un job en estado pending. Retorna True si existía."""
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
def get_recent_jobs(limit: int = 50) -> list[dict]:
    """Retorna los N jobs mas recientes con sus resultados resumidos."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, type, status, accounts, scheduled_for,
                      created_at, started_at, finished_at
               FROM jobs
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

        jobs = []
        for r in rows:
            # Contar éxitos/fallos del job
            results = conn.execute(
                """SELECT success, error_msg, account_name, group_id
                   FROM job_results WHERE job_id=?""",
                (r["id"],),
            ).fetchall()

            succeeded = sum(1 for x in results if x["success"] and x["account_name"] != "__system__")
            failed    = sum(1 for x in results if not x["success"] and x["account_name"] != "__system__")
            errors    = [x["error_msg"] for x in results
                         if x["account_name"] == "__system__" and x["error_msg"]]

            jobs.append({
                "id":            r["id"],
                "type":          r["type"],
                "status":        r["status"],
                "accounts":      json.loads(r["accounts"]) if r["accounts"] else None,
                "scheduled_for": r["scheduled_for"],
                "created_at":    r["created_at"],
                "started_at":    r["started_at"],
                "finished_at":   r["finished_at"],
                "groups_ok":     succeeded,
                "groups_fail":   failed,
                "errors":        errors,
            })
        return jobs


# ---------------------------------------------------------------------------
# Cookies — asociadas al email, sobreviven renombres de cuenta
# ---------------------------------------------------------------------------
def save_cookies(email: str, cookies: list) -> None:
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO account_cookies (email, cookies, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                   cookies    = excluded.cookies,
                   updated_at = excluded.updated_at""",
            (email, json.dumps(cookies), now),
        )


def load_cookies(email: str) -> list | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT cookies FROM account_cookies WHERE email=?", (email,)
        ).fetchone()
    return json.loads(row["cookies"]) if row else None


def delete_cookies(email: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM account_cookies WHERE email=?", (email,))


# ---------------------------------------------------------------------------
# Jobs scheduled
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
