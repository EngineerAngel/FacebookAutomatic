"""
test_run.py — Ejecuta una publicación de prueba inmediatamente.

No espera al programador. Puedes usar solo la primera cuenta.
"""

import logging
import os
import sys
from pathlib import Path

import job_store
from config import CONFIG, load_accounts
from account_manager import AccountManager

# Configurar logging básico
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s"
)

logger = logging.getLogger("test_run")


def main() -> None:
    logger.info("=== Iniciando prueba de publicación ===\n")

    job_store.init_db()

    try:
        accounts = load_accounts()
    except ValueError as exc:
        logger.error("Error de configuración: %s", exc)
        sys.exit(1)

    logger.info("Cuentas disponibles: %s\n", ", ".join(a.name for a in accounts))

    # Seleccionar cuenta
    print("Cuentas disponibles:")
    for i, a in enumerate(accounts):
        print(f"  {i + 1}. {a.name}")
    choice = input("\nElige el número de cuenta: ").strip()
    try:
        account = accounts[int(choice) - 1]
    except (ValueError, IndexError):
        logger.error("Selección inválida.")
        sys.exit(1)
    logger.info("Usando la cuenta: %s\n", account.name)

    # Leer texto desde anuncio.txt
    anuncio_path = Path(__file__).resolve().parent / "anuncio.txt"
    if not anuncio_path.exists():
        logger.error("No se encontró anuncio.txt en el directorio del proyecto.")
        sys.exit(1)

    with open(anuncio_path, "r", encoding="utf-8") as f:
        text = f.read().strip()

    if not text:
        logger.error("El archivo anuncio.txt está vacío.")
        sys.exit(1)

    logger.info("Texto de publicación:\n%s\n", text)

    # Usar solo el primer grupo para testing
    if account.groups:
        test_group = account.groups[0]
        logger.info("Grupos disponibles: %s", ", ".join(account.groups))
        logger.info("Usando solo PRIMER GRUPO para testing: %s\n", test_group)
        account.groups = [test_group]
    else:
        logger.error("La cuenta no tiene grupos configurados.")
        sys.exit(1)

    # Confirmar antes de continuar
    confirm = input("\n¿Continuar con la publicación? (s/n): ").strip().lower()
    if confirm != "s":
        logger.info("Prueba cancelada.")
        sys.exit(0)

    # Ejecutar para esta cuenta única
    manager = AccountManager([account], CONFIG, text)
    logger.info("\nIniciando sesión de publicación...\n")
    try:
        results = manager.run()
    except ValueError as exc:
        logger.error("No se pudo publicar: %s", exc)
        sys.exit(1)

    logger.info("\n=== Resultados de la prueba ===\n")
    manager.print_summary(results)


if __name__ == "__main__":
    main()
