#!/usr/bin/env bash
# ==============================================================================
# Desk Agent R1 - TaishanPi (RK3566) Edge Deployment Installer
# ==============================================================================
set -euo pipefail

INSTALL_DIR="/opt/desk-agent"
DATA_DIR="/var/lib/desk-agent"
CONFIG_DIR="/etc/desk-agent"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SERVICE_SRC="${SCRIPT_DIR}/desk-agent.service"
CONFIG_SRC="${SCRIPT_DIR}/config.example.json"

echo "=== [1/6] Creating dedicated system user 'deskagent' ==="
if ! id "deskagent" &>/dev/null; then
    useradd --system --user-group --shell /usr/sbin/nologin deskagent
    echo "Created system user: deskagent"
else
    echo "System user 'deskagent' already exists"
fi

echo "=== [2/6] Setting up data and config directories ==="
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

echo "=== [3/6] Installing application code to ${INSTALL_DIR} ==="
mkdir -p "${INSTALL_DIR}"
cp -r "${PROJECT_ROOT}/host" "${INSTALL_DIR}/"
if [ -d "${PROJECT_ROOT}/companion" ]; then
    cp -r "${PROJECT_ROOT}/companion" "${INSTALL_DIR}/"
fi
if [ -d "${PROJECT_ROOT}/contracts" ]; then
    cp -r "${PROJECT_ROOT}/contracts" "${INSTALL_DIR}/"
fi
cp "${PROJECT_ROOT}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
echo "Application code copied to ${INSTALL_DIR}"

echo "=== [4/6] Setting up Python virtual environment and dependencies ==="
if [ ! -d "${INSTALL_DIR}/venv" ]; then
    echo "Creating Python virtual environment at ${INSTALL_DIR}/venv..."
    python3 -m venv "${INSTALL_DIR}/venv"
else
    echo "Reusing existing virtual environment at ${INSTALL_DIR}/venv"
fi

echo "Installing requirements into ${INSTALL_DIR}/venv..."
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "Setting permissions on ${INSTALL_DIR}..."
chown -R deskagent:deskagent "${INSTALL_DIR}"

echo "=== [5/6] Installing systemd service unit ==="
if [ -f "${SERVICE_SRC}" ]; then
    cp "${SERVICE_SRC}" /etc/systemd/system/desk-agent.service
    if command -v systemctl &>/dev/null && [ -d /run/systemd/system ]; then
        systemctl daemon-reload
    fi
    echo "Systemd service installed to /etc/systemd/system/desk-agent.service"
else
    echo "Error: desk-agent.service template not found at ${SERVICE_SRC}"
    exit 1
fi

echo "=== [6/6] Deployment Complete ==="
echo "To enable and start the service:"
echo "  sudo systemctl enable --now desk-agent.service"
echo "To check status and logs:"
echo "  sudo systemctl status desk-agent.service"
echo "  sudo journalctl -u desk-agent.service -f"

