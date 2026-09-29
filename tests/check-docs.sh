#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Documentation checks: required files, license text, table of contents,
# local links, unresolved placeholders, and a strict Sphinx build of docs/.

# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

APACHE_SHA256="cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

echo "# required files"
for f in README.md LICENSE NOTICE CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md CLAUDE.md \
    .env.example .gitignore docs/index.md docs/conf.py docs/requirements.in docs/requirements.txt \
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

echo "# table of contents"
check "every page in docs/ is listed in the index toctree" python3 - <<'EOF'
import glob, os, re, sys
index = open("docs/index.md", encoding="utf-8").read()
block = re.search(r"```\{toctree\}\n(.*?)```", index, re.S)
if not block:
    sys.exit("docs/index.md has no toctree")
listed = {line.strip() for line in block.group(1).splitlines() if line.strip() and not line.startswith(":")}
pages = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob("docs/*.md")} - {"index"}
missing, unknown = pages - listed, listed - pages
if missing or unknown:
    sys.exit("not in toctree: %s; listed but missing: %s" % (sorted(missing), sorted(unknown)))
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

echo "# sphinx build"
sphinx="${SPHINX_BUILD:-$(command -v sphinx-build || true)}"
[[ -z "$sphinx" && -x .venv/bin/sphinx-build ]] && sphinx=.venv/bin/sphinx-build
if [[ -n "$sphinx" ]]; then
    check "docs build without warnings ($("$sphinx" --version))" "$sphinx" -W --keep-going -q -b html docs "${TMP_ROOT}/html"
elif [[ "${CI:-}" == true ]]; then
    not_ok "sphinx-build is required in CI (pip install --require-hashes -r docs/requirements.txt)"
else
    echo "SKIP: sphinx-build not installed (pip install --require-hashes -r docs/requirements.txt)"
fi

finish
