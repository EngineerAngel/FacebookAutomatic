"""
proxy_manager.py — Pool de SIM hotspots con asignación dinámica LRU.

Cada teléfono Android conectado por USB actúa como gateway SOCKS5/HTTP.

Flujo de asignación dinámica (resolve_proxy):
  1. Si la cuenta tiene proxy asignado y el nodo está online → usarlo
  2. Si la cuenta NO tiene proxy → buscar nodo con capacidad libre → asignar
  3. Si todos los nodos están llenos → expulsar la cuenta LRU (la que lleva más
     tiempo sin publicar) del nodo con más holgura → asignar al solicitante
  4. Actualizar last_used_at en cada uso para mantener el LRU fresco

MAX_ACCOUNTS_PER_NODE controla la capacidad por teléfono. Cuando se agrega
un segundo teléfono, las cuentas que superen la capacidad del primero migran
automáticamente en la siguiente llamada a resolve_proxy.
"""

import json
import logging
import re
import subprocess
import threading
import time
from datetime import datetime

import requests

import job_store

logger = logging.getLogger("proxy_manager")

CHECK_INTERVAL_S    = 120   # health check cada 2 minutos
FAIL_THRESHOLD      = 3     # fallos consecutivos → offline
MAX_ACCOUNTS_PER_NODE = 10  # capacidad máxima por nodo (ajustar según el pool)
NODE_COOLDOWN_S     = 120   # min segundos entre logins de cuentas distintas en el mismo nodo
NODE_COOLDOWN_MAX_WAIT_S = 180  # tope de espera para no bloquear indefinidamente

_started     = False
_lock        = threading.Lock()
_assign_lock = threading.Lock()
_cooldown_lock = threading.Lock()

# Cache de proxies resueltos (TTL 30s)
_proxy_cache: dict[str, tuple[dict, float]] = {}
_PROXY_CACHE_TTL_S = 30


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
        if resp.status_code != 200:
            return False, ""
        data = resp.json()
        ip = data.get("ip", "")
        return (bool(ip), ip)
    except requests.Timeout:
        return False, ""
    except requests.ConnectionError:
        return False, ""
    except Exception:
        logger.exception("[ProxyCheck] %s error inesperado", node["id"])
        return False, ""


def _ensure_usb_never_default() -> None:
    """Protege las interfaces USB contra consumo de datos del SIM.

    Verifica que todas las interfaces de tethering USB (enx*, usb*, rndis*,
    enu*) tengan never-default=yes en NetworkManager y bloquea DNS por ellas.
    Se ejecuta al arrancar y cada CHECK_INTERVAL_S durante la operacion.
    Solo loguea si encuentra y corrige un problema.
    """
    try:
        out = subprocess.run(
            ["ip", "-br", "link", "show"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return

    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        iface = parts[0].rstrip("@")
        state = parts[1]
        if not re.match(r"^(usb|rndis|enx|enu)", iface):
            continue
        if state not in ("UP", "UNKNOWN"):
            continue

        try:
            # Buscar perfil NM asociado
            nm_out = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            profile = None
            for nl in nm_out.splitlines():
                np = nl.strip().split(":")
                if len(np) >= 2 and np[1] == iface:
                    profile = np[0]
                    break
            if not profile:
                continue

            # Verificar never-default
            nd_out = subprocess.run(
                ["nmcli", "-t", "-f", "ipv4.never-default",
                 "connection", "show", profile],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()

            if nd_out == "yes":
                continue  # ya esta protegido

            # Corregir
            subprocess.run(
                ["nmcli", "connection", "modify", profile,
                 "ipv4.never-default", "yes",
                 "connection.autoconnect-priority", "-999"],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["nmcli", "device", "reapply", iface],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["resolvectl", "domain", iface, "~."],
                capture_output=True, timeout=5,
            )
            logger.info(
                "[ProxyHealth] Ruta USB corregida: %s (%s) → never-default", iface, profile)
        except Exception:
            pass


def _run_health_checker() -> None:
    logger.info("[ProxyHealth] Daemon iniciado (intervalo=%ds)", CHECK_INTERVAL_S)
    while True:
        try:
            _ensure_usb_never_default()
            nodes = job_store.list_proxy_nodes()
            for node in nodes:
                ok, ip = _check_node(node)
                if ok:
                    job_store.update_proxy_node_status(
                        node["id"], status="online", last_ip=ip, reset_fails=True,
                    )
                    if node["status"] != "online":
                        logger.info("[Proxy] Nodo %s recuperado — IP: %s", node["id"], ip)
                else:
                    fails = (node["check_fail_count"] or 0) + 1
                    new_status = "offline" if fails >= FAIL_THRESHOLD else node["status"]
                    job_store.update_proxy_node_status(
                        node["id"], status=new_status, fail_count=fails,
                    )
                    if new_status == "offline" and node["status"] != "offline":
                        logger.warning(
                            "[Proxy] Nodo %s OFFLINE tras %d fallos", node["id"], fails,
                        )
                        _alert_node_down(node)
        except Exception:
            logger.exception("[ProxyHealth] Error en ciclo de chequeo")
        time.sleep(CHECK_INTERVAL_S)


def _alert_node_down(node: dict) -> None:
    try:
        accounts = job_store.get_accounts_for_node(node["id"])
        names = [a["name"] for a in accounts]
    except Exception:
        names = []
    logger.critical(
        "[ALERTA] Proxy caído: %s (%s) — cuentas afectadas: %s",
        node["label"], node["server"], ", ".join(names) or "ninguna",
    )
    try:
        if names:
            job_store.create_system_alert(
                f"Nodo proxy {node['label']} offline. Cuentas: {', '.join(names)}",
                severity="critical",
            )
    except Exception:
        pass


def start() -> None:
    """Arranca el health checker como daemon thread (idempotente)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    _ensure_usb_never_default()
    threading.Thread(
        target=_run_health_checker, daemon=True, name="proxy-health-checker",
    ).start()


# ---------------------------------------------------------------------------
# Asignación dinámica interna
# ---------------------------------------------------------------------------

def _online_nodes() -> list[dict]:
    return [n for n in job_store.list_proxy_nodes() if n["status"] != "offline"]


def _assign_to_free_slot(account_name: str, account_groups: list[str]) -> str | None:
    """
    Busca el nodo online con capacidad libre y menor solapamiento de grupos.
    Retorna node_id asignado, o None si todos están llenos.
    """
    nodes = _online_nodes()
    if not nodes:
        return None

    best_node: str | None = None
    best_score: float     = float("inf")

    for node in nodes:
        count = job_store.count_accounts_for_node(node["id"])
        if count >= MAX_ACCOUNTS_PER_NODE:
            continue  # nodo lleno

        existing = job_store.get_accounts_for_node(node["id"])
        existing_groups: set[str] = set()
        for acc in existing:
            try:
                existing_groups.update(json.loads(acc.get("groups") or "[]"))
            except Exception:
                pass

        overlap = len(set(account_groups) & existing_groups)
        # Penalizar solapamiento de grupos (mismas cuentas en mismo proxy = riesgo)
        score = overlap * 100 + count
        if score < best_score:
            best_score = score
            best_node  = node["id"]

    if best_node:
        job_store.set_proxy_assignment(account_name, best_node)
        logger.info(
            "[Proxy] '%s' → nodo %s (slot libre, score=%.0f)",
            account_name, best_node, best_score,
        )
    return best_node


def _evict_lru_and_assign(account_name: str, account_groups: list[str]) -> str | None:
    """
    Todos los nodos están llenos. Expulsa la cuenta con last_used_at más antiguo
    del nodo más apropiado y asigna ese nodo al solicitante.
    """
    nodes = _online_nodes()
    if not nodes:
        return None

    # Para cada nodo, obtener su cuenta LRU y calcular antigüedad
    best_node:    str | None = None
    best_evictee: str | None = None
    oldest_ts:    float      = float("inf")

    for node in nodes:
        lru = job_store.get_lru_account_for_node(node["id"])
        if not lru:
            continue  # nodo sin cuentas (raro), asignar directo
        last_used = lru.get("last_used_at")
        if last_used is None:
            ts = 0.0  # nunca usada → candidata inmediata
        else:
            try:
                ts = datetime.fromisoformat(last_used).timestamp()
            except Exception:
                ts = 0.0

        if ts < oldest_ts:
            oldest_ts    = ts
            best_node    = node["id"]
            best_evictee = lru["account_name"]

    if not best_node or not best_evictee:
        logger.error("[Proxy] No se pudo encontrar candidato a expulsión para '%s'", account_name)
        return None

    # Expulsar la cuenta LRU
    job_store.delete_proxy_assignment(best_evictee)
    _proxy_cache.pop(best_evictee, None)

    last_str = (
        datetime.fromtimestamp(oldest_ts).strftime("%Y-%m-%d %H:%M")
        if oldest_ts > 0 else "nunca"
    )
    logger.warning(
        "[Proxy] ROTACIÓN: '%s' expulsada de %s (último uso: %s) → entra '%s'",
        best_evictee, best_node, last_str, account_name,
    )

    # Asignar el nodo liberado al solicitante
    job_store.set_proxy_assignment(account_name, best_node)
    logger.info("[Proxy] '%s' → nodo %s (post-rotación LRU)", account_name, best_node)
    return best_node


def _ensure_assigned(account_name: str) -> str | None:
    """
    Garantiza que la cuenta tenga proxy. Llama a _assign_to_free_slot primero,
    luego a _evict_lru_and_assign si no hay espacio. Protegido con lock global.
    """
    with _assign_lock:
        # Re-verificar dentro del lock (puede haberse asignado mientras esperaba)
        existing = job_store.get_proxy_assignment(account_name)
        if existing:
            return existing["primary_node"]

        # Obtener grupos de la cuenta para el scoring
        accounts = job_store.list_accounts_full()
        acc_data = next((a for a in accounts if a["name"] == account_name), {})
        try:
            groups = json.loads(acc_data.get("groups") or "[]")
        except Exception:
            groups = []

        node_id = _assign_to_free_slot(account_name, groups)
        if node_id:
            return node_id

        return _evict_lru_and_assign(account_name, groups)


# ---------------------------------------------------------------------------
# Cooldown entre cuentas en el mismo nodo
# ---------------------------------------------------------------------------

def _wait_for_node_cooldown(node_id: str, account_name: str) -> None:
    """Espera si otra cuenta del mismo nodo se usó hace menos de NODE_COOLDOWN_S.

    Reduce la correlación de logins simultáneos desde la misma IP pública
    (señal anti-fraude de Facebook con varias cuentas por proxy).
    """
    with _cooldown_lock:
        last_used = job_store.last_node_use(node_id, exclude_account=account_name)
        if not last_used:
            return
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last_used)).total_seconds()
        except Exception:
            return

        if elapsed >= NODE_COOLDOWN_S:
            return

        wait = min(NODE_COOLDOWN_S - elapsed, NODE_COOLDOWN_MAX_WAIT_S)
        logger.info(
            "[Proxy] Cooldown nodo %s — esperando %.0fs antes de '%s' "
            "(otra cuenta lo usó hace %.0fs)",
            node_id, wait, account_name, elapsed,
        )
        time.sleep(wait)


# ---------------------------------------------------------------------------
# Resolución de proxy para una cuenta (punto de entrada principal)
# ---------------------------------------------------------------------------

def resolve_proxy(account_name: str, force_refresh: bool = False) -> dict | None:
    """
    Retorna el proxy asignado a la cuenta, asignando dinámicamente si es necesario.

    Prioridad:
    1. Cache válido (TTL 30s)
    2. Nodo primario online
    3. Nodo secundario (fallback manual)
    4. Asignación dinámica — slot libre o rotación LRU
    5. Cualquier nodo online de emergencia
    """
    now = time.time()

    if not force_refresh and account_name in _proxy_cache:
        proxy, ts = _proxy_cache[account_name]
        if now - ts < _PROXY_CACHE_TTL_S:
            return proxy

    assignment = job_store.get_proxy_assignment(account_name)

    # Sin asignación → asignar dinámicamente ahora
    if not assignment:
        logger.info("[Proxy] '%s' sin asignación — buscando nodo automáticamente", account_name)
        node_id = _ensure_assigned(account_name)
        if not node_id:
            logger.error("[Proxy] Sin nodos disponibles para '%s'", account_name)
            return None
        assignment = job_store.get_proxy_assignment(account_name)
        if not assignment:
            return None

    candidates = [assignment["primary_node"], assignment.get("secondary_node")]

    for node_id in candidates:
        if not node_id:
            continue
        node = job_store.get_proxy_node(node_id)
        if not node or node["status"] == "offline":
            continue

        # Si el health checker lleva > 3 min sin verificar, validar rápido
        last_checked = node.get("last_checked")
        if last_checked:
            try:
                if now - datetime.fromisoformat(last_checked).timestamp() > 180:
                    ok, ip = _check_node(node)
                    if not ok:
                        logger.warning("[Proxy] %s offline (validación rápida)", node_id)
                        continue
            except Exception:
                pass

        _wait_for_node_cooldown(node_id, account_name)
        proxy = {"server": node["server"]}
        _proxy_cache[account_name] = (proxy, time.time())
        job_store.touch_proxy_assignment(account_name)
        logger.info(
            "[Proxy] '%s' → %s (%s)", account_name, node_id, node.get("last_seen_ip", "?"),
        )
        return proxy

    # Ambos candidatos offline → fallback de emergencia
    fallback = job_store.get_any_online_proxy_node(exclude_nodes=candidates)
    if fallback:
        logger.warning(
            "[Proxy] '%s' fallback emergencia → %s (IP compartida)", account_name, fallback["id"],
        )
        _wait_for_node_cooldown(fallback["id"], account_name)
        proxy = {"server": fallback["server"]}
        _proxy_cache[account_name] = (proxy, time.time())
        job_store.touch_proxy_assignment(account_name)
        return proxy

    logger.error("[Proxy] Sin nodos disponibles para '%s'", account_name)
    return None


# ---------------------------------------------------------------------------
# Asignación manual (desde admin panel / setup script)
# ---------------------------------------------------------------------------

def assign_proxy_to_account(
    account_name: str,
    account_groups: list[str],
    secondary_node: str | None = None,
) -> str | None:
    """
    Asignación manual explícita. Busca el mejor nodo libre sin aplicar rotación LRU.
    Retorna node_id asignado, o None si sin nodos disponibles.
    """
    with _assign_lock:
        if secondary_node and not job_store.get_proxy_node(secondary_node):
            logger.error("[Proxy] Nodo secundario '%s' no existe", secondary_node)
            secondary_node = None

        nodes = _online_nodes()
        if not nodes:
            logger.warning("[Proxy] Sin nodos en pool para asignar a '%s'", account_name)
            return None

        best_node: str | None = None
        best_score: float     = float("inf")

        for node in nodes:
            existing = job_store.get_accounts_for_node(node["id"])
            existing_groups: set[str] = set()
            for acc in existing:
                try:
                    existing_groups.update(json.loads(acc.get("groups") or "[]"))
                except Exception:
                    pass
            overlap = len(set(account_groups) & existing_groups)
            score   = overlap * 100 + len(existing)
            if score < best_score:
                best_score = score
                best_node  = node["id"]

        if best_node:
            job_store.set_proxy_assignment(account_name, best_node, secondary_node)
            logger.info(
                "[Proxy] '%s' asignado manualmente a %s (score=%.0f)",
                account_name, best_node, best_score,
            )
            return best_node

    logger.error("[Proxy] Sin nodos disponibles para asignar a '%s'", account_name)
    return None
