# Codendum

Codendum is shared, local infrastructure for coding agents. A single NVIDIA GB10
system (DGX Spark) runs [vLLM](https://docs.vllm.ai/) with a coding model, and
about 40 users run [OpenCode](https://opencode.ai/) on their own workstations
against it. It works the same for a classroom or a company team: prompts and
source code stay on the organization's network.

These pages explain how to install, secure, test, measure and operate the
service. They follow the order of a first installation.

## How it works

```text
 Workstations (x40)                      GB10 host (DGX OS, ARM64)
 ┌──────────────────────────┐            ┌───────────────────────────────────────────┐
 │ OpenCode                 │  HTTPS     │ nginx container :8443                     │
 │ Git, JDK, Maven/Gradle,  │───────────▶│  network allowlist, per-user API key,     │
 │ builds and tests         │  /v1/...   │  per-user limits, only 2 endpoints        │
 └──────────────────────────┘  LAN/VPN   │        │                                  │
                                         │        ▼ 127.0.0.1:8000                   │
                                         │ vLLM container (served model "coder")     │
                                         │ Qwen3-Coder-30B-A3B-Instruct-FP8          │
                                         └───────────────────────────────────────────┘
```

- **The GB10 only serves inference.** Users' code, Git, the JDK and
  Maven/Gradle builds run on the workstations or in dedicated isolated
  development environments. Do not run 40 Java builds on the model host: CPU and
  GPU share the same memory.
- **Every service runs in Docker.** vLLM listens on the loopback interface only,
  and an nginx container is the only way in. nginx accepts HTTPS on a dedicated
  port from the organization's LAN or VPN, checks a per-user API key and
  forwards only `/v1/chat/completions` and `/v1/models`. The scripts in this
  repository run on the host: they drive Docker and act as test clients, and
  they install nothing on the system.
- **OpenCode uses an explicit OpenAI-compatible provider.** The served model name
  is `coder`, tool calling is enabled explicitly, and the context limit matches
  the server profile.

| Path | Purpose |
| --- | --- |
| [`scripts/preflight.sh`](https://github.com/stefanoferi/codendum/blob/main/scripts/preflight.sh) | Read-only host checks (ARM64, Docker, GPU runtime, memory, disk, ports) |
| [`scripts/start-vllm.sh`](https://github.com/stefanoferi/codendum/blob/main/scripts/start-vllm.sh) | Starts the vLLM container with the selected profile |
| [`scripts/start-proxy.sh`](https://github.com/stefanoferi/codendum/blob/main/scripts/start-proxy.sh) | Starts, checks and reloads the nginx proxy container |
| [`scripts/smoke-test.sh`](https://github.com/stefanoferi/codendum/blob/main/scripts/smoke-test.sh) | Health, model list, chat, streaming and tool-call checks |
| [`scripts/metrics.sh`](https://github.com/stefanoferi/codendum/blob/main/scripts/metrics.sh) | KV cache, running/waiting requests, preemptions, host memory, GPU |
| [`scripts/bench.sh`](https://github.com/stefanoferi/codendum/blob/main/scripts/bench.sh) | Repeatable load test at 1, 8, 16, 24 and 40 concurrent requests |
| [`scripts/bench-classroom.sh`](https://github.com/stefanoferi/codendum/blob/main/scripts/bench-classroom.sh) | Simulated class: 40 users running OpenCode-like agent sessions |
| [`scripts/gen-api-keys.sh`](https://github.com/stefanoferi/codendum/blob/main/scripts/gen-api-keys.sh) | Generates per-user API keys for nginx, locally |
| [`config/profiles/`](https://github.com/stefanoferi/codendum/blob/main/config/profiles/) | Serving profiles (`classroom-64k`, `classroom-64k-high-concurrency`, `deep-128k`) |
| [`config/nginx.example.conf`](https://github.com/stefanoferi/codendum/blob/main/config/nginx.example.conf) | HTTPS reverse proxy |
| [`config/opencode.example.json`](https://github.com/stefanoferi/codendum/blob/main/config/opencode.example.json) | OpenCode V2 provider configuration |
| [`.env.example`](https://github.com/stefanoferi/codendum/blob/main/.env.example) | All server settings, no secrets |

## Contents

```{toctree}
:maxdepth: 2

sizing
installation
proxy
opencode
smoke-test
benchmark
profiles
troubleshooting
security
reference
```
