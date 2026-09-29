#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Functional checks of the model endpoint: health, model list, chat,
# streaming and structured tool calls.

set -Eeuo pipefail
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: scripts/smoke-test.sh [options]

Checks /health, /v1/models, a chat completion, a streamed completion, a tool
call with tool_choice "auto" (the path OpenCode uses), a tool call with
tool_choice "required", and a tool-result round trip.

Options:
      --base-url URL   server root without /v1 (default: http://127.0.0.1:$CODENDUM_PORT)
      --proxy          the URL is the public HTTPS proxy: also verify that private
                       endpoints (/health, /metrics, ...) are NOT reachable and that
                       requests without an API key are rejected
      --model NAME     served model name (default: $CODENDUM_SERVED_MODEL_NAME or coder)
      --cacert FILE    CA bundle for a private certificate
      --skip-tools     skip the tool-calling checks
  -e, --env-file FILE  configuration file (default: .env in the repository root)
  -h, --help           show this help

The API key, when needed, is read from the CODENDUM_API_KEY environment
variable; it is never accepted on the command line.

Exit status: 0 all checks passed, 1 a check failed, 2 inconclusive (for
example the model answered without calling the tool with tool_choice "auto").
EOF
}

env_file="" args=()
while (($#)); do
    case "$1" in
        -e | --env-file) env_file="${2:?--env-file needs a value}"; shift 2 ;;
        --base-url | --model | --cacert) args+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
        --proxy | --skip-tools) args+=("$1"); shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" 64 ;;
    esac
done
load_config "$env_file"

run_py smoke \
    --base-url "http://127.0.0.1:${CODENDUM_PORT:-8000}" \
    --model "${CODENDUM_SERVED_MODEL_NAME:-coder}" \
    "${args[@]}"
