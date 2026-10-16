#!/usr/bin/env bash
# ==============================================================================
# Desk Agent R1 - TaishanPi (RK3566) Edge Deployment Installer
# ==============================================================================
set -euo pipefail

INSTALL_DIR="/opt/desk-agent"
DATA_DIR="/var/lib/desk-agent"
CONFIG_DIR="/etc/desk-agent"
SERVICE_SRC="$(dirname "$0")/desk-agent.service"
CONFIG_SRC="$(dirname "$0")/config.example.json"

echo "=== [1/5] Creating dedicated system user 'deskagent' ==="
if ! id "deskagent" &>/dev/null; then
    useradd --system --user-group --shell /usr/sbin/nologin deskagent
    echo "Created system user: deskagent"
else
    echo "System user 'deskagent' already exists"
fi

echo "=== [2/5] Setting up data and config directories ==="
mkdir -p "${DATA_DIR}"
mkdir -p "${CONFIG_DIR}"
chown -R deskagent:deskagent "${DATA_DIR}"
chmod 750 "${DATA_DIR}"

if [ ! -f "${CONFIG_DIR}/config.json" ]; then
    cp "${CONFIG_SRC}" "${CONFIG_DIR}/config.json"
    chown deskagent:deskagent "${CONFIG_DIR}/config.json"
    chmod 640 "${CONFIG_DIR}/config.json"
    echo "Installed default config at ${CONFIG_DIR}/config.json"
else
    echo "Preserved existing config at ${CONFIG_DIR}/config.json"
fi

echo "=== [3/5] Verifying Python virtual environment ==="
if [ -d "${INSTALL_DIR}/venv" ]; then
    echo "Virtual environment detected at ${INSTALL_DIR}/venv"
else
    echo "Warning: Virtual environment at ${INSTALL_DIR}/venv not found."
    echo "Please set up the venv before starting the systemd service:"
    echo "  python3 -m venv ${INSTALL_DIR}/venv"
    echo "  ${INSTALL_DIR}/venv/bin/pip install -r requirements.txt"
fi

echo "=== [4/5] Installing systemd service unit ==="
if [ -f "${SERVICE_SRC}" ]; then
    cp "${SERVICE_SRC}" /etc/systemd/system/desk-agent.service
    systemctl daemon-reload
    echo "Systemd service installed to /etc/systemd/system/desk-agent.service"
else
    echo "Error: desk-agent.service template not found at ${SERVICE_SRC}"
    exit 1
fi

echo "=== [5/5] Deployment Complete ==="
echo "To enable and start the service:"
echo "  sudo systemctl enable --now desk-agent.service"
echo "To check status and logs:"
echo "  sudo systemctl status desk-agent.service"
echo "  sudo journalctl -u desk-agent.service -f"
