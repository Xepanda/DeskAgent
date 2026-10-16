#!/usr/bin/env bash
# ==============================================================================
# Desk Agent R1 - Edge Environment Pre-flight Verification Script
# ==============================================================================
set -euo pipefail

PASS=0
FAIL=0

check_step() {
    local desc="$1"
    local cmd="$2"
    echo -n "Checking: ${desc}... "
    if eval "$cmd" >/dev/null 2>&1; then
        echo "[PASS]"
        PASS=$((PASS + 1))
    else
        echo "[FAIL]"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Desk Agent R1 Edge Pre-flight Verification ==="

check_step "Python 3 is installed" "command -v python3"
check_step "Python version >= 3.10" "python3 -c 'import sys; assert sys.version_info >= (3, 10)'"
check_step "SQLite3 module available" "python3 -c 'import sqlite3; conn = sqlite3.connect(\":memory:\"); conn.execute(\"PRAGMA journal_mode = WAL;\")'"
check_step "JSON module available" "python3 -c 'import json; json.dumps({})'"
check_step "Threading & Socket available" "python3 -c 'import socket, threading; s = socket.socket(); s.close()'"

if [ -f "/etc/desk-agent/config.json" ]; then
    check_step "Configuration syntax valid" "python3 -m json.tool /etc/desk-agent/config.json"
fi

if [ -d "/var/lib/desk-agent" ]; then
    check_step "Data directory writable" "test -w /var/lib/desk-agent"
fi

echo "=================================================="
echo "Verification Summary: ${PASS} passed, ${FAIL} failed"
if [ ${FAIL} -eq 0 ]; then
    echo "Result: READY FOR EDGE EXECUTION"
    exit 0
else
    echo "Result: PRE-FLIGHT CHECKS FAILED"
    exit 1
fi
