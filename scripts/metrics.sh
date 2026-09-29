#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Summary of vLLM metrics (KV cache, running/waiting requests, preemptions)
# plus host memory and GPU state.

set -Eeuo pipefail
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: scripts/metrics.sh [options]

Options:
      --url URL          vLLM metrics URL (default: http://127.0.0.1:$CODENDUM_PORT/metrics)
      --watch SECONDS    print one compact line every SECONDS until Ctrl-C
      --json             machine-readable output
  -e, --env-file FILE    configuration file (default: .env in the repository root)
  -h, --help             show this help

/metrics is only reachable on the host itself; it is not exposed by the proxy.
EOF
}

env_file="" args=()
while (($#)); do
    case "$1" in
        -e | --env-file) env_file="${2:?--env-file needs a value}"; shift 2 ;;
        --url | --watch) args+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
        --json) args+=("$1"); shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" 64 ;;
    esac
done
load_config "$env_file"

run_py metrics --url "http://127.0.0.1:${CODENDUM_PORT:-8000}/metrics" "${args[@]}"
