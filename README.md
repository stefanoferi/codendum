# Codendum

**English** · [Italiano](docs/it/README.md)

Codendum is shared, local infrastructure for coding agents. A single NVIDIA GB10
system (DGX Spark) runs [vLLM](https://docs.vllm.ai/) with a coding model, and
about 40 users run [OpenCode](https://opencode.ai/) on their own workstations
against it. It works the same for a classroom or a company team: prompts and
source code stay on the organization's network.

This repository contains the scripts, example configuration and documentation
to set it up, check it, measure it and operate it.

> **Project status: 0.1.0, unreleased.** Every script and configuration file is
> tested offline in CI (see [Development](#development)). The service was also
> run on one GB10 with 40 simulated OpenCode users (see
> [Measured on a GB10](#measured-on-a-gb10)). Treat those figures as a
> reference for this model and client version, not as a promise of
> performance.

## Contents

- [How it works](#how-it-works)
- [Sizing: memory, context and concurrency](#sizing-memory-context-and-concurrency)
- [1. Prerequisites](#1-prerequisites)
- [2. Configuration](#2-configuration)
- [3. Launch vLLM](#3-launch-vllm)
- [4. HTTPS proxy](#4-https-proxy)
- [5. OpenCode on the workstations](#5-opencode-on-the-workstations)
- [6. Smoke test](#6-smoke-test)
- [7. Benchmark](#7-benchmark)
- [8. Profiles](#8-profiles)
- [9. Troubleshooting](#9-troubleshooting)
- [10. Limits and responsibilities](#10-limits-and-responsibilities)
- [Security model](#security-model)
- [Components, versions and licenses](#components-versions-and-licenses)
- [Versioning and compatibility](#versioning-and-compatibility)
- [Development](#development)
- [References](#references)

<!-- section: overview -->
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
| [`scripts/preflight.sh`](scripts/preflight.sh) | Read-only host checks (ARM64, Docker, GPU runtime, memory, disk, ports) |
| [`scripts/start-vllm.sh`](scripts/start-vllm.sh) | Starts the vLLM container with the selected profile |
| [`scripts/start-proxy.sh`](scripts/start-proxy.sh) | Starts, checks and reloads the nginx proxy container |
| [`scripts/smoke-test.sh`](scripts/smoke-test.sh) | Health, model list, chat, streaming and tool-call checks |
| [`scripts/metrics.sh`](scripts/metrics.sh) | KV cache, running/waiting requests, preemptions, host memory, GPU |
| [`scripts/bench.sh`](scripts/bench.sh) | Repeatable load test at 1, 8, 16, 24 and 40 concurrent requests |
| [`scripts/bench-classroom.sh`](scripts/bench-classroom.sh) | Simulated class: 40 users running OpenCode-like agent sessions |
| [`scripts/gen-api-keys.sh`](scripts/gen-api-keys.sh) | Generates per-user API keys for nginx, locally |
| [`config/profiles/`](config/profiles/) | Serving profiles (`classroom-64k`, `classroom-64k-high-concurrency`, `deep-128k`) |
| [`config/nginx.example.conf`](config/nginx.example.conf) | HTTPS reverse proxy |
| [`config/opencode.example.json`](config/opencode.example.json) | OpenCode V2 provider configuration |
| [`.env.example`](.env.example) | All server settings, no secrets |

<!-- section: sizing -->
## Sizing: memory, context and concurrency

**Memory.** The GB10 has **128 GB of unified memory shared by CPU and GPU**. This
is not 128 GB of VRAM added to system RAM: the operating system, Docker, nginx,
the vLLM processes, the model weights and the KV cache all share it.

**Weights.** The FP8 checkpoint is about 31.2 GB (29.05 GiB) on disk and uses
about the same in memory.

**KV cache per token.** The model has 48 layers, 4 KV heads and a head size of
128. With an FP8 KV cache (1 byte per value), each token held in context costs:

```text
2 (K and V) × 48 layers × 4 KV heads × 128 × 1 byte = 49,152 bytes = 48 KiB per token
```

| Active context | KV cache (theoretical) |
| --- | ---: |
| 1 request × 65,536 tokens | 3 GiB |
| 16 requests × 65,536 tokens | 48 GiB |
| 24 requests × 65,536 tokens | 72 GiB |
| 40 requests × 65,536 tokens | 120 GiB |
| 8 requests × 131,072 tokens | 48 GiB |

This is a lower bound before weights and runtime overhead. Block allocation,
prefix-cache sharing, CUDA graphs, activations, reserves and the real lengths
of the requests all change the measured usage. As a rough example: if about
120 GiB are visible to CUDA, `--gpu-memory-utilization 0.80` gives vLLM about
96 GiB. That leaves about 60 GiB for the KV cache after weights and overhead,
roughly 1.3 million tokens or about 20 full 64K contexts.

At startup vLLM logs the real figures: a `KV cache size: … tokens` line and a
`Maximum concurrency for … tokens per request` line. `scripts/metrics.sh` shows
the same capacity. On the GB10 used for the [measurements](#measured-on-a-gb10),
CUDA saw 121.6 GiB, and the default profile got 62.4 GiB of KV cache: 1,362,640
tokens, or 20.8 full 64K contexts. Always check the figures on your own host.

**Connected, active and waiting are different numbers.**

- *Connected users* (40) have OpenCode open. Most of the time they are reading,
  typing or running tests, and they send nothing.
- *Active requests* are being processed. `--max-num-seqs` caps how many run at
  the same time (16 in the default profile).
- *Waiting requests* are queued inside vLLM, first come first served. They wait
  without limit unless you set `CODENDUM_MAX_NUM_QUEUED_REQS`.

`--max-num-seqs` is a cap. It guarantees neither throughput nor latency. When
the KV cache runs out, vLLM *preempts* a running request: it frees its memory
and computes the request again later. That shows up as a latency spike and as
`preemptions` in the metrics.

**The context limit is a ceiling, not a reservation.** Raising
`max-model-len` does not allocate memory for every user. A request that really
is long still costs KV memory and prefill time, and a 60K-token prompt with no
prefix-cache hits takes seconds to tens of seconds before the first token.

**Work in small steps.** A professional client-server Java project is built
module by module, with compilation, tests and human review after each step. It
is not produced by one huge generation. Short, focused requests are also what
keeps a shared service responsive for everyone.

<!-- section: prerequisites -->
## 1. Prerequisites

**GB10 host**

- NVIDIA DGX Spark or another GB10 system with DGX OS (Ubuntu 24.04 based),
  **ARM64/AArch64**.
- Docker and the NVIDIA Container Toolkit. DGX OS ships them already
  configured. **Do not reinstall the NVIDIA driver on a configured DGX Spark.**
- `python3` (3.8 or newer), `curl` and `git`, all present on DGX OS. nginx runs
  in a container and does not need to be installed on the host.
- Disk space: about 30 GiB for the model cache and 20–30 GiB for the container
  image. Keep at least 60 GiB free.
- A DNS name for the service (placeholder: `llm.lab.example`) and a TLS
  certificate that the workstations trust. Use a public CA with DNS-01
  validation, or the organization's internal CA.
- Firewall rules that allow the proxy port (TCP 8443 by default) only from the
  classroom or office LAN and the VPN.

**Workstations**

- OpenCode V2. OpenCode 1.x also works with the V1 example config.
- The development tools for the course or project: Git, JDK, Maven or Gradle,
  and so on.

Get the repository onto the GB10 host and run the read-only checks:

```bash
git clone https://github.com/stefanoferi/codendum.git
cd codendum
scripts/preflight.sh
```

`preflight.sh` changes nothing. It checks the architecture, Docker and the
NVIDIA runtime, `nvidia-smi`, memory, disk space and port 8000, and prints a
hint for every warning or failure. Add `--gpu-test` to also run `nvidia-smi`
inside the vLLM image once the image has been pulled.

<!-- section: configuration -->
## 2. Configuration

All server settings live in `.env`, which Git ignores. Profiles provide the
serving parameters. Values exported in the shell override `.env`, and `.env`
overrides the profile.

```bash
cp .env.example .env
sudo install -d -m 0700 -o "$USER" /etc/codendum
sudo install -d -m 0755 -o "$USER" /var/lib/codendum/huggingface
```

If the service account has no `sudo` rights, use directories it owns instead,
for example `/home/codendum/codendum-data/huggingface` for
`CODENDUM_HF_CACHE_DIR` and `/home/codendum/codendum-data/proxy` for
`CODENDUM_PROXY_DIR`. Values in `.env` are not expanded, so write absolute paths.

Review `.env`. The main settings are:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODENDUM_PROFILE` | `classroom-64k` | Serving profile (see [Profiles](#8-profiles)) |
| `CODENDUM_VLLM_IMAGE` | `vllm/vllm-openai:v0.30.0@sha256:8a69…` | Container image, pinned by tag and digest |
| `CODENDUM_MODEL` / `CODENDUM_MODEL_REVISION` | `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` @ `dcaee4d4…` | Checkpoint and exact revision |
| `CODENDUM_SERVED_MODEL_NAME` | `coder` | Model name used by clients |
| `CODENDUM_HOST` / `CODENDUM_PORT` | `127.0.0.1` / `8000` | API address (loopback only; enforced) |
| `CODENDUM_HF_CACHE_DIR` | `/var/lib/codendum/huggingface` | Persistent model cache |
| `CODENDUM_SECRETS_ENV_FILE` | empty | Optional file with secrets for the container |
| `CODENDUM_HF_OFFLINE` | `0` | Set to `1` after the first download |
| `CODENDUM_TOOL_CALL_PARSER` | `qwen3_coder` | Tool-call parser (same parser as `qwen3_xml` in vLLM ≥ 0.25) |
| `CODENDUM_SSE_KEEPALIVE_SECONDS` | `15` | Keep-alive comments while a streamed request waits |
| `CODENDUM_MAX_NUM_QUEUED_REQS` | empty | Optional queue bound (HTTP 503 beyond it) |
| `CODENDUM_PROXY_IMAGE` | `nginx:1.30.5-alpine@sha256:0985…` | Proxy container image, pinned by tag and digest |
| `CODENDUM_PROXY_DIR` | `/etc/codendum/proxy` | Proxy configuration, certificate, key map and logs |

Secrets never go in `.env`, in the repository or on a command line. The model
used here is not gated, so no Hugging Face token is needed. If you ever need
one (`HF_TOKEN=...`), or want vLLM's own API key (`VLLM_API_KEY=...`, see
[the proxy section](#4-https-proxy)), put it in a file readable only by the user
who runs `start-vllm.sh`:

```bash
install -m 0600 /dev/null /etc/codendum/vllm.secrets.env
```

Then set `CODENDUM_SECRETS_ENV_FILE=/etc/codendum/vllm.secrets.env` in `.env`.
The file is passed to Docker with `--env-file` and never printed. Anyone who can
run `docker inspect` can read it, but Docker access on the host is equivalent to
root anyway.

<!-- section: launch -->
## 3. Launch vLLM

Check the configuration and the exact command first, then start the container:

```bash
scripts/start-vllm.sh --dry-run
scripts/start-vllm.sh --wait
```

The first start downloads about 31 GB of weights, so `--wait` can take a while.
`CODENDUM_START_TIMEOUT` (3600 s by default) bounds both the wait and the
Docker health-check grace period. On a slow connection, raise it before the
first start: at 5 MB/s the download alone takes about 100 minutes. Otherwise
Docker reports the container as `unhealthy` while it is still downloading.

The script:

- validates every setting, and refuses unpinned images and any non-loopback
  address;
- never replaces an existing container unless you pass `--replace`;
- runs the container with the GPU, host networking (the API binds to
  `127.0.0.1:8000` only), a persistent model cache, a restart policy, rotated
  logs and a Docker health check;
- enables chunked prefill, prefix caching, automatic tool choice with the
  `qwen3_coder` parser, and an FP8 KV cache.

Read the real KV capacity from the logs:

```bash
docker logs codendum-vllm 2>&1 | grep -E "KV cache size|Maximum concurrency"
```

Day-to-day operations use plain Docker commands:

```bash
docker logs -f codendum-vllm
docker inspect --format '{{.State.Health.Status}}' codendum-vllm
docker restart codendum-vllm
docker stop codendum-vllm
docker start codendum-vllm
```

After the first successful download, set `CODENDUM_HF_OFFLINE=1` so that
restarts never contact the Hugging Face Hub.

### Upgrading

vLLM releases often, and a flag or a kernel can change behaviour. Upgrade in a
maintenance window:

1. Pick the new version and read its release notes.
2. Resolve its digest and check that the index includes `linux/arm64`.
3. Update `CODENDUM_VLLM_IMAGE` in `.env` and keep the old line as a comment for
   rollback.
4. Recreate the container, then run the smoke test and the benchmark.

```bash
docker buildx imagetools inspect vllm/vllm-openai:v0.30.0
scripts/start-vllm.sh --replace --wait
scripts/smoke-test.sh
```

To roll back, restore the previous image line and run
`scripts/start-vllm.sh --replace --wait` again. Pin the model revision the same
way through `CODENDUM_MODEL_REVISION`.

**Image choice.** The default is the upstream `vllm/vllm-openai` image, pinned by
digest. NVIDIA's current DGX Spark vLLM playbook uses the same image for
single-node serving. Version 0.30.0 is built on CUDA 13.0.2, the CUDA version of
the DGX Spark driver. NVIDIA's NGC image (`nvcr.io/nvidia/vllm`, for example
`26.08-py3` with vLLM 0.27.1) is an alternative but has not been tested with
this project. Three things to know if you use it:

- `start-vllm.sh` overrides the image entrypoint with `vllm`, so NVIDIA's
  entrypoint script does not run.
- vLLM releases before 0.29 do not support `CODENDUM_MAX_NUM_QUEUED_REQS`; leave
  it empty.
- If the image does not accept `--sse-keep-alive-interval`, set
  `CODENDUM_SSE_KEEPALIVE_SECONDS=0`.

Check the vLLM version inside any image with:

```bash
docker run --rm --entrypoint python3 vllm/vllm-openai:v0.30.0@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90 -c "import vllm; print(vllm.__version__)"
```

<!-- section: proxy -->
## 4. HTTPS proxy

nginx runs in its own container, `codendum-proxy`, on a dedicated port (8443
by default), so it stays clear of other web services on the host. It terminates
TLS, accepts only the allowed networks, maps each API key to a user, applies
per-user limits and forwards two endpoints to vLLM. Everything else, including
`/health`, `/metrics`, `/tokenize`, `/invocations` and the rest of the vLLM API,
stays reachable from the host only. The proxy serves the API for OpenCode and
other OpenAI-compatible clients; it has no web interface.

**1. Create the proxy directory.** `--init` creates `/etc/codendum/proxy`
(`CODENDUM_PROXY_DIR`) with `conf.d/`, `tls/` and `logs/`, and copies the example
configuration. It never overwrites existing files:

```bash
scripts/start-proxy.sh --init
```

**2. Adapt the site configuration** in
`/etc/codendum/proxy/conf.d/codendum.conf`:

- the host name;
- the LAN/VPN ranges (`192.0.2.0/24` and `198.51.100.0/24` are documentation
  placeholders);
- the port in the `listen` line, if 8443 is taken on the host;
- the limits, if needed.

**3. Install the certificate and key** from your CA as `fullchain.pem` and
`privkey.pem`, readable only by the service account:

```bash
install -m 0600 fullchain.pem privkey.pem /etc/codendum/proxy/tls/
```

**4. Generate one API key per user, locally.** Keys are 256-bit random values
with the `cdm_` prefix. They are written with mode 0600, never overwrite
existing files, and the script refuses to write them inside a Git working tree
unless Git ignores the location:

```bash
KEYDIR="$HOME/codendum-keys/$(date -u +%Y%m%d)"
scripts/gen-api-keys.sh --out-dir "$KEYDIR" --count 40
install -m 0600 "$KEYDIR/api-keys.map" /etc/codendum/proxy/api-keys.map
```

`api-keys.csv` in the same directory lists `user_id,api_key` pairs for
distribution. Give each user their key through a private channel, then delete
the CSV or store it in the organization's password manager. Use
`--users-file names.txt` for your own user ids.

- To **rotate** keys, generate a new directory, install the new map and reload
  the proxy.
- To **revoke** a single key, delete its line from the map and reload the
  proxy.

**5. Start the proxy.** The script checks the files and their permissions and
tests the configuration in a throwaway container. It refuses to replace an
existing proxy unless you pass `--replace`:

```bash
scripts/start-proxy.sh --dry-run
scripts/start-proxy.sh
```

The container is hardened:

- host networking, so nginx reaches vLLM on `127.0.0.1:8000`;
- a read-only root file system;
- no capabilities beyond the few that nginx needs to start;
- `no-new-privileges`.

Logs go to `/etc/codendum/proxy/logs/`. After changing keys, certificates or
the configuration, reload without downtime:

```bash
scripts/start-proxy.sh --reload
```

**6. Restrict the port at the firewall as well.** If the host uses `ufw`, the
rules look like this; make sure SSH stays allowed before enabling a firewall:

```bash
sudo ufw allow from 192.0.2.0/24 to any port 8443 proto tcp
sudo ufw allow from 198.51.100.0/24 to any port 8443 proto tcp
```

**Using the distribution's nginx instead.** The same configuration works with the
host's nginx package, which reads it from `/etc/nginx/conf.d/` and expects the
certificate and key map under `/etc/nginx/codendum/`:

```bash
sudo install -d -m 0700 -o root -g root /etc/nginx/codendum /etc/nginx/codendum/tls
sudo install -m 0600 -o root -g root /etc/codendum/proxy/tls/fullchain.pem /etc/codendum/proxy/tls/privkey.pem /etc/nginx/codendum/tls/
sudo install -m 0600 -o root -g root /etc/codendum/proxy/api-keys.map /etc/nginx/codendum/api-keys.map
sudo install -m 0644 -o root -g root /etc/codendum/proxy/conf.d/codendum.conf /etc/nginx/conf.d/codendum.conf
sudo nginx -t
sudo systemctl reload nginx
```

### Responses from the proxy

| Status | Meaning |
| --- | --- |
| 401 | Missing or unknown API key |
| 403 | Client address outside the allowed networks |
| 404 | Endpoint not exposed |
| 429 | Per-user limit exceeded (3 requests in flight, 60 per minute with a burst of 30) or global cap (64 in flight) |
| 502 / 504 | vLLM is down, still starting, or did not answer in time |
| 400 (from vLLM) | Invalid request, for example a prompt longer than `max-model-len` |
| 503 (from vLLM) | Queue bound reached, only if `CODENDUM_MAX_NUM_QUEUED_REQS` is set |

Stock nginx answers 503 when a `limit_conn` or `limit_req` limit is hit. This
configuration uses 429 instead, with a `Retry-After` header, so that "slow down"
is distinguishable from "server unavailable".

### Queueing and fairness

- **nginx limits requests, not tokens.** The per-user cap stops one person, or
  one agent spawning sub-agents, from occupying every slot. It does not make
  usage fair: a 60K-token request costs far more than a 2K-token one.
- **vLLM schedules first come, first served.** It runs up to `max-num-seqs`
  requests and queues the rest without limit. With
  `CODENDUM_MAX_NUM_QUEUED_REQS=N` (vLLM ≥ 0.29), requests beyond N running and
  waiting get an immediate HTTP 503 instead of a long wait.
- **Chunked prefill** splits long prompts into 8,192-token steps
  (`max-num-batched-tokens`), so other users' output keeps flowing during a
  large prefill. The large prefill itself still takes time.
- **Per-user token quotas, budgets or priorities** need a dedicated LLM gateway
  with authentication in front of vLLM. Codendum does not include or test one.

### Identity and upstream authentication

- **Organizational identity.** If the organization has an authentication gateway
  (SSO or an identity-aware proxy), it can issue or map per-user keys in place
  of `gen-api-keys.sh`. Two constraints apply: OpenCode sends a static bearer
  key, and nginx must still see a key it can map to a user.
- **Authentication, quota, active requests and queue size are separate
  controls.** nginx provides the first and a simple form of the third. vLLM
  provides the fourth.
- **Upstream key (optional).** vLLM's own `--api-key` protects only routes under
  `/v1`, `/v2`, `/inference` and `/cohere`. `/metrics`, `/health`, `/tokenize`
  and `/invocations` stay open on the loopback interface. The real protection is
  therefore the loopback binding plus restricted shell and Docker access on the
  host. For defense in depth:
  1. Set `VLLM_API_KEY=...` in the secrets file.
  2. Put `proxy_set_header Authorization "Bearer ...";` in a mode-0600 file
     in the proxy directory and include it in place of the
     `proxy_set_header Authorization "";` line.

<!-- section: opencode -->
## 5. OpenCode on the workstations

Install OpenCode V2 by following the
[official instructions](https://opencode.ai/v2/docs/). On managed machines,
prefer the organization's software distribution. Then configure the provider:

1. Copy [`config/opencode.example.json`](config/opencode.example.json) to
   `~/.config/opencode/opencode.json`, or to `opencode.json` in a project.
2. Replace `https://llm.lab.example:8443/v1` with the service URL, including
   the proxy port.
3. Provide the user's key in the `CODENDUM_API_KEY` environment variable. The
   configuration references it as `{env:CODENDUM_API_KEY}`.

```json
"codendum": {
  "name": "Codendum (local)",
  "package": "@opencode/ai/providers/openai-compatible",
  "settings": { "baseURL": "https://llm.lab.example:8443/v1", "apiKey": "{env:CODENDUM_API_KEY}" },
  "models": {
    "coder": {
      "modelID": "coder",
      "capabilities": { "tools": true, "input": ["text"], "output": ["text"] },
      "limit": { "context": 65536, "output": 8192 }
    }
  }
}
```

Why each setting matters:

- **`capabilities.tools: true` is required.** OpenCode's automatic discovery of
  vLLM models cannot see server-side tool support, so discovered models start
  with tools disabled. Declare the model explicitly, and do not name the
  provider `vllm`: that id belongs to the built-in discovery plugin.
- **`limit.context` must match the server's `max-model-len`.** OpenCode uses it
  to manage the size of a conversation, for example to decide when to compact
  it. If it is larger than the server's limit, long sessions fail with HTTP 400. For the `deep-128k` profile, change
  it to `131072` on every workstation at the same time as the server.
- **OpenCode V2 runs a background service,** which sees an environment variable
  only if the variable was set when the service started. Start OpenCode from a
  shell that exports `CODENDUM_API_KEY`, or follow the V2
  [network documentation](https://opencode.ai/v2/docs/network) to store it in
  the service configuration.
- **Certificates from an internal CA** need
  `NODE_EXTRA_CA_CERTS=/path/to/ca.pem` on the workstation.
- **OpenCode 1.x** uses the older format: see
  [`config/opencode.v1.example.json`](config/opencode.v1.example.json).
- **Session sharing is disabled** in both examples (`"share": "disabled"`).
  Shared OpenCode sessions are public links hosted outside the organization.
- **Editor warnings on V2 keys are expected.** Editors that validate against
  `https://opencode.ai/config.json` may flag `providers`, `package`, `settings`
  and `capabilities`, because the published schema still describes the V1
  format. OpenCode V2 accepts them.

A chat reply alone does not prove that the agent works. Verify a real
read → edit → test loop in a scratch directory on a workstation with a JDK:

```bash
mkdir -p /tmp/codendum-check && cd /tmp/codendum-check
cat > Calc.java <<'EOF'
public class Calc {
    static int add(int a, int b) { return a - b; }
    public static void main(String[] args) {
        if (add(2, 3) != 5) throw new AssertionError("add(2, 3) should be 5");
        System.out.println("OK");
    }
}
EOF
opencode run --auto --model codendum/coder "Run 'java Calc.java', fix the bug in Calc.java, then run it again until it prints OK."
java Calc.java
```

The check passes when OpenCode has read the file, edited it with its tools and
run the program, and the final `java Calc.java` prints `OK`. `--auto`
auto-approves tool permissions; use it only in a scratch directory.

<!-- section: smoke-test -->
## 6. Smoke test

On the GB10 host, test vLLM directly:

```bash
scripts/smoke-test.sh
```

Then test through the proxy with a user key. Here the script also checks that
`/health`, `/metrics` and the other private endpoints are **not** reachable, and
that requests without a key are rejected:

```bash
read -rsp "API key: " CODENDUM_API_KEY && export CODENDUM_API_KEY
scripts/smoke-test.sh --proxy --base-url https://llm.lab.example:8443
```

Add `--cacert /path/to/ca.pem` for a certificate from a private CA.

The checks run in this order:

1. Health, and the model list containing `coder`.
2. A chat completion.
3. A streamed completion, which must arrive in chunks and end with `[DONE]`.
4. A tool call with `tool_choice: "auto"`. This is the path OpenCode uses and the
   one that exercises the `qwen3_coder` parser.
5. A tool call with `tool_choice: "required"`. This goes through structured
   outputs and does not test the parser.
6. A round trip in which a tool result is sent back and a text answer is
   expected.

Exit status is `0` when every check passes and `1` when a check fails.
`2` means inconclusive: for example, the model legitimately answered without
calling the tool under `"auto"`. An inconclusive result is not a success.
Repeat it, and use the OpenCode loop above to decide. If raw `<tool_call>`
markup appears in the message text, the parser is misconfigured and the check
fails.

<!-- section: benchmark -->
## 7. Benchmark

> Run benchmarks only in a maintenance window. They put the server under
> sustained load for many minutes. The script refuses to start while requests
> are running or waiting, unless you pass `--allow-busy`, and asks for
> confirmation unless you pass `--yes`.

```bash
scripts/metrics.sh
scripts/bench.sh
scripts/bench.sh --concurrency 1,8,16 --shapes short --yes
scripts/metrics.sh --watch 5
```

`bench.sh` runs closed-loop load. At each concurrency level (default 1, 8, 16,
24 and 40) it keeps N requests in flight, for short prompts (about 512 input
tokens) and long prompts (about 16,384). Each request generates exactly 256
output tokens (`ignore_eos`), so runs are comparable. Every prompt starts with
a unique tag, so the results are **cache-cold**. In real OpenCode sessions the
system prompt and tool definitions repeat, and prefix caching usually makes
time to first token better than this worst case.

For each level and prompt shape, the benchmark reports:

- request count, successes and errors, grouped by error type;
- mean input and output tokens, as counted by the server;
- wall time, requests per second, output tokens per second and total tokens per
  second;
- time to first token (TTFT) at the median, P95 and P99, and end-to-end latency
  at the median and P95;
- mean time per output token;
- preemptions during the level, from `/metrics`;
- peak KV cache usage and peak running and waiting requests, sampled every
  second.

Results go to `bench-results/<UTC timestamp>/` (ignored by Git) as
`summary.csv`, `summary.json` (with the parameters used) and `requests.jsonl`.
Percentiles are computed by linear interpolation between closest ranks.

Compare profiles by running the same benchmark after each profile switch. When
you share results, include the profile, image digest, model revision and DGX OS
version, and present them as measurements, not guarantees.

### Classroom simulation

`bench.sh` measures raw capacity with independent synthetic requests. A class
of students working with OpenCode behaves differently, and
`scripts/bench-classroom.sh` simulates it:

- Each simulated user (40 by default) works on a small Java client-server chat
  project held in memory. It receives a lab exercise, then up to three
  follow-up requests such as "run the tests and fix any failure".
- The **real model** answers with an agent system prompt and OpenCode-style
  tools (`read`, `write`, `edit`, `bash`, `glob`, `grep`, `list`). It reads
  files, writes and edits code and runs builds and tests. The tools run against
  the in-memory project; builds and tests take 3–12 s of simulated time, and
  the first test run of a session fails 30 % of the time.
- The context grows with every step, as in a real session, and is compacted
  near the client's context limit.
- Users start within a two-minute ramp-up, pause 20–90 s between requests, and
  retry after HTTP 429 or 503.
- Every user has its own API key (`--api-keys-file`), so the proxy's per-user
  limits apply as they would in class.

The model's output and the server load are real; the file system, builds and
students are simulated. For the most faithful load, capture the system prompt
and tool definitions of a real OpenCode session and pass them with
`--prompt-file`.

Run it from a workstation, through the proxy, so that the network is part of
the measurement. The simulator needs only Python, so it runs in a stock
container. Copy `api-keys.csv` and, for a private CA, `ca.pem` into `keys/` (Git
ignores both), then:

```bash
mkdir -p bench-results keys
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/codendum -w /codendum python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f \
  python3 scripts/lib/codendum.py classroom --base-url https://llm.lab.example:8443 \
  --api-keys-file keys/api-keys.csv --cacert keys/ca.pem --users 40 --duration 900 --yes
```

At the same time, record the server side on the GB10 host:

```bash
scripts/metrics.sh --watch 5 --json > bench-results/server-timeline.jsonl
```

The simulator reports:

- requests and errors, and retries after 429 or 503;
- time to first token and request time (P50 and P95);
- output speed per user;
- model time per request, meaning the wait a student experiences across all
  tool steps;
- prompt sizes, compactions and tool errors.

On the host, it adds the running and waiting peaks, KV cache usage, preemptions
and prefix-cache hit rate when `/metrics` is reachable. Results go to
`bench-results/classroom-<UTC timestamp>/`.

### Measured on a GB10

**Setup.** Measured on 2026-09-29 on one Lenovo ThinkStation PGX:

- NVIDIA GB10, 128 GB, DGX OS 7.2.3, driver 580.178.04;
- vLLM 0.30.0, with image and model pinned as in `.env.example`;
- OpenCode 2.0.19 as the reference client.

The simulated students ran in Docker on a remote workstation connected through
a WireGuard VPN, with a round trip of about 100 ms, so the latencies include
that network.

**Startup.**

- vLLM reported 62.4 GiB of KV cache: 1,362,640 tokens, enough for 20.8 full
  64K contexts at the same time.
- Restarting with the model in the local cache took about 5 minutes, most of
  it spent loading the weights.

**Calibration.** A real OpenCode session for one exercise made 33 model calls
in 63 s:

- 1 title request;
- 20 steps of the main agent;
- 12 steps of an exploration subagent.

The prompts grew from 6.8K to 11K tokens. With one or two users, time to first
token was about 0.4 s and each user received about 31 output tokens per second.
A coding request completed in 1.7–2.2 minutes.

**40 simulated students.** The simulation used OpenCode 2.0.19's captured
system prompt and tools. Each student made 4 requests, with 20–90 s pauses.
The window for new requests was 15 minutes, followed by the time needed to
finish the requests in progress.

| | `classroom-64k` (16 running) | `classroom-64k-high-concurrency` (24 running) |
| --- | ---: | ---: |
| Server output, steady state | ~110 tok/s | ~140–150 tok/s |
| Time to first token, P50 / P95 | 17.5 / 60 s | 10.6 / 34 s |
| Output speed per user, P50 | 7.2 tok/s | 6.1 tok/s |
| Time to complete a coding request, P50 / P95 | 11 / 22 min | 8.3 / 18 min |
| Coding requests completed | 45 | 64 |
| Peak waiting requests | 24 | 15 |
| Peak KV cache usage | 8.5 % | 11.5 % |
| Preemptions / server errors | 0 / 0 | 0 / 0 |
| Prefix-cache hit rate | 98.2 % | 98.2 % |

What the numbers show:

- **The service stayed stable.** There were no server errors, no preemptions
  and no host memory pressure. About 1 % of requests failed on the VPN path
  and never reached the proxy.
- **The limit is generation throughput, not memory.** Agent contexts stayed
  between 7K and 19K tokens, so the KV cache never went above 12 %. Admitting
  more requests at once raised the server's total output. The memory would
  allow even more than 24 running requests, but that was not measured.
- **Prefix caching is essential.** 98 % of the 7 million prompt tokens were
  served from the cache.
- **Plan capacity in coding requests per hour.** A coding request produced
  about 2,700 output tokens over about 15 model calls. At 110–150 tokens per
  second, one GB10 completes roughly 150–200 such requests per hour for the
  whole class: about four or five per student per hour for 40 students. The
  simulation, with pauses of under 90 s, asks for more than that, so requests
  queue. A class that asks less waits less.

<!-- section: profiles -->
## 8. Profiles

| Profile | `max-model-len` | `max-num-seqs` | `max-num-batched-tokens` | `gpu-memory-utilization` | KV cache | OpenCode `limit.context` |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| `classroom-64k` (default) | 65536 | 16 | 8192 | 0.80 | FP8 | 65536 |
| `classroom-64k-high-concurrency` | 65536 | 24 | 8192 | 0.80 (initial) | FP8 | 65536 |
| `deep-128k` | 131072 | 8 | 8192 | 0.80 | FP8 | 131072 |

- **`classroom-64k`** is the default for a class or team. By the estimate
  above, all 16 running requests can be at full length at once.
- **`classroom-64k-high-concurrency`** admits more simultaneous requests. It
  relies on most requests being much shorter than 64K; many long requests at
  the same time cause preemption. OpenCode agent sessions fit that pattern
  (7–19K tokens): in the 40-student simulation this profile finished 42 % more
  coding requests than `classroom-64k`, with no preemption.
- **`deep-128k`** is for a few users working on large contexts. Very long
  prefills are slow and memory-intensive, and every OpenCode client must switch
  to 131072 at the same time.

Switch profiles with a controlled restart:

```bash
scripts/start-vllm.sh --profile classroom-64k-high-concurrency --replace --wait
```

To make the choice permanent, set `CODENDUM_PROFILE` in `.env`. Individual
values can be overridden in `.env` (for example `CODENDUM_MAX_NUM_SEQS=20`);
see the profile files in [`config/profiles/`](config/profiles/).

`0.80` for `gpu-memory-utilization` leaves headroom for the operating system
and the host processes, which share the same memory. NVIDIA documents
aggressive allocation on unified-memory systems as a cause of out-of-memory
errors. Raise the value only while `scripts/metrics.sh` shows comfortable
available host memory under load.

<!-- section: troubleshooting -->
## 9. Troubleshooting

**Diagnostics**

| Symptom | Likely cause and action |
| --- | --- |
| Container exits during startup with out-of-memory (`NV_ERR_NO_MEMORY`) | Lower `CODENDUM_GPU_MEMORY_UTILIZATION` (0.75, then 0.70) or `CODENDUM_MAX_NUM_SEQS`, and stop other workloads. NVIDIA documents flushing the page cache on DGX Spark as a workaround: `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'`. See also vLLM issue [#56824](https://github.com/vllm-project/vllm/issues/56824). |
| System becomes unresponsive during very long prefills | Instability with 128K-token prefills on DGX Spark has been reported ([dgx-spark-playbooks#97](https://github.com/NVIDIA/dgx-spark-playbooks/issues/97)). Avoid `deep-128k` with many users and keep `0.80`. |
| Answers degrade, or long outputs repeat themselves | The FP8 KV cache can affect quality, and vLLM's DGX Spark guidance says to use it only when memory pressure requires it and quality checks pass. Compare with `CODENDUM_KV_CACHE_DTYPE=auto`, which doubles KV memory per token to 96 KiB, and lower `max-num-seqs` accordingly. |
| Kernel errors mentioning FP8 block scaling on SM 12.1 | Seen with CUDA 12.9 builds ([vllm#43367](https://github.com/vllm-project/vllm/issues/43367)). Use a CUDA 13 image; the default is one. |
| `<tool_call>` text in answers; OpenCode never edits files | Tool parsing is off or wrong: run `scripts/smoke-test.sh`. On the client, check `capabilities.tools: true`, that the provider id is not `vllm`, and that V1 and V2 keys are not mixed in one provider. |
| HTTP 400 mentioning the maximum context length | The conversation exceeds `max-model-len`. Align OpenCode's `limit.context` with the profile and compact or restart the session. |
| Slow responses for everyone | Check `scripts/metrics.sh --watch 5` for waiting requests, KV cache near 100 % and growing preemptions. Reduce concurrency or context, or switch profile. |
| TLS errors in OpenCode | Set `NODE_EXTRA_CA_CERTS` to the internal CA certificate. |
| `docker: permission denied` | Use an account that may run Docker. On DGX OS, adding the user to the `docker` group is optional and grants root-equivalent access. |
| `start-proxy.sh` reports the port as in use | Another service uses 8443. Change the `listen` line in `/etc/codendum/proxy/conf.d/codendum.conf` and the port in every client's `baseURL`, then start the proxy again. |
| NVIDIA runtime missing in `docker info` | NVIDIA's troubleshooting step is `sudo nvidia-ctk runtime configure --runtime=docker` followed by `sudo systemctl restart docker`. Do not reinstall drivers. |

For the HTTP status codes 401, 403, 404, 429, 502 and 504, see
[the proxy section](#4-https-proxy).

**Logs**

- vLLM: `docker logs codendum-vllm`. Prompts are not logged, because
  `--enable-log-requests` is off by default.
- nginx: `/etc/codendum/proxy/logs/codendum.access.log` records the time,
  client address, user id, path, status and durations, never the keys or the
  request bodies. Errors go to `codendum.error.log` in the same directory. With
  the distribution's nginx, the directory is `/var/log/nginx/`.

<!-- section: limits -->
## 10. Limits and responsibilities

- **No per-user token quotas and no fair scheduling.** nginx caps requests, and
  vLLM serves requests first come, first served. See
  [Queueing and fairness](#queueing-and-fairness).
- **No isolation between users' repositories.** The model server sees only what
  the clients send it and keeps no per-user state beyond the shared prefix
  cache. Isolating users' code, credentials and build environments is the job
  of the workstations or development environments.
- **Agents run commands on the workstations.** OpenCode executes tools with the
  user's permissions. Sandboxing and permission policies belong on the
  workstation side.
- **Single host, no high availability.** When the GB10 or vLLM is down, every
  user is affected.
- **Measured on one GB10, one model and one client version.** Other hosts,
  images, models or OpenCode releases change the numbers. Measure again with
  `bench.sh` and `bench-classroom.sh` after upgrades.
- **Model output needs review.** Generated code must be compiled, tested and
  reviewed by people, and projects should be built iteratively, not in one
  generation.
- **Policies and personal data.** Prompts contain users' code, and the nginx
  logs contain user ids and timestamps. The operator is responsible for the
  applicable policies, for example data protection rules for students or
  employees, and for log retention.

<!-- section: security -->
## Security model

A summary of Codendum's threat model. For reporting vulnerabilities, see
[SECURITY.md](SECURITY.md).

| Asset | Threats | Mitigations in Codendum | Residual risk / operator duties |
| --- | --- | --- | --- |
| Proxy and API | Access from outside the organization; use of undocumented vLLM endpoints; resource exhaustion | vLLM bound to `127.0.0.1`; nginx network allowlist plus firewall; TLS; only `/v1/chat/completions` and `/v1/models` forwarded, with non-normalized paths rejected; per-user and global caps; JSON errors without internal details; proxy container with a read-only file system, minimal capabilities and `no-new-privileges` | nginx and vLLM vulnerabilities (keep them updated); authenticated users can still generate heavy load |
| API keys | Leakage, sharing, reuse after someone leaves | 256-bit random keys generated locally; key map and TLS key readable only by the service account (mode 0600); keys never on command lines or in Git; clients read them from an environment variable; rotation and revocation by editing the map | Users can share keys; accountability relies on the nginx logs; distribute keys privately |
| Users' repositories and prompts | Disclosure in transit or on the server | TLS; prompts not logged by vLLM; no persistence beyond in-memory caches | The prefix cache is shared, so timing could in principle reveal that someone recently sent the same prefix. If that matters, add `--no-enable-prefix-caching` to `CODENDUM_VLLM_EXTRA_ARGS`, at a throughput cost. Workstation security is out of scope. |
| Caches and logs | Unintended retention of personal data | Model cache holds public weights only; nginx logs record user id, address, path, status and timing; benchmark data is synthetic | Define log retention; restrict shell and Docker access on the host (Docker access equals root) |
| Supply chain | Tampered image, model or CI action | vLLM and nginx images pinned by digest; model pinned by revision; CI actions pinned to full commit SHAs and updated by Dependabot; CI uses a read-only token and no secrets | Upstream compromise before pinning; review upgrades |

<!-- section: components -->
## Components, versions and licenses

This repository is licensed under the Apache License 2.0: see [LICENSE](LICENSE)
and [NOTICE](NOTICE). That license covers only the files in this repository:
scripts, configuration examples and documentation. The repository does not
contain or redistribute container images, model weights or third-party
software. The operator downloads those separately, and they come under their
own licenses and terms, which the operator must review and comply with.

| Component | Pinned version | License | Where it runs |
| --- | --- | --- | --- |
| Codendum (this repository) | 0.1.0 (unreleased) | Apache-2.0 | GB10 host, workstations |
| vLLM container image | `vllm/vllm-openai:v0.30.0@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90` (index; the linux/arm64 manifest is `sha256:4864d466…`), CUDA 13.0.2 | vLLM: Apache-2.0; the image bundles third-party software (for example the CUDA runtime and PyTorch) under their own licenses | GB10 host |
| Model | `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` at revision `dcaee4d4dfc5ee71ad501f01f530e5652438fde0` | Apache-2.0, according to the model card; check the model's license file | GB10 host |
| OpenCode | V2 (2.0.x); 1.x with the V1 example | MIT | Workstations |
| nginx container image | `nginx:1.30.5-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94` | nginx: BSD-2-Clause; the Alpine base includes packages under their own licenses | GB10 host |
| Docker Engine, NVIDIA Container Toolkit | Preinstalled with DGX OS | Apache-2.0 | GB10 host |
| `actions/checkout` | v7.0.1 @ `3d3c42e5aac5ba805825da76410c181273ba90b1` | MIT | CI only |
| Code of conduct | Contributor Covenant 3.0 | CC BY-SA 4.0 | Documentation |

<!-- section: versioning -->
## Versioning and compatibility

Codendum follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The public interface is:

- script names and options;
- the `CODENDUM_*` variable names;
- the profile names;
- the layout of the configuration examples.

Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

| Area | Status |
| --- | --- |
| Offline tests (CI) | Ubuntu 24.04 on x64 and ARM64: lint, script tests against a mock vLLM, proxy tests (nginx container and distribution package), documentation and secret checks |
| Target host | DGX OS 7 on NVIDIA GB10. Run on a Lenovo ThinkStation PGX (DGX OS 7.2.3) on 2026-09-29: startup, smoke tests, proxy, a real OpenCode session and 40-user simulations |
| vLLM | Built for v0.30.0. `qwen3_coder` requires ≥ 0.25 (earlier releases had a different parser implementation); `CODENDUM_MAX_NUM_QUEUED_REQS` requires ≥ 0.29 |
| OpenCode | V2 native configuration; V1 format provided separately |

<!-- section: development -->
## Development

The offline test suite needs Linux with bash ≥ 4.4, Python 3, curl, OpenSSL and
Git, plus ShellCheck, yamllint, Docker and nginx for the full run. It needs no
GPU or model weights; the proxy container test pulls the pinned nginx image.

```bash
tests/run.sh
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and style,
[docs/TRANSLATING.md](docs/TRANSLATING.md) for the translations and
[docs/MAINTAINING.md](docs/MAINTAINING.md) for releases and upgrades. By
participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

<!-- section: references -->
## References

- NVIDIA, DGX Spark: <https://www.nvidia.com/en-us/products/workstations/dgx-spark/> and the hardware overview <https://docs.nvidia.com/dgx/dgx-spark/hardware.html>
- NVIDIA, vLLM on DGX Spark (playbook): <https://build.nvidia.com/spark/vllm/instructions>
- NVIDIA, container runtime for Docker on DGX Spark: <https://docs.nvidia.com/dgx/dgx-spark/nvidia-container-runtime-for-docker.html>
- NVIDIA, vLLM container release notes: <https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/>
- vLLM on DGX Spark (vLLM blog): <https://vllm.ai/blog/2026-06-01-vllm-dgx-spark>
- Qwen, FP8 checkpoint: <https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8>
- vLLM, `serve` options: <https://docs.vllm.ai/en/latest/cli/serve/>
- vLLM, tool calling: <https://docs.vllm.ai/en/latest/features/tool_calling/>
- vLLM, metrics: <https://docs.vllm.ai/en/latest/usage/metrics/>
- vLLM, security: <https://docs.vllm.ai/en/latest/usage/security/>
- OpenCode V2, models and local servers: <https://opencode.ai/v2/docs/models>
- OpenCode V2, providers: <https://opencode.ai/v2/docs/providers>
- Open Source Guides, starting a project: <https://opensource.guide/starting-a-project/>
- GitHub, licensing a repository: <https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository>
- Apache License 2.0: <https://choosealicense.com/licenses/apache-2.0/>
- GitHub Actions, secure use: <https://docs.github.com/en/actions/reference/security/secure-use>
- GitHub, private vulnerability reporting: <https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository>

## Author

Codendum is created and maintained by [Stefano Noferi](https://noferi.it/)
(GitHub: [`stefanoferi`](https://github.com/stefanoferi)).
