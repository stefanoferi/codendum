#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# End-to-end test of the web panel prototype, all in Docker: the nginx proxy
# container (start-proxy.sh) publishes the panel, the panel runs in a stock
# Python container with the Docker socket, and tests/mock_vllm.py stands in
# for vLLM. The administrator registers through the one-time setup link; a
# user registers, is approved, uses the API key, runs the one-time workstation
# setup command (with a fake opencode) and is blocked; repeated failed logins
# are refused.

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
    -e CODENDUM_WEB_API_URL="https://{host}:${HTTPS_PORT}" \
    "$PY_IMAGE" python3 /codendum/web/codendum_web.py
for _ in $(seq 1 100); do curl -s -o /dev/null "http://127.0.0.1:${WEB_PORT}/login" && break; sleep 0.1; done

URL="https://localhost:${HTTPS_PORT}"
C=(curl -sS --noproxy '*' --cacert "${P}/tls/fullchain.pem" --max-time 20)
page_of() { "${C[@]}" -b "$1" "${URL}/"; }
code_of() { "${C[@]}" -o /dev/null -w '%{http_code}' "$@"; }
A="${TMP_ROOT}/admin.jar"
S="${TMP_ROOT}/student.jar"

echo "# registration and approval through the proxy"
SETUP="$(docker logs "$WEB" 2>&1 | grep -oE 'register\?setup=[A-Za-z0-9_-]+' | head -1 | cut -d= -f2)"
if [[ -n "$SETUP" ]]; then ok "panel logs a one-time setup link"; else not_ok "panel logs a one-time setup link"; fi
check "student registers before any administrator" "${C[@]}" -o /dev/null -c "$S" -d "email=s@example.org&password=password123" "${URL}/register"
if page_of "$S" | grep -q 'Waiting for approval'; then ok "student waits for approval, does not become admin"; else not_ok "student waits for approval, does not become admin"; fi
check "admin registers with the setup link" "${C[@]}" -o /dev/null -c "$A" -d "email=T@Example.org&password=password123&setup=${SETUP}" "${URL}/register"
if page_of "$A" | grep -q 'Approve all'; then ok "setup account is the administrator"; else not_ok "setup account is the administrator"; fi
if [[ "$(code_of -d "email=i@example.org&password=password123&setup=${SETUP}" "${URL}/register")" == 400 ]]; then
    ok "setup link works only once"; else not_ok "setup link works only once"; fi
echo "# e-mail as user name"
check_email() { "${C[@]}" -d "email=$1" "${URL}/register/check"; }
if check_email 's@example.org' | grep -q '"ok": false' && check_email 'S@EXAMPLE.ORG' | grep -q 'already registered'; then
    ok "registration check: e-mail already registered (any case)"; else not_ok "registration check: e-mail already registered"; fi
if check_email 'new.person@example.org' | grep -q '"ok": true'; then ok "registration check: free e-mail"; else not_ok "registration check: free e-mail"; fi
if check_email 'not-an-email' | grep -q 'not a valid'; then ok "registration check: invalid e-mail"; else not_ok "registration check: invalid e-mail"; fi
if [[ "$(code_of -d "email=s@example.org&password=password123" "${URL}/register")" == 400 ]]; then
    ok "the same e-mail cannot register twice"; else not_ok "the same e-mail cannot register twice"; fi
if page_of "$A" | grep -q 't@example.org'; then ok "header shows the e-mail as user name"; else not_ok "header shows the e-mail as user name"; fi
check "admin approves everyone" "${C[@]}" -o /dev/null -b "$A" -X POST "${URL}/admin/approve-all"
KEY="$("${C[@]}" -b "$S" "${URL}/" | grep -oE 'cdm_[0-9a-f]{64}' | head -1)"
if [[ -n "$KEY" ]]; then ok "student sees an API key"; else not_ok "student sees an API key"; fi
if page_of "$S" | grep -q "https://localhost:${HTTPS_PORT}/v1"; then ok "API URL uses the host name the browser used"; else not_ok "API URL uses the host name the browser used"; fi
sleep 1
got="$("${C[@]}" -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${KEY}" "${URL}/v1/models")"
if [[ "$got" == 200 ]]; then ok "the new key works on the API at once (proxy reloaded)"; else not_ok "the new key works on the API (got ${got})" "$(docker logs "$WEB" 2>&1 | tail -5)"; fi
check "map file is private" bash -c "[[ \$(stat -c %a '${P}/api-keys-web.map') == 600 ]]"
check "proxy still running after reloads, never killed" bash -c "[[ \$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' '$PROXY') == 'true 0' ]]"
if [[ "$(code_of -b "$S" "${URL}/api/status")" == 403 ]]; then ok "student cannot read the service status"; else not_ok "student cannot read the service status"; fi
if "${C[@]}" -b "$A" "${URL}/api/status" | grep -q '"vllm": "up"'; then ok "admin reads the service status"; else not_ok "admin reads the service status"; fi

echo "# workstation setup"
check "student creates a setup command" "${C[@]}" -o /dev/null -b "$S" -X POST "${URL}/setup/new"
CMD="$(page_of "$S" | grep -oE "curl -fsSk --pinnedpubkey &#x27;sha256//[A-Za-z0-9+/=]+&#x27; https://[^ ]+/s/[A-Za-z0-9_-]+ \| sh" | sed "s/&#x27;/'/g")"
if [[ -n "$CMD" ]]; then ok "page shows the pinned one-line command"; else not_ok "page shows the pinned one-line command"; fi
if page_of "$S" | grep -q 'Out-String | Invoke-Expression'; then ok "page shows the Windows command"; else not_ok "page shows the Windows command"; fi
W="${TMP_ROOT}/workstation"
mkdir -p "$W/home" "$W/bin"
cat >"$W/bin/opencode" <<'STUB'
#!/bin/sh
case "$1 $2" in
    "--version "*) echo "opencode v2.0.32" ;;
    "debug paths") printf 'config     %s/.config/opencode\n' "$HOME" ;;
    "service "*) echo "$*" >>"$HOME/opencode-calls" ;;
esac
STUB
chmod +x "$W/bin/opencode"
mkdir -p "$W/home/.config/opencode"
echo '{"theme": "system", "providers": {"other": {"name": "Other"}}}' >"$W/home/.config/opencode/opencode.json"
if env NO_PROXY="*" no_proxy="*" HOME="$W/home" SHELL=/bin/bash PATH="$W/bin:$PATH" NODE_EXTRA_CA_CERTS= bash -c "$CMD" >"$W/out" 2>&1; then
    ok "setup command runs on a workstation"; else not_ok "setup command runs on a workstation" "$(tail -5 "$W/out")"; fi
OC="$W/home/.config/opencode/opencode.json"
if python3 - "$OC" "$HTTPS_PORT" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
p = c["providers"]["codendum"]
assert c["theme"] == "system" and "other" in c["providers"], "other settings kept"
assert c["model"] == "codendum/coder" and c["share"] == "disabled"
assert p["settings"]["baseURL"] == "https://localhost:%s/v1" % sys.argv[2], p["settings"]["baseURL"]
assert p["settings"]["apiKey"] == "{file:~/.config/codendum/api-key}"
assert p["models"]["coder"]["limit"]["context"] > 0
PY
then ok "OpenCode configuration merged, other settings kept"; else not_ok "OpenCode configuration merged"; fi
if compgen -G "$W/home/.config/opencode/opencode.json.bak-*" >/dev/null; then ok "backup of the old configuration"; else not_ok "backup of the old configuration"; fi
if [[ "$(cat "$W/home/.config/codendum/api-key")" == "$KEY" && "$(stat -c %a "$W/home/.config/codendum/api-key")" == 600 ]]; then
    ok "API key stored in a private file"; else not_ok "API key stored in a private file"; fi
if ! grep -q "$KEY" "$OC"; then ok "API key is not in opencode.json"; else not_ok "API key is not in opencode.json"; fi
if grep -q "service set env NODE_EXTRA_CA_CERTS $W/home/.config/codendum/ca.pem" "$W/home/opencode-calls" && grep -q 'codendum/ca.pem' "$W/home/.bashrc"; then
    ok "OpenCode service and shell trust the certificate"; else not_ok "OpenCode service and shell trust the certificate"; fi
if [[ "$(code_of "${URL}/ca.pem")" == 200 ]] && diff -q <("${C[@]}" "${URL}/ca.pem") "${P}/tls/fullchain.pem" >/dev/null; then
    ok "certificate is served at /ca.pem"; else not_ok "certificate is served at /ca.pem"; fi
if ! env NO_PROXY="*" no_proxy="*" HOME="$W/home" PATH="$W/bin:$PATH" bash -o pipefail -c "$CMD" >/dev/null 2>&1; then ok "setup command works only once"; else not_ok "setup command works only once"; fi
PIN="$(grep -oE 'sha256//[A-Za-z0-9+/=]+' <<<"$CMD")"
if curl -fsSk --noproxy '*' --pinnedpubkey "$PIN" -o /dev/null "${URL}/ca.pem" &&
    ! curl -fsSk --noproxy '*' --pinnedpubkey 'sha256//AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=' -o /dev/null "${URL}/ca.pem" 2>/dev/null; then
    ok "pin matches the proxy key, a different key is refused"; else not_ok "pin matches the proxy key, a different key is refused"; fi

echo "# block"
SID="$(docker exec "$WEB" python3 -c "import sqlite3; print(sqlite3.connect('/data/web.sqlite').execute(\"select id from users where username='s@example.org'\").fetchone()[0])")"
check "admin blocks the student" "${C[@]}" -o /dev/null -b "$A" -X POST "${URL}/admin/${SID}/block"
sleep 1
got="$("${C[@]}" -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${KEY}" "${URL}/v1/models")"
if [[ "$got" == 401 ]]; then ok "blocked key is rejected"; else not_ok "blocked key is rejected (got ${got})"; fi

echo "# login rate limit"
for _ in 1 2 3 4 5; do code_of -d "email=s@example.org&password=wrong-password" "${URL}/login" >/dev/null; done
if [[ "$(code_of -d "email=s@example.org&password=password123" "${URL}/login")" == 429 ]]; then
    ok "login refused after 5 failures, even with the right password"; else not_ok "login refused after 5 failures"; fi
if [[ "$(code_of -d "email=t@example.org&password=password123" "${URL}/login")" == 303 ]]; then
    ok "other users can still log in"; else not_ok "other users can still log in"; fi

finish
