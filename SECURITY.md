# Security policy

## Supported versions

Codendum is a young project. Security fixes are made on `main` and included in
the next release. Only the latest release receives fixes.

| Version | Supported |
| --- | --- |
| Latest release (0.x) | Yes |
| Older releases | No; upgrade to the latest |

## Reporting a vulnerability

Please **do not** open a public issue, discussion or pull request for a
security problem.

Report it privately through GitHub's private vulnerability reporting:

- <https://github.com/stefanoferi/codendum/security/advisories/new>, or
- the repository's **Security** tab → **Report a vulnerability**.

A useful report includes:

- the affected file, script or configuration and the Codendum version or
  commit;
- the impact, and the conditions an attacker needs (for example network
  position or a valid API key);
- the steps to reproduce, with placeholders instead of real keys, host names
  or user data;
- a proposed fix or mitigation, if you have one.

The maintainer aims to acknowledge reports within 7 days, to agree on a
disclosure timeline with the reporter, and to credit reporters who wish to be
named in the advisory. This is a volunteer-maintained project, so timelines
are best effort.

## Scope

In scope: the contents of this repository, meaning scripts, the nginx and
OpenCode configuration examples, CI workflows and any documented procedure that
leads to an insecure deployment.

Out of scope, and best reported upstream:

- vLLM: <https://github.com/vllm-project/vllm/security>
- OpenCode: its repository's security policy
- nginx: <https://nginx.org/en/security_advisories.html>
- NVIDIA software (drivers, container toolkit, DGX OS): <https://www.nvidia.com/en-us/security/>
- The model weights and their behaviour, for example unsafe generated code.
  Generated code must always be reviewed before use.

If you are unsure whether something is in scope, report it privately anyway.

## Deployment hardening

The threat model and the security-relevant defaults are described in
[docs/security.md](docs/security.md). In short:

- vLLM listens on `127.0.0.1` only.
- nginx, in a hardened container on a dedicated port, is the single entry
  point. It provides TLS, a network allowlist, per-user API keys, only two
  forwarded endpoints and per-user limits.
- API keys are generated locally and never stored in Git.
- The container image, model and CI actions are pinned.
