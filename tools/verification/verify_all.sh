#!/usr/bin/env bash
# ==============================================================================
# Desk Agent R1 - Master System Verification & Acceptance Suite
# Enforces AGENTS.md Section 4 (Evidence Layering: Level 1 to Level 4)
#
# Runs all 8 verification phases. Phases 1-5, 7, 8 are automated gates; phase 6
# verifies the recorded evidence matrix only. This script does not exercise
# hardware or connected services and cannot promote any L3/L4 result.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

echo "========================================================================"
echo "  Desk Agent R1 Master System Verification & Evidence Generator         "
echo "========================================================================"
echo "Working directory: ${REPO_ROOT}"
echo "Timestamp: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo ""

TOTAL_STEPS=8
CURRENT_STEP=0

step_pass() {
  CURRENT_STEP=$((CURRENT_STEP + 1))
  echo -e "\033[0;32m[PASS]\033[0m (${CURRENT_STEP}/${TOTAL_STEPS}) $1"
}

step_fail() {
  echo -e "\033[0;31m[FAIL]\033[0m $1"
  exit 1
}

# ------------------------------------------------------------------------------
# 1. Level 1: Deep Contract & JSON Schema Verification
# ------------------------------------------------------------------------------
echo "--- Step 1: Deep Contract & Schema Validation (contracts/validator) ---"
python3 contracts/validator/validate.py || step_fail "Contract validation failed"
step_pass "All JSON Schemas, envelopes, positive/negative fixtures, and error catalogs verified"

# ------------------------------------------------------------------------------
# 2. Level 2: Firmware Native HAL & Protocol Library Compilation
# ------------------------------------------------------------------------------
echo "--- Step 2: Firmware Native HAL & SDK Bridge Compilation (firmware/) ---"
if ! CMAKE_BIN="$(command -v cmake)"; then
  step_fail "CMake is required for the standalone host firmware check; install CMake and retry"
fi

# This is a standalone host build. Keep the ESP-IDF environment out of CMake's
# configuration and never reuse firmware/build, which contains IDF artifacts.
unset IDF_PATH
HOST_BUILD_DIR="$(mktemp -d "/tmp/desk-agent-host-cmake.XXXXXX")" \
  || step_fail "Could not create a unique temporary host CMake build directory"
trap 'rm -rf -- "${HOST_BUILD_DIR}"' EXIT

if ! "${CMAKE_BIN}" -S firmware -B "${HOST_BUILD_DIR}"; then
  step_fail "Firmware native CMake configure failed"
fi
if ! "${CMAKE_BIN}" --build "${HOST_BUILD_DIR}"; then
  step_fail "Firmware native build failed"
fi
step_pass "Firmware libdesk_hardware.a and libdesk_sdk_bridge.a compiled clean"

# ------------------------------------------------------------------------------
# 3. Level 2: Complete Python Test Suite (All domains, contract & e2e tests)
# ------------------------------------------------------------------------------
echo ""
echo "--- Step 3: Complete Python Test Suite (All domains & integration) ---"
PYTHONPATH=. python3 -m unittest discover -s tests -t . -p "test_*.py" || step_fail "Python test suite failed"
step_pass "All unit, contract, E2E, fault injection, and stress tests passed 100%"

# ------------------------------------------------------------------------------
# 4. Level 2: Real Chain End-to-End (TClaw -> SDK Bridge TCP -> Host -> back)
# ------------------------------------------------------------------------------
echo ""
echo "--- Step 4: Real Chain E2E - TClaw -> SDK Bridge TCP -> Host Router -> Device ---"
PYTHONPATH=. python3 -m unittest tests.e2e.test_real_chain_tclaw_to_host \
  || step_fail "Real chain end-to-end verification failed"
step_pass "Tool request -> Host Router -> Life execution -> response returned over TCP, with no faked ACK"

# ------------------------------------------------------------------------------
# 5. Level 2: Cross-Module P1 Regression Gates (PR #11 remediation)
# ------------------------------------------------------------------------------
echo ""
echo "--- Step 5: Cross-Module P1 Regression Gates + Real-Firmware Motion-Fault Chain (interfaces.md 6.1-6.5) ---"
PYTHONPATH=. python3 -m unittest tests.e2e.test_p1_regression_gates \
  || step_fail "Cross-module P1 regression gates failed"
# Final-P1 cross-layer chain: a fault injected in the REAL compiled C driver is
# carried through the REAL C bridge protocol, the REAL RobotAdapter and the REAL
# DeskAgentHostApp, and asserted on the OUTERMOST response envelope.
PYTHONPATH=. python3 -m unittest tests.e2e.test_real_firmware_motion_fault_chain \
  || step_fail "Real-firmware motion-fault cross-layer chain failed"
step_pass "Driver failure/timeout never fake success; ACK correlation, TCP framing, pairing fail-closed, companion LAN gate, and the real-C-driver cross-layer motion-fault chain all enforced"

# ------------------------------------------------------------------------------
# 6. Level 2: Edge Deployment Environment Verification
# ------------------------------------------------------------------------------
echo ""
echo "--- Step 6: Edge Deployment Pre-Flight Checks (deploy/edge/) ---"
bash deploy/edge/verify_edge.sh || step_fail "Edge deployment verification failed"
step_pass "TaishanPi edge environment pre-flight checks passed (directories, permissions, toolchain, Python modules)"

# ------------------------------------------------------------------------------
# 7. Level 3 & Level 4: Physical Bench & Hardware Gap Audit
# ------------------------------------------------------------------------------
echo ""
echo "--- Step 7: Hardware & Acceptance Matrix Audit (docs/acceptance/) ---"
test -f docs/acceptance/acceptance-matrix.md || step_fail "acceptance-matrix.md missing"
test -f docs/acceptance/field-runbook-2026-10-21.md || step_fail "Current field runbook missing"
step_pass "Acceptance matrix and dated field runbook are present (this step does not exercise hardware)"

# ------------------------------------------------------------------------------
# 8. Level 1 & Level 4: Evidence Layering Anti-Overclaim Audit (AGENTS.md Sec.4)
# ------------------------------------------------------------------------------
echo ""
echo "--- Step 8: Evidence Layering Anti-Overclaim Audit ---"
MATRIX="docs/acceptance/acceptance-matrix.md"

# Anchor on the Level 3 / Level 4 table rows themselves so prose elsewhere
# cannot mask a stale or overstated capability-level status.
grep -E "^\| \*\*Level 3" "${MATRIX}" | grep -q "部分单项通过" \
  || step_fail "Level 3 row must preserve the partial item-level evidence status"
grep -E "^\| \*\*Level 3" "${MATRIX}" | grep -q "未验证" \
  || step_fail "Level 3 row must continue to identify unverified hardware/services"
grep -E "^\| \*\*Level 4" "${MATRIX}" | grep -q "未完成" \
  || step_fail "Level 4 row must still record the live loop as 未完成"

# The matrix must stay in sync with the real chain suite it cites.
test -f tests/e2e/test_real_chain_tclaw_to_host.py \
  || step_fail "Real chain E2E suite tests/e2e/test_real_chain_tclaw_to_host.py is missing"
grep -q "test_real_chain_tclaw_to_host" "${MATRIX}" \
  || step_fail "Acceptance matrix must cite tests/e2e/test_real_chain_tclaw_to_host.py"
grep -q "field-runbook-2026-10-21.md" "${MATRIX}" \
  || step_fail "Acceptance matrix must link the current field runbook"

# This script's own evidence summary must present the physical tiers as pending.
grep -qE "Level 3 \(Physical Node\): *RECORDED_PARTIAL" tools/verification/verify_all.sh \
  || step_fail "verify_all.sh must report recorded partial L3 evidence"
grep -qE "Level 4 \(End-to-End Live\): *PENDING_HARDWARE" tools/verification/verify_all.sh \
  || step_fail "verify_all.sh must report Level 4 as pending"
grep -q "This script does not exercise hardware" tools/verification/verify_all.sh \
  || step_fail "verify_all.sh must state that it does not exercise hardware"
step_pass "Evidence layering intact: L3 is item-specific, L4 remains pending, this run cannot promote either"

echo ""
echo "========================================================================"
echo -e "\033[0;32m  VERIFICATION SCRIPT REACHED ITS SUMMARY BLOCK \033[0m"
echo "========================================================================"
echo "Evidence Summary:"
echo "  Level 1/2:                      Checks reached above without a failing gate; use each command's output as evidence."
echo "  Level 3 (Physical Node):        RECORDED_PARTIAL (see docs/acceptance/acceptance-matrix.md; this script does not exercise hardware)"
echo "  Level 4 (End-to-End Live):      PENDING_HARDWARE (see field runbook for remaining live-service and recovery gates)"
echo ""
echo "NOTE: This script does not exercise hardware or connected services. Recorded L3 evidence"
echo "      is item-specific; this run cannot verify, revoke, or promote those records."
echo "========================================================================"
