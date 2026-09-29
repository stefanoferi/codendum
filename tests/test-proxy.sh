#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Runs config/nginx.example.conf in front of tests/mock_vllm.py and checks the
# proxy behaviour: network allowlist, API keys, endpoint allowlist (including
# dot-segment paths), unbuffered streaming, per-user limits and JSON errors.
#
#   tests/test-proxy.sh docker   nginx container started by scripts/start-proxy.sh
#   tests/test-proxy.sh native   unprivileged nginx binary from the host

# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

MODE="${1:-docker}"
case "$MODE" in
    native) needed=nginx ;;
    docker) needed=docker ;;
    *) echo "usage: tests/test-proxy.sh docker|native" >&2; exit 64 ;;
esac
if ! command -v "$needed" >/dev/null 2>&1 || { [[ "$MODE" == docker ]] && ! docker info >/dev/null 2>&1; }; then
    if [[ "${CI:-}" == true ]]; then
        echo "${needed} is required in CI" >&2
        exit 1
    fi
    echo "SKIP: ${needed} not available (needed for the ${MODE} proxy tests)"
    exit 0
fi
for cmd in openssl curl python3; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "missing: $cmd" >&2; exit 1; }
done

# Layout shared by both modes (the same as CODENDUM_PROXY_DIR).
P="${TMP_ROOT}/proxy"
mkdir -p "$P"/{conf.d,tls,logs,tmp}
HTTPS_PORT="$(free_port)"
start_mock --first-token-delay 0.2 --stream-delay 0.25
UPSTREAM_PORT="$MOCK_PORT"

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost" \
    -keyout "${P}/tls/privkey.pem" -out "${P}/tls/fullchain.pem" >/dev/null 2>&1
scripts/gen-api-keys.sh --out-dir "${TMP_ROOT}/keys" --count 3 >/dev/null
cp "${TMP_ROOT}/keys/api-keys.map" "${P}/api-keys.map"
chmod 600 "${P}/tls/privkey.pem" "${P}/api-keys.map"
KEY1="$(sed -n '2p' "${TMP_ROOT}/keys/api-keys.csv" | cut -d, -f2)"
KEY2="$(sed -n '3p' "${TMP_ROOT}/keys/api-keys.csv" | cut -d, -f2)"

# render_site [SED_EXPR...]: the example configuration with test ports and
# host name. Native mode also rewrites the container paths to the prefix.
render_site() {
    local extra=()
    [[ "$MODE" == native ]] && extra=(-e "s#/etc/nginx/codendum/#${P}/#g" -e "s#/var/log/nginx/#${P}/logs/#g")
    sed -e "s#server 127.0.0.1:8000;#server 127.0.0.1:${UPSTREAM_PORT};#" \
        -e "s#listen 8443 ssl;#listen 127.0.0.1:${HTTPS_PORT} ssl;#" \
        -e "s#server_name llm.lab.example;#server_name localhost;#" \
        "${extra[@]}" "$@" config/nginx.example.conf >"${P}/conf.d/codendum.conf"
}
render_site

if [[ "$MODE" == native ]]; then
    cat >"${P}/nginx.conf" <<CONF
pid ${P}/nginx.pid;
error_log ${P}/logs/error.log warn;
events { worker_connections 256; }
http {
    access_log ${P}/logs/access.log;
    client_body_temp_path ${P}/tmp/body;
    proxy_temp_path ${P}/tmp/proxy;
    fastcgi_temp_path ${P}/tmp/fastcgi;
    uwsgi_temp_path ${P}/tmp/uwsgi;
    scgi_temp_path ${P}/tmp/scgi;
    include ${P}/conf.d/codendum.conf;
}
CONF
    NGINX=(nginx -p "$P" -c "${P}/nginx.conf" -e "${P}/logs/error.log")
    start_proxy() {
        check "example configuration is valid" "${NGINX[@]}" -t -q
        "${NGINX[@]}"
    }
    reload_proxy() { "${NGINX[@]}" -s reload; }
    stop_proxy() { "${NGINX[@]}" -s stop >/dev/null 2>&1 || true; }
else
    CONTAINER="codendum-proxy-test-$$"
    PROXY_ENV="${TMP_ROOT}/proxy.env"
    {
        grep '^CODENDUM_PROXY_IMAGE=' .env.example
        echo "CODENDUM_PROXY_CONTAINER_NAME=${CONTAINER}"
        echo "CODENDUM_PROXY_DIR=${P}"
        echo "CODENDUM_PORT=${UPSTREAM_PORT}"
    } >"$PROXY_ENV"
    proxy_cmd() { env -u CODENDUM_ENV_FILE -u CODENDUM_PROXY_DIR -u CODENDUM_PROXY_CONTAINER_NAME scripts/start-proxy.sh -e "$PROXY_ENV" "$@"; }
    start_proxy() {
        expect_exit 0 "start-proxy.sh starts the container" proxy_cmd
        check "root file system is read-only" bash -c "[[ \$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' ${CONTAINER}) == true ]]"
        check "capabilities dropped (no NET_BIND_SERVICE on a high port)" bash -c \
            "docker inspect -f '{{.HostConfig.CapDrop}}' ${CONTAINER} | grep -q ALL && ! docker inspect -f '{{.HostConfig.CapAdd}}' ${CONTAINER} | grep -q NET_BIND_SERVICE"
        expect_exit 1 "an existing proxy container is not replaced silently" proxy_cmd
    }
    reload_proxy() { proxy_cmd --reload >/dev/null; }
    stop_proxy() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
fi
CLEANUP_HOOK=stop_proxy

echo "# start (${MODE})"
start_proxy
for _ in $(seq 1 100); do
    curl -s -o /dev/null --noproxy '*' -k "https://127.0.0.1:${HTTPS_PORT}/" && break
    sleep 0.1
done

URL="https://localhost:${HTTPS_PORT}"
CURL=(curl -sS --noproxy '*' --cacert "${P}/tls/fullchain.pem" --max-time 20)

# status METHOD PATH [KEY]: prints the HTTP status code.
status() {
    local auth=()
    [[ -n "${3:-}" ]] && auth=(-H "Authorization: Bearer $3")
    "${CURL[@]}" -o /dev/null -w '%{http_code}' -X "$1" "${auth[@]}" "${URL}$2"
}
expect_status() {
    local want="$1" desc="$2" got
    shift 2
    got="$(status "$@")"
    if [[ "$got" == "$want" ]]; then ok "$desc"; else not_ok "$desc (got ${got}, expected ${want})"; fi
}

echo "# authentication"
expect_status 401 "no API key -> 401" GET /v1/models
expect_status 401 "unknown API key -> 401" GET /v1/models "cdm_$(printf '0%.0s' {1..64})"
expect_status 200 "valid API key -> 200" GET /v1/models "$KEY1"
body="$("${CURL[@]}" -D "${TMP_ROOT}/h401" "${URL}/v1/models")"
check "401 body is OpenAI-style JSON" python3 -c 'import json,sys; assert json.loads(sys.argv[1])["error"]["type"] == "authentication_error"' "$body"
check "401 carries WWW-Authenticate" grep -qi '^www-authenticate: Bearer' "${TMP_ROOT}/h401"

echo "# endpoint allowlist"
for path in /health /metrics /version /ping /load /invocations /tokenize /v1/embeddings /v1/completions /v1/responses /v1/messages /docs /; do
    expect_status 404 "GET ${path} is not exposed" GET "$path" "$KEY1"
done
expect_status 404 "POST /tokenize is not exposed" POST /tokenize "$KEY1"
for path in /metrics/../v1/models /v1/models/../../metrics /tokenize/../v1/models; do
    body="$("${CURL[@]}" --path-as-is -H "Authorization: Bearer ${KEY1}" "${URL}${path}")"
    if [[ "$body" == *"vllm:"* ]]; then not_ok "dot segments cannot reach private endpoints (${path})" "$body"; else ok "dot segments cannot reach private endpoints (${path})"; fi
done

echo "# smoke test through the proxy"
LAST_OUTPUT="$(CODENDUM_API_KEY="$KEY1" python3 scripts/lib/codendum.py smoke --base-url "$URL" --proxy --cacert "${P}/tls/fullchain.pem" 2>&1)" && rc=0 || rc=$?
if [[ "$rc" == 0 ]]; then ok "smoke-test --proxy passes"; else not_ok "smoke-test --proxy passes (exit ${rc})" "$LAST_OUTPUT"; fi
expect_output_contains "private endpoints verified" "[PASS] private endpoints"
expect_output_contains "anonymous access verified" "[PASS] authentication"

echo "# streaming is not buffered"
check "SSE chunks arrive progressively" python3 - "$URL" "$KEY1" "${P}/tls/fullchain.pem" <<'PY'
import sys, importlib.util
spec = importlib.util.spec_from_file_location("codendum", "scripts/lib/codendum.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
client = mod.Client(sys.argv[1], sys.argv[2], sys.argv[3], 20)
payload = {"model": "coder", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 6,
           "stream": True, "ignore_eos": True}
times = [t for t, ev in client.stream("/v1/chat/completions", payload)
         if not ev.get("done") and (mod._first_choice(ev).get("delta") or {}).get("content")]
spread = times[-1] - times[0]
# 6 chunks, 0.25 s apart upstream: a buffering proxy would deliver them together.
assert len(times) == 6 and spread > 0.8, (len(times), spread)
PY

echo "# per-user concurrency limit"
stream_status() {
    "${CURL[@]}" -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
        -d '{"model":"coder","messages":[{"role":"user","content":"x"}],"max_tokens":8,"stream":true,"ignore_eos":true}' \
        "${URL}/v1/chat/completions"
}
: >"${TMP_ROOT}/codes"
pids=()
for _ in 1 2 3 4 5; do
    stream_status "$KEY1" >>"${TMP_ROOT}/codes" &
    pids+=($!)
done
stream_status "$KEY2" >"${TMP_ROOT}/other"
wait "${pids[@]}"
check "user over its limit gets 429" grep -q '^429$' "${TMP_ROOT}/codes"
check "at most 3 concurrent requests per user succeed" bash -c "[[ \$(grep -c '^200$' '${TMP_ROOT}/codes') -le 3 ]]"
check "another user is not affected" grep -qx 200 "${TMP_ROOT}/other"
check "access log records the user id, not the key" bash -c "grep -q 'user=user01' '${P}/logs/codendum.access.log' && ! grep -q 'cdm_' '${P}/logs/codendum.access.log'"

echo "# upstream down"
stop_mock
expect_status 502 "upstream unavailable -> 502" GET /v1/models "$KEY1"
body="$("${CURL[@]}" -H "Authorization: Bearer ${KEY1}" "${URL}/v1/models")"
check "502 body is OpenAI-style JSON" python3 -c 'import json,sys; assert json.loads(sys.argv[1])["error"]["type"] == "server_error"' "$body"

echo "# network allowlist (configuration reload)"
render_site -e "s#^    127.0.0.1/32     1;.*##"
reload_proxy
sleep 1
expect_status 403 "address outside the allowlist -> 403" GET /v1/models "$KEY1"

finish
