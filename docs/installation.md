# Installation

## Prerequisites

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

## Configuration

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
| `CODENDUM_PROFILE` | `classroom-64k` | Serving profile (see [Profiles](profiles.md)) |
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
[the proxy section](proxy.md)), put it in a file readable only by the user
who runs `start-vllm.sh`:

```bash
install -m 0600 /dev/null /etc/codendum/vllm.secrets.env
```

Then set `CODENDUM_SECRETS_ENV_FILE=/etc/codendum/vllm.secrets.env` in `.env`.
The file is passed to Docker with `--env-file` and never printed. Anyone who can
run `docker inspect` can read it, but Docker access on the host is equivalent to
root anyway.

## Launch vLLM

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
