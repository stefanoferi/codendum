#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# End-to-end test of the web panel prototype, all in Docker: the nginx proxy
# container (start-proxy.sh) publishes the panel, the panel runs in a stock
# Python container with the Docker socket, and tests/mock_vllm.py stands in
# for vLLM. The administrator registers through the one-time setup link; a
# user registers, is approved, uses the API key, runs the one-time workstation
# setup command (with a fake opencode) and is blocked; the administration
# settings and password reset work; forms need the session's CSRF token;
# administrators follow into Open WebUI (tests/mock_openwebui.py) and switch
# its web search; the dashboard gets samples, the engine options of a stand-in
# vLLM container (never its environment) and GPU figures from a fake
# nvidia-smi; repeated failed logins are refused.

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
OWUI_PORT_FILE="${TMP_ROOT}/owui.port"
python3 tests/mock_openwebui.py --port-file "$OWUI_PORT_FILE" &
CLEANUP_PIDS+=("$!")
for _ in $(seq 1 100); do [[ -s "$OWUI_PORT_FILE" ]] && break; sleep 0.05; done
OWUI_PORT="$(cat "$OWUI_PORT_FILE")"
owui_state() { curl -s --noproxy '*' "http://127.0.0.1:${OWUI_PORT}/_state"; }
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
VLLM="codendum-vllm-webtest-$$"
mkdir -p "${P}/web"
{
    grep '^CODENDUM_PROXY_IMAGE=' .env.example
    echo "CODENDUM_PROXY_CONTAINER_NAME=${PROXY}"
    echo "CODENDUM_PROXY_DIR=${P}"
} >"${TMP_ROOT}/proxy.env"
stop_all() { docker rm -f "$PROXY" "$WEB" "$VLLM" >/dev/null 2>&1 || true; }
CLEANUP_HOOK=stop_all

echo "# start"
expect_exit 0 "proxy starts with the panel configuration" env -u CODENDUM_ENV_FILE scripts/start-proxy.sh -e "${TMP_ROOT}/proxy.env"
# Stands in for the vLLM container: the options start-vllm.sh passes, a secret
# in the environment, and an nvidia-smi that reports one field as [N/A].
printf '#!/bin/sh\necho "NVIDIA GB10, 37, 51, 14.20, [N/A]"\n' >"${TMP_ROOT}/nvidia-smi"
chmod 755 "${TMP_ROOT}/nvidia-smi"
check "stand-in vLLM container starts" docker run -d --name "$VLLM" --label io.codendum.profile=test \
    -e HF_TOKEN=hf_webtest_not_shown -v "${TMP_ROOT}/nvidia-smi":/usr/local/bin/nvidia-smi:ro \
    --entrypoint python3 "$PY_IMAGE" -c 'import time; time.sleep(86400)' serve coder \
    --max-model-len 65536 --max-num-seqs 24 --gpu-memory-utilization 0.80 --kv-cache-dtype fp8 --enable-prefix-caching
check "panel container starts" docker run -d --name "$WEB" --network host \
    -v "$ROOT":/codendum:ro -v "$P":/proxy -v "${P}/web":/data -v /var/run/docker.sock:/var/run/docker.sock \
    -e CODENDUM_WEB_LISTEN="127.0.0.1:${WEB_PORT}" -e CODENDUM_WEB_DB=/data/web.sqlite \
    -e CODENDUM_WEB_MAP=/proxy/api-keys-web.map -e CODENDUM_WEB_PROXY_CONTAINER="$PROXY" \
    -e CODENDUM_WEB_METRICS_URL="http://127.0.0.1:${MOCK_PORT}/metrics" -e CODENDUM_WEB_VLLM_CONTAINER="$VLLM" \
    -e CODENDUM_WEB_API_URL="https://{host}:${HTTPS_PORT}" -e CODENDUM_WEB_CHAT_URL="https://{host}:8444/" \
    -e CODENDUM_WEB_OPENWEBUI_URL="http://127.0.0.1:${OWUI_PORT}" \
    "$PY_IMAGE" python3 /codendum/web/codendum_web.py
for _ in $(seq 1 100); do curl -s -o /dev/null "http://127.0.0.1:${WEB_PORT}/login" && break; sleep 0.1; done

URL="https://localhost:${HTTPS_PORT}"
C=(curl -sS --noproxy '*' --cacert "${P}/tls/fullchain.pem" --max-time 20)
page_of() { "${C[@]}" -b "$1" "${URL}/"; }
admin_of() { "${C[@]}" -b "$1" "${URL}/admin${2:-}"; }
csrf_of() { page_of "$1" | grep -oE 'name=csrf value="[0-9a-f]{64}"' | head -1 | grep -oE '[0-9a-f]{64}'; }
# post JAR PATH [FIELDS]: a form sent from the panel's own page, with the CSRF token.
post() { "${C[@]}" -o /dev/null -w '%{http_code}' -b "$1" --data "csrf=$(csrf_of "$1")${3:+&$3}" "${URL}$2"; }
code_of() { "${C[@]}" -o /dev/null -w '%{http_code}' "$@"; }
A="${TMP_ROOT}/admin.jar"
S="${TMP_ROOT}/student.jar"

HEAD_SHA="$(git rev-parse HEAD)"
if "${C[@]}" "${URL}/login" | grep -q "href=\"https://codendum.ai\".*href=\"https://github.com/stefanoferi/codendum/commit/${HEAD_SHA}\".*<code>${HEAD_SHA:0:7}</code>"; then
    ok "footer: links to the site and GitHub, and the commit"; else not_ok "footer: links to the site and GitHub, and the commit"; fi
echo "# registration and approval through the proxy"
SETUP="$(docker logs "$WEB" 2>&1 | grep -oE 'register\?setup=[A-Za-z0-9_-]+' | head -1 | cut -d= -f2)"
if [[ -n "$SETUP" ]]; then ok "panel logs a one-time setup link"; else not_ok "panel logs a one-time setup link"; fi
check "student registers before any administrator" "${C[@]}" -o /dev/null -c "$S" -d "email=s@example.org&password=password123" "${URL}/register"
if page_of "$S" | grep -q 'Waiting for approval'; then ok "student waits for approval, does not become admin"; else not_ok "student waits for approval, does not become admin"; fi
check "admin registers with the setup link" "${C[@]}" -o /dev/null -c "$A" -d "email=T@Example.org&password=password123&setup=${SETUP}" "${URL}/register"
if admin_of "$A" /people | grep -q 'Approve all'; then ok "setup account is the administrator"; else not_ok "setup account is the administrator"; fi
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
if [[ "$(code_of -b "$A" -X POST "${URL}/admin/approve-all")" == 403 ]] && page_of "$S" | grep -q 'Waiting for approval'; then
    ok "a form without the CSRF token is refused"; else not_ok "a form without the CSRF token is refused"; fi
if [[ "$(post "$A" /admin/approve-all)" == 303 ]]; then ok "admin approves everyone"; else not_ok "admin approves everyone"; fi
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
if [[ "$(post "$S" /setup/new)" == 303 ]]; then ok "student creates a setup command"; else not_ok "student creates a setup command"; fi
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

echo "# single sign-on for Open WebUI"
if "${C[@]}" -o /dev/null -D - -b "$S" "${URL}/auth/verify" | grep -qi '^x-codendum-email: s@example.org'; then
    ok "/auth/verify gives the e-mail of an approved user"; else not_ok "/auth/verify gives the e-mail of an approved user"; fi
if [[ "$(code_of "${URL}/auth/verify")" == 401 ]]; then ok "/auth/verify refuses without a session"; else not_ok "/auth/verify refuses without a session"; fi
if page_of "$S" | grep -q 'href="https://localhost:8444/" target="_blank" rel="noopener">Open the chat'; then
    ok "chat link uses the browser host and opens in a new tab"; else not_ok "chat link uses the browser host and opens in a new tab"; fi

loc_of() { "${C[@]}" -o /dev/null -w '%{redirect_url}' -d "email=t@example.org&password=password123&next=$1" "${URL}/login"; }
if [[ "$(loc_of 'https://localhost:8444/c/abc')" == "https://localhost:8444/c/abc" ]]; then
    ok "after login, back to the chat on the same host"; else not_ok "after login, back to the chat on the same host"; fi
if [[ "$(loc_of 'https://evil.example/')" == "${URL}/" && "$(loc_of '//evil.example/')" == "${URL}/" ]]; then
    ok "after login, other hosts are refused (no open redirect)"; else not_ok "after login, other hosts are refused"; fi

echo "# administration"
if [[ "$(code_of -b "$S" "${URL}/admin")" == 403 ]]; then ok "student cannot open the administration"; else not_ok "student cannot open the administration"; fi
post "$A" /admin/settings "domains=example.org&notice=Server+restarts+at+13:00" >/dev/null
if [[ "$("${C[@]}" -d "email=late@example.org&password=password123" -o /dev/null -w '%{http_code}' "${URL}/register")" == 400 ]] &&
    check_email 'late@example.org' | grep -q 'Registration is closed' && ! "${C[@]}" "${URL}/login" | grep -q 'href="/register"'; then
    ok "registration closed: no new accounts, no Register link"; else not_ok "registration closed: no new accounts, no Register link"; fi
if page_of "$S" | grep -q 'Server restarts at 13:00'; then ok "users see the notice"; else not_ok "users see the notice"; fi
if [[ "$(post "$A" /admin/settings "registration=open&domains=not_a_domain")" == 400 ]]; then
    ok "settings: invalid domain refused"; else not_ok "settings: invalid domain refused"; fi
post "$A" /admin/settings "registration=open&domains=%40Example.org&auto_approve=on" >/dev/null
if admin_of "$A" /settings | grep -q 'value="example.org"' && check_email 'x@other.example' | grep -q 'Only addresses at example.org'; then
    ok "registration limited to the allowed domains"; else not_ok "registration limited to the allowed domains"; fi
R="${TMP_ROOT}/auto.jar"
"${C[@]}" -o /dev/null -c "$R" -d "email=auto@example.org&password=password123" "${URL}/register"
if page_of "$R" | grep -qE 'cdm_[0-9a-f]{64}'; then ok "allowed domain approved automatically"; else not_ok "allowed domain approved automatically"; fi
post "$A" /admin/settings "registration=open" >/dev/null
"${C[@]}" -o /dev/null -c "$R" -d "email=other@other.example&password=password123" "${URL}/register"
if page_of "$R" | grep -q 'Waiting for approval'; then ok "without domains, new accounts wait again"; else not_ok "without domains, new accounts wait again"; fi
SID="$(docker exec "$WEB" python3 -c "import sqlite3; print(sqlite3.connect('/data/web.sqlite').execute(\"select id from users where username='s@example.org'\").fetchone()[0])")"
NEWPW="$("${C[@]}" -b "$A" --data "csrf=$(csrf_of "$A")" "${URL}/admin/${SID}/password" | grep -oE '<pre id="new-password">[^<]+' | cut -d'>' -f2)"
if [[ -n "$NEWPW" && "$(code_of -b "$S" "${URL}/")" == 303 ]]; then ok "password reset logs the user out"; else not_ok "password reset logs the user out"; fi
if [[ "$(code_of -c "$S" --data-urlencode "email=s@example.org" --data-urlencode "password=${NEWPW}" "${URL}/login")" == 303 ]] &&
    [[ "$(code_of -d "email=s@example.org&password=password123" "${URL}/login")" == 401 ]]; then
    ok "the new password works, the old one does not"; else not_ok "the new password works, the old one does not"; fi

echo "# dashboard and chat"
if admin_of "$A" | grep -q 'id="c-tps"' && admin_of "$A" /people | grep -q 'href="/admin/settings"'; then
    ok "administration split into dashboard, people and settings"; else not_ok "administration split into dashboard, people and settings"; fi
# The panel samples every 5 seconds; rates need two samples in a row.
for _ in $(seq 1 15); do "${C[@]}" -b "$A" "${URL}/api/status" | grep -q '"gen_tps"' && break; sleep 1; done
if "${C[@]}" -b "$A" "${URL}/api/status" | python3 -c '
import json, sys
s = json.load(sys.stdin)
h = s["history"]
assert any(p.get("up") == 1 for p in h) and any("gen_tps" in p for p in h), h[-3:]
assert s["model"] == "coder" and s["people"]["active"] >= 2
'; then ok "dashboard: live samples with token rates, model, people"; else not_ok "dashboard: live samples with token rates, model, people"; fi
if "${C[@]}" -b "$A" "${URL}/api/status" | python3 -c '
import json, sys
text = sys.stdin.read()
s, e = json.loads(text), json.loads(text)["engine"]
assert "hf_webtest_not_shown" not in text and "HF_TOKEN" not in text
assert e["max-num-seqs"] == "24" and e["kv-cache-dtype"] == "fp8" and e["enable-prefix-caching"] is True, e
assert e["profile"] == "test" and e["state"] == "running" and "started" in e and "enable-chunked-prefill" not in e, e
assert s["version"] == "mock" and s["weights"].startswith("Qwen/") and s["gpu_name"] == "NVIDIA GB10", s
g = [p for p in s["history"] if "gpu" in p]
assert g and g[-1]["gpu"] == 37 and g[-1]["temp"] == 51 and g[-1]["power"] == 14.2 and g[-1]["clock"] is None, g[-1:]
assert any(p.get("itl") for p in s["history"]) and s["last_request"] and s["e2e_mean"] == 0.5, s["history"][-3:]
'; then ok "dashboard: engine options without the environment, GPU, latency, last request"
else not_ok "dashboard: engine options without the environment, GPU, latency, last request"; fi
if "${C[@]}" -o /dev/null -D - -b "$A" "${URL}/auth/verify" | grep -qi '^x-codendum-role: admin' &&
    "${C[@]}" -o /dev/null -D - -b "$S" "${URL}/auth/verify" | grep -qi '^x-codendum-role: user'; then
    ok "/auth/verify gives the role for Open WebUI"; else not_ok "/auth/verify gives the role for Open WebUI"; fi
if admin_of "$A" /settings | grep -q 'name=web_search value=on checked' &&
    owui_state | grep -q '"t@example.org": "admin"'; then
    ok "settings show the chat web search, read as an administrator of Open WebUI"; else not_ok "settings show the chat web search"; fi
post "$A" /admin/chat "" >/dev/null
if owui_state | grep -q '"web_search": false' && ! admin_of "$A" /settings | grep -q 'name=web_search value=on checked'; then
    ok "web search switched off in Open WebUI"; else not_ok "web search switched off in Open WebUI"; fi
post "$A" /admin/chat "web_search=on" >/dev/null
if owui_state | grep -q '"web_search": true'; then ok "web search switched on again"; else not_ok "web search switched on again"; fi
post "$A" "/admin/${SID}/promote" >/dev/null
promoted="$(owui_state | grep -o '"s@example.org": "[a-z]*"')"
post "$A" "/admin/${SID}/demote" >/dev/null
if [[ "$promoted" == '"s@example.org": "admin"' ]] && owui_state | grep -q '"s@example.org": "user"'; then
    ok "promote and demote apply to Open WebUI at once"; else not_ok "promote and demote apply to Open WebUI at once ($promoted)"; fi

echo "# profile"
S2="${TMP_ROOT}/student2.jar"
"${C[@]}" -o /dev/null -c "$S2" --data-urlencode "email=s@example.org" --data-urlencode "password=${NEWPW}" "${URL}/login"
if page_of "$S" | grep -q 'href="/profile">Profile and password'; then ok "profile menu in the header"; else not_ok "profile menu in the header"; fi
if [[ "$(post "$S" /profile/password "current=wrong-password&password=password456&repeat=password456")" == 400 &&
      "$(post "$S" /profile/password "current=${NEWPW}&password=password456&repeat=password789")" == 400 ]]; then
    ok "password change refused: wrong current password, different repeat"; else not_ok "password change refused when wrong"; fi
if [[ "$(post "$S" /profile/password "current=${NEWPW}&password=password456&repeat=password456")" == 303 ]] &&
    [[ "$(code_of -b "$S" "${URL}/")" == 200 && "$(code_of -b "$S2" "${URL}/")" == 303 ]]; then
    ok "password changed: this session stays, the others are logged out"; else not_ok "password changed: sessions"; fi
if [[ "$(code_of -d "email=s@example.org&password=password456" "${URL}/login")" == 303 ]]; then
    ok "the changed password works"; else not_ok "the changed password works"; fi

echo "# block"
if [[ "$(post "$A" "/admin/${SID}/block")" == 303 ]]; then ok "admin blocks the student"; else not_ok "admin blocks the student"; fi
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
