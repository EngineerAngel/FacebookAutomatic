"""
main.py — Entry point del Facebook Auto-Poster.

Levanta el servidor API Flask en 0.0.0.0:{API_PORT} (default 5000).
OpenClaw u otro orquestador externo envía las órdenes vía POST /post.
"""

import logging
import os
import subprocess
import sys
import threading
import time
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
# Cloudflared tunnel (acceso público con HTTPS)
# ---------------------------------------------------------------------------
def start_cloudflared(port: int) -> None:
    """Inicia cloudflared en un thread separado."""
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    cloudflared_exe = PROJECT_ROOT / "cloudflared.exe"

    if not cloudflared_exe.exists():
        main_logger.warning("cloudflared.exe no encontrado en %s — saltando túnel público", PROJECT_ROOT)
        return

    def run_tunnel():
        try:
            main_logger.info("Iniciando Cloudflare Tunnel (acceso HTTPS público)...")
            subprocess.run(
                [str(cloudflared_exe), "tunnel", "--url", f"http://localhost:{port}"],
                check=False
            )
        except Exception as e:
            main_logger.error("Error en cloudflared: %s", e)

    thread = threading.Thread(target=run_tunnel, daemon=True)
    thread.start()
    time.sleep(2)  # Dar tiempo a cloudflared para mostrar la URL


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    import job_store
    import scheduler_runner
    from api_server import app
    from config import load_accounts

    job_store.init_db()
    n = job_store.upsert_accounts(load_accounts())
    main_logger.info("Sincronizadas %d cuentas en DB", n)
    scheduler_runner.start()

    port = CONFIG.get("api_port", 5000)
    main_logger.info(
        "Facebook Auto-Poster arrancando — API 0.0.0.0:%d | scheduler activo", port
    )

    # Iniciar cloudflared para acceso HTTPS público (opcional)
    start_cloudflared(port)

    # use_reloader=False evita el proceso doble que Flask lanza en modo debug
    # dentro de contenedores Docker esto es obligatorio
    app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    main()
