"""
config.py — Global configuration and account loading.

All credentials are loaded from .env via python-dotenv.
Accounts are read from the DB (jobs.db) first; .env is used as fallback
on the first run before the DB is populated.
"""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
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
    "text_variation_mode": True,
    "post_hours_allowed": range(0, 24),  # TODO: revert to range(6, 23) after testing
    "execution_mode": os.getenv("EXECUTION_MODE", "sequential").strip().lower(),
    "api_port": int(os.getenv("API_PORT", "5000")),
    # Idle aleatorio entre publicaciones (simula distracción humana)
    "idle_probability": 0.20,
    "idle_min_seconds": 5,
    "idle_max_seconds": 10,
    # Refresco periódico de la sesión cada N publicaciones exitosas
    "refresh_every_n_posts": 10,
    "refresh_pause_min": 60,
    "refresh_pause_max": 120,
}


# ---------------------------------------------------------------------------
# AccountConfig dataclass
# ---------------------------------------------------------------------------
@dataclass
class AccountConfig:
    name: str
    email: str
    password: str
    groups: list[str] = field(default_factory=list)
    log_file: str = ""
    screenshots_dir: str = ""

    def __post_init__(self) -> None:
        base = Path(__file__).resolve().parent
        if not self.log_file:
            self.log_file = str(base / "logs" / f"{self.name}.log")
        if not self.screenshots_dir:
            self.screenshots_dir = str(base / "screenshots" / self.name)


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
            accounts = []
            for r in rows:
                groups = json.loads(r["groups"]) if r.get("groups") else []
                if not groups:
                    continue
                accounts.append(
                    AccountConfig(
                        name=r["name"],
                        email=r["email"],
                        password=global_password,
                        groups=groups,
                    )
                )
            if accounts:
                return accounts
    except Exception:
        pass  # BD no existe aún → fallback a .env

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

        email = os.getenv(f"{prefix}_EMAIL", "").strip()
        groups_raw = os.getenv(f"{prefix}_GROUPS", "").strip()

        missing: list[str] = []
        if not email:
            missing.append(f"{prefix}_EMAIL")
        if not groups_raw:
            missing.append(f"{prefix}_GROUPS")

        if missing:
            raise ValueError(
                f"Missing .env keys for account '{name}': {', '.join(missing)}. "
                f"Check your .env file."
            )

        groups = [g.strip() for g in groups_raw.split(",") if g.strip()]

        accounts.append(
            AccountConfig(
                name=name.lower(),
                email=email,
                password=global_password,
                groups=groups
            )
        )

    return accounts
