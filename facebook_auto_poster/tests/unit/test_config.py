"""
test_config.py — Tests ancla para config.is_account_hour_allowed().

Cubre los casos identificados en plan/OBSERVACIONES_P3.md (P3.1):
  - Hora dentro de ventana
  - Hora fuera de ventana (madrugada)
  - Boundary inicio (start <= hour → True)
  - Boundary fin   (hour < end  → False en hour==end)
  - Conversión de timezone (UTC → America/Mexico_City)
  - Ventana imposible (0, 0)
  - Ventana completa  (0, 24)
"""

from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from config import AccountConfig, is_account_hour_allowed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account(tz: str = "UTC", active_hours: tuple[int, int] = (7, 23)) -> AccountConfig:
    return AccountConfig(
        name="testaccount",
        email="test@test.com",
        password="x",
        timezone=tz,
        active_hours=active_hours,
    )


from contextlib import contextmanager

@contextmanager
def _patch_now(utc_hour: int):
    """Parcha config.datetime.now para devolver una hora UTC fija.

    La lambda convierte correctamente a cualquier timezone usando astimezone,
    reproduciendo el comportamiento real de datetime.now(tz).
    """
    utc_dt = datetime(2026, 4, 26, utc_hour, 0, 0, tzinfo=timezone.utc)
    with patch("config.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: utc_dt.astimezone(tz) if tz else utc_dt
        yield mock_dt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_hour_inside_window():
    with _patch_now(12):  # 12 UTC, ventana 7-23 UTC
        assert is_account_hour_allowed(_account("UTC", (7, 23))) is True


def test_hour_before_window():
    with _patch_now(3):  # 3 UTC, fuera de ventana 7-23
        assert is_account_hour_allowed(_account("UTC", (7, 23))) is False


def test_hour_after_window():
    with _patch_now(23):  # 23 UTC, fuera de ventana (7, 22)
        # start <= hour <= end → 7 <= 23 <= 22 → False
        assert is_account_hour_allowed(_account("UTC", (7, 22))) is False


def test_hour_at_window_end_included():
    with _patch_now(23):  # 23 UTC, exactamente en el límite superior de (7, 23)
        # start <= hour <= end → 7 <= 23 <= 23 → True (bug G4 corregido)
        assert is_account_hour_allowed(_account("UTC", (7, 23))) is True


def test_boundary_start_included():
    with _patch_now(7):  # exactamente a la hora de inicio
        # 7 <= 7 < 23 → True
        assert is_account_hour_allowed(_account("UTC", (7, 23))) is True


def test_boundary_one_before_start():
    with _patch_now(6):
        assert is_account_hour_allowed(_account("UTC", (7, 23))) is False


def test_window_zero_zero_only_midnight():
    """Ventana (0, 0): 0 <= h <= 0 → solo medianoche permitida."""
    with _patch_now(0):
        assert is_account_hour_allowed(_account("UTC", (0, 0))) is True
    for utc_hour in (1, 6, 12, 18, 23):
        with _patch_now(utc_hour):
            assert is_account_hour_allowed(_account("UTC", (0, 0))) is False


def test_fullday_window_always_true():
    """Ventana (0, 23): todas las horas 0-23 satisfacen 0 <= h <= 23."""
    for utc_hour in range(24):
        with _patch_now(utc_hour):
            assert is_account_hour_allowed(_account("UTC", (0, 23))) is True


def test_timezone_mexico_city_inside():
    """UTC 18:00 = 13:00 CDT (America/Mexico_City, UTC-5 en abril) → dentro de (7,23)."""
    with _patch_now(18):
        assert is_account_hour_allowed(_account("America/Mexico_City", (7, 23))) is True


def test_timezone_mexico_city_outside_early_morning():
    """UTC 11:00 = 06:00 CDT → fuera de (7,23)."""
    with _patch_now(11):
        assert is_account_hour_allowed(_account("America/Mexico_City", (7, 23))) is False


def test_timezone_different_from_utc():
    """Timezone 'Europe/Madrid' (UTC+2 en abril): UTC 04:00 = 06:00 local → fuera (7,23)."""
    with _patch_now(4):
        assert is_account_hour_allowed(_account("Europe/Madrid", (7, 23))) is False


def test_timezone_different_from_utc_inside():
    """'Europe/Madrid' UTC+2: UTC 10:00 = 12:00 local → dentro (7,23)."""
    with _patch_now(10):
        assert is_account_hour_allowed(_account("Europe/Madrid", (7, 23))) is True
