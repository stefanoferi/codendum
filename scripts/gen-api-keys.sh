#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Generate one API key per user for the nginx proxy. Keys are created
# locally, written with mode 0600, and must never be committed.

set -Eeuo pipefail
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: scripts/gen-api-keys.sh --out-dir DIR [--count N] [--prefix NAME | --users-file FILE]

Writes two files into DIR (created with mode 0700 if missing):
  api-keys.map   nginx map entries ("Bearer <key>" -> user id)
  api-keys.csv   user_id,api_key for distributing keys to users

Existing files are never overwritten: rotate keys by generating into a new
directory and switching the nginx include. DIR must be outside the Git
working tree (or ignored by Git).

Options:
      --out-dir DIR      output directory (required)
      --count N          number of users (default: 40)
      --prefix NAME      user id prefix, ids become NAME01..NAMEnn (default: user)
      --users-file FILE  one user id per line instead of --count/--prefix
  -h, --help             show this help
EOF
}

out_dir="" args=()
while (($#)); do
    case "$1" in
        --out-dir) out_dir="${2:?--out-dir needs a value}"; shift 2 ;;
        --count | --prefix | --users-file) args+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" 64 ;;
    esac
done
[[ -n "$out_dir" ]] || { usage >&2; die "--out-dir is required" 64; }

umask 077
mkdir -p "$out_dir"

# Refuse to write keys where Git could pick them up.
if have git && git -C "$out_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 &&
    ! git -C "$out_dir" check-ignore -q api-keys.csv; then
    die "${out_dir} is inside a Git working tree and not ignored; use a directory such as /etc/codendum/keys-$(date -u +%Y%m%d)"
fi

run_py gen-keys --out-dir "$out_dir" "${args[@]}"
