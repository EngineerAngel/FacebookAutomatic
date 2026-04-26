"""
conftest.py — Fixtures compartidos para todos los tests de Fase 3.

Requisitos previos:
- facebook_auto_poster/.env debe existir (copia de .env.example).
  En CI: crear el archivo con los valores mínimos antes de correr pytest.
  Valores mínimos: FB_PASSWORD=test, ADMIN_KEY=test, OPENCLAW_API_KEY=test
"""

import sys
from pathlib import Path

# Añadir facebook_auto_poster/ al path para que los imports funcionen
# desde cualquier directorio de trabajo.
_FB_DIR = Path(__file__).resolve().parent.parent
if str(_FB_DIR) not in sys.path:
    sys.path.insert(0, str(_FB_DIR))

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """DB SQLite aislada por test. Parcha job_store.DB_PATH antes de init_db().

    Uso:
        def test_algo(tmp_db):
            import job_store
            job_store.create_account("alice", "alice@test.com", ["123"])
    """
    import job_store
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(job_store, "DB_PATH", db_file)
    job_store.init_db()
    yield db_file
