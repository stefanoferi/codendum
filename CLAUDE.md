# CLAUDE.md

Guidance for AI coding agents working in this repository. Human contributors
should read [CONTRIBUTING.md](CONTRIBUTING.md); the rules below are the same,
condensed.

## Project

Codendum provides scripts, configuration examples and documentation for running
vLLM on one NVIDIA GB10 (DGX Spark: ARM64, 128 GB unified CPU/GPU memory, DGX
OS). The GB10 serves a coding model to concurrent OpenCode users on their own
workstations. The GB10 does inference only; users' builds, tests and Git run on
the workstations.

- Model: `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8`, served as `coder`.
- Image: `vllm/vllm-openai`, pinned by digest in `.env.example`.
- Every service runs in Docker. Only the nginx container is exposed: port 8443,
  configured by `config/nginx.example.conf` and started by
  `scripts/start-proxy.sh`. vLLM listens on `127.0.0.1:8000`. Scripts run on the
  host as Docker drivers and test clients, and install nothing.

## Layout

| Path | Contents |
| --- | --- |
| `scripts/*.sh` | Operator scripts: preflight, start-vllm, start-proxy, smoke-test, metrics, bench, gen-api-keys |
| `scripts/lib/common.sh` | Shared shell helpers: safe `.env` parsing, profiles, validation |
| `scripts/lib/codendum.py` | Stdlib-only client: smoke, metrics, bench, gen-keys |
| `config/profiles/*.env` | Serving profiles |
| `config/*.example.*` | nginx and OpenCode examples (placeholders only) |
| `tests/` | Offline suites; `mock_vllm.py` imitates the vLLM API |
| `docs/` | Documentation sources (Markdown, Sphinx + MyST); `docs/requirements.txt` pins the toolchain |

## Commands

```bash
tests/run.sh               # everything (Linux, bash >= 4.4, python3, curl, openssl, git, shellcheck, yamllint, docker, nginx)
tests/lint.sh              # bash -n, shellcheck, py_compile, JSON, YAML, SPDX headers
tests/test-scripts.sh      # scripts against the mock server and a fake docker
tests/test-proxy.sh docker # proxy container via start-proxy.sh, in front of the mock
tests/test-proxy.sh native # same config with the distribution nginx, unprivileged
tests/check-docs.sh        # links, placeholders, license text, strict Sphinx build
tests/check-secrets.sh     # run before every commit
```

On macOS, run the suites in a Linux container (see CONTRIBUTING.md).

## Rules

- **English** for code, comments, messages and docs. Keep `README.md` short;
  details belong in `docs/`. Add new pages to the toctree in `docs/index.md`,
  and link to repository files with full GitHub URLs so the built site works.
- **No secrets, ever.** No keys, tokens, certificates or real host names. Use
  placeholders (`llm.lab.example`, `192.0.2.0/24`, `198.51.100.0/24`). History is
  public; run `tests/check-secrets.sh` before committing.
- **Do not create, move, push or delete Git tags or releases,** and do not push
  unless explicitly asked. Releases are the maintainer's decision.
- **Verify instead of assuming.** Never invent versions, digests, commit SHAs,
  CLI flags, config keys or benchmark numbers:
  - check vLLM flags against the source or docs of the pinned release;
  - check OpenCode keys against the current V2 docs;
  - check action SHAs with `git ls-remote`;
  - say explicitly when something could not be verified.
- **Be honest about hardware.** Nothing is validated on a GB10 unless someone
  ran it there. Label estimates as estimates.
- **Shell:** bash ≥ 4.4, `set -Eeuo pipefail`, ShellCheck-clean, `--help` on
  every script. Safe defaults:
  - loopback only;
  - no silent overwrite or delete;
  - no secrets in arguments or output;
  - `.env` values parsed, never evaluated.
- **Python:** standard library only, Python ≥ 3.8.
- **Headers:** add an SPDX header (`Apache-2.0`) and a copyright line to new
  source files.
- **Mock:** when a client starts relying on a new vLLM behaviour, keep
  `tests/mock_vllm.py` consistent with the real API, and add a test.
- **Public interface:** script options, `CODENDUM_*` variables, profile names
  and config layout follow SemVer. Record changes in `CHANGELOG.md`.
- **Platform facts:** the GB10 is ARM64. Do not suggest reinstalling NVIDIA
  drivers on DGX OS, publishing port 8000, or running builds on the model host.
