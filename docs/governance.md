# Governance

Codendum controls **who** can use the model: the organization's networks, one
API key per user, limits per user and overall, and access logs keyed by user.
It does not govern **what** goes through the model. It does not inspect prompts
or answers, apply content policies, protect personal data, guard agents or keep
a tamper-evident audit trail. Classrooms and companies often need some of these,
for example to protect students' or employees' personal data, or to meet
internal policies and the EU AI Act.

## Today

| Area | What Codendum provides |
| --- | --- |
| Access | Network allowlist, TLS, one API key per user, per-user and global limits |
| Exposure | Only `/v1/chat/completions` and `/v1/models` reach vLLM; everything else stays on the host |
| Accountability | nginx access logs with time, user id, path, status and durations, never keys or bodies |
| Privacy | vLLM does not log prompts; no persistence beyond in-memory caches |
| Content, personal data, agents, audit | Not covered |

## Possible future integration with Admina

[Admina](https://admina.org/) is an open-source framework for building AI
applications that are governed by design. It is licensed under Apache-2.0,
self-hosted, and maintained by the author of Codendum
([GitHub](https://github.com/admina-org/admina)). Its OpenAI-compatible gateway
supports vLLM as an engine. It is a natural candidate to add the missing
governance layer, placed between the proxy and the model:

```text
 User workstations ──HTTPS──▶ nginx container    network allowlist, TLS, per-user keys and limits
                                  │  authenticated user id (set by nginx, never by the client)
                                  ▼
                              Admina gateway     policies, personal-data redaction, injection firewall,
                                  │              agent loop breaker, audit log
                                  ▼
                              vLLM container     127.0.0.1:8000
```

The integration would add one more container to the Docker deployment. For
each Admina capability, this is what it would bring to a shared coding-agent
service:

| Admina capability | Use in Codendum |
| --- | --- |
| Personal-data redaction on requests and streamed responses | Keep personal data in prompts and code out of the model, including OpenCode's streamed answers |
| Prompt-injection firewall | Screen instructions hidden in repositories, issues or web pages that agents read |
| Agent loop breaker | Stop runaway agent loops. In the [measurements](benchmark.md#measured-on-a-gb10), a coding request took a median of 15 model calls in the simulation and 33 in a real OpenCode session. |
| MCP tool-call allowlist | Restrict which MCP servers and tools agents may reach, when OpenCode is configured with MCP |
| Forensic audit log with hash chains | Tamper-evident record of requests per user, with retention decided by the organization |
| EU AI Act classification | Support the organization's assessment of its obligations, for example in education |

**Open points to design and measure before the integration:**

- how nginx passes the authenticated user id to Admina, so that policies and
  audit records are per user;
- which policies suit a classroom and which suit a company team;
- where the audit log is stored, and for how long;
- the effect on time to first token and on throughput. The effect would be
  measured with `scripts/bench.sh` and `scripts/bench-classroom.sh` against the
  results in [Benchmarks](benchmark.md#measured-on-a-gb10).

**Status.** This is a possible future integration. It is not implemented or
tested in Codendum yet, and no configuration in this repository depends on
Admina.
