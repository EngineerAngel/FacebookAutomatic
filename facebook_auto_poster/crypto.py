"""
crypto.py — Cifrado simétrico de contraseñas con Fernet (AES-128-CBC + HMAC-SHA256).

Clave maestra:
  - Se almacena en .secret.key (próximo al módulo, fuera del repo vía .gitignore)
  - Se genera automáticamente en el primer uso si no existe
  - Sin la clave, las contraseñas cifradas son irrecuperables → no eliminar ni rotar sin migrar primero

Uso:
    from crypto import encrypt_password, decrypt_password

    enc = encrypt_password("mi_pass_secreta")   # → str (token Fernet base64-url)
    dec = decrypt_password(enc)                  # → str original
"""

import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Ruta de la clave maestra — junto al módulo, excluida del repo
_KEY_PATH = Path(__file__).resolve().parent / ".secret.key"


# ---------------------------------------------------------------------------
# Gestión de la clave maestra
# ---------------------------------------------------------------------------

def _load_or_create_key() -> bytes:
    """
    Devuelve la clave Fernet (32 bytes base64-url).

    Si .secret.key no existe lo crea con una clave nueva generada con
    os.urandom(32) vía Fernet.generate_key(). El archivo se crea con
    permisos restrictivos (0o600) cuando el OS lo permita.
    """
    if _KEY_PATH.exists():
        key = _KEY_PATH.read_bytes().strip()
        if len(key) != 44:  # Fernet key es siempre 44 bytes en base64-url
            raise ValueError(
                f"[crypto] .secret.key parece corrompida (longitud {len(key)}, esperado 44). "
                "Si es una clave válida de Fernet, verifica que no tenga espacios o saltos de línea extra."
            )
        return key

    # Primera ejecución — generar y persistir
    key = Fernet.generate_key()
    _KEY_PATH.write_bytes(key)
    try:
        _KEY_PATH.chmod(0o600)
    except Exception:
        pass  # Windows no soporta chmod POSIX — ignorar sin fallar
    logger.warning(
        "[crypto] Nueva clave maestra generada en %s — "
        "MANTENER SEGURA: sin ella los passwords cifrados son irrecuperables.",
        _KEY_PATH,
    )
    return key


def _fernet() -> Fernet:
    """Instancia Fernet con la clave maestra. Se cachea en módulo-level."""
    if _fernet._instance is None:  # type: ignore[attr-defined]
        _fernet._instance = Fernet(_load_or_create_key())
    return _fernet._instance  # type: ignore[attr-defined]

_fernet._instance = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def encrypt_password(plain: str) -> str:
    """
    Cifra *plain* con Fernet y devuelve el token como str (base64-url seguro).

    El token incluye un timestamp y HMAC — es autenticado, no solo cifrado.
    Cada llamada produce un token distinto (IV aleatorio interno), por lo que
    no se puede comparar directamente dos tokens del mismo plain.
    """
    if not plain:
        raise ValueError("[crypto] No se puede cifrar una contraseña vacía")
    token: bytes = _fernet().encrypt(plain.encode("utf-8"))
    return token.decode("ascii")


def decrypt_password(token: str) -> str:
    """
    Descifra un token producido por encrypt_password().

    Lanza:
        cryptography.fernet.InvalidToken — si el token fue alterado, está
            corrompido, o fue cifrado con una clave distinta.
        ValueError — si token está vacío.
    """
    if not token:
        raise ValueError("[crypto] Token de contraseña vacío")
    try:
        plain: bytes = _fernet().decrypt(token.encode("ascii"))
        return plain.decode("utf-8")
    except InvalidToken:
        logger.error(
            "[crypto] Fallo al descifrar token — ¿clave rotada o token corrompido?"
        )
        raise


def key_path() -> Path:
    """Retorna la ruta de .secret.key (útil para mensajes de diagnóstico)."""
    return _KEY_PATH
