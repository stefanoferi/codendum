# Codendum web (prototype)

A small panel for a classroom, hackathon, lab or office:

- people register themselves and wait for approval;
- the first administrator is created through a one-time setup link printed in
  the panel log; more administrators can be added later;
- every approved person finds on their own page how to install OpenCode, a
  one-time command that sets it up for Codendum, the link to the chat, and
  their API key;
- administrators approve (one or all), block, unblock, delete, renew keys and
  grant the administrator role;
- a status box shows whether vLLM is up, the running and waiting requests, KV
  cache usage, and who made the most requests in the last 10 minutes.

The panel writes the keys of approved users to `api-keys-web.map`, next to the
`api-keys.map` from `gen-api-keys.sh`, and reloads the proxy with
`nginx -s reload` through the Docker socket. Keys from `gen-api-keys.sh` keep
working.

**Prototype:** standard library only, one Python file and one SQLite file,
covered by `tests/test-web.sh`. Not yet included:

- login to Open WebUI through Codendum (`/auth/verify` is ready for nginx
  `auth_request`, but it is not wired up yet);
- protection against cross-site request forgery beyond `SameSite=Strict`
  cookies.

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
  or two); the status box shows vLLM as down meanwhile.
- The panel listens on `127.0.0.1:8090` only, so nginx is the only way in.
- Mounting the Docker socket gives the panel control over Docker. This is
  acceptable on a dedicated host, but it is one of the points to revisit
  before a release.

To remove the panel, stop and remove the container, restore
`codendum.conf.bak`, and run `scripts/start-proxy.sh --reload`.
