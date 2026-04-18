"""
main.py — Entry point del Facebook Auto-Poster.

Levanta el servidor API Flask en 0.0.0.0:{API_PORT} (default 5000).
OpenClaw u otro orquestador externo envía las órdenes vía POST /post.
"""

import logging
from pathlib import Path

from config import CONFIG

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
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    import job_store
    import scheduler_runner
    from api_server import app

    job_store.init_db()
    scheduler_runner.start()

    port = CONFIG.get("api_port", 5000)
    main_logger.info(
        "Facebook Auto-Poster arrancando — API 0.0.0.0:%d | scheduler activo", port
    )

    # use_reloader=False evita el proceso doble que Flask lanza en modo debug
    # dentro de contenedores Docker esto es obligatorio
    app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    main()
