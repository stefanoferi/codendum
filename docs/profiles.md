# Profiles

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
  to 131072 at the same time: run `scripts/configure-opencode.sh` again on the
  workstations (see [OpenCode](opencode.md#automatic-configuration)).

Switch profiles with a controlled restart:

```bash
scripts/start-vllm.sh --profile classroom-64k-high-concurrency --replace --wait
```

To make the choice permanent, set `CODENDUM_PROFILE` in `.env`. Individual
values can be overridden in `.env` (for example `CODENDUM_MAX_NUM_SEQS=20`);
see the profile files in [`config/profiles/`](https://github.com/stefanoferi/codendum/blob/main/config/profiles/).

`0.80` for `gpu-memory-utilization` leaves headroom for the operating system
and the host processes, which share the same memory. NVIDIA documents
aggressive allocation on unified-memory systems as a cause of out-of-memory
errors. Raise the value only while `scripts/metrics.sh` shows comfortable
available host memory under load.
