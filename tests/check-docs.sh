#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Documentation checks: required files, license text, EN <-> IT links,
# section order, identical commands in both languages, local links, and
# unresolved placeholders. Translation drift is reported as a warning.

# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

APACHE_SHA256="cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

echo "# required files"
for f in README.md docs/it/README.md docs/TRANSLATING.md docs/MAINTAINING.md LICENSE NOTICE \
    CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md CLAUDE.md .env.example .gitignore \
    config/opencode.example.json config/nginx.example.conf .github/workflows/ci.yml \
    .github/pull_request_template.md .github/dependabot.yml scripts/preflight.sh scripts/start-vllm.sh \
    scripts/start-proxy.sh scripts/smoke-test.sh scripts/metrics.sh scripts/bench.sh \
    scripts/bench-classroom.sh; do
    if [[ -s "$f" ]]; then ok "present: ${f}"; else not_ok "missing or empty: ${f}"; fi
done

echo "# license"
sum="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' LICENSE)"
if [[ "$sum" == "$APACHE_SHA256" ]]; then ok "LICENSE is the unmodified Apache-2.0 text"; else not_ok "LICENSE differs from the Apache-2.0 text (sha256 ${sum})"; fi
check "NOTICE names the copyright holder" grep -q 'Copyright 2026 Stefano Noferi' NOTICE

echo "# language links"
check "README links to the Italian translation near the top" bash -c "head -n 12 README.md | grep -q '(docs/it/README.md)'"
check "Italian README links back to English near the top" bash -c "head -n 12 docs/it/README.md | grep -q '(../../README.md)'"

echo "# sections and commands (README.md vs docs/it/README.md)"
check "same section markers, required order, same commands" python3 - <<'EOF'
import re, sys
REQUIRED = ["prerequisites", "configuration", "launch", "proxy", "opencode", "smoke-test",
            "benchmark", "profiles", "troubleshooting", "limits"]

def sections(text):
    return re.findall(r"<!-- section: ([a-z0-9-]+) -->", text)

def commands(text):
    out = []
    for block in re.findall(r"```(?:bash|sh|console)\n(.*?)```", text, re.S):
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out

en = open("README.md", encoding="utf-8").read()
it = open("docs/it/README.md", encoding="utf-8").read()
errors = []
if sections(en) != sections(it):
    errors.append("section markers differ:\n EN %s\n IT %s" % (sections(en), sections(it)))
order = [s for s in sections(en) if s in REQUIRED]
if order != REQUIRED:
    errors.append("required sections missing or out of order: %s" % order)
ce, ci = commands(en), commands(it)
if ce != ci:
    for i, (a, b) in enumerate(zip(ce, ci)):
        if a != b:
            errors.append("first differing command #%d:\n EN %s\n IT %s" % (i, a, b))
            break
    else:
        errors.append("command count differs: EN %d, IT %d" % (len(ce), len(ci)))
if errors:
    print("\n".join(errors))
    sys.exit(1)
EOF

echo "# local links"
check "relative links resolve" python3 - <<'EOF'
import os, re, subprocess, sys
files = [f for f in subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
                                   capture_output=True, text=True, check=True).stdout.split() if os.path.exists(f)]
bad = []
for path in files:
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"^(https?:|mailto:|#)", target):
            continue
        target = target.split("#", 1)[0]
        if not os.path.exists(os.path.normpath(os.path.join(os.path.dirname(path), target))):
            bad.append("%s -> %s" % (path, target))
if bad:
    print("\n".join(bad))
    sys.exit(1)
EOF

echo "# placeholders"
check "no unresolved placeholders in public docs" python3 - <<'EOF'
import re, subprocess, sys
files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md", ".github/*"],
                       capture_output=True, text=True, check=True).stdout.split()
pattern = re.compile(r"\b(TODO|TBD|FIXME|XXX)\b|\[NOTE:|\[yyyy\]|lorem ipsum|<insert", re.I)
hits = []
for path in files:
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except (FileNotFoundError, IsADirectoryError):
        continue
    for number, line in enumerate(lines, 1):
        if pattern.search(line):
            hits.append("%s:%d: %s" % (path, number, line.strip()))
if hits:
    print("\n".join(hits))
    sys.exit(1)
EOF

echo "# translation freshness"
marker="$(grep -o 'translation-source: README.md sha256=[0-9a-f]*' docs/it/README.md || true)"
current="$(python3 -c 'import hashlib; print(hashlib.sha256(open("README.md","rb").read()).hexdigest())')"
if [[ -z "$marker" ]]; then
    not_ok "docs/it/README.md lacks the translation-source marker (see docs/TRANSLATING.md)"
elif [[ "${marker##*=}" == "$current" ]]; then
    ok "Italian translation is synced with the current README.md"
else
    msg="docs/it/README.md was synced with an older README.md; update it or mark it outdated (docs/TRANSLATING.md)"
    if [[ "${CI:-}" == true ]]; then echo "::warning file=docs/it/README.md::${msg}"; else echo "warning: ${msg}"; fi
fi

finish
