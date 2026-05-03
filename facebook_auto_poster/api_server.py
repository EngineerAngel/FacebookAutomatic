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
    GET    /admin/api/proxies               → listar nodos proxy + asignaciones
    POST   /admin/api/proxies               → registrar nodo proxy
    DELETE /admin/api/proxies/<id>          → eliminar nodo proxy
    PUT    /admin/api/proxies/<id>/status   → cambiar status manualmente
    POST   /admin/api/accounts/<name>/proxy → asignar proxy a cuenta
    DELETE /admin/api/accounts/<name>/proxy → quitar proxy de cuenta
    POST   /admin/api/accounts/<name>/login → iniciar sesión manual (async + polling)
    GET    /admin/api/accounts/<name>/login/<run_id> → estado del login manual
    GET    /admin/api/templates             → listar plantillas
    POST   /admin/api/templates             → crear plantilla
    GET    /admin/api/templates/<id>        → obtener plantilla
    PUT    /admin/api/templates/<id>        → actualizar plantilla
    DELETE /admin/api/templates/<id>        → eliminar plantilla
    POST   /admin/api/upload-images         → subir 1–5 imágenes (devuelve paths)
    GET    /admin/uploaded-images/<file>    → servir imagen guardada (preview admin)
"""

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template,
                   request, send_from_directory, session)

from config import CONFIG, load_accounts, apply_group_filter, AccountConfig, pick_fingerprint
from account_manager_async import AsyncAccountManager
import job_store
import proxy_manager
import webhook
import metrics
import worker_core
from group_discoverer import discover_groups_for_account
try:
    from crypto import encrypt_password as _encrypt_password
    _CRYPTO_AVAILABLE = True
except ImportError:
    _encrypt_password = None  # type: ignore[assignment]
    _CRYPTO_AVAILABLE = False

app = Flask(__name__)

# Timestamp del arranque para reportar uptime en /health
_START_TIME = time.time()

# ---------------------------------------------------------------------------
# Pool de workers (Fase 2.3)
# ---------------------------------------------------------------------------
# ThreadPoolExecutor con límite → evita que 10 requests simultáneos lancen
# 10 Chromes. Cada lock (en worker_core) garantiza que una misma cuenta no
# tenga dos browsers/jobs compitiendo por el mismo user_data_dir.
_executor = ThreadPoolExecutor(
    max_workers=CONFIG.get("max_concurrent_workers", 2),
    thread_name_prefix="fb-worker",
)

# Estado de runs de login manual — en memoria, no necesita persistencia en BD
_login_runs: dict[str, dict] = {}


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
# Healthcheck — público, sin info sensible
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """Liveness/readiness probe para OpenClaw y monitoreo externo."""
    try:
        with job_store._connect() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False

    active_accounts = job_store.count_active_accounts() if db_ok else 0
    pending = job_store.count_pending_jobs() if db_ok else -1

    status = "ok" if (db_ok and active_accounts > 0) else "degraded"
    return jsonify({
        "status": status,
        "db": db_ok,
        "active_accounts": active_accounts,
        "pending_jobs": pending,
        "uptime_s": int(time.time() - _START_TIME),
    }), (200 if status == "ok" else 503)

# ---------------------------------------------------------------------------
# Claves y configuración de seguridad
# ---------------------------------------------------------------------------
ADMIN_KEY        = os.getenv("ADMIN_KEY", "").strip()
OPENCLAW_API_KEY = os.getenv("OPENCLAW_API_KEY", "").strip()

logger = logging.getLogger("api_server")
logger.setLevel(logging.INFO)

_ch = logging.StreamHandler()
_ch.setFormatter(
    logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
)
logger.addHandler(_ch)

# [FIX P0-1] SESSION_SECRET dedicado para signing de sesiones — DISTINTO de ADMIN_KEY.
_SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
app.secret_key = _SESSION_SECRET if _SESSION_SECRET else secrets.token_hex(32)
if not _SESSION_SECRET:
    logger.warning(
        "[security] SESSION_SECRET no configurado — usando clave volátil. "
        "Las sesiones admin se invalidarán en cada reinicio. "
        "Añade SESSION_SECRET=<token-aleatorio-largo> a .env"
    )

UPLOAD_DIR = Path(__file__).resolve().parent / "uploaded_images"

# Extensiones de imagen permitidas
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Límite de imágenes por publicación / por subida
_MAX_IMAGES = 5

# Validación de plantillas
MAX_TEMPLATE_NAME_CHARS = 100
MIN_TEMPLATE_TEXT_CHARS = 10
MAX_TEMPLATE_TEXT_CHARS = 50_000
_TEMPLATE_ID_PATTERN    = re.compile(r"^[a-f0-9]{12}$")

# ---------------------------------------------------------------------------
# Rate limiter — persistente en SQLite (sobrevive a reinicios)
# ---------------------------------------------------------------------------
_RATE_LIMIT  = 10   # máx requests por ventana
_RATE_WINDOW = 60   # ventana en segundos


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
        endpoint = request.endpoint or request.path
        if job_store.is_rate_limited(ip, endpoint, _RATE_LIMIT, _RATE_WINDOW):
            logger.warning("Rate limit alcanzado para %s en %s", ip, endpoint)
            metrics.inc_api_request(endpoint, 429)
            return jsonify({"error": "Demasiadas peticiones, espera un momento"}), 429

        result = f(*args, **kwargs)
        # Registrar request exitoso (200 o 202)
        status = result[1] if isinstance(result, tuple) else 200
        metrics.inc_api_request(endpoint, status)
        return result
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
_NAME_RE         = re.compile(r"^[a-z0-9_]{1,30}$")
_PHONE_RE        = re.compile(r"^\+?[0-9]{7,15}$")
_GROUP_NUMERIC   = re.compile(r"^\d{8,20}$")           # ID numérico Facebook
_GROUP_SLUG      = re.compile(r"^[a-zA-Z][a-zA-Z0-9._/-]{2,99}$")  # slug alfanumérico


def _validate_login_id(login_id: str) -> str | None:
    """Valida un identificador de login: correo electrónico o número de teléfono.

    Acepta:
      - Correo:    user@dominio.com
      - Teléfono:  +521234567890 o 521234567890 (7-15 dígitos, + opcional)
    Retorna None si es válido, o un mensaje de error descriptivo.
    """
    if not login_id:
        return "El correo o número de teléfono es obligatorio"
    if "@" in login_id:
        if "." not in login_id.split("@")[-1]:
            return "Correo electrónico inválido"
    else:
        if not _PHONE_RE.match(login_id):
            return (
                "Identificador inválido: ingresa un correo (user@dominio.com) "
                "o número de teléfono (ej: +521234567890)"
            )
    return None


def _is_valid_group_id(g: str) -> bool:
    """Acepta IDs numéricos (8-20 dígitos) y slugs alfanuméricos de Facebook.
    Rechaza: todos-mismo-dígito (ej: 5555555555), menos de 3 chars."""
    g = str(g).strip()
    if _GROUP_NUMERIC.match(g):
        return len(set(g)) > 1          # rechaza 5555555555555, 0000000000
    return bool(_GROUP_SLUG.match(g))


def _validate_account_input(name: str, email: str, groups: list) -> str | None:
    if not _NAME_RE.match(name):
        return "Nombre inválido: solo letras minúsculas, números y _ (máx 30)"
    err = _validate_login_id(email)
    if err:
        return err
    if groups and isinstance(groups, list):
        for g in groups:
            if not _is_valid_group_id(str(g)):
                return (
                    f"ID de grupo inválido: '{g}' — acepta números (ej: 123456789012345) "
                    "o slugs (ej: trabajosdemeridayucatan). Evita IDs de prueba como 5555555555"
                )
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


def _safe_image_paths(value) -> tuple[list[str] | None, str | None]:
    """Normaliza y valida una entrada de paths (modo JSON). Acepta:
      - list[str] (preferido, multi-imagen)
      - str       (legacy, single)
    Retorna (lista_validada, None) en éxito, o (None, error_msg) si falla.
    None o '' → ([], None). Cada path pasa por _safe_image_path().
    """
    if value is None or value == "":
        return [], None
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple)):
        candidates = [str(v) for v in value if v]
    else:
        return None, "Formato inválido para image_paths"
    if len(candidates) > _MAX_IMAGES:
        return None, f"Máximo {_MAX_IMAGES} imágenes por publicación"
    out: list[str] = []
    for raw in candidates:
        safe = _safe_image_path(raw)
        if safe is None:
            return None, "image_path inválido o fuera del directorio permitido"
        out.append(safe)
    return out, None


def _validate_template_id(template_id: str) -> bool:
    return bool(_TEMPLATE_ID_PATTERN.match(template_id))


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
        group_ids = None

        # Múltiples archivos bajo el campo 'image' (o uno solo, backward compat)
        files = [f for f in request.files.getlist("image") if f and f.filename]
        if len(files) > _MAX_IMAGES:
            return None, ({"error": f"Máximo {_MAX_IMAGES} imágenes por publicación"}, 400)
        image_paths: list[str] = []
        for f in files:
            path, err = _validate_image_upload(f)
            if err:
                return None, ({"error": err}, 400)
            image_paths.append(path)  # type: ignore[arg-type]

        # Permitir reusar imágenes ya subidas vía campo 'image_paths' JSON
        if not image_paths:
            raw_paths_field = request.form.get("image_paths")
            if raw_paths_field:
                try:
                    parsed = json.loads(raw_paths_field)
                except (ValueError, TypeError):
                    return None, ({"error": "image_paths debe ser JSON válido"}, 400)
                reused, err = _safe_image_paths(parsed)
                if err:
                    return None, ({"error": err}, 400)
                image_paths = reused or []
    else:
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        raw_accounts = data.get("accounts")
        accounts = raw_accounts if isinstance(raw_accounts, list) and raw_accounts else None
        scheduled_for = data.get("scheduled_for") or None
        callback_url = data.get("callback_url") or None

        # Acepta image_paths (lista) o image_path (legacy string)
        raw_paths = data.get("image_paths") or data.get("image_path") or None
        image_paths, err = _safe_image_paths(raw_paths)
        if err:
            return None, ({"error": err}, 400)

        # Filtro de grupos por publicación: {"account_name": ["gid1", "gid2"]}
        raw_gids = data.get("group_ids")
        group_ids = raw_gids if isinstance(raw_gids, dict) else None

    if not text:
        return None, ({"error": "Campo 'text' es obligatorio"}, 400)

    return {
        "text":          text,
        "image_paths":   image_paths,
        "accounts":      accounts,
        "scheduled_for": scheduled_for,
        "callback_url":  callback_url,
        "group_ids":     group_ids,
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


# Aliases para compatibilidad con v2_router.py y tests existentes
_filter_rate_limited_accounts = worker_core.filter_rate_limited_accounts


def _enqueue_job(job_id: str, accounts, text: str,
                 image_paths: list[str], callback_url: str | None) -> None:
    """Envía un job al pool (monoproceso) o es no-op en modo biproceso.

    image_paths: lista de rutas validadas. Se pasa la primera al worker como
    puente hasta que Bloque 4 adapte facebook_poster_async a multi-imagen.
    """
    if CONFIG.get("split_processes"):
        return  # job ya en DB con status='pending'; worker_main lo recoge
    _executor.submit(worker_core.run_job, job_id, accounts, text, image_paths, callback_url)


def shutdown_executor(wait: bool = False) -> None:
    """Detiene el pool. Invocado desde main.py en graceful shutdown.

    wait=False por default: no bloquea al signal handler; los browsers
    abiertos se cierran cuando terminen sus operaciones activas. Los
    jobs en cola se descartan (se reencolan en el próximo arranque
    por la lógica de orphan recovery de 2.4).
    """
    try:
        _executor.shutdown(wait=wait, cancel_futures=True)
    except Exception:
        logger.exception("Error cerrando ThreadPoolExecutor")


def _run_discovery(run_id: str, account_name: str) -> None:
    """Ejecuta el descubrimiento de grupos para una cuenta en un thread.

    Usa list_accounts_full() en lugar de load_accounts() para incluir cuentas
    sin grupos asignados — exactamente las que necesitan descubrir sus grupos.
    """
    try:
        rows = job_store.list_accounts_full()
        row = next((r for r in rows if r["name"] == account_name), None)
        if not row:
            job_store.fail_discovery_run(
                run_id, f"Cuenta '{account_name}' no encontrada"
            )
            return

        global_password = os.getenv("FB_PASSWORD", "").strip()
        password = global_password
        if row.get("password_enc"):
            try:
                from crypto import decrypt_password
                password = decrypt_password(row["password_enc"])
            except Exception:
                pass

        fp_raw = row.get("fingerprint_json")
        fingerprint = json.loads(fp_raw) if fp_raw else pick_fingerprint([])
        active_hours = tuple(json.loads(row.get("active_hours") or "[7, 23]"))
        groups = json.loads(row.get("groups") or "[]")

        account = AccountConfig(
            name=row["name"],
            email=row["email"],
            password=password,
            groups=groups,
            timezone=row.get("timezone") or "America/Mexico_City",
            active_hours=active_hours,
            fingerprint=fingerprint,
        )

        logger.info("[%s] Iniciando descubrimiento de grupos (run=%s)", account_name, run_id)
        groups = discover_groups_for_account(account, CONFIG)

        now = datetime.now().isoformat()
        for g in groups:
            job_store.upsert_discovered_group(
                account_name, g["id"], g["name"], now
            )

        job_store.finish_discovery_run(run_id, len(groups))
        logger.info("[%s] Descubrimiento completado: %d grupos (run=%s)",
                   account_name, len(groups), run_id)
    except Exception as e:
        logger.exception("[%s] Error en descubrimiento (run=%s)", account_name, run_id)
        job_store.fail_discovery_run(run_id, str(e))


# ===========================================================================
# ENDPOINTS OPENCLAW (todos protegidos con X-API-Key)
# ===========================================================================

@app.get("/health/detailed")
@openclaw_required
def health_detailed():
    """Healthcheck completo — requiere X-API-Key."""
    try:
        with job_store._connect() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False

    if not db_ok:
        return jsonify({"status": "degraded", "db": False}), 503

    return jsonify({
        "status": "ok",
        "db": True,
        "uptime_s": int(time.time() - _START_TIME),
        "active_accounts": job_store.count_active_accounts(),
        "banned_accounts": len(job_store.list_active_bans()),
        "jobs_by_status": job_store.count_jobs_by_status(),
        "recent_bans": job_store.list_recent_bans(limit=10),
    })


@app.get("/metrics")
def prometheus_metrics():
    """Endpoint Prometheus para Fase 3.3b — requiere METRICS_ENABLED=1."""
    if not CONFIG.get("metrics_enabled"):
        return "", 404
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


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

    accounts, err = _resolve_accounts(payload["accounts"])
    if err:
        return jsonify(err[0]), err[1]

    filtered = apply_group_filter(accounts, payload["group_ids"])

    job_id = job_store.create_job(
        text=payload["text"],
        accounts=payload["accounts"],
        image_paths=payload["image_paths"],
        callback_url=payload["callback_url"],
        job_type="immediate",
        group_ids=payload["group_ids"],
    )

    logger.info("Job %s aceptado | cuentas=%s", job_id,
                [a.name for a in filtered])

    _enqueue_job(job_id, filtered, payload["text"],
                 payload["image_paths"], payload["callback_url"])

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

    from datetime import timezone as _tz
    now_cmp = datetime.now(_tz.utc) if when.tzinfo else datetime.now()
    if when <= now_cmp:
        return jsonify({"error": "'scheduled_for' debe ser en el futuro"}), 400

    _, err = _resolve_accounts(payload["accounts"])
    if err:
        return jsonify(err[0]), err[1]

    job_id = job_store.create_job(
        text=payload["text"],
        accounts=payload["accounts"],
        image_paths=payload["image_paths"],
        callback_url=payload["callback_url"],
        job_type="scheduled",
        scheduled_for=when,
        group_ids=payload["group_ids"],
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
    # [FIX P1-4] Rate limiting en login — previene brute force contra ADMIN_KEY
    ip = request.remote_addr or "unknown"
    if job_store.is_rate_limited(ip, "admin_login", _RATE_LIMIT, _RATE_WINDOW):
        logger.warning("Rate limit en /admin/login para %s", ip)
        if request.is_json:
            return jsonify({"error": "Demasiados intentos, espera un momento"}), 429
        return render_template("admin_login.html", error="Demasiados intentos, espera un momento"), 429

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
    rows = job_store.list_accounts_full()
    # Transformar: no exponer el token cifrado al frontend,
    # solo indicar si la cuenta tiene una contraseña distinta a la principal.
    for r in rows:
        r["has_custom_password"] = bool(r.pop("password_enc", None))
        groups_list = json.loads(r.get("groups") or "[]")
        r["groups"] = groups_list
        r["has_groups"] = len(groups_list) > 0
    return jsonify(rows)


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


@app.post("/admin/api/accounts/<name>/password")
@admin_required
def admin_set_account_password(name: str):
    """
    Establece o elimina la contraseña individual de una cuenta.

    - Body {"password": "<texto>"} → cifra y guarda (cuenta usará ésta)
    - Body {"password": null} o {"password": ""} → borra la individual;
      la cuenta vuelve a usar FB_PASSWORD (contraseña principal)

    La contraseña nunca se loguea ni se retorna en claro.
    El 98% de las cuentas usa la contraseña principal — solo configura
    una individual para cuentas con credenciales distintas.
    """
    data = request.get_json(silent=True) or {}
    plain = (data.get("password") or "").strip()

    # --- Limpiar: restaurar contraseña principal global ---
    if not plain:
        found = job_store.clear_account_password(name)
        if not found:
            return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404
        logger.info("Password individual eliminado para '%s' — usará FB_PASSWORD", name)
        return jsonify({
            "status": "reset_to_default",
            "account": name,
            "message": "La cuenta ahora usa la contraseña principal (FB_PASSWORD)",
        })

    # --- Establecer contraseña individual ---
    if not _CRYPTO_AVAILABLE or _encrypt_password is None:
        return jsonify({
            "error": "Módulo de cifrado no disponible. "
                     "Instala: pip install cryptography"
        }), 503

    if len(plain) < 6:
        return jsonify({"error": "La contraseña debe tener al menos 6 caracteres"}), 400
    if len(plain) > 256:
        return jsonify({"error": "La contraseña no puede superar 256 caracteres"}), 400

    try:
        encrypted = _encrypt_password(plain)
    except Exception as exc:
        logger.error("Error cifrando password para '%s': %s", name, exc)
        return jsonify({"error": "Error interno al cifrar la contraseña"}), 500

    if not job_store.set_account_password(name, encrypted):
        return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404

    logger.info("Password individual configurado para cuenta '%s'", name)
    return jsonify({"status": "updated", "account": name})


# ===========================================================================
# ADMIN API — Login manual de cuenta
# ===========================================================================

def _run_login(run_id: str, account_name: str) -> None:
    """Abre Chromium para la cuenta indicada y hace login guardando la cookie.

    Corre en un thread daemon. Usa FacebookPosterAsync + asyncio.run() porque
    fase-3 eliminó el poster sync; el thread no tiene event loop propio.
    Lee directo de list_accounts_full() para incluir cuentas sin grupos.
    """
    _login_runs[run_id] = {"status": "running", "message": "Iniciando…", "account": account_name}

    try:
        rows = job_store.list_accounts_full()
        row = next((r for r in rows if r["name"] == account_name), None)
        if not row:
            _login_runs[run_id] = {
                "status": "error",
                "message": f"Cuenta '{account_name}' no encontrada",
                "account": account_name,
            }
            return

        global_password = os.getenv("FB_PASSWORD", "").strip()
        password = global_password
        if row.get("password_enc"):
            try:
                from crypto import decrypt_password
                password = decrypt_password(row["password_enc"])
            except Exception:
                pass

        fp_raw = row.get("fingerprint_json")
        fingerprint = json.loads(fp_raw) if fp_raw else pick_fingerprint([])
        active_hours = tuple(json.loads(row.get("active_hours") or "[7, 23]"))
        groups = json.loads(row.get("groups") or "[]")

        account = AccountConfig(
            name=row["name"],
            email=row["email"],
            password=password,
            groups=groups,
            timezone=row.get("timezone") or "America/Mexico_City",
            active_hours=active_hours,
            fingerprint=fingerprint,
        )

        from facebook_poster_async import FacebookPosterAsync
        _login_runs[run_id]["message"] = "Abriendo navegador…"
        poster = FacebookPosterAsync(account, CONFIG)
        try:
            _login_runs[run_id]["message"] = "Iniciando sesión en Facebook…"
            success = asyncio.run(poster.login())
            if success:
                _login_runs[run_id] = {
                    "status": "done",
                    "message": "Sesión iniciada correctamente. Cookie guardada.",
                    "account": account_name,
                }
                logger.info("[Login manual] '%s' — sesión iniciada OK (run=%s)", account_name, run_id)
            else:
                _login_runs[run_id] = {
                    "status": "error",
                    "message": "Login fallido. Verifica email/contraseña o que la cuenta no esté bloqueada.",
                    "account": account_name,
                }
                logger.warning("[Login manual] '%s' — login fallido (run=%s)", account_name, run_id)
        finally:
            asyncio.run(poster.close())

    except Exception as exc:
        logger.exception("[Login manual] '%s' — error inesperado (run=%s)", account_name, run_id)
        _login_runs[run_id] = {
            "status": "error",
            "message": str(exc),
            "account": account_name,
        }


@app.post("/admin/api/accounts/<name>/login")
@admin_required
def admin_trigger_login(name: str):
    """Inicia sesión en Facebook para la cuenta indicada (crea/renueva cookie).

    Retorna {run_id, status: 'running'} para polling.
    """
    if not any(r["name"] == name for r in job_store.list_accounts_full()):
        return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404

    run_id = uuid.uuid4().hex[:12]
    _login_runs[run_id] = {"status": "running", "message": "En cola…", "account": name}

    threading.Thread(
        target=_run_login,
        args=(run_id, name),
        daemon=True,
        name=f"login-{name}",
    ).start()

    logger.info("[Login manual] Iniciado para '%s' (run=%s)", name, run_id)
    return jsonify({"run_id": run_id, "status": "running"}), 202


@app.get("/admin/api/accounts/<name>/login/<run_id>")
@admin_required
def admin_login_status(name: str, run_id: str):
    """Polling del estado de un login manual."""
    run = _login_runs.get(run_id)
    if not run:
        return jsonify({"error": "run_id no encontrado"}), 404
    return jsonify(run), 200


# ===========================================================================
# ADMIN API — Descubrimiento de grupos (Fase 2.10)
# ===========================================================================

@app.post("/admin/api/accounts/<name>/discover-groups")
@admin_required
def admin_trigger_discovery(name: str):
    """
    Inicia descubrimiento automático de grupos para una cuenta.
    Retorna {run_id, status: "running"} para polling.
    """
    if not any(r["name"] == name for r in job_store.list_accounts_full()):
        return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404

    run_id = uuid.uuid4().hex[:12]
    job_store.create_discovery_run(run_id, name)

    # Lanzar en thread daemon
    threading.Thread(
        target=_run_discovery,
        args=(run_id, name),
        daemon=True,
        name=f"discovery-{name}",
    ).start()

    logger.info("Descubrimiento iniciado para '%s' (run=%s)", name, run_id)
    return jsonify({"run_id": run_id, "status": "running"}), 202


@app.get("/admin/api/discovery/<run_id>")
@admin_required
def admin_discovery_status(run_id: str):
    """Polling del estado de un descubrimiento."""
    run = job_store.get_discovery_run(run_id)
    if not run:
        return jsonify({"error": "run_id no encontrado"}), 404
    return jsonify(run), 200


@app.get("/admin/api/accounts/<name>/discovered-groups")
@admin_required
def admin_list_discovered_groups(name: str):
    """Lista grupos descubiertos para una cuenta."""
    if not any(r["name"] == name for r in job_store.list_accounts_full()):
        return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404

    groups = job_store.list_discovered_groups(name)
    return jsonify({
        "account": name,
        "groups": groups,
        "total": len(groups),
        "pending": sum(1 for g in groups if not g["added_to_posting"]),
    }), 200


@app.post("/admin/api/accounts/<name>/discovered-groups/<group_id>/add")
@admin_required
def admin_add_discovered_group(name: str, group_id: str):
    """
    Añade un grupo descubierto a la lista activa de publicación de la cuenta.
    Actualiza tanto discovered_groups.added_to_posting como accounts.groups.
    """
    # Obtener los detalles actuales de la cuenta
    rows = job_store.list_accounts_full()
    account = next((r for r in rows if r["name"] == name), None)
    if not account:
        return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404

    # Obtener lista de grupos actual
    groups = json.loads(account.get("groups") or "[]")

    # Añadir si no está ya en la lista
    if group_id not in groups:
        groups.append(group_id)
        # Actualizar account con nuevo grupo
        job_store.update_account(
            name,
            account["email"],
            groups,
        )
        logger.info("Grupo %s añadido a la lista de '%s'", group_id, name)

    # Marcar como added_to_posting en la tabla de descubrimiento
    job_store.mark_group_added_to_posting(name, group_id)

    return jsonify({
        "status": "added",
        "account": name,
        "group_id": group_id,
    }), 200


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


@app.get("/admin/api/bans")
@admin_required
def admin_list_bans():
    """Lista cuentas en cooldown y últimos eventos de ban."""
    limit = min(int(request.args.get("limit", "50")), 200)
    return jsonify({
        "active_cooldowns": job_store.list_active_bans(),
        "recent_events": job_store.list_recent_bans(limit),
    })


@app.post("/admin/api/bans/<name>/clear")
@admin_required
def admin_clear_ban(name: str):
    """Levanta el cooldown de una cuenta y marca sus bans como reviewed."""
    ok = job_store.clear_ban(name)
    if not ok:
        return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404
    return jsonify({"ok": True, "account": name})


@app.get("/admin/api/selector-repairs")
@admin_required
def admin_list_selector_repairs():
    """Lista reparaciones de selectores pendientes (Fase 3.4)."""
    return jsonify(job_store.list_pending_repairs())


@app.post("/admin/api/selector-repairs/<repair_id>/approve")
@admin_required
def admin_approve_repair(repair_id: str):
    if not job_store.approve_repair(repair_id):
        return jsonify({"error": "No encontrado"}), 404
    return jsonify({"status": "approved"})


@app.post("/admin/api/selector-repairs/<repair_id>/reject")
@admin_required
def admin_reject_repair(repair_id: str):
    if not job_store.reject_repair(repair_id):
        return jsonify({"error": "No encontrado"}), 404
    return jsonify({"status": "rejected"})


@app.get("/admin/api/proxies")
@admin_required
def admin_list_proxies():
    """Lista nodos proxy + asignaciones de cuentas."""
    nodes = job_store.list_proxy_nodes()
    assignments = job_store.list_proxy_assignments()
    return jsonify({"nodes": nodes, "assignments": assignments})


@app.post("/admin/api/proxies")
@admin_required
def admin_create_proxy():
    """Registra un nuevo nodo proxy (teléfono SIM)."""
    data = request.get_json(silent=True) or {}
    node_id = (data.get("id") or "").strip()
    label   = (data.get("label") or "").strip()
    server  = (data.get("server") or "").strip()
    notes   = (data.get("notes") or "").strip()

    if not node_id or not label or not server:
        return jsonify({"error": "Campos requeridos: id, label, server"}), 400

    if not re.match(r'^[a-z0-9_]{1,40}$', node_id):
        return jsonify({"error": "id debe ser [a-z0-9_]{1,40}"}), 400

    if not server.startswith(("socks5://", "http://", "https://")):
        return jsonify({"error": "server debe comenzar con socks5://, http:// o https://"}), 400

    job_store.upsert_proxy_node(node_id, label, server, notes)
    logger.info("Nodo proxy '%s' registrado: %s", node_id, server)
    return jsonify({"ok": True, "id": node_id}), 201


@app.delete("/admin/api/proxies/<node_id>")
@admin_required
def admin_delete_proxy(node_id: str):
    """Elimina un nodo proxy."""
    deleted = job_store.delete_proxy_node(node_id)
    if not deleted:
        return jsonify({"error": f"Nodo '{node_id}' no encontrado"}), 404
    return "", 204


@app.put("/admin/api/proxies/<node_id>/status")
@admin_required
def admin_set_proxy_status(node_id: str):
    """Cambia el status de un nodo manualmente (maintenance, online, etc.)."""
    data   = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in ("online", "offline", "maintenance"):
        return jsonify({"error": "status debe ser: online | offline | maintenance"}), 400
    node = job_store.get_proxy_node(node_id)
    if not node:
        return jsonify({"error": f"Nodo '{node_id}' no encontrado"}), 404
    job_store.update_proxy_node_status(node_id, status=status, reset_fails=(status == "online"))
    return jsonify({"ok": True, "id": node_id, "status": status})


@app.post("/admin/api/accounts/<name>/proxy")
@admin_required
def admin_assign_proxy(name: str):
    """Asigna un proxy (manual) o ejecuta asignación automática a una cuenta."""
    data      = request.get_json(silent=True) or {}
    primary   = (data.get("primary_node") or "").strip() or None
    secondary = (data.get("secondary_node") or "").strip() or None
    auto      = data.get("auto", False)

    accounts = job_store.list_accounts_full()
    account  = next((a for a in accounts if a["name"] == name), None)
    if not account:
        return jsonify({"error": f"Cuenta '{name}' no encontrada"}), 404

    if auto:
        try:
            groups = json.loads(account.get("groups") or "[]")
        except Exception:
            groups = []
        assigned = proxy_manager.assign_proxy_to_account(name, groups, secondary)
        if not assigned:
            return jsonify({"error": "Sin nodos disponibles para asignar"}), 409
        return jsonify({"ok": True, "account": name, "primary_node": assigned})

    if not primary:
        return jsonify({"error": "Proveer 'primary_node' o usar 'auto': true"}), 400

    if not job_store.get_proxy_node(primary):
        return jsonify({"error": f"Nodo '{primary}' no existe"}), 400

    if secondary and not job_store.get_proxy_node(secondary):
        return jsonify({"error": f"Nodo secundario '{secondary}' no existe"}), 400

    job_store.set_proxy_assignment(name, primary, secondary)
    return jsonify({"ok": True, "account": name, "primary_node": primary, "secondary_node": secondary})


@app.delete("/admin/api/accounts/<name>/proxy")
@admin_required
def admin_remove_proxy_assignment(name: str):
    """Elimina la asignación de proxy de una cuenta."""
    deleted = job_store.delete_proxy_assignment(name)
    if not deleted:
        return jsonify({"error": "Sin asignación para esta cuenta"}), 404
    return "", 204


@app.get("/admin/api/queue")
@admin_required
def admin_queue_status():
    """Estado del pool de workers en tiempo real."""
    base = {
        "max_workers": CONFIG.get("max_concurrent_workers", 2),
        "pending_jobs": job_store.count_pending_jobs(),
        "jobs_by_status": job_store.count_jobs_by_status(),
    }
    if CONFIG.get("split_processes"):
        # En modo biproceso la memoria del proceso API no refleja ejecuciones
        # del worker — leer estado real desde la BD.
        base["accounts_in_progress"] = []
        base["worker_mode"] = "external"
    else:
        base["accounts_in_progress"] = worker_core.running_accounts_snapshot()
    return jsonify(base)


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

    accounts, err = _resolve_accounts(payload["accounts"])
    if err:
        return jsonify(err[0]), err[1]

    filtered = apply_group_filter(accounts, payload["group_ids"])

    job_id = job_store.create_job(
        text=payload["text"],
        accounts=payload["accounts"],
        image_paths=payload["image_paths"],
        callback_url=None,
        job_type="immediate",
        group_ids=payload["group_ids"],
    )

    logger.info("Job %s aceptado via admin | cuentas=%s", job_id,
                [a.name for a in filtered])

    _enqueue_job(job_id, filtered, payload["text"], payload["image_paths"], None)

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

    # [FIX P1-1] Comparar aware vs naive correctamente para evitar TypeError
    from datetime import timezone as _tz
    now_cmp = datetime.now(_tz.utc) if when.tzinfo else datetime.now()
    if when <= now_cmp:
        return jsonify({"error": "'scheduled_for' debe ser en el futuro"}), 400

    _, err = _resolve_accounts(payload["accounts"])
    if err:
        return jsonify(err[0]), err[1]

    job_id = job_store.create_job(
        text=payload["text"],
        accounts=payload["accounts"],
        image_paths=payload["image_paths"],
        callback_url=None,
        job_type="scheduled",
        scheduled_for=when,
        group_ids=payload["group_ids"],
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


# ===========================================================================
# ADMIN API — Upload de imágenes (independiente del flujo de publicación)
# ===========================================================================

@app.post("/admin/api/upload-images")
@admin_required
def admin_upload_images():
    """Sube 1–5 imágenes y devuelve sus rutas seguras.

    No crea jobs — solo persiste los archivos en UPLOAD_DIR para que el
    cliente las incluya luego en una plantilla o publicación.
    """
    files = [f for f in request.files.getlist("image") if f and f.filename]
    if not files:
        return jsonify({"error": "No se enviaron archivos"}), 400
    if len(files) > _MAX_IMAGES:
        return jsonify({"error": f"Máximo {_MAX_IMAGES} imágenes por subida"}), 400

    saved_paths: list[str] = []
    for f in files:
        path, err = _validate_image_upload(f)
        if err:
            return jsonify({"error": err}), 400
        saved_paths.append(path)  # type: ignore[arg-type]

    return jsonify({"image_paths": saved_paths}), 201


@app.get("/admin/uploaded-images/<path:filename>")
@admin_required
def admin_serve_uploaded_image(filename: str):
    """Sirve una imagen de UPLOAD_DIR para previews del panel admin.

    Verifica path traversal: solo devuelve archivos dentro de UPLOAD_DIR.
    """
    safe = _safe_image_path(str(UPLOAD_DIR / filename))
    if not safe:
        return jsonify({"error": "Imagen no encontrada"}), 404
    return send_from_directory(str(UPLOAD_DIR), Path(safe).name)


# ===========================================================================
# ADMIN API — Plantillas CRUD
# ===========================================================================

@app.get("/admin/api/templates")
@admin_required
def admin_list_templates():
    templates = job_store.list_templates()
    return jsonify(templates), 200


@app.post("/admin/api/templates")
@admin_required
def admin_create_template():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    text = (data.get("text") or "").strip()

    if not name or len(name) < 2 or len(name) > MAX_TEMPLATE_NAME_CHARS:
        return jsonify({"error": f"El nombre debe tener entre 2 y {MAX_TEMPLATE_NAME_CHARS} caracteres"}), 400
    if not text or len(text) < MIN_TEMPLATE_TEXT_CHARS:
        return jsonify({"error": f"El texto debe tener entre {MIN_TEMPLATE_TEXT_CHARS} y {MAX_TEMPLATE_TEXT_CHARS} caracteres"}), 400
    if len(text) > MAX_TEMPLATE_TEXT_CHARS:
        return jsonify({"error": f"El texto no puede exceder {MAX_TEMPLATE_TEXT_CHARS} caracteres"}), 400

    raw_paths = data.get("image_paths") if data.get("image_paths") is not None else data.get("image_path")
    image_paths, err = _safe_image_paths(raw_paths)
    if err:
        return jsonify({"error": err}), 400

    try:
        template_id = job_store.create_template(name, text, "", image_paths)
        logger.info("Plantilla '%s' creada (id=%s) via admin", name, template_id)
        return jsonify({"status": "created", "id": template_id, "name": name}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": f"Ya existe una plantilla con el nombre '{name}'"}), 409
    except Exception:
        logger.exception("Error creando plantilla (nombre='%s')", name)
        return jsonify({"error": "Error interno al crear plantilla"}), 500


@app.get("/admin/api/templates/<template_id>")
@admin_required
def admin_get_template(template_id: str):
    if not _validate_template_id(template_id):
        return jsonify({"error": "ID de plantilla inválido"}), 400
    template = job_store.get_template(template_id)
    if not template:
        return jsonify({"error": "Plantilla no encontrada"}), 404
    return jsonify(template), 200


@app.put("/admin/api/templates/<template_id>")
@admin_required
def admin_update_template(template_id: str):
    if not _validate_template_id(template_id):
        return jsonify({"error": "ID de plantilla inválido"}), 400
    if not job_store.get_template(template_id):
        return jsonify({"error": "Plantilla no encontrada"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or None
    text = (data.get("text") or "").strip() or None

    if name is not None and (len(name) < 2 or len(name) > MAX_TEMPLATE_NAME_CHARS):
        return jsonify({"error": f"El nombre debe tener entre 2 y {MAX_TEMPLATE_NAME_CHARS} caracteres"}), 400
    if text is not None and (len(text) < MIN_TEMPLATE_TEXT_CHARS or len(text) > MAX_TEMPLATE_TEXT_CHARS):
        return jsonify({"error": f"El texto debe tener entre {MIN_TEMPLATE_TEXT_CHARS} y {MAX_TEMPLATE_TEXT_CHARS} caracteres"}), 400

    image_paths: list[str] | None = None
    if "image_paths" in data or "image_path" in data:
        raw_paths = data.get("image_paths") if data.get("image_paths") is not None else data.get("image_path")
        image_paths, err = _safe_image_paths(raw_paths)
        if err:
            return jsonify({"error": err}), 400

    try:
        success = job_store.update_template(template_id, name, text, None, image_paths)
        if not success:
            return jsonify({"error": "No hay campos para actualizar"}), 400
        logger.info("Plantilla '%s' actualizada via admin", template_id)
        return jsonify({"status": "updated", "id": template_id}), 200
    except sqlite3.IntegrityError:
        return jsonify({"error": f"Ya existe una plantilla con el nombre '{name}'"}), 409
    except Exception:
        logger.exception("Error actualizando plantilla (id='%s')", template_id)
        return jsonify({"error": "Error interno al actualizar plantilla"}), 500


@app.delete("/admin/api/templates/<template_id>")
@admin_required
def admin_delete_template(template_id: str):
    if not _validate_template_id(template_id):
        return jsonify({"error": "ID de plantilla inválido"}), 400
    if job_store.delete_template(template_id):
        logger.info("Plantilla '%s' eliminada via admin", template_id)
        return "", 204
    return jsonify({"error": "Plantilla no encontrada"}), 404
