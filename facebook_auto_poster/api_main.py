"""
api_main.py — Entry point del proceso API (Fase 3.7, SPLIT_PROCESSES=1).

En modo monoproceso (SPLIT_PROCESSES=0) main.py delega aquí también,
por lo que api_main.py es el entry point real en ambos modos.

En modo biproceso (SPLIT_PROCESSES=1):
- Solo levanta Flask/FastAPI — NO arranca scheduler ni executor de workers
- Los jobs se crean en DB con status='pending'; worker_main.py los recoge

En modo monoproceso (SPLIT_PROCESSES=0):
- Comportamiento idéntico al main.py original
- Arranca scheduler_runner y el executor está activo en api_server.py
"""
from __future__ import annotations

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

LOG_DIR = Path(__file__).resolve().parent / "logs"
setup_logging(structured=CONFIG.get("structured_logging", False), log_dir=LOG_DIR)
main_logger = logging.getLogger("api_main")

# ---------------------------------------------------------------------------
# Túnel público — misma lógica de main.py (cloudflare / ngrok)
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
                    main_logger.warning("Túnel activo (TEMPORAL): %s", m.group(0))
                    ready.set()
            proc.wait()
        except Exception as exc:
            main_logger.error("Error en cloudflared quick: %s", exc)

    threading.Thread(target=run, daemon=True, name="cloudflared").start()
    ready.wait(timeout=30)


def _find_ngrok() -> str | None:
    import shutil
    return shutil.which("ngrok")


def _start_ngrok_tunnel() -> None:
    exe = _find_ngrok()
    if not exe:
        main_logger.error("ngrok no encontrado")
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


def start_tunnel(port: int) -> None:
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
                main_logger.error("cloudflared no encontrado")
                return
        main_logger.info("API pública disponible en: %s", url)
        return

    main_logger.warning(
        "Sin túnel estático — URL cambiará en cada reinicio. "
        "Para URL permanente: ./setup_tunnel.sh --ngrok"
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
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutting_down = threading.Event()


def _install_signal_handlers() -> None:
    import api_server
    import job_store
    import scheduler_runner

    def _handler(signum, _frame):
        if _shutting_down.is_set():
            return
        _shutting_down.set()
        main_logger.warning("Señal %s recibida — iniciando shutdown graceful", signum)
        try:
            scheduler_runner.stop()
            api_server.shutdown_executor(wait=False)
            if not CONFIG.get("split_processes"):
                n = job_store.mark_running_as_interrupted()
                if n:
                    main_logger.info("Marcados %d jobs 'running' → 'interrupted'", n)
        except Exception:
            main_logger.exception("Error durante shutdown")
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

    split = CONFIG.get("split_processes", False)

    if not split:
        # En monoproceso el proceso API es dueño de los jobs 'running'
        orphans = job_store.mark_running_as_interrupted()
        if orphans:
            main_logger.warning(
                "Orphan recovery: %d jobs 'running' → 'interrupted'", orphans
            )

    n = job_store.upsert_accounts(load_accounts())
    main_logger.info("Sincronizadas %d cuentas en DB", n)

    proxy_manager.start()

    if not split:
        # En monoproceso el scheduler vive en este proceso
        scheduler_runner.start()
        main_logger.info("Scheduler arrancado (monoproceso)")
    else:
        main_logger.info("Modo biproceso: scheduler y workers en worker_main.py")

    _install_signal_handlers()

    port = CONFIG.get("api_port", 5000)
    start_tunnel(port)

    if CONFIG.get("use_fastapi", False):
        import uvicorn
        from v2_app import create_app
        asgi_app = create_app(app)
        main_logger.info(
            "API arrancando con uvicorn (FastAPI+Flask) — 0.0.0.0:%d | /v2/* | /docs",
            port,
        )
        uvicorn.run(asgi_app, host="0.0.0.0", port=port, log_level="warning")
    else:
        mode = "biproceso" if split else "monoproceso"
        main_logger.info(
            "API arrancando con waitress — 0.0.0.0:%d | modo=%s", port, mode
        )
        serve(app, host="0.0.0.0", port=port, threads=8, ident="FBAutoPoster/1.0")


if __name__ == "__main__":
    main()
