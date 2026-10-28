# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
Dates use ISO 8601 (YYYY-MM-DD).

## [Unreleased]

### Changed

- `README.md` and the documentation home show the architecture as a diagram
  (`docs/_static/architecture.svg`), including the web panel and Open WebUI;
  `README.md` and `docs/web.md` show screenshots of the web panel, taken from
  the test setup.
- Web panel dashboard: the card header says once that rates average the last
  minute, instead of a suffix on every tile.

### Security

- `docs/requirements.txt`: certifi 2026.10.18 (was 2026.7.22), which removes
  the emSign root CAs whose intermediate CA keys were compromised
  ([GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)).
  Only the documentation toolchain uses it.

## [0.2.0] - 2026-10-28

### Added

- Web panel, as a **preview** (`web/`, documented in `docs/web.md`): its
  options, routes and pages may still change in a minor release.
  - People register with their e-mail address and wait for approval, unless
    their domain is approved automatically. The first administrator is
    created with a one-time link printed in the panel log.
  - Each approved person gets a personal page with a three-step OpenCode
    guide, the API key and a one-time setup command for macOS, Linux and
    Windows, pinned to the proxy's public key. The Windows script has not
    been tested on a real Windows workstation yet.
  - Administrators approve, block, delete and promote people, renew keys and
    reset passwords; close registration, limit it to e-mail domains and show
    a notice; follow vLLM, the GPU and the users on a live dashboard.
  - Optional single login for Open WebUI through nginx `auth_request`, with
    the panel's administrators as chat administrators, and a switch for web
    search in the chat.
  - CSRF tokens on every form, rate-limited logins, password change from the
    profile menu.
  - Keys go to a separate `api-keys-web.map`, so keys from
    `scripts/gen-api-keys.sh` keep working. The panel mounts the Docker socket
    to reload the proxy and read the vLLM container; this is the main point to
    revisit before it leaves the preview.
  - `CODENDUM_WEB_*` variables configure it; see `docs/web.md`.
- `web/patch-nginx.py` adapts an existing proxy configuration for the panel.
- `tests/test-web.sh` tests the panel end to end in Docker, with
  `tests/mock_openwebui.py`, in CI and in `tests/run.sh`; `tests/mock_vllm.py`
  also exposes the latency histograms the dashboard reads.

### Changed

- `config/nginx.example.conf`: plain HTTP sent to the TLS port is redirected to
  HTTPS (301) instead of getting nginx's 400 error page.

### Fixed

- `scripts/start-proxy.sh --reload` reloads nginx with `nginx -s reload` inside
  the container instead of `docker kill --signal HUP`. Docker counted the kill
  as a manual stop, so with `--restart unless-stopped` the proxy did not start
  again after a reboot.

## [0.1.1] - 2026-10-01

### Changed

- The documentation describes the service for concurrent users in general;
  capacity figures come only from the benchmarks.
- `scripts/gen-api-keys.sh` needs `--users-file FILE` or `--count N`; there is
  no default number of users any more.
- The documentation is neutral about the language and platform users work
  with. The OpenCode check uses a Python example.

### Added

- `scripts/configure-opencode.sh` (and `codendum.py opencode-config` for
  Windows): configures OpenCode on a workstation from the server's served model
  and maximum context length. It runs a test completion, keeps the other
  settings and backs up the previous file.
- Project logo in the README and in the documentation site, with a favicon.
- Governance page in the documentation, with a possible future integration
  with [Admina](https://admina.org/) for personal-data redaction, a
  prompt-injection firewall, agent loop breaking and audit logging.
- `scripts/bench-classroom.sh --scenario python`: a Python project with pytest,
  next to the default Java scenario.

## [0.1.0] - 2026-09-29

First public version. Tested offline in CI. Also run on one GB10 (Lenovo
ThinkStation PGX, DGX OS 7.2.3) on 2026-09-29, with:

- startup and smoke tests, locally and through the proxy;
- a real OpenCode 2.0.19 session;
- 40-user classroom simulations on both 64K profiles.

The results are in `docs/benchmark.md`.

### Added

- `scripts/preflight.sh`: read-only host checks (ARM64, Docker, NVIDIA runtime,
  `nvidia-smi`, memory, disk space, port exposure, image pinning).
- `scripts/start-vllm.sh`: starts vLLM in Docker with the selected profile. It:
  - binds to loopback only;
  - refuses unpinned images;
  - never replaces an existing container without `--replace`;
  - prints the effective command without secrets;
  - has `--dry-run` and `--wait` options.
- Serving profiles `classroom-64k`, `classroom-64k-high-concurrency` and
  `deep-128k` for `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8`, served as `coder`,
  with an FP8 KV cache, chunked prefill, prefix caching and the `qwen3_coder`
  tool parser.
- `scripts/smoke-test.sh`: checks health, the model list, chat, streaming, tool
  calls with `auto` and `required`, and a tool-result round trip. With
  `--proxy` it also checks endpoint exposure and authentication. Distinct exit
  status for inconclusive results.
- `scripts/metrics.sh`: KV cache usage and capacity, running and waiting
  requests, preemptions, prefix-cache hit rate, host memory and GPU state.
  Includes a watch mode.
- `scripts/bench.sh`: closed-loop benchmark at 1, 8, 16, 24 and 40 concurrent
  requests with short and long prompts. Reports TTFT P50/P95/P99, throughput,
  errors, preemptions and peak KV cache usage, and refuses to run on a busy
  server.
- `scripts/bench-classroom.sh`: simulated class of users running
  OpenCode-like agent sessions against the real model. The simulation has:
  - tool calls on an in-memory Java project;
  - growing context;
  - pauses and a ramp-up;
  - per-user API keys.

  It reports TTFT, per-user output speed, model time per request, retries and
  server peaks.
- `scripts/metrics.sh --watch N --json` writes one JSON object per line, for
  timelines.
- `scripts/gen-api-keys.sh`: local per-user API key generation for nginx.
- `scripts/start-proxy.sh`: runs the nginx proxy in a hardened container. It:
  - uses host networking;
  - has a read-only root file system and minimal capabilities;
  - checks file permissions;
  - tests the configuration before starting or replacing;
  - supports `--init`, `--reload` and `--dry-run`.
- `config/nginx.example.conf`: HTTPS proxy on the dedicated port 8443 with:
  - a network allowlist and per-user API keys;
  - only `/v1/chat/completions` and `/v1/models` forwarded, with
    non-normalized request paths rejected;
  - unbuffered SSE;
  - per-user and global limits;
  - JSON error responses.
- `config/opencode.example.json` (OpenCode V2) and
  `config/opencode.v1.example.json` (OpenCode 1.x).
- Documentation in `docs/`, built with Sphinx and MyST: sizing, installation,
  proxy, OpenCode, testing, benchmarks with measured results, profiles,
  troubleshooting, threat model and component inventory.
- Offline CI on Ubuntu 24.04 (x64 and ARM64): lint, script tests against a mock
  vLLM server, proxy tests with the nginx container and with the distribution
  package, documentation checks and secret hygiene. Minimal permissions, and
  actions pinned by commit SHA with Dependabot updates.

[Unreleased]: https://github.com/stefanoferi/codendum/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/stefanoferi/codendum/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/stefanoferi/codendum/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/stefanoferi/codendum/releases/tag/v0.1.0
