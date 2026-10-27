# Codendum web (prototype)

A small panel for a classroom, hackathon, lab or office:

- people register themselves and wait for approval;
- the first account becomes the administrator; more administrators can be
  added later;
- every approved person finds their API key, the OpenCode setup command and
  the link to the chat on their own page;
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
  cookies;
- rate limiting of login attempts.

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
  -e CODENDUM_WEB_API_URL=https://HOST:8443 -e CODENDUM_WEB_CHAT_URL=https://HOST/ \
  python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f \
  python3 /codendum/web/codendum_web.py
```

Replace `HOST` with the address the students use. Then open `https://HOST:8443/`
and **register your own account first**: the first account becomes the
administrator.

- `patch-nginx.py` keeps a backup as `codendum.conf.bak`.
- The panel listens on `127.0.0.1:8090` only, so nginx is the only way in.
- Mounting the Docker socket gives the panel control over Docker. This is
  acceptable on a dedicated host, but it is one of the points to revisit
  before a release.

To remove the panel, stop and remove the container, restore
`codendum.conf.bak`, and run `scripts/start-proxy.sh --reload`.
