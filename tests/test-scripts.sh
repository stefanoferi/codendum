#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Offline tests for the scripts in scripts/, using tests/mock_vllm.py and a
# fake docker command. No GPU, network access or model weights are needed.

# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

echo "# Python unit tests"
check "unittest tests/test_codendum.py" python3 -m unittest -q tests/test_codendum.py

# ------------------------------------------------------------------ fixtures
HF_DIR="${TMP_ROOT}/hf-cache"
mkdir -p "$HF_DIR"
ENV_FILE="${TMP_ROOT}/test.env"
cat >"$ENV_FILE" <<EOF
# test configuration
CODENDUM_VLLM_IMAGE=registry.example/vllm/vllm-openai:v0.0.0@sha256:0000000000000000000000000000000000000000000000000000000000000000
CODENDUM_MODEL_REVISION=0123456789abcdef0123456789abcdef01234567
CODENDUM_HF_CACHE_DIR="${HF_DIR}"   # quoted value, then a comment
CODENDUM_CONTAINER_NAME=codendum-test   # inline comment
EOF
SECRETS="${TMP_ROOT}/vllm.secrets.env"
printf 'HF_TOKEN=placeholder-value-for-tests\n' >"$SECRETS"
chmod 600 "$SECRETS"

# Run with no inherited CODENDUM_* variables so the developer's shell cannot leak in.
clean_env() {
    local name unset_args=()
    for name in $(compgen -e); do
        [[ "$name" == CODENDUM_* ]] && unset_args+=(-u "$name")
    done
    env "${unset_args[@]}" "$@"
}

echo "# start-vllm.sh"
expect_exit 0 "help" scripts/start-vllm.sh --help
expect_exit 0 "list profiles" scripts/start-vllm.sh --list-profiles
expect_output_contains "lists classroom-64k" "classroom-64k"
expect_output_contains "lists deep-128k" "deep-128k"

expect_exit 0 "dry run, default profile" clean_env scripts/start-vllm.sh --env-file "$ENV_FILE" --dry-run
for flag in "--max-model-len 65536" "--max-num-seqs 16" "--max-num-batched-tokens 8192" \
    "--gpu-memory-utilization 0.80" "--kv-cache-dtype fp8" "--enable-chunked-prefill" \
    "--enable-prefix-caching" "--enable-auto-tool-choice" "--tool-call-parser qwen3_coder" \
    "--served-model-name coder" "--host 127.0.0.1" "--port 8000" "--network host" \
    "--revision 0123456789abcdef0123456789abcdef01234567" "--sse-keep-alive-interval 15" \
    "--name codendum-test" "--restart unless-stopped" "--health-start-period 3600s"; do
    expect_output_contains "dry run contains ${flag}" "$flag"
done
expect_output_lacks "no published port" "--publish"
expect_output_contains "quoted value with comment parsed" "--volume ${HF_DIR}:/root/.cache/huggingface"
expect_output_lacks "no -p port mapping" " -p "

expect_exit 0 "dry run, high-concurrency profile" clean_env scripts/start-vllm.sh -e "$ENV_FILE" -p classroom-64k-high-concurrency --dry-run
expect_output_contains "high-concurrency uses 24 sequences" "--max-num-seqs 24"
expect_exit 0 "dry run, deep-128k profile" clean_env scripts/start-vllm.sh -e "$ENV_FILE" --profile deep-128k --dry-run
expect_output_contains "deep-128k context" "--max-model-len 131072"
expect_output_contains "deep-128k sequences" "--max-num-seqs 8"
expect_exit 0 "environment overrides profile" clean_env CODENDUM_MAX_NUM_SEQS=12 scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_output_contains "override applied" "--max-num-seqs 12"
expect_output_contains "override reported" "CODENDUM_MAX_NUM_SEQS=12 overrides the profile value 16"
expect_output_contains "client limit printed" "limit.context=65536"
expect_exit 0 "profile from environment" clean_env CODENDUM_PROFILE=deep-128k scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_output_contains "profile selected by variable" "--max-model-len 131072"
expect_exit 0 "optional queue bound" clean_env CODENDUM_MAX_NUM_QUEUED_REQS=64 scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_output_contains "queue bound passed" "--max-num-queued-reqs 64"

expect_exit 0 "secrets passed by file" clean_env CODENDUM_SECRETS_ENV_FILE="$SECRETS" scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_output_contains "env-file flag present" "--env-file ${SECRETS}"
expect_output_lacks "secret value never printed" "placeholder-value-for-tests"
chmod 644 "$SECRETS"
expect_exit 1 "world-readable secrets file rejected" clean_env CODENDUM_SECRETS_ENV_FILE="$SECRETS" scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
chmod 600 "$SECRETS"

expect_exit 1 "unknown profile rejected" clean_env scripts/start-vllm.sh -e "$ENV_FILE" -p does-not-exist --dry-run
expect_exit 1 "image 'latest' rejected" clean_env CODENDUM_VLLM_IMAGE=vllm/vllm-openai:latest scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_exit 1 "untagged image rejected" clean_env CODENDUM_VLLM_IMAGE=vllm/vllm-openai scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_exit 1 "non-loopback host rejected" clean_env CODENDUM_HOST=0.0.0.0 scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_exit 1 "only 127.0.0.1 accepted" clean_env CODENDUM_HOST=192.0.2.10 scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_exit 1 "bad memory fraction rejected" clean_env CODENDUM_GPU_MEMORY_UTILIZATION=1.2 scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_exit 1 "credentials in extra args rejected" clean_env CODENDUM_VLLM_EXTRA_ARGS="--api-key abc" scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_exit 1 "credentials in extra args rejected (= form)" clean_env CODENDUM_VLLM_EXTRA_ARGS="--hf-token=abc" scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_exit 0 "ordinary extra args accepted" clean_env CODENDUM_VLLM_EXTRA_ARGS="--tokenizer-mode auto --no-enable-prefix-caching" scripts/start-vllm.sh -e "$ENV_FILE" --dry-run
expect_output_contains "extra args appended" "--no-enable-prefix-caching"
expect_exit 64 "unknown option rejected" scripts/start-vllm.sh --bogus

printf 'CODENDUM_PROFILE="deep-128k\n' >"${TMP_ROOT}/unterminated.env"
expect_exit 1 "unterminated quote rejected" clean_env scripts/start-vllm.sh -e "${TMP_ROOT}/unterminated.env" --dry-run
BAD_ENV="${TMP_ROOT}/bad.env"
printf 'PATH=/tmp\n' >"$BAD_ENV"
expect_exit 1 "non-CODENDUM variable in env file rejected" clean_env scripts/start-vllm.sh -e "$BAD_ENV" --dry-run
MARKER="${TMP_ROOT}/pwned"
cp "$ENV_FILE" "${TMP_ROOT}/inject.env"
# shellcheck disable=SC2016  # the literal $(...) must reach the env file unexpanded
printf 'CODENDUM_VLLM_EXTRA_ARGS=$(touch %s)\n' "$MARKER" >>"${TMP_ROOT}/inject.env"
expect_exit 0 "env file values are not evaluated" clean_env scripts/start-vllm.sh -e "${TMP_ROOT}/inject.env" --dry-run
if [[ -e "$MARKER" ]]; then not_ok "command substitution executed from env file"; else ok "command substitution not executed"; fi

# Fake docker: records calls; "container inspect" succeeds when FAKE_EXISTS=1.
SHIM="${TMP_ROOT}/shim"
mkdir -p "$SHIM"
cat >"${SHIM}/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >>"$FAKE_DOCKER_LOG"
case "$1 $2" in
    "info "*) exit 0 ;;
    "container inspect") [[ "${FAKE_EXISTS:-0}" == 1 ]] && exit 0 || exit 1 ;;
    "image inspect") exit 0 ;;
    "run --detach") echo "0123456789abcdef"; exit 0 ;;
    "stop "* | "rm "*) exit 0 ;;
esac
exit 0
EOF
chmod +x "${SHIM}/docker"
export FAKE_DOCKER_LOG="${TMP_ROOT}/docker.log"

: >"$FAKE_DOCKER_LOG"
expect_exit 1 "existing container is not replaced silently" clean_env FAKE_EXISTS=1 PATH="${SHIM}:${PATH}" scripts/start-vllm.sh -e "$ENV_FILE"
expect_output_contains "explains --replace" "--replace"
if grep -qE '^docker (run|rm|stop)' "$FAKE_DOCKER_LOG"; then not_ok "no run/stop/rm without --replace" "$(cat "$FAKE_DOCKER_LOG")"; else ok "no run/stop/rm without --replace"; fi

: >"$FAKE_DOCKER_LOG"
expect_exit 0 "--replace recreates the container" clean_env FAKE_EXISTS=1 PATH="${SHIM}:${PATH}" scripts/start-vllm.sh -e "$ENV_FILE" --replace
check "stop, rm and run were called in order" bash -c "grep -E '^docker (stop|rm|run)' '$FAKE_DOCKER_LOG' | cut -d' ' -f2 | paste -sd, - | grep -qx 'stop,rm,run'"

: >"$FAKE_DOCKER_LOG"
expect_exit 0 "fresh start" clean_env FAKE_EXISTS=0 PATH="${SHIM}:${PATH}" scripts/start-vllm.sh -e "$ENV_FILE"
check "container started with vllm entrypoint and GPU" grep -q -- '--gpus all.*--entrypoint vllm' "$FAKE_DOCKER_LOG"

echo "# start-proxy.sh"
PD="${TMP_ROOT}/proxy-dir"
PROXY_ENV="${TMP_ROOT}/proxy.env"
{
    grep '^CODENDUM_PROXY_IMAGE=' .env.example
    echo "CODENDUM_PROXY_DIR=${PD}"
    echo "CODENDUM_PROXY_CONTAINER_NAME=codendum-proxy-test"
} >"$PROXY_ENV"
proxy() { clean_env PATH="${SHIM}:${PATH}" scripts/start-proxy.sh -e "$PROXY_ENV" "$@"; }

expect_exit 0 "help" scripts/start-proxy.sh --help
expect_exit 1 "missing proxy directory explained" proxy --dry-run
expect_output_contains "points to --init" "--init"
expect_exit 0 "--init creates the layout" proxy --init
check "config copied from the example" cmp -s config/nginx.example.conf "${PD}/conf.d/codendum.conf"
check "directories are private" bash -c "[[ \$(stat -c %a '${PD}') == 700 && -d '${PD}/tls' && -d '${PD}/logs' ]]"
echo "# local change" >>"${PD}/conf.d/codendum.conf"
expect_exit 0 "--init again" proxy --init
check "--init never overwrites the configuration" grep -q '^# local change' "${PD}/conf.d/codendum.conf"
expect_exit 1 "missing certificate and key map rejected" proxy --dry-run
expect_output_contains "names the missing file" "tls/privkey.pem"
printf 'placeholder\n' >"${PD}/tls/fullchain.pem"
printf 'placeholder\n' >"${PD}/tls/privkey.pem"
printf '"Bearer placeholder" "user01";\n' >"${PD}/api-keys.map"
chmod 644 "${PD}/tls/privkey.pem"
chmod 600 "${PD}/api-keys.map"
expect_exit 1 "readable private key rejected" proxy --dry-run
chmod 600 "${PD}/tls/privkey.pem"
expect_exit 0 "dry run" proxy --dry-run
for flag in "--network host" "--read-only" "--cap-drop ALL" "--security-opt no-new-privileges:true" \
    "${PD}:/etc/nginx/codendum:ro" "${PD}/conf.d:/etc/nginx/conf.d:ro" "${PD}/logs:/var/log/nginx" \
    "--restart unless-stopped"; do
    expect_output_contains "dry run contains ${flag}" "$flag"
done
expect_output_contains "port read from the configuration" "https://llm.lab.example:8443/v1"
expect_output_lacks "no privileged-port capability on 8443" "NET_BIND_SERVICE"
sed -i 's/listen 8443 ssl;/listen 443 ssl;/' "${PD}/conf.d/codendum.conf"
expect_exit 0 "dry run with port 443" proxy --dry-run
expect_output_contains "port 443 adds NET_BIND_SERVICE" "NET_BIND_SERVICE"
sed -i 's/listen 443 ssl;/listen 8443 ssl;/' "${PD}/conf.d/codendum.conf"
expect_exit 1 "unpinned proxy image rejected" clean_env CODENDUM_PROXY_IMAGE=nginx:latest PATH="${SHIM}:${PATH}" scripts/start-proxy.sh -e "$PROXY_ENV" --dry-run
expect_exit 1 "relative proxy directory rejected" clean_env CODENDUM_PROXY_DIR=proxy scripts/start-proxy.sh --dry-run

: >"$FAKE_DOCKER_LOG"
expect_exit 1 "existing proxy container is not replaced silently" clean_env FAKE_EXISTS=1 PATH="${SHIM}:${PATH}" scripts/start-proxy.sh -e "$PROXY_ENV"
if grep -qE '^docker (run|rm|stop)' "$FAKE_DOCKER_LOG"; then not_ok "no run/stop/rm without --replace" "$(cat "$FAKE_DOCKER_LOG")"; else ok "no run/stop/rm without --replace"; fi
: >"$FAKE_DOCKER_LOG"
expect_exit 0 "--replace tests the config before replacing" clean_env FAKE_EXISTS=1 PATH="${SHIM}:${PATH}" scripts/start-proxy.sh -e "$PROXY_ENV" --replace
check "order: config test, stop, rm, run" bash -c "grep -E '^docker (run|stop|rm)' '$FAKE_DOCKER_LOG' | awk '{print (\$2 == \"run\" ? \$2 \$3 : \$2)}' | paste -sd, - | grep -qx 'run--rm,stop,rm,run--detach'"
: >"$FAKE_DOCKER_LOG"
expect_exit 0 "--reload" clean_env FAKE_EXISTS=1 PATH="${SHIM}:${PATH}" scripts/start-proxy.sh -e "$PROXY_ENV" --reload
check "reload tests then signals nginx" bash -c "grep -E '^docker (exec|kill)' '$FAKE_DOCKER_LOG' | cut -d' ' -f2 | paste -sd, - | grep -qx 'exec,kill'"

echo "# preflight.sh"
expect_exit 0 "help" scripts/preflight.sh --help
rc=0
LAST_OUTPUT="$(clean_env scripts/preflight.sh -e "$ENV_FILE" 2>&1)" || rc=$?
if [[ "$rc" == 0 || "$rc" == 1 ]]; then ok "runs to completion (exit ${rc})"; else not_ok "runs to completion (exit ${rc})" "$LAST_OUTPUT"; fi
expect_output_contains "prints a summary" "Summary:"
expect_output_contains "checks the architecture" "Architecture"
expect_output_lacks "no unbound variables" "unbound variable"

echo "# smoke-test.sh"
start_mock
BASE="http://127.0.0.1:${MOCK_PORT}"
expect_exit 0 "all checks pass against a correct server" clean_env scripts/smoke-test.sh --base-url "$BASE"
expect_output_contains "tool call checked" "[PASS] tool call (auto)"
expect_output_contains "round trip checked" "[PASS] tool round trip"
expect_exit 1 "unknown model fails" clean_env scripts/smoke-test.sh --base-url "$BASE" --model other
stop_mock

start_mock --tool-mode none
expect_exit 2 "no tool call with tool_choice=auto is inconclusive" clean_env scripts/smoke-test.sh --base-url "http://127.0.0.1:${MOCK_PORT}"
expect_output_contains "reported as inconclusive" "INCONCLUSIVE"
expect_output_contains "required path still verified" "[PASS] tool call (required)"
stop_mock

start_mock --tool-mode raw-markup
expect_exit 1 "raw tool markup in content fails" clean_env scripts/smoke-test.sh --base-url "http://127.0.0.1:${MOCK_PORT}"
expect_output_contains "parser hint" "tool-call-parser"
stop_mock

start_mock --require-key test-key-123
expect_exit 1 "missing API key fails" clean_env scripts/smoke-test.sh --base-url "http://127.0.0.1:${MOCK_PORT}"
expect_exit 0 "API key read from CODENDUM_API_KEY" clean_env CODENDUM_API_KEY=test-key-123 scripts/smoke-test.sh --base-url "http://127.0.0.1:${MOCK_PORT}"
stop_mock
expect_exit 1 "unreachable server fails" clean_env scripts/smoke-test.sh --base-url "http://127.0.0.1:$(free_port)"

echo "# metrics.sh"
start_mock
expect_exit 0 "metrics summary" clean_env scripts/metrics.sh --url "http://127.0.0.1:${MOCK_PORT}/metrics"
expect_output_contains "KV cache usage shown" "KV cache usage"
expect_output_contains "preemptions shown" "Preemptions"
expect_output_contains "waiting requests shown" "Requests waiting"
expect_output_contains "KV capacity from cache_config_info" "1310720 tokens"
expect_exit 0 "metrics JSON" clean_env scripts/metrics.sh --url "http://127.0.0.1:${MOCK_PORT}/metrics" --json
check "metrics JSON is valid" python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert "kv_cache_usage_percent" in d["vllm"]' "$LAST_OUTPUT"
stop_mock
expect_exit 1 "unreachable metrics endpoint fails" clean_env scripts/metrics.sh --url "http://127.0.0.1:$(free_port)/metrics"

echo "# bench.sh"
start_mock --stream-delay 0.01
BENCH_OUT="${TMP_ROOT}/bench"
expect_exit 0 "small benchmark run" clean_env scripts/bench.sh --base-url "http://127.0.0.1:${MOCK_PORT}" \
    --metrics-url "http://127.0.0.1:${MOCK_PORT}/metrics" --concurrency 1,4 --short-input 32 --long-input 96 \
    --output-tokens 6 --rounds 1 --min-requests 2 --out-dir "$BENCH_OUT" --yes
check "summary.csv has one row per level and shape" bash -c "[[ \$(wc -l <'${BENCH_OUT}/summary.csv') -eq 5 ]]"
check "summary.json has percentiles and preemptions" python3 -c '
import json, sys
data = json.load(open(sys.argv[1]))
assert len(data["levels"]) == 4
for level in data["levels"]:
    assert level["failed"] == 0, level
    for key in ("ttft_p50_s", "ttft_p95_s", "ttft_p99_s", "output_throughput_tok_s", "preemptions"):
        assert key in level, key
    assert level["output_tokens_mean"] == 6
' "${BENCH_OUT}/summary.json"
check "requests.jsonl has one line per request" bash -c "[[ \$(wc -l <'${BENCH_OUT}/requests.jsonl') -eq 12 ]]"
expect_exit 64 "refuses without --yes on non-interactive input" clean_env scripts/bench.sh --base-url "http://127.0.0.1:${MOCK_PORT}" \
    --metrics-url none --concurrency 1 --out-dir "${TMP_ROOT}/bench2" </dev/null
stop_mock
start_mock --fake-waiting 3
expect_exit 1 "refuses to run while the server is busy" clean_env scripts/bench.sh --base-url "http://127.0.0.1:${MOCK_PORT}" \
    --metrics-url "http://127.0.0.1:${MOCK_PORT}/metrics" --concurrency 1 --out-dir "${TMP_ROOT}/bench3" --yes
expect_output_contains "busy message" "server is busy"
expect_exit 2 "zero rounds rejected" clean_env scripts/bench.sh --base-url "http://127.0.0.1:${MOCK_PORT}" --rounds 0 --yes
stop_mock

echo "# bench-classroom.sh"
expect_exit 0 "help" scripts/bench-classroom.sh --help
expect_exit 0 "simulator options listed" scripts/bench-classroom.sh --help-simulator
expect_output_contains "documents think time" "--think-time-min"
start_mock --stream-delay 0.005
CLASS_OUT="${TMP_ROOT}/classroom"
expect_exit 0 "small simulated class" clean_env scripts/bench-classroom.sh --base-url "http://127.0.0.1:${MOCK_PORT}" \
    --metrics-url "http://127.0.0.1:${MOCK_PORT}/metrics" --users 4 --duration 3 --ramp-up 0.5 --grace 20 \
    --think-time-min 0 --think-time-max 0.2 --tool-time-min 0 --tool-time-max 0.1 --max-turns 2 \
    --out-dir "$CLASS_OUT" --yes
check "every user completed its requests with tool calls" python3 -c '
import json, sys
data = json.load(open(sys.argv[1]))
client, server = data["client"], data["server"]
assert client["users_active"] == 4, client
assert client["turns_completed"] == 8 and client["turns_started"] == 8, client
assert client["tool_calls"] >= 8 and client["requests_ok"] == client["requests"], client
assert client["ttft_p95_s"] is not None and server["running_peak"] is not None, data
' "${CLASS_OUT}/summary.json"
check "per-request and per-turn logs written" bash -c "[[ -s '${CLASS_OUT}/requests.jsonl' && \$(wc -l <'${CLASS_OUT}/turns.jsonl') -eq 8 ]]"
check "tool calls were streamed and executed" bash -c "grep -q '\"finish_reason\": \"tool_calls\"' '${CLASS_OUT}/requests.jsonl'"
expect_exit 2 "zero users rejected" clean_env scripts/bench-classroom.sh --base-url "http://127.0.0.1:${MOCK_PORT}" --users 0 --yes
stop_mock

echo "# gen-api-keys.sh"
KEYS="${TMP_ROOT}/keys"
expect_exit 0 "generates keys" scripts/gen-api-keys.sh --out-dir "$KEYS" --count 5
check "map has 5 well-formed entries" bash -c "[[ \$(grep -cE '^\"Bearer cdm_[0-9a-f]{64}\" \"user0[1-5]\";$' '${KEYS}/api-keys.map') -eq 5 ]]"
check "csv has header and 5 rows" bash -c "[[ \$(wc -l <'${KEYS}/api-keys.csv') -eq 6 ]]"
check "keys are unique" bash -c "[[ \$(cut -d, -f2 '${KEYS}/api-keys.csv' | sort -u | wc -l) -eq 6 ]]"
check "files are mode 0600" bash -c "[[ \$(stat -c %a '${KEYS}/api-keys.map') == 600 && \$(stat -c %a '${KEYS}/api-keys.csv') == 600 ]]"
expect_exit 1 "does not overwrite existing keys" scripts/gen-api-keys.sh --out-dir "$KEYS" --count 5
printf 'alice\nBob Smith\n' >"${TMP_ROOT}/users.txt"
expect_exit 64 "rejects invalid user ids" scripts/gen-api-keys.sh --out-dir "${TMP_ROOT}/keys2" --users-file "${TMP_ROOT}/users.txt"
printf '  # indented comment\nalice\n\nbob\n' >"${TMP_ROOT}/users-ok.txt"
expect_exit 0 "users file with indented comments and blank lines" scripts/gen-api-keys.sh --out-dir "${TMP_ROOT}/keys3" --users-file "${TMP_ROOT}/users-ok.txt"
check "users file ids used" grep -q '"alice";' "${TMP_ROOT}/keys3/api-keys.map"
expect_exit 64 "missing users file is a usage error" scripts/gen-api-keys.sh --out-dir "${TMP_ROOT}/keys4" --users-file "${TMP_ROOT}/nope.txt"
expect_output_lacks "no traceback" "Traceback"
expect_exit 2 "zero count rejected" scripts/gen-api-keys.sh --out-dir "${TMP_ROOT}/keys5" --count 0
expect_exit 64 "users file or count required" scripts/gen-api-keys.sh --out-dir "${TMP_ROOT}/keys6"
git init -q "${TMP_ROOT}/repo"
expect_exit 1 "refuses a non-ignored directory inside a Git working tree" scripts/gen-api-keys.sh --out-dir "${TMP_ROOT}/repo/keys-out" --count 2

finish
