#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# End-to-end test of the web panel prototype, all in Docker: the nginx proxy
# container (start-proxy.sh) publishes the panel, the panel runs in a stock
# Python container with the Docker socket, and tests/mock_vllm.py stands in
# for vLLM. A user registers, is approved, uses the API key, and is blocked.

# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    [[ "${CI:-}" == true ]] && { echo "docker is required in CI" >&2; exit 1; }
    echo "SKIP: docker not available"
    exit 0
fi
PY_IMAGE="python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"

P="${TMP_ROOT}/proxy"
mkdir -p "$P"/{conf.d,tls,logs}
HTTPS_PORT="$(free_port)"
WEB_PORT="$(free_port)"
start_mock --stream-delay 0
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost" \
    -keyout "${P}/tls/privkey.pem" -out "${P}/tls/fullchain.pem" >/dev/null 2>&1
chmod 600 "${P}/tls/privkey.pem"
: >"${P}/api-keys.map"
chmod 600 "${P}/api-keys.map"
sed -e "s#server 127.0.0.1:8000;#server 127.0.0.1:${MOCK_PORT};#" \
    -e "s#listen 8443 ssl;#listen 127.0.0.1:${HTTPS_PORT} ssl;#" \
    -e "s#server_name llm.lab.example;#server_name localhost;#" \
    config/nginx.example.conf >"${P}/conf.d/codendum.conf"
python3 web/patch-nginx.py "${P}/conf.d/codendum.conf" "127.0.0.1:${WEB_PORT}"

PROXY="codendum-proxy-webtest-$$"
WEB="codendum-web-test-$$"
mkdir -p "${P}/web"
{
    grep '^CODENDUM_PROXY_IMAGE=' .env.example
    echo "CODENDUM_PROXY_CONTAINER_NAME=${PROXY}"
    echo "CODENDUM_PROXY_DIR=${P}"
} >"${TMP_ROOT}/proxy.env"
stop_all() { docker rm -f "$PROXY" "$WEB" >/dev/null 2>&1 || true; }
CLEANUP_HOOK=stop_all

echo "# start"
expect_exit 0 "proxy starts with the panel configuration" env -u CODENDUM_ENV_FILE scripts/start-proxy.sh -e "${TMP_ROOT}/proxy.env"
check "panel container starts" docker run -d --name "$WEB" --network host \
    -v "$ROOT":/codendum:ro -v "$P":/proxy -v "${P}/web":/data -v /var/run/docker.sock:/var/run/docker.sock \
    -e CODENDUM_WEB_LISTEN="127.0.0.1:${WEB_PORT}" -e CODENDUM_WEB_DB=/data/web.sqlite \
    -e CODENDUM_WEB_MAP=/proxy/api-keys-web.map -e CODENDUM_WEB_PROXY_CONTAINER="$PROXY" \
    -e CODENDUM_WEB_METRICS_URL="http://127.0.0.1:${MOCK_PORT}/metrics" \
    "$PY_IMAGE" python3 /codendum/web/codendum_web.py
for _ in $(seq 1 100); do curl -s -o /dev/null "http://127.0.0.1:${WEB_PORT}/login" && break; sleep 0.1; done

URL="https://localhost:${HTTPS_PORT}"
C=(curl -sS --noproxy '*' --cacert "${P}/tls/fullchain.pem" --max-time 20)
page_of() { "${C[@]}" -b "$1" "${URL}/"; }
code_of() { "${C[@]}" -o /dev/null -w '%{http_code}' "$@"; }
A="${TMP_ROOT}/admin.jar"
S="${TMP_ROOT}/student.jar"

echo "# registration and approval through the proxy"
check "admin registers (first account)" "${C[@]}" -o /dev/null -c "$A" -d "username=teacher&email=t@example.org&password=password123" "${URL}/register"
check "student registers" "${C[@]}" -o /dev/null -c "$S" -d "username=student1&email=s@example.org&password=password123" "${URL}/register"
if page_of "$S" | grep -q 'Waiting for approval'; then ok "student waits for approval"; else not_ok "student waits for approval"; fi
check "admin approves everyone" "${C[@]}" -o /dev/null -b "$A" -X POST "${URL}/admin/approve-all"
KEY="$("${C[@]}" -b "$S" "${URL}/" | grep -oE 'cdm_[0-9a-f]{64}' | head -1)"
if [[ -n "$KEY" ]]; then ok "student sees an API key"; else not_ok "student sees an API key"; fi
sleep 1
got="$("${C[@]}" -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${KEY}" "${URL}/v1/models")"
if [[ "$got" == 200 ]]; then ok "the new key works on the API at once (proxy reloaded)"; else not_ok "the new key works on the API (got ${got})" "$(docker logs "$WEB" 2>&1 | tail -5)"; fi
check "map file is private" bash -c "[[ \$(stat -c %a '${P}/api-keys-web.map') == 600 ]]"
check "proxy still running after reloads, never killed" bash -c "[[ \$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' '$PROXY') == 'true 0' ]]"
if [[ "$(code_of -b "$S" "${URL}/api/status")" == 403 ]]; then ok "student cannot read the service status"; else not_ok "student cannot read the service status"; fi
if "${C[@]}" -b "$A" "${URL}/api/status" | grep -q '"vllm": "up"'; then ok "admin reads the service status"; else not_ok "admin reads the service status"; fi

echo "# block"
SID="$(docker exec "$WEB" python3 -c "import sqlite3; print(sqlite3.connect('/data/web.sqlite').execute(\"select id from users where username='student1'\").fetchone()[0])")"
check "admin blocks the student" "${C[@]}" -o /dev/null -b "$A" -X POST "${URL}/admin/${SID}/block"
sleep 1
got="$("${C[@]}" -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${KEY}" "${URL}/v1/models")"
if [[ "$got" == 401 ]]; then ok "blocked key is rejected"; else not_ok "blocked key is rejected (got ${got})"; fi

finish
