#!/usr/bin/env python3
"""
proxy_cli.py — Gestión simplificada de proxies SIM para Facebook Auto-Poster.

Reemplaza a setup_phone_proxy.sh con un CLI en Python puro que usa job_store
y proxy_manager directamente. Sin bash heredocs, sin wizard interactivo.

Comandos:
    python proxy_cli.py setup              Auto-detectar teléfono + registrar + auto-asignar cuentas
    python proxy_cli.py status             Mostrar estado de todos los nodos
    python proxy_cli.py fix NODE_ID        Re-detectar IP del teléfono si cambió
    python proxy_cli.py assign NODE CUENTA Asignar proxy manualmente
    python proxy_cli.py unassign CUENTA    Quitar asignación de proxy
    python proxy_cli.py test              Probar conectividad del proxy del teléfono conectado
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

# ── Verificar que PySocks esté disponible ───────────────────────────────────
try:
    import socks  # noqa: F401 — requerido por requests para SOCKS5
except ImportError:
    print("ERROR: PySocks no instalado. Ejecuta con el Python del venv:")
    print(f"  {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/.venv/bin/python proxy_cli.py ...")
    sys.exit(1)

try:
    import requests  # noqa: F401
except ImportError:
    print("ERROR: requests no instalado.")
    sys.exit(1)

# ── Path setup ──────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import job_store
import proxy_manager

logger = logging.getLogger("proxy_cli")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S"
)

# ── Constantes ───────────────────────────────────────────────────────────────
SCAN_PORTS = [1080, 8080, 8888, 1081, 3128, 8123, 1090, 9050, 10808]
SCAN_TIMEOUT = 4
CURL_TIMEOUT = 6
MAX_ACCOUNTS_PER_NODE = 10


# ══════════════════════════════════════════════════════════════════════════════
# Helpers de red
# ══════════════════════════════════════════════════════════════════════════════

def _run(cmd: list[str], timeout: int = 10) -> str:
    """Ejecuta un comando y retorna stdout decodificado, o '' en error."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _find_usb_interfaces() -> list[str]:
    """Detecta interfaces USB tethering activas."""
    out = _run(["ip", "-br", "link", "show"])
    ifaces = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        iface = parts[0].rstrip("@")
        state = parts[1] if len(parts) > 1 else ""
        # USB tethering: estado UP o UNKNOWN (común en interfaces USB)
        if re.match(r"^(usb|rndis|enx|enu)", iface) and state in ("UP", "UNKNOWN"):
            ifaces.append(iface)
    return ifaces


def _get_local_ip(iface: str) -> str:
    """IP local del host en la interfaz USB."""
    out = _run(["ip", "-4", "-o", "addr", "show", iface])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def _get_phone_ip(iface: str) -> str | None:
    """IP del teléfono vía ARP. La MAC física (PERMANENT/REACHABLE/STALE/DELAY)
    es el teléfono; FAILED/INCOMPLETE se ignoran."""
    out = _run(["ip", "neigh", "show", "dev", iface])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] not in ("FAILED", "INCOMPLETE"):
            # parts[0] es la IP
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
                return parts[0]
    # Fallback: probar IPs comunes
    local = _get_local_ip(iface)
    if local:
        parts = local.rsplit(".", 1)
        for host in [f"{parts[0]}.1", f"{parts[0]}.129", f"{parts[0]}.128"]:
            if _port_open(host, 1080):
                return host
    return None


def _get_gateway(iface: str) -> str:
    """Gateway de la interfaz USB (via DHCP)."""
    out = _run(["ip", "route", "show", "dev", iface])
    m = re.search(r"via (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Verifica si un puerto TCP está abierto."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _get_direct_ip() -> str:
    """IP pública del servidor (sin proxy)."""
    out = _run([
        "curl", "-s", "--max-time", "5",
        "https://api.ipify.org?format=json"
    ], timeout=8)
    try:
        return json.loads(out).get("ip", "")
    except Exception:
        return ""


def _get_proxy_ip(proxy_url: str) -> str:
    """IP pública vista a través del proxy."""
    out = _run([
        "curl", "-s", "--max-time", str(CURL_TIMEOUT),
        "--proxy", proxy_url,
        "https://api.ipify.org?format=json"
    ], timeout=CURL_TIMEOUT + 2)
    try:
        return json.loads(out).get("ip", "")
    except Exception:
        return ""


def _test_proxy_protocol(host: str, port: int) -> str | None:
    """Prueba SOCKS5, HTTP y SOCKS4 en orden. Retorna URL o None."""
    protocols = [
        f"socks5://{host}:{port}",
        f"http://{host}:{port}",
        f"socks4://{host}:{port}",
    ]
    for url in protocols:
        ip = _get_proxy_ip(url)
        if ip:
            return url
    return None


# ══════════════════════════════════════════════════════════════════════════════
# NetworkManager helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_nm_profile(iface: str) -> str | None:
    """Busca el perfil NetworkManager asociado a una interfaz."""
    out = _run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show"])
    for line in out.splitlines():
        parts = line.strip().split(":")
        if len(parts) >= 2 and parts[1] == iface:
            return parts[0]
    return None


def _nm_profile_get(profile: str, key: str) -> str:
    """Lee una propiedad de un perfil NM."""
    out = _run(["nmcli", "-t", "-f", key, "connection", "show", profile])
    return out.strip()


def _ensure_never_default(iface: str) -> bool:
    """Garantiza que la interfaz USB NO tome la ruta por defecto.
    Modifica el perfil NM y reaplica inmediatamente. Retorna True si se aplico.
    Verifica el resultado para detectar fallos silenciosos de polkit."""
    profile = _get_nm_profile(iface)
    if not profile:
        logger.error("No se encontró perfil NM para %s", iface)
        return False

    current = _nm_profile_get(profile, "ipv4.never-default")
    if current == "yes":
        logger.info("NM %s: never-default ya está en yes — sin cambios", profile)
    else:
        _run(["nmcli", "connection", "modify", profile,
              "ipv4.never-default", "yes",
              "connection.autoconnect-priority", "-999"])

        # Verificar que el comando realmente surtió efecto
        after = _nm_profile_get(profile, "ipv4.never-default")
        if after != "yes":
            logger.error(
                "nmcli modify no se aplicó al perfil '%s' — "
                "posiblemente bloqueado por polkit.\n"
                "Solución permanente (ejecutar una sola vez con sudo):\n"
                "  sudo nmcli connection modify '%s' ipv4.never-default yes\n"
                "  sudo nmcli device reapply %s",
                profile, profile, iface,
            )
            return False
        logger.info("NM %s: never-default cambiado a yes ✓", profile)

    # Bloquear DNS por esta interfaz (evita que systemd-resolved la use)
    _run(["resolvectl", "domain", iface, "~."], timeout=5)

    # Reaplicar el perfil para forzar never-default sin desconectar
    _run(["nmcli", "device", "reapply", iface], timeout=10)

    return True


# ══════════════════════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════════════════════

def _auto_generate_node_id(host: str, label: str) -> str:
    """Genera un ID de nodo basado en el label + IP."""
    clean = re.sub(r"[^a-z0-9_]", "", label.lower().replace(" ", "_"))[:20]
    return f"{clean}_{host.replace('.', '_')}"


def _auto_assign_accounts(node_id: str, max_accounts: int = MAX_ACCOUNTS_PER_NODE) -> int:
    """Asigna cuentas activas sin proxy al nodo dado. Retorna cantidad asignada."""
    accounts = job_store.list_accounts_full()
    assigned = 0

    for acc in accounts:
        if not acc.get("is_active"):
            continue
        existing = job_store.get_proxy_assignment(acc["name"])
        if existing:
            continue  # ya tiene proxy

        # Asignar
        groups = json.loads(acc.get("groups") or "[]")
        node_id_result = proxy_manager.assign_proxy_to_account(acc["name"], groups)
        if node_id_result:
            logger.info("  ✓ %s → %s", acc["name"], node_id_result)
            assigned += 1
        else:
            logger.warning("  ✗ %s: no se pudo asignar", acc["name"])

        if assigned >= max_accounts:
            logger.info("  Límite de %d cuentas alcanzado", max_accounts)
            break

    return assigned


# ══════════════════════════════════════════════════════════════════════════════
# Comandos
# ══════════════════════════════════════════════════════════════════════════════

def cmd_setup():
    """Detecta teléfono, configura red, registra proxy y auto-asigna cuentas."""
    logger.info("═══ Proxy CLI — Setup automático ═══")

    # 1. Detectar interfaz USB
    ifaces = _find_usb_interfaces()
    if not ifaces:
        logger.error("No se detectó ningún teléfono conectado por USB")
        logger.info("Asegúrate de: activar Anclaje USB en el teléfono + cable conectado")
        return False

    iface = ifaces[0]
    local_ip = _get_local_ip(iface)
    logger.info("Interfaz: %s | IP local: %s", iface, local_ip)

    if len(ifaces) > 1:
        logger.info("Detectadas %d interfaces: %s — usando %s", len(ifaces), ifaces, iface)

    # 2. Configurar NetworkManager
    if not _ensure_never_default(iface):
        return False

    # 3. Detectar IP del teléfono
    phone_ip = _get_phone_ip(iface)
    if not phone_ip:
        logger.error("No se pudo detectar la IP del teléfono en %s", iface)
        logger.info("¿Every Proxy está corriendo en el teléfono?")
        return False
    logger.info("Teléfono detectado en: %s", phone_ip)

    # 4. Escanear puertos
    logger.info("Escaneando puertos en %s...", phone_ip)
    proxy_url = None
    for port in SCAN_PORTS:
        if not _port_open(phone_ip, port):
            continue
        logger.info("  Puerto %d abierto — probando protocolos...", port)
        url = _test_proxy_protocol(phone_ip, port)
        if url:
            proxy_url = url
            logger.info("  ✓ %s OK", url)
            break
        else:
            logger.info("  ✗ Puerto %d abierto pero sin respuesta de proxy", port)

    if not proxy_url:
        logger.error("No se encontró proxy en %s", phone_ip)
        logger.info("Verifica que Every Proxy esté corriendo (SOCKS5, puerto 1080)")
        return False

    # 5. Verificar SIM vs WiFi
    direct_ip = _get_direct_ip()
    proxy_ip = _get_proxy_ip(proxy_url)
    logger.info("IP directa (WiFi): %s", direct_ip)
    logger.info("IP via proxy  (SIM): %s", proxy_ip)

    if proxy_ip == direct_ip:
        logger.error("¡WiFi bypass detectado! El teléfono está usando WiFi, no datos SIM")
        logger.info("Desactiva el WiFi en el teléfono y vuelve a ejecutar setup")
        return False

    if not proxy_ip:
        logger.error("El proxy no pudo alcanzar internet")
        return False

    # 6. Registrar en DB
    # Label automático basado en la IP del teléfono
    label = _get_nm_profile(iface) or f"Teléfono {phone_ip}"
    parsed = urlparse(proxy_url)
    node_id = f"phone_{phone_ip.replace('.', '_')}"

    # Buscar si ya existe un nodo con esta IP
    existing_nodes = job_store.list_proxy_nodes()
    existing = None
    for n in existing_nodes:
        if phone_ip in n.get("server", ""):
            existing = n
            break

    if existing:
        logger.info("Nodo existente encontrado: %s (%s)", existing["id"], existing["label"])
        # Actualizar server por si cambió el puerto
        job_store.upsert_proxy_node(existing["id"], existing["label"], proxy_url,
                                     existing.get("notes") or f"Iface: {iface} | IP local: {local_ip} | Proto: {parsed.scheme}")
        job_store.update_proxy_node_status(existing["id"], status="online",
                                            last_ip=proxy_ip, reset_fails=True)
        node_id = existing["id"]
    else:
        notes = f"Iface: {iface} | IP local: {local_ip} | Proto: {parsed.scheme}"
        job_store.upsert_proxy_node(node_id, label, proxy_url, notes)
        job_store.update_proxy_node_status(node_id, status="online",
                                            last_ip=proxy_ip, reset_fails=True)
        logger.info("Nodo registrado: %s → %s (%s)", node_id, proxy_url, proxy_ip)

    # 7. Auto-asignar cuentas sin proxy
    logger.info("Asignando cuentas activas sin proxy...")
    assigned = _auto_assign_accounts(node_id)
    if assigned > 0:
        logger.info("%d cuenta(s) asignada(s) a %s", assigned, node_id)
    else:
        logger.info("Todas las cuentas activas ya tienen proxy asignado")

    logger.info("═══ Setup completado ✓ ═══")
    return True


def cmd_status():
    """Muestra estado de todos los nodos proxy y sus cuentas."""
    job_store.init_db()
    nodes = job_store.list_proxy_nodes()
    assignments = job_store.list_proxy_assignments()

    if not nodes:
        print("No hay nodos proxy registrados.")
        return

    # Mapa node_id → cuentas
    node_accounts: dict[str, list[dict]] = {}
    for a in assignments:
        nid = a.get("primary_node", "")
        node_accounts.setdefault(nid, []).append(a)

    print(f"\n{'Nodo':<25s} {'Estado':<10s} {'IP Pública':<18s} {'Cuentas':<8s}")
    print("-" * 75)
    for n in nodes:
        status = n.get("status", "?")
        icon = {"online": "✓", "offline": "✗", "maintenance": "⊘"}.get(status, "?")
        accounts = node_accounts.get(n["id"], [])
        print(f"{icon} {n['id']:<23s} {status:<10s} {n.get('last_seen_ip', '—'):<18s} {len(accounts):<8d}")

    # Detalle de cuentas
    print(f"\nCuentas asignadas:")
    for n in nodes:
        accounts = node_accounts.get(n["id"], [])
        if not accounts:
            continue
        print(f"  [{n['id']}] ({n.get('last_seen_ip', '?')})")
        for a in accounts:
            lu = a.get("last_used_at") or "—"
            print(f"    • {a['account_name']:<25s} último uso: {lu}")


def cmd_fix(node_id: str):
    """Re-detectar IP del teléfono para un nodo (por si cambió la IP)."""
    node = job_store.get_proxy_node(node_id)
    if not node:
        logger.error("Nodo '%s' no encontrado", node_id)
        return

    logger.info("Re-detectando IP para nodo: %s (%s)", node_id, node["label"])

    ifaces = _find_usb_interfaces()
    if not ifaces:
        logger.error("No se detectó ningún teléfono conectado por USB")
        return

    for iface in ifaces:
        phone_ip = _get_phone_ip(iface)
        if not phone_ip:
            continue

        logger.info("Teléfono en %s: %s", iface, phone_ip)

        # Escanear puertos
        for port in SCAN_PORTS:
            if not _port_open(phone_ip, port):
                continue
            url = _test_proxy_protocol(phone_ip, port)
            if url:
                proxy_ip = _get_proxy_ip(url)
                job_store.upsert_proxy_node(node_id, node["label"], url,
                                             node.get("notes", ""))
                job_store.update_proxy_node_status(node_id, status="online",
                                                    last_ip=proxy_ip, reset_fails=True)
                logger.info("✓ Nodo actualizado: %s → %s (IP: %s)", node_id, url, proxy_ip)
                return

    logger.error("No se pudo re-detectar el proxy para %s", node_id)


def cmd_assign(node_id: str, account: str):
    """Asigna manualmente una cuenta a un nodo proxy."""
    node = job_store.get_proxy_node(node_id)
    if not node:
        logger.error("Nodo '%s' no encontrado", node_id)
        return

    accounts = job_store.list_accounts_full()
    if not any(a["name"] == account for a in accounts):
        logger.error("Cuenta '%s' no encontrada", account)
        return

    job_store.set_proxy_assignment(account, node_id)
    logger.info("✓ %s asignado a %s", account, node_id)


def cmd_unassign(account: str):
    """Quita la asignación de proxy de una cuenta."""
    existing = job_store.get_proxy_assignment(account)
    if not existing:
        logger.warning("La cuenta '%s' no tiene proxy asignado", account)
        return

    job_store.delete_proxy_assignment(account)
    logger.info("✓ Proxy desasignado de %s", account)


def cmd_test():
    """Prueba conectividad del proxy en el teléfono conectado (sin modificar DB)."""
    logger.info("═══ Proxy CLI — Test de conectividad ═══")

    ifaces = _find_usb_interfaces()
    if not ifaces:
        logger.error("No se detectó teléfono conectado por USB")
        return

    iface = ifaces[0]
    local_ip = _get_local_ip(iface)
    phone_ip = _get_phone_ip(iface)
    logger.info("Interfaz: %s | IP local: %s | Teléfono: %s", iface, local_ip, phone_ip)

    if not phone_ip:
        logger.error("No se pudo detectar la IP del teléfono")
        return

    # Escanear puertos
    found = []
    for port in SCAN_PORTS:
        if not _port_open(phone_ip, port):
            continue
        logger.info("Puerto %d abierto — probando protocolos...", port)
        url = _test_proxy_protocol(phone_ip, port)
        if url:
            proxy_ip = _get_proxy_ip(url)
            found.append((port, url, proxy_ip))
            logger.info("  ✓ %s → IP: %s", url, proxy_ip)
        else:
            logger.info("  ✗ Sin respuesta de proxy en puerto %d", port)

    if not found:
        logger.error("No se encontró proxy funcional en %s", phone_ip)
        logger.info("¿Every Proxy está corriendo? Protocolo: SOCKS5, Puerto: 1080")
        return

    # WiFi bypass
    direct_ip = _get_direct_ip()
    for port, url, proxy_ip in found:
        if proxy_ip == direct_ip:
            logger.warning("⚠ Puerto %d: misma IP que WiFi — bypass detectado", port)
        else:
            logger.info("✓ Puerto %d: IP SIM diferente → OK", port)
            logger.info("  Para registrar: python proxy_cli.py setup")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def _print_help():
    print(__doc__)


def main():
    job_store.init_db()
    proxy_manager.start()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"

    if cmd in ("-h", "--help", "help"):
        _print_help()
    elif cmd == "setup":
        cmd_setup()
    elif cmd == "status":
        cmd_status()
    elif cmd == "fix":
        if len(sys.argv) < 3:
            print("Uso: proxy_cli.py fix NODE_ID")
            sys.exit(1)
        cmd_fix(sys.argv[2])
    elif cmd == "assign":
        if len(sys.argv) < 4:
            print("Uso: proxy_cli.py assign NODE_ID CUENTA")
            sys.exit(1)
        cmd_assign(sys.argv[2], sys.argv[3])
    elif cmd == "unassign":
        if len(sys.argv) < 3:
            print("Uso: proxy_cli.py unassign CUENTA")
            sys.exit(1)
        cmd_unassign(sys.argv[2])
    elif cmd == "test":
        cmd_test()
    else:
        print(f"Comando desconocido: {cmd}")
        _print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
