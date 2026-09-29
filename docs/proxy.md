# HTTPS proxy

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

**4. Generate one API key per user, locally.** `users.txt` lists one user id
per line, using lowercase letters, digits, `.`, `_` or `-`; `--count N` creates
numbered ids instead. Keys are 256-bit random values with the `cdm_` prefix.
They are written with mode 0600, never overwrite existing files, and the script
refuses to write them inside a Git working tree unless Git ignores the
location:

```bash
KEYDIR="$HOME/codendum-keys/$(date -u +%Y%m%d)"
scripts/gen-api-keys.sh --out-dir "$KEYDIR" --users-file users.txt
install -m 0600 "$KEYDIR/api-keys.map" /etc/codendum/proxy/api-keys.map
```

`api-keys.csv` in the same directory lists `user_id,api_key` pairs for
distribution. Give each user their key through a private channel, then delete
the CSV or store it in the organization's password manager.

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

## Responses from the proxy

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

## Queueing and fairness

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
  For governance of the content itself, see the possible integration with
  Admina in [Governance](governance.md).

## Identity and upstream authentication

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
