#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Start the Codendum vLLM container on the GB10 host.

set -Eeuo pipefail
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
    cat <<EOF
Usage: scripts/start-vllm.sh [options]

Start vLLM in Docker with the selected serving profile. The API listens on
${CODENDUM_HOST:-127.0.0.1}:${CODENDUM_PORT:-8000} only; publish it through the HTTPS proxy.

Options:
  -p, --profile NAME   serving profile (default: CODENDUM_PROFILE or ${CODENDUM_DEFAULT_PROFILE})
  -e, --env-file FILE  configuration file (default: .env in the repository root)
      --dry-run        validate the configuration and print the command only
      --replace        stop and remove an existing container with the same name
      --wait           wait until /health answers (up to CODENDUM_START_TIMEOUT s, default 3600)
      --list-profiles  print the available profiles and exit
  -h, --help           show this help

Profiles: $(list_profiles | paste -sd ' ' -)
EOF
}

profile_arg="" env_file="" dry_run=0 replace=0 wait_health=0
while (($#)); do
    case "$1" in
        -p | --profile) profile_arg="${2:?--profile needs a value}"; shift 2 ;;
        -e | --env-file) env_file="${2:?--env-file needs a value}"; shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        --replace) replace=1; shift ;;
        --wait) wait_health=1; shift ;;
        --list-profiles) list_profiles; exit 0 ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" 64 ;;
    esac
done

[[ -n "$profile_arg" ]] && export CODENDUM_PROFILE="$profile_arg"
load_config "$env_file"
load_profile

image="${CODENDUM_VLLM_IMAGE:-}"
model="${CODENDUM_MODEL:-Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8}"
revision="${CODENDUM_MODEL_REVISION:-}"
served_name="${CODENDUM_SERVED_MODEL_NAME:-coder}"
container="${CODENDUM_CONTAINER_NAME:-codendum-vllm}"
host="${CODENDUM_HOST:-127.0.0.1}"
port="${CODENDUM_PORT:-8000}"
hf_cache="${CODENDUM_HF_CACHE_DIR:-}"
secrets_file="${CODENDUM_SECRETS_ENV_FILE:-}"
restart_policy="${CODENDUM_RESTART_POLICY:-unless-stopped}"
max_model_len="${CODENDUM_MAX_MODEL_LEN:-}"
max_num_seqs="${CODENDUM_MAX_NUM_SEQS:-}"
max_batched="${CODENDUM_MAX_NUM_BATCHED_TOKENS:-}"
gpu_util="${CODENDUM_GPU_MEMORY_UTILIZATION:-}"
kv_dtype="${CODENDUM_KV_CACHE_DTYPE:-fp8}"
hf_offline="${CODENDUM_HF_OFFLINE:-0}"
tool_parser="${CODENDUM_TOOL_CALL_PARSER:-qwen3_coder}"
sse_keepalive="${CODENDUM_SSE_KEEPALIVE_SECONDS:-15}"
max_queued="${CODENDUM_MAX_NUM_QUEUED_REQS:-}"
extra_args="${CODENDUM_VLLM_EXTRA_ARGS:-}"
start_timeout="${CODENDUM_START_TIMEOUT:-3600}"

# ---------------------------------------------------------------- validation
errors=0
fail() { log_error "$1"; errors=$((errors + 1)); }

if [[ -z "$image" ]]; then
    fail "CODENDUM_VLLM_IMAGE is not set (copy .env.example to .env)"
else
    rc=0
    check_pinned_image CODENDUM_VLLM_IMAGE "$image" || rc=$?
    ((rc != 2)) || errors=$((errors + 1))
fi
[[ -n "$revision" ]] || log_warn "CODENDUM_MODEL_REVISION is empty; the latest model revision will be loaded"
[[ "$served_name" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid CODENDUM_SERVED_MODEL_NAME: ${served_name}"
[[ "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || fail "invalid CODENDUM_CONTAINER_NAME: ${container}"
# nginx, the health check and the scripts all use 127.0.0.1; publish through the proxy only.
[[ "$host" == 127.0.0.1 ]] || fail "CODENDUM_HOST must be 127.0.0.1 (got '${host}'); expose the API through the proxy"
uint_in_range "$port" 1 65535 || fail "invalid CODENDUM_PORT: ${port}"
uint_in_range "$max_model_len" 1024 1048576 || fail "invalid CODENDUM_MAX_MODEL_LEN: ${max_model_len}"
uint_in_range "$max_num_seqs" 1 4096 || fail "invalid CODENDUM_MAX_NUM_SEQS: ${max_num_seqs}"
uint_in_range "$max_batched" 512 1048576 || fail "invalid CODENDUM_MAX_NUM_BATCHED_TOKENS: ${max_batched}"
is_fraction "$gpu_util" || fail "CODENDUM_GPU_MEMORY_UTILIZATION must be a fraction such as 0.80 (got '${gpu_util}')"
[[ "$kv_dtype" =~ ^[a-z0-9_]+$ ]] || fail "invalid CODENDUM_KV_CACHE_DTYPE: ${kv_dtype}"
[[ "$tool_parser" =~ ^[a-z0-9_]+$ ]] || fail "invalid CODENDUM_TOOL_CALL_PARSER: ${tool_parser}"
is_uint "$sse_keepalive" || fail "invalid CODENDUM_SSE_KEEPALIVE_SECONDS: ${sse_keepalive}"
[[ -z "$max_queued" ]] || uint_in_range "$max_queued" 1 100000 || fail "invalid CODENDUM_MAX_NUM_QUEUED_REQS: ${max_queued}"
case "$restart_policy" in
    no | always | unless-stopped | on-failure | on-failure:[0-9]*) ;;
    *) fail "invalid CODENDUM_RESTART_POLICY: ${restart_policy}" ;;
esac
[[ "$hf_offline" == 0 || "$hf_offline" == 1 ]] || fail "CODENDUM_HF_OFFLINE must be 0 or 1"
uint_in_range "$start_timeout" 60 86400 || fail "invalid CODENDUM_START_TIMEOUT: ${start_timeout}"
if [[ -z "$hf_cache" ]]; then
    fail "CODENDUM_HF_CACHE_DIR is not set"
elif [[ ! -d "$hf_cache" ]]; then
    if ((dry_run)); then
        log_warn "Hugging Face cache directory does not exist yet: ${hf_cache}"
    else
        fail "Hugging Face cache directory does not exist: ${hf_cache} (create it: sudo install -d -m 0755 -o \"\$USER\" ${hf_cache})"
    fi
fi
if [[ -n "$secrets_file" ]]; then
    if [[ ! -f "$secrets_file" ]]; then
        fail "secrets file not found: ${secrets_file}"
    elif [[ "$(uname -s)" == Linux ]]; then
        mode="$(stat -c '%a' "$secrets_file")"
        [[ "$mode" =~ ^[0-7]00$ ]] || fail "secrets file ${secrets_file} has mode ${mode}; run: chmod 600 ${secrets_file}"
    fi
fi
if [[ " $extra_args" =~ [[:space:]]--(api-key|hf-token)([=[:space:]]|$) ]]; then
    fail "CODENDUM_VLLM_EXTRA_ARGS must not carry credentials; use the secrets file"
fi
((errors == 0)) || die "${errors} configuration error(s); nothing was started"

# ---------------------------------------------------------------- command
health_cmd="python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:${port}/health', timeout=5)\""

docker_args=(
    run --detach
    --name "$container"
    --gpus all
    --ipc host
    --network host
    --ulimit memlock=-1
    --ulimit stack=67108864
    --restart "$restart_policy"
    --log-driver local --log-opt max-size=50m --log-opt max-file=5
    --health-cmd "$health_cmd"
    # The first start downloads the model; failures during this period do not
    # mark the container unhealthy.
    --health-interval 30s --health-timeout 10s --health-retries 3 --health-start-period "${start_timeout}s"
    --label "io.codendum.component=vllm"
    --label "io.codendum.profile=${CODENDUM_PROFILE}"
    --volume "${hf_cache}:/root/.cache/huggingface"
    --env HF_HOME=/root/.cache/huggingface
)
((hf_offline)) && docker_args+=(--env HF_HUB_OFFLINE=1)
[[ -n "$secrets_file" ]] && docker_args+=(--env-file "$secrets_file")
docker_args+=(--entrypoint vllm "$image")

serve_args=(
    serve "$model"
    --served-model-name "$served_name"
    --host "$host"
    --port "$port"
    --max-model-len "$max_model_len"
    --max-num-seqs "$max_num_seqs"
    --max-num-batched-tokens "$max_batched"
    --gpu-memory-utilization "$gpu_util"
    --kv-cache-dtype "$kv_dtype"
    --enable-chunked-prefill
    --enable-prefix-caching
    --enable-auto-tool-choice
    --tool-call-parser "$tool_parser"
)
[[ -n "$revision" ]] && serve_args+=(--revision "$revision")
((10#$sse_keepalive > 0)) && serve_args+=(--sse-keep-alive-interval "$sse_keepalive")
[[ -n "$max_queued" ]] && serve_args+=(--max-num-queued-reqs "$max_queued")
if [[ -n "$extra_args" ]]; then
    read -r -a extra <<<"$extra_args"
    serve_args+=("${extra[@]}")
fi

print_command() {
    local arg out="docker"
    for arg in "${docker_args[@]}" "${serve_args[@]}"; do
        if [[ "$arg" == -* ]]; then
            out+=$' \\\n    '"$(printf '%q' "$arg")"
        else
            out+=" $(printf '%q' "$arg")"
        fi
    done
    printf '%s\n' "$out"
}

log_info "Profile: ${CODENDUM_PROFILE} (max-model-len=${max_model_len}, max-num-seqs=${max_num_seqs}," \
    "max-num-batched-tokens=${max_batched}, gpu-memory-utilization=${gpu_util}, kv-cache-dtype=${kv_dtype})"
if [[ -n "${CODENDUM_CLIENT_CONTEXT_LIMIT:-}" ]]; then
    log_info "OpenCode clients must use limit.context=${CODENDUM_CLIENT_CONTEXT_LIMIT}, limit.output=${CODENDUM_CLIENT_OUTPUT_LIMIT:-8192}"
fi
log_info "Command (secrets, if any, are passed through --env-file and never printed):"
print_command

if ((dry_run)); then
    log_info "Dry run: nothing was started."
    exit 0
fi

# ---------------------------------------------------------------- launch
require_cmd docker "see docs/installation.md"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running, and is this user allowed to use it?)"

if docker container inspect "$container" >/dev/null 2>&1; then
    if ((replace)); then
        log_info "Stopping and removing existing container '${container}' (--replace)"
        docker stop --time 60 "$container" >/dev/null || true
        docker rm "$container" >/dev/null
    else
        die "container '${container}' already exists. Inspect it with 'docker ps -a'; use --replace to recreate it."
    fi
fi

if port_in_use "$port"; then
    die "port ${port} is already in use on this host"
fi

if ! docker image inspect "$image" >/dev/null 2>&1; then
    log_info "Pulling ${image} (several GiB, first run only)"
    docker pull "$image"
fi

id="$(docker "${docker_args[@]}" "${serve_args[@]}")"
log_info "Started container ${container} (${id:0:12})."
log_info "Follow the logs with: docker logs -f ${container}"

if ((wait_health)); then
    require_cmd curl
    log_info "Waiting up to ${start_timeout} s for http://${host}:${port}/health (model download and load)"
    deadline=$((SECONDS + start_timeout))
    until curl -fsS --max-time 5 --noproxy '*' "http://127.0.0.1:${port}/health" >/dev/null 2>&1; do
        if ! docker container inspect -f '{{.State.Running}}' "$container" 2>/dev/null | grep -q true; then
            die "container stopped; check: docker logs ${container}"
        fi
        ((SECONDS < deadline)) || die "timed out waiting for /health; check: docker logs ${container}"
        sleep 10
    done
    log_info "vLLM is healthy. Next: scripts/smoke-test.sh"
fi
