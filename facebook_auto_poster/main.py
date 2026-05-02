"""
main.py — Entry point del Facebook Auto-Poster.

Levanta el servidor API Flask en 0.0.0.0:{API_PORT} (default 5000).
OpenClaw u otro orquestador externo envía las órdenes vía POST /post.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from waitress import serve

from config import CONFIG
from logging_config import setup_logging

# ---------------------------------------------------------------------------
# Logger global → logs/main.log + consola
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent / "logs"

setup_logging(
    structured=CONFIG.get("structured_logging", False),
    log_dir=LOG_DIR,
)

main_logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Túnel público — Named Cloudflare / ngrok (estático) o Quick (aleatorio)
#
# setup_tunnel.sh crea estos archivos en el setup único:
#   ~/.cloudflared/fb-autoposter.url     → URL pública permanente
#   ~/.cloudflared/fb-autoposter.backend → "cloudflare" | "ngrok"
#   ~/.cloudflared/config.yml            → config cloudflare (si aplica)
#   ~/.config/ngrok/ngrok.yml            → config ngrok (si aplica)
# ---------------------------------------------------------------------------

_TUNNEL_BASE    = Path.home() / ".cloudflared"
_URL_FILE       = _TUNNEL_BASE / "fb-autoposter.url"
_BACKEND_FILE   = _TUNNEL_BASE / "fb-autoposter.backend"
_CF_CONFIG_FILE = _TUNNEL_BASE / "config.yml"
_CF_TUNNEL_NAME = "fb-autoposter"
_NGROK_CONFIG   = Path.home() / ".config" / "ngrok" / "ngrok.yml"


def _static_tunnel_configured() -> bool:
    return _URL_FILE.exists() and _BACKEND_FILE.exists()


def _read_static_url() -> str:
    return _URL_FILE.read_text().strip()


def _read_backend() -> str:
    return _BACKEND_FILE.read_text().strip()


# ---------------------------------------------------------------------------
# Cloudflare helpers
# ---------------------------------------------------------------------------

def _find_cloudflared() -> str | None:
    import shutil, platform
    which = shutil.which("cloudflared")
    if which:
        return which
    system = platform.system()
    root = Path(__file__).resolve().parent.parent
    candidate = root / ("cloudflared.exe" if system == "Windows" else "cloudflared")
    return str(candidate) if candidate.exists() else None


def _start_cloudflare_tunnel(exe: str) -> None:
    def run() -> None:
        try:
            subprocess.run(
                [exe, "tunnel", "--config", str(_CF_CONFIG_FILE), "run", _CF_TUNNEL_NAME],
                check=False,
            )
        except Exception as exc:
            main_logger.error("Error en cloudflared: %s", exc)

    threading.Thread(target=run, daemon=True, name="cloudflared").start()
    time.sleep(3)


def _start_cloudflare_quick(exe: str, port: int) -> None:
    """Quick tunnel: captura la URL aleatoria del output de cloudflared."""
    import re
    ready = threading.Event()

    def run() -> None:
        try:
            proc = subprocess.Popen(
                [exe, "tunnel", "--url", f"http://localhost:{port}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                main_logger.debug("[cloudflared] %s", line)
                m = re.search(r'https://[^\s]+\.trycloudflare\.com', line)
                if m and not ready.is_set():
                    main_logger.warning("Túnel activo (TEMPORAL, cambia al reiniciar): %s", m.group(0))
                    main_logger.warning("Para URL permanente ejecuta: ./setup_tunnel.sh")
                    ready.set()
            proc.wait()
        except Exception as exc:
            main_logger.error("Error en cloudflared quick: %s", exc)

    threading.Thread(target=run, daemon=True, name="cloudflared").start()
    ready.wait(timeout=30)


# ---------------------------------------------------------------------------
# ngrok helper
# ---------------------------------------------------------------------------

def _find_ngrok() -> str | None:
    import shutil
    return shutil.which("ngrok")


def _start_ngrok_tunnel() -> None:
    exe = _find_ngrok()
    if not exe:
        main_logger.error(
            "ngrok no encontrado — instalar: sudo apt install ngrok  "
            "o ver https://ngrok.com/download"
        )
        return

    def run() -> None:
        try:
            subprocess.run(
                [exe, "start", "fb-autoposter", "--config", str(_NGROK_CONFIG)],
                check=False,
            )
        except Exception as exc:
            main_logger.error("Error en ngrok: %s", exc)

    threading.Thread(target=run, daemon=True, name="ngrok").start()
    time.sleep(4)


# ---------------------------------------------------------------------------
# Entry point del túnel
# ---------------------------------------------------------------------------

def start_tunnel(port: int) -> None:
    """Inicia el túnel público: estático (ngrok/cloudflare) o quick como fallback."""
    if _static_tunnel_configured():
        url     = _read_static_url()
        backend = _read_backend()
        main_logger.info("Túnel estático (%s): %s", backend, url)

        if backend == "ngrok":
            _start_ngrok_tunnel()
        else:
            exe = _find_cloudflared()
            if exe:
                _start_cloudflare_tunnel(exe)
            else:
                main_logger.error("cloudflared no encontrado para el named tunnel")
                return

        main_logger.info("API pública disponible en: %s", url)
        return

    # Sin config → quick tunnel de cloudflare con aviso
    main_logger.warning(
        "Sin túnel estático configurado — URL cambiará en cada reinicio. "
        "Para URL permanente (gratis, sin dominio): ./setup_tunnel.sh --ngrok"
    )
    exe = _find_cloudflared()
    if exe:
        _start_cloudflare_quick(exe, port)
    else:
        import platform
        hint = {"Darwin": "brew install cloudflared", "Linux": "sudo apt install cloudflared"}.get(
            platform.system(), "ver https://developers.cloudflare.com/cloudflared"
        )
        main_logger.warning("cloudflared no encontrado — túnel desactivado. Instalar: %s", hint)


# ---------------------------------------------------------------------------
# Graceful shutdown — registrado antes de arrancar waitress
# ---------------------------------------------------------------------------
_shutting_down = threading.Event()


def _install_signal_handlers() -> None:
    """Registra handlers para SIGTERM/SIGINT.

    Al recibir la señal:
    1. Detiene scheduler_runner
    2. Cancela jobs encolados en el ThreadPoolExecutor (2.3)
    3. Marca jobs 'running' como 'interrupted' en DB
    4. Sale del proceso — waitress atrapa la señal y cierra su loop
    """
    import api_server
    import job_store
    import scheduler_runner

    def _handler(signum, _frame):
        if _shutting_down.is_set():
            return  # ya en shutdown — evitar re-entrada
        _shutting_down.set()
        main_logger.warning("Señal %s recibida — iniciando shutdown graceful", signum)
        try:
            scheduler_runner.stop()
            api_server.shutdown_executor(wait=False)
            n = job_store.mark_running_as_interrupted()
            if n:
                main_logger.info("Marcados %d jobs 'running' → 'interrupted'", n)
        except Exception:
            main_logger.exception("Error durante shutdown")
        # Salir — waitress captura KeyboardInterrupt / SIGTERM y cierra su loop
        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    import job_store
    import proxy_manager
    import scheduler_runner
    from api_server import app
    from config import load_accounts

    job_store.init_db()

    # Orphan recovery: jobs 'running' al arranque son de un crash previo
    orphans = job_store.mark_running_as_interrupted()
    if orphans:
        main_logger.warning(
            "Orphan recovery: %d jobs 'running' de un shutdown previo → 'interrupted'",
            orphans,
        )

    n = job_store.upsert_accounts(load_accounts())
    main_logger.info("Sincronizadas %d cuentas en DB", n)

    proxy_manager.start()
    scheduler_runner.start()

    _install_signal_handlers()

    port = CONFIG.get("api_port", 5000)

    # Iniciar túnel público HTTPS (ngrok o cloudflare, estático si está configurado)
    start_tunnel(port)

    if CONFIG.get("use_fastapi", False):
        # Fase 3.2 — FastAPI (uvicorn ASGI): Flask montado como sub-app en /
        import uvicorn
        from v2_app import create_app
        asgi_app = create_app(app)
        main_logger.info(
            "Facebook Auto-Poster arrancando con uvicorn (FastAPI+Flask) — "
            "API 0.0.0.0:%d | /v2/* FastAPI | /* Flask | /docs Swagger",
            port,
        )
        uvicorn.run(asgi_app, host="0.0.0.0", port=port, log_level="warning")
    else:
        # Default — waitress WSGI (sin cambios respecto a Fase 2)
        main_logger.info(
            "Facebook Auto-Poster arrancando con waitress — API 0.0.0.0:%d | scheduler activo",
            port,
        )
        serve(app, host="0.0.0.0", port=port, threads=8, ident="FBAutoPoster/1.0")


if __name__ == "__main__":
    main()
