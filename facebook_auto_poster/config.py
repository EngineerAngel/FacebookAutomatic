"""
config.py — Global configuration and account loading.

All credentials are loaded from .env via python-dotenv.
Accounts are read from the DB (jobs.db) first; .env is used as fallback
on the first run before the DB is populated.
"""

import json
import logging
import os
import random
import sys
from dataclasses import dataclass, field, replace as _dc_replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Load .env — fail fast if missing
# ---------------------------------------------------------------------------
ENV_PATH = Path(__file__).resolve().parent / ".env"

if not ENV_PATH.exists():
    print(
        "[FATAL] .env file not found. Copy .env.example to .env and fill in "
        "your credentials before running."
    )
    sys.exit(1)

load_dotenv(ENV_PATH)


# ---------------------------------------------------------------------------
# Global CONFIG dict
# ---------------------------------------------------------------------------
CONFIG: dict = {
    "max_groups_per_session": 5,
    "wait_between_groups_min": 30,
    "wait_between_groups_max": 60,
    "wait_after_login_min": 5,
    "wait_after_login_max": 10,
    "wait_between_accounts_min": 60,
    "wait_between_accounts_max": 120,
    "browser_headless": False,
    "browser_window_size": (1280, 720),
    "browser_window_position": (0, 0),  # Para sincronizar Emunium con la ventana real
    "emunium_enabled": True,              # False = solo Patchright (sin mouse/keyboard OS-level)
    "implicit_wait": 10,
    "max_retries": 3,
    "text_variation_mode": "gemini",  # "gemini" | "zero_width" | "off"
    "structured_logging": os.getenv("STRUCTURED_LOGGING", "0").strip() == "1",
    # Fase 3.2 — FastAPI montado en /v2 (default OFF; activar con USE_FASTAPI=1)
    "use_fastapi": os.getenv("USE_FASTAPI", "0").strip() == "1",

    "execution_mode": os.getenv("EXECUTION_MODE", "sequential").strip().lower(),
    "api_port": int(os.getenv("API_PORT", "5000")),
    # Fase 3.3b — Prometheus metrics (default OFF; activar con METRICS_ENABLED=1)
    "metrics_enabled": os.getenv("METRICS_ENABLED", "0").strip() == "1",
    # Fase 3.4 — Adaptive DOM repair (default OFF; activar con ADAPTIVE_SELECTORS=1)
    "adaptive_selectors_enabled": os.getenv("ADAPTIVE_SELECTORS", "0").strip() == "1",
    # Fase 3.7 — Separar API y worker en procesos distintos (default OFF)
    "split_processes": os.getenv("SPLIT_PROCESSES", "0").strip() == "1",
    "worker_poll_interval": int(os.getenv("WORKER_POLL_INTERVAL", "5")),
    # Pool de workers (Fase 2.3): cuántos jobs (cada uno con potencialmente
    # varias cuentas) corren en paralelo. Más de 2-3 Chromes simultáneos
    # desde la misma IP/host aumenta detección.
    "max_concurrent_workers": int(os.getenv("MAX_CONCURRENT_WORKERS", "2")),
    # Rate limit por cuenta (protege contra ráfagas que disparan soft-bans)
    "max_posts_per_account_per_hour": 3,
    "max_posts_per_account_per_day": 15,
    # Idle aleatorio entre publicaciones (simula distracción humana)
    "idle_probability": 0.20,
    "idle_min_seconds": 5,
    "idle_max_seconds": 10,
    # Refresco periódico de la sesión cada N publicaciones exitosas
    "refresh_every_n_posts": 10,
    "refresh_pause_min": 60,
    "refresh_pause_max": 120,
    # ---- Calentamiento humano antes de publicar ------------------------
    "human_browsing_enabled": True,
    "warmup_probability": 0.60,                 # % de grupos con warmup
    "warmup_scrolls_min": 2,
    "warmup_scrolls_max": 5,
    "warmup_hover_probability": 0.5,            # hover sobre 1 publicación
    "warmup_open_comments_probability": 0.3,    # abrir 1 hilo de comentarios
    "warmup_duration_min": 8,                   # segundos mínimos en el feed
    "warmup_duration_max": 25,
    # ---- Gemini commenter (publicaciones ajenas durante warmup) --------
    "gemini_comment_enabled": True,
    "gemini_comment_probability": 0.20,         # % de warmups que comentan
    "gemini_comment_max_per_session": 2,        # tope por cuenta por run
    "gemini_comment_lang": "es-MX",
    "gemini_timeout": 15,                       # timeout blando entre reintentos (s)
    # Cuota diaria por cuenta — target aleatorio determinista (account, día)
    "gemini_daily_min": 1,
    "gemini_daily_max": 3,
    # Tras un timeout duro (60s), no llamar a Gemini durante X segundos.
    # Si la respuesta tardía llega antes, el cooldown se levanta automáticamente.
    "gemini_degraded_cooldown_s": 300,
    # Claves primarias separadas por coma. Rota automáticamente si agota quota.
    # Formato: "clave1,clave2,clave3" (sin espacios).
    "gemini_api_keys": [k for k in os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", "")).split(",") if k.strip()],
    "gemini_model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
}


# ---------------------------------------------------------------------------
# Validación de active_hours
# ---------------------------------------------------------------------------
def _validate_active_hours(hours: tuple[int, int], account_name: str = "") -> tuple[int, int]:
    """Valida rango de horas. Si es inválido, retorna (7, 23) con WARNING."""
    start, end = hours
    logger = logging.getLogger(__name__)

    if not (0 <= start <= 23 and 0 <= end <= 23):
        logger.warning(
            "Cuenta '%s' tiene active_hours inválido %s (fuera de [0-23]) — fallback a (7, 23)",
            account_name, hours,
        )
        return (7, 23)

    if start > end:
        logger.warning(
            "Cuenta '%s' tiene active_hours inválido %s (start > end) — fallback a (7, 23)",
            account_name, hours,
        )
        return (7, 23)

    return hours


# ---------------------------------------------------------------------------
# AccountConfig dataclass
# ---------------------------------------------------------------------------
@dataclass
class AccountConfig:
    name: str
    email: str
    password: str
    groups: list[str] = field(default_factory=list)
    timezone: str = "UTC"
    active_hours: tuple[int, int] = (7, 23)
    fingerprint: dict = field(default_factory=dict)
    log_file: str = ""
    screenshots_dir: str = ""

    def __post_init__(self) -> None:
        base = Path(__file__).resolve().parent
        if not self.log_file:
            self.log_file = str(base / "logs" / f"{self.name}.log")
        if not self.screenshots_dir:
            self.screenshots_dir = str(base / "screenshots" / self.name)
        self.active_hours = _validate_active_hours(self.active_hours, self.name)


def load_fingerprints() -> list[dict]:
    """Carga el catálogo de fingerprints desde fingerprints.json."""
    path = Path(__file__).resolve().parent / "fingerprints.json"
    return json.loads(path.read_text(encoding="utf-8"))


def pick_fingerprint(taken_ids: list[str]) -> dict:
    """Elige un fingerprint no asignado aún. Si todos están tomados, reutiliza al azar."""
    catalog = load_fingerprints()
    available = [fp for fp in catalog if fp["id"] not in taken_ids]
    return random.choice(available if available else catalog)


def is_account_hour_allowed(account: AccountConfig) -> bool:
    """Verifica si la hora local de la cuenta está dentro de su ventana de publicación."""
    local_hour = datetime.now(ZoneInfo(account.timezone)).hour
    start, end = account.active_hours
    return start <= local_hour <= end


# ---------------------------------------------------------------------------
# load_accounts() — DB first, .env as fallback
# ---------------------------------------------------------------------------
def load_accounts() -> list[AccountConfig]:
    """
    Carga las cuentas desde la BD si existen (gestionadas via UI admin).
    Usa .env como fallback en el primer arranque (antes de que haya BD).
    """
    global_password = os.getenv("FB_PASSWORD", "").strip()
    if not global_password:
        raise ValueError(
            "FB_PASSWORD is not set in .env. "
            "Provide the shared Facebook password for all accounts."
        )

    # --- Intentar desde BD --------------------------------------------------
    try:
        import job_store
        rows = job_store.list_accounts_full()
        if rows:
            now_iso = datetime.now().isoformat()
            accounts = []
            taken_ids: list[str] = []
            for r in rows:
                groups = json.loads(r["groups"]) if r.get("groups") else []
                if not groups:
                    import logging as _lg
                    _lg.getLogger(__name__).warning(
                        "[config] Cuenta '%s' omitida — no tiene grupos configurados", r["name"]
                    )
                    continue
                cooldown = r.get("ban_cooldown_until")
                if cooldown and cooldown > now_iso:
                    continue  # Cuenta en cooldown post-ban — saltar
                active_hours_raw = r.get("active_hours") or "[7, 23]"
                active_hours = tuple(json.loads(active_hours_raw))

                # Fingerprint per-account (Fase 1.3)
                fp_raw = r.get("fingerprint_json")
                if fp_raw:
                    fingerprint = json.loads(fp_raw)
                else:
                    fingerprint = pick_fingerprint(taken_ids)
                    job_store.save_fingerprint(r["name"], json.dumps(fingerprint))
                taken_ids.append(fingerprint.get("id", ""))

                # Contraseña individual cifrada (Fase 1.2)
                password = global_password
                password_enc = r.get("password_enc")
                if password_enc:
                    try:
                        from crypto import decrypt_password
                        password = decrypt_password(password_enc)
                    except Exception:
                        pass  # fallback a contraseña global si falla descifrado

                accounts.append(
                    AccountConfig(
                        name=r["name"],
                        email=r["email"],
                        password=password,
                        groups=groups,
                        timezone=r.get("timezone") or "UTC",
                        active_hours=active_hours,
                        fingerprint=fingerprint,
                    )
                )
            if accounts:
                return accounts
    except (FileNotFoundError, ImportError):
        pass  # BD genuinamente no existe aún — fallback a .env esperado
    except Exception as _db_exc:
        # [FIX P0-2] Error inesperado (corrupción, disco lleno, etc.)
        # Logea visible antes de hacer fallback — no silencia problemas reales
        import logging as _lg
        _lg.getLogger(__name__).error(
            "[config] Error inesperado leyendo DB — fallback a .env. "
            "Verificar integridad de jobs.db. Error: %s", _db_exc
        )

    # --- Fallback: leer desde .env ------------------------------------------
    return _load_accounts_from_env(global_password)


def _load_accounts_from_env(global_password: str) -> list[AccountConfig]:
    """Lee cuentas desde variables de entorno (comportamiento original)."""
    raw_names = os.getenv("ACCOUNT_NAMES", "").strip()
    if not raw_names:
        raise ValueError(
            "ACCOUNT_NAMES is not set in .env. "
            "Provide a comma-separated list of account names."
        )

    names = [n.strip() for n in raw_names.split(",") if n.strip()]
    if not names:
        raise ValueError("ACCOUNT_NAMES is empty after parsing.")

    accounts: list[AccountConfig] = []

    for name in names:
        prefix = name.upper()

        # Acepta EMAIL o PHONE como identificador de login — ambos se escriben
        # en el campo email del formulario de Facebook, que admite los dos.
        email = (
            os.getenv(f"{prefix}_EMAIL", "").strip()
            or os.getenv(f"{prefix}_PHONE", "").strip()
        )
        groups_raw = os.getenv(f"{prefix}_GROUPS", "").strip()

        # Email/phone es obligatorio; grupos es opcional (puede agregarse después en el admin)
        if not email:
            raise ValueError(
                f"Missing .env key for account '{name}': {prefix}_EMAIL (o {prefix}_PHONE). "
                f"Check your .env file."
            )

        groups = [g.strip() for g in groups_raw.split(",") if g.strip()] if groups_raw else []

        accounts.append(
            AccountConfig(
                name=name.lower(),
                email=email,
                password=global_password,
                groups=groups
            )
        )

    return accounts


# ---------------------------------------------------------------------------
# Filtro de grupos por publicación
# ---------------------------------------------------------------------------

def apply_group_filter(
    accounts: list[AccountConfig],
    group_ids: dict[str, list[str]] | None,
) -> list[AccountConfig]:
    """Restringe los grupos de cada cuenta al subconjunto seleccionado.

    group_ids: {"account_name": ["gid1", "gid2"], ...} o None
    - None              → sin filtro (backward compat — llamadas sin group_ids)
    - dict vacío {}     → ninguna cuenta seleccionada, devuelve []
    - Cuenta ausente    → esa cuenta se omite (no publica)
    - Lista vacía []    → esa cuenta se omite
    - Intersección vacía→ esa cuenta se omite (selected no coincide con acc.groups)
    """
    if group_ids is None:
        return accounts
    result = []
    for acc in accounts:
        selected = group_ids.get(acc.name)
        if not selected:
            continue
        allowed = set(acc.groups)
        filtered = [g for g in selected if g in allowed]
        if filtered:
            result.append(_dc_replace(acc, groups=filtered))
    return result
