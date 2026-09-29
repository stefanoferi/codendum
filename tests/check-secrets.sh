#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Secret hygiene: whitespace/conflict checks, .gitignore coverage, and a scan
# of every file that is (or would be) committed for literal credentials.
# Run it before every commit; Git history is public and permanent.

# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

mapfile -t files < <(git ls-files --cached --others --exclude-standard | sort -u)

echo "# git diff --check"
if git rev-parse --verify -q HEAD >/dev/null; then
    empty_tree="$(git hash-object -t tree /dev/null)"
    check "committed tree has no whitespace errors or conflict markers" git diff --check "$empty_tree" HEAD
fi
check "staged changes are clean" git diff --cached --check
check "working tree changes are clean" git diff --check
check "no trailing whitespace or CRLF in uncommitted files" python3 - "${files[@]}" <<'EOF'
import sys
bad = []
for path in sys.argv[1:]:
    try:
        data = open(path, "rb").read()
    except (FileNotFoundError, IsADirectoryError):
        continue
    if path == "LICENSE" or b"\0" in data:
        continue
    for number, line in enumerate(data.split(b"\n"), 1):
        if line.endswith((b" ", b"\t", b"\r")) or line.startswith((b"<<<<<<< ", b">>>>>>> ")):
            bad.append("%s:%d" % (path, number))
print("\n".join(bad[:20]))
sys.exit(1 if bad else 0)
EOF

echo "# .gitignore coverage"
for p in .env .env.local secrets/x keys/x api-keys.csv api-keys.map tls/privkey.pem site.key \
    bench-results/run/summary.csv hf-cache/x; do
    if git check-ignore -q --no-index "$p"; then ok "ignored: ${p}"; else not_ok "not ignored: ${p}"; fi
done
for p in .env.example config/nginx.example.conf config/opencode.example.json scripts/gen-api-keys.sh; do
    if git check-ignore -q --no-index "$p"; then not_ok "wrongly ignored: ${p}"; else ok "tracked: ${p}"; fi
done

echo "# forbidden files"
bad_names=()
for f in "${files[@]}"; do
    case "$f" in
        .env | */.env | *.pem | *.key | *.p12 | *.pfx | api-keys.* | */api-keys.* | *.secrets.env)
            bad_names+=("$f")
            ;;
    esac
done
if ((${#bad_names[@]} == 0)); then ok "no key, certificate or env files would be committed"; else not_ok "sensitive files would be committed" "${bad_names[*]}"; fi
check "no file larger than 1 MiB" python3 -c '
import os, sys
big = [p for p in sys.argv[1:] if os.path.isfile(p) and os.path.getsize(p) > 1 << 20]
print(big); sys.exit(1 if big else 0)' "${files[@]}"

echo "# literal credentials"
patterns=(
    '-----BEGIN [A-Z ]*PRIVATE KEY'
    '\bhf_[A-Za-z0-9]{30,}'
    '\bsk-[A-Za-z0-9_-]{20,}'
    '(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}'
    'github_pat_[A-Za-z0-9_]{40,}'
    'AKIA[0-9A-Z]{16}'
    'xox[abprs]-[A-Za-z0-9-]{10,}'
    'cdm_[0-9a-f]{64}'
    'Bearer [A-Za-z0-9._~+/=-]{24,}'
)
hits=""
for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    for re in "${patterns[@]}"; do
        if match="$(grep -nEI -- "$re" "$f" 2>/dev/null)"; then
            hits+="${f}: ${match%%$'\n'*}"$'\n'
        fi
    done
done
if [[ -z "$hits" ]]; then ok "no literal credentials found in ${#files[@]} files"; else not_ok "possible credentials found" "$hits"; fi

finish
