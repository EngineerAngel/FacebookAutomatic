#!/usr/bin/env bash
# setup.sh — Instalación unificada del Facebook Auto-Poster
# Compatible con: Ubuntu 20.04+, macOS 12+ (Intel y Apple Silicon)
#
# Uso:
#   chmod +x setup.sh
#   ./setup.sh
#
# Qué hace:
#   1. Detecta OS y arquitectura
#   2. Instala dependencias Python
#   3. Instala el binario de Chromium parcheado (Patchright)
#   4. Instala cloudflared según el OS
#   5. Instala dependencias de display si es Ubuntu sin DISPLAY

set -euo pipefail

OS=$(uname -s)
ARCH=$(uname -m)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/facebook_auto_poster"

echo ""
echo "======================================================"
echo "  Facebook Auto-Poster — Setup"
echo "  OS: $OS | Arch: $ARCH"
echo "======================================================"
echo ""

# ── 1. Python virtual environment ──────────────────────────────────────────

if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    echo "[1/4] Creando entorno virtual Python..."
    python3 -m venv "$SCRIPT_DIR/.venv"
fi

# Activar venv
source "$SCRIPT_DIR/.venv/bin/activate"
echo "[1/4] Instalando dependencias Python..."
pip install --upgrade pip --quiet
pip install -r "$APP_DIR/requirements.txt" --quiet
echo "      ✓ Dependencias Python instaladas"

# ── 2. Patchright — Chromium parcheado ─────────────────────────────────────

echo "[2/4] Instalando Chromium parcheado (Patchright)..."
if [ "$ARCH" = "arm64" ] && [ "$OS" = "Darwin" ]; then
    echo "      Detectado Apple Silicon (M1/M2/M3)"
fi
patchright install chromium
echo "      ✓ Chromium parcheado listo"

# ── 3. Cloudflared ─────────────────────────────────────────────────────────

echo "[3/4] Instalando cloudflared..."

if command -v cloudflared &>/dev/null; then
    echo "      ✓ cloudflared ya instalado ($(cloudflared --version 2>&1 | head -1))"
elif [ "$OS" = "Darwin" ]; then
    if command -v brew &>/dev/null; then
        brew install cloudflared
        echo "      ✓ cloudflared instalado via Homebrew"
    else
        echo "      ⚠ Homebrew no encontrado. Instalar manualmente:"
        echo "        /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        echo "        brew install cloudflared"
    fi
elif [ "$OS" = "Linux" ]; then
    # Intentar via apt primero (Debian/Ubuntu con repo oficial)
    if command -v apt-get &>/dev/null; then
        echo "      Descargando cloudflared (GitHub releases)..."
        ARCH_DEB="amd64"
        [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ] && ARCH_DEB="arm64"
        CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH_DEB}"
        sudo curl -fsSL "$CF_URL" -o /usr/local/bin/cloudflared
        sudo chmod +x /usr/local/bin/cloudflared
        echo "      ✓ cloudflared instalado en /usr/local/bin/cloudflared"
    else
        echo "      ⚠ No se pudo instalar cloudflared automáticamente."
        echo "        Ver: https://developers.cloudflare.com/cloudflared/get-started/linux"
    fi
fi

# ── 4. Dependencias de display (solo Ubuntu/Linux con entorno gráfico) ──────

echo "[4/4] Verificando dependencias de display..."

if [ "$OS" = "Linux" ]; then
    if command -v apt-get &>/dev/null; then
        # python3-xlib y scrot son necesarios para Emunium (mouse Bézier OS-level)
        # Solo aplican si hay entorno gráfico (X11 o Wayland)
        PKGS_NEEDED=()
        dpkg -l python3-xlib &>/dev/null 2>&1 || PKGS_NEEDED+=("python3-xlib")
        dpkg -l scrot &>/dev/null 2>&1 || PKGS_NEEDED+=("scrot")

        if [ ${#PKGS_NEEDED[@]} -gt 0 ]; then
            echo "      Instalando: ${PKGS_NEEDED[*]}"
            sudo apt-get install -y "${PKGS_NEEDED[@]}" --quiet
        fi
        echo "      ✓ Dependencias de display OK"
    fi
else
    echo "      ✓ Mac — sin dependencias adicionales de display"
fi

# ── Configuración inicial ───────────────────────────────────────────────────

echo ""
echo "======================================================"
echo "  Setup completado"
echo "======================================================"
echo ""
echo "Próximos pasos:"
echo "  1. Copiar y configurar el archivo de entorno:"
echo "     cp facebook_auto_poster/.env.example facebook_auto_poster/.env"
echo "     nano facebook_auto_poster/.env"
echo ""
echo "  2. Iniciar el servidor:"
echo "     source .venv/bin/activate"
echo "     python facebook_auto_poster/main.py"
echo ""
echo "  3. Panel de administración:"
echo "     http://localhost:5000/admin"
echo ""
