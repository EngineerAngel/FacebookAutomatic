"""
job_store.py — SQLite como fuente única de verdad para todos los jobs y cuentas.

Tablas:
  accounts          — cuentas activas (nombre, email, grupos, historial)
  group_tags        — etiquetas amigables por grupo (default 'generico')
  login_events      — historial de intentos de login
  jobs              — cola de trabajos (inmediatos y agendados)
  job_results       — resultados por cuenta/grupo
  templates         — plantillas de publicación reutilizables

Base de datos: jobs.db (local, gitignored, nunca expuesto por API)
"""

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "jobs.db"
_lock = threading.Lock()


def _decode_image_paths(raw: str | None) -> list[str]:
    """Decodifica el campo image_path (TEXT) tolerando ambos formatos:
    JSON array (nuevo) o string crudo (legacy). None/'' → lista vacía."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return [raw]
    if isinstance(value, list):
        return [str(p) for p in value if p]
    if isinstance(value, str):
        return [value] if value else []
    return []


def _encode_image_paths(paths: list[str] | None) -> str | None:
    """Serializa una lista de rutas a JSON para persistir en image_path (TEXT).
    Lista vacía o None → None (NULL en DB)."""
    if not paths:
        return None
    return json.dumps(list(paths))


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

            CREATE TABLE IF NOT EXISTS account_bans (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name    TEXT NOT NULL,
                detected_at     TEXT NOT NULL,
                context         TEXT,
                screenshot_path TEXT,
                reviewed        INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS rate_limit_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ip       TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                ts       REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS text_variations (
                cache_key     TEXT PRIMARY KEY,
                original_hash TEXT NOT NULL,
                variated      TEXT NOT NULL,
                created_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discovery_runs (
                id            TEXT PRIMARY KEY,
                account_name  TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'running',
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                groups_found  INTEGER DEFAULT 0,
                error         TEXT
            );

            CREATE TABLE IF NOT EXISTS discovered_groups (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name     TEXT NOT NULL,
                group_id         TEXT NOT NULL,
                group_name       TEXT NOT NULL,
                discovered_at    TEXT NOT NULL,
                added_to_posting INTEGER NOT NULL DEFAULT 0,
                last_seen        TEXT NOT NULL,
                UNIQUE(account_name, group_id)
            );

            CREATE TABLE IF NOT EXISTS templates (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                text       TEXT NOT NULL,
                url        TEXT NOT NULL,
                image_path TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_scheduled ON jobs(scheduled_for)
                WHERE type = 'scheduled';
            CREATE INDEX IF NOT EXISTS idx_login_events_account ON login_events(account_name);
            CREATE INDEX IF NOT EXISTS idx_gemini_usage_account_date
                ON gemini_usage(account_name, used_at);
            CREATE INDEX IF NOT EXISTS idx_account_bans_account
                ON account_bans(account_name, detected_at);
            CREATE INDEX IF NOT EXISTS idx_templates_created ON templates(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ratelimit_lookup
                ON rate_limit_events(ip, endpoint, ts);
        """)

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS proxy_nodes (
                id               TEXT PRIMARY KEY,
                label            TEXT NOT NULL,
                server           TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'online',
                last_checked     TEXT,
                last_seen_ip     TEXT,
                check_fail_count INTEGER NOT NULL DEFAULT 0,
                notes            TEXT
            );

            CREATE TABLE IF NOT EXISTS account_proxy_assignment (
                account_name   TEXT PRIMARY KEY REFERENCES accounts(name),
                primary_node   TEXT NOT NULL REFERENCES proxy_nodes(id),
                secondary_node TEXT REFERENCES proxy_nodes(id),
                assigned_at    TEXT NOT NULL
            );
        """)

        # Migraciones seguras — no fallan si la columna ya existe
        for stmt in [
            "ALTER TABLE job_results ADD COLUMN group_tag TEXT NOT NULL DEFAULT 'generico'",
            "ALTER TABLE accounts ADD COLUMN groups TEXT",
            "ALTER TABLE accounts ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE accounts ADD COLUMN timezone TEXT NOT NULL DEFAULT 'America/Mexico_City'",
            "ALTER TABLE accounts ADD COLUMN active_hours TEXT NOT NULL DEFAULT '[7, 23]'",
            "ALTER TABLE accounts ADD COLUMN ban_cooldown_until TEXT",
            "ALTER TABLE accounts ADD COLUMN fingerprint_json TEXT",
            "ALTER TABLE accounts ADD COLUMN password_enc TEXT",
            "ALTER TABLE account_proxy_assignment ADD COLUMN last_used_at TEXT",
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
            """SELECT name, email, groups, timezone, active_hours,
                      ban_cooldown_until, fingerprint_json, password_enc,
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
                   email            = excluded.email,
                   groups           = excluded.groups,
                   fingerprint_json = COALESCE(excluded.fingerprint_json, accounts.fingerprint_json),
                   is_active        = 1""",
            (name, email, json.dumps(groups), fingerprint_json, now),
        )


def save_fingerprint(account_name: str, fingerprint_json: str) -> None:
    """Persiste el fingerprint asignado a una cuenta."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE accounts SET fingerprint_json=? WHERE name=?",
            (fingerprint_json, account_name),
        )


def set_account_password(account_name: str, password_enc: str) -> None:
    """Guarda la contraseña cifrada con Fernet para una cuenta."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE accounts SET password_enc=? WHERE name=?",
            (password_enc, account_name),
        )


def clear_account_password(account_name: str) -> None:
    """Elimina la contraseña individual — la cuenta usará FB_PASSWORD global."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE accounts SET password_enc=NULL WHERE name=?",
            (account_name,),
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
    Renombra una cuenta actualizando su PK en una sola transacción.
    Usa UPDATE en lugar de INSERT+DELETE para preservar todos los campos
    (incluyendo password_enc, fingerprint_json, ban_cooldown_until, etc.).
    Retorna True si old_name existía.
    """
    with _lock, _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM accounts WHERE name=? AND is_active=1", (old_name,)
        ).fetchone()
        if not exists:
            return False
        conn.execute(
            "UPDATE accounts SET name=?, email=?, groups=? WHERE name=?",
            (new_name, email, json.dumps(groups), old_name),
        )
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
# Bans — detección de soft-bans y cooldown
# ---------------------------------------------------------------------------
def record_ban(
    account_name: str,
    context: str,
    screenshot_path: str | None = None,
) -> int:
    """Registra un evento de ban. Retorna el id generado."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO account_bans
               (account_name, detected_at, context, screenshot_path)
               VALUES (?, ?, ?, ?)""",
            (account_name, now, context, screenshot_path),
        )
        return int(cur.lastrowid)


def set_account_ban_cooldown(account_name: str, hours: int) -> None:
    """Marca la cuenta en cooldown hasta now + hours."""
    from datetime import timedelta
    until = (datetime.now() + timedelta(hours=hours)).isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE accounts SET ban_cooldown_until=? WHERE name=?",
            (until, account_name),
        )


def clear_ban(account_name: str) -> bool:
    """Levanta el cooldown y marca bans como reviewed. Retorna True si existía."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE accounts SET ban_cooldown_until=NULL WHERE name=?",
            (account_name,),
        )
        conn.execute(
            "UPDATE account_bans SET reviewed=1 WHERE account_name=? AND reviewed=0",
            (account_name,),
        )
        return cur.rowcount > 0


def list_active_bans() -> list[dict]:
    """Lista cuentas actualmente en cooldown con tiempo restante."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT name, ban_cooldown_until FROM accounts
               WHERE ban_cooldown_until IS NOT NULL
                 AND ban_cooldown_until > ?
               ORDER BY ban_cooldown_until ASC""",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_recent_bans(limit: int = 50) -> list[dict]:
    """Lista los últimos N eventos de ban (incluye ya reviewed)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, account_name, detected_at, context,
                      screenshot_path, reviewed
               FROM account_bans
               ORDER BY detected_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def is_account_in_cooldown(account_name: str) -> bool:
    """True si la cuenta tiene ban_cooldown_until > now."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM accounts
               WHERE name=? AND ban_cooldown_until IS NOT NULL
                 AND ban_cooldown_until > ?""",
            (account_name, now),
        ).fetchone()
        return row is not None


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
    image_paths: list[str] | None,
    callback_url: str | None,
    job_type: str = "immediate",
    scheduled_for: datetime | None = None,
) -> str:
    """Inserta un nuevo job y retorna su id.

    image_paths se serializa como JSON en la columna image_path (TEXT).
    """
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
                _encode_image_paths(image_paths),
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


def mark_running_as_interrupted() -> int:
    """Marca todos los jobs en estado 'running' como 'interrupted'.

    Usado en el arranque del servidor (crash/shutdown previo dejó jobs huérfanos)
    y durante graceful shutdown. Retorna el número de filas afectadas.
    """
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='interrupted', finished_at=? WHERE status='running'",
            (now,),
        )
        return int(cur.rowcount)


# ---------------------------------------------------------------------------
# Métricas para healthcheck
# ---------------------------------------------------------------------------
def count_pending_jobs() -> int:
    """Número de jobs en cola (pending o running)."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','running')"
        ).fetchone()
        return int(row["n"]) if row else 0


def count_active_accounts() -> int:
    """Cuentas activas que NO están en cooldown de ban."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM accounts
               WHERE is_active=1
                 AND (ban_cooldown_until IS NULL OR ban_cooldown_until <= ?)""",
            (now,),
        ).fetchone()
        return int(row["n"]) if row else 0


def count_jobs_by_status() -> dict[str, int]:
    """Distribución de jobs por estado (para healthcheck detallado)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}


def account_recent_post_count(account_name: str, window_minutes: int = 60) -> int:
    """Cuenta publicaciones exitosas de una cuenta en los últimos N minutos.

    Usado por el rate limiter por cuenta (Fase 2.3) — previene ráfagas que
    disparan soft-bans.
    """
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(minutes=window_minutes)).isoformat()
    with _lock, _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n
               FROM job_results jr JOIN jobs j ON jr.job_id = j.id
               WHERE jr.account_name = ?
                 AND jr.success = 1
                 AND jr.account_name != '__system__'
                 AND (j.finished_at IS NOT NULL AND j.finished_at >= ?)""",
            (account_name, cutoff),
        ).fetchone()
        return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Rate limiter persistente (SQLite)
# ---------------------------------------------------------------------------
def is_rate_limited(ip: str, endpoint: str, limit: int, window_s: int) -> bool:
    """Retorna True si ip+endpoint excedió `limit` hits en los últimos window_s.

    Registra el hit actual si no está limitado, en operación atómica bajo _lock.
    Sobrevive a reinicios del servidor — los hits viejos se purgan en la misma
    llamada.
    """
    now = time.time()
    cutoff = now - window_s
    with _lock, _connect() as conn:
        # Purgar eventos viejos para este (ip, endpoint)
        conn.execute(
            "DELETE FROM rate_limit_events WHERE ip=? AND endpoint=? AND ts < ?",
            (ip, endpoint, cutoff),
        )
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM rate_limit_events WHERE ip=? AND endpoint=? AND ts >= ?",
            (ip, endpoint, cutoff),
        ).fetchone()
        count = int(row["n"]) if row else 0
        if count >= limit:
            return True
        conn.execute(
            "INSERT INTO rate_limit_events (ip, endpoint, ts) VALUES (?, ?, ?)",
            (ip, endpoint, now),
        )
        return False


def purge_old_rate_limit_events(days: int = 7) -> int:
    """Elimina eventos más viejos que N días. Retorna filas eliminadas."""
    cutoff = time.time() - days * 86400
    with _lock, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM rate_limit_events WHERE ts < ?",
            (cutoff,),
        )
        return int(cur.rowcount)


# ---------------------------------------------------------------------------
# Text variations cache (Fase 2.2)
# ---------------------------------------------------------------------------
def get_text_variation(cache_key: str, ttl_seconds: int) -> str | None:
    """Retorna el parafraseo cacheado si no ha expirado, None si miss/expirado."""
    cutoff = time.time() - ttl_seconds
    with _lock, _connect() as conn:
        row = conn.execute(
            """SELECT variated FROM text_variations
               WHERE cache_key=? AND created_at >= ?""",
            (cache_key, cutoff),
        ).fetchone()
        return row["variated"] if row else None


def save_text_variation(cache_key: str, original_hash: str, variated: str) -> None:
    """Guarda (o reemplaza) una variación cacheada."""
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO text_variations (cache_key, original_hash, variated, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                   original_hash = excluded.original_hash,
                   variated      = excluded.variated,
                   created_at    = excluded.created_at""",
            (cache_key, original_hash, variated, time.time()),
        )


def purge_old_text_variations(days: int = 7) -> int:
    """Elimina variaciones más viejas que N días. Retorna filas eliminadas."""
    cutoff = time.time() - days * 86400
    with _lock, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM text_variations WHERE created_at < ?",
            (cutoff,),
        )
        return int(cur.rowcount)


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
                "image_paths": _decode_image_paths(row["image_path"]),
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


def get_job(job_id: str) -> dict | None:
    """Retorna un job por ID con sus resultados, o None si no existe."""
    jobs = get_recent_jobs(limit=1)  # no sirve por ID, query directo
    with _lock, _connect() as conn:
        r = conn.execute(
            """SELECT id, type, status, accounts, scheduled_for,
                      created_at, started_at, finished_at
               FROM jobs WHERE id=?""",
            (job_id,),
        ).fetchone()
        if not r:
            return None
        results = conn.execute(
            """SELECT success, error_msg, account_name, group_id, group_tag
               FROM job_results WHERE job_id=? ORDER BY account_name, group_tag""",
            (job_id,),
        ).fetchall()
        succeeded = sum(1 for x in results if x["success"] and x["account_name"] != "__system__")
        failed    = sum(1 for x in results if not x["success"] and x["account_name"] != "__system__")
        errors    = [x["error_msg"] for x in results
                     if x["account_name"] == "__system__" and x["error_msg"]]
        group_results = [
            {
                "account":   x["account_name"],
                "group_id":  x["group_id"],
                "group_tag": x["group_tag"],
                "success":   bool(x["success"]),
            }
            for x in results if x["account_name"] != "__system__"
        ]
        return {
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
            "group_results": group_results,
        }


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
                "image_paths": _decode_image_paths(r["image_path"]),
                "callback_url": r["callback_url"],
                "scheduled_for": r["scheduled_for"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Descubrimiento de grupos (Fase 2.10)
# ---------------------------------------------------------------------------
def create_discovery_run(run_id: str, account_name: str) -> None:
    """Inicia un nuevo run de descubrimiento. Estado inicial: 'running'."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO discovery_runs (id, account_name, status, started_at)
               VALUES (?, ?, ?, ?)""",
            (run_id, account_name, "running", now),
        )


def finish_discovery_run(run_id: str, groups_found: int) -> None:
    """Marca un run como completado exitosamente."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """UPDATE discovery_runs
               SET status='done', finished_at=?, groups_found=?
               WHERE id=?""",
            (now, groups_found, run_id),
        )


def fail_discovery_run(run_id: str, error: str) -> None:
    """Marca un run como fallido con mensaje de error."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """UPDATE discovery_runs
               SET status='failed', finished_at=?, error=?
               WHERE id=?""",
            (now, error[:500], run_id),
        )


def get_discovery_run(run_id: str) -> dict | None:
    """Obtiene el estado de un run de descubrimiento."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM discovery_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        return dict(row) if row else None


def upsert_discovered_group(
    account_name: str,
    group_id: str,
    group_name: str,
    discovered_at: str,
) -> None:
    """Guarda o actualiza un grupo descubierto. Si existe, actualiza last_seen."""
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO discovered_groups
               (account_name, group_id, group_name, discovered_at, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(account_name, group_id)
               DO UPDATE SET
                   last_seen = excluded.last_seen,
                   group_name = excluded.group_name""",
            (account_name, group_id, group_name, discovered_at, discovered_at),
        )


def list_discovered_groups(account_name: str) -> list[dict]:
    """Lista grupos descubiertos de una cuenta, pendientes primero."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT group_id, group_name, discovered_at, added_to_posting, last_seen
               FROM discovered_groups
               WHERE account_name=?
               ORDER BY added_to_posting ASC, discovered_at DESC""",
            (account_name,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_group_added_to_posting(account_name: str, group_id: str) -> bool:
    """Marca un grupo descubierto como añadido a la lista de publicación."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            """UPDATE discovered_groups
               SET added_to_posting=1
               WHERE account_name=? AND group_id=?""",
            (account_name, group_id),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Proxy nodes — CRUD
# ---------------------------------------------------------------------------

def upsert_proxy_node(
    node_id: str,
    label: str,
    server: str,
    notes: str = "",
) -> None:
    """Crea o actualiza un nodo proxy. No resetea status/ip si ya existe."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO proxy_nodes (id, label, server, notes, last_checked)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   label  = excluded.label,
                   server = excluded.server,
                   notes  = excluded.notes""",
            (node_id, label, server, notes, now),
        )


def list_proxy_nodes() -> list[dict]:
    """Lista todos los nodos proxy."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, label, server, status, last_checked,
                      last_seen_ip, check_fail_count, notes
               FROM proxy_nodes ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_proxy_node(node_id: str) -> dict | None:
    """Devuelve un nodo proxy por ID, o None si no existe."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM proxy_nodes WHERE id=?", (node_id,)
        ).fetchone()
        return dict(row) if row else None


def update_proxy_node_status(
    node_id: str,
    *,
    status: str | None = None,
    last_ip: str | None = None,
    reset_fails: bool = False,
    fail_count: int | None = None,
) -> None:
    """Actualiza status, IP pública y/o contadores de fallo de un nodo."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        parts = ["last_checked = ?"]
        params: list = [now]

        if status is not None:
            parts.append("status = ?")
            params.append(status)
        if last_ip is not None:
            parts.append("last_seen_ip = ?")
            params.append(last_ip)
        if reset_fails:
            parts.append("check_fail_count = 0")
        elif fail_count is not None:
            parts.append("check_fail_count = ?")
            params.append(fail_count)

        params.append(node_id)
        conn.execute(
            f"UPDATE proxy_nodes SET {', '.join(parts)} WHERE id=?",
            params,
        )


def delete_proxy_node(node_id: str) -> bool:
    """Elimina un nodo proxy. Retorna True si existía."""
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM proxy_nodes WHERE id=?", (node_id,))
        return cur.rowcount > 0


def get_any_online_proxy_node(exclude_nodes: list[str | None]) -> dict | None:
    """Retorna cualquier nodo 'online' que no esté en la lista de exclusión."""
    clean = [n for n in exclude_nodes if n]
    with _lock, _connect() as conn:
        if clean:
            placeholders = ",".join("?" * len(clean))
            row = conn.execute(
                f"SELECT * FROM proxy_nodes WHERE status='online' AND id NOT IN ({placeholders}) LIMIT 1",
                clean,
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM proxy_nodes WHERE status='online' LIMIT 1"
            ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Proxy assignments — asignación cuenta → nodo
# ---------------------------------------------------------------------------

def set_proxy_assignment(
    account_name: str,
    primary_node: str,
    secondary_node: str | None = None,
) -> None:
    """Asigna (o reasigna) los nodos proxy de una cuenta."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO account_proxy_assignment
                   (account_name, primary_node, secondary_node, assigned_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(account_name) DO UPDATE SET
                   primary_node   = excluded.primary_node,
                   secondary_node = excluded.secondary_node,
                   assigned_at    = excluded.assigned_at""",
            (account_name, primary_node, secondary_node, now),
        )


def get_proxy_assignment(account_name: str) -> dict | None:
    """Retorna la asignación de proxy de una cuenta, o None."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM account_proxy_assignment WHERE account_name=?",
            (account_name,),
        ).fetchone()
        return dict(row) if row else None


def delete_proxy_assignment(account_name: str) -> bool:
    """Elimina la asignación de proxy de una cuenta."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM account_proxy_assignment WHERE account_name=?",
            (account_name,),
        )
        return cur.rowcount > 0


def get_accounts_for_node(node_id: str) -> list[dict]:
    """Retorna las cuentas asignadas a un nodo (primary o secondary)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT a.name, a.groups
               FROM accounts a
               JOIN account_proxy_assignment p ON p.account_name = a.name
               WHERE (p.primary_node=? OR p.secondary_node=?) AND a.is_active=1""",
            (node_id, node_id),
        ).fetchall()
        return [dict(r) for r in rows]


def list_proxy_assignments() -> list[dict]:
    """Lista todas las asignaciones con info del nodo primario."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT p.account_name, p.primary_node, p.secondary_node,
                      p.assigned_at, p.last_used_at, n.label, n.status, n.last_seen_ip
               FROM account_proxy_assignment p
               LEFT JOIN proxy_nodes n ON n.id = p.primary_node
               ORDER BY p.account_name"""
        ).fetchall()
        return [dict(r) for r in rows]


def touch_proxy_assignment(account_name: str) -> None:
    """Actualiza last_used_at al momento actual (llamar cuando se usa el proxy)."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE account_proxy_assignment SET last_used_at=? WHERE account_name=?",
            (now, account_name),
        )


def count_accounts_for_node(node_id: str) -> int:
    """Número de cuentas con primary_node = node_id."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM account_proxy_assignment WHERE primary_node=?",
            (node_id,),
        ).fetchone()
        return row[0] if row else 0


def get_lru_account_for_node(node_id: str) -> dict | None:
    """
    Retorna la cuenta del nodo que lleva más tiempo sin usar el proxy.
    Ordena por last_used_at ASC (NULL primero = nunca usada).
    """
    with _lock, _connect() as conn:
        row = conn.execute(
            """SELECT account_name, last_used_at
               FROM account_proxy_assignment
               WHERE primary_node=?
               ORDER BY last_used_at ASC NULLS FIRST
               LIMIT 1""",
            (node_id,),
        ).fetchone()
        return dict(row) if row else None


def last_node_use(node_id: str, exclude_account: str) -> str | None:
    """
    Retorna el last_used_at más reciente de cualquier cuenta en este nodo,
    excluyendo la cuenta indicada. None si nunca se ha usado por otra cuenta.
    """
    with _lock, _connect() as conn:
        row = conn.execute(
            """SELECT MAX(last_used_at) as latest
               FROM account_proxy_assignment
               WHERE primary_node=?
                 AND account_name != ?
                 AND last_used_at IS NOT NULL""",
            (node_id, exclude_account),
        ).fetchone()
        return row["latest"] if row and row["latest"] else None


# ---------------------------------------------------------------------------
# Templates — plantillas de publicación reutilizables
# ---------------------------------------------------------------------------
def create_template(
    name: str,
    text: str,
    url: str,
    image_paths: list[str] | None = None,
) -> str:
    """
    Crea una nueva plantilla de publicación.
    Retorna el ID generado (UUID corto).
    """
    template_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO templates (id, name, text, url, image_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (template_id, name, text, url, _encode_image_paths(image_paths), now),
        )
    return template_id


def _row_to_template(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "text": row["text"],
        "url": row["url"],
        "image_paths": _decode_image_paths(row["image_path"]),
        "created_at": row["created_at"],
    }


def get_template(template_id: str) -> dict | None:
    """Obtiene una plantilla por ID."""
    with _lock, _connect() as conn:
        row = conn.execute(
            """SELECT id, name, text, url, image_path, created_at
               FROM templates WHERE id=?""",
            (template_id,),
        ).fetchone()
        return _row_to_template(row) if row else None


def list_templates() -> list[dict]:
    """Lista todas las plantillas ordenadas por fecha (más recientes primero)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, name, text, url, image_path, created_at
               FROM templates
               ORDER BY created_at DESC""",
        ).fetchall()
        return [_row_to_template(r) for r in rows]


def update_template(
    template_id: str,
    name: str | None = None,
    text: str | None = None,
    url: str | None = None,
    image_paths: list[str] | None = None,
) -> bool:
    """
    Actualiza una plantilla con los campos proporcionados (solo non-None).
    Retorna True si existía y fue actualizada.
    """
    parts = []
    params = []

    if name is not None:
        parts.append("name = ?")
        params.append(name)
    if text is not None:
        parts.append("text = ?")
        params.append(text)
    if url is not None:
        parts.append("url = ?")
        params.append(url)
    if image_paths is not None:
        parts.append("image_path = ?")
        params.append(_encode_image_paths(image_paths))

    if not parts:
        return False

    params.append(template_id)

    with _lock, _connect() as conn:
        cur = conn.execute(
            f"UPDATE templates SET {', '.join(parts)} WHERE id=?",
            params,
        )
        return cur.rowcount > 0


def delete_template(template_id: str) -> bool:
    """Elimina una plantilla. Retorna True si existía."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM templates WHERE id=?",
            (template_id,),
        )
        return cur.rowcount > 0
