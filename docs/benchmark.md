# Benchmarks

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

## Classroom simulation

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

## Measured on a GB10

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
