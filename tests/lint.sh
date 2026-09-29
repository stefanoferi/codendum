#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Static checks: shell syntax, ShellCheck, Python compilation, JSON syntax,
# license headers and executable bits.

# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

mapfile -t shell_files < <(find scripts tests -type f -name '*.sh' | sort)
mapfile -t python_files < <(find scripts tests -type f -name '*.py' | sort)
mapfile -t json_files < <(find config .github -type f -name '*.json' 2>/dev/null | sort)

echo "# bash -n"
for f in "${shell_files[@]}"; do check "syntax: ${f}" bash -n "$f"; done

echo "# shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
    check "shellcheck ($(shellcheck --version | sed -n 's/^version: //p'))" shellcheck -x "${shell_files[@]}"
elif [[ "${CI:-}" == true ]]; then
    not_ok "shellcheck is required in CI"
else
    echo "SKIP: shellcheck not installed"
fi

echo "# python"
check "py_compile" python3 -m py_compile "${python_files[@]}"
find scripts tests -name '__pycache__' -type d -prune -exec rm -rf {} +

echo "# json"
for f in "${json_files[@]}"; do check "valid JSON: ${f}" python3 -m json.tool "$f"; done

echo "# yaml"
if command -v yamllint >/dev/null 2>&1; then
    check "yamllint .github" yamllint -s -d '{extends: relaxed, rules: {line-length: {max: 160}}}' .github
elif [[ "${CI:-}" == true ]]; then
    not_ok "yamllint is required in CI"
else
    echo "SKIP: yamllint not installed"
fi

echo "# license headers and modes"
for f in "${shell_files[@]}" "${python_files[@]}" config/nginx.example.conf .github/workflows/*.yml; do
    if grep -q 'SPDX-License-Identifier: Apache-2.0' "$f"; then ok "SPDX header: ${f}"; else not_ok "SPDX header missing: ${f}"; fi
done
for f in scripts/*.sh tests/*.sh; do
    [[ "$f" == tests/lib.sh ]] && continue
    if [[ -x "$f" ]]; then ok "executable: ${f}"; else not_ok "not executable: ${f} (chmod +x)"; fi
done

finish
