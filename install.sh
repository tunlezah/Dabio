#!/bin/bash
# Do NOT use set -euo pipefail — it causes silent exits on benign failures
# like grep-no-match in pipelines. We handle errors explicitly instead.
set +e

# ══════════════════════════════════════════════════════════════════════
#  Dabio Installer — DAB+ Radio Web Player
#  Target: Ubuntu 24.04 (Noble Numbat)
#
#  This installer will:
#    1. Clean up any prior Dabio / welle-cli processes
#    2. Install system dependencies
#    3. Blacklist DVB-T kernel drivers for RTL-SDR
#    4. Compile welle-cli v2.7 from source (or fall back to apt v2.4)
#    5. Create a Python virtualenv and install Python dependencies
#    6. Detect SDR hardware (or enable mock mode if absent)
#    7. Create and configure a systemd service
#    8. Start the Dabio web server
#    9. Verify the web server is responding
#   10. Print the access URL and status
# ══════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WELLE_TAG="v2.7"
WELLE_INTERNAL_PORT=7979
VENV_DIR="$SCRIPT_DIR/.venv"
CONFIG_FILE="$SCRIPT_DIR/config.yaml"
LOG_FILE="$SCRIPT_DIR/data/install.log"

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

# Step counter
STEP=0
TOTAL_STEPS=10

step()  { STEP=$((STEP + 1)); echo -e "\n${BLUE}[${STEP}/${TOTAL_STEPS}]${NC} ${BOLD}$*${NC}"; }
info()  { echo -e "  ${GREEN}✓${NC} $*"; }
warn()  { echo -e "  ${YELLOW}⚠${NC} $*"; }
error() { echo -e "  ${RED}✗${NC} $*"; }
detail(){ echo -e "    $*"; }

# Ensure log directory exists
mkdir -p "$SCRIPT_DIR/data"

# No ERR trap — we handle all errors explicitly with if/else

# Tee all output to log file
exec > >(tee -a "$LOG_FILE") 2>&1

echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Dabio Installer — DAB+ Radio Web Player${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════════${NC}"
echo "  Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Log file: $LOG_FILE"
echo ""

# Track overall status
INSTALL_OK=true
WARNINGS=()

# ─────────────────────────────────────────────────────────────────────
step "Checking prerequisites"
# ─────────────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    error "This installer must be run as root."
    echo ""
    echo "  Run: sudo ./install.sh"
    echo ""
    exit 1
fi
info "Running as root"

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    if [ "$ID" = "ubuntu" ] && [[ "$VERSION_ID" == "24.04"* ]]; then
        info "Ubuntu 24.04 detected"
    else
        warn "This installer targets Ubuntu 24.04. Detected: $ID $VERSION_ID"
        WARNINGS+=("Running on unsupported OS: $ID $VERSION_ID")
    fi
else
    warn "Could not detect OS version"
fi

# Check internet connectivity (needed for apt and git clone)
if curl -sf --max-time 5 https://github.com > /dev/null 2>&1; then
    info "Internet connectivity OK"
else
    warn "Cannot reach github.com — source compilation may fail"
    WARNINGS+=("No internet — will try apt only for welle-cli")
fi

# ─────────────────────────────────────────────────────────────────────
step "Cleaning up previous installations"
# ─────────────────────────────────────────────────────────────────────

# Stop systemd service if it exists
if systemctl is-active --quiet dabio.service 2>/dev/null; then
    systemctl stop dabio.service
    info "Stopped existing dabio service"
else
    detail "No existing dabio service running"
fi

# Kill orphaned processes (exclude this script's own PID)
KILLED=0
MY_PID=$$
for proc in welle-cli "python.*dabio" uvicorn; do
    PIDS=$(pgrep -f "$proc" 2>/dev/null | grep -v "^${MY_PID}$" || true)
    if [ -n "$PIDS" ]; then
        echo "$PIDS" | xargs kill 2>/dev/null || true
        KILLED=$((KILLED + 1))
    fi
done
if [ "$KILLED" -gt 0 ]; then
    info "Killed $KILLED orphaned process(es)"
    sleep 2  # Let ports release
else
    detail "No orphaned processes found"
fi

# Check if ports are free
for port in 8800 "$WELLE_INTERNAL_PORT"; do
    PORT_CHECK=$(ss -tlnp 2>/dev/null | grep ":${port} " || true)
    if [ -n "$PORT_CHECK" ]; then
        PID=$(echo "$PORT_CHECK" | grep -oP 'pid=\K\d+' | head -1 || true)
        warn "Port $port is in use (PID: ${PID:-unknown})"
        WARNINGS+=("Port $port occupied — may need to kill process or change config")
    fi
done
info "Port cleanup complete"

# ─────────────────────────────────────────────────────────────────────
step "Installing system dependencies"
# ─────────────────────────────────────────────────────────────────────

apt-get update -qq >> "$LOG_FILE" 2>&1

SYSTEM_PKGS=(
    python3 python3-venv python3-pip
    rtl-sdr librtlsdr-dev librtlsdr2
    ffmpeg
    avahi-daemon avahi-utils
    libusb-1.0-0-dev
    curl
)

for pkg in "${SYSTEM_PKGS[@]}"; do
    if dpkg -s "$pkg" > /dev/null 2>&1; then
        detail "$pkg — already installed"
    else
        if apt-get install -y -qq "$pkg" >> "$LOG_FILE" 2>&1; then
            info "$pkg — installed"
        else
            error "$pkg — FAILED to install"
            INSTALL_OK=false
        fi
    fi
done

# Ensure avahi is running (needed for Chromecast mDNS)
if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    info "avahi-daemon is running"
else
    systemctl start avahi-daemon 2>/dev/null || true
    if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
        info "avahi-daemon started"
    else
        warn "avahi-daemon not running — Chromecast discovery will not work"
        WARNINGS+=("avahi-daemon failed to start")
    fi
fi

# ─────────────────────────────────────────────────────────────────────
step "Blacklisting DVB-T kernel drivers"
# ─────────────────────────────────────────────────────────────────────

BLACKLIST_FILE="/etc/modprobe.d/rtl-sdr-blacklist.conf"
cat > "$BLACKLIST_FILE" << 'BLACKLIST'
# Blacklist DVB-T drivers so RTL-SDR can use the device directly
blacklist dvb_usb_rtl28xxu
blacklist dvb_usb_rtl2832u
blacklist dvb_usb_v2
blacklist r820t
blacklist rtl2830
blacklist rtl2832
blacklist rtl2832_sdr
blacklist rtl2838
BLACKLIST
info "Written $BLACKLIST_FILE"

# Unload if currently loaded
if command -v lsmod > /dev/null 2>&1; then
    for mod in dvb_usb_rtl28xxu dvb_usb_rtl2832u rtl2832_sdr; do
        if lsmod 2>/dev/null | grep -q "^$mod"; then
            modprobe -r "$mod" 2>/dev/null && info "Unloaded kernel module: $mod" || warn "Could not unload $mod (may need reboot)"
        fi
    done
fi
info "DVB-T drivers blacklisted"

# ─────────────────────────────────────────────────────────────────────
step "Installing welle-cli"
# ─────────────────────────────────────────────────────────────────────

WELLE_CLI=""
WELLE_VERSION=""

install_welle_from_source() {
    info "Attempting to compile welle-cli ${WELLE_TAG} from source..."

    # Build dependencies
    BUILD_DEPS=(git build-essential cmake libfaad-dev libmpg123-dev libfftw3-dev libmp3lame-dev libasound2-dev)
    for pkg in "${BUILD_DEPS[@]}"; do
        if ! dpkg -s "$pkg" > /dev/null 2>&1; then
            apt-get install -y -qq "$pkg" >> "$LOG_FILE" 2>&1 || true
        fi
    done

    local BUILD_DIR="/tmp/welle-build-$$"
    rm -rf "$BUILD_DIR"

    detail "Cloning welle.io ${WELLE_TAG}..."
    if ! git clone --branch "$WELLE_TAG" --depth 1 \
        https://github.com/AlbrechtL/welle.io.git "$BUILD_DIR" >> "$LOG_FILE" 2>&1; then
        warn "git clone failed (check internet connectivity)"
        return 1
    fi

    mkdir -p "$BUILD_DIR/build"
    cd "$BUILD_DIR/build"

    detail "Running cmake..."
    if ! cmake .. \
        -DBUILD_WELLE_IO=OFF \
        -DBUILD_WELLE_CLI=ON \
        -DRTLSDR=1 \
        >> "$LOG_FILE" 2>&1; then
        warn "cmake failed (check build dependencies)"
        cd "$SCRIPT_DIR"
        rm -rf "$BUILD_DIR"
        return 1
    fi

    detail "Compiling (this may take 1-2 minutes)..."
    if ! make -j"$(nproc)" >> "$LOG_FILE" 2>&1; then
        warn "make failed (check $LOG_FILE for details)"
        cd "$SCRIPT_DIR"
        rm -rf "$BUILD_DIR"
        return 1
    fi

    make install >> "$LOG_FILE" 2>&1
    cd "$SCRIPT_DIR"
    rm -rf "$BUILD_DIR"

    if [ -x /usr/local/bin/welle-cli ]; then
        WELLE_CLI="/usr/local/bin/welle-cli"
        WELLE_VERSION="2.7"
        info "welle-cli ${WELLE_TAG} compiled and installed to /usr/local/bin/welle-cli"
        return 0
    fi

    warn "Compilation completed but binary not found"
    return 1
}

install_welle_from_apt() {
    info "Falling back to apt package (welle-cli v2.4)..."
    if apt-get install -y -qq welle.io >> "$LOG_FILE" 2>&1; then
        if [ -x /usr/bin/welle-cli ]; then
            WELLE_CLI="/usr/bin/welle-cli"
            WELLE_VERSION="2.4"
            info "welle-cli v2.4 installed from apt"
            warn "v2.4 is 3 years old — POST /channel may hang. App will use process restart for retuning."
            WARNINGS+=("Using welle-cli v2.4 (apt fallback). Retuning uses process restart.")
            return 0
        fi
    fi
    error "apt installation failed"
    return 1
}

# Check if already installed and adequate
if [ -x /usr/local/bin/welle-cli ]; then
    existing_ver=$(/usr/local/bin/welle-cli -v 2>&1 | grep -oP '\d+\.\d+' | head -1 2>/dev/null || echo "")
    if [ -n "$existing_ver" ]; then
        info "welle-cli v${existing_ver} already installed at /usr/local/bin/welle-cli"
        WELLE_CLI="/usr/local/bin/welle-cli"
        WELLE_VERSION="$existing_ver"
    fi
fi

if [ -z "$WELLE_CLI" ]; then
    install_welle_from_source || install_welle_from_apt || {
        error "Could not install welle-cli via any method."
        echo ""
        echo "  Troubleshooting:"
        echo "    1. Check internet connectivity: curl -I https://github.com"
        echo "    2. Check build log: less $LOG_FILE"
        echo "    3. Try manual apt install: sudo apt install welle.io"
        echo "    4. Try manual source build: see docs/architecture-decisions.md"
        echo ""
        INSTALL_OK=false
    }
fi

if [ -n "$WELLE_CLI" ]; then
    # Verify the binary actually works
    if "$WELLE_CLI" -h > /dev/null 2>&1; then
        info "welle-cli binary verified: $WELLE_CLI (v${WELLE_VERSION})"
    else
        error "welle-cli binary exists but fails to run"
        INSTALL_OK=false
    fi
fi

# ─────────────────────────────────────────────────────────────────────
step "Setting up Python environment"
# ─────────────────────────────────────────────────────────────────────

PYTHON_BIN=$(command -v python3 || true)
if [ -z "$PYTHON_BIN" ]; then
    error "python3 not found"
    INSTALL_OK=false
else
    PY_VER=$($PYTHON_BIN --version 2>&1)
    info "Found $PY_VER"
fi

if [ ! -d "$VENV_DIR" ]; then
    detail "Creating virtualenv..."
    python3 -m venv "$VENV_DIR" >> "$LOG_FILE" 2>&1
    info "Virtualenv created at $VENV_DIR"
else
    info "Virtualenv already exists at $VENV_DIR"
fi

detail "Upgrading pip..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip >> "$LOG_FILE" 2>&1

detail "Installing Python dependencies..."
if "$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt" >> "$LOG_FILE" 2>&1; then
    info "Python dependencies installed"
else
    error "Failed to install Python dependencies"
    echo "  Check: $LOG_FILE"
    INSTALL_OK=false
fi

# Verify imports
if PYTHONPATH="$SCRIPT_DIR/src" "$VENV_DIR/bin/python" -c "from dabio.app import app; print('OK')" > /dev/null 2>&1; then
    info "Python application imports verified"
else
    error "Python application import failed"
    echo "  Run manually to see error:"
    echo "    PYTHONPATH=$SCRIPT_DIR/src $VENV_DIR/bin/python -c 'from dabio.app import app'"
    INSTALL_OK=false
fi

# ─────────────────────────────────────────────────────────────────────
step "Detecting SDR hardware"
# ─────────────────────────────────────────────────────────────────────

SDR_FOUND=false

# Check for RTL-SDR USB device
if lsusb 2>/dev/null | grep -qi "RTL2838\|RTL2832\|0bda:2838\|0bda:2832"; then
    info "RTL-SDR USB device detected"
    SDR_FOUND=true

    # Quick test with rtl_test
    if command -v rtl_test > /dev/null 2>&1; then
        if timeout 3 rtl_test -t > /dev/null 2>&1; then
            info "RTL-SDR device accessible (rtl_test passed)"
        else
            warn "RTL-SDR device found via USB but rtl_test failed"
            detail "The DVB-T kernel driver may still be loaded."
            detail "A reboot is usually required after blacklisting DVB-T drivers."
            detail "Try: sudo reboot"
            WARNINGS+=("RTL-SDR detected on USB but rtl_test failed — reboot likely needed")
        fi
    fi
else
    warn "No RTL-SDR USB device detected"
    detail "The application will start but cannot receive radio without hardware."
    detail "Plug in an RTL-SDR dongle and run: sudo systemctl restart dabio"
    detail ""
    detail "For development without hardware, edit config.yaml and set mock_mode: true"
    WARNINGS+=("No SDR hardware detected — plug in RTL-SDR dongle before using")
fi

# Never auto-modify mock_mode — that is a development-only setting.
# Ensure config.yaml has mock_mode: false for production installs.
if [ -f "$CONFIG_FILE" ]; then
    sed -i 's/^mock_mode: true/mock_mode: false/' "$CONFIG_FILE"
fi

# ─────────────────────────────────────────────────────────────────────
step "Configuring systemd service"
# ─────────────────────────────────────────────────────────────────────

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

SYSTEMD_STATE=$(systemctl is-system-running 2>/dev/null || true)
if [ "$SYSTEMD_STATE" = "running" ] || [ "$SYSTEMD_STATE" = "degraded" ]; then
    HAS_SYSTEMD=true
    systemctl daemon-reload 2>> "$LOG_FILE"
    systemctl enable dabio.service >> "$LOG_FILE" 2>&1
    info "Systemd service created and enabled"
else
    HAS_SYSTEMD=false
    warn "systemd not active (state: ${SYSTEMD_STATE:-unknown}) — service file written but not enabled"
    detail "Start manually: cd $SCRIPT_DIR && PYTHONPATH=src $VENV_DIR/bin/python -m dabio"
    WARNINGS+=("systemd not active — manual startup required")
fi

# ─────────────────────────────────────────────────────────────────────
step "Starting Dabio web server"
# ─────────────────────────────────────────────────────────────────────

# Read port from config
DABIO_PORT=$(grep -oP '^\s*port:\s*\K\d+' "$CONFIG_FILE" 2>/dev/null | head -1 || echo "8800")
DABIO_HOST=$(grep -oP '^\s*host:\s*"\K[^"]+' "$CONFIG_FILE" 2>/dev/null | head -1 || echo "0.0.0.0")

if $HAS_SYSTEMD; then
    # Stop if already running
    systemctl stop dabio.service 2>/dev/null || true
    sleep 1

    # Start the service
    detail "Starting dabio.service..."
    if systemctl start dabio.service 2>> "$LOG_FILE"; then
        info "dabio.service started"
    else
        error "dabio.service failed to start"
        detail "Check logs: sudo journalctl -u dabio -n 30 --no-pager"
        INSTALL_OK=false
    fi
else
    # No systemd — start directly in background
    detail "No systemd — starting Dabio directly..."
    # Kill any existing instance
    EXISTING=$(pgrep -f "python.*dabio" 2>/dev/null | grep -v "^$$\$" || true)
    if [ -n "$EXISTING" ]; then
        echo "$EXISTING" | xargs kill 2>/dev/null || true
        sleep 1
    fi
    cd "$SCRIPT_DIR"
    PYTHONPATH="$SCRIPT_DIR/src" nohup "$VENV_DIR/bin/python" -m dabio >> "$LOG_FILE" 2>&1 &
    DABIO_PID=$!
    info "Dabio started in background (PID: $DABIO_PID)"
fi

# Give it time to boot
detail "Waiting for server to initialize..."
sleep 4

# ─────────────────────────────────────────────────────────────────────
step "Verifying web server is responding"
# ─────────────────────────────────────────────────────────────────────

SERVER_OK=false
VERIFY_URL="http://127.0.0.1:${DABIO_PORT}"

# Check if process is running
PROCESS_ALIVE=false
if $HAS_SYSTEMD; then
    if systemctl is-active --quiet dabio.service 2>/dev/null; then
        PROCESS_ALIVE=true
        info "dabio.service is active"
    else
        error "dabio.service is not running"
        detail ""
        detail "Service logs:"
        journalctl -u dabio -n 15 --no-pager 2>/dev/null | while IFS= read -r line; do
            detail "  $line"
        done
        detail ""
        detail "Troubleshooting:"
        detail "  1. Check service status:  sudo systemctl status dabio"
        detail "  2. View service logs:     sudo journalctl -u dabio -n 50 --no-pager"
        detail "  3. Try running manually:"
        detail "     cd $SCRIPT_DIR && PYTHONPATH=src .venv/bin/python -m dabio"
        INSTALL_OK=false
    fi
else
    # Check background process
    if kill -0 "${DABIO_PID:-0}" 2>/dev/null; then
        PROCESS_ALIVE=true
        info "Dabio process is running (PID: $DABIO_PID)"
    else
        error "Dabio process failed to start"
        detail "Check the log: tail -30 $LOG_FILE"
        INSTALL_OK=false
    fi
fi

if $PROCESS_ALIVE; then
    # Poll health endpoint with retries
    MAX_RETRIES=8
    RETRY_DELAY=2
    for i in $(seq 1 $MAX_RETRIES); do
        if curl -sf "${VERIFY_URL}/api/health" > /tmp/dabio_health_$$.json 2>/dev/null; then
            SERVER_OK=true
            break
        fi
        if [ "$i" -lt "$MAX_RETRIES" ]; then
            detail "Attempt $i/$MAX_RETRIES — waiting ${RETRY_DELAY}s..."
            sleep "$RETRY_DELAY"
        fi
    done

    if $SERVER_OK; then
        info "Health endpoint responding"

        # Parse health response
        HEALTH_STATUS=$(python3 -c "import json; d=json.load(open('/tmp/dabio_health_$$.json')); print(d.get('status','unknown'))" 2>/dev/null || echo "unknown")
        STATION_COUNT=$(python3 -c "import json; d=json.load(open('/tmp/dabio_health_$$.json')); print(d.get('stations_count',0))" 2>/dev/null || echo "0")
        IS_MOCK=$(python3 -c "import json; d=json.load(open('/tmp/dabio_health_$$.json')); print(d.get('mock_mode',False))" 2>/dev/null || echo "Unknown")

        info "Server status: $HEALTH_STATUS"
        info "Stations loaded: $STATION_COUNT"
        if [ "$IS_MOCK" = "True" ]; then
            info "Mode: Mock (no SDR hardware)"
        else
            info "Mode: Live SDR"
        fi

        # Test that the frontend loads
        if curl -sf "${VERIFY_URL}/" > /dev/null 2>&1; then
            info "Frontend (index.html) loads OK"
        else
            warn "Frontend failed to load"
        fi

        # Test stations endpoint
        if curl -sf "${VERIFY_URL}/api/stations" > /dev/null 2>&1; then
            info "Stations API responding"
        else
            warn "Stations API not responding"
        fi

        rm -f "/tmp/dabio_health_$$.json"
    else
        error "Health endpoint not responding after ${MAX_RETRIES} attempts"
        INSTALL_OK=false

        detail ""
        detail "Troubleshooting:"
        detail "  1. Check if the process is running:  ps aux | grep dabio"
        detail "  2. Check the install log:  tail -30 $LOG_FILE"
        if $HAS_SYSTEMD; then
            detail "  3. View service logs:  sudo journalctl -u dabio -n 50 --no-pager"
        fi
        detail "  4. Try running manually:"
        detail "     cd $SCRIPT_DIR && PYTHONPATH=src .venv/bin/python -m dabio"
    fi
fi

# ─────────────────────────────────────────────────────────────────────
#  RESULTS
# ─────────────────────────────────────────────────────────────────────

# Determine access URLs
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "")
ACCESS_URL="http://127.0.0.1:${DABIO_PORT}"
if [ -n "$LOCAL_IP" ] && [ "$DABIO_HOST" = "0.0.0.0" ]; then
    LAN_URL="http://${LOCAL_IP}:${DABIO_PORT}"
else
    LAN_URL=""
fi

echo ""
echo ""
if $SERVER_OK; then
    # ── SUCCESS ──────────────────────────────────────────────────────
    echo -e "${GREEN}${BOLD}══════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}${BOLD}  ✓ INSTALLATION SUCCESSFUL${NC}"
    echo -e "${GREEN}${BOLD}══════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  ${BOLD}Dabio is running and ready.${NC}"
    echo ""
    echo -e "  ${BOLD}Open in your browser:${NC}"
    echo ""
    echo -e "    Local:   ${BOLD}${ACCESS_URL}${NC}"
    if [ -n "$LAN_URL" ]; then
        echo -e "    Network: ${BOLD}${LAN_URL}${NC}"
    fi
    echo ""
    echo -e "  ${BOLD}Components:${NC}"
    echo "    welle-cli:   $WELLE_CLI (v${WELLE_VERSION})"
    echo "    Python:      $VENV_DIR"
    echo "    Config:      $CONFIG_FILE"
    echo "    Service:     dabio.service (enabled, active)"
    echo "    Port:        $DABIO_PORT"
    if ! $SDR_FOUND; then
        echo ""
        echo -e "  ${YELLOW}No SDR hardware detected.${NC}"
        echo "  Plug in an RTL-SDR USB dongle and restart:"
        echo "    sudo systemctl restart dabio"
    fi
    if [ ${#WARNINGS[@]} -gt 0 ]; then
        echo ""
        echo -e "  ${YELLOW}Warnings:${NC}"
        for w in "${WARNINGS[@]}"; do
            echo -e "    ${YELLOW}⚠${NC} $w"
        done
    fi
    echo ""
    echo "  Useful commands:"
    echo "    sudo systemctl status dabio    # Check status"
    echo "    sudo systemctl restart dabio   # Restart"
    echo "    sudo journalctl -u dabio -f    # View live logs"
    echo "    curl ${ACCESS_URL}/api/health  # Health check"
    echo ""
    echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"

    exit 0
else
    # ── FAILURE ──────────────────────────────────────────────────────
    echo -e "${RED}${BOLD}══════════════════════════════════════════════════════${NC}"
    echo -e "${RED}${BOLD}  ✗ INSTALLATION COMPLETED WITH ERRORS${NC}"
    echo -e "${RED}${BOLD}══════════════════════════════════════════════════════${NC}"
    echo ""

    if [ -z "$WELLE_CLI" ]; then
        echo -e "  ${RED}Problem: welle-cli is not installed${NC}"
        echo ""
        echo "  Troubleshooting:"
        echo "    1. Check internet: curl -I https://github.com"
        echo "    2. Try manual install: sudo apt install welle.io"
        echo "    3. Check build log: less $LOG_FILE"
        echo ""
    fi

    if ! $SERVER_OK; then
        echo -e "  ${RED}Problem: Web server is not responding on port ${DABIO_PORT}${NC}"
        echo ""
        echo "  Troubleshooting:"
        echo "    1. Check service status:  sudo systemctl status dabio"
        echo "    2. View service logs:     sudo journalctl -u dabio -n 50 --no-pager"
        echo "    3. Check port is free:    ss -tlnp | grep ${DABIO_PORT}"
        echo "    4. Try running manually:"
        echo "       cd $SCRIPT_DIR"
        echo "       PYTHONPATH=src .venv/bin/python -m dabio"
        echo ""
        echo "    5. If no SDR hardware, enable mock mode:"
        echo "       Edit $CONFIG_FILE → mock_mode: true"
        echo "       sudo systemctl restart dabio"
        echo ""
    fi

    if [ ${#WARNINGS[@]} -gt 0 ]; then
        echo -e "  ${YELLOW}Warnings encountered:${NC}"
        for w in "${WARNINGS[@]}"; do
            echo -e "    ${YELLOW}⚠${NC} $w"
        done
        echo ""
    fi

    echo "  Full install log: $LOG_FILE"
    echo ""
    echo -e "${RED}══════════════════════════════════════════════════════${NC}"

    exit 1
fi
