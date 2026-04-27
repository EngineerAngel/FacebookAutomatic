#!/usr/bin/env bash
# setup_phone_proxy.sh — Gestión completa de proxies SIM via USB
#
# USO:
#   ./setup_phone_proxy.sh                          # escaneo automático
#   ./setup_phone_proxy.sh --add                    # agregar teléfono nuevo (interactivo)
#   ./setup_phone_proxy.sh --test socks5://IP:PORT  # probar proxy específico
#   ./setup_phone_proxy.sh --list                   # listar nodos en DB
#   ./setup_phone_proxy.sh --status                 # health check de todos los nodos
#   ./setup_phone_proxy.sh --info NODE_ID           # detalles de un nodo
#   ./setup_phone_proxy.sh --edit NODE_ID           # editar nodo existente
#   ./setup_phone_proxy.sh --remove NODE_ID         # eliminar nodo de la DB
#   ./setup_phone_proxy.sh --assign NODE_ID CUENTA  # asignar proxy a cuenta
#   ./setup_phone_proxy.sh --unassign CUENTA        # quitar proxy de una cuenta
#   ./setup_phone_proxy.sh --fix NODE_ID            # re-detectar IP/puerto de un nodo

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; MAGENTA='\033[0;35m'
BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

ok()    { echo -e "${GREEN}  ✓${RESET} $*"; }
fail()  { echo -e "${RED}  ✗${RESET} $*"; }
info()  { echo -e "${BLUE}  →${RESET} $*"; }
warn()  { echo -e "${YELLOW}  ⚠${RESET} $*"; }
step()  { echo -e "\n${CYAN}${BOLD}[$*]${RESET}"; }
title() { echo -e "\n${BOLD}=== $* ===${RESET}"; }
hr()    { echo -e "${BOLD}──────────────────────────────────────────────${RESET}"; }
note()  { echo -e "${DIM}  $*${RESET}"; }

DB_DIR="$(dirname "$0")/facebook_auto_poster"
SCRIPT_DIR="$(dirname "$0")"

# Usar el Python del venv del proyecto (tiene PySocks); fallback al sistema
_pick_python() {
    local candidates=(
        "$SCRIPT_DIR/../.venv/bin/python3"
        "$SCRIPT_DIR/.venv/bin/python3"
        "$HOME/Proyectos/.venv/bin/python3"
    )
    for p in "${candidates[@]}"; do
        if [ -x "$p" ]; then echo "$p"; return; fi
    done
    echo "python3"  # último recurso
}
PYTHON="$(_pick_python)"

# Puertos a escanear (por orden de probabilidad)
PROXY_PORTS=(1080 8080 8888 3128 1081 8123 1090 9050 10808)

# ---------------------------------------------------------------------------
# Dependencias
# ---------------------------------------------------------------------------
check_deps() {
    local missing=0
    for cmd in curl ip timeout python3; do
        if ! command -v "$cmd" &>/dev/null; then
            fail "Dependencia faltante: $cmd"
            missing=1
        fi
    done
    [ $missing -eq 0 ] && return 0 || return 1
}

# ---------------------------------------------------------------------------
# Interfaces USB de tethering
# Detecta: usb*, rndis*, enx*, eth* (USB dongles), enu*
# ---------------------------------------------------------------------------
detect_usb_interfaces() {
    mapfile -t USB_IFACES < <(ip link show 2>/dev/null \
        | grep -E "^[0-9]+: (usb[0-9]|rndis[0-9]|enx[0-9a-f]+|enu[0-9a-f]+)" \
        | awk -F': ' '{print $2}' \
        | awk '{print $1}')
}

get_gateway_ip() {
    local iface="$1"
    ip route show dev "$iface" 2>/dev/null \
        | grep -oP "via \K[\d.]+" | head -1
}

get_local_ip() {
    local iface="$1"
    ip addr show "$iface" 2>/dev/null | grep "inet " | awk '{print $2}' | head -1
}

get_direct_ip() {
    curl -s --max-time 8 "https://api.ipify.org?format=json" 2>/dev/null \
        | grep -oP '"ip":"\K[^"]+' || echo ""
}

# ---------------------------------------------------------------------------
# Detectar protocolo de proxy en un host:puerto
# Retorna: "socks5://H:P", "http://H:P" o ""
# ---------------------------------------------------------------------------
detect_proxy_protocol() {
    local host="$1" port="$2"

    # Intentar SOCKS5
    if curl -s --proxy "socks5://$host:$port" --max-time 6 \
            "https://api.ipify.org?format=json" 2>/dev/null | grep -q '"ip"'; then
        echo "socks5://$host:$port"
        return 0
    fi

    # Intentar HTTP
    if curl -s --proxy "http://$host:$port" --max-time 6 \
            "https://api.ipify.org?format=json" 2>/dev/null | grep -q '"ip"'; then
        echo "http://$host:$port"
        return 0
    fi

    # Intentar SOCKS4
    if curl -s --proxy "socks4://$host:$port" --max-time 6 \
            "https://api.ipify.org?format=json" 2>/dev/null | grep -q '"ip"'; then
        echo "socks4://$host:$port"
        return 0
    fi

    echo ""
    return 1
}

# ---------------------------------------------------------------------------
# Obtener IP pública via proxy (cualquier protocolo soportado)
# ---------------------------------------------------------------------------
get_proxy_ip() {
    local proxy_url="$1"
    curl -s --proxy "$proxy_url" --max-time 15 \
        "https://api.ipify.org?format=json" 2>/dev/null \
        | grep -oP '"ip":"\K[^"]+' || echo ""
}

# ---------------------------------------------------------------------------
# Diagnóstico cuando el proxy no responde
# ---------------------------------------------------------------------------
diagnose_proxy_failure() {
    local host port proxy_url="${1:-}"
    host=$(echo "$proxy_url" | grep -oP '(?<=://)[\d.]+' || echo "")
    port=$(echo "$proxy_url" | grep -oP '(?<=:)\d+$' || echo "")

    [ -z "$host" ] && return 0

    echo ""
    echo -e "${RED}  DIAGNÓSTICO:${RESET}"

    if ping -c 1 -W 2 "$host" &>/dev/null 2>&1; then
        ok "Host $host responde ping — USB tethering activo"
        if timeout 3 bash -c "echo >/dev/tcp/$host/$port" 2>/dev/null; then
            warn "Puerto $port abierto pero el proxy no devuelve IP pública"
            echo ""
            echo -e "  ${YELLOW}POSIBLES CAUSAS:${RESET}"
            echo "  1. El teléfono no tiene salida a internet"
            echo "     → Verifica señal SIM / saldo de datos"
            echo "  2. La app está configurada en modo HTTP pero el test usó SOCKS5"
            echo "     → El script ya prueba ambos protocolos automáticamente"
            echo "  3. El proxy bloquea conexiones a dominios externos"
            echo "     → En Every Proxy: asegúrate de que 'Remote Proxy' esté desactivado"
        else
            fail "Puerto $port cerrado — la app proxy no está activa"
            _print_proxy_app_help "$host" "$port"
        fi
    else
        fail "Host $host no responde — USB tethering o red no disponible"
        echo ""
        echo -e "  ${YELLOW}PASOS PARA RECUPERAR LA CONEXIÓN:${RESET}"
        echo "  1. Desconecta y reconecta el cable USB"
        echo "  2. En el teléfono: desactiva y reactiva 'Anclaje USB'"
        echo "  3. Ejecuta en la PC: sudo dhclient \$(ip link | grep 'usb\|rndis\|enx' | awk '{print \$2}' | tr -d ':')"
        echo "  4. Si no aparece la interfaz: sudo modprobe rndis_host"
    fi
    echo ""
}

_print_proxy_app_help() {
    local host="$1" port="$2"
    echo ""
    echo -e "  ${YELLOW}SOLUCIONES POR APP PROXY:${RESET}"
    echo ""
    echo -e "  ${BOLD}Every Proxy (recomendada):${RESET}"
    echo "  → Abre la app → botón de encendido en la parte superior"
    echo "  → Protocol: SOCKS5  |  Port: 1080"
    echo "  → El ícono de la notificación debe estar activo"
    echo ""
    echo -e "  ${BOLD}Drony:${RESET}"
    echo "  → Ajustes → Networks → agrega una regla para WIFI/USB"
    echo "  → El proxy escucha en 8020 por defecto"
    echo "  → Prueba: $0 --test socks5://$host:8020"
    echo ""
    echo -e "  ${BOLD}SocksDroid:${RESET}"
    echo "  → Habilita 'Listen on USB' además de WiFi"
    echo "  → Puerto por defecto: 1080"
    echo ""
    echo -e "  ${BOLD}ProxyDroid:${RESET}"
    echo "  → Proxy Host: 0.0.0.0  Port: 8123  Protocol: SOCKS5"
    echo "  → Activa 'Global Proxy'"
    echo "  → Prueba: $0 --test socks5://$host:8123"
    echo ""
    echo "  Puertos a escanear: ${PROXY_PORTS[*]}"
}

# ---------------------------------------------------------------------------
# Diagnosticar si el teléfono usa WiFi en vez de SIM
# ---------------------------------------------------------------------------
diagnose_wifi_bypass() {
    local proxy_ip="$1" direct_ip="$2" proxy_url="$3"
    if [ "$proxy_ip" = "$direct_ip" ]; then
        warn "La IP del proxy ($proxy_ip) es IGUAL a la del servidor"
        warn "El teléfono está usando WiFi en lugar de la SIM"
        echo ""
        echo -e "  ${YELLOW}FIX:${RESET} Desactiva el WiFi en el teléfono"
        echo "    Ajustes → WiFi → Desactivar (mantén solo datos móviles)"
        echo ""
        echo "  Luego vuelve a probar:"
        echo "    $0 --test $proxy_url"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Probar proxy con diagnóstico completo y reintentos
# ---------------------------------------------------------------------------
test_proxy() {
    local proxy_url="$1"
    local label="${2:-proxy}"
    local retries="${3:-1}"

    title "Probando $label: $proxy_url"
    check_deps || return 1

    info "Obteniendo IP directa del servidor..."
    DIRECT_IP=$(get_direct_ip)
    if [ -z "$DIRECT_IP" ]; then
        warn "No se pudo obtener la IP directa (¿sin internet?)"
        DIRECT_IP="desconocida"
    else
        info "IP directa del servidor: $DIRECT_IP"
    fi

    local attempt=1
    while [ $attempt -le $retries ]; do
        [ $retries -gt 1 ] && info "Intento $attempt / $retries..."

        PROXY_IP=$(get_proxy_ip "$proxy_url")

        if [ -n "$PROXY_IP" ]; then
            ok "Proxy responde — IP pública: ${BOLD}$PROXY_IP${RESET}"

            if ! diagnose_wifi_bypass "$PROXY_IP" "$DIRECT_IP" "$proxy_url"; then
                echo ""
                read -r -p "  Desactiva el WiFi y presiona Enter para reintentar (o Ctrl+C para salir)..." || true
                PROXY_IP=$(get_proxy_ip "$proxy_url")
                if [ -n "$PROXY_IP" ] && [ "$PROXY_IP" != "$DIRECT_IP" ]; then
                    ok "IP SIM confirmada: ${BOLD}$PROXY_IP${RESET}"
                else
                    fail "Sigue usando WiFi — verifica que WiFi esté desactivado"
                    return 1
                fi
            fi

            ok "IP SIM: ${BOLD}$PROXY_IP${RESET}  (servidor: $DIRECT_IP)"
            echo ""
            echo -e "  ${BOLD}Resultado:${RESET} proxy operativo, listo para registrar"
            return 0
        fi

        attempt=$((attempt + 1))
        [ $attempt -le $retries ] && sleep 3
    done

    fail "Sin respuesta del proxy tras $retries intento(s)"
    diagnose_proxy_failure "$proxy_url"
    return 1
}

# ---------------------------------------------------------------------------
# Escaneo automático de interfaces USB
# ---------------------------------------------------------------------------
auto_scan() {
    title "Interfaces USB conectadas"
    detect_usb_interfaces

    if [ ${#USB_IFACES[@]} -eq 0 ]; then
        fail "No se detectó ningún teléfono con USB tethering activo"
        echo ""
        echo -e "${YELLOW}  El teléfono está conectado pero no aparece como interfaz de red.${RESET}"
        echo ""
        echo -e "  ${BOLD}PASOS:${RESET}"
        echo "  1. Ajustes → Conexiones → Zona Activa y Anclaje → Anclaje USB"
        echo "  2. Acepta la confirmación en el teléfono"
        echo "  3. Ejecuta este script de nuevo"
        echo ""
        echo -e "  ${YELLOW}Si ya lo activaste y no aparece:${RESET}"
        echo "  • sudo modprobe rndis_host"
        echo "  • Cambia el modo USB en la notificación del teléfono:"
        echo "    'Transferencia de archivos' o 'Acceso a internet'"
        echo "  • Prueba otro cable USB (algunos son solo carga)"
        return 1
    fi

    title "Escaneo por interfaz"
    local found_any=0

    for iface in "${USB_IFACES[@]}"; do
        hr
        IP_LOCAL=$(get_local_ip "$iface")
        STATE=$(ip link show "$iface" 2>/dev/null | grep -oP "state \K\w+" || echo "?")
        GW=$(get_gateway_ip "$iface")

        echo -e "  Interfaz: ${BOLD}$iface${RESET}   Estado: $STATE"
        echo "  IP host:  ${IP_LOCAL:-sin IP — DHCP pendiente}"
        echo "  Gateway:  ${GW:-no detectado}"

        if [ -z "$IP_LOCAL" ]; then
            warn "Sin IP — ejecuta: sudo dhclient $iface"
            continue
        fi
        if [ -z "$GW" ]; then
            warn "Gateway no detectado — ejecuta: sudo dhclient $iface"
            continue
        fi

        echo ""
        info "Buscando proxy en $GW (puertos: ${PROXY_PORTS[*]})..."

        local proxy_found=0 open_port="" detected_url=""

        for port in "${PROXY_PORTS[@]}"; do
            if timeout 2 bash -c "echo >/dev/tcp/$GW/$port" 2>/dev/null; then
                open_port="$port"
                info "Puerto $port abierto — detectando protocolo..."

                detected_url=$(detect_proxy_protocol "$GW" "$port" 2>/dev/null || echo "")
                if [ -n "$detected_url" ]; then
                    proxy_found=1
                    break
                else
                    warn "Puerto $port abierto pero no devuelve IP (la app puede estar apagada)"
                fi
            fi
        done

        if [ $proxy_found -eq 1 ] && [ -n "$detected_url" ]; then
            DIRECT_IP=$(get_direct_ip)
            PROXY_IP=$(get_proxy_ip "$detected_url")
            PROTO=$(echo "$detected_url" | grep -oP '^[a-z0-9]+(?=://)')

            if [ -z "$PROXY_IP" ]; then
                fail "Proxy $detected_url no devuelve IP pública"
                diagnose_proxy_failure "$detected_url"
            elif [ "$PROXY_IP" = "$DIRECT_IP" ]; then
                ok "Proxy activo [$PROTO], pero IP igual al servidor (teléfono usa WiFi)"
                warn "Desactiva el WiFi en el teléfono para usar la SIM"
                echo "    Una vez desactivado: $0 --test $detected_url"
            else
                ok "Proxy SIM funcionando ${BOLD}[$PROTO]${RESET}"
                ok "IP SIM: ${BOLD}$PROXY_IP${RESET}  (servidor directo: $DIRECT_IP)"
                echo ""
                echo -e "  ${BOLD}Para registrar este nodo:${RESET}"
                echo "    $0 --add"
                echo "  O si ya sabes el ID:"
                echo "    $0 --fix EXISTING_NODE_ID"
                found_any=1
            fi
        else
            if [ -n "$open_port" ]; then
                fail "Puerto(s) abiertos pero ningún protocolo responde (app apagada o sin datos SIM)"
                _print_proxy_app_help "$GW" "$open_port"
            else
                fail "Ningún puerto respondió en $GW"
                diagnose_proxy_failure "socks5://$GW:1080"
            fi
        fi
    done

    hr
    echo ""
    echo -e "${BOLD}=== Comandos útiles ===${RESET}"
    echo "  Agregar a DB:           $0 --add"
    echo "  Ver nodos registrados:  $0 --list"
    echo "  Estado de nodos:        $0 --status"
    echo "  Probar proxy manual:    $0 --test socks5://IP:PUERTO"
    echo ""
}

# ---------------------------------------------------------------------------
# Flujo interactivo para agregar un teléfono nuevo
# ---------------------------------------------------------------------------
add_phone_interactive() {
    title "Agregar teléfono al pool de proxies"

    step "Paso 1 — Checklist del teléfono"
    echo "  □ WiFi DESACTIVADO (solo datos SIM activos)"
    echo "  □ Datos móviles ACTIVADOS con señal"
    echo "  □ App proxy instalada y configurada (Every Proxy: SOCKS5, puerto 1080)"
    echo "  □ Servidor proxy INICIADO en la app"
    echo "  □ Cable USB conectado"
    echo "  □ USB Tethering ACTIVADO en el teléfono"
    echo "    Samsung:  Ajustes → Conexiones → Zona activa → Anclaje USB"
    echo "    Xiaomi:   Ajustes → Conexión y compartición → Anclaje USB"
    echo "    Motorola: Ajustes → Red e internet → Anclaje de red → Anclaje USB"
    echo "    OnePlus:  Ajustes → WiFi e internet → Zona y anclaje → Anclaje USB"
    echo ""
    read -r -p "  ¿Todo completado? [s/N]: " CONFIRM
    [[ ! "$CONFIRM" =~ ^[sS]$ ]] && { echo "  Completa los pasos y vuelve a ejecutar: $0 --add"; exit 0; }

    step "Paso 2 — Detectando interfaz USB"
    detect_usb_interfaces

    if [ ${#USB_IFACES[@]} -eq 0 ]; then
        fail "No se detectaron interfaces USB"
        echo "  • Comprueba que el USB tethering esté activo"
        echo "  • Ejecuta: sudo modprobe rndis_host"
        exit 1
    fi

    if [ ${#USB_IFACES[@]} -gt 1 ]; then
        echo "  Se detectaron varias interfaces:"
        for i in "${!USB_IFACES[@]}"; do
            GW_TMP=$(get_gateway_ip "${USB_IFACES[$i]}" || echo "?")
            echo "  $((i+1)). ${USB_IFACES[$i]}  (gateway: ${GW_TMP:-?})"
        done
        read -r -p "  ¿Cuál es el nuevo teléfono? (número): " IDX
        SELECTED_IFACE="${USB_IFACES[$((IDX-1))]}"
    else
        SELECTED_IFACE="${USB_IFACES[0]}"
    fi
    ok "Interfaz seleccionada: $SELECTED_IFACE"

    GW=$(get_gateway_ip "$SELECTED_IFACE")
    if [ -z "$GW" ]; then
        fail "No se pudo obtener el gateway de $SELECTED_IFACE"
        echo "  Intenta: sudo dhclient $SELECTED_IFACE"
        exit 1
    fi
    IP_LOCAL=$(get_local_ip "$SELECTED_IFACE")
    ok "Gateway (IP teléfono): $GW  |  IP local: $IP_LOCAL"

    step "Paso 3 — Detectando proxy"
    DETECTED_URL=""
    DETECTED_PORT=""

    echo "  Buscando en puertos: ${PROXY_PORTS[*]}"
    echo ""

    for port in "${PROXY_PORTS[@]}"; do
        if timeout 2 bash -c "echo >/dev/tcp/$GW/$port" 2>/dev/null; then
            info "Puerto $port abierto — detectando protocolo..."
            url=$(detect_proxy_protocol "$GW" "$port" 2>/dev/null || echo "")
            if [ -n "$url" ]; then
                DETECTED_URL="$url"
                DETECTED_PORT="$port"
                ok "Proxy detectado: $url"
                break
            else
                warn "Puerto $port abierto pero sin respuesta de IP"
                echo "    Causas posibles:"
                echo "    a) La app proxy no está iniciada → ábrela y actívala"
                echo "    b) El teléfono no tiene datos SIM → verifica señal y saldo"
                echo "    c) WiFi activo desviando el tráfico → desactívalo"
            fi
        fi
    done

    if [ -z "$DETECTED_URL" ]; then
        fail "No se detectó proxy automáticamente en $GW"
        echo ""
        echo "  Opciones:"
        echo "  A) Ingresar URL manualmente"
        echo "  B) Cancelar y revisar la configuración del teléfono"
        read -r -p "  ¿Continuar manualmente? [A/B]: " MANUAL_CHOICE

        if [[ "${MANUAL_CHOICE^^}" == "A" ]]; then
            echo ""
            echo "  Formatos aceptados:"
            echo "    socks5://IP:PUERTO  (ej: socks5://$GW:1080)"
            echo "    http://IP:PUERTO    (ej: http://$GW:8080)"
            read -r -p "  URL del proxy: " DETECTED_URL
            [ -z "$DETECTED_URL" ] && { fail "URL vacía"; exit 1; }
        else
            echo ""
            echo "  Verifica en el teléfono:"
            _print_proxy_app_help "$GW" "1080"
            exit 0
        fi
    fi

    step "Paso 4 — Verificando IP SIM vs WiFi"
    DIRECT_IP=$(get_direct_ip)
    info "IP directa del servidor: $DIRECT_IP"

    local retry_count=0
    while true; do
        PROXY_IP=$(get_proxy_ip "$DETECTED_URL")

        if [ -z "$PROXY_IP" ]; then
            fail "El proxy no devuelve IP pública"
            diagnose_proxy_failure "$DETECTED_URL"
            exit 1
        fi

        if [ "$PROXY_IP" = "$DIRECT_IP" ]; then
            warn "El teléfono usa WiFi (IP igual al servidor: $DIRECT_IP)"
            echo ""
            retry_count=$((retry_count + 1))
            if [ $retry_count -ge 3 ]; then
                fail "3 intentos sin cambio — cancela y desactiva el WiFi manualmente"
                exit 1
            fi
            echo "  Desactiva el WiFi en el teléfono y presiona Enter para reintentar..."
            read -r || true
        else
            break
        fi
    done

    ok "IP SIM confirmada: ${BOLD}$PROXY_IP${RESET}  (servidor: $DIRECT_IP)"

    step "Paso 5 — Registrar en la DB"
    EXISTING_COUNT=$($PYTHON -c "
import sys; sys.path.insert(0, '$DB_DIR')
import job_store; job_store.init_db()
print(len(job_store.list_proxy_nodes()))
" 2>/dev/null || echo "0")
    NEXT_NUM=$((EXISTING_COUNT + 1))

    echo ""
    echo "  Datos detectados:"
    echo "    Servidor: $DETECTED_URL"
    echo "    IP SIM:   $PROXY_IP"
    echo ""

    read -r -p "  ID del nodo (ej: phone${NEXT_NUM}_sim): " NODE_ID
    read -r -p "  Etiqueta (ej: Teléfono $NEXT_NUM SIM Telcel): " NODE_LABEL
    read -r -p "  Notas (opcional): " NODE_NOTES

    [ -z "$NODE_ID" ] || [ -z "$NODE_LABEL" ] && { fail "ID y etiqueta son obligatorios"; exit 1; }

    PROTO=$(echo "$DETECTED_URL" | grep -oP '^[a-z0-9]+(?=://)')

    $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
job_store.upsert_proxy_node(
    '$NODE_ID', '$NODE_LABEL', '$DETECTED_URL',
    notes='Iface: $SELECTED_IFACE | IP local: $IP_LOCAL | Proto: $PROTO | ${NODE_NOTES}'
)
job_store.update_proxy_node_status('$NODE_ID', last_ip='$PROXY_IP', reset_fails=True)
print('  OK: nodo guardado en DB')
PYEOF

    ok "Nodo '${NODE_ID}' registrado"

    step "Paso 6 — Asignar cuentas (opcional)"
    echo "  Para asignar manualmente:"
    echo "    $0 --assign $NODE_ID NOMBRE_CUENTA"
    echo ""
    echo "  Para asignación automática de cuentas sin proxy:"
    read -r -p "  ¿Asignar cuentas sin proxy a este nodo ahora? [s/N]: " DO_ASSIGN
    if [[ "$DO_ASSIGN" =~ ^[sS]$ ]]; then
        $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store, proxy_manager, json
job_store.init_db()
assigned = 0
for c in job_store.list_accounts_full():
    a = job_store.get_proxy_assignment(c['name'])
    if not a:
        groups = json.loads(c.get('groups') or '[]')
        proxy_manager.assign_proxy_to_account(c['name'], groups)
        print(f"  Asignado: {c['name']}")
        assigned += 1
if assigned == 0:
    print("  Sin cuentas disponibles sin proxy asignado")
PYEOF
    fi

    hr
    ok "Teléfono configurado y listo"
    echo ""
    echo "  Ver nodos:   $0 --list"
    echo "  Estado:      $0 --status"
    echo "  Editar:      $0 --edit $NODE_ID"
    hr
}

# ---------------------------------------------------------------------------
# Listar nodos de la DB
# ---------------------------------------------------------------------------
list_db_nodes() {
    title "Nodos proxy registrados"
    $PYTHON - <<'PYEOF'
import sys, os; sys.path.insert(0, os.environ.get('DB_DIR', 'facebook_auto_poster'))
import job_store
job_store.init_db()
nodes = job_store.list_proxy_nodes()
if not nodes:
    print("  Sin nodos registrados.\n  Para agregar: ./setup_phone_proxy.sh --add")
    sys.exit(0)

assignments = {}
for a in job_store.list_proxy_assignments():
    assignments.setdefault(a['primary_node'], []).append(a['account_name'])

status_color = {
    'online':      '\033[0;32m',
    'offline':     '\033[0;31m',
    'maintenance': '\033[1;33m',
}
reset = '\033[0m'
bold  = '\033[1m'

for n in nodes:
    color  = status_color.get(n['status'], '')
    accs   = assignments.get(n['id'], [])
    ip_str = n.get('last_seen_ip') or '?'
    print(f"  {color}{n['status'].upper():12}{reset} {bold}{n['id']:22}{reset} {n['server']:38}")
    print(f"  {'':12} IP SIM: {ip_str:20} Cuentas: {len(accs)}")
    if n.get('notes'):
        print(f"  {'':12} {n['notes']}")
    for acc in accs:
        print(f"  {'':12} └─ {acc}")
    print()
PYEOF
}

# ---------------------------------------------------------------------------
# Health check de todos los nodos
# ---------------------------------------------------------------------------
check_status() {
    title "Health check de nodos"
    $PYTHON - <<'PYEOF'
import sys, os; sys.path.insert(0, os.environ.get('DB_DIR', 'facebook_auto_poster'))
import job_store, proxy_manager
job_store.init_db()
nodes = job_store.list_proxy_nodes()
if not nodes:
    print("  Sin nodos registrados.")
    sys.exit(0)

ok_sym   = '\033[0;32m✓\033[0m'
fail_sym = '\033[0;31m✗\033[0m'

for n in nodes:
    ok, ip = proxy_manager._check_node(n)
    sym = ok_sym if ok else fail_sym
    status = f"OK  IP: {ip}" if ok else "OFFLINE"
    print(f"  {sym} {n['id']:25} [{n['server']:38}] {status}")
PYEOF
}

# ---------------------------------------------------------------------------
# Información detallada de un nodo
# ---------------------------------------------------------------------------
info_node() {
    local node_id="$1"
    title "Detalle del nodo: $node_id"
    DB_DIR_PY="$DB_DIR" $PYTHON - <<PYEOF
import sys, os; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
n = job_store.get_proxy_node('$node_id')
if not n:
    print("  Nodo '$node_id' no encontrado")
    sys.exit(1)

bold = '\033[1m'; reset = '\033[0m'
fields = [
    ('ID',          n.get('id')),
    ('Etiqueta',    n.get('label')),
    ('Servidor',    n.get('server')),
    ('Status',      n.get('status')),
    ('Última IP',   n.get('last_seen_ip') or '?'),
    ('Fallos',      str(n.get('fail_count', 0))),
    ('Notas',       n.get('notes') or '—'),
    ('Creado',      n.get('created_at') or '?'),
    ('Actualizado', n.get('updated_at') or '?'),
]
for k, v in fields:
    print(f"  {bold}{k:14}{reset} {v}")

print()
accs = [a for a in job_store.list_proxy_assignments() if a['primary_node'] == '$node_id']
if accs:
    print(f"  {bold}Cuentas asignadas ({len(accs)}):${reset}")
    for a in accs:
        sec = f"  (fallback: {a['secondary_node']})" if a.get('secondary_node') else ''
        print(f"    • {a['account_name']}{sec}")
else:
    print("  Sin cuentas asignadas")
PYEOF
}

# ---------------------------------------------------------------------------
# Editar un nodo existente
# ---------------------------------------------------------------------------
edit_node() {
    local node_id="$1"
    title "Editar nodo: $node_id"

    # Mostrar valores actuales
    $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
n = job_store.get_proxy_node('$node_id')
if not n:
    print("  ERROR: nodo '$node_id' no encontrado")
    sys.exit(1)
print(f"  Etiqueta actual: {n.get('label','')}")
print(f"  Servidor actual: {n.get('server','')}")
print(f"  Notas actuales:  {n.get('notes','')}")
PYEOF

    echo ""
    echo "  (Deja en blanco para mantener el valor actual)"
    echo ""
    read -r -p "  Nueva etiqueta: " NEW_LABEL
    read -r -p "  Nuevo servidor (URL completa, ej: socks5://192.168.42.129:1080): " NEW_SERVER
    read -r -p "  Nuevas notas: " NEW_NOTES
    read -r -p "  Nuevo status [online/offline/maintenance] (Enter para no cambiar): " NEW_STATUS

    $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
n = job_store.get_proxy_node('$node_id')
if not n:
    print("  ERROR: nodo no encontrado")
    sys.exit(1)

label  = '$NEW_LABEL'  or n['label']
server = '$NEW_SERVER' or n['server']
notes  = '$NEW_NOTES'  or n.get('notes', '')
status = '$NEW_STATUS' or n['status']

job_store.upsert_proxy_node('$node_id', label, server, notes)

if '$NEW_STATUS' in ('online', 'offline', 'maintenance'):
    job_store.update_proxy_node_status('$node_id', status=status,
                                       reset_fails=(status == 'online'))

print(f"  Nodo '$node_id' actualizado")
print(f"  Etiqueta: {label}")
print(f"  Servidor: {server}")
PYEOF

    echo ""
    # Si cambió el servidor, verificar que funciona
    if [ -n "$NEW_SERVER" ]; then
        read -r -p "  ¿Probar el nuevo servidor ahora? [S/n]: " DO_TEST
        if [[ ! "${DO_TEST:-s}" =~ ^[nN]$ ]]; then
            test_proxy "$NEW_SERVER" "$node_id"
        fi
    fi
    ok "Edición completada"
}

# ---------------------------------------------------------------------------
# Eliminar nodo
# ---------------------------------------------------------------------------
remove_node() {
    local node_id="$1"
    title "Eliminar nodo: $node_id"

    $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
n = job_store.get_proxy_node('$node_id')
if not n:
    print("  ERROR: nodo '$node_id' no encontrado")
    sys.exit(1)
accs = [a for a in job_store.list_proxy_assignments() if a['primary_node'] == '$node_id']
print(f"  Nodo: {n['label']} — {n['server']}")
print(f"  Cuentas asignadas: {len(accs)}")
for a in accs:
    print(f"    • {a['account_name']}")
PYEOF

    echo ""
    read -r -p "  ¿Confirmar eliminación? Las cuentas asignadas quedarán sin proxy. [s/N]: " CONFIRM
    if [[ "$CONFIRM" =~ ^[sS]$ ]]; then
        $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
deleted = job_store.delete_proxy_node('$node_id')
print("  Nodo eliminado" if deleted else "  Error: nodo no encontrado")
PYEOF
        ok "Nodo '$node_id' eliminado"
    else
        info "Operación cancelada"
    fi
}

# ---------------------------------------------------------------------------
# Asignar proxy a cuenta manualmente
# ---------------------------------------------------------------------------
assign_proxy() {
    local node_id="$1" account="$2"
    title "Asignar nodo '$node_id' a cuenta '$account'"

    $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
n = job_store.get_proxy_node('$node_id')
if not n:
    print("  ERROR: nodo '$node_id' no encontrado")
    sys.exit(1)
accounts = job_store.list_accounts_full()
acc = next((a for a in accounts if a['name'] == '$account'), None)
if not acc:
    print("  ERROR: cuenta '$account' no encontrada")
    sys.exit(1)
job_store.upsert_proxy_assignment('$account', '$node_id', None)
print(f"  Cuenta '$account' asignada al nodo '$node_id' ({n['server']})")
PYEOF
    ok "Asignación completada"
}

# ---------------------------------------------------------------------------
# Quitar proxy de cuenta
# ---------------------------------------------------------------------------
unassign_proxy() {
    local account="$1"
    title "Quitar proxy de cuenta '$account'"
    $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
a = job_store.get_proxy_assignment('$account')
if not a:
    print("  La cuenta '$account' no tiene proxy asignado")
    sys.exit(0)
job_store.delete_proxy_assignment('$account')
print(f"  Proxy quitado de la cuenta '$account'")
PYEOF
    ok "Hecho"
}

# ---------------------------------------------------------------------------
# Re-detectar IP/Puerto de un nodo ya registrado (fix IP dinámica)
# ---------------------------------------------------------------------------
fix_node() {
    local node_id="$1"
    title "Re-detectar proxy para nodo: $node_id"

    # Obtener servidor actual
    CURRENT_SERVER=$($PYTHON - <<PYEOF 2>/dev/null || echo ""
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
n = job_store.get_proxy_node('$node_id')
print(n['server'] if n else '')
PYEOF
    )

    if [ -z "$CURRENT_SERVER" ]; then
        fail "Nodo '$node_id' no encontrado en DB"
        exit 1
    fi

    info "Servidor actual: $CURRENT_SERVER"
    echo ""

    detect_usb_interfaces
    if [ ${#USB_IFACES[@]} -eq 0 ]; then
        fail "No hay interfaces USB detectadas — conecta el teléfono"
        exit 1
    fi

    echo "  Interfaces disponibles:"
    for i in "${!USB_IFACES[@]}"; do
        GW_TMP=$(get_gateway_ip "${USB_IFACES[$i]}" || echo "?")
        echo "  $((i+1)). ${USB_IFACES[$i]}  (gateway: ${GW_TMP:-?})"
    done

    local iface_to_use
    if [ ${#USB_IFACES[@]} -eq 1 ]; then
        iface_to_use="${USB_IFACES[0]}"
    else
        read -r -p "  ¿Cuál interfaz es este nodo? (número): " IDX
        iface_to_use="${USB_IFACES[$((IDX-1))]}"
    fi

    GW=$(get_gateway_ip "$iface_to_use")
    if [ -z "$GW" ]; then
        fail "No se pudo obtener gateway de $iface_to_use"
        exit 1
    fi

    info "Escaneando $GW en puertos: ${PROXY_PORTS[*]}..."
    NEW_URL=""
    for port in "${PROXY_PORTS[@]}"; do
        if timeout 2 bash -c "echo >/dev/tcp/$GW/$port" 2>/dev/null; then
            url=$(detect_proxy_protocol "$GW" "$port" 2>/dev/null || echo "")
            if [ -n "$url" ]; then
                NEW_URL="$url"
                ok "Nuevo proxy detectado: $url"
                break
            fi
        fi
    done

    if [ -z "$NEW_URL" ]; then
        fail "No se detectó proxy en $GW"
        echo "  Verifica que la app proxy esté activa en el teléfono"
        exit 1
    fi

    DIRECT_IP=$(get_direct_ip)
    NEW_PROXY_IP=$(get_proxy_ip "$NEW_URL")

    if [ -z "$NEW_PROXY_IP" ] || [ "$NEW_PROXY_IP" = "$DIRECT_IP" ]; then
        warn "Proxy detectado pero no confirma IP SIM válida"
        [ "$NEW_PROXY_IP" = "$DIRECT_IP" ] && warn "Usando WiFi en vez de SIM — desactívalo"
        exit 1
    fi

    ok "IP SIM: ${BOLD}$NEW_PROXY_IP${RESET}"

    if [ "$NEW_URL" != "$CURRENT_SERVER" ]; then
        warn "El servidor cambió de $CURRENT_SERVER a $NEW_URL"
        read -r -p "  ¿Actualizar en DB? [S/n]: " DO_UPDATE
        if [[ ! "${DO_UPDATE:-s}" =~ ^[nN]$ ]]; then
            $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
n = job_store.get_proxy_node('$node_id')
job_store.upsert_proxy_node('$node_id', n['label'], '$NEW_URL', n.get('notes',''))
job_store.update_proxy_node_status('$node_id', last_ip='$NEW_PROXY_IP', reset_fails=True)
print("  DB actualizada")
PYEOF
        fi
    else
        $PYTHON - <<PYEOF
import sys; sys.path.insert(0, '$DB_DIR')
import job_store
job_store.init_db()
job_store.update_proxy_node_status('$node_id', last_ip='$NEW_PROXY_IP', reset_fails=True)
print("  IP SIM actualizada en DB")
PYEOF
    fi

    ok "Nodo '$node_id' sincronizado"
}

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
print_header() {
    echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║   Facebook Auto-Poster — Gestión de Proxies SIM     ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
export DB_DIR
print_header
check_deps || exit 1

case "${1:-}" in
    --add)
        add_phone_interactive
        ;;
    --test)
        [ -z "${2:-}" ] && { echo "Uso: $0 --test socks5://IP:PUERTO"; exit 1; }
        test_proxy "$2" "proxy" "${3:-1}"
        ;;
    --list)
        list_db_nodes
        ;;
    --status)
        check_status
        ;;
    --info)
        [ -z "${2:-}" ] && { echo "Uso: $0 --info NODE_ID"; exit 1; }
        info_node "$2"
        ;;
    --edit)
        [ -z "${2:-}" ] && { echo "Uso: $0 --edit NODE_ID"; exit 1; }
        edit_node "$2"
        ;;
    --remove|--delete)
        [ -z "${2:-}" ] && { echo "Uso: $0 --remove NODE_ID"; exit 1; }
        remove_node "$2"
        ;;
    --assign)
        [ -z "${2:-}" ] || [ -z "${3:-}" ] && { echo "Uso: $0 --assign NODE_ID CUENTA"; exit 1; }
        assign_proxy "$2" "$3"
        ;;
    --unassign)
        [ -z "${2:-}" ] && { echo "Uso: $0 --unassign CUENTA"; exit 1; }
        unassign_proxy "$2"
        ;;
    --fix)
        [ -z "${2:-}" ] && { echo "Uso: $0 --fix NODE_ID"; exit 1; }
        fix_node "$2"
        ;;
    --help|-h)
        echo ""
        echo "  Comandos:"
        echo "    (sin args)              Escaneo automático de teléfonos"
        echo "    --add                   Agregar teléfono nuevo (interactivo)"
        echo "    --test URL [REINTENTOS] Probar proxy específico"
        echo "    --list                  Listar nodos en DB"
        echo "    --status                Health check de todos los nodos"
        echo "    --info NODE_ID          Detalles de un nodo"
        echo "    --edit NODE_ID          Editar etiqueta / servidor / notas"
        echo "    --remove NODE_ID        Eliminar nodo de la DB"
        echo "    --fix NODE_ID           Re-detectar IP/puerto (IP dinámica)"
        echo "    --assign NODE_ID CUENTA Asignar proxy a una cuenta"
        echo "    --unassign CUENTA       Quitar proxy de una cuenta"
        echo ""
        ;;
    *)
        auto_scan
        ;;
esac
