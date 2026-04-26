#!/usr/bin/env bash
# setup_phone_proxy.sh — Verificación, diagnóstico y alta de proxies SIM via USB
#
# USO:
#   ./setup_phone_proxy.sh                          # escanea todos los teléfonos conectados
#   ./setup_phone_proxy.sh --add                    # guía interactiva para agregar un teléfono nuevo
#   ./setup_phone_proxy.sh --test socks5://IP:PORT  # prueba un proxy concreto
#   ./setup_phone_proxy.sh --list                   # lista nodos registrados en la DB
#   ./setup_phone_proxy.sh --status                 # estado de todos los nodos en DB

set -euo pipefail

# ---------------------------------------------------------------------------
# Colores
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()    { echo -e "${GREEN}  ✓${RESET} $*"; }
fail()  { echo -e "${RED}  ✗${RESET} $*"; }
info()  { echo -e "${BLUE}  →${RESET} $*"; }
warn()  { echo -e "${YELLOW}  ⚠${RESET} $*"; }
step()  { echo -e "\n${CYAN}${BOLD}[$*]${RESET}"; }
title() { echo -e "\n${BOLD}=== $* ===${RESET}"; }
hr()    { echo -e "${BOLD}──────────────────────────────────────────────${RESET}"; }

DB_DIR="$(dirname "$0")/facebook_auto_poster"

# ---------------------------------------------------------------------------
# Verificar dependencias mínimas
# ---------------------------------------------------------------------------
check_deps() {
    local missing=0
    for cmd in curl ip timeout; do
        if ! command -v "$cmd" &>/dev/null; then
            fail "Dependencia faltante: $cmd  →  sudo apt install $cmd"
            missing=1
        fi
    done
    [ $missing -eq 0 ] && return 0 || return 1
}

# ---------------------------------------------------------------------------
# Detectar interfaces USB de tethering Android
# Retorna array USB_IFACES (global)
# ---------------------------------------------------------------------------
detect_usb_interfaces() {
    # Android tethering puede aparecer como: usb0, usb1, rndis0, enxXXXX...
    mapfile -t USB_IFACES < <(ip link show 2>/dev/null \
        | grep -E "^[0-9]+: (usb[0-9]|rndis[0-9]|enx[0-9a-f]+)" \
        | awk -F': ' '{print $2}' \
        | awk '{print $1}')
}

# ---------------------------------------------------------------------------
# IP del gateway de una interfaz (= IP del teléfono Android)
# ---------------------------------------------------------------------------
get_gateway_ip() {
    local iface="$1"
    ip route show dev "$iface" 2>/dev/null \
        | grep -oP "via \K[\d.]+" | head -1
}

# ---------------------------------------------------------------------------
# IP pública directa del servidor (sin proxy)
# ---------------------------------------------------------------------------
get_direct_ip() {
    curl -s --max-time 8 "https://api.ipify.org?format=json" 2>/dev/null \
        | grep -oP '"ip":"\K[^"]+' || echo ""
}

# ---------------------------------------------------------------------------
# Diagnóstico: detectar si el proxy está usando WiFi en vez de SIM
# ---------------------------------------------------------------------------
diagnose_wifi_bypass() {
    local proxy_url="$1"
    local proxy_ip="$2"
    local direct_ip="$3"

    if [ "$proxy_ip" = "$direct_ip" ]; then
        warn "La IP del proxy ($proxy_ip) es IGUAL a la del servidor — el teléfono está usando WiFi en lugar de la SIM"
        echo ""
        echo -e "${YELLOW}  CAUSA:${RESET} Android prioriza WiFi sobre datos móviles."
        echo -e "${YELLOW}  FIX — Desactiva el WiFi en el teléfono:${RESET}"
        echo "    Ajustes → WiFi → Desactivar"
        echo "    (mantén solo datos móviles activos)"
        echo ""
        echo "  Una vez desactivado el WiFi, prueba de nuevo:"
        echo "    $0 --test $proxy_url"
        echo ""
        echo -e "${YELLOW}  ALTERNATIVA si necesitas mantener el WiFi:${RESET}"
        echo "    Algunos teléfonos permiten forzar datos por SIM aunque WiFi esté activo:"
        echo "    Ajustes → Conexiones → Uso de datos → Datos móviles → Activar"
        echo "    (el comportamiento varía por marca/versión Android)"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Probar un proxy SOCKS5 con diagnóstico completo
# ---------------------------------------------------------------------------
test_proxy() {
    local proxy_url="$1"
    local label="${2:-proxy}"

    title "Probando $label: $proxy_url"
    check_deps || return 1

    info "Obteniendo IP directa del servidor..."
    DIRECT_IP=$(get_direct_ip)
    if [ -z "$DIRECT_IP" ]; then
        warn "No se pudo obtener la IP directa (sin conexión a internet?)"
        DIRECT_IP="desconocida"
    else
        info "IP directa del servidor: $DIRECT_IP"
    fi

    info "Conectando via proxy..."
    PROXY_RESULT=$(curl -s --proxy "$proxy_url" --max-time 15 \
        "https://api.ipify.org?format=json" 2>&1) || true

    if echo "$PROXY_RESULT" | grep -q '"ip"'; then
        PROXY_IP=$(echo "$PROXY_RESULT" | grep -oP '"ip":"\K[^"]+')
        ok "Proxy responde — IP pública: ${BOLD}$PROXY_IP${RESET}"

        if ! diagnose_wifi_bypass "$proxy_url" "$PROXY_IP" "$DIRECT_IP"; then
            return 1
        fi

        ok "IP diferente a la del servidor ($DIRECT_IP) — tráfico saliendo por la SIM"
        echo ""
        echo -e "  ${BOLD}Resultado:${RESET} proxy listo para registrar en DB"
        echo "  Servidor: $proxy_url"
        echo "  IP SIM:   $PROXY_IP"
        return 0
    else
        fail "Sin respuesta del proxy"
        diagnose_proxy_failure "$proxy_url" "$PROXY_RESULT"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Diagnosticar fallo de conexión al proxy con causas específicas
# ---------------------------------------------------------------------------
diagnose_proxy_failure() {
    local proxy_url="$1"
    local error_msg="${2:-}"
    local host port

    host=$(echo "$proxy_url" | grep -oP '(?<=://)[\d.]+')
    port=$(echo "$proxy_url" | grep -oP '(?<=:)\d+$')

    echo ""
    echo -e "${RED}  DIAGNÓSTICO DE FALLO:${RESET}"

    # ¿El host responde ping?
    if ping -c 1 -W 2 "$host" &>/dev/null 2>&1; then
        info "Host $host responde a ping — el teléfono está accesible"
    else
        fail "Host $host NO responde a ping"
        echo "    → El USB tethering puede estar activo pero sin IP asignada"
        echo "    → Intenta: sudo dhclient INTERFAZ"
        echo "    → O desconecta y reconecta el cable USB"
    fi

    # ¿El puerto está abierto?
    if timeout 3 bash -c "echo >/dev/tcp/$host/$port" 2>/dev/null; then
        info "Puerto $port está abierto — la app proxy está corriendo"
        echo "    → El problema puede ser que el proxy no tiene salida a internet"
        echo "    → Verifica que el teléfono tenga datos móviles activos"
        echo "    → Verifica que la SIM tenga saldo/datos disponibles"
    else
        fail "Puerto $port CERRADO en $host"
        echo ""
        echo -e "${YELLOW}  CAUSAS COMUNES Y SOLUCIONES:${RESET}"
        echo ""
        echo "  A) La app proxy no está iniciada"
        echo "     → Abre Every Proxy → toca el botón de encendido"
        echo "     → El ícono debe mostrar que el servidor está activo"
        echo ""
        echo "  B) La app escucha en un puerto diferente"
        echo "     → Revisa el puerto configurado en la app (suele ser 1080 u 8080)"
        echo "     → Prueba: $0 --test socks5://$host:8080"
        echo ""
        echo "  C) El firewall del teléfono bloquea conexiones externas"
        echo "     → Algunas ROMs con firewall bloquean por defecto"
        echo "     → Busca ajustes de firewall en Ajustes → Seguridad"
        echo ""
        echo "  D) El teléfono no confía en conexiones USB (depuración desactivada)"
        echo "     → No afecta tethering normal, pero algunos teléfonos lo requieren"
        echo ""
        echo "  E) Marca específica con ruta de menú diferente"
        echo "     → Samsung:   Ajustes → Conexiones → Zona Wi-Fi y Anclaje → Anclaje USB"
        echo "     → Xiaomi:    Ajustes → Conexión y compartición → Anclaje USB"
        echo "     → Motorola:  Ajustes → Red e Internet → Anclaje de red → Anclaje USB"
        echo "     → Huawei:    Ajustes → Datos de conexión inalámbrica → Anclaje USB"
        echo "     → OnePlus:   Ajustes → WiFi e Internet → Zona y anclaje → Anclaje USB"
    fi

    if [ -n "$error_msg" ]; then
        echo ""
        info "Error técnico recibido: $error_msg"
    fi
}

# ---------------------------------------------------------------------------
# Scan automático: detectar interfaces, probar puertos, reportar
# ---------------------------------------------------------------------------
auto_scan() {
    title "Interfaces USB conectadas"
    detect_usb_interfaces

    if [ ${#USB_IFACES[@]} -eq 0 ]; then
        fail "No se detectó ningún teléfono con USB tethering activo"
        echo ""
        echo -e "${YELLOW}  El cable USB está conectado pero el teléfono no aparece como interfaz de red.${RESET}"
        echo "  Esto pasa cuando USB tethering NO está activado en el teléfono."
        echo ""
        echo -e "  ${BOLD}PASOS:${RESET}"
        echo "  1. En el teléfono: Ajustes → Conexiones → Zona Activa y Anclaje"
        echo "  2. Activa 'Anclaje USB' (USB Tethering)"
        echo "  3. El teléfono te pedirá confirmación — acepta"
        echo "  4. Ejecuta este script de nuevo"
        echo ""
        echo -e "  ${YELLOW}Si ya lo activaste y sigue sin aparecer:${RESET}"
        echo "  • Carga el driver RNDIS:  sudo modprobe rndis_host"
        echo "  • Prueba otro cable USB (algunos cables son solo carga, no datos)"
        echo "  • Desconecta y vuelve a conectar el cable"
        echo "  • Verifica que el teléfono no esté en modo 'Solo carga' (en la"
        echo "    notificación de USB del teléfono elige 'Transferencia de archivos'"
        echo "    o 'Acceso a internet')"
        return 1
    fi

    title "Escaneo de proxies por interfaz"
    PROXY_PORTS=(1080 8080 8888 3128 1081)
    local found_any=0

    for iface in "${USB_IFACES[@]}"; do
        hr
        IP_LOCAL=$(ip addr show "$iface" 2>/dev/null | grep "inet " | awk '{print $2}' | head -1)
        STATE=$(ip link show "$iface" 2>/dev/null | grep -oP "state \K\w+" || echo "?")
        GW=$(get_gateway_ip "$iface")

        echo -e "  Interfaz: ${BOLD}$iface${RESET}"
        echo "  Estado:   $STATE"
        echo "  IP host:  ${IP_LOCAL:-sin IP — DHCP pendiente}"
        echo "  Gateway:  ${GW:-no detectado}"

        if [ -z "$IP_LOCAL" ]; then
            warn "Sin IP — el tethering está activo pero DHCP no asignó IP"
            echo "    Intenta: sudo dhclient $iface"
            continue
        fi

        if [ -z "$GW" ]; then
            warn "Gateway no detectado para $iface"
            echo "    Intenta: sudo dhclient $iface"
            continue
        fi

        echo ""
        info "Buscando proxy en $GW..."
        local proxy_found=0

        for port in "${PROXY_PORTS[@]}"; do
            if timeout 2 bash -c "echo >/dev/tcp/$GW/$port" 2>/dev/null; then
                ok "Puerto $port abierto"
                PROXY_URL="socks5://$GW:$port"

                info "Probando proxy..."
                DIRECT_IP=$(get_direct_ip)
                PROXY_RESULT=$(curl -s --proxy "$PROXY_URL" --max-time 15 \
                    "https://api.ipify.org?format=json" 2>/dev/null) || true

                if echo "$PROXY_RESULT" | grep -q '"ip"'; then
                    PROXY_IP=$(echo "$PROXY_RESULT" | grep -oP '"ip":"\K[^"]+')

                    if [ "$PROXY_IP" = "$DIRECT_IP" ]; then
                        warn "Proxy conectado pero IP igual a la del servidor"
                        warn "El teléfono está usando WiFi en lugar de datos SIM"
                        echo ""
                        echo "    ${BOLD}FIX:${RESET} Desactiva el WiFi en el teléfono"
                        echo "    Una vez desactivado, prueba: $0 --test $PROXY_URL"
                    else
                        ok "Proxy SIM funcionando"
                        ok "IP SIM: ${BOLD}$PROXY_IP${RESET}  (servidor directo: $DIRECT_IP)"
                        echo ""
                        echo -e "  ${BOLD}Para registrar este nodo en la DB:${RESET}"
                        echo "    $0 --add"
                        echo "  O directamente:"
                        echo "    python3 facebook_auto_poster/register_proxy.py \\"
                        echo "      --id NOMBRE_ID --label 'Teléfono N SIM' \\"
                        echo "      --server $PROXY_URL"
                        found_any=1
                    fi
                    proxy_found=1
                else
                    fail "Puerto $port abierto pero proxy no responde — la app puede estar configurada como HTTP, no SOCKS5"
                    echo "    Verifica el protocolo en la app (debe ser SOCKS5)"
                fi
                break
            fi
        done

        if [ $proxy_found -eq 0 ]; then
            fail "Ningún puerto de proxy respondió en $GW"
            diagnose_proxy_failure "socks5://$GW:1080" ""
        fi
    done
    hr
    return 0
}

# ---------------------------------------------------------------------------
# Flujo interactivo para agregar un teléfono nuevo
# ---------------------------------------------------------------------------
add_phone_interactive() {
    title "Agregar teléfono nuevo al pool de proxies"

    step "Paso 1 — Preparar el teléfono"
    echo "  Asegúrate de haber completado en el teléfono:"
    echo "  □ WiFi DESACTIVADO (datos solo por SIM)"
    echo "  □ Datos móviles ACTIVADOS"
    echo "  □ App proxy instalada (Every Proxy en Play Store)"
    echo "  □ App proxy configurada: protocolo SOCKS5, puerto 1080"
    echo "  □ Servidor proxy INICIADO en la app"
    echo "  □ Cable USB conectado"
    echo "  □ USB Tethering ACTIVADO: Ajustes → Conexiones → Anclaje USB"
    echo ""
    read -r -p "  ¿Completados todos los pasos? [s/N]: " CONFIRM
    if [[ ! "$CONFIRM" =~ ^[sS]$ ]]; then
        echo "  Completa los pasos y vuelve a ejecutar: $0 --add"
        exit 0
    fi

    step "Paso 2 — Detectando interfaz USB"
    detect_usb_interfaces
    if [ ${#USB_IFACES[@]} -eq 0 ]; then
        fail "No se detectaron interfaces USB"
        echo "  Revisa que el USB tethering esté activo en el teléfono"
        exit 1
    fi

    # Si hay varias interfaces, preguntar cuál
    if [ ${#USB_IFACES[@]} -gt 1 ]; then
        echo "  Se detectaron varias interfaces:"
        for i in "${!USB_IFACES[@]}"; do
            GW_TMP=$(get_gateway_ip "${USB_IFACES[$i]}")
            echo "  $((i+1)). ${USB_IFACES[$i]} (gateway: ${GW_TMP:-?})"
        done
        read -r -p "  ¿Cuál es el teléfono nuevo? (número): " IDX
        SELECTED_IFACE="${USB_IFACES[$((IDX-1))]}"
    else
        SELECTED_IFACE="${USB_IFACES[0]}"
    fi

    GW=$(get_gateway_ip "$SELECTED_IFACE")
    if [ -z "$GW" ]; then
        fail "No se pudo obtener el gateway de $SELECTED_IFACE"
        echo "  Intenta: sudo dhclient $SELECTED_IFACE"
        exit 1
    fi
    ok "Interfaz: $SELECTED_IFACE | Gateway (IP teléfono): $GW"

    step "Paso 3 — Detectando puerto del proxy"
    DETECTED_PORT=""
    for port in 1080 8080 8888 3128; do
        if timeout 2 bash -c "echo >/dev/tcp/$GW/$port" 2>/dev/null; then
            ok "Puerto $port abierto"
            DETECTED_PORT="$port"
            break
        fi
    done

    if [ -z "$DETECTED_PORT" ]; then
        fail "No se encontró ningún proxy activo en $GW"
        echo "  Verifica que la app proxy esté iniciada en el teléfono"
        read -r -p "  ¿Ingresar puerto manualmente? [s/N]: " MANUAL
        if [[ "$MANUAL" =~ ^[sS]$ ]]; then
            read -r -p "  Puerto: " DETECTED_PORT
        else
            exit 1
        fi
    fi

    PROXY_URL="socks5://$GW:$DETECTED_PORT"

    step "Paso 4 — Verificando IP SIM vs WiFi"
    DIRECT_IP=$(get_direct_ip)
    info "IP directa del servidor: $DIRECT_IP"
    info "Probando proxy $PROXY_URL..."

    PROXY_RESULT=$(curl -s --proxy "$PROXY_URL" --max-time 15 \
        "https://api.ipify.org?format=json" 2>/dev/null) || true

    if ! echo "$PROXY_RESULT" | grep -q '"ip"'; then
        fail "El proxy no responde"
        diagnose_proxy_failure "$PROXY_URL" "$PROXY_RESULT"
        exit 1
    fi

    PROXY_IP=$(echo "$PROXY_RESULT" | grep -oP '"ip":"\K[^"]+')

    if [ "$PROXY_IP" = "$DIRECT_IP" ]; then
        warn "El teléfono está usando WiFi (misma IP que el servidor: $DIRECT_IP)"
        echo ""
        echo "  Desactiva el WiFi en el teléfono y presiona Enter para continuar..."
        read -r

        # Reintentar
        PROXY_RESULT2=$(curl -s --proxy "$PROXY_URL" --max-time 15 \
            "https://api.ipify.org?format=json" 2>/dev/null) || true
        PROXY_IP=$(echo "$PROXY_RESULT2" | grep -oP '"ip":"\K[^"]+' || echo "")

        if [ -z "$PROXY_IP" ] || [ "$PROXY_IP" = "$DIRECT_IP" ]; then
            fail "Sigue sin cambiar la IP — verifica que WiFi esté desactivado y hay señal SIM"
            exit 1
        fi
    fi
    ok "IP SIM confirmada: ${BOLD}$PROXY_IP${RESET}  (diferente al servidor: $DIRECT_IP)"

    step "Paso 5 — Registrar en la DB"
    echo "  Datos detectados:"
    echo "    Servidor: $PROXY_URL"
    echo "    IP SIM:   $PROXY_IP"
    echo ""

    # Sugerir ID basado en nodos ya existentes
    EXISTING_COUNT=$(python3 -c "
import sys; sys.path.insert(0, '$DB_DIR')
import job_store; job_store.init_db()
print(len(job_store.list_proxy_nodes()))
" 2>/dev/null || echo "0")
    NEXT_NUM=$((EXISTING_COUNT + 1))

    read -r -p "  ID del nodo (ej: phone${NEXT_NUM}_sim): " NODE_ID
    read -r -p "  Etiqueta (ej: Teléfono $NEXT_NUM SIM): " NODE_LABEL

    if [ -z "$NODE_ID" ] || [ -z "$NODE_LABEL" ]; then
        fail "ID y etiqueta son obligatorios"
        exit 1
    fi

    python3 -c "
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
job_store.upsert_proxy_node('$NODE_ID', '$NODE_LABEL', '$PROXY_URL',
    notes='Interface: $SELECTED_IFACE | IP host local: $(ip addr show "$SELECTED_IFACE" | grep "inet " | awk "{print \$2}" | head -1)')
job_store.update_proxy_node_status('$NODE_ID', last_ip='$PROXY_IP', reset_fails=True)
print('Nodo registrado en DB')
"
    ok "Nodo '$NODE_ID' registrado"

    step "Paso 6 — Asignar cuentas"
    echo "  Para asignar las cuentas a este nodo automáticamente:"
    echo ""
    echo "    python3 -c \""
    echo "    import sys; sys.path.insert(0, 'facebook_auto_poster')"
    echo "    import job_store, proxy_manager, json"
    echo "    job_store.init_db()"
    echo "    for c in job_store.list_accounts_full():"
    echo "        # Solo asignar cuentas sin proxy o con proxy offline"
    echo "        a = job_store.get_proxy_assignment(c['name'])"
    echo "        if not a:"
    echo "            groups = json.loads(c.get('groups') or '[]')"
    echo "            proxy_manager.assign_proxy_to_account(c['name'], groups)"
    echo "            print('Asignado:', c['name'])"
    echo "    \""
    echo ""
    ok "Teléfono configurado y listo"
    hr
}

# ---------------------------------------------------------------------------
# Listar nodos de la DB
# ---------------------------------------------------------------------------
list_db_nodes() {
    title "Nodos proxy en la DB"
    python3 -c "
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
nodes = job_store.list_proxy_nodes()
if not nodes:
    print('  Sin nodos registrados aún.')
    print('  Para agregar: ./setup_phone_proxy.sh --add')
else:
    assignments = {a['account_name']: a for a in job_store.list_proxy_assignments()}
    node_accounts = {}
    for a in assignments.values():
        node_accounts.setdefault(a['primary_node'], []).append(a['account_name'])
    for n in nodes:
        status_color = '\033[0;32m' if n['status'] == 'online' else '\033[0;31m'
        reset = '\033[0m'
        accounts = node_accounts.get(n['id'], [])
        print(f\"  {status_color}{n['status'].upper():10}{reset} {n['id']:20} {n['server']:35} IP: {n['last_seen_ip'] or '?':18} Cuentas: {len(accounts)}\")
        for acc in accounts:
            print(f\"               └─ {acc}\")
" 2>/dev/null || echo "  Error leyendo la DB (¿está en el directorio correcto?)"
}

# ---------------------------------------------------------------------------
# Estado en tiempo real de todos los nodos (health check manual)
# ---------------------------------------------------------------------------
check_status() {
    title "Estado de nodos (health check manual)"
    python3 -c "
import sys; sys.path.insert(0, '$DB_DIR')
import job_store, proxy_manager
job_store.init_db()
nodes = job_store.list_proxy_nodes()
if not nodes:
    print('  Sin nodos registrados.')
else:
    for n in nodes:
        ok, ip = proxy_manager._check_node(n)
        symbol = '✓' if ok else '✗'
        print(f\"  {symbol} {n['id']:20} {'OK: '+ip if ok else 'OFFLINE'}\")
" 2>/dev/null || echo "  Error ejecutando health check"
}

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
print_header() {
    echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║   Facebook Auto-Poster — Setup de Proxies SIM       ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
print_header
check_deps || exit 1

case "${1:-}" in
    --add)
        add_phone_interactive
        ;;
    --test)
        if [ -z "${2:-}" ]; then
            echo "Uso: $0 --test socks5://IP:PUERTO"
            exit 1
        fi
        test_proxy "$2"
        ;;
    --list)
        list_db_nodes
        ;;
    --status)
        check_status
        ;;
    --help|-h)
        echo ""
        echo "  Comandos disponibles:"
        echo "    (sin args)        Escanea todos los teléfonos conectados"
        echo "    --add             Guía interactiva para agregar teléfono nuevo"
        echo "    --test URL        Prueba un proxy específico (ej: socks5://IP:1080)"
        echo "    --list            Lista nodos registrados en la DB"
        echo "    --status          Health check manual de todos los nodos"
        echo ""
        ;;
    *)
        auto_scan
        echo ""
        echo -e "${BOLD}=== Próximos pasos ===${RESET}"
        echo "  • Agregar teléfono nuevo a la DB:  $0 --add"
        echo "  • Ver nodos registrados:           $0 --list"
        echo "  • Estado de todos los nodos:       $0 --status"
        echo "  • Probar proxy específico:         $0 --test socks5://IP:1080"
        echo ""
        ;;
esac
