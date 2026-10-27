#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Codendum workstation setup for macOS and Linux (prototype). The panel fills
# in the @@...@@ values for one user and serves it once, for
#   curl -fsSk --pinnedpubkey 'sha256//...' https://HOST:8443/s/CODE | sh
#
# It needs OpenCode 2 and curl, and changes only:
# - ~/.config/codendum/ (certificate, API key, provider snippet);
# - the "codendum" provider and default model in the OpenCode configuration
#   (backup first);
# - NODE_EXTRA_CA_CERTS for the OpenCode service and the shell profile.

set -eu

BASE='@@BASE@@'
MODEL='@@MODEL@@'
DIR="$HOME/.config/codendum"

say() { printf '%s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

# shellcheck disable=SC2016  # JavaScript and Python programs, not shell
merge_js='ObjC.import("Foundation");
function readText(p) {
  var s = $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, null);
  return s.isNil() ? null : ObjC.unwrap(s);
}
function run(argv) {
  var text = readText(argv[0]), cfg = {};
  if (text !== null && text.trim() !== "") {
    try { cfg = JSON.parse(text); } catch (e) { return "not plain JSON (comments?)"; }
  }
  if (cfg === null || typeof cfg !== "object" || Array.isArray(cfg)) return "not a JSON object";
  if (cfg.provider && cfg.provider.codendum) return "uses the OpenCode 1.x format";
  var out = {"$schema": cfg["$schema"] || "https://opencode.ai/config.json"};
  for (var k in cfg) out[k] = cfg[k];
  out.providers = out.providers || {};
  out.providers.codendum = JSON.parse(readText(argv[1]));
  out.model = argv[2];
  if (!("share" in out)) out.share = "disabled";
  var ok = $(JSON.stringify(out, null, 2) + "\n").writeToFileAtomicallyEncodingError(argv[0], true, $.NSUTF8StringEncoding, null);
  return ok ? "OK" : "cannot be written";
}'

# shellcheck disable=SC2016
merge_py='import json, os, sys
path, snippet, model = sys.argv[1:4]
cfg = {}
if os.path.exists(path):
    text = open(path, encoding="utf-8").read()
    if text.strip():
        try:
            cfg = json.loads(text)
        except ValueError:
            sys.exit("not plain JSON (comments?)")
if not isinstance(cfg, dict):
    sys.exit("not a JSON object")
if "codendum" in (cfg.get("provider") or {}):
    sys.exit("uses the OpenCode 1.x format")
out = {"$schema": cfg.get("$schema") or "https://opencode.ai/config.json"}
out.update(cfg)
out.setdefault("providers", {})["codendum"] = json.load(open(snippet, encoding="utf-8"))
out["model"] = model
out.setdefault("share", "disabled")
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(out, indent=2) + "\n")
os.replace(tmp, path)
print("OK")'

# Add or replace the Codendum provider, keeping every other setting.
merge() {
    if [ "$(uname -s)" = Darwin ]; then
        result=$(osascript -l JavaScript -e "$merge_js" "$1" "$2" "$3" 2>&1 </dev/null) || true
    elif command -v python3 >/dev/null 2>&1; then
        result=$(python3 -I -c "$merge_py" "$1" "$2" "$3" 2>&1 </dev/null) || true
    elif [ ! -s "$1" ]; then
        # shellcheck disable=SC2016
        { printf '{\n  "$schema": "https://opencode.ai/config.json",\n  "model": "%s",\n' "$3"
          printf '  "share": "disabled",\n  "providers": {\n    "codendum": '
          cat "$2"
          printf '  }\n}\n'; } >"$1"
        result=OK
    else
        result="needs python3 to be merged"
    fi
    [ "$result" = OK ] || fail "$1 $result. Add the provider in $2 to it by hand, under \"providers\"."
}

main() {
    say "Codendum: setting up OpenCode for $BASE"
    command -v curl >/dev/null 2>&1 || fail "curl is required"
    command -v opencode >/dev/null 2>&1 ||
        fail "OpenCode is not installed. Run: curl -fsSL https://opencode.ai/v2/install | bash
       then open a new terminal and run the Codendum command again (create a new one on the panel)."
    version=$(opencode --version 2>/dev/null | sed -n 's/^[^0-9]*\([0-9][0-9]*\)\..*/\1/p' | head -n 1)
    [ "${version:-0}" -ge 2 ] || fail "OpenCode 2 or later is required, found: $(opencode --version 2>&1 | head -n 1)"
    confdir=$(opencode debug paths 2>/dev/null | sed -n 's/^config[[:space:]][[:space:]]*//p' | head -n 1)
    [ -n "$confdir" ] || confdir="$HOME/.config/opencode"

    umask 077
    mkdir -p "$DIR" "$confdir"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    cat >"$DIR/ca.pem" <<'CODENDUM_CA'
@@CA@@
CODENDUM_CA
    printf '%s' '@@KEY@@' >"$DIR/api-key"
    cat >"$DIR/provider.json" <<'CODENDUM_PROVIDER'
@@PROVIDER@@
CODENDUM_PROVIDER

    say "- checking the API key with a short request"
    printf 'Authorization: Bearer %s\n' "$(cat "$DIR/api-key")" >"$tmp/auth"
    printf '{"model":"%s","messages":[{"role":"user","content":"Reply with the single word: pong"}],"max_tokens":8}' \
        "$MODEL" >"$tmp/probe"
    code=$(curl -sS -o "$tmp/reply" -w '%{http_code}' --max-time 120 --cacert "$DIR/ca.pem" -H @"$tmp/auth" \
        -H 'Content-Type: application/json' --data-binary @"$tmp/probe" "$BASE/v1/chat/completions") ||
        fail "cannot reach $BASE with the Codendum certificate"
    case "$code" in
        200) ;;
        401) fail "the server rejected the API key (HTTP 401)" ;;
        502|503) fail "the model server is not ready yet (HTTP $code); try again in a minute" ;;
        *) fail "the test request returned HTTP $code" ;;
    esac

    file="$confdir/opencode.json"
    if [ ! -f "$file" ] && [ -f "$confdir/opencode.jsonc" ]; then
        file="$confdir/opencode.jsonc"
    fi
    if [ -f "$file" ]; then
        cp -p "$file" "$file.bak-$(date +%Y%m%d%H%M%S)"
        say "- backup of $file"
    fi
    merge "$file" "$DIR/provider.json" "codendum/$MODEL"
    say "- OpenCode configured: $file (model codendum/$MODEL)"

    ca="$DIR/ca.pem"
    if [ -n "${NODE_EXTRA_CA_CERTS:-}" ] && [ "$NODE_EXTRA_CA_CERTS" != "$ca" ]; then
        say "- warning: NODE_EXTRA_CA_CERTS is already set to $NODE_EXTRA_CA_CERTS."
        say "  OpenCode must also trust $ca: put both certificates in one file."
    else
        opencode service set env NODE_EXTRA_CA_CERTS "$ca" >/dev/null 2>&1 </dev/null ||
            say "- warning: could not set NODE_EXTRA_CA_CERTS for the OpenCode service"
        opencode service restart >/dev/null 2>&1 </dev/null || true
        case "${SHELL:-}" in
            */zsh) rc="$HOME/.zshrc" ;;
            */bash) if [ "$(uname -s)" = Darwin ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
            *) rc="$HOME/.profile" ;;
        esac
        if ! grep -qs 'codendum/ca.pem' "$rc"; then
            # shellcheck disable=SC2016  # $HOME is expanded when the profile runs
            printf '\n# Codendum: OpenCode trusts the Codendum certificate\nexport NODE_EXTRA_CA_CERTS="$HOME/.config/codendum/ca.pem"\n' >>"$rc"
            say "- NODE_EXTRA_CA_CERTS added to $rc"
        fi
        say "- OpenCode trusts the Codendum certificate"
    fi

    say ""
    say "Done. Open a new terminal, go to your project folder and run: opencode"
    if [ "$(uname -s)" = Darwin ]; then
        say "macOS 15 or later: if OpenCode reports EHOSTUNREACH, allow OpenCode (and your terminal app) in"
        say "System Settings > Privacy & Security > Local Network, then restart it."
    fi
}

main "$@" </dev/null
