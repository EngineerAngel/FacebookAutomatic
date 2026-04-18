"""
api_server.py — Servidor Flask para OpenClaw.

Endpoints:
    GET    /accounts            → cuentas y grupos configurados
    POST   /post                → publicación inmediata (JSON o multipart)
    POST   /schedule            → agendar publicación
    GET    /schedule            → listar agendadas pendientes
    DELETE /schedule/<id>       → cancelar agendada

Todos los resultados llegan a OpenClaw vía webhook (callback_url).
La base de datos SQLite es interna — no hay endpoints GET de historial.
"""

import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify

from config import CONFIG, load_accounts
from account_manager import AccountManager
import job_store
import webhook

app = Flask(__name__)

logger = logging.getLogger("api_server")
logger.setLevel(logging.INFO)

_ch = logging.StreamHandler()
_ch.setFormatter(
    logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
)
logger.addHandler(_ch)

UPLOAD_DIR = Path(__file__).resolve().parent / "uploaded_images"


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _extract_payload() -> tuple[dict | None, tuple[dict, int] | None]:
    """
    Extrae campos comunes de JSON o multipart/form-data.

    Retorna (payload_dict, None) en éxito o (None, (error_dict, http_status)).

    Campos:
      text          str   obligatorio
      image_path    str   opcional (JSON: ruta local; multipart: se sube y se guarda)
      accounts      list  opcional (filtro de cuentas; None = todas)
      scheduled_for str   opcional (ISO 8601)
      callback_url  str   opcional (webhook de retorno para OpenClaw)
    """
    content_type = (request.content_type or "").lower()

    if content_type.startswith("multipart/"):
        text = (request.form.get("text") or "").strip()
        raw_accounts = (request.form.get("accounts") or "").strip()
        accounts = [a.strip() for a in raw_accounts.split(",") if a.strip()] or None
        scheduled_for = (request.form.get("scheduled_for") or "").strip() or None
        callback_url = (request.form.get("callback_url") or "").strip() or None

        image_path: str | None = None
        file = request.files.get("image")
        if file and file.filename:
            UPLOAD_DIR.mkdir(exist_ok=True)
            ext = Path(file.filename).suffix or ".jpg"
            image_path = str(UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}")
            file.save(image_path)
            logger.info("Imagen guardada: %s", image_path)
    else:
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        image_path = data.get("image_path") or None
        raw_accounts = data.get("accounts")
        accounts = raw_accounts if isinstance(raw_accounts, list) and raw_accounts else None
        scheduled_for = data.get("scheduled_for") or None
        callback_url = data.get("callback_url") or None

    if not text:
        return None, ({"error": "Campo 'text' es obligatorio"}, 400)

    return {
        "text": text,
        "image_path": image_path,
        "accounts": accounts,
        "scheduled_for": scheduled_for,
        "callback_url": callback_url,
    }, None


def _resolve_accounts(account_filter: list[str] | None):
    """Carga y filtra cuentas. Retorna (accounts, None) o (None, (error, status))."""
    try:
        accounts = load_accounts()
    except ValueError as exc:
        return None, ({"error": str(exc)}, 500)

    if account_filter:
        accounts = [a for a in accounts if a.name in account_filter]
        if not accounts:
            return None, ({"error": "Ninguna cuenta del filtro encontrada en .env"}, 400)

    return accounts, None


def _hour_allowed(hour: int) -> bool:
    return hour in CONFIG["post_hours_allowed"]


def _hours_range_str() -> str:
    r = CONFIG["post_hours_allowed"]
    return f"{r.start:02d}:00-{(r.stop - 1):02d}:59"


def _run_job(job_id: str, accounts, text: str,
             image_path: str | None, callback_url: str | None) -> None:
    """
    Worker compartido para publicaciones inmediatas y agendadas.
    Corre en hilo daemon. Actualiza SQLite y dispara webhook al terminar.
    """
    job_store.mark_running(job_id)
    try:
        mgr = AccountManager(accounts, CONFIG, text, image_path=image_path)
        results = mgr.run()
        mgr.print_summary(results)
        job_store.mark_done(job_id, results)
        webhook.fire(callback_url, job_id, "done", results)
    except Exception:
        logger.exception("Fallo en worker | job=%s", job_id)
        job_store.mark_failed(job_id, "Unhandled exception in worker")
        webhook.fire(callback_url, job_id, "failed",
                     error_msg="Unhandled exception in worker")


# ---------------------------------------------------------------------------
# GET /accounts
# ---------------------------------------------------------------------------
@app.get("/accounts")
def list_accounts_endpoint():
    try:
        accounts = load_accounts()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({
        "accounts": [
            {"name": a.name, "groups": a.groups}
            for a in accounts
        ]
    })


# ---------------------------------------------------------------------------
# POST /post — publicación inmediata
# ---------------------------------------------------------------------------
@app.post("/post")
def handle_post():
    payload, err = _extract_payload()
    if err:
        return jsonify(err[0]), err[1]

    current_hour = datetime.now().hour
    if not _hour_allowed(current_hour):
        return jsonify({
            "error": f"Fuera del horario permitido ({_hours_range_str()})",
            "current_hour": current_hour,
        }), 403

    accounts, err = _resolve_accounts(payload["accounts"])
    if err:
        return jsonify(err[0]), err[1]

    job_id = job_store.create_job(
        text=payload["text"],
        accounts=payload["accounts"],
        image_path=payload["image_path"],
        callback_url=payload["callback_url"],
        job_type="immediate",
    )

    logger.info("Job %s aceptado | cuentas=%s", job_id,
                [a.name for a in accounts])

    threading.Thread(
        target=_run_job,
        args=(job_id, accounts, payload["text"],
              payload["image_path"], payload["callback_url"]),
        daemon=True,
        name=f"worker-{job_id}",
    ).start()

    return jsonify({
        "status": "accepted",
        "job_id": job_id,
        "accounts": [a.name for a in accounts],
        "text_preview": payload["text"][:80],
    }), 202


# ---------------------------------------------------------------------------
# POST /schedule — agendar publicación futura
# ---------------------------------------------------------------------------
@app.post("/schedule")
def create_schedule():
    payload, err = _extract_payload()
    if err:
        return jsonify(err[0]), err[1]

    if not payload["scheduled_for"]:
        return jsonify({"error": "Campo 'scheduled_for' es obligatorio (ISO 8601)"}), 400

    try:
        when = datetime.fromisoformat(payload["scheduled_for"])
    except ValueError:
        return jsonify({
            "error": "Formato inválido para 'scheduled_for'. Usa ISO 8601, ej: 2026-04-18T15:30:00"
        }), 400

    if when <= datetime.now():
        return jsonify({"error": "'scheduled_for' debe ser en el futuro"}), 400

    if not _hour_allowed(when.hour):
        return jsonify({
            "error": f"Fuera del horario permitido ({_hours_range_str()})",
            "scheduled_hour": when.hour,
        }), 400

    # Validar que las cuentas del filtro existen
    _, err = _resolve_accounts(payload["accounts"])
    if err:
        return jsonify(err[0]), err[1]

    job_id = job_store.create_job(
        text=payload["text"],
        accounts=payload["accounts"],
        image_path=payload["image_path"],
        callback_url=payload["callback_url"],
        job_type="scheduled",
        scheduled_for=when,
    )

    logger.info("Job %s agendado para %s", job_id, when.isoformat())

    return jsonify({
        "status": "scheduled",
        "job_id": job_id,
        "scheduled_for": when.isoformat(),
        "accounts": payload["accounts"] or "todas",
        "text_preview": payload["text"][:80],
    }), 201


# ---------------------------------------------------------------------------
# GET /schedule — listar agendadas pendientes
# ---------------------------------------------------------------------------
@app.get("/schedule")
def list_schedules():
    jobs = job_store.list_pending_scheduled()
    return jsonify({"pending": jobs, "count": len(jobs)})


# ---------------------------------------------------------------------------
# DELETE /schedule/<id> — cancelar agendada
# ---------------------------------------------------------------------------
@app.delete("/schedule/<job_id>")
def cancel_schedule(job_id: str):
    if job_store.cancel_job(job_id):
        logger.info("Job %s cancelado", job_id)
        return "", 204
    return jsonify({"error": f"Job '{job_id}' no encontrado o ya no está pendiente"}), 404
