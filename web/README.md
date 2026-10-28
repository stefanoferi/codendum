# Codendum web (prototype)

A small panel for a classroom, hackathon, lab or office:

- people register with their e-mail address, which is also what they log in
  with; the form tells them at once if the address is invalid or already
  registered (accounts created with a user name before keep it);
- they then wait for approval;
- the first administrator is created through a one-time setup link printed in
  the panel log; more administrators can be added later;
- every approved person finds on their own page a three-step guide (install
  OpenCode, a one-time command that sets it up for Codendum, start), the link
  to the chat, and their API key;
- the profile menu in the header leads to the profile page, where people
  change their password (their other sessions are logged out);
- the administration has three pages:
  - **Dashboard** (`/admin`):
    - tiles averaged over the last minute: tokens per second, requests, time
      to first token, tokens per second for each request, the time of the
      last request; running and waiting requests, KV cache, prefix cache hits,
      GPU utilisation, temperature and power, host memory, preemptions;
    - live charts of the last 5 minutes, 15 minutes or hour, with the peak
      (vLLM is sampled every 5 seconds and the last hour is kept in memory);
    - the model and the engine: served name, weights, vLLM version, image,
      profile, context, requests at once, KV cache size and type, GPU memory
      share, prefix caching, container state, uptime and restarts, GPU name
      and clock;
    - totals since vLLM started: requests, tokens, how requests ended, mean
      latencies;
    - people, and who made the most API requests in the last 10 minutes;
  - **People** (`/admin/people`): approve (one or all), block, unblock,
    delete, renew keys, reset passwords, grant the administrator role;
  - **Settings** (`/admin/settings`): registration open or closed, the e-mail
    domains allowed to register (optionally approved at once), a notice shown
    at the top of every personal page, and web search in the chat.

The footer of every page credits the project, links to codendum.ai and GitHub,
and shows the commit the panel runs from (read from `.git` at start).

The panel writes the keys of approved users to `api-keys-web.map`, next to the
`api-keys.map` from `gen-api-keys.sh`, and reloads the proxy with
`nginx -s reload` through the Docker socket. Keys from `gen-api-keys.sh` keep
working.

**Prototype:** standard library only, one Python file and one SQLite file,
covered by `tests/test-web.sh`.

## Open WebUI

`/auth/verify` answers nginx `auth_request`, so that Open WebUI can trust the
logged-in user. It returns `X-Codendum-Email`, `X-Codendum-User` and
`X-Codendum-Role` (`admin` for administrators, `user` otherwise), so that
administrators of the panel are administrators of the chat. The nginx side is
not in `patch-nginx.py` yet; in the `location` that proxies Open WebUI:

```nginx
auth_request /_codendum_verify;          # internal location → panel /auth/verify
auth_request_set $codendum_email $upstream_http_x_codendum_email;
auth_request_set $codendum_name  $upstream_http_x_codendum_user;
auth_request_set $codendum_role  $upstream_http_x_codendum_role;
# Always overwritten, never the ones sent by the client.
proxy_set_header X-Codendum-Email $codendum_email;
proxy_set_header X-Codendum-User  $codendum_name;
proxy_set_header X-Codendum-Role  $codendum_role;
```

Open WebUI (checked with 0.12.0) then needs
`WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-Codendum-Email`,
`WEBUI_AUTH_TRUSTED_NAME_HEADER=X-Codendum-User` and
`WEBUI_AUTH_TRUSTED_ROLE_HEADER=X-Codendum-Role`, and must listen on loopback
only: anyone who reaches it directly can send these headers. Set the role
header in nginx before Open WebUI trusts it. Open WebUI applies the role at
each sign-in.

With `CODENDUM_WEB_OPENWEBUI_URL` (for example `http://127.0.0.1:3000`), the
panel also talks to Open WebUI itself, signing in with the same headers:

- it applies a role change (make or remove an administrator, block or delete
  one) at once instead of at the next chat login;
- the settings page turns web search in the chat on or off
  (`ENABLE_WEB_SEARCH`, through `/api/v1/retrieval/config`), acting as the
  administrator who uses the page.

Every form that changes something carries a token derived from the session
(CSRF protection), on top of `SameSite=Strict` cookies. A password reset shows
a random password once to the administrator and logs the person out
everywhere; there is no e-mail delivery.

Failed logins are limited: 5 per username and client address, and 30 per
client address, in 10 minutes. The second limit is generous because a
classroom often shares one address.

## Workstation setup

No repository copy and no Python are needed on workstations. The personal page
shows the official OpenCode install commands and creates a setup command that
works once, for 10 minutes:

```bash
curl -fsSk --pinnedpubkey 'sha256//…' https://HOST:8443/s/CODE | sh                            # macOS, Linux
curl.exe -fsSk --pinnedpubkey 'sha256//…' https://HOST:8443/s/CODE.ps1 | Out-String | Invoke-Expression  # Windows
```

- `--pinnedpubkey` makes curl accept only the proxy's public key, so the script
  cannot be replaced on the network even before the workstation trusts the
  certificate (`-k` only skips the CA check). The panel computes the pin from
  `CODENDUM_WEB_TLS_CHAIN` (default `/proxy/tls/fullchain.pem`) on every page.
- The key is not in the URL: the script is generated when the code is used.
- `setup-workstation.sh` and `setup-workstation.ps1` save the certificate and
  the key in `~/.config/codendum/` (the key in a private file, read by OpenCode
  through `{file:…}`), add the `codendum` provider and default model to the
  OpenCode configuration after a backup, set `NODE_EXTRA_CA_CERTS` for the
  OpenCode service and the shell, and send a test request.
- `/ca.pem` serves the last certificate of the chain, for browsers.

Tested with OpenCode 2.0.32 on macOS and with a simulated Linux workstation in
`tests/test-web.sh`. The Windows script is not tested yet; it needs `curl.exe` 7.58.1 or later
(pinning with Schannel), as shipped in up-to-date Windows 10 and 11.

OpenCode 2 accepts the 1.x `provider` key next to `providers`, so existing
1.x provider entries keep working (checked with 2.0.32).

## Install on the GB10 host

Run these steps between sessions, not during a class. Paths follow the layout
used by `start-proxy.sh`.

```bash
cd ~/codendum
mkdir -p -m 0700 ~/codendum-data/web
web/patch-nginx.py ~/codendum-data/proxy/conf.d/codendum.conf
scripts/start-proxy.sh --reload
docker run -d --name codendum-web --restart unless-stopped --network host \
  -v ~/codendum:/codendum:ro -v ~/codendum-data/proxy:/proxy -v ~/codendum-data/web:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e CODENDUM_WEB_DB=/data/web.sqlite -e CODENDUM_WEB_MAP=/proxy/api-keys-web.map \
  -e CODENDUM_WEB_PROXY_CONTAINER=codendum-proxy \
  -e CODENDUM_WEB_ACCESS_LOG=/proxy/logs/codendum.access.log \
  -e CODENDUM_WEB_API_URL='https://{host}:8443' -e CODENDUM_WEB_CHAT_URL='https://{host}/' \
  python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f \
  python3 /codendum/web/codendum_web.py
```

`{host}` stays as written: the panel fills it with the host name each browser
used, so nothing changes when the host gets a new address. Drop
`CODENDUM_WEB_CHAT_URL` if there is no Open WebUI. Then create the
administrator account with the setup link from the log (replace `HOST` in it
with the host address):

```bash
docker logs codendum-web | grep setup=
```

The link works once. Until an administrator exists, a new link is printed at
every start, and everyone else who registers waits for approval.

- `patch-nginx.py` keeps a backup as `codendum.conf.bak`.
- After a boot, the API answers 502 until vLLM has loaded the model (a minute
  or two); the dashboard shows vLLM as down meanwhile.
- The panel listens on `127.0.0.1:8090` only, so nginx is the only way in.
- Mounting the Docker socket gives the panel control over Docker. This is
  acceptable on a dedicated host, but it is one of the points to revisit
  before a release.
- Through the same socket the dashboard reads the vLLM container
  (`CODENDUM_WEB_VLLM_CONTAINER`, default `codendum-vllm`; empty turns it
  off): a few serving options, the state and the image from `docker inspect`,
  never the environment, and every 5 seconds one `nvidia-smi --query-gpu`
  inside the container. If `nvidia-smi` fails, the GPU tiles stay empty and
  it is tried again a minute later.

To remove the panel, stop and remove the container, restore
`codendum.conf.bak`, and run `scripts/start-proxy.sh --reload`.
