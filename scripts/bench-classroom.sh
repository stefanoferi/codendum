#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Simulated class: N users running OpenCode-like agent sessions (tool calls,
# growing context, pauses) against the real model. Heavy, long-running load:
# run it in a maintenance window, never during a class or working session.

set -Eeuo pipefail
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: scripts/bench-classroom.sh [options]

Each simulated user works on an in-memory Java client-server project: the
model reads files, writes and edits code and runs (simulated) builds and
tests through OpenCode-style tools, over several follow-up requests. Users
start within the ramp-up period and pause between requests.

Run it from a workstation, through the proxy, to include the network path:
  scripts/bench-classroom.sh --base-url https://llm.lab.example:8443 \
      --api-keys-file keys/api-keys.csv --cacert ca.pem

Options (all others are passed to the simulator; see --help-simulator):
      --base-url URL       server root (default: http://127.0.0.1:$CODENDUM_PORT)
      --api-keys-file CSV  api-keys.csv from gen-api-keys.sh: one key per user
      --users N            simulated users (default: 40)
      --duration S         seconds during which users start requests (default: 900)
      --help-simulator     list every simulator option
  -e, --env-file FILE      configuration file (default: .env in the repository root)
  -h, --help               show this help

The results (per-request and per-turn JSON Lines, summary.json) go to
bench-results/classroom-<UTC timestamp>/. Server-side metrics are sampled
only when --metrics-url is reachable, that is when running on the host.
EOF
}

env_file="" args=()
while (($#)); do
    case "$1" in
        -e | --env-file) env_file="${2:?--env-file needs a value}"; shift 2 ;;
        --help-simulator) run_py classroom --help; exit 0 ;;
        -h | --help) usage; exit 0 ;;
        *) args+=("$1"); shift ;;
    esac
done
load_config "$env_file"

port="${CODENDUM_PORT:-8000}"
defaults=(--base-url "http://127.0.0.1:${port}" --model "${CODENDUM_SERVED_MODEL_NAME:-coder}"
    --profile "${CODENDUM_PROFILE:-unknown}")
if [[ " ${args[*]} " != *" --base-url "* ]]; then
    defaults+=(--metrics-url "http://127.0.0.1:${port}/metrics")
fi
run_py classroom "${defaults[@]}" "${args[@]}"
