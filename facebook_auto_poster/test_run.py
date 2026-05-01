"""
test_run.py — Ejecuta una publicación de prueba inmediatamente.

No espera al programador. Puedes usar solo la primera cuenta.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import job_store
from config import CONFIG, load_accounts
from account_manager_async import AsyncAccountManager

# Configurar logging básico
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s"
)

logger = logging.getLogger("test_run")


async def main() -> None:
    logger.info("=== Iniciando prueba de publicación ===\n")

    job_store.init_db()

    try:
        accounts = load_accounts()
    except ValueError as exc:
        logger.error("Error de configuración: %s", exc)
        sys.exit(1)

    logger.info("Cuentas disponibles: %s\n", ", ".join(a.name for a in accounts))

    # Usar automáticamente la primera cuenta
    if not accounts:
        logger.error("No hay cuentas configuradas.")
        sys.exit(1)
    account = accounts[0]
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

    # Buscar imagen de prueba (imagen.jpg/png en el directorio, o primera de uploaded_images/)
    image_path = None
    base = Path(__file__).resolve().parent
    for candidate in [base / "imagen.jpg", base / "imagen.png"]:
        if candidate.exists():
            image_path = str(candidate)
            break
    if not image_path:
        uploads = sorted((base / "uploaded_images").glob("*.png")) + \
                  sorted((base / "uploaded_images").glob("*.jpg"))
        if uploads:
            image_path = str(uploads[0])
    if image_path:
        logger.info("Imagen adjunta: %s\n", image_path)
    else:
        logger.info("Sin imagen (coloca imagen.jpg en el directorio para probar)\n")

    # Usar solo el primer grupo para testing
    if account.groups:
        test_group = account.groups[0]
        logger.info("Grupos disponibles: %s", ", ".join(account.groups))
        logger.info("Usando solo PRIMER GRUPO para testing: %s\n", test_group)
        account.groups = [test_group]
    else:
        logger.error("La cuenta no tiene grupos configurados.")
        sys.exit(1)

    # Ejecutar para esta cuenta única
    manager = AsyncAccountManager([account], CONFIG, text, image_path=image_path)
    logger.info("\nIniciando sesión de publicación...\n")
    try:
        results = await manager.run()
    except ValueError as exc:
        logger.error("No se pudo publicar: %s", exc)
        sys.exit(1)

    logger.info("\n=== Resultados de la prueba ===\n")
    AsyncAccountManager.print_summary(results)


if __name__ == "__main__":
    asyncio.run(main())
