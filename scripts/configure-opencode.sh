#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Configure OpenCode on a workstation (Linux or macOS) for a Codendum server.
# On Windows run: py scripts\lib\codendum.py opencode-config --base-url URL

set -Eeuo pipefail

if [[ "${1:-}" == -h || "${1:-}" == --help ]]; then
    cat <<'USAGE'
Usage: scripts/configure-opencode.sh --base-url URL [options]

Reads the served model and its context length from the server through the
proxy, checks a test completion, and adds the Codendum provider to the
OpenCode configuration (default ~/.config/opencode/opencode.json). Other
settings are kept and the previous file is backed up. Run it again whenever
the server profile changes.

The API key is read from CODENDUM_API_KEY, or asked for without echoing.
It is never written to the configuration file.

Options:
      --base-url URL    proxy URL, for example https://llm.lab.example:8443 (required)
      --cacert FILE     CA bundle for a private TLS certificate
      --format v2|v1    OpenCode configuration format (default: v2)
      --model ID        served model id (default: the only one served)
      --max-output N    limit.output (default: 8192)
      --output FILE     configuration file to update
      --no-default      do not make this model OpenCode's default
      --dry-run         print the resulting configuration only
  -h, --help            show this help
USAGE
    exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "error: python3 is required" >&2; exit 1; }
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/codendum.py" opencode-config "$@"
