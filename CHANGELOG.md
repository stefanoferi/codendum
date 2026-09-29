# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
Dates use ISO 8601 (YYYY-MM-DD).

## [Unreleased]

### Changed

- The documentation describes the service for concurrent users in general;
  capacity figures come only from the benchmarks.
- `scripts/gen-api-keys.sh` needs `--users-file FILE` or `--count N`; there is
  no default number of users any more.

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

[Unreleased]: https://github.com/stefanoferi/codendum/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/stefanoferi/codendum/releases/tag/v0.1.0
