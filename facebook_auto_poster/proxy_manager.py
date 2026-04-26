"""
proxy_manager.py — Pool de SIM hotspots con health checking y fallback automático.

Cada teléfono Android conectado por USB actúa como gateway SOCKS5.
El health checker corre como daemon thread y actualiza proxy_nodes en DB cada 2 min.
"""

import json
import logging
import threading
import time

import requests

import job_store

logger = logging.getLogger("proxy_manager")

CHECK_INTERVAL_S = 120   # chequear cada 2 minutos
FAIL_THRESHOLD   = 3     # 3 fallos consecutivos → offline

_started = False
_lock    = threading.Lock()


# ---------------------------------------------------------------------------
# Health checking
# ---------------------------------------------------------------------------

def _check_node(node: dict) -> tuple[bool, str]:
    """Verifica conectividad del nodo. Retorna (ok, ip_publica)."""
    server = node["server"]
    try:
        proxies = {"http": server, "https": server}
        resp = requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=10,
        )
        if resp.status_code == 200:
            ip = resp.json().get("ip", "")
            return True, ip
    except Exception as exc:
        logger.debug("[ProxyCheck] %s falló: %s", node["id"], exc)
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
    """Log crítico + lista de cuentas afectadas cuando un nodo cae."""
    accounts = job_store.get_accounts_for_node(node["id"])
    names = [a["name"] for a in accounts]
    logger.critical(
        "[ALERTA] Proxy caído: %s (%s) — cuentas afectadas: %s",
        node["label"], node["server"], names,
    )


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

def resolve_proxy(account_name: str) -> dict | None:
    """
    Retorna el dict de proxy para Playwright: {"server": "socks5://..."}.

    Prioridad:
    1. Nodo primario asignado si está online.
    2. Nodo secundario si el primario está offline.
    3. Cualquier nodo online como emergencia (riesgo aceptable temporal).
    4. None → sin proxy disponible (caller decide si reintentar o continuar sin proxy).
    """
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
            logger.info(
                "[Proxy] '%s' → nodo %s (%s)",
                account_name, node_id, node.get("last_seen_ip", "?"),
            )
            return {"server": node["server"]}

    # Emergencia: cualquier nodo online
    fallback = job_store.get_any_online_proxy_node(exclude_nodes=candidates)
    if fallback:
        logger.warning(
            "[Proxy] '%s' usando fallback de emergencia: %s — "
            "cuentas de distintos grupos compartirán IP temporalmente",
            account_name, fallback["id"],
        )
        return {"server": fallback["server"]}

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
    Asigna el nodo con menos cuentas que no comparta grupos con esta cuenta.
    Retorna el node_id asignado, o None si no hay nodos disponibles.
    """
    nodes = job_store.list_proxy_nodes()
    if not nodes:
        logger.warning("[Proxy] No hay nodos en el pool para asignar a '%s'", account_name)
        return None

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
            "[Proxy] '%s' asignado a nodo %s (score=%.0f)",
            account_name, best_node, best_score,
        )

    return best_node
