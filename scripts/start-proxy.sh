#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Run the Codendum HTTPS proxy (nginx) in Docker, in front of vLLM.

set -Eeuo pipefail
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: scripts/start-proxy.sh [options]

Run nginx in a hardened Docker container with host networking. It listens on
the port set by the "listen" directive of the site configuration (8443 in
the example) and forwards the allowed endpoints to vLLM on 127.0.0.1.

The proxy directory (CODENDUM_PROXY_DIR, default /etc/codendum/proxy) holds:
  conf.d/codendum.conf   site configuration, from config/nginx.example.conf
  tls/fullchain.pem      certificate chain trusted by the workstations
  tls/privkey.pem        private key (mode 0600)
  api-keys.map           per-user keys from scripts/gen-api-keys.sh (mode 0600)
  logs/                  nginx logs

Options:
      --init           create the proxy directory and copy the example
                       configuration (never overwrites existing files)
      --dry-run        validate the files and print the docker command only
      --replace        stop and remove an existing proxy container first
      --reload         test the configuration and reload the running proxy
                       (after changing keys, certificates or configuration)
  -e, --env-file FILE  configuration file (default: .env in the repository root)
  -h, --help           show this help
EOF
}

env_file="" mode=start dry_run=0 replace=0
while (($#)); do
    case "$1" in
        -e | --env-file) env_file="${2:?--env-file needs a value}"; shift 2 ;;
        --init) mode=init; shift ;;
        --reload) mode=reload; shift ;;
        --dry-run) dry_run=1; shift ;;
        --replace) replace=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" 64 ;;
    esac
done

load_config "$env_file"

image="${CODENDUM_PROXY_IMAGE:-}"
container="${CODENDUM_PROXY_CONTAINER_NAME:-codendum-proxy}"
dir="${CODENDUM_PROXY_DIR:-/etc/codendum/proxy}"
conf="${dir}/conf.d/codendum.conf"
[[ "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "invalid CODENDUM_PROXY_CONTAINER_NAME: ${container}"
[[ "$dir" == /* ]] || die "CODENDUM_PROXY_DIR must be an absolute path (got '${dir}')"

# ---------------------------------------------------------------- --init
if [[ "$mode" == init ]]; then
    umask 077
    mkdir -p "${dir}/conf.d" "${dir}/tls" "${dir}/logs"
    if [[ -e "$conf" ]]; then
        log_info "Keeping existing ${conf}"
    else
        cp "${CODENDUM_ROOT}/config/nginx.example.conf" "$conf"
        log_info "Created ${conf} from config/nginx.example.conf"
    fi
    log_info "Next steps:"
    log_info "  1. edit ${conf}: host name, allowed networks, port"
    log_info "  2. install the certificate as ${dir}/tls/fullchain.pem and ${dir}/tls/privkey.pem (mode 0600)"
    log_info "  3. install the key map from scripts/gen-api-keys.sh as ${dir}/api-keys.map (mode 0600)"
    log_info "  4. scripts/start-proxy.sh"
    exit 0
fi

# ---------------------------------------------------------------- validation
errors=0
fail() { log_error "$1"; errors=$((errors + 1)); }

if [[ -z "$image" ]]; then
    fail "CODENDUM_PROXY_IMAGE is not set (see .env.example)"
else
    rc=0
    check_pinned_image CODENDUM_PROXY_IMAGE "$image" || rc=$?
    ((rc != 2)) || errors=$((errors + 1))
fi
[[ -d "$dir" ]] || die "proxy directory not found: ${dir} (create it with: scripts/start-proxy.sh --init)"
for f in "$conf" "${dir}/tls/fullchain.pem" "${dir}/tls/privkey.pem" "${dir}/api-keys.map"; do
    [[ -f "$f" ]] || fail "missing file: ${f}"
done
[[ -d "${dir}/logs" ]] || fail "missing directory: ${dir}/logs"
if [[ "$(uname -s)" == Linux ]]; then
    for f in "${dir}/tls/privkey.pem" "${dir}/api-keys.map"; do
        [[ -f "$f" ]] || continue
        file_mode="$(stat -c '%a' "$f")"
        [[ "$file_mode" =~ ^[0-7]00$ ]] || fail "${f} has mode ${file_mode}; secrets must not be readable by others: chmod 600 ${f}"
    done
fi
ports=()
if [[ -f "$conf" ]]; then
    mapfile -t ports < <(proxy_listen_ports "$conf")
    ((${#ports[@]})) || fail "no 'listen' directive found in ${conf}"
fi
((errors == 0)) || die "${errors} configuration error(s); nothing was started"

# ---------------------------------------------------------------- command
mounts=(
    --volume "${dir}:/etc/nginx/codendum:ro"
    --volume "${dir}/conf.d:/etc/nginx/conf.d:ro"
    --volume "${dir}/logs:/var/log/nginx"
)
# The master process runs as root inside the container: it reads the key map
# and TLS key (DAC_OVERRIDE), prepares the temp directories (CHOWN) and
# starts the workers as the unprivileged "nginx" user (SETUID, SETGID).
caps=(--cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add SETUID --cap-add SETGID)
for p in "${ports[@]}"; do
    if ((10#$p < 1024)); then
        caps+=(--cap-add NET_BIND_SERVICE)
        break
    fi
done
hardening=(--read-only --tmpfs /var/cache/nginx --tmpfs /run --security-opt no-new-privileges:true)

docker_args=(
    run --detach
    --name "$container"
    --network host
    --restart unless-stopped
    --log-driver local --log-opt max-size=10m --log-opt max-file=3
    --label "io.codendum.component=proxy"
    --env NGINX_ENTRYPOINT_QUIET_LOGS=1
    "${hardening[@]}"
    "${caps[@]}"
    "${mounts[@]}"
    "$image"
)
test_args=(run --rm --network none --env NGINX_ENTRYPOINT_QUIET_LOGS=1 "${hardening[@]}" "${caps[@]}" "${mounts[@]}" "$image" nginx -t -q)

server_name="$(sed -nE 's/^[[:space:]]*server_name[[:space:]]+([^;[:space:]]+).*/\1/p' "$conf" | head -n 1)"

if [[ "$mode" == reload ]]; then
    require_cmd docker
    docker container inspect "$container" >/dev/null 2>&1 || die "proxy container '${container}' does not exist; start it first"
    docker exec "$container" nginx -t -q || die "configuration test failed; the running proxy was not changed"
    docker kill --signal HUP "$container" >/dev/null
    log_info "Proxy configuration reloaded."
    exit 0
fi

log_info "Proxy listening on port(s) ${ports[*]}; clients use https://${server_name:-<host>}:${ports[0]}/v1"
log_info "Command:"
printf 'docker'
for arg in "${docker_args[@]}"; do
    if [[ "$arg" == -* ]]; then printf ' \\\n    %q' "$arg"; else printf ' %q' "$arg"; fi
done
printf '\n'
if ((dry_run)); then
    log_info "Dry run: nothing was started."
    exit 0
fi

# ---------------------------------------------------------------- launch
require_cmd docker "see README: Prerequisites"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running, and is this user allowed to use it?)"

exists=0
docker container inspect "$container" >/dev/null 2>&1 && exists=1
if ((exists && !replace)); then
    die "container '${container}' already exists. Inspect it with 'docker ps -a'; use --replace to recreate it or --reload to apply changes."
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then
    log_info "Pulling ${image}"
    docker pull --quiet "$image" >/dev/null
fi
log_info "Testing the configuration"
docker "${test_args[@]}" || die "nginx configuration test failed; fix ${conf} and retry"

if ((exists)); then
    log_info "Stopping and removing existing container '${container}' (--replace)"
    docker stop --time 10 "$container" >/dev/null || true
    docker rm "$container" >/dev/null
fi
for p in "${ports[@]}"; do
    port_in_use "$p" && die "port ${p} is already in use on this host; change 'listen' in ${conf}"
done

id="$(docker "${docker_args[@]}")"
log_info "Started proxy container ${container} (${id:0:12}). Logs: ${dir}/logs/"
if have curl && ! curl -fsS --max-time 3 --noproxy '*' "http://127.0.0.1:${CODENDUM_PORT:-8000}/health" >/dev/null 2>&1; then
    log_warn "vLLM does not answer on 127.0.0.1:${CODENDUM_PORT:-8000} yet; the proxy returns 502 until it does"
fi
