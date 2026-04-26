"""
test_job_store.py — Tests ancla para job_store CRUD.

Usa el fixture tmp_db (conftest.py) que redirige DB_PATH a un archivo temporal
aislado por test — no toca jobs.db de producción.
"""

import pytest


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def test_create_job_returns_12_char_hex(tmp_db):
    import job_store
    job_id = job_store.create_job(
        text="Hola mundo", accounts=None, image_path=None, callback_url=None
    )
    assert len(job_id) == 12
    assert all(c in "0123456789abcdef" for c in job_id)


def test_create_multiple_jobs_have_unique_ids(tmp_db):
    import job_store
    ids = {
        job_store.create_job(text=f"msg {i}", accounts=None, image_path=None, callback_url=None)
        for i in range(30)
    }
    assert len(ids) == 30


def test_cancel_pending_job(tmp_db):
    import job_store
    job_id = job_store.create_job(
        text="cancelar", accounts=None, image_path=None, callback_url=None
    )
    assert job_store.cancel_job(job_id) is True


def test_cancel_job_is_idempotent(tmp_db):
    import job_store
    job_id = job_store.create_job(
        text="cancelar dos veces", accounts=None, image_path=None, callback_url=None
    )
    job_store.cancel_job(job_id)
    assert job_store.cancel_job(job_id) is False  # ya cancelado


def test_cancel_nonexistent_job_returns_false(tmp_db):
    import job_store
    assert job_store.cancel_job("nonexistent00") is False


def test_mark_running_then_done(tmp_db):
    import job_store
    job_id = job_store.create_job(
        text="completar", accounts=None, image_path=None, callback_url=None
    )
    job_store.mark_running(job_id)
    results = {"cuenta1": {"111": True, "222": False}}
    job_store.mark_done(job_id, results)

    jobs = job_store.get_recent_jobs(limit=10)
    match = next((j for j in jobs if j["id"] == job_id), None)
    assert match is not None
    assert match["status"] == "done"
    assert match["groups_ok"] == 1
    assert match["groups_fail"] == 1


def test_mark_running_as_interrupted(tmp_db):
    import job_store
    j1 = job_store.create_job(text="a", accounts=None, image_path=None, callback_url=None)
    j2 = job_store.create_job(text="b", accounts=None, image_path=None, callback_url=None)
    job_store.mark_running(j1)
    job_store.mark_running(j2)

    n = job_store.mark_running_as_interrupted()
    assert n == 2

    jobs = {j["id"]: j for j in job_store.get_recent_jobs(limit=10)}
    assert jobs[j1]["status"] == "interrupted"
    assert jobs[j2]["status"] == "interrupted"


# ---------------------------------------------------------------------------
# Cuentas
# ---------------------------------------------------------------------------

def test_create_account_appears_in_list(tmp_db):
    import job_store
    job_store.create_account("alice", "alice@test.com", ["123456"])
    accounts = job_store.list_accounts_full()
    assert any(a["name"] == "alice" for a in accounts)


def test_delete_account_soft(tmp_db):
    import job_store
    job_store.create_account("bob", "bob@test.com", ["789"])
    assert job_store.delete_account("bob") is True
    accounts = job_store.list_accounts_full()
    assert not any(a["name"] == "bob" for a in accounts)


def test_delete_account_twice_returns_false(tmp_db):
    import job_store
    job_store.create_account("carol", "carol@test.com", ["111"])
    job_store.delete_account("carol")
    assert job_store.delete_account("carol") is False


def test_update_account_email_and_groups(tmp_db):
    import job_store
    import json
    job_store.create_account("diana", "diana@test.com", ["111"])
    assert job_store.update_account("diana", "diana2@test.com", ["222", "333"]) is True
    accounts = {a["name"]: a for a in job_store.list_accounts_full()}
    assert accounts["diana"]["email"] == "diana2@test.com"
    assert json.loads(accounts["diana"]["groups"]) == ["222", "333"]


def test_update_nonexistent_account_returns_false(tmp_db):
    import job_store
    assert job_store.update_account("noexiste", "x@x.com", []) is False


# ---------------------------------------------------------------------------
# Login events
# ---------------------------------------------------------------------------

def test_record_login_creates_event(tmp_db):
    import job_store
    job_store.create_account("eve", "eve@test.com", ["555"])
    job_store.record_login("eve", success=True)
    job_store.record_login("eve", success=False)
    logins = [l for l in job_store.get_recent_logins(50) if l["account_name"] == "eve"]
    assert len(logins) == 2
    successes = [l for l in logins if l["success"]]
    assert len(successes) == 1


# ---------------------------------------------------------------------------
# Group tags
# ---------------------------------------------------------------------------

def test_group_tag_roundtrip(tmp_db):
    import job_store
    job_store.set_group_tag("999888", "vivienda")
    assert job_store.get_group_tag("999888") == "vivienda"


def test_group_tag_default_for_unknown(tmp_db):
    import job_store
    assert job_store.get_group_tag("grupo_sin_tag") == "generico"


def test_group_tag_update(tmp_db):
    import job_store
    job_store.set_group_tag("111", "autos")
    job_store.set_group_tag("111", "inmuebles")  # actualizar
    assert job_store.get_group_tag("111") == "inmuebles"


# ---------------------------------------------------------------------------
# Ban cooldown
# ---------------------------------------------------------------------------

def test_ban_cooldown_lifecycle(tmp_db):
    import job_store
    job_store.create_account("frank", "frank@test.com", ["222"])

    assert not job_store.is_account_in_cooldown("frank")
    job_store.set_account_ban_cooldown("frank", hours=48)
    assert job_store.is_account_in_cooldown("frank")
    job_store.clear_ban("frank")
    assert not job_store.is_account_in_cooldown("frank")


def test_banned_account_excluded_from_active_count(tmp_db):
    import job_store
    job_store.create_account("grace", "grace@test.com", ["333"])
    before = job_store.count_active_accounts()
    job_store.set_account_ban_cooldown("grace", hours=48)
    after = job_store.count_active_accounts()
    assert after == before - 1


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def test_rate_limiter_allows_under_limit(tmp_db):
    import job_store
    for _ in range(3):
        result = job_store.is_rate_limited("1.2.3.4", "/post", limit=5, window_s=60)
        assert result is False


def test_rate_limiter_blocks_over_limit(tmp_db):
    import job_store
    for _ in range(5):
        job_store.is_rate_limited("5.6.7.8", "/post", limit=5, window_s=60)
    # La 6ª llamada debe ser bloqueada
    assert job_store.is_rate_limited("5.6.7.8", "/post", limit=5, window_s=60) is True


def test_rate_limiter_different_ips_independent(tmp_db):
    import job_store
    for _ in range(5):
        job_store.is_rate_limited("10.0.0.1", "/post", limit=5, window_s=60)
    # IP distinta no está limitada
    assert job_store.is_rate_limited("10.0.0.2", "/post", limit=5, window_s=60) is False
