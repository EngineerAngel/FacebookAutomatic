"""
main_es.py — Punto de entrada y programador para el Auto-Poster de Facebook.

Ejecuta sesiones en un horario (martes 10:00, jueves 14:30) o
puede ser invocado una sola vez mediante run_session().
"""

import logging
import os
import sys
import time
from pathlib import Path

import schedule

from config import CONFIG, load_accounts
from account_manager import AccountManager

# ---------------------------------------------------------------------------
# Logger global → logs/main.log + consola
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

main_logger = logging.getLogger("main")
main_logger.setLevel(logging.DEBUG)

_fh = logging.FileHandler(LOG_DIR / "main.log", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(
    logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
)
main_logger.addHandler(_fh)

_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(
    logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
)
main_logger.addHandler(_ch)


# ---------------------------------------------------------------------------
# Ejecutor de sesión
# ---------------------------------------------------------------------------
def run_session() -> None:
    """Ejecuta una única sesión de publicación para todas las cuentas configuradas."""
    main_logger.info("=== Iniciando nueva sesión de publicación ===")

    try:
        accounts = load_accounts()
    except ValueError as exc:
        main_logger.error("Error de configuración: %s", exc)
        return

    main_logger.info(
        "Se cargaron %d cuenta(s): %s",
        len(accounts),
        ", ".join(a.name for a in accounts),
    )
    main_logger.info("Modo de ejecución: %s", CONFIG["execution_mode"])

    # Resolver texto de publicación
    text = os.getenv("POST_TEXT", "").strip()
    if not text:
        try:
            text = input("Introduce el texto del post: ").strip()
        except EOFError:
            main_logger.error(
                "No se proporcionó texto de publicación y stdin no está disponible."
            )
            return

    if not text:
        main_logger.error("El texto de publicación está vacío — abortando sesión.")
        return

    manager = AccountManager(accounts, CONFIG, text)
    results = manager.run()
    manager.print_summary(results)

    main_logger.info("=== Sesión finalizada ===")


# ---------------------------------------------------------------------------
# Programador
# ---------------------------------------------------------------------------
def main() -> None:
    main_logger.info("Auto-Poster de Facebook iniciando …")
    main_logger.info(
        "Ejecuciones programadas: martes 10:00, jueves 14:30  "
        "(modo=%s)",
        CONFIG["execution_mode"],
    )

    schedule.every().tuesday.at("10:00").do(run_session)
    schedule.every().thursday.at("14:30").do(run_session)

    print(
        "El programador está ejecutándose. Esperando la próxima hora programada …\n"
        "Presiona Ctrl+C para salir.\n"
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        main_logger.info("Programador detenido por el usuario.")
        print("\nHasta luego.")
        sys.exit(0)


if __name__ == "__main__":
    main()
