# Sizing

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
the same capacity. On the GB10 used for the [measurements](benchmark.md#measured-on-a-gb10),
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
