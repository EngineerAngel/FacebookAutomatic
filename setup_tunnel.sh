#!/usr/bin/env bash
# setup_tunnel.sh — Túnel HTTPS estático para Facebook Auto-Poster
#
# Soporta dos backends, ambos con URL permanente:
#   A) ngrok        — dominio gratuito estático (*.ngrok-free.app)  ← recomendado sin dominio
#   B) Cloudflare   — dominio propio gestionado en Cloudflare
#
# El sistema detecta automáticamente cuál está configurado y lo usa.
#
# USO:
#   ./setup_tunnel.sh                # menú de opciones
#   ./setup_tunnel.sh --ngrok        # setup ngrok directamente
#   ./setup_tunnel.sh --cloudflare   # setup cloudflare directamente
#   ./setup_tunnel.sh --status       # ver configuración activa
#   ./setup_tunnel.sh --reset        # borrar y reconfigurar

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()    { echo -e "${GREEN}  ✓${RESET} $*"; }
fail()  { echo -e "${RED}  ✗${RESET} $*"; }
info()  { echo -e "${BLUE}  →${RESET} $*"; }
warn()  { echo -e "${YELLOW}  ⚠${RESET} $*"; }
step()  { echo -e "\n${CYAN}${BOLD}[$*]${RESET}"; }
hr()    { echo -e "${BOLD}──────────────────────────────────────────────${RESET}"; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/facebook_auto_poster"

# Archivos de estado (compartidos entre backends)
URL_FILE="$HOME/.cloudflared/fb-autoposter.url"          # URL pública final
BACKEND_FILE="$HOME/.cloudflared/fb-autoposter.backend"  # "ngrok" | "cloudflare"

# Cloudflare Named Tunnel
CF_CONFIG_FILE="$HOME/.cloudflared/config.yml"
CF_TUNNEL_NAME="fb-autoposter"

# ngrok
NGROK_CONFIG_FILE="$HOME/.config/ngrok/ngrok.yml"
NGROK_TOKEN_FILE="$HOME/.cloudflared/ngrok.token"       # guardamos token aquí también

# ---------------------------------------------------------------------------
get_api_port() {
    python3 -c "
import sys; sys.path.insert(0, '$PROJECT_DIR')
try:
    from config import CONFIG; print(CONFIG.get('api_port', 5000))
except Exception: print(5000)
" 2>/dev/null || echo "5000"
}

print_header() {
    echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║   Facebook Auto-Poster — Setup Túnel Estático       ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
}

# ---------------------------------------------------------------------------
# Estado actual
# ---------------------------------------------------------------------------
show_status() {
    step "Estado del túnel"
    if [ ! -f "$URL_FILE" ]; then
        warn "Sin túnel estático configurado — main.py usa quick tunnel (URL aleatoria)"
        echo "  Para configurar: $0"
        return 0
    fi

    URL=$(cat "$URL_FILE")
    BACKEND=$(cat "$BACKEND_FILE" 2>/dev/null || echo "desconocido")
    ok "Túnel estático activo"
    echo -e "  URL:     ${BOLD}$URL${RESET}"
    echo -e "  Backend: $BACKEND"

    case "$BACKEND" in
        cloudflare)
            [ -f "$CF_CONFIG_FILE" ] && grep -E "tunnel:|hostname:" "$CF_CONFIG_FILE" | sed 's/^/    /'
            ;;
        ngrok)
            [ -f "$NGROK_CONFIG_FILE" ] && grep "domain:" "$NGROK_CONFIG_FILE" | sed 's/^/    /'
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------
do_reset() {
    step "Borrando configuración"
    rm -f "$URL_FILE" "$BACKEND_FILE"
    ok "Configuración de URL borrada"
    echo "  Los archivos de cloudflare/ngrok no se tocan (solo se desvincula del sistema)"
    echo "  Para reconfigurar: $0"
}

# ---------------------------------------------------------------------------
# Menú de selección de backend
# ---------------------------------------------------------------------------
select_backend() {
    echo ""
    echo -e "  ${BOLD}¿Qué backend usar para el túnel estático?${RESET}"
    echo ""
    echo -e "  ${CYAN}A)${RESET} ngrok        — ${BOLD}Gratis, sin dominio propio${RESET}"
    echo "     URL: algo-algo-algo.ngrok-free.app"
    echo "     Requiere: cuenta ngrok gratuita (ngrok.com)"
    echo ""
    echo -e "  ${CYAN}B)${RESET} Cloudflare   — ${BOLD}Con dominio propio en Cloudflare${RESET}"
    echo "     URL: api.tudominio.com (totalmente personalizado)"
    echo "     Requiere: dominio gestionado en Cloudflare"
    echo ""
    read -r -p "  Elige [A/B]: " CHOICE
    case "${CHOICE^^}" in
        A) setup_ngrok ;;
        B) setup_cloudflare ;;
        *) fail "Opción inválida"; exit 1 ;;
    esac
}

# ===========================================================================
# BACKEND A — ngrok
# ===========================================================================
install_ngrok() {
    if command -v ngrok &>/dev/null; then
        ok "ngrok ya instalado: $(ngrok version 2>/dev/null | head -1)"
        return 0
    fi
    info "Instalando ngrok..."
    curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
        | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
    echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
        | sudo tee /etc/apt/sources.list.d/ngrok.list >/dev/null
    sudo apt-get update -qq && sudo apt-get install -y ngrok
    ok "ngrok instalado"
}

setup_ngrok() {
    step "Setup ngrok — dominio estático gratuito"
    echo ""
    echo "  Necesitas una cuenta ngrok gratuita (si no tienes, créala en ngrok.com)."
    echo ""

    # Instalar ngrok si no está
    install_ngrok

    # Authtoken
    step "Paso 1 — Authtoken"
    echo "  Ve a: https://dashboard.ngrok.com/get-started/your-authtoken"
    echo "  Copia el token y pégalo aquí."
    echo ""
    read -r -p "  Authtoken: " NGROK_TOKEN
    if [ -z "$NGROK_TOKEN" ]; then
        fail "Token vacío"; exit 1
    fi

    ngrok config add-authtoken "$NGROK_TOKEN"
    echo "$NGROK_TOKEN" > "$NGROK_TOKEN_FILE"
    chmod 600 "$NGROK_TOKEN_FILE"
    ok "Token configurado"

    # Dominio estático
    step "Paso 2 — Dominio estático"
    echo "  Ve a: https://dashboard.ngrok.com/domains"
    echo "  Crea un dominio gratuito (botón 'New Domain' → 'Get free domain')"
    echo "  Recibirás algo como: algo-algo-algo.ngrok-free.app"
    echo ""
    read -r -p "  Dominio (ej: algo-algo-algo.ngrok-free.app): " NGROK_DOMAIN

    NGROK_DOMAIN="${NGROK_DOMAIN#https://}"
    NGROK_DOMAIN="${NGROK_DOMAIN%%/*}"

    if [ -z "$NGROK_DOMAIN" ]; then
        fail "Dominio vacío"; exit 1
    fi

    # Escribir config ngrok
    step "Paso 3 — Escribiendo configuración"
    API_PORT=$(get_api_port)
    mkdir -p "$(dirname "$NGROK_CONFIG_FILE")"

    cat > "$NGROK_CONFIG_FILE" << EOF
version: "3"
agent:
  authtoken: $NGROK_TOKEN

tunnels:
  fb-autoposter:
    proto: http
    addr: $API_PORT
    domain: $NGROK_DOMAIN
EOF
    ok "Config ngrok escrita en $NGROK_CONFIG_FILE"

    # Guardar URL y backend
    STATIC_URL="https://$NGROK_DOMAIN"
    echo "$STATIC_URL" > "$URL_FILE"
    echo "ngrok" > "$BACKEND_FILE"

    # Verificación rápida
    step "Paso 4 — Verificación"
    info "Iniciando ngrok para verificar..."
    ngrok start fb-autoposter --config "$NGROK_CONFIG_FILE" &
    NGROK_PID=$!
    sleep 6

    if curl -s --max-time 8 "$STATIC_URL" &>/dev/null; then
        ok "ngrok respondiendo en $STATIC_URL"
    else
        warn "ngrok no responde aún (normal si Flask no está corriendo)"
        info "El túnel se activará cuando arranques: python main.py"
    fi
    kill $NGROK_PID 2>/dev/null || true
    wait $NGROK_PID 2>/dev/null || true

    _print_success "$STATIC_URL" "ngrok"
}

# ===========================================================================
# BACKEND B — Cloudflare Named Tunnel
# ===========================================================================
setup_cloudflare() {
    step "Setup Cloudflare Named Tunnel"
    echo ""

    if ! command -v cloudflared &>/dev/null; then
        fail "cloudflared no encontrado"
        echo "  Instalar: sudo apt install cloudflared"
        exit 1
    fi
    ok "cloudflared $(cloudflared --version 2>/dev/null | head -1)"

    # Login
    step "Paso 1 — Autenticación con Cloudflare"
    echo "  Se abrirá el navegador. Si el servidor no tiene browser,"
    echo "  copia la URL que aparezca en consola y ábrela en otro equipo."
    echo ""
    read -r -p "  Presiona Enter para iniciar..."
    cloudflared tunnel login
    ok "Autenticación completada"

    # Crear o reusar túnel
    step "Paso 2 — Creando túnel '$CF_TUNNEL_NAME'"
    EXISTING=$(cloudflared tunnel list 2>/dev/null | grep "$CF_TUNNEL_NAME" | awk '{print $1}') || EXISTING=""
    if [ -n "$EXISTING" ]; then
        warn "Túnel '$CF_TUNNEL_NAME' ya existe (ID: $EXISTING)"
        read -r -p "  ¿Reusar? [S/n]: " REUSE
        if [[ "${REUSE:-s}" =~ ^[nN]$ ]]; then
            cloudflared tunnel delete "$CF_TUNNEL_NAME" 2>/dev/null || true
            cloudflared tunnel create "$CF_TUNNEL_NAME"
        fi
    else
        cloudflared tunnel create "$CF_TUNNEL_NAME"
    fi

    TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | grep "$CF_TUNNEL_NAME" | awk '{print $1}')
    ok "Tunnel ID: $TUNNEL_ID"

    # Hostname
    step "Paso 3 — Hostname"
    echo "  Dominio que apunte a Cloudflare (ej: api.tudominio.com)"
    echo ""
    read -r -p "  Hostname: " CF_HOSTNAME
    CF_HOSTNAME="${CF_HOSTNAME#https://}"; CF_HOSTNAME="${CF_HOSTNAME%%/*}"

    cloudflared tunnel route dns "$CF_TUNNEL_NAME" "$CF_HOSTNAME"
    ok "DNS configurado: $CF_HOSTNAME → $CF_TUNNEL_NAME"

    # config.yml
    step "Paso 4 — Escribiendo configuración"
    API_PORT=$(get_api_port)
    CREDS_FILE="$HOME/.cloudflared/$TUNNEL_ID.json"

    cat > "$CF_CONFIG_FILE" << EOF
tunnel: $TUNNEL_ID
credentials-file: $CREDS_FILE

ingress:
  - hostname: $CF_HOSTNAME
    service: http://localhost:$API_PORT
  - service: http_status:404
EOF
    ok "Config escrita en $CF_CONFIG_FILE"

    STATIC_URL="https://$CF_HOSTNAME"
    echo "$STATIC_URL" > "$URL_FILE"
    echo "cloudflare" > "$BACKEND_FILE"

    # Verificación
    step "Paso 5 — Verificación"
    cloudflared tunnel run "$CF_TUNNEL_NAME" &
    CF_PID=$!
    sleep 8
    if curl -s --max-time 10 "$STATIC_URL/health" &>/dev/null; then
        ok "Túnel respondiendo en $STATIC_URL"
    else
        warn "No responde aún (normal si Flask no está activo)"
    fi
    kill $CF_PID 2>/dev/null || true
    wait $CF_PID 2>/dev/null || true

    _print_success "$STATIC_URL" "cloudflare"
}

# ---------------------------------------------------------------------------
_print_success() {
    local url="$1" backend="$2"
    hr
    echo ""
    ok "Configuración completada — backend: $backend"
    echo ""
    echo -e "  URL permanente: ${BOLD}$url${RESET}"
    echo ""
    echo "  Esta URL NO cambia aunque reinicias el servidor."
    echo ""
    echo "  Reinicia el sistema para activarlo:"
    echo "    python facebook_auto_poster/main.py"
    echo ""
    hr
}

# ===========================================================================
# Main
# ===========================================================================
print_header

case "${1:-}" in
    --ngrok)       setup_ngrok ;;
    --cloudflare)  setup_cloudflare ;;
    --status)      show_status ;;
    --reset)       do_reset ;;
    --help|-h)
        echo ""
        echo "  (sin args)      Menú de selección de backend"
        echo "  --ngrok         Setup ngrok (gratis, sin dominio)"
        echo "  --cloudflare    Setup Cloudflare (con dominio propio)"
        echo "  --status        Ver URL configurada"
        echo "  --reset         Desvincular configuración actual"
        echo ""
        ;;
    *)
        if [ -f "$URL_FILE" ]; then
            show_status
            echo ""
            read -r -p "  ¿Reconfigurar? [s/N]: " REDO
            [[ "$REDO" =~ ^[sS]$ ]] && select_backend
        else
            select_backend
        fi
        ;;
esac
