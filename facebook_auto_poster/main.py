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
# Cloudflared tunnel — multiplataforma (Windows / Mac / Ubuntu)
# ---------------------------------------------------------------------------
def _find_cloudflared() -> str | None:
    """
    Localiza el binario cloudflared según el OS.

    Orden de búsqueda:
      1. PATH del sistema (instalado via brew, apt, winget, etc.)
      2. Binario junto a la raíz del proyecto (fallback manual)

    Retorna la ruta como str, o None si no se encuentra.
    """
    import platform
    import shutil

    # 1. Buscar en PATH (preferido — instalación limpia)
    which = shutil.which("cloudflared")
    if which:
        return which

    # 2. Fallback: binario manual junto al proyecto
    system = platform.system()
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    candidates = {
        "Windows": PROJECT_ROOT / "cloudflared.exe",
        "Darwin":  PROJECT_ROOT / "cloudflared",
        "Linux":   PROJECT_ROOT / "cloudflared",
    }
    candidate = candidates.get(system)
    if candidate and candidate.exists():
        return str(candidate)

    return None


def start_cloudflared(port: int) -> None:
    """Inicia cloudflared en un thread daemon separado."""
    import platform

    exe = _find_cloudflared()
    if not exe:
        system = platform.system()
        install_hint = {
            "Darwin":  "brew install cloudflared",
            "Linux":   "sudo apt install cloudflared",
            "Windows": "winget install Cloudflare.cloudflared  o colocar cloudflared.exe junto al proyecto",
        }.get(system, "ver https://developers.cloudflare.com/cloudflared")
        main_logger.warning(
            "cloudflared no encontrado — túnel público desactivado. Instalar: %s",
            install_hint,
        )
        return

    def run_tunnel() -> None:
        try:
            main_logger.info("Iniciando Cloudflare Tunnel en http://localhost:%d ...", port)
            subprocess.run(
                [exe, "tunnel", "--url", f"http://localhost:{port}"],
                check=False,
            )
        except Exception as exc:
            main_logger.error("Error en cloudflared: %s", exc)

    thread = threading.Thread(target=run_tunnel, daemon=True, name="cloudflared")
    thread.start()
    time.sleep(2)


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
