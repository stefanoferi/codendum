#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Repeatable load test at several concurrency levels with short and long
# prompts. Generates heavy load: run it in a maintenance window, never
# during a class or working session.

set -Eeuo pipefail
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: scripts/bench.sh [options]

Runs closed-loop load at each concurrency level (default 1, 8, 16, 24, 40)
for short and long prompts and reports input/output sizes, throughput,
TTFT median/P95/P99, end-to-end latency, errors, preemptions and peak KV
cache usage. Results go to bench-results/<UTC timestamp>/ (ignored by Git).

Options:
      --concurrency LIST   comma-separated levels (default: 1,8,16,24,40)
      --shapes LIST        short,long or a subset (default: short,long)
      --short-input N      approx. input tokens of short prompts (default: 512)
      --long-input N       approx. input tokens of long prompts (default: 16384)
      --output-tokens N    output tokens per request (default: 256)
      --rounds N           requests per worker at each level (default: 2)
      --min-requests N     minimum requests per level (default: 8)
      --base-url URL       server root (default: http://127.0.0.1:$CODENDUM_PORT)
      --metrics-url URL    vLLM metrics URL or 'none' (default: local /metrics)
      --model NAME         served model name (default: $CODENDUM_SERVED_MODEL_NAME)
      --cacert FILE        CA bundle for a private certificate
      --out-dir DIR        output directory
      --timeout S          per-request idle timeout in seconds (default: 900)
      --no-warmup          skip the initial warm-up request
      --allow-busy         run even if requests are in flight (not recommended)
      --yes                do not ask for confirmation
  -e, --env-file FILE      configuration file (default: .env in the repository root)
  -h, --help               show this help

The API key, when needed, is read from CODENDUM_API_KEY. The benchmark
refuses to start while the server is serving requests unless --allow-busy.
EOF
}

env_file="" args=()
while (($#)); do
    case "$1" in
        -e | --env-file) env_file="${2:?--env-file needs a value}"; shift 2 ;;
        --concurrency | --shapes | --short-input | --long-input | --output-tokens | --rounds | \
            --min-requests | --base-url | --metrics-url | --model | --cacert | --out-dir | --timeout)
            args+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
        --allow-busy | --yes | --no-warmup) args+=("$1"); shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" 64 ;;
    esac
done
load_config "$env_file"

port="${CODENDUM_PORT:-8000}"
run_py bench \
    --base-url "http://127.0.0.1:${port}" \
    --metrics-url "http://127.0.0.1:${port}/metrics" \
    --model "${CODENDUM_SERVED_MODEL_NAME:-coder}" \
    --profile "${CODENDUM_PROFILE:-unknown}" \
    "${args[@]}"
