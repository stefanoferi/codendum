#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Read-only host checks before starting Codendum. Prints suggestions; never
# changes the system.

set -Eeuo pipefail
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: scripts/preflight.sh [options]

Check that this host can run the Codendum vLLM service. Nothing is modified.

Options:
  -e, --env-file FILE  configuration file (default: .env in the repository root)
      --gpu-test       also run nvidia-smi inside the configured vLLM image
                       (starts a short-lived container; the image must be present)
  -h, --help           show this help

Exit status: 0 when no check failed (warnings allowed), 1 otherwise.
EOF
}

env_file="" gpu_test=0
while (($#)); do
    case "$1" in
        -e | --env-file) env_file="${2:?--env-file needs a value}"; shift 2 ;;
        --gpu-test) gpu_test=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" 64 ;;
    esac
done

load_config "$env_file"
load_profile

passed=0 warned=0 failed=0
pass() { printf '[PASS] %s\n' "$1"; passed=$((passed + 1)); }
warn() { printf '[WARN] %s\n' "$1"; [[ -n "${2:-}" ]] && printf '       hint: %s\n' "$2"; warned=$((warned + 1)); }
failc() { printf '[FAIL] %s\n' "$1"; [[ -n "${2:-}" ]] && printf '       hint: %s\n' "$2"; failed=$((failed + 1)); }
info() { printf '[INFO] %s\n' "$1"; }

# free_gib PATH: free space in GiB on the filesystem holding PATH (or its
# closest existing parent).
free_gib() {
    local path="$1"
    while [[ ! -e "$path" && "$path" != / ]]; do path="$(dirname "$path")"; done
    df -Pk "$path" | awk 'NR == 2 { printf "%d", $4 / 1048576 }'
}

printf 'Codendum preflight  %s  profile=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$CODENDUM_PROFILE"

# ---------------------------------------------------------------- platform
os="$(uname -s)"
arch="$(uname -m)"
if [[ "$os" == Linux ]]; then pass "Operating system: Linux"; else failc "Operating system: ${os}" "Codendum targets Linux (DGX OS / Ubuntu) on the GB10 host"; fi
if [[ "$arch" == aarch64 ]]; then
    pass "Architecture: aarch64 (ARM64)"
else
    failc "Architecture: ${arch}" "the GB10 is ARM64 (aarch64); x86 hosts are not a supported target"
fi
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    distro="$(. /etc/os-release && printf '%s %s' "${NAME:-unknown}" "${VERSION:-}")"
    info "Distribution: ${distro}"
fi

# ---------------------------------------------------------------- tools
for cmd in curl python3; do
    if have "$cmd"; then pass "Command available: ${cmd}"; else failc "Command missing: ${cmd}" "install it with the distribution package manager"; fi
done
if have python3 && ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    failc "python3 is older than 3.8"
fi

# ---------------------------------------------------------------- docker
docker_ok=0
if ! have docker; then
    failc "Docker not installed" "DGX OS ships Docker; on other systems follow the Docker Engine documentation"
elif ! docker info >/dev/null 2>&1; then
    failc "Docker daemon not reachable by user $(id -un)" "check 'systemctl status docker'; optionally add the user to the docker group (sudo usermod -aG docker \$USER, then log in again)"
else
    docker_ok=1
    pass "Docker daemon reachable (server $(docker info --format '{{.ServerVersion}}' 2>/dev/null))"
    runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
    if [[ "$runtimes" == *nvidia* ]]; then
        pass "NVIDIA container runtime registered with Docker"
    elif have nvidia-ctk; then
        warn "NVIDIA runtime not listed by 'docker info'" "NVIDIA troubleshooting: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
    else
        failc "NVIDIA Container Toolkit not found" "it is preinstalled on DGX OS; see the NVIDIA container runtime guide listed in docs/reference.md"
    fi
fi

# ---------------------------------------------------------------- GPU
if have nvidia-smi && gpu="$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null)" && [[ -n "$gpu" ]]; then
    pass "nvidia-smi: ${gpu}"
    if [[ "$gpu" == *GB10* ]]; then
        pass "GPU is a GB10"
    else
        warn "GPU is not a GB10" "profiles and sizing in this repository are written for the GB10 (128 GB unified memory)"
    fi
else
    failc "nvidia-smi not available or failing" "on DGX OS the driver is preinstalled; do not reinstall drivers on a configured DGX Spark, check 'nvidia-smi' output and system logs"
fi

if ((gpu_test)); then
    image="${CODENDUM_VLLM_IMAGE:-}"
    if ((!docker_ok)) || [[ -z "$image" ]]; then
        failc "GPU container test skipped (Docker or CODENDUM_VLLM_IMAGE unavailable)"
    elif ! docker image inspect "$image" >/dev/null 2>&1; then
        warn "GPU container test skipped: image not present locally" "pull it first: docker pull ${image}"
    elif docker run --rm --gpus all --entrypoint nvidia-smi "$image" -L >/dev/null 2>&1; then
        pass "GPU visible inside the vLLM image (docker run --gpus all)"
    else
        failc "GPU not visible inside a container" "try the NVIDIA test: docker run --rm --gpus=all nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04 nvidia-smi"
    fi
fi

# ---------------------------------------------------------------- memory
gpu_util="${CODENDUM_GPU_MEMORY_UTILIZATION:-0.80}"
if [[ -r /proc/meminfo ]]; then
    read -r mem_total_kib mem_avail_kib < <(awk '/^MemTotal:/ {t=$2} /^MemAvailable:/ {a=$2} END {print t, a}' /proc/meminfo)
    mem_total_gib=$((mem_total_kib / 1048576))
    mem_avail_gib=$((mem_avail_kib / 1048576))
    need_gib="$(awk -v t="$mem_total_kib" -v u="$gpu_util" 'BEGIN { printf "%d", t * u / 1048576 }')"
    if ((mem_total_gib >= 100)); then
        pass "Memory total: ${mem_total_gib} GiB (unified CPU/GPU)"
    else
        warn "Memory total: ${mem_total_gib} GiB" "the default profiles assume a 128 GB GB10"
    fi
    if ((mem_avail_gib >= need_gib + 4)); then
        pass "Memory available: ${mem_avail_gib} GiB (vLLM will claim about ${need_gib} GiB at ${gpu_util})"
    else
        warn "Memory available: ${mem_avail_gib} GiB, vLLM wants about ${need_gib} GiB at ${gpu_util}" "stop other GPU/CPU workloads (Java builds belong on the workstations). If vLLM still fails to start, NVIDIA documents flushing the page cache: sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'"
    fi
else
    warn "Memory: /proc/meminfo not readable"
fi

# ---------------------------------------------------------------- disk
hf_cache="${CODENDUM_HF_CACHE_DIR:-}"
if [[ -n "$hf_cache" ]]; then
    free="$(free_gib "$hf_cache")"
    if [[ -d "$hf_cache" ]]; then info "Hugging Face cache: ${hf_cache}"; else warn "Hugging Face cache directory missing: ${hf_cache}" "sudo install -d -m 0755 -o \"\$USER\" ${hf_cache}"; fi
    if ((free >= 60)); then pass "Free space for the model cache: ${free} GiB"; else warn "Free space for the model cache: ${free} GiB" "the FP8 checkpoint needs about 30 GiB; keep at least 60 GiB free"; fi
else
    warn "CODENDUM_HF_CACHE_DIR not set" "copy .env.example to .env and review it"
fi
if ((docker_ok)); then
    docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
    free="$(free_gib "$docker_root")"
    if ((free >= 40)); then pass "Free space for images (${docker_root}): ${free} GiB"; else warn "Free space for images (${docker_root}): ${free} GiB" "the vLLM image needs roughly 20-30 GiB once extracted"; fi
fi

# ---------------------------------------------------------------- network
port="${CODENDUM_PORT:-8000}"
if have ss; then
    listeners="$(ss -Hltn "sport = :${port}" 2>/dev/null | awk '{print $4}' | sort -u | paste -sd ' ' -)"
    if [[ -z "$listeners" ]]; then
        pass "Port ${port} is free"
    elif [[ "$listeners" =~ (0\.0\.0\.0|\[::\]|\*): ]]; then
        failc "Port ${port} is exposed on all interfaces (${listeners})" "the model API must listen on 127.0.0.1 only; stop the service that publishes it"
    else
        info "Port ${port} in use on loopback (${listeners}); fine if it is the Codendum container"
    fi
else
    warn "Cannot check port ${port} ('ss' not found)"
fi
proxy_conf="${CODENDUM_PROXY_DIR:-/etc/codendum/proxy}/conf.d/codendum.conf"
proxy_ports=(8443)
if [[ -f "$proxy_conf" ]]; then
    mapfile -t proxy_ports < <(proxy_listen_ports "$proxy_conf")
fi
for p in "${proxy_ports[@]}"; do
    if ! have ss; then
        break
    elif port_in_use "$p"; then
        info "Proxy port ${p} in use; fine if it is the Codendum proxy container, otherwise change 'listen' in ${proxy_conf}"
    else
        pass "Proxy port ${p} is free"
    fi
done
if ((docker_ok)); then
    for c in "${CODENDUM_CONTAINER_NAME:-codendum-vllm}" "${CODENDUM_PROXY_CONTAINER_NAME:-codendum-proxy}"; do
        if docker container inspect "$c" >/dev/null 2>&1; then
            info "Container ${c} exists ($(docker container inspect -f '{{.State.Status}}' "$c"))"
        fi
    done
fi

# ---------------------------------------------------------------- config
for var in CODENDUM_VLLM_IMAGE CODENDUM_PROXY_IMAGE; do
    image="${!var:-}"
    if [[ -z "$image" ]]; then
        warn "${var} not set" "cp .env.example .env"
    elif [[ "$image" == *@sha256:* ]]; then
        pass "${var} pinned by digest"
    else
        warn "${var} not pinned by digest: ${image}" "see 'Upgrading' in docs/installation.md"
    fi
done

printf '\nSummary: %d passed, %d warnings, %d failed\n' "$passed" "$warned" "$failed"
((failed == 0))
