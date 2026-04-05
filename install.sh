#!/bin/bash
set -euo pipefail

# Dabio Installer — Ubuntu 24.04
# Compiles welle-cli v2.7 from source (primary), falls back to apt v2.4

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WELLE_TAG="v2.7"
WELLE_INTERNAL_PORT=7979
VENV_DIR="$SCRIPT_DIR/.venv"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Step 0: Prerequisites ────────────────────────────────────────────
info "Checking prerequisites..."

if [ "$(id -u)" -ne 0 ]; then
    error "This installer must be run as root (sudo ./install.sh)"
    exit 1
fi

# Detect Ubuntu version
if [ -f /etc/os-release ]; then
    . /etc/os-release
    if [ "$ID" != "ubuntu" ] || [[ "$VERSION_ID" != "24.04"* ]]; then
        warn "This installer targets Ubuntu 24.04. Detected: $ID $VERSION_ID"
    fi
fi

# ── Step 1: Kill any existing welle-cli / dabio processes ────────────
info "Cleaning up any existing processes..."
pkill -f "welle-cli" 2>/dev/null || true
pkill -f "dabio" 2>/dev/null || true
sleep 1

# ── Step 2: System dependencies ──────────────────────────────────────
info "Installing system dependencies..."
apt-get update -qq

# Common deps needed regardless of welle-cli installation method
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    rtl-sdr librtlsdr-dev librtlsdr2 \
    ffmpeg \
    avahi-daemon avahi-utils \
    libusb-1.0-0-dev \
    curl \
    2>/dev/null

# ── Step 3: Blacklist DVB-T kernel drivers ───────────────────────────
info "Blacklisting DVB-T kernel drivers for RTL-SDR..."
cat > /etc/modprobe.d/rtl-sdr-blacklist.conf << 'BLACKLIST'
blacklist dvb_usb_rtl28xxu
blacklist dvb_usb_rtl2832u
blacklist dvb_usb_v2
blacklist r820t
blacklist rtl2830
blacklist rtl2832
blacklist rtl2832_sdr
blacklist rtl2838
BLACKLIST
modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true

# ── Step 4: Install welle-cli ────────────────────────────────────────
WELLE_CLI=""
WELLE_VERSION=""

install_welle_from_source() {
    info "Compiling welle-cli ${WELLE_TAG} from source..."

    # Build dependencies
    apt-get install -y -qq \
        git build-essential cmake \
        libfaad-dev libmpg123-dev libfftw3-dev \
        libmp3lame-dev libasound2-dev \
        2>/dev/null

    local BUILD_DIR="/tmp/welle-build-$$"
    rm -rf "$BUILD_DIR"

    if git clone --branch "$WELLE_TAG" --depth 1 \
        https://github.com/AlbrechtL/welle.io.git "$BUILD_DIR" 2>/dev/null; then

        mkdir -p "$BUILD_DIR/build"
        cd "$BUILD_DIR/build"

        if cmake .. \
            -DBUILD_WELLE_IO=OFF \
            -DBUILD_WELLE_CLI=ON \
            -DRTLSDR=1 \
            2>/dev/null; then

            if make -j"$(nproc)" 2>/dev/null; then
                make install 2>/dev/null
                cd "$SCRIPT_DIR"
                rm -rf "$BUILD_DIR"

                if [ -x /usr/local/bin/welle-cli ]; then
                    WELLE_CLI="/usr/local/bin/welle-cli"
                    WELLE_VERSION="2.7"
                    info "welle-cli ${WELLE_TAG} compiled and installed successfully"
                    return 0
                fi
            fi
        fi

        cd "$SCRIPT_DIR"
        rm -rf "$BUILD_DIR"
    fi

    warn "Source compilation failed"
    return 1
}

install_welle_from_apt() {
    info "Installing welle-cli from apt (v2.4 fallback)..."
    if apt-get install -y -qq welle.io 2>/dev/null; then
        if [ -x /usr/bin/welle-cli ]; then
            WELLE_CLI="/usr/bin/welle-cli"
            WELLE_VERSION="2.4"
            info "welle-cli installed from apt (v2.4)"
            return 0
        fi
    fi
    error "apt installation also failed"
    return 1
}

# Check if already installed
if [ -x /usr/local/bin/welle-cli ]; then
    existing_ver=$(/usr/local/bin/welle-cli -v 2>&1 | grep -oP '\d+\.\d+' | head -1 || echo "")
    if [[ "$existing_ver" == "2.7"* ]] || [[ "$existing_ver" == "2.8"* ]]; then
        info "welle-cli v${existing_ver} already installed at /usr/local/bin/welle-cli"
        WELLE_CLI="/usr/local/bin/welle-cli"
        WELLE_VERSION="$existing_ver"
    fi
fi

if [ -z "$WELLE_CLI" ]; then
    # Try source first, then apt
    install_welle_from_source || install_welle_from_apt || {
        error "Could not install welle-cli via any method. Aborting."
        exit 1
    }
fi

info "Using welle-cli: $WELLE_CLI (v${WELLE_VERSION})"

# ── Step 5: Python virtualenv and dependencies ───────────────────────
info "Setting up Python virtualenv..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

# ── Step 6: Create data directory ────────────────────────────────────
mkdir -p "$SCRIPT_DIR/data"

# ── Step 7: Systemd service ──────────────────────────────────────────
info "Creating systemd service..."
cat > /etc/systemd/system/dabio.service << SYSTEMD
[Unit]
Description=Dabio DAB+ Radio Web Player
After=network.target avahi-daemon.service
Wants=avahi-daemon.service

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${VENV_DIR}/bin/python -m dabio
Restart=on-failure
RestartSec=5
Environment=PYTHONPATH=${SCRIPT_DIR}/src
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable dabio.service

# ── Step 8: Smoke test ───────────────────────────────────────────────
info "Running smoke test..."
SMOKE_OK=true

# Test welle-cli can start (briefly)
if [ -n "$WELLE_CLI" ]; then
    info "Testing welle-cli launch..."
    timeout 8 "$WELLE_CLI" -c 9C -w "$WELLE_INTERNAL_PORT" &
    WELLE_PID=$!
    sleep 4

    if kill -0 "$WELLE_PID" 2>/dev/null; then
        # Try to hit mux.json
        if curl -sf "http://127.0.0.1:${WELLE_INTERNAL_PORT}/mux.json" > /dev/null 2>&1; then
            info "welle-cli web server responding"
        else
            warn "welle-cli started but web server not responding (may need SDR device)"
        fi
        kill "$WELLE_PID" 2>/dev/null || true
        wait "$WELLE_PID" 2>/dev/null || true
    else
        warn "welle-cli exited quickly (may need SDR device connected)"
    fi
fi

# Test Python app can import
if "$VENV_DIR/bin/python" -c "import sys; sys.path.insert(0, '$SCRIPT_DIR/src'); import dabio" 2>/dev/null; then
    info "Python application imports OK"
else
    warn "Python import check failed"
    SMOKE_OK=false
fi

# ── Step 9: Summary ─────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
info "Dabio installation complete!"
echo ""
echo "  welle-cli:   $WELLE_CLI (v${WELLE_VERSION})"
echo "  Python venv: $VENV_DIR"
echo "  Config:      $SCRIPT_DIR/config.yaml"
echo "  Service:     dabio.service"
echo ""
echo "  To start:    sudo systemctl start dabio"
echo "  To run manually: cd $SCRIPT_DIR && PYTHONPATH=src .venv/bin/python -m dabio"
echo "  Mock mode:   Edit config.yaml → mock_mode: true"
echo ""
if [ "$WELLE_VERSION" = "2.4" ]; then
    warn "Using apt-packaged welle-cli v2.4 (fallback)."
    warn "POST /channel may not work — app will use process restart for retuning."
fi
echo "════════════════════════════════════════════════════════"
