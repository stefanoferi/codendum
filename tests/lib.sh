# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Minimal helpers for the offline test scripts. Source this file.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS_FAILED=0
TESTS_PASSED=0
CLEANUP_PIDS=()
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codendum-test.XXXXXX")"

cleanup() {
    local pid
    for pid in "${CLEANUP_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    if [[ -n "${CLEANUP_HOOK:-}" ]]; then "$CLEANUP_HOOK" || true; fi
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

ok() { printf 'ok   - %s\n' "$1"; TESTS_PASSED=$((TESTS_PASSED + 1)); }
not_ok() {
    printf 'FAIL - %s\n' "$1"
    [[ -n "${2:-}" ]] && printf '%s\n' "$2" | sed 's/^/       /'
    TESTS_FAILED=$((TESTS_FAILED + 1))
}

# check DESCRIPTION COMMAND...: passes when COMMAND succeeds.
check() {
    local desc="$1" out
    shift
    if out="$("$@" 2>&1)"; then ok "$desc"; else not_ok "$desc" "$out"; fi
}

# expect_exit CODE DESCRIPTION COMMAND...: passes when COMMAND exits with CODE.
# The combined output is kept in $LAST_OUTPUT.
LAST_OUTPUT=""
expect_exit() {
    local want="$1" desc="$2" rc=0
    shift 2
    LAST_OUTPUT="$("$@" 2>&1)" || rc=$?
    if [[ "$rc" == "$want" ]]; then ok "$desc"; else not_ok "$desc (exit ${rc}, expected ${want})" "$LAST_OUTPUT"; fi
}

# expect_output_contains DESCRIPTION NEEDLE: checks $LAST_OUTPUT.
expect_output_contains() {
    if [[ "$LAST_OUTPUT" == *"$2"* ]]; then ok "$1"; else not_ok "$1 (missing: $2)" "$LAST_OUTPUT"; fi
}

expect_output_lacks() {
    if [[ "$LAST_OUTPUT" != *"$2"* ]]; then ok "$1"; else not_ok "$1 (found: $2)" "$LAST_OUTPUT"; fi
}

# start_mock [ARGS...]: starts tests/mock_vllm.py on a free port, sets MOCK_PORT.
start_mock() {
    local port_file
    port_file="$(mktemp "${TMP_ROOT}/port.XXXXXX")"
    rm -f "$port_file"
    python3 "${ROOT}/tests/mock_vllm.py" --port-file "$port_file" "$@" &
    MOCK_PID=$!
    CLEANUP_PIDS+=("$MOCK_PID")
    for _ in $(seq 1 100); do
        [[ -s "$port_file" ]] && break
        sleep 0.05
    done
    [[ -s "$port_file" ]] || { echo "mock server did not start" >&2; return 1; }
    # shellcheck disable=SC2034  # used by the sourcing test scripts
    MOCK_PORT="$(cat "$port_file")"
}

stop_mock() {
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
}

free_port() {
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

finish() {
    printf '\n%d passed, %d failed\n' "$TESTS_PASSED" "$TESTS_FAILED"
    ((TESTS_FAILED == 0))
}
