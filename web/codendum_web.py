#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
"""Codendum web: a small panel for users, API keys and service status.

PROTOTYPE. Standard library only (http.server + sqlite3).

- Users register and wait for approval. While there is no administrator, the
  panel prints a one-time setup link to its log; the account created through
  that link becomes the first administrator.
- People sign in with their e-mail address; the registration form checks it
  while it is typed. Failed logins are limited per client address and e-mail.
- Administrators manage people and a few settings on /admin: registration
  open or closed, allowed e-mail domains (optionally approved at once), a
  notice shown to every user, and password resets. Forms that change
  something carry a token derived from the session (CSRF protection).
- Each active user gets an API key; the nginx key map is rewritten on every
  change and the proxy is reloaded with CODENDUM_WEB_RELOAD_CMD.
- /auth/verify answers nginx auth_request, so that Open WebUI can trust the
  identity of the logged-in user (trusted e-mail header).
- The administration has three pages: a dashboard (vLLM /metrics sampled every
  5 seconds for a live chart of the last hour, host memory, people and the
  proxy access log), people, and settings.
- With Open WebUI (CODENDUM_WEB_OPENWEBUI_URL), administrators are also
  administrators of the chat: /auth/verify gives the role, and the panel
  updates it in Open WebUI when it changes. The settings page turns the chat's
  web search on or off through the Open WebUI API.
- Workstation setup: the personal page creates a one-time command (10
  minutes) that downloads setup-workstation.sh or .ps1 with the user's key,
  pinned to the proxy's public key, so no repository copy is needed.

Configuration (environment):
  CODENDUM_WEB_LISTEN       host:port to listen on (default 127.0.0.1:8090)
  CODENDUM_WEB_DB           SQLite file (default ./codendum-web.sqlite)
  CODENDUM_WEB_MAP          nginx key map to write (default ./api-keys-web.map); a
                            separate file, so keys from gen-api-keys.sh keep working
  CODENDUM_WEB_PROXY_CONTAINER  proxy container reloaded through the Docker
                            socket (/var/run/docker.sock) after the map changes
  CODENDUM_WEB_RELOAD_CMD   alternative: command run after the map changes
  CODENDUM_WEB_METRICS_URL  vLLM metrics (default http://127.0.0.1:8000/metrics)
  CODENDUM_WEB_VLLM_CONTAINER  vLLM container read through the Docker socket for
                            the dashboard: engine options from docker inspect,
                            GPU figures from nvidia-smi (default codendum-vllm;
                            empty turns it off)
  CODENDUM_WEB_ACCESS_LOG   proxy access log (optional)
  CODENDUM_WEB_API_URL      public proxy URL shown to users (default https://{host}:8443)
  CODENDUM_WEB_CHAT_URL     Open WebUI URL shown to users (optional)
  CODENDUM_WEB_OPENWEBUI_URL  Open WebUI on loopback, for the panel itself (optional,
                            for example http://127.0.0.1:3000). Open WebUI must
                            trust the X-Codendum-Email, -User and -Role headers
                            (WEBUI_AUTH_TRUSTED_*_HEADER) and listen on loopback only.
  In both URLs, {host} is replaced with the host name the browser used, so the
  panel needs no change when the host address changes.
  CODENDUM_WEB_TLS_CHAIN    proxy certificate chain, read for the pin and /ca.pem
                            (default /proxy/tls/fullchain.pem; the last certificate
                            is served as the CA)
"""

from __future__ import annotations

import base64
import collections
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shlex
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "lib"))
import codendum as core  # noqa: E402  (metrics parser shared with the CLI)

ENV = os.environ.get
LISTEN = ENV("CODENDUM_WEB_LISTEN", "127.0.0.1:8090")
DB_PATH = ENV("CODENDUM_WEB_DB", "codendum-web.sqlite")
MAP_PATH = ENV("CODENDUM_WEB_MAP", "api-keys-web.map")
RELOAD_CMD = ENV("CODENDUM_WEB_RELOAD_CMD", "")
PROXY_CONTAINER = ENV("CODENDUM_WEB_PROXY_CONTAINER", "")
DOCKER_SOCKET = ENV("CODENDUM_WEB_DOCKER_SOCKET", "/var/run/docker.sock")
METRICS_URL = ENV("CODENDUM_WEB_METRICS_URL", "http://127.0.0.1:8000/metrics")
VLLM_CONTAINER = ENV("CODENDUM_WEB_VLLM_CONTAINER", "codendum-vllm")
ACCESS_LOG = ENV("CODENDUM_WEB_ACCESS_LOG", "")
API_URL = ENV("CODENDUM_WEB_API_URL", "https://{host}:8443")
CHAT_URL = ENV("CODENDUM_WEB_CHAT_URL", "")
OPENWEBUI_URL = ENV("CODENDUM_WEB_OPENWEBUI_URL", "").rstrip("/")
TLS_CHAIN = ENV("CODENDUM_WEB_TLS_CHAIN", "/proxy/tls/fullchain.pem")
HERE = os.path.dirname(os.path.abspath(__file__))
LOGO = os.path.join(HERE, "..", "docs", "_static", "codendum-logo.png")
SETUP_CODE_TTL = 600
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
# Values from docker inspect, nvidia-smi and /version shown on the dashboard.
FACT_RE = re.compile(r"^[A-Za-z0-9 ._:/@+-]{1,128}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# New accounts use the e-mail as user name (also the label in the key map and
# the access log); accounts created before keep their user name.
EMAIL_RE = re.compile(r"^[a-z0-9._%+-]{1,64}@[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+$")
CHECKS_PER_CLIENT = 60  # registration e-mail checks per address in LOGIN_WINDOW
HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])$")
SESSION_COOKIE = "codendum_session"
SESSION_TTL = 12 * 3600
DB_LOCK = threading.Lock()
# Failed logins in a sliding window. A classroom often shares one NAT address,
# so the per-address limit is generous; the per-username limit is strict.
LOGIN_WINDOW = 600
LOGIN_MAX_PER_USER = 5
LOGIN_MAX_PER_CLIENT = 30
FAILURES: Dict[Any, List[float]] = {}
FAILURES_LOCK = threading.Lock()
SETUP_TOKEN = ""  # one-time token for the first administrator, see main()


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


CONN = db()
CONN.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL,
    password TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'pending',
    api_key TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS setup_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""")
DEFAULT_SETTINGS = {"registration": "open", "domains": "", "auto_approve": "", "notice": ""}
DOMAIN_RE = re.compile(r"^[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+$")
NOTICE_MAX = 500


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 300_000)
    return "pbkdf2$" + salt.hex() + "$" + digest.hex()


def check_password(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)).split("$")[2], digest)


def public_url(template: str, host: str) -> str:
    """Fill {host} with the host name from the request (nginx passes $host)."""
    if host.startswith("["):
        host = host.split("]")[0] + "]"
    else:
        host = host.rsplit(":", 1)[0]
    return template.replace("{host}", host if HOST_RE.match(host) else "HOST")


def read_chain() -> List[bytes]:
    """DER certificates of the proxy chain, server first; empty if unreadable."""
    try:
        with open(TLS_CHAIN, encoding="ascii") as handle:
            text = handle.read()
    except OSError:
        return []
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----", text)
    return [ssl.PEM_cert_to_DER_cert(b) for b in blocks]


def _der(data: bytes, i: int) -> Tuple[int, int, int]:
    """One DER element at i: (tag, start of content, end of element)."""
    tag, length = data[i], data[i + 1]
    i += 2
    if length & 0x80:
        size = length & 0x7F
        length = int.from_bytes(data[i:i + size], "big")
        i += size
    return tag, i, i + length


def spki_pin(der: bytes) -> str:
    """curl --pinnedpubkey value: sha256 of the certificate's SubjectPublicKeyInfo."""
    _, cert, _ = _der(der, 0)
    _, i, end = _der(der, cert)  # tbsCertificate
    fields = []
    while i < end:
        tag, _, nxt = _der(der, i)
        fields.append((tag, i, nxt))
        i = nxt
    if fields[0][0] == 0xA0:  # explicit version
        fields = fields[1:]
    _, start, stop = fields[5]  # serial, signature, issuer, validity, subject, SPKI
    return "sha256//" + base64.b64encode(hashlib.sha256(der[start:stop]).digest()).decode()


def vllm_get(path: str) -> Any:
    with urllib.request.urlopen(METRICS_URL.rsplit("/metrics", 1)[0] + path, timeout=5) as resp:
        return json.loads(resp.read())


def model_entry() -> Optional[Dict[str, Any]]:
    """The first model of /v1/models, or None while vLLM is not ready."""
    try:
        data = vllm_get("/v1/models")["data"][0]
    except Exception:
        return None
    if not MODEL_RE.match(str(data.get("id", ""))) or not isinstance(data.get("max_model_len"), int):
        return None
    return data


def served_model() -> Optional[Tuple[str, int]]:
    """(model id, max_model_len) from vLLM, or None while it is not ready."""
    data = model_entry()
    return (data["id"], data["max_model_len"]) if data else None


def workstation_script(kind: str, user: sqlite3.Row, base: str, chain: List[bytes], model: Tuple[str, int]) -> str:
    model_id, context = model
    provider = core.opencode_provider("v2", base + "/v1", model_id, context, min(8192, context // 4))
    provider["settings"]["apiKey"] = "{file:~/.config/codendum/api-key}"
    with open(os.path.join(HERE, "setup-workstation." + kind), encoding="utf-8") as handle:
        text = handle.read()
    values = {"BASE": base, "PIN": spki_pin(chain[0]), "MODEL": model_id, "KEY": user["api_key"],
              "CA": ssl.DER_cert_to_PEM_cert(chain[-1]).strip(), "PROVIDER": json.dumps(provider, indent=2)}
    for name, value in values.items():
        text = text.replace("@@{}@@".format(name), value)
    return text.replace("\r\n", "\n").replace("\n", "\r\n") if kind == "ps1" else text


def take_setup_code(code: str) -> Optional[int]:
    """User id of a valid setup code, which is used up."""
    with DB_LOCK:
        row = CONN.execute("SELECT user_id FROM setup_codes WHERE code = ? AND expires > ?",
                           (code, time.time())).fetchone()
        CONN.execute("DELETE FROM setup_codes WHERE code = ? OR expires <= ?", (code, time.time()))
        CONN.commit()
    return row["user_id"] if row else None


def settings() -> Dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    values.update((r["key"], r["value"]) for r in query("SELECT key, value FROM settings"))
    return values


def domains_of(text: str) -> List[str]:
    return [d.lstrip("@") for d in re.split(r"[\s,;]+", text.lower()) if d.lstrip("@")]


def csrf_of(session_token: str) -> str:
    """Form token for one session: only pages of that session can know it."""
    return hmac.new(session_token.encode(), b"codendum-csrf", hashlib.sha256).hexdigest()


def new_key() -> str:
    return "cdm_" + secrets.token_hex(32)


def query(sql: str, args: Tuple = ()) -> List[sqlite3.Row]:
    with DB_LOCK:
        cur = CONN.execute(sql, args)
        CONN.commit()
        return cur.fetchall()


def too_many_failures(keys: List[Tuple[Any, int]]) -> bool:
    cutoff = time.time() - LOGIN_WINDOW
    with FAILURES_LOCK:
        blocked = False
        for key, limit in keys:
            recent = [t for t in FAILURES.get(key, []) if t > cutoff]
            if recent:
                FAILURES[key] = recent
            else:
                FAILURES.pop(key, None)
            blocked = blocked or len(recent) >= limit
        return blocked


def record_failure(keys: List[Tuple[Any, int]]) -> None:
    with FAILURES_LOCK:
        for key, _ in keys:
            FAILURES.setdefault(key, []).append(time.time())


def write_key_map() -> None:
    """Rewrite the nginx map with the keys of active users, then reload."""
    rows = query("SELECT username, api_key FROM users WHERE status = 'active' AND api_key IS NOT NULL ORDER BY username")
    lines = ["# Generated by codendum-web on {}. Secret: do not commit or share.\n".format(now_iso())]
    lines += ['"Bearer {}" "{}";\n'.format(r["api_key"], r["username"]) for r in rows]
    tmp = MAP_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    os.replace(tmp, MAP_PATH)
    if PROXY_CONTAINER:
        try:
            docker_exec(PROXY_CONTAINER, ["nginx", "-t", "-q"])
            docker_exec(PROXY_CONTAINER, ["nginx", "-s", "reload"])
        except Exception as exc:
            sys.stderr.write("proxy reload failed: {}\n".format(exc))
    elif RELOAD_CMD:
        result = subprocess.run(shlex.split(RELOAD_CMD), capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            sys.stderr.write("reload failed: {}\n".format(result.stderr.strip()))


def docker_api(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 30) -> Tuple[int, bytes]:
    """One call to the Docker Engine API on the socket (no CLI needed)."""
    import http.client
    import socket

    class UnixConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(DOCKER_SOCKET)

    conn = UnixConnection("localhost")
    if body is None:
        conn.request(method, path)
    else:
        conn.request(method, path, json.dumps(body), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def docker_exec(container: str, cmd: List[str], timeout: float = 30) -> bytes:
    """Run a command in a container and return its standard output."""
    status, data = docker_api("POST", "/containers/{}/exec".format(urllib.parse.quote(container)),
                              {"Cmd": cmd, "AttachStdout": True, "AttachStderr": True}, timeout)
    if status != 201:
        raise RuntimeError("exec create {}: {}".format(status, data[:200]))
    exec_id = json.loads(data)["Id"]
    status, data = docker_api("POST", "/exec/{}/start".format(exec_id), {"Detach": False, "Tty": False}, timeout)
    if status != 200:
        raise RuntimeError("exec start {}".format(status))
    _, info = docker_api("GET", "/exec/{}/json".format(exec_id), timeout=timeout)
    code = json.loads(info).get("ExitCode")
    if code != 0:
        raise RuntimeError("{} exited with {}".format(" ".join(cmd), code))
    # Without a TTY the output is multiplexed: frames with an 8-byte header
    # (stream, 3 zero bytes, big-endian length); stream 1 is stdout.
    out, i = b"", 0
    while i + 8 <= len(data):
        size = int.from_bytes(data[i + 4:i + 8], "big")
        if data[i] == 1:
            out += data[i + 8:i + 8 + size]
        i += 8 + size
    return out


def docker_ready() -> bool:
    return bool(VLLM_CONTAINER) and bool(CONTAINER_RE.match(VLLM_CONTAINER)) and os.path.exists(DOCKER_SOCKET)


ENGINE_OPTIONS = ("--max-model-len", "--max-num-seqs", "--max-num-batched-tokens", "--gpu-memory-utilization",
                  "--kv-cache-dtype", "--tool-call-parser")
ENGINE_SWITCHES = ("--enable-prefix-caching", "--enable-chunked-prefill")
ENGINE_CACHE: Dict[str, Any] = {"at": 0.0, "info": None}


def engine_info() -> Optional[Dict[str, Any]]:
    """Image, profile, state and the main serving options of the vLLM
    container, from docker inspect (cached for 30 seconds). Only these
    fields leave the function: the environment may hold secrets."""
    if not docker_ready():
        return None
    if time.time() - ENGINE_CACHE["at"] < 30:
        return ENGINE_CACHE["info"]
    info: Optional[Dict[str, Any]] = None
    try:
        status, data = docker_api("GET", "/containers/{}/json".format(urllib.parse.quote(VLLM_CONTAINER)), timeout=5)
        if status == 200:
            raw = json.loads(data)
            state = raw.get("State") or {}
            image = str((raw.get("Config") or {}).get("Image", ""))
            name, _, digest = image.partition("@")
            info = {"image": name + ("@" + digest[:19] if digest else ""),
                    "profile": ((raw.get("Config") or {}).get("Labels") or {}).get("io.codendum.profile"),
                    "state": state.get("Status"), "health": (state.get("Health") or {}).get("Status"),
                    "restarts": raw.get("RestartCount")}
            try:
                info["started"] = dt.datetime.strptime(str(state.get("StartedAt", ""))[:19], "%Y-%m-%dT%H:%M:%S") \
                    .replace(tzinfo=dt.timezone.utc).timestamp()
            except ValueError:
                pass
            args = [str(a) for a in raw.get("Args") or []]
            for i, arg in enumerate(args):
                option, equals, value = arg.partition("=")
                if option in ENGINE_OPTIONS:
                    info[option[2:]] = value if equals else (args[i + 1] if i + 1 < len(args) else "")
                elif arg in ENGINE_SWITCHES:
                    info[arg[2:]] = True
            info = {k: v for k, v in info.items()
                    if isinstance(v, (bool, int, float)) or (isinstance(v, str) and FACT_RE.match(v))}
    except Exception:  # no Docker socket or no such container: nothing shown
        info = None
    ENGINE_CACHE.update(at=time.time(), info=info)
    return info


GPU_QUERY = ["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw,clocks.sm",
             "--format=csv,noheader,nounits"]
GPU_NAME = {"name": ""}


def gpu_sample() -> Dict[str, Optional[float]]:
    """Utilisation, temperature, power and SM clock of the first GPU, from
    nvidia-smi in the vLLM container. Fields the GPU does not report are None."""
    line = docker_exec(VLLM_CONTAINER, GPU_QUERY, timeout=5).decode("utf-8", "replace").strip().splitlines()[0]
    fields = [f.strip() for f in line.split(",")]
    if len(fields) != 5:
        raise RuntimeError("unexpected nvidia-smi output")
    if FACT_RE.match(fields[0]):
        GPU_NAME["name"] = fields[0]

    def number(value: str) -> Optional[float]:
        try:
            return round(float(value), 1)
        except ValueError:  # [N/A]
            return None

    return dict(zip(("gpu", "temp", "power", "clock"), map(number, fields[1:])))


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

SAMPLE_EVERY = 5
HISTORY: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=720)  # one hour
HISTORY_LOCK = threading.Lock()
LAST_REQUEST: Dict[str, Optional[int]] = {"t": None}  # last sample with vLLM at work


def sampler() -> None:
    """Samples vLLM every SAMPLE_EVERY seconds for the dashboard chart; rates
    come from the counters of two samples in a row."""
    previous: Optional[Tuple[float, Dict[str, Optional[float]]]] = None
    gpu_retry = 0.0
    while True:
        now = time.time()
        point: Dict[str, Any] = {"t": int(now), "up": 0}
        try:
            samples = core.fetch_metrics(METRICS_URL, timeout=3)
            summary = core.summarize_metrics(samples)
            # vLLM before 0.11 called the inter-token latency time_per_output_token.
            itl = ("vllm:inter_token_latency_seconds", "vllm:time_per_output_token_seconds")
            counters = {"prompt": summary["prompt_tokens_total"], "gen": summary["generation_tokens_total"],
                        "done": sum(summary["requests_finished"].values()),
                        "ttft_sum": core._pick(samples, ("vllm:time_to_first_token_seconds_sum",)),
                        "ttft_count": core._pick(samples, ("vllm:time_to_first_token_seconds_count",)),
                        "itl_sum": core._pick(samples, tuple(n + "_sum" for n in itl)),
                        "itl_count": core._pick(samples, tuple(n + "_count" for n in itl))}
            point.update(up=1, running=summary["requests_running"], waiting=summary["requests_waiting"],
                         kv=summary["kv_cache_usage_percent"])
            if summary["requests_running"] or (previous and counters["gen"] != previous[1]["gen"]):
                LAST_REQUEST["t"] = int(now)
            if previous:
                elapsed, old = now - previous[0], previous[1]

                def delta(key: str) -> Optional[float]:
                    a, b = counters[key], old[key]
                    return None if a is None or b is None or a < b else a - b  # None after a restart

                gen, prompt, done = delta("gen"), delta("prompt"), delta("done")
                ttft_sum, ttft_count = delta("ttft_sum"), delta("ttft_count")
                itl_sum, itl_count = delta("itl_sum"), delta("itl_count")
                point.update(gen_tps=None if gen is None else round(gen / elapsed, 1),
                             prompt_tps=None if prompt is None else round(prompt / elapsed, 1),
                             req_min=None if done is None else round(done * 60 / elapsed, 1),
                             ttft=round(ttft_sum / ttft_count, 3) if ttft_sum is not None and ttft_count else None,
                             itl=round(itl_sum / itl_count, 4) if itl_sum is not None and itl_count else None)
            previous = (now, counters)
        except Exception:  # down or unreachable: a gap in the chart
            previous = None
        memory = core.host_memory()
        if memory:
            point["mem"] = round(memory["mem_total_gib"] - memory["mem_available_gib"], 1)
        if docker_ready() and now >= gpu_retry:
            try:
                point.update(gpu_sample())
            except Exception:  # no nvidia-smi or container down: try again in a minute
                gpu_retry = now + 60
        with HISTORY_LOCK:
            HISTORY.append(point)
        time.sleep(SAMPLE_EVERY)


def recent_users() -> List[Tuple[str, int]]:
    """API requests per user in the last 10 minutes, from the proxy access log."""
    counts: Dict[str, int] = {}
    if ACCESS_LOG and os.path.exists(ACCESS_LOG):
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        with open(ACCESS_LOG, encoding="utf-8", errors="replace") as handle:
            for line in handle.readlines()[-20000:]:
                match = re.match(r"^(\S+) \S+ user=(\S*) ", line)
                if not match or not match.group(2):
                    continue
                try:
                    stamp = dt.datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if stamp >= cutoff:
                    counts[match.group(2)] = counts.get(match.group(2), 0) + 1
    return sorted(counts.items(), key=lambda item: -item[1])


def service_status() -> Dict[str, Any]:
    status: Dict[str, Any] = {"vllm": "down", "running": None, "waiting": None, "kv": None, "top": []}
    try:
        summary = core.summarize_metrics(core.fetch_metrics(METRICS_URL, timeout=3))
        status.update(vllm="up", running=summary["requests_running"], waiting=summary["requests_waiting"],
                      kv=summary["kv_cache_usage_percent"], prefix_hit=summary["prefix_cache_hit_rate_percent"],
                      preemptions=summary["preemptions_total"], kv_tokens=summary["kv_cache_capacity_tokens"],
                      ttft_mean=summary["ttft_mean_seconds"], e2e_mean=summary["e2e_latency_mean_seconds"],
                      queue_mean=summary["queue_time_mean_seconds"], finished=summary["requests_finished"],
                      prompt_total=summary["prompt_tokens_total"], gen_total=summary["generation_tokens_total"])
        model = model_entry()
        if model:
            status.update(model=model["id"], context=model["max_model_len"])
            if FACT_RE.match(str(model.get("root", ""))):
                status["weights"] = model["root"]
        version = str(vllm_get("/version").get("version", ""))
        if FACT_RE.match(version):
            status["version"] = version
    except Exception:  # down or unreachable: shown as such
        pass
    status["engine"] = engine_info()
    status["gpu_name"], status["last_request"] = GPU_NAME["name"], LAST_REQUEST["t"]
    memory = core.host_memory()
    if memory:
        status.update(mem_total=memory["mem_total_gib"],
                      mem_used=round(memory["mem_total_gib"] - memory["mem_available_gib"], 1))
    recent = recent_users()
    status["top"], status["active_users"] = recent[:5], len(recent)
    status["people"] = {r["status"]: r["n"] for r in query("SELECT status, count(*) AS n FROM users GROUP BY status")}
    with HISTORY_LOCK:
        status["history"] = list(HISTORY)
    return status


# --------------------------------------------------------------------------
# Open WebUI
# --------------------------------------------------------------------------

OWUI_TOKENS: Dict[str, Tuple[str, float]] = {}


def owui_call(method: str, path: str, body: Any = None, token: str = "",
              headers: Optional[Dict[str, str]] = None) -> Any:
    request = urllib.request.Request(OPENWEBUI_URL + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"null")


def owui_role(user: sqlite3.Row) -> str:
    if user["status"] != "active":
        return "pending"
    return "admin" if user["role"] == "admin" else "user"


def owui_signin(user: sqlite3.Row, role: str) -> str:
    """Signs in to Open WebUI as this user with the trusted headers, as nginx
    does for the browser. Open WebUI also sets the role it is given."""
    email = user["email"].lower()
    result = owui_call("POST", "/api/v1/auths/signin", {"email": email, "password": ""},
                       headers={"X-Codendum-Email": email, "X-Codendum-User": user["username"],
                                "X-Codendum-Role": role})
    if role == "pending":  # Open WebUI answers pending accounts without a session
        return ""
    if not isinstance(result, dict) or not result.get("token"):
        raise RuntimeError("Open WebUI gave no session (multi-factor authentication?)")
    if result.get("role") != role:
        raise RuntimeError("Open WebUI did not apply the role: set WEBUI_AUTH_TRUSTED_ROLE_HEADER=X-Codendum-Role")
    return str(result["token"])


def owui_admin_token(admin: sqlite3.Row) -> str:
    cached = OWUI_TOKENS.get(admin["email"].lower())
    if cached and cached[1] > time.time():
        return cached[0]
    token = owui_signin(admin, "admin")
    OWUI_TOKENS[admin["email"].lower()] = (token, time.time() + 600)
    return token


def sync_owui_role(user: sqlite3.Row, role: str) -> None:
    """Applies a role change in Open WebUI at once (otherwise at the next chat login)."""
    if OPENWEBUI_URL:
        OWUI_TOKENS.pop(user["email"].lower(), None)
        try:
            owui_signin(user, role)
        except Exception as exc:
            sys.stderr.write("Open WebUI role of {}: {}\n".format(user["username"], exc))


def owui_web_search(admin: sqlite3.Row, enable: Optional[bool] = None) -> bool:
    """Reads, or sets, whether the chat can search the web."""
    token = owui_admin_token(admin)
    if enable is not None:
        owui_call("POST", "/api/v1/retrieval/config/update", {"web": {"ENABLE_WEB_SEARCH": enable}}, token)
    return bool(owui_call("GET", "/api/v1/retrieval/config", token=token)["web"]["ENABLE_WEB_SEARCH"])


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

CSS = """
:root{--bg:#f6f7fb;--card:#fff;--ink:#1d2433;--muted:#667085;--line:#e4e7ec;--brand:#3155d0;
--link:#1d3a9e;--ok:#12a87a;--warn:#e6622f;--pend:#b98900}
@media (prefers-color-scheme:dark){:root{--bg:#0f1420;--card:#171d2b;--ink:#e8ecf4;--muted:#98a2b3;--line:#273044;
--link:#a9bfff}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}
a{color:var(--link);text-decoration:underline;text-underline-offset:2px}a.btn{text-decoration:none}
/* The logo is blue on transparent: keep the header light in both themes. */
header{display:flex;align-items:center;justify-content:space-between;padding:14px 24px;background:#fff;color:#1d2433;
border-bottom:3px solid var(--brand);box-shadow:0 2px 10px rgba(16,24,40,.18)}
.wide{display:block;width:100%;text-align:center;padding:9px 12px}
.switch{margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}.switch p{margin:0 0 8px;color:var(--ink)}
a.btn.wide{color:var(--link);border:2px solid var(--link);font-weight:600}
header button{background:#fff;color:#1d2433;border-color:#d0d5dd}header .muted{color:#475467}
header a{color:#325bd1;font-weight:600}  /* the colour of the word in the logo */
header img{height:40px}main{max-width:980px;margin:24px auto;padding:0 16px;display:grid;gap:18px}
footer{max-width:980px;margin:8px auto 28px;padding:0 16px;text-align:center;color:var(--muted);font-size:13px;line-height:1.8}
footer a{color:inherit}footer code{font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
h2{font-size:16px;margin:0 0 12px}code,.key{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.key{display:block;padding:10px;border:1px dashed var(--line);border-radius:8px;word-break:break-all;margin:8px 0}
pre{background:var(--bg);padding:10px;border-radius:8px;overflow-x:auto;font-size:13px}
button,.btn{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:6px 12px;border-radius:8px;cursor:pointer;text-decoration:none}
button.primary,.btn.primary{background:var(--brand);border-color:var(--brand);color:#fff}
table{width:100%;border-collapse:collapse}td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;font-size:14px}
th{color:var(--muted);font-weight:500}form.inline{display:inline}
.badge{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid currentColor}
.active{color:var(--ok)}.pending{color:var(--pend)}.blocked{color:var(--warn)}
input{font:inherit;width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);margin:4px 0 12px}
.narrow{width:100%;max-width:380px;margin:60px auto}.muted{color:var(--muted)}.err{color:var(--warn)}
.hint{min-height:1.4em;margin:-6px 0 10px;font-size:13px}.hint.ok{color:var(--ok)}
h3{font-size:15px;margin:18px 0 10px}
.steps{list-style:none;padding:0;margin:0;display:grid;gap:12px}
.step{border:1px solid var(--line);border-left:4px solid var(--brand);border-radius:10px;padding:14px 16px}
.step-head{display:flex;gap:12px;align-items:center}.step-head b{display:block}.step-head .muted{font-size:13px}
.num{flex:none;width:32px;height:32px;border-radius:50%;background:var(--brand);color:#fff;font-weight:700;
display:flex;align-items:center;justify-content:center}
.step-body{margin-left:44px}.step-body p{margin:10px 0 4px}
.os{font-weight:600;font-size:13px}.os span{font-weight:400;color:var(--muted);margin-left:4px}
@media (max-width:600px){.step-body{margin-left:0}}
.copy{display:flex;gap:8px;align-items:flex-start}.copy pre{flex:1;margin:0;white-space:pre-wrap;word-break:break-all}
details{margin-top:8px}summary{cursor:pointer;color:var(--link)}
nav{display:flex;gap:14px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
textarea{font:inherit;width:100%;min-height:70px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--bg);color:var(--ink);margin:4px 0 12px}
label.check{display:flex;gap:8px;align-items:center;margin:4px 0}label.check input{width:auto;margin:0}
.notice{border-left:4px solid var(--brand);white-space:pre-line}.ok-msg{color:var(--ok)}
.field-help{margin:-8px 0 12px;font-size:13px}.narrow-card{max-width:480px}
.tabs{display:flex;gap:4px;justify-content:flex-start;border-bottom:1px solid var(--line)}.tabs a{padding:8px 14px;text-decoration:none;
color:var(--muted);border-bottom:3px solid transparent;margin-bottom:-1px;font-weight:600}
.tabs a.on{color:var(--ink);border-bottom-color:var(--brand)}.tabs .count{background:var(--pend);color:#fff;
border-radius:999px;padding:0 7px;font-size:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
.tile{border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:0}
.tile b{display:block;font-size:22px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tile span{color:var(--muted);font-size:12px}.small{font-size:12px;font-weight:400}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(340px,100%),1fr));gap:0 28px;margin:0}
.facts div{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid var(--line);padding:7px 0;min-width:0}
.facts dt{color:var(--muted)}.facts dd{margin:0;font-weight:600;text-align:right;overflow-wrap:anywhere}
canvas{display:block;width:100%;height:170px}canvas.short{height:120px}
.chart-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.chart-head h2{margin:0}
.range{display:flex}.range button{border-radius:0;margin-left:-1px;font-size:13px;padding:4px 10px}
.range button:first-child{border-radius:8px 0 0 8px}.range button:last-child{border-radius:0 8px 8px 0}
.range button.on{background:var(--brand);border-color:var(--brand);color:#fff}.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin-top:8px}
.legend i{display:inline-block;width:12px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.scroll{overflow-x:auto}
.menu{position:relative}.menu summary{list-style:none;display:flex;gap:6px;align-items:center;color:#1d2433;
padding:6px 10px;border:1px solid #d0d5dd;border-radius:8px}.menu summary::-webkit-details-marker{display:none}
.menu summary::after{content:"";border:5px solid transparent;border-top-color:currentColor;margin-top:5px}
.menu-list{position:absolute;right:0;top:calc(100% + 6px);z-index:10;min-width:220px;background:#fff;
border:1px solid #d0d5dd;border-radius:10px;box-shadow:0 8px 24px rgba(16,24,40,.18);padding:8px;display:grid;gap:2px}
.menu-list>*{padding:8px 10px;border-radius:6px}.menu-list a:hover{background:#f2f4f7}
.menu-list form{padding:0}.menu-list button{width:100%;text-align:left;border:0;background:none;padding:8px 10px;color:#1d2433}
.menu-list button:hover{background:#f2f4f7}.menu-list .muted{font-size:13px;border-bottom:1px solid #eaecf0;
border-radius:0;word-break:break-all}
.menu-list .small-only{display:none}
@media (max-width:600px){header{padding:10px 14px}header img{height:32px}.menu summary span,nav>a{display:none}
.menu-list .small-only{display:block}}
"""


PERSON_ICON = ('<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
               'aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7"/></svg>')
# Closes the profile menu on a click outside it.
MENU_JS = ("document.addEventListener('click',function(e){document.querySelectorAll('details.menu[open]')"
           ".forEach(function(d){if(!d.contains(e.target))d.removeAttribute('open')})});")


def source_commit() -> str:
    """Commit of the repository the panel runs from, read from .git without
    the git command; empty when there is no repository."""
    git = os.path.join(HERE, "..", ".git")
    try:
        with open(os.path.join(git, "HEAD"), encoding="ascii") as handle:
            head = handle.read().strip()
        if not head.startswith("ref: "):
            sha = head
        elif os.path.exists(os.path.join(git, head[5:])):
            with open(os.path.join(git, head[5:]), encoding="ascii") as handle:
                sha = handle.read().strip()
        else:
            with open(os.path.join(git, "packed-refs"), encoding="ascii") as handle:
                sha = next((line.split()[0] for line in handle if line.rstrip().endswith(" " + head[5:])), "")
    except (OSError, UnicodeDecodeError):
        return ""
    return sha if re.match(r"^[0-9a-f]{40}$", sha) else ""


COMMIT = source_commit()
REPOSITORY = "https://github.com/stefanoferi/codendum"
FOOTER = ('<footer>Codendum © 2026 Stefano Noferi · Apache License 2.0 · <a href="https://codendum.ai/">codendum.ai</a>'
          ' · <a href="{r}">GitHub</a>{c}</footer>').format(
              r=REPOSITORY, c=' · <a href="{}/commit/{}" title="{}"><code>{}</code></a>'.format(
                  REPOSITORY, COMMIT, COMMIT, COMMIT[:7]) if COMMIT else "")


def page(title: str, body: str, user: Optional[sqlite3.Row] = None, csrf: str = "") -> bytes:
    right = ""
    if user:
        nav = ('<a href="/">My access</a> <a href="/admin">Administration</a> '
               if user["role"] == "admin" and user["status"] == "active" else "")
        right = ('<nav>{}<details class="menu"><summary aria-label="Profile menu">{icon}<span>{name}</span>'
                 '</summary><div class="menu-list"><span class="muted">{name}</span>{small}<a href="/profile">'
                 'Profile and password</a><form method="post" action="/logout"><button>Log out</button></form></div>'
                 '</details></nav>').format(nav, icon=PERSON_ICON, name=html.escape(user["username"]),
                                            small=nav.replace("<a ", '<a class="small-only" '))
    if csrf:
        right, body = (re.sub(r'(<form [^>]*method="post"[^>]*>)',
                              r'\1<input type=hidden name=csrf value="{}">'.format(csrf), part)
                       for part in (right, body))
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>{t} · Codendum</title>"
            "<style>{css}</style></head><body><header><img src='/logo.png' alt='Codendum'>{r}</header>"
            "<main>{b}</main>{f}<script>function copy(id){{navigator.clipboard.writeText("
            "document.getElementById(id).innerText)}}{menu}</script></body></html>").format(
                t=html.escape(title), css=CSS, r=right, b=body, f=FOOTER, menu=MENU_JS).encode()


def login_form(error: str = "", register: bool = False, setup: str = "", next_url: str = "") -> str:
    action, label = ("/register", "Create account") if register else ("/login", "Log in")
    extra = '<input type=hidden name=next value="{}">'.format(html.escape(next_url)) if next_url else ""
    if register and setup:
        label = "Create the administrator account"
        extra += '<input type=hidden name=setup value="{}">'.format(html.escape(setup))
    switch = ('<p>Already registered?</p><a class="btn wide" href="/login">Log in</a>' if register
              else '<p>No account yet?</p><a class="btn wide" href="/register">Register</a>'
              if settings()["registration"] == "open" else "")
    if register and not setup and settings()["registration"] != "open":
        return ('<div class="card narrow"><h2>Registration is closed</h2><p>Ask the administrator.</p>'
                '<div class="switch">{}</div></div>').format(switch)
    err = '<p class="err">{}</p>'.format(html.escape(error)) if error else ""
    return ('<div class="card narrow"><h2>{label}</h2>{err}<form method="post" action="{action}">'
            '<label>E-mail<input id=email name=email required inputmode=email autocomplete=username></label>'
            '{hint}{extra}<label>Password<input name=password type=password required minlength=8></label>'
            '<button class="primary wide">{label}</button></form>{switch}</div>{script}').format(
        label=label, err=err, action=action, extra=extra,
        switch='<div class="switch">{}</div>'.format(switch) if switch else "",
        hint='<p class="hint" id="email-msg" aria-live="polite"></p>' if register else "",
        script=EMAIL_CHECK_JS if register else "")


# Checks the e-mail while it is typed (POST, so that addresses stay out of URLs and logs).
EMAIL_CHECK_JS = """<script>(function(){var e=document.getElementById("email"),m=document.getElementById("email-msg"),t;
e.addEventListener("input",function(){clearTimeout(t);m.textContent="";t=setTimeout(async function(){
var v=e.value.trim();if(!v)return;try{var r=await(await fetch("/register/check",{method:"POST",
headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"email="+encodeURIComponent(v)})).json();
m.textContent=r.message;m.className="hint "+(r.ok?"ok":"err")}catch(x){}},400)})})()</script>"""


def email_problem(email: str, first: bool = False) -> str:
    """Why an e-mail cannot be registered, or "" if it can. The first
    administrator (setup link) is not bound by the registration settings."""
    conf = settings()
    if not first and conf["registration"] != "open":
        return "Registration is closed."
    if len(email) > 254 or not EMAIL_RE.match(email):
        return "This is not a valid e-mail address."
    domains = domains_of(conf["domains"])
    if not first and domains and email.rsplit("@", 1)[1] not in domains:
        return "Only addresses at {} can register.".format(", ".join(domains))
    if query("SELECT 1 FROM users WHERE username = ? OR lower(email) = ?", (email, email)):
        return "This e-mail is already registered: log in instead."
    return ""


def user_card(user: sqlite3.Row, host: str) -> str:
    if user["status"] == "pending":
        return ('<div class="card"><h2>Waiting for approval</h2><p>Your account <b>{}</b> was created. '
                'An administrator will approve it shortly; reload this page then.</p></div>').format(
            html.escape(user["username"]))
    if user["status"] == "blocked":
        return '<div class="card"><h2>Account blocked</h2><p>Contact the administrator.</p></div>'
    base = public_url(API_URL, host)
    chat = ('<p><a class="btn primary" href="{}" target="_blank" rel="noopener">Open the chat</a></p>'.format(
        html.escape(public_url(CHAT_URL, host)))
            if CHAT_URL else "")
    chain = read_chain()
    if not chain:
        connect = ('<p>Ask the administrator: the panel cannot read the server certificate, so it cannot create '
                   'the setup command.</p>')
    else:
        rows = query("SELECT code, expires FROM setup_codes WHERE user_id = ? AND expires > ? "
                     "ORDER BY expires DESC LIMIT 1", (user["id"], time.time()))
        if rows:
            pin, url = spki_pin(chain[0]), "{}/s/{}".format(base, rows[0]["code"])
            minutes = max(1, int((rows[0]["expires"] - time.time()) // 60))
            connect = ('<p class="os">macOS · Linux <span>Terminal</span></p>{sh}<p class="os">Windows '
                       '<span>PowerShell</span></p>{ps}<p class="muted">Windows needs <code>curl.exe</code> 7.58.1 or '
                       'later (up-to-date Windows 10 and 11). The command works once, for {m} more minutes, and it is '
                       'personal: it contains your key.</p>'
                       '<form method="post" action="/setup/new"><button>New command</button></form>').format(
                sh=copyable("cmd-sh", "curl -fsSk --pinnedpubkey '{}' {} | sh".format(pin, url)),
                ps=copyable("cmd-ps", "curl.exe -fsSk --pinnedpubkey '{}' {}.ps1 | Out-String | Invoke-Expression".format(
                    pin, url)), m=minutes)
        else:
            connect = ('<p>Create a command to paste on your computer. It works once, for 10 minutes.</p>'
                       '<form method="post" action="/setup/new"><button class="primary">Create the setup command'
                       '</button></form>')
    return ('<div class="card"><h2>Your access</h2>{chat}<h3>Set up OpenCode on your computer</h3><ol class="steps">'
            + step(1, "Install OpenCode", "Once per computer.",
                   '<p class="os">macOS · Linux <span>Terminal</span></p>{inst_sh}'
                   '<p class="os">Windows <span>PowerShell</span></p>{inst_ps}'
                   '<p class="muted">Then open a new terminal window.</p>')
            + step(2, "Connect OpenCode to Codendum", "Saves your key and the server certificate, and selects "
                   "the model.", "{connect}")
            + step(3, "Start coding", "In your project folder:",
                   '{start}<p class="muted">macOS 15 or later: if OpenCode reports <code>EHOSTUNREACH</code>, allow it '
                   '(and your terminal app) in System Settings → Privacy &amp; Security → Local Network, then restart '
                   'it.</p>')
            + '</ol>'
            '<details><summary>API key and certificate, for other tools</summary>'
            '<span class="key" id="key">{key}</span><button onclick="copy(\'key\')">Copy</button> '
            '<form class="inline" method="post" action="/key/regenerate"><button>Regenerate</button></form>'
            '<p>API: <code>{base}/v1</code> · certificate: <a href="/ca.pem">ca.pem</a></p>'
            '<p class="muted">Keep the key private. Regenerating it disables the old one immediately; then run '
            'step 2 again.</p></details></div>').format(
        chat=chat, inst_sh=copyable("inst-sh", "curl -fsSL https://opencode.ai/v2/install | bash"),
        start=copyable("start", "opencode"),
        inst_ps=copyable("inst-ps", WINDOWS_INSTALL), connect=connect, key=html.escape(user["api_key"]),
        base=html.escape(base))


# Official Windows zip (opencode.ai/v2/docs), unpacked per user and added to PATH.
WINDOWS_INSTALL = """$d = "$env:LOCALAPPDATA\\Programs\\opencode"
$a = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
curl.exe -fsSL -o "$env:TEMP\\opencode.zip" "https://opencode.ai/update/api/latest/cli/opencode/opencode-windows-$a.zip"
Expand-Archive -Force "$env:TEMP\\opencode.zip" $d
$bin = (Get-ChildItem -Recurse -Filter opencode.exe $d | Select-Object -First 1).DirectoryName
$p = [Environment]::GetEnvironmentVariable('Path', 'User')
if ("$p" -notlike "*$bin*") { [Environment]::SetEnvironmentVariable('Path', "$p;$bin", 'User') }
$env:Path += ";$bin"; opencode --version"""


def step(number: int, title: str, lead: str, content: str) -> str:
    return ('<li class="step"><div class="step-head"><span class="num" aria-hidden="true">{}</span><div><b>{}</b>'
            '<span class="muted">{}</span></div></div><div class="step-body">{}</div></li>').format(
        number, title, lead, content)


def copyable(element_id: str, text: str) -> str:
    return '<div class="copy"><pre id="{i}">{t}</pre><button onclick="copy(\'{i}\')">Copy</button></div>'.format(
        i=element_id, t=html.escape(text))


def profile_card(user: sqlite3.Row, message: str = "", error: str = "") -> str:
    flash = ('<p class="err">{}</p>'.format(html.escape(error)) if error else
             '<p class="ok-msg">{}</p>'.format(html.escape(message)) if message else "")
    status = {"active": "Approved", "pending": "Waiting for approval", "blocked": "Blocked"}.get(user["status"], "")
    return ('<div class="card"><h2>Profile</h2><table><tr><th>E-mail</th><td>{email}</td></tr>'
            '<tr><th>Role</th><td>{role}</td></tr><tr><th>Status</th><td><span class="badge {st}">{status}</span></td>'
            '</tr><tr><th>Registered (UTC)</th><td>{reg}</td></tr></table></div>'
            '<div class="card narrow-card"><h2>Change password</h2>{flash}<form method="post" action="/profile/password">'
            '<input type=hidden name=username value="{email}" autocomplete=username>'
            '<label>Current password<input name=current type=password required autocomplete=current-password></label>'
            '<label>New password<input name=password type=password required minlength=8 '
            'autocomplete=new-password></label><label>New password again<input name=repeat type=password required '
            'minlength=8 autocomplete=new-password></label><button class="primary">Change password</button></form>'
            '<p class="muted">Your other sessions are logged out; this one stays open.</p></div>').format(
        email=html.escape(user["email"].lower()), role="Administrator" if user["role"] == "admin" else "User",
        st=user["status"], status=status, reg=user["created_at"][:16].replace("T", " "), flash=flash)


def admin_tabs(active: str) -> str:
    pending = len(query("SELECT 1 FROM users WHERE status = 'pending'"))
    tabs = [("/admin", "Dashboard"), ("/admin/people", "People"), ("/admin/settings", "Settings")]
    return '<nav class="tabs">{}</nav>'.format("".join(
        '<a href="{}"{}>{}{}</a>'.format(href, ' class="on" aria-current="page"' if href == active else "", label,
                                         ' <span class="count">{}</span>'.format(pending)
                                         if label == "People" and pending else "")
        for href, label in tabs))


def flash_of(message: str, error: str) -> str:
    return ('<p class="err">{}</p>'.format(html.escape(error)) if error else
            '<p class="ok-msg">{}</p>'.format(html.escape(message)) if message else "")


def dashboard() -> str:
    return admin_tabs("/admin") + (
        '<div class="card"><h2>Service <span class="muted small" id="updated"></span></h2>'
        '<div class="tiles" id="tiles">loading…</div></div>'
        '<div class="card"><div class="chart-head"><h2>Activity</h2><div class="range" role="group" '
        'aria-label="Time range"><button data-r="300">5 min</button><button data-r="900" class="on">15 min</button>'
        '<button data-r="3600">1 hour</button></div></div>'
        '<h3>Tokens per second</h3><canvas id="c-tps"></canvas><div class="legend" id="l-tps"></div>'
        '<h3>Requests</h3><canvas id="c-req"></canvas><div class="legend" id="l-req"></div>'
        '<h3>KV cache and GPU</h3><canvas id="c-kv" class="short"></canvas><div class="legend" id="l-kv"></div></div>'
        '<div class="card"><h2>Model and engine</h2><dl class="facts" id="engine"></dl></div>'
        '<div class="card"><h2>Since vLLM started</h2><dl class="facts" id="totals"></dl></div>'
        '<div class="card"><h2>People</h2><div class="tiles" id="people"></div><p class="muted" id="top"></p></div>'
    ) + DASHBOARD_JS


# Live dashboard: polls /api/status every 5 seconds and draws the last hour on
# two canvases (no chart library).
DASHBOARD_JS = """<script>
const F=(v,d)=>v===null||v===undefined?'–':(d===undefined?v:Number(v).toFixed(d));
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const E=v=>String(v).replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';');
const N=v=>v==null?'–':Math.round(v).toLocaleString('en');
const U=(v,u,d)=>v==null?'–':F(v,d||0)+u;
function dur(s){if(s==null)return '–';const d=Math.floor(s/86400),h=Math.floor(s%%86400/3600),m=Math.floor(s%%3600/60);
 return d?`${d} d ${h} h`:h?`${h} h ${m} min`:`${m} min`}
function tile(v,l,cls){return `<div class="tile"><b class="${cls||''}">${E(v)}</b><span>${l}</span></div>`}
function facts(rows){return rows.filter(r=>r[1]!=null&&r[1]!=='').map(r=>`<div><dt>${r[0]}</dt><dd>${E(r[1])}</dd></div>`).join('')}
let range=900,status=null;
function draw(id,legendId,all,series,fixedTop){
 const c=document.getElementById(id),dpr=window.devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;
 c.width=w*dpr;c.height=h*dpr;const g=c.getContext('2d');g.scale(dpr,dpr);g.clearRect(0,0,w,h);
 const L=44,R=10,T=8,B=22,now=Date.now()/1000,t0=now-range,x=t=>L+(t-t0)/range*(w-L-R),points=all.filter(p=>p.t>=t0);
 let max=4;series.forEach(s=>points.forEach(p=>{if(p[s.k]!=null)max=Math.max(max,p[s.k])}));
 const step=Math.pow(10,Math.floor(Math.log10(max/4))),top=fixedTop||Math.ceil(max/4/step)*step*4,y=v=>T+(1-v/top)*(h-T-B);
 g.font='11px system-ui,sans-serif';g.strokeStyle=css('--line');g.fillStyle=css('--muted');g.lineWidth=1;
 for(let i=0;i<=4;i++){const v=top*i/4,yy=Math.round(y(v))+.5;g.beginPath();g.moveTo(L,yy);g.lineTo(w-R,yy);g.stroke();
  g.textAlign='right';g.fillText(Number.isInteger(v)?v:v.toFixed(1),L-6,yy+4)}
 const mins=range/60,tick=mins<=5?1:mins<=15?5:15;g.textAlign='center';
 for(let m=mins;m>=0;m-=tick)g.fillText(m?`-${m} min`:'now',Math.min(Math.max(x(now-m*60),L+18),w-R-14),h-6);
 series.forEach(s=>{g.strokeStyle=s.c;g.lineWidth=2;g.lineJoin='round';g.beginPath();let on=false,prev=0;
  points.forEach(p=>{const v=p[s.k];if(v==null||p.t-prev>3*%d){on=false}
   if(v!=null){const X=x(p.t),Y=y(v);on?g.lineTo(X,Y):g.moveTo(X,Y);on=true}prev=p.t});g.stroke()});
 const last=points.length?points[points.length-1]:{},peak=k=>points.reduce((m,p)=>p[k]!=null&&(m==null||p[k]>m)?p[k]:m,null);
 document.getElementById(legendId).innerHTML=series.map(s=>`<span><i style="background:${s.c}"></i>${s.l}: <b>${F(last[s.k])}</b>${s.u||''}`+
  ` <span class="muted">· peak ${F(peak(s.k))}${s.u||''}</span></span>`).join('');
}
function charts(){if(!status)return;const h=status.history||[];
 draw('c-tps','l-tps',h,[{k:'gen_tps',l:'output',c:css('--brand')},{k:'prompt_tps',l:'input',c:css('--ok')}]);
 draw('c-req','l-req',h,[{k:'running',l:'running',c:css('--brand')},{k:'waiting',l:'waiting',c:css('--warn')}]);
 draw('c-kv','l-kv',h,[{k:'kv',l:'KV cache used',c:css('--ok'),u:'%%'},{k:'gpu',l:'GPU busy',c:css('--brand'),u:'%%'}],100);
}
document.querySelectorAll('.range button').forEach(b=>b.addEventListener('click',()=>{range=+b.dataset.r;
 document.querySelectorAll('.range button').forEach(o=>o.classList.toggle('on',o===b));charts()}));
async function refresh(){let s;try{s=await (await fetch('/api/status')).json()}catch(e){return}status=s;
 const up=s.vllm=='up',h=s.history||[],last=h.length?h[h.length-1]:{},p=s.people||{},e=s.engine||{};
 const seqs=e['max-num-seqs'],gpu=h.slice().reverse().find(q=>q.gpu!=null||q.temp!=null)||{};
 // Tiles average the last minute: with a few people, most 5-second samples are idle.
 const minute=h.filter(q=>last.t-q.t<60),avg=(k,busy)=>{const v=minute.map(q=>q[k]).filter(x=>x!=null&&(!busy||x>0));
  return v.length?v.reduce((a,b)=>a+b,0)/v.length:null};
 const itl=avg('itl',true),ago=s.last_request?Date.now()/1000-s.last_request:null;
 document.getElementById('tiles').innerHTML=
  tile(s.vllm,e.health?'vLLM · '+E(e.health):'vLLM',up?'active':'blocked')+
  tile(F(avg('gen_tps'),0),'output tokens/s')+tile(F(avg('prompt_tps'),0),'input tokens/s')+
  tile(F(avg('req_min'),0),'requests per minute')+
  tile(ago==null?'–':ago<15?'now':dur(ago)+' ago','last request')+
  tile(F(s.running),seqs?'running · of '+E(seqs):'running')+tile(F(s.waiting),'waiting',s.waiting>0?'pending':'')+
  tile(U(s.kv,'%%'),'KV cache',s.kv>90?'blocked':'')+
  tile(U(avg('ttft',true),' s',2),'first token')+tile(itl?F(1/itl,0):'–','tokens/s per request')+
  tile(U(s.prefix_hit,'%%'),'prefix cache hits')+
  tile(U(gpu.gpu,'%%'),'GPU busy')+tile(U(gpu.temp,' °C'),'GPU temperature',gpu.temp>85?'blocked':'')+
  tile(U(gpu.power,' W'),'GPU power')+
  tile(s.mem_used==null?'–':`${F(s.mem_used,0)}/${F(s.mem_total,0)}`,'memory GiB')+tile(F(s.preemptions),'preemptions',s.preemptions>0?'pending':'');
 const kvt=s.kv_tokens,ctx=s.context,on=v=>v?'on':'off';
 document.getElementById('engine').innerHTML=facts([
  ['Served as',s.model],['Weights',s.weights],['vLLM',s.version],['Image',e.image],['Profile',e.profile],
  ['Context',ctx&&N(ctx)+' tokens'],['Requests at once',seqs],['Tokens per step',e['max-num-batched-tokens']&&N(+e['max-num-batched-tokens'])],
  ['KV cache',kvt&&`${N(kvt)} tokens${e['kv-cache-dtype']?', '+e['kv-cache-dtype']:''}${ctx?` (${F(kvt/ctx,1)} full contexts)`:''}`],
  ['GPU memory for vLLM',e['gpu-memory-utilization']&&F(100*e['gpu-memory-utilization'],0)+'%%'],
  ['Prefix caching',s.engine&&on(e['enable-prefix-caching'])],['Chunked prefill',s.engine&&on(e['enable-chunked-prefill'])],
  ['Tool call parser',e['tool-call-parser']],['Container',e.state&&e.state+(e.health?', '+e.health:'')],
  ['Up for',e.started&&e.state=='running'?dur(Date.now()/1000-e.started):null],['Restarts',e.restarts],
  ['GPU',s.gpu_name],['GPU clock',gpu.clock!=null?N(gpu.clock)+' MHz':null]]);
 const fin=s.finished||{},done=Object.values(fin).reduce((a,b)=>a+b,0),share=k=>done?F(100*(fin[k]||0)/done,0)+'%%':null;
 document.getElementById('totals').innerHTML=facts([
  ['Requests answered',up?N(done):null],['Input tokens',up?N(s.prompt_total):null],['Output tokens',up?N(s.gen_total):null],
  ['Finished normally',share('stop')],['Cut at the output limit',share('length')],['Aborted',fin.abort?share('abort'):null],
  ['Mean time to answer',s.e2e_mean!=null?F(s.e2e_mean,1)+' s':null],['Mean first token',s.ttft_mean!=null?F(s.ttft_mean,2)+' s':null],
  ['Mean wait in queue',s.queue_mean!=null?F(s.queue_mean,2)+' s':null]])||'<div class="muted">vLLM is down.</div>';
 document.getElementById('people').innerHTML=tile(F(p.active||0),'approved','active')+tile(F(p.pending||0),'waiting',p.pending?'pending':'')+
  tile(F(p.blocked||0),'blocked')+tile(F(s.active_users),'using the API now');
 document.getElementById('top').textContent=s.top.length?'Most API requests, last 10 min: '+s.top.map(t=>t[0]+' ('+t[1]+')').join(', '):'';
 document.getElementById('updated').textContent='rates average the last minute · updated '+new Date().toLocaleTimeString();
 charts();
}
refresh();setInterval(refresh,5000);window.addEventListener('resize',charts);
</script>""" % SAMPLE_EVERY


def people_card(me: int, message: str = "") -> str:
    rows = query("SELECT * FROM users ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, username")
    pending = sum(1 for r in rows if r["status"] == "pending")
    actions = {"pending": [("approve", "Approve"), ("delete", "Reject")],
               "active": [("block", "Block"), ("regen", "New key"), ("password", "Reset password"),
                          ("promote", "Make admin")],
               "blocked": [("unblock", "Unblock"), ("delete", "Delete")]}
    trs = []
    for r in rows:
        buttons = [(a, l) for a, l in actions[r["status"]]]
        if r["role"] == "admin" and r["status"] == "active":
            buttons = [(a, l) for a, l in buttons if a not in ("promote",)] + [("demote", "Remove admin")]
        if r["id"] == me:
            buttons = [(a, l) for a, l in buttons if a not in ("block", "delete", "demote", "password")]
        forms = " ".join('<form class="inline" method="post" action="/admin/{}/{}"><button>{}</button></form>'.format(
            r["id"], a, l) for a, l in buttons)
        trs.append('<tr><td><b>{}</b>{}<br><span class="muted">{}</span></td><td><span class="badge {}">{}</span></td>'
                   '<td class="muted">{}</td><td>{}</td></tr>'.format(
                       html.escape(r["username"]), " · admin" if r["role"] == "admin" else "",
                       "" if r["username"] == r["email"].lower() else html.escape(r["email"]), r["status"], r["status"], r["created_at"][:16].replace("T", " "), forms))
    bulk = ('<form class="inline" method="post" action="/admin/approve-all"><button class="primary">'
            'Approve all ({})</button></form>'.format(pending) if pending else "")
    chat = ('<p class="muted">Administrators here are also administrators of the chat.</p>' if OPENWEBUI_URL else "")
    return admin_tabs("/admin/people") + (
        '<div class="card"><h2>People {}</h2>{}{}<div class="scroll"><table><tr><th>User</th><th>Status</th>'
        '<th>Registered (UTC)</th><th></th></tr>{}</table></div></div>').format(
            bulk, flash_of(message, ""), chat, "".join(trs))


def settings_cards(admin: sqlite3.Row, message: str = "", error: str = "", chat_message: str = "",
                   chat_error: str = "") -> str:
    conf = settings()
    checked = lambda on: " checked" if on else ""  # noqa: E731
    options = ('<div class="card" id="settings"><h2>Registration and notice</h2>{flash}'
               '<form method="post" action="/admin/settings">'
               '<label class="check"><input type=checkbox name=registration value=open{reg}> Registration open</label>'
               '<p class="muted field-help">When closed, nobody can create an account; existing accounts keep working.</p>'
               '<label>E-mail domains allowed to register<input name=domains value="{dom}" '
               'placeholder="school.example, lab.example"></label>'
               '<p class="muted field-help">Separated by commas. Empty: any domain.</p>'
               '<label class="check"><input type=checkbox name=auto_approve value=on{auto}> Approve these domains '
               'automatically</label><p class="muted field-help">Only when domains are listed above. Everyone else '
               'waits for approval.</p>'
               '<label>Notice for users<textarea name=notice maxlength={nmax} placeholder="For example: the server '
               'restarts at 13:00.">{notice}</textarea></label><p class="muted field-help">Shown at the top of every '
               'personal page. Empty: no notice.</p><button class="primary">Save settings</button></form></div>').format(
        flash=flash_of(message, error), reg=checked(conf["registration"] == "open"), dom=html.escape(conf["domains"]),
        auto=checked(conf["auto_approve"] == "on"), nmax=NOTICE_MAX, notice=html.escape(conf["notice"]))
    chat = ""
    if OPENWEBUI_URL:
        try:
            enabled: Optional[bool] = owui_web_search(admin)
        except Exception as exc:
            enabled, chat_error = None, chat_error or "Open WebUI cannot be reached or did not accept you as an " \
                                                      "administrator ({}).".format(exc)
        form = ('<form method="post" action="/admin/chat"><label class="check"><input type=checkbox name=web_search '
                'value=on{}> Web search</label><p class="muted field-help">When on, users can let the model search '
                'the web from a conversation (the search button in the chat). When off, the button disappears for '
                'everyone.</p><button class="primary">Save</button></form>'.format(checked(bool(enabled)))
                if enabled is not None else "")
        chat = '<div class="card" id="chat"><h2>Chat (Open WebUI)</h2>{}{}</div>'.format(
            flash_of(chat_message, chat_error), form)
    return admin_tabs("/admin/settings") + options + chat


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "codendum-web"
    csrf = ""  # form token of the current session, set by current_user()
    session_token = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s %s\n" % (now_iso(), fmt % args))

    def send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8",
             headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, cookie: Optional[str] = None) -> None:
        headers = {"Location": location}
        if cookie is not None:
            headers["Set-Cookie"] = cookie
        self.send(303, b"", headers=headers)

    def form(self) -> Dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        return {k: v[0].strip() for k, v in data.items()}

    def client_ip(self) -> str:
        # The panel listens on loopback behind nginx, which appends the real
        # client address as the last X-Forwarded-For entry.
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.rsplit(",", 1)[-1].strip() or self.client_address[0]

    def safe_next(self, url: str) -> str:
        """Where to go after login: a local path, or HTTPS on this same host (the chat
        on another port); anything else is dropped, so this is no open redirect."""
        if url.startswith("/") and not url.startswith("//") and "\\" not in url:
            return url
        parts = urllib.parse.urlsplit(url)
        host = self.headers.get("Host", "").rsplit(":", 1)[0].lower()
        if parts.scheme == "https" and parts.hostname and parts.hostname.lower() == host and not parts.username:
            return url
        return ""

    def current_user(self) -> Optional[sqlite3.Row]:
        self.csrf = self.session_token = ""
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if SESSION_COOKIE not in jar:
            return None
        token = jar[SESSION_COOKIE].value
        rows = query("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires > ?",
                     (token, time.time()))
        if not rows:
            return None
        self.csrf, self.session_token = csrf_of(token), token
        return rows[0]

    def page(self, title: str, body: str, user: Optional[sqlite3.Row] = None) -> bytes:
        return page(title, body, user, self.csrf if user else "")

    def session_cookie(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        query("INSERT INTO sessions VALUES (?, ?, ?)", (token, user_id, time.time() + SESSION_TTL))
        return "{}={}; Path=/; HttpOnly; SameSite=Strict; Max-Age={}".format(SESSION_COOKIE, token, SESSION_TTL)

    # ---- GET ---------------------------------------------------------------
    def do_GET(self) -> None:
        user = self.current_user()
        path, _, qs = self.path.partition("?")
        if path == "/logo.png":
            with open(LOGO, "rb") as handle:
                self.send(200, handle.read(), "image/png", {"Cache-Control": "max-age=86400"})
        elif path == "/ca.pem":
            chain = read_chain()
            if chain:
                self.send(200, ssl.DER_cert_to_PEM_cert(chain[-1]).encode(), "application/x-pem-file",
                          {"Content-Disposition": 'attachment; filename="codendum-ca.pem"'})
            else:
                self.send(404, b"no certificate\n", "text/plain")
        elif re.match(r"^/s/[A-Za-z0-9_-]{16,64}(\.ps1)?$", path):
            # One-time workstation setup script; the code is used up only when a script is served.
            code, kind = (path[3:-4], "ps1") if path.endswith(".ps1") else (path[3:], "sh")
            chain, model = read_chain(), served_model()
            if not chain or not model:
                self.send(503, b"The model server is not ready yet: try again in a minute.\n", "text/plain")
                return
            owner = take_setup_code(code)
            rows = query("SELECT * FROM users WHERE id = ? AND status = 'active'", (owner,)) if owner else []
            if not rows:
                self.send(404, b"This setup command is not valid any more: create a new one on the panel.\n",
                          "text/plain")
                return
            script = workstation_script(kind, rows[0], public_url(API_URL, self.headers.get("Host", "")), chain, model)
            self.send(200, script.encode(), "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
        elif path == "/login":
            self.send(200, self.page("Log in", login_form(next_url=self.safe_next(
                urllib.parse.parse_qs(qs).get("next", [""])[0]))))
        elif path == "/register":
            setup = urllib.parse.parse_qs(qs).get("setup", [""])[0]
            self.send(200, self.page("Register", login_form(register=True, setup=setup)))
        elif self.path == "/auth/verify":
            # For nginx auth_request in front of Open WebUI (trusted header login).
            if user and user["status"] == "active":
                self.send(200, b"", "text/plain", {"X-Codendum-User": user["username"],
                                                   "X-Codendum-Email": user["email"].lower(),
                                                   "X-Codendum-Role": owui_role(user)})
            else:
                self.send(401, b"", "text/plain")
        elif self.path == "/api/status":
            if not user or user["role"] != "admin" or user["status"] != "active":
                self.send(403, b"{}", "application/json")
            else:
                self.send(200, json.dumps(service_status()).encode(), "application/json")
        elif self.path == "/":
            if not user:
                self.redirect("/login")
                return
            notice = settings()["notice"]
            body = '<div class="card notice">{}</div>'.format(html.escape(notice)) if notice else ""
            body += user_card(user, self.headers.get("Host", ""))
            if user["role"] == "admin" and user["status"] == "active":
                pending = len(query("SELECT 1 FROM users WHERE status = 'pending'"))
                body += ('<div class="card"><h2>Administration</h2><p>{} waiting for approval.</p>'
                         '<a class="btn primary" href="/admin{}">Open the administration</a></div>').format(
                    "/people" if pending else "",
                    "1 person is" if pending == 1 else "{} people are".format(pending) if pending else "Nobody is")
            self.send(200, self.page("Home", body, user))
        elif path == "/profile":
            if not user:
                self.redirect("/login?next=/profile")
            else:
                saved = "Password changed. Other sessions were logged out." if "saved=1" in qs.split("&") else ""
                self.send(200, self.page("Profile", profile_card(user, message=saved), user))
        elif path in ("/admin", "/admin/people", "/admin/settings"):
            if not user:
                self.redirect("/login?next=" + path)
            elif user["role"] != "admin" or user["status"] != "active":
                self.send(403, self.page("Forbidden", '<div class="card">Not allowed.</div>', user))
            elif path == "/admin":
                self.send(200, self.page("Dashboard", dashboard(), user))
            elif path == "/admin/people":
                self.send(200, self.page("People", people_card(user["id"]), user))
            else:
                flags = qs.split("&")
                self.send(200, self.page("Settings", settings_cards(
                    user, "Settings saved." if "saved=1" in flags else "",
                    chat_message="Chat settings saved." if "chat=1" in flags else ""), user))
        else:
            self.send(404, self.page("Not found", '<div class="card">Not found.</div>'))

    # ---- POST --------------------------------------------------------------
    def do_POST(self) -> None:
        global SETUP_TOKEN
        data = self.form()
        user = self.current_user()
        if (user and self.path not in ("/register", "/login", "/register/check")
                and not hmac.compare_digest(data.get("csrf", ""), self.csrf)):
            self.send(403, self.page("Forbidden", '<div class="card">This form has expired: go back, reload the page '
                                     'and try again.</div>', user))
            return
        if self.path == "/register":
            email, password = data.get("email", "").lower(), data.get("password", "")
            username, setup = email, data.get("setup", "")
            first = bool(setup)
            if first and not (SETUP_TOKEN and hmac.compare_digest(setup, SETUP_TOKEN)):
                self.send(400, self.page("Register", login_form("This setup link is not valid any more.", True)))
                return
            problem = email_problem(email, first) or ("" if len(password) >= 8 else
                                                      "Use a password of 8 or more characters.")
            if problem:
                self.send(400, self.page("Register", login_form(problem, True, setup)))
                return
            conf = settings()
            active = first or (conf["auto_approve"] == "on" and email.rsplit("@", 1)[1] in domains_of(conf["domains"]))
            try:
                query("INSERT INTO users (username, email, password, role, status, api_key, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (username, email, hash_password(password), "admin" if first else "user",
                       "active" if active else "pending", new_key() if active else None, now_iso()))
            except sqlite3.IntegrityError:
                self.send(400, self.page("Register", login_form("This e-mail is already registered: log in instead.",
                                                                True, setup)))
                return
            if first:
                SETUP_TOKEN = ""
            if active:
                write_key_map()
            new = query("SELECT id FROM users WHERE username = ?", (username,))[0]
            self.redirect("/", self.session_cookie(new["id"]))
        elif self.path == "/login":
            username = data.get("email", "").lower()
            ip = self.client_ip()
            keys = [((ip, username), LOGIN_MAX_PER_USER), (ip, LOGIN_MAX_PER_CLIENT)]
            if too_many_failures(keys):
                self.send(429, self.page("Log in", login_form("Too many failed attempts. Try again in a few minutes.",
                                                        next_url=self.safe_next(data.get("next", "")))))
                return
            rows = query("SELECT * FROM users WHERE username = ? OR lower(email) = ? ORDER BY id LIMIT 1",
                         (username, username))
            if not rows or not check_password(data.get("password", ""), rows[0]["password"]):
                record_failure(keys)
                self.send(401, self.page("Log in", login_form("Wrong e-mail or password.",
                                                        next_url=self.safe_next(data.get("next", "")))))
                return
            with FAILURES_LOCK:
                FAILURES.pop((ip, username), None)
            self.redirect(self.safe_next(data.get("next", "")) or "/", self.session_cookie(rows[0]["id"]))
        elif self.path == "/register/check":
            keys = [(("check", self.client_ip()), CHECKS_PER_CLIENT)]
            if too_many_failures(keys):
                self.send(429, json.dumps({"ok": False, "message": "Too many checks: wait a few minutes."}).encode(),
                          "application/json")
                return
            record_failure(keys)
            problem = email_problem(data.get("email", "").strip().lower())
            self.send(200, json.dumps({"ok": not problem, "message": problem or "OK, this e-mail can be used."}).encode(),
                      "application/json")
        elif self.path == "/setup/new" and user and user["status"] == "active":
            query("DELETE FROM setup_codes WHERE user_id = ? OR expires <= ?", (user["id"], time.time()))
            query("INSERT INTO setup_codes VALUES (?, ?, ?)",
                  (secrets.token_urlsafe(18), user["id"], time.time() + SETUP_CODE_TTL))
            self.redirect("/")
        elif self.path == "/profile/password" and user:
            keys = [(("password", user["id"]), LOGIN_MAX_PER_USER)]
            current, new = data.get("current", ""), data.get("password", "")
            error = ""
            if too_many_failures(keys):
                error = "Too many wrong passwords. Try again in a few minutes."
            elif not check_password(current, user["password"]):
                record_failure(keys)
                error = "The current password is wrong."
            elif len(new) < 8:
                error = "Use a password of 8 or more characters."
            elif new != data.get("repeat", ""):
                error = "The two new passwords are different."
            if error:
                self.send(400, self.page("Profile", profile_card(user, error=error), user))
                return
            query("UPDATE users SET password = ? WHERE id = ?", (hash_password(new), user["id"]))
            query("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user["id"], self.session_token))
            self.redirect("/profile?saved=1")
        elif self.path == "/logout":
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            if SESSION_COOKIE in jar:
                query("DELETE FROM sessions WHERE token = ?", (jar[SESSION_COOKIE].value,))
            self.redirect("/login", "{}=; Path=/; Max-Age=0".format(SESSION_COOKIE))
        elif self.path == "/key/regenerate" and user and user["status"] == "active":
            query("UPDATE users SET api_key = ? WHERE id = ?", (new_key(), user["id"]))
            write_key_map()
            self.redirect("/")
        elif self.path.startswith("/admin/") and user and user["role"] == "admin" and user["status"] == "active":
            self.admin_action(user, data)
        else:
            self.send(403, self.page("Forbidden", '<div class="card">Not allowed.</div>'))

    def admin_action(self, admin: sqlite3.Row, data: Dict[str, str]) -> None:
        if self.path == "/admin/approve-all":
            for row in query("SELECT id FROM users WHERE status = 'pending'"):
                query("UPDATE users SET status = 'active', api_key = ? WHERE id = ?", (new_key(), row["id"]))
            write_key_map()
            self.redirect("/admin/people")
            return
        if self.path == "/admin/settings":
            self.save_settings(admin, data)
            return
        if self.path == "/admin/chat" and OPENWEBUI_URL:
            try:
                owui_web_search(admin, data.get("web_search") == "on")
            except Exception as exc:
                self.send(502, self.page("Settings", settings_cards(
                    admin, chat_error="Open WebUI did not save the change ({}).".format(exc)), admin))
                return
            self.redirect("/admin/settings?chat=1#chat")
            return
        match = re.match(r"^/admin/(\d+)/(approve|block|unblock|delete|regen|password|promote|demote)$", self.path)
        if not match:
            self.send(404, self.page("Not found", '<div class="card">Not found.</div>'))
            return
        target, action = int(match.group(1)), match.group(2)
        if target == admin["id"] and action in ("block", "delete", "demote", "password"):
            self.send(400, self.page("Not allowed", '<div class="card">You cannot do this to your own account.</div>',
                                     admin))
            return
        if action == "password":
            rows = query("SELECT username FROM users WHERE id = ?", (target,))
            if not rows:
                self.redirect("/admin/people")
                return
            password = secrets.token_urlsafe(9)
            query("UPDATE users SET password = ? WHERE id = ?", (hash_password(password), target))
            query("DELETE FROM sessions WHERE user_id = ?", (target,))
            self.send(200, self.page("Password reset", (
                '<div class="card"><h2>New password for {}</h2><p>Give it to this person; it is shown only once. '
                'They are logged out everywhere.</p>{}<p><a class="btn" href="/admin/people">Back to people</a></p>'
                '</div>').format(html.escape(rows[0]["username"]), copyable("new-password", password)), admin),
                      headers={"Cache-Control": "no-store"})
            return
        statements = {
            "approve": ("UPDATE users SET status = 'active', api_key = COALESCE(api_key, ?) WHERE id = ?", (new_key(), target)),
            "unblock": ("UPDATE users SET status = 'active', api_key = COALESCE(api_key, ?) WHERE id = ?", (new_key(), target)),
            "block": ("UPDATE users SET status = 'blocked' WHERE id = ?", (target,)),
            "delete": ("DELETE FROM users WHERE id = ?", (target,)),
            "regen": ("UPDATE users SET api_key = ? WHERE id = ?", (new_key(), target)),
            "promote": ("UPDATE users SET role = 'admin' WHERE id = ?", (target,)),
            "demote": ("UPDATE users SET role = 'user' WHERE id = ?", (target,)),
        }
        before = query("SELECT * FROM users WHERE id = ?", (target,))
        sql, args = statements[action]
        query(sql, args)
        if action in ("block", "delete"):
            query("DELETE FROM sessions WHERE user_id = ?", (target,))
        write_key_map()
        # Administrators of the chat follow the administrators here.
        after = query("SELECT * FROM users WHERE id = ?", (target,))
        if before and (before[0]["role"] == "admin" or action == "promote"):
            role = owui_role(after[0]) if after else "pending"
            if role != owui_role(before[0]):
                sync_owui_role(before[0], role)
        self.redirect("/admin/people")

    def save_settings(self, admin: sqlite3.Row, data: Dict[str, str]) -> None:
        domains = domains_of(data.get("domains", ""))
        notice = data.get("notice", "").replace("\r\n", "\n")
        bad = [d for d in domains if not DOMAIN_RE.match(d)]
        error = ("Not a domain: {}.".format(", ".join(bad)) if bad else
                 "The notice is too long ({} characters at most).".format(NOTICE_MAX) if len(notice) > NOTICE_MAX else "")
        if error:
            self.send(400, self.page("Settings", settings_cards(admin, error=error), admin))
            return
        values = {"registration": "open" if data.get("registration") == "open" else "closed",
                  "domains": ", ".join(dict.fromkeys(domains)),
                  "auto_approve": "on" if data.get("auto_approve") == "on" else "",
                  "notice": notice}
        for key, value in values.items():
            query("INSERT INTO settings (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.redirect("/admin/settings?saved=1")


def main() -> None:
    global SETUP_TOKEN
    host, _, port = LISTEN.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    print("codendum-web (prototype) on http://{}  db={}  map={}".format(LISTEN, DB_PATH, MAP_PATH), flush=True)
    threading.Thread(target=sampler, name="sampler", daemon=True).start()
    if not query("SELECT 1 FROM users WHERE role = 'admin'"):
        # Only readable from the container log. A new link is printed on every
        # start until an administrator exists.
        SETUP_TOKEN = secrets.token_urlsafe(24)
        print("No administrator yet. Create one at {}/register?setup={}".format(
            public_url(API_URL, "HOST"), SETUP_TOKEN), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
