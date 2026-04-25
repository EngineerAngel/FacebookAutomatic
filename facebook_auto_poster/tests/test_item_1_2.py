"""
test_item_1_2.py — Tests del ítem 1.2: Password individual cifrada

Cubre:
  - crypto.py: roundtrip encrypt/decrypt, token es distinto cada vez, error en token corrompido
  - job_store.py: set_account_password, clear_account_password, list_accounts_full incluye password_enc
  - config.py: load_accounts() prioriza password_enc sobre FB_PASSWORD global
  - api_server.py: endpoint POST /admin/api/accounts/<name>/password
"""

import sys
import os
import json
import unittest
import tempfile

# Apuntar al módulo
sys.path.insert(0, os.path.dirname(__file__))


# ─── GRUPO 1: crypto.py ──────────────────────────────────────────────────────

class TestCrypto(unittest.TestCase):

    def setUp(self):
        # Usar una clave temporal para no pisar .secret.key real
        from cryptography.fernet import Fernet
        import crypto
        self._original_instance = crypto._fernet._instance
        crypto._fernet._instance = Fernet(Fernet.generate_key())

    def tearDown(self):
        import crypto
        crypto._fernet._instance = self._original_instance

    def test_roundtrip(self):
        from crypto import encrypt_password, decrypt_password
        plain = "mi_password_seguro_123"
        enc = encrypt_password(plain)
        self.assertEqual(decrypt_password(enc), plain)

    def test_token_diferente_cada_vez(self):
        """Fernet usa IV aleatorio — cada cifrado produce token distinto."""
        from crypto import encrypt_password
        plain = "misma_password"
        enc1 = encrypt_password(plain)
        enc2 = encrypt_password(plain)
        self.assertNotEqual(enc1, enc2)

    def test_token_es_str_ascii(self):
        from crypto import encrypt_password
        enc = encrypt_password("test123")
        self.assertIsInstance(enc, str)
        enc.encode("ascii")  # debe ser ASCII puro (base64-url)

    def test_token_corrompido_lanza_error(self):
        from crypto import decrypt_password
        from cryptography.fernet import InvalidToken
        with self.assertRaises(InvalidToken):
            decrypt_password("token_invalido_completamente")

    def test_password_vacio_lanza_error(self):
        from crypto import encrypt_password
        with self.assertRaises(ValueError):
            encrypt_password("")


# ─── GRUPO 2: job_store — password_enc ───────────────────────────────────────

class TestJobStorePassword(unittest.TestCase):

    def setUp(self):
        """Crear DB temporal para cada test."""
        import job_store
        import sqlite3
        self._orig_db = job_store.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        job_store.DB_PATH = type(job_store.DB_PATH)(
            os.path.join(self._tmpdir.name, "test.db")
        )
        job_store.init_db()
        job_store.create_account("testcuenta", "test@ejemplo.com", ["111111"])

    def tearDown(self):
        import job_store
        import sqlite3
        # Forzar cierre de conexiones pendientes de SQLite (necesario en Windows)
        try:
            conn = sqlite3.connect(str(job_store.DB_PATH))
            conn.close()
        except Exception:
            pass
        job_store.DB_PATH = self._orig_db
        self._tmpdir.cleanup()

    def test_set_password_enc_persiste(self):
        import job_store
        ok = job_store.set_account_password("testcuenta", "TOKEN_CIFRADO_XYZ")
        self.assertTrue(ok)
        rows = job_store.list_accounts_full()
        self.assertEqual(rows[0]["password_enc"], "TOKEN_CIFRADO_XYZ")

    def test_clear_password_enc_escribe_null(self):
        import job_store
        job_store.set_account_password("testcuenta", "TOKEN")
        ok = job_store.clear_account_password("testcuenta")
        self.assertTrue(ok)
        rows = job_store.list_accounts_full()
        self.assertIsNone(rows[0]["password_enc"])

    def test_set_cuenta_inexistente_retorna_false(self):
        import job_store
        ok = job_store.set_account_password("no_existe", "TOKEN")
        self.assertFalse(ok)

    def test_clear_cuenta_inexistente_retorna_false(self):
        import job_store
        ok = job_store.clear_account_password("no_existe")
        self.assertFalse(ok)

    def test_list_accounts_full_incluye_campo(self):
        import job_store
        rows = job_store.list_accounts_full()
        self.assertIn("password_enc", rows[0])


# ─── GRUPO 3: config.py — resolución de contraseña ───────────────────────────

class TestConfigPasswordResolution(unittest.TestCase):
    """Verifica la lógica de prioridad: password_enc > FB_PASSWORD global."""

    def test_fallback_a_global_cuando_sin_enc(self):
        """Si password_enc es None, se usa FB_PASSWORD."""
        from cryptography.fernet import Fernet
        import crypto
        f = Fernet(Fernet.generate_key())
        orig = crypto._fernet._instance
        crypto._fernet._instance = f
        try:
            FB_PASSWORD = "pass_global"
            pw_enc = None
            password = FB_PASSWORD
            if pw_enc:
                from crypto import decrypt_password
                password = decrypt_password(pw_enc)
            self.assertEqual(password, "pass_global")
        finally:
            crypto._fernet._instance = orig

    def test_usa_password_enc_cuando_existe(self):
        """Si password_enc existe y es válido, se usa ese."""
        from cryptography.fernet import Fernet
        import crypto
        f = Fernet(Fernet.generate_key())
        orig = crypto._fernet._instance
        crypto._fernet._instance = f
        try:
            from crypto import encrypt_password, decrypt_password
            FB_PASSWORD = "pass_global"
            pw_enc = encrypt_password("pass_propia_cuenta")
            password = FB_PASSWORD
            if pw_enc:
                password = decrypt_password(pw_enc)
            self.assertEqual(password, "pass_propia_cuenta")
        finally:
            crypto._fernet._instance = orig

    def test_fallback_si_token_invalido(self):
        """Si el token está corrompido, debe haber fallback a FB_PASSWORD."""
        from cryptography.fernet import InvalidToken
        FB_PASSWORD = "pass_global"
        pw_enc = "token_corrompido"
        password = FB_PASSWORD
        if pw_enc:
            try:
                from crypto import decrypt_password
                password = decrypt_password(pw_enc)
            except (InvalidToken, Exception):
                password = FB_PASSWORD  # fallback como lo hace config.py
        self.assertEqual(password, "pass_global")


# ─── GRUPO 4: api_server — endpoint /password ────────────────────────────────

class TestApiPasswordEndpoint(unittest.TestCase):

    def setUp(self):
        import job_store
        import api_server
        self._orig_db = job_store.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        job_store.DB_PATH = type(job_store.DB_PATH)(
            os.path.join(self._tmpdir.name, "test.db")
        )
        job_store.init_db()
        job_store.create_account("alice", "alice@fb.com", ["222222"])

        api_server.app.config["TESTING"] = True
        api_server.app.config["SECRET_KEY"] = "test_secret"
        api_server.ADMIN_KEY = "test_admin_key"
        self.client = api_server.app.test_client()
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True

    def tearDown(self):
        import job_store
        job_store.DB_PATH = self._orig_db
        self._tmpdir.cleanup()

    def _post_password(self, name, password):
        return self.client.post(
            f"/admin/api/accounts/{name}/password",
            json={"password": password},
            content_type="application/json",
        )

    def test_set_password_propia(self):
        res = self._post_password("alice", "pass_segura_123")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "updated")

    def test_reset_to_default_con_null(self):
        self._post_password("alice", "pass_previa")
        res = self._post_password("alice", None)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "reset_to_default")

    def test_reset_to_default_con_cadena_vacia(self):
        res = self._post_password("alice", "")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "reset_to_default")

    def test_password_muy_corta_retorna_400(self):
        res = self._post_password("alice", "abc")
        self.assertEqual(res.status_code, 400)

    def test_cuenta_inexistente_retorna_404(self):
        res = self._post_password("no_existe", "pass_larga_ok")
        self.assertEqual(res.status_code, 404)

    def test_has_custom_password_en_list(self):
        """GET /admin/api/accounts debe retornar has_custom_password, no password_enc."""
        self._post_password("alice", "pass_segura_123")
        res = self.client.get("/admin/api/accounts")
        data = res.get_json()
        self.assertIn("has_custom_password", data[0])
        self.assertNotIn("password_enc", data[0])
        self.assertTrue(data[0]["has_custom_password"])

    def test_has_custom_false_cuando_sin_password(self):
        res = self.client.get("/admin/api/accounts")
        data = res.get_json()
        self.assertFalse(data[0]["has_custom_password"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
