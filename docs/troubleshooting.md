# Troubleshooting

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
[the proxy section](proxy.md).

**Logs**

- vLLM: `docker logs codendum-vllm`. Prompts are not logged, because
  `--enable-log-requests` is off by default.
- nginx: `/etc/codendum/proxy/logs/codendum.access.log` records the time,
  client address, user id, path, status and durations, never the keys or the
  request bodies. Errors go to `codendum.error.log` in the same directory. With
  the distribution's nginx, the directory is `/var/log/nginx/`.
