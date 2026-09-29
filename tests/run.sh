#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Run every offline check. Requires Linux with bash >= 4.4, python3, curl,
# openssl, git; ShellCheck, yamllint, Docker and nginx for the full suite.

set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

status=0
for suite in "lint.sh" "test-scripts.sh" "test-proxy.sh docker" "test-proxy.sh native" "check-docs.sh" "check-secrets.sh"; do
    printf '\n=== %s ===\n' "$suite"
    read -r -a cmd <<<"$suite"
    "tests/${cmd[0]}" "${cmd[@]:1}" || status=1
done
if ((status == 0)); then echo $'\nAll suites passed.'; else echo $'\nSome suites failed.'; fi
exit "$status"
