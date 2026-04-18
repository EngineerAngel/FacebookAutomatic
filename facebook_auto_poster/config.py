"""
config.py — Global configuration and account loading.

All credentials are loaded from .env via python-dotenv.
Never import or reference real credentials in this file.
"""

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
    "implicit_wait": 10,
    "max_retries": 3,
    "text_variation_mode": True,
    "post_hours_allowed": range(6, 23),
    "execution_mode": os.getenv("EXECUTION_MODE", "sequential").strip().lower(),
    "api_port": int(os.getenv("API_PORT", "5000")),
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
# load_accounts() — parse ACCOUNT_NAMES and build AccountConfig list
# ---------------------------------------------------------------------------
def load_accounts() -> list[AccountConfig]:
    """Read ACCOUNT_NAMES from .env and build a validated list of AccountConfig."""

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
        password = os.getenv(f"{prefix}_PASSWORD", "").strip()
        groups_raw = os.getenv(f"{prefix}_GROUPS", "").strip()

        missing: list[str] = []
        if not email:
            missing.append(f"{prefix}_EMAIL")
        if not password:
            missing.append(f"{prefix}_PASSWORD")
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
                password=password,
                groups=groups,
            )
        )

    return accounts
