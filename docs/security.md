# Security and limits

## Security model

A summary of Codendum's threat model. For reporting vulnerabilities, see
[SECURITY.md](https://github.com/stefanoferi/codendum/blob/main/SECURITY.md).

| Asset | Threats | Mitigations in Codendum | Residual risk / operator duties |
| --- | --- | --- | --- |
| Proxy and API | Access from outside the organization; use of undocumented vLLM endpoints; resource exhaustion | vLLM bound to `127.0.0.1`; nginx network allowlist plus firewall; TLS; only `/v1/chat/completions` and `/v1/models` forwarded, with non-normalized paths rejected; per-user and global caps; JSON errors without internal details; proxy container with a read-only file system, minimal capabilities and `no-new-privileges` | nginx and vLLM vulnerabilities (keep them updated); authenticated users can still generate heavy load |
| API keys | Leakage, sharing, reuse after someone leaves | 256-bit random keys generated locally; key map and TLS key readable only by the service account (mode 0600); keys never on command lines or in Git; clients read them from an environment variable; rotation and revocation by editing the map | Users can share keys; accountability relies on the nginx logs; distribute keys privately |
| Users' repositories and prompts | Disclosure in transit or on the server | TLS; prompts not logged by vLLM; no persistence beyond in-memory caches | The prefix cache is shared, so timing could in principle reveal that someone recently sent the same prefix. If that matters, add `--no-enable-prefix-caching` to `CODENDUM_VLLM_EXTRA_ARGS`, at a throughput cost. Workstation security is out of scope. |
| Caches and logs | Unintended retention of personal data | Model cache holds public weights only; nginx logs record user id, address, path, status and timing; benchmark data is synthetic | Define log retention; restrict shell and Docker access on the host (Docker access equals root) |
| Supply chain | Tampered image, model or CI action | vLLM and nginx images pinned by digest; model pinned by revision; CI actions pinned to full commit SHAs and updated by Dependabot; CI uses a read-only token and no secrets | Upstream compromise before pinning; review upgrades |

## Limits and responsibilities

- **No per-user token quotas and no fair scheduling.** nginx caps requests, and
  vLLM serves requests first come, first served. See
  [Queueing and fairness](proxy.md#queueing-and-fairness).
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
