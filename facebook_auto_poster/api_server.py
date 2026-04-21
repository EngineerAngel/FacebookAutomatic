"""
api_server.py — Servidor Flask para OpenClaw + panel de administración.

Endpoints OpenClaw (requieren X-API-Key: OPENCLAW_API_KEY):
    GET    /accounts            → cuentas y grupos configurados
    POST   /post                → publicación inmediata (JSON o multipart)
    POST   /schedule            → agendar publicación
    GET    /schedule            → listar agendadas pendientes
    DELETE /schedule/<id>       → cancelar agendada

Endpoints admin (requieren autenticación con ADMIN_KEY):
    GET    /admin               → panel web (HTML)
    GET    /admin/login         → página de login
    POST   /admin/login         → iniciar sesión
    GET    /admin/logout        → cerrar sesión
    GET    /admin/api/accounts              → listar cuentas
    POST   /admin/api/accounts              → crear cuenta
    PUT    /admin/api/accounts/<name>       → editar cuenta
    DELETE /admin/api/accounts/<name>       → eliminar cuenta
    GET    /admin/api/groups                → listar grupos + tags
    PUT    /admin/api/groups/<group_id>/tag → asignar tag a grupo
    GET    /admin/api/history               → historial de logins
"""

import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template,
                   request, send_from_directory, session)

from config import CONFIG, load_accounts
from account_manager import AccountManager
import job_store
import webhook

app = Flask(__name__)


@app.get("/")
def root():
    return redirect("/admin")

@app.get("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")

@app.get("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")

# ---------------------------------------------------------------------------
# Claves y configuración de seguridad
# ---------------------------------------------------------------------------
ADMIN_KEY       = os.getenv("ADMIN_KEY", "").strip()
OPENCLAW_API_KEY = os.getenv("OPENCLAW_API_KEY", "").strip()

# Firma las session cookies del panel admin
app.secret_key = ADMIN_KEY if ADMIN_KEY else secrets.token_hex(32)

logger = logging.getLogger("api_server")
logger.setLevel(logging.INFO)

_ch = logging.StreamHandler()
_ch.setFormatter(
    logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
)
logger.addHandler(_ch)

UPLOAD_DIR = Path(__file__).resolve().parent / "uploaded_images"

# Extensiones de imagen permitidas
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# ---------------------------------------------------------------------------
# Rate limiter simple en memoria (por IP, ventana deslizante)
# ---------------------------------------------------------------------------
_rate_data: defaultdict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()
_RATE_LIMIT   = 10   # máx requests
_RATE_WINDOW  = 60   # por ventana de N segundos


def _is_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = _rate_data[ip]
        # Eliminar hits fuera de la ventana
        _rate_data[ip] = [t for t in hits if now - t < _RATE_WINDOW]
        if len(_rate_data[ip]) >= _RATE_LIMIT:
            return True
        _rate_data[ip].append(now)
        return False


# ---------------------------------------------------------------------------
# Decoradores de autenticación
# ---------------------------------------------------------------------------
def openclaw_required(f):
    """Verifica X-API-Key para endpoints de OpenClaw."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not OPENCLAW_API_KEY:
            return jsonify({"error": "OPENCLAW_API_KEY no configurado en .env"}), 503

        provided = request.headers.get("X-API-Key", "").strip()
        if not provided or not secrets.compare_digest(provided, OPENCLAW_API_KEY):
            logger.warning("Acceso OpenClaw rechazado desde %s — clave inválida",
                           request.remote_addr)
            return jsonify({"error": "API key inválida o ausente"}), 401

        ip = request.remote_addr or "unknown"
        if _is_rate_limited(ip):
            logger.warning("Rate limit alcanzado para %s", ip)
            return jsonify({"error": "Demasiadas peticiones, espera un momento"}), 429

        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Verifica sesión autenticada para el panel admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ADMIN_KEY:
            if request.path.startswith("/admin/api/"):
                return jsonify({"error": "ADMIN_KEY no configurado en .env"}), 503
            return render_template("admin_login.html",
                                   error="ADMIN_KEY no configurado en .env"), 503
        if not session.get("admin_authenticated"):
            if request.path.startswith("/admin/api/"):
                return jsonify({"error": "No autorizado"}), 401
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Validaciones de input admin
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r"^[a-z0-9_]{1,30}$")


def _validate_account_input(name: str, email: str, groups: list) -> str | None:
    if not _NAME_RE.match(name):
        return "Nombre inválido: solo letras minúsculas, números y _ (máx 30)"
    if "@" not in email or "." not in email.split("@")[-1]:
        return "Correo electrónico inválido"
    if not isinstance(groups, list) or not groups:
        return "Debes proporcionar al menos un grupo"
    for g in groups:
        if not str(g).strip().isdigit():
            return f"ID de grupo inválido: '{g}' — solo se aceptan números"
    return None


def _sanitize_tag(tag: str) -> str | None:
    """Retorna tag limpio o None si es inválido. Soporta emojis y Unicode."""
    tag = tag.strip()[:80]
    if not tag:
        return None
    tag = re.sub(r"[<>\"'&]", "", tag).strip()
    return tag or None


def _safe_image_path(path: str) -> str | None:
    """
    Valida que image_path (modo JSON) sea una ruta dentro de UPLOAD_DIR.
    Previene path traversal. Retorna la ruta resuelta o None si es inválida.
    """
    try:
        resolved = Path(path).resolve()
        upload_resolved = UPLOAD_DIR.resolve()
        # La ruta debe estar dentro de uploaded_images/
        if upload_resolved not in resolved.parents and resolved != upload_resolved:
            return None
        if not resolved.exists():
            return None
        if resolved.suffix.lower() not in _ALLOWED_EXTENSIONS:
            return None
        return str(resolved)
    except Exception:
        return None


def _validate_image_upload(file) -> tuple[str | None, str | None]:
    """
    Valida y guarda un archivo subido por multipart.
    Retorna (path_guardado, None) en éxito, o (None, error_msg) si falla.
    """
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return None, f"Tipo de archivo no permitido. Solo: {', '.join(_ALLOWED_EXTENSIONS)}"

    # Verificar MIME type del Content-Type del file part
    mime = (file.content_type or "").lower()
    if mime and not any(mime.startswith(m) for m in
                        ("image/jpeg", "image/png", "image/gif", "image/webp")):
        return None, "MIME type no permitido para la imagen"

    UPLOAD_DIR.mkdir(exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / safe_name
    file.save(str(dest))
    logger.info("Imagen guardada: %s", dest)
    return str(dest), None


# ---------------------------------------------------------------------------
# Helpers internos (OpenClaw)
# ---------------------------------------------------------------------------
def _extract_payload() -> tuple[dict | None, tuple[dict, int] | None]:
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
            image_path, err = _validate_image_upload(file)
            if err:
                return None, ({"error": err}, 400)
    else:
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        raw_image = data.get("image_path") or None
        image_path = None
        if raw_image:
            image_path = _safe_image_path(str(raw_image))
            if image_path is None:
                return None, ({"error": "image_path inválido o fuera del directorio permitido"}, 400)
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


def _resolve_accounts(
    account_filter: list[str] | None,
) -> tuple[list, tuple[dict, int] | None]:
    try:
        accounts = load_accounts()
    except ValueError as exc:
        return [], ({"error": str(exc)}, 500)

    if account_filter:
        accounts = [a for a in accounts if a.name in account_filter]
        if not accounts:
            return [], ({"error": "Ninguna cuenta del filtro encontrada"}, 400)

    return accounts, None


def _hour_allowed(hour: int) -> bool:
    return hour in CONFIG["post_hours_allowed"]


def _hours_range_str() -> str:
    r = CONFIG["post_hours_allowed"]
    return f"{r.start:02d}:00-{(r.stop - 1):02d}:59"


def _run_job(job_id: str, accounts, text: str,
             image_path: str | None, callback_url: str | None) -> None:
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


# ===========================================================================
# ENDPOINTS OPENCLAW (todos protegidos con X-API-Key)
# ===========================================================================

@app.get("/accounts")
@openclaw_required
def list_accounts_endpoint():
    try:
        accounts = load_accounts()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 500

    db_info = {r["name"]: r for r in job_store.get_accounts_info()}

    return jsonify({
        "accounts": [
            {
                "name": a.name,
                "email": a.email,
                "groups": a.groups,
                "last_login_at": db_info.get(a.name, {}).get("last_login_at"),
                "last_published_at": db_info.get(a.name, {}).get("last_published_at"),
            }
            for a in accounts
        ]
    })


@app.post("/post")
@openclaw_required
def handle_post():
    payload, err = _extract_payload()
    if err:
        return jsonify(err[0]), err[1]
    assert payload is not None

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


@app.post("/schedule")
@openclaw_required
def create_schedule():
    payload, err = _extract_payload()
    if err:
        return jsonify(err[0]), err[1]
    assert payload is not None

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


@app.get("/schedule")
@openclaw_required
def list_schedules():
    jobs = job_store.list_pending_scheduled()
    return jsonify({"pending": jobs, "count": len(jobs)})


@app.delete("/schedule/<job_id>")
@openclaw_required
def cancel_schedule(job_id: str):
    if job_store.cancel_job(job_id):
        logger.info("Job %s cancelado", job_id)
        return "", 204
    return jsonify({"error": f"Job '{job_id}' no encontrado o ya no está pendiente"}), 404


@app.put("/groups/<group_id>/tag")
@openclaw_required
def set_group_tag_public(group_id: str):
    data = request.get_json(silent=True) or {}
    tag = _sanitize_tag(data.get("tag") or "")
    if not tag:
        return jsonify({"error": "Campo 'tag' es obligatorio"}), 400
    job_store.set_group_tag(group_id, tag)
    return jsonify({"group_id": group_id, "tag": tag})


# ===========================================================================
# ADMIN — Sesión
# ===========================================================================

@app.get("/admin/login")
def admin_login_page():
    if session.get("admin_authenticated"):
        return redirect("/admin")
    return render_template("admin_login.html", error=None)


@app.post("/admin/login")
def admin_login():
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or request.form.get("key") or "").strip()

    if not ADMIN_KEY:
        return jsonify({"error": "ADMIN_KEY no configurado en .env"}), 503

    if not secrets.compare_digest(key, ADMIN_KEY):
        logger.warning("Intento de acceso admin fallido desde %s", request.remote_addr)
        if request.is_json:
            return jsonify({"error": "Clave incorrecta"}), 401
        return render_template("admin_login.html", error="Clave incorrecta"), 401

    session.permanent = False
    session["admin_authenticated"] = True
    logger.info("Acceso admin autorizado desde %s", request.remote_addr)

    if request.is_json:
        return jsonify({"status": "ok"})
    return redirect("/admin")


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")


@app.get("/admin")
@admin_required
def admin_panel():
    return render_template("admin.html")


# ===========================================================================
# ADMIN API — Cuentas
# ===========================================================================

@app.get("/admin/api/accounts")
@admin_required
def admin_list_accounts():
    return jsonify(job_store.list_accounts_full())


@app.post("/admin/api/accounts")
@admin_required
def admin_create_account():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip().lower()
    email = (data.get("email") or "").strip()
    groups = [str(g).strip() for g in (data.get("groups") or []) if str(g).strip()]

    err = _validate_account_input(name, email, groups)
    if err:
        return jsonify({"error": err}), 400

    job_store.create_account(name, email, groups)
    logger.info("Cuenta '%s' creada via admin", name)
    return jsonify({"status": "created", "name": name}), 201


@app.put("/admin/api/accounts/<name>")
@admin_required
def admin_update_account(name: str):
    data = request.get_json(silent=True) or {}
    new_name = (data.get("name") or name).strip().lower()
    email  = (data.get("email") or "").strip()
    groups = [str(g).strip() for g in (data.get("groups") or []) if str(g).strip()]

    err = _validate_account_input(new_name, email, groups)
    if err:
        return jsonify({"error": err}), 400

    if new_name != name:
        if not job_store.rename_account(name, new_name, email, groups):
            return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404
        logger.info("Cuenta '%s' renombrada a '%s' via admin", name, new_name)
        return jsonify({"status": "renamed", "name": new_name})

    if not job_store.update_account(name, email, groups):
        return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404

    logger.info("Cuenta '%s' actualizada via admin", name)
    return jsonify({"status": "updated", "name": name})


@app.delete("/admin/api/accounts/<name>")
@admin_required
def admin_delete_account(name: str):
    if not job_store.delete_account(name):
        return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404
    logger.info("Cuenta '%s' eliminada via admin", name)
    return "", 204


# ===========================================================================
# ADMIN API — Grupos & Tags
# ===========================================================================

@app.get("/admin/api/groups")
@admin_required
def admin_list_groups():
    return jsonify(job_store.list_group_tags())


@app.put("/admin/api/groups/<group_id>/tag")
@admin_required
def admin_set_group_tag(group_id: str):
    data = request.get_json(silent=True) or {}
    tag = _sanitize_tag(data.get("tag") or "")
    if not tag:
        return jsonify({"error": "Campo 'tag' es obligatorio (máx 80 caracteres)"}), 400
    job_store.set_group_tag(group_id, tag)
    logger.info("Grupo %s etiquetado como '%s'", group_id, tag)
    return jsonify({"group_id": group_id, "tag": tag})


# ===========================================================================
# ADMIN API — Historial
# ===========================================================================

@app.get("/admin/api/history")
@admin_required
def admin_history():
    limit = min(int(request.args.get("limit", "50")), 200)
    return jsonify(job_store.get_recent_logins(limit))


@app.get("/admin/api/jobs")
@admin_required
def admin_jobs():
    limit = min(int(request.args.get("limit", "50")), 200)
    return jsonify(job_store.get_recent_jobs(limit))


# ===========================================================================
# ADMIN — Página de publicación
# ===========================================================================

@app.get("/admin/publish")
@admin_required
def admin_publish_page():
    return render_template("publish.html")


@app.get("/admin/api/anuncio")
@admin_required
def admin_get_anuncio():
    anuncio_path = Path(__file__).resolve().parent / "anuncio.txt"
    if not anuncio_path.exists():
        return jsonify({"error": "anuncio.txt no encontrado"}), 404
    text = anuncio_path.read_text(encoding="utf-8").strip()
    return jsonify({"text": text})


@app.post("/admin/api/post")
@admin_required
def admin_post():
    payload, err = _extract_payload()
    if err:
        return jsonify(err[0]), err[1]
    assert payload is not None

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
        callback_url=None,
        job_type="immediate",
    )

    logger.info("Job %s aceptado via admin | cuentas=%s", job_id,
                [a.name for a in accounts])

    threading.Thread(
        target=_run_job,
        args=(job_id, accounts, payload["text"], payload["image_path"], None),
        daemon=True,
        name=f"worker-{job_id}",
    ).start()

    return jsonify({
        "status": "accepted",
        "job_id": job_id,
        "accounts": [a.name for a in accounts],
        "text_preview": payload["text"][:80],
    }), 202


@app.post("/admin/api/schedule")
@admin_required
def admin_create_schedule():
    payload, err = _extract_payload()
    if err:
        return jsonify(err[0]), err[1]
    assert payload is not None

    if not payload["scheduled_for"]:
        return jsonify({"error": "Campo 'scheduled_for' es obligatorio (ISO 8601)"}), 400

    try:
        when = datetime.fromisoformat(payload["scheduled_for"])
    except ValueError:
        return jsonify({
            "error": "Formato inválido para 'scheduled_for'. Usa ISO 8601, ej: 2026-04-20T15:30:00"
        }), 400

    if when <= datetime.now():
        return jsonify({"error": "'scheduled_for' debe ser en el futuro"}), 400

    if not _hour_allowed(when.hour):
        return jsonify({
            "error": f"Fuera del horario permitido ({_hours_range_str()})",
            "scheduled_hour": when.hour,
        }), 400

    _, err = _resolve_accounts(payload["accounts"])
    if err:
        return jsonify(err[0]), err[1]

    job_id = job_store.create_job(
        text=payload["text"],
        accounts=payload["accounts"],
        image_path=payload["image_path"],
        callback_url=None,
        job_type="scheduled",
        scheduled_for=when,
    )

    logger.info("Job %s agendado para %s via admin", job_id, when.isoformat())

    return jsonify({
        "status": "scheduled",
        "job_id": job_id,
        "scheduled_for": when.isoformat(),
        "accounts": payload["accounts"] or "todas",
        "text_preview": payload["text"][:80],
    }), 201


@app.get("/admin/api/schedule")
@admin_required
def admin_list_schedule():
    jobs = job_store.list_pending_scheduled()
    return jsonify({"pending": jobs, "count": len(jobs)})


@app.delete("/admin/api/schedule/<job_id>")
@admin_required
def admin_cancel_schedule(job_id: str):
    if job_store.cancel_job(job_id):
        logger.info("Job %s cancelado via admin", job_id)
        return "", 204
    return jsonify({"error": f"Job '{job_id}' no encontrado o ya no está pendiente"}), 404
