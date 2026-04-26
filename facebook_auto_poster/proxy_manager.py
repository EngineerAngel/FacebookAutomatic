"""
proxy_manager.py — Pool de SIM hotspots con health checking y fallback automático.

Cada teléfono Android conectado por USB actúa como gateway SOCKS5.
El health checker corre como daemon thread y actualiza proxy_nodes en DB cada 2 min.
"""

import json
import logging
import threading
import time
from datetime import datetime

import requests

import job_store

logger = logging.getLogger("proxy_manager")

CHECK_INTERVAL_S = 120   # chequear cada 2 minutos
FAIL_THRESHOLD   = 3     # 3 fallos consecutivos → offline

_started = False
_lock    = threading.Lock()
_assign_lock = threading.Lock()

# Cache de proxies asignados (TTL 30s)
_proxy_cache: dict[str, tuple[dict, float]] = {}
_PROXY_CACHE_TTL_S = 30


# ---------------------------------------------------------------------------
# Health checking
# ---------------------------------------------------------------------------

def _check_node(node: dict) -> tuple[bool, str]:
    """Verifica conectividad del nodo con validaciones robustas. Retorna (ok, ip_publica)."""
    server = node["server"]
    try:
        proxies = {"http": server, "https": server}
        resp = requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=10,
        )

        # Validar status HTTP
        if resp.status_code != 200:
            logger.debug("[ProxyCheck] %s retornó HTTP %d", node["id"], resp.status_code)
            return False, ""

        # Validar que sea JSON válido
        try:
            data = resp.json()
            ip = data.get("ip", "")
            if not ip:
                logger.debug("[ProxyCheck] %s sin IP en respuesta", node["id"])
                return False, ""
            return True, ip
        except Exception as json_err:
            logger.warning("[ProxyCheck] %s respuesta no es JSON: %s", node["id"], json_err)
            return False, ""

    except requests.Timeout:
        logger.debug("[ProxyCheck] %s timeout (10s)", node["id"])
        return False, ""
    except requests.ConnectionError:
        logger.debug("[ProxyCheck] %s error de conexión", node["id"])
        return False, ""
    except Exception as exc:
        logger.exception("[ProxyCheck] %s error inesperado", node["id"])
        return False, ""


def _run_health_checker() -> None:
    """Hilo daemon — verifica todos los nodos cada CHECK_INTERVAL_S segundos."""
    logger.info("[ProxyHealth] Daemon iniciado (intervalo=%ds)", CHECK_INTERVAL_S)
    while True:
        try:
            nodes = job_store.list_proxy_nodes()
            if not nodes:
                logger.debug("[ProxyHealth] Sin nodos configurados")
            for node in nodes:
                ok, ip = _check_node(node)
                if ok:
                    job_store.update_proxy_node_status(
                        node["id"],
                        status="online",
                        last_ip=ip,
                        reset_fails=True,
                    )
                    if node["status"] != "online":
                        logger.info("[Proxy] Nodo %s recuperado — IP: %s", node["id"], ip)
                else:
                    fails = (node["check_fail_count"] or 0) + 1
                    new_status = "offline" if fails >= FAIL_THRESHOLD else node["status"]
                    job_store.update_proxy_node_status(
                        node["id"],
                        status=new_status,
                        fail_count=fails,
                    )
                    if new_status == "offline" and node["status"] != "offline":
                        logger.warning(
                            "[Proxy] Nodo %s OFFLINE tras %d fallos consecutivos",
                            node["id"], fails,
                        )
                        _alert_node_down(node)
        except Exception:
            logger.exception("[ProxyHealth] Error en ciclo de chequeo")
        time.sleep(CHECK_INTERVAL_S)


def _alert_node_down(node: dict) -> None:
    """Log crítico + guardar alerta en BD para dashboard."""
    try:
        accounts = job_store.get_accounts_for_node(node["id"])
        names = [a["name"] for a in accounts]
    except Exception as e:
        logger.error("Error obteniendo cuentas para nodo %s: %s", node["id"], e)
        names = []

    logger.critical(
        "[ALERTA] Proxy caído: %s (%s) — cuentas afectadas: %s",
        node["label"], node["server"], ", ".join(names) if names else "ninguna",
    )

    # Guardar en BD si existe tabla de alertas
    try:
        if names:
            alert_msg = f"Nodo proxy {node['label']} offline. Cuentas: {', '.join(names)}"
            job_store.create_system_alert(alert_msg, severity="critical")
    except Exception as e:
        logger.debug("Error creando alerta (puede no existir tabla): %s", e)


def start() -> None:
    """Arranca el health checker como daemon thread (idempotente)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(
        target=_run_health_checker,
        daemon=True,
        name="proxy-health-checker",
    )
    thread.start()


# ---------------------------------------------------------------------------
# Resolución de proxy para una cuenta
# ---------------------------------------------------------------------------

def resolve_proxy(account_name: str, force_refresh: bool = False) -> dict | None:
    """
    Retorna dict proxy con validación reciente.

    Prioridad:
    1. Cache (si TTL válido)
    2. Nodo primario si está online
    3. Nodo secundario si primario está offline
    4. Fallback de emergencia (cualquier nodo online)
    5. None si sin nodos disponibles
    """
    now = time.time()

    # Usar cache si es reciente
    if not force_refresh and account_name in _proxy_cache:
        proxy, ts = _proxy_cache[account_name]
        if now - ts < _PROXY_CACHE_TTL_S:
            return proxy

    assignment = job_store.get_proxy_assignment(account_name)
    if not assignment:
        logger.debug("[Proxy] Sin asignación para '%s'", account_name)
        return None

    candidates = [assignment["primary_node"], assignment.get("secondary_node")]

    for node_id in candidates:
        if not node_id:
            continue
        node = job_store.get_proxy_node(node_id)
        if node and node["status"] == "online":
            # Validación: ¿health checker lo vio online hace poco?
            last_checked = node.get("last_checked")
            if last_checked:
                try:
                    last_ts = datetime.fromisoformat(last_checked).timestamp()
                    if now - last_ts > 180:  # > 3 minutos, validar rápido
                        ok, ip = _check_node(node)
                        if not ok:
                            logger.warning("[Proxy] %s offline (validación rápida)", node_id)
                            continue
                except Exception:
                    pass  # Usar valor del DB si hay error en parsing

            proxy = {"server": node["server"]}
            _proxy_cache[account_name] = (proxy, now)
            logger.info(
                "[Proxy] '%s' → nodo %s (%s)",
                account_name, node_id, node.get("last_seen_ip", "?"),
            )
            return proxy

    # Fallback de emergencia
    fallback = job_store.get_any_online_proxy_node(exclude_nodes=candidates)
    if fallback:
        logger.warning(
            "[Proxy] '%s' fallback → %s (IP compartida temporalmente)",
            account_name, fallback["id"],
        )
        proxy = {"server": fallback["server"]}
        _proxy_cache[account_name] = (proxy, now)
        return proxy

    logger.error("[Proxy] Sin nodos disponibles para '%s'", account_name)
    return None


# ---------------------------------------------------------------------------
# Asignación automática de cuentas a nodos
# ---------------------------------------------------------------------------

def assign_proxy_to_account(
    account_name: str,
    account_groups: list[str],
    secondary_node: str | None = None,
) -> str | None:
    """
    Asigna proxy con transacción atómica (lock).
    Asigna el nodo con menos cuentas que no comparta grupos.
    Retorna node_id asignado, o None si sin nodos disponibles.
    """
    with _assign_lock:
        nodes = job_store.list_proxy_nodes()
        if not nodes:
            logger.warning("[Proxy] Sin nodos en pool para asignar a '%s'", account_name)
            return None

        # Validar que secondary_node exista
        if secondary_node:
            if not job_store.get_proxy_node(secondary_node):
                logger.error("[Proxy] Nodo secundario '%s' no existe", secondary_node)
                secondary_node = None

        best_node: str | None = None
        best_score: float = float("inf")

        for node in nodes:
            if node["status"] == "offline":
                continue
            existing = job_store.get_accounts_for_node(node["id"])
            existing_groups: set[str] = set()
            for acc in existing:
                try:
                    groups = json.loads(acc.get("groups") or "[]")
                    existing_groups.update(groups)
                except Exception:
                    pass

            overlap = len(set(account_groups) & existing_groups)
            count   = len(existing)
            # Penalizar fuertemente el solapamiento de grupos con el mismo nodo
            score = overlap * 100 + count
            if score < best_score:
                best_score = score
                best_node  = node["id"]

        if best_node:
            job_store.set_proxy_assignment(account_name, best_node, secondary_node)
            logger.info(
                "[Proxy] '%s' asignado a %s (score=%.0f)",
                account_name, best_node, best_score,
            )
            return best_node

    logger.error("[Proxy] Sin nodos disponibles para asignar a '%s'", account_name)
    return None
