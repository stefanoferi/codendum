# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Shared helpers for Codendum scripts. Source this file; do not execute it.

if [[ -z "${BASH_VERSINFO[0]:-}" ]] || ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
    echo "error: bash 4.4 or newer is required" >&2
    exit 1
fi

CODENDUM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CODENDUM_PY="${CODENDUM_ROOT}/scripts/lib/codendum.py"
CODENDUM_DEFAULT_PROFILE="classroom-64k"

log_info() { printf '%s\n' "$*" >&2; }
log_warn() { printf 'warning: %s\n' "$*" >&2; }
log_error() { printf 'error: %s\n' "$*" >&2; }

# die MESSAGE [EXIT_CODE]
die() {
    log_error "$1"
    exit "${2:-1}"
}

have() { command -v "$1" >/dev/null 2>&1; }

require_cmd() {
    have "$1" || die "required command not found: $1${2:+ ($2)}"
}

# load_env_file FILE [warn]
# Reads CODENDUM_* assignments without evaluating any shell code. Variables
# that are already set in the environment are left untouched, so exported
# values win over the file. With "warn", report such overrides.
load_env_file() {
    local file="$1" warn="${2:-}" line key value lineno=0
    [[ -r "$file" ]] || die "cannot read configuration file: $file"
    while IFS= read -r line || [[ -n "$line" ]]; do
        lineno=$((lineno + 1))
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ ! "$line" =~ ^[[:space:]]*(export[[:space:]]+)?(CODENDUM_[A-Z0-9_]+)=(.*)$ ]]; then
            die "${file}:${lineno}: expected CODENDUM_NAME=value"
        fi
        key="${BASH_REMATCH[2]}"
        value="${BASH_REMATCH[3]}"
        if [[ "$value" =~ ^\"([^\"]*)\"[[:space:]]*(#.*)?$ ]] || [[ "$value" =~ ^\'([^\']*)\'[[:space:]]*(#.*)?$ ]]; then
            value="${BASH_REMATCH[1]}"
        elif [[ "$value" == [\"\']* ]]; then
            die "${file}:${lineno}: unterminated quote"
        else
            value="${value%%[[:space:]]#*}"
            value="${value%"${value##*[![:space:]]}"}"
        fi
        if [[ -z "${!key+x}" ]]; then
            export "${key}=${value}"
        elif [[ -n "$warn" && "${!key}" != "$value" ]]; then
            log_warn "${key}=${!key} overrides the profile value ${value}"
        fi
    done <"$file"
}

# load_config [ENV_FILE]
# Loads the given file, or $CODENDUM_ENV_FILE, or ./.env at the repository
# root when present. Missing default files are not an error.
load_config() {
    local file="${1:-${CODENDUM_ENV_FILE:-}}"
    if [[ -n "$file" ]]; then
        load_env_file "$file"
    elif [[ -f "${CODENDUM_ROOT}/.env" ]]; then
        load_env_file "${CODENDUM_ROOT}/.env"
    fi
}

list_profiles() {
    local f
    for f in "${CODENDUM_ROOT}"/config/profiles/*.env; do
        [[ -e "$f" ]] || continue
        f="${f##*/}"
        printf '%s\n' "${f%.env}"
    done
}

# load_profile: applies config/profiles/$CODENDUM_PROFILE.env for any
# variable not already set by the environment or the configuration file.
load_profile() {
    local name="${CODENDUM_PROFILE:-$CODENDUM_DEFAULT_PROFILE}"
    [[ "$name" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "invalid profile name: ${name}"
    local file="${CODENDUM_ROOT}/config/profiles/${name}.env"
    [[ -f "$file" ]] || die "unknown profile '${name}'. Available: $(list_profiles | paste -sd ' ' -)"
    export CODENDUM_PROFILE="$name"
    load_env_file "$file" warn
}

# proxy_listen_ports FILE: ports of the active "listen" directives in an nginx file.
proxy_listen_ports() {
    sed -nE 's/^[[:space:]]*listen[[:space:]]+([^;[:space:]]*:)?([0-9]+)([[:space:];].*)?$/\2/p' "$1" | sort -un
}

# port_in_use PORT: true when something listens on TCP PORT (needs ss).
port_in_use() {
    have ss && ss -Hltn "sport = :$1" 2>/dev/null | grep -q .
}

# check_pinned_image VAR_NAME IMAGE: 0 when pinned by digest, 1 with a warning
# when pinned by tag only, 2 when unpinned ("latest" or no tag).
check_pinned_image() {
    local image="$2"
    if [[ "$image" == *@sha256:* ]]; then
        return 0
    elif [[ "$image" == *:latest || "${image##*/}" != *:* ]]; then
        log_error "$1 must be pinned to a version tag and preferably a digest, not '${image}'"
        return 2
    fi
    log_warn "$1 is pinned by tag only; add @sha256:<digest> for a reproducible install"
    return 1
}

# is_uint VALUE
is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

# uint_in_range VALUE MIN MAX: VALUE is an integer with MIN <= VALUE <= MAX.
uint_in_range() { is_uint "$1" && ((10#$1 >= $2 && 10#$1 <= $3)); }

# is_fraction VALUE: decimal strictly between 0 and 1 (for example 0.80)
is_fraction() { [[ "$1" =~ ^0?\.[0-9]*[1-9][0-9]*$ ]]; }

run_py() {
    require_cmd python3 "install python3 3.8 or newer"
    python3 "$CODENDUM_PY" "$@"
}
