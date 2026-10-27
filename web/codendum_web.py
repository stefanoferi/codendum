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
- Each active user gets an API key; the nginx key map is rewritten on every
  change and the proxy is reloaded with CODENDUM_WEB_RELOAD_CMD.
- /auth/verify answers nginx auth_request, so that Open WebUI can trust the
  identity of the logged-in user (trusted e-mail header).
- The status box reads vLLM /metrics and the proxy access log.
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
  CODENDUM_WEB_ACCESS_LOG   proxy access log (optional)
  CODENDUM_WEB_API_URL      public proxy URL shown to users (default https://{host}:8443)
  CODENDUM_WEB_CHAT_URL     Open WebUI URL shown to users (optional)
  In both URLs, {host} is replaced with the host name the browser used, so the
  panel needs no change when the host address changes.
  CODENDUM_WEB_TLS_CHAIN    proxy certificate chain, read for the pin and /ca.pem
                            (default /proxy/tls/fullchain.pem; the last certificate
                            is served as the CA)
"""

from __future__ import annotations

import base64
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
ACCESS_LOG = ENV("CODENDUM_WEB_ACCESS_LOG", "")
API_URL = ENV("CODENDUM_WEB_API_URL", "https://{host}:8443")
CHAT_URL = ENV("CODENDUM_WEB_CHAT_URL", "")
TLS_CHAIN = ENV("CODENDUM_WEB_TLS_CHAIN", "/proxy/tls/fullchain.pem")
HERE = os.path.dirname(os.path.abspath(__file__))
LOGO = os.path.join(HERE, "..", "docs", "_static", "codendum-logo.png")
SETUP_CODE_TTL = 600
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")

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
""")


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


def served_model() -> Optional[Tuple[str, int]]:
    """(model id, max_model_len) from vLLM, or None while it is not ready."""
    url = METRICS_URL.rsplit("/metrics", 1)[0] + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())["data"][0]
    except Exception:
        return None
    if not MODEL_RE.match(str(data.get("id", ""))) or not isinstance(data.get("max_model_len"), int):
        return None
    return data["id"], data["max_model_len"]


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


def docker_exec(container: str, cmd: List[str]) -> None:
    """Run a command in a container through the Docker Engine API (no CLI needed)."""
    import http.client
    import socket

    class UnixConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(30)
            self.sock.connect(DOCKER_SOCKET)

    def call(method: str, path: str, body: Dict[str, Any]) -> Tuple[int, bytes]:
        conn = UnixConnection("localhost")
        conn.request(method, path, json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    status, data = call("POST", "/containers/{}/exec".format(urllib.parse.quote(container)),
                        {"Cmd": cmd, "AttachStdout": True, "AttachStderr": True})
    if status != 201:
        raise RuntimeError("exec create {}: {}".format(status, data[:200]))
    exec_id = json.loads(data)["Id"]
    status, _ = call("POST", "/exec/{}/start".format(exec_id), {"Detach": False, "Tty": False})
    if status != 200:
        raise RuntimeError("exec start {}".format(status))
    conn = UnixConnection("localhost")
    conn.request("GET", "/exec/{}/json".format(exec_id))
    info = json.loads(conn.getresponse().read())
    conn.close()
    if info.get("ExitCode") != 0:
        raise RuntimeError("{} exited with {}".format(" ".join(cmd), info.get("ExitCode")))


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def service_status() -> Dict[str, Any]:
    status: Dict[str, Any] = {"vllm": "down", "running": None, "waiting": None, "kv": None, "top": []}
    try:
        summary = core.summarize_metrics(core.fetch_metrics(METRICS_URL, timeout=3))
        status.update(vllm="up", running=summary["requests_running"], waiting=summary["requests_waiting"],
                      kv=summary["kv_cache_usage_percent"])
    except Exception:  # down or unreachable: shown as such
        pass
    if ACCESS_LOG and os.path.exists(ACCESS_LOG):
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        counts: Dict[str, int] = {}
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
        status["top"] = sorted(counts.items(), key=lambda item: -item[1])[:5]
    return status


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
header img{height:40px}main{max-width:980px;margin:24px auto;padding:0 16px;display:grid;gap:18px}
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
.stats{display:flex;gap:28px;flex-wrap:wrap}.stat b{display:block;font-size:24px}.stat span{color:var(--muted);font-size:13px}
input{font:inherit;width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);margin:4px 0 12px}
.narrow{width:100%;max-width:380px;margin:60px auto}.muted{color:var(--muted)}.err{color:var(--warn)}
.hint{min-height:1.4em;margin:-6px 0 10px;font-size:13px}.hint.ok{color:var(--ok)}
h3{font-size:15px;margin:18px 0 6px}.steps{padding-left:20px}.steps li{margin:0 0 16px}.steps p{margin:8px 0 4px}
.copy{display:flex;gap:8px;align-items:flex-start}.copy pre{flex:1;margin:0;white-space:pre-wrap;word-break:break-all}
details{margin-top:8px}summary{cursor:pointer;color:var(--link)}
"""


def page(title: str, body: str, user: Optional[sqlite3.Row] = None) -> bytes:
    right = ""
    if user:
        right = ('<div><span class="muted">{}</span> <form class="inline" method="post" action="/logout">'
                 '<button>Log out</button></form></div>').format(html.escape(user["username"]))
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>{t} · Codendum</title>"
            "<style>{css}</style></head><body><header><img src='/logo.png' alt='Codendum'>{r}</header>"
            "<main>{b}</main><script>function copy(id){{navigator.clipboard.writeText("
            "document.getElementById(id).innerText)}}</script></body></html>").format(
                t=html.escape(title), css=CSS, r=right, b=body).encode()


def login_form(error: str = "", register: bool = False, setup: str = "", next_url: str = "") -> str:
    action, label = ("/register", "Create account") if register else ("/login", "Log in")
    extra = '<input type=hidden name=next value="{}">'.format(html.escape(next_url)) if next_url else ""
    if register and setup:
        label = "Create the administrator account"
        extra += '<input type=hidden name=setup value="{}">'.format(html.escape(setup))
    switch = ('<p>Already registered?</p><a class="btn wide" href="/login">Log in</a>' if register
              else '<p>No account yet?</p><a class="btn wide" href="/register">Register</a>')
    err = '<p class="err">{}</p>'.format(html.escape(error)) if error else ""
    return ('<div class="card narrow"><h2>{label}</h2>{err}<form method="post" action="{action}">'
            '<label>E-mail<input id=email name=email required inputmode=email autocomplete=username></label>'
            '{hint}{extra}<label>Password<input name=password type=password required minlength=8></label>'
            '<button class="primary wide">{label}</button></form><div class="switch">{switch}</div></div>{script}').format(
        label=label, err=err, action=action, extra=extra, switch=switch,
        hint='<p class="hint" id="email-msg" aria-live="polite"></p>' if register else "",
        script=EMAIL_CHECK_JS if register else "")


# Checks the e-mail while it is typed (POST, so that addresses stay out of URLs and logs).
EMAIL_CHECK_JS = """<script>(function(){var e=document.getElementById("email"),m=document.getElementById("email-msg"),t;
e.addEventListener("input",function(){clearTimeout(t);m.textContent="";t=setTimeout(async function(){
var v=e.value.trim();if(!v)return;try{var r=await(await fetch("/register/check",{method:"POST",
headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"email="+encodeURIComponent(v)})).json();
m.textContent=r.message;m.className="hint "+(r.ok?"ok":"err")}catch(x){}},400)})})()</script>"""


def email_problem(email: str) -> str:
    """Why an e-mail cannot be registered, or "" if it can."""
    if len(email) > 254 or not EMAIL_RE.match(email):
        return "This is not a valid e-mail address."
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
            connect = ('<p>macOS and Linux, in a terminal:</p>{sh}<p>Windows, in PowerShell (an up-to-date '
                       'Windows 10 or 11: <code>curl.exe --version</code> must show 7.58.1 or later):</p>{ps}'
                       '<p class="muted">Works once, for {m} more minutes. It is personal: it contains your key.</p>'
                       '<form method="post" action="/setup/new"><button>New command</button></form>').format(
                sh=copyable("cmd-sh", "curl -fsSk --pinnedpubkey '{}' {} | sh".format(pin, url)),
                ps=copyable("cmd-ps", "curl.exe -fsSk --pinnedpubkey '{}' {}.ps1 | Out-String | Invoke-Expression".format(
                    pin, url)), m=minutes)
        else:
            connect = ('<p>Create a command to paste on your computer. It works once, for 10 minutes.</p>'
                       '<form method="post" action="/setup/new"><button class="primary">Create the setup command'
                       '</button></form>')
    return ('<div class="card"><h2>Your access</h2>{chat}<h3>Set up OpenCode on your computer</h3><ol class="steps">'
            '<li><b>Install OpenCode</b> (once).<p>macOS and Linux, in a terminal:</p>{inst_sh}'
            '<p>Windows, in PowerShell:</p>{inst_ps}<p class="muted">Then open a new terminal window.</p></li>'
            '<li><b>Connect OpenCode to Codendum.</b>{connect}</li>'
            '<li><b>Start.</b> In your project folder run <code>opencode</code>: the model <code>codendum/…</code> is '
            'already selected.<p class="muted">macOS 15 or later: if OpenCode reports <code>EHOSTUNREACH</code>, '
            'allow it (and your terminal app) in System Settings → Privacy &amp; Security → Local Network, then '
            'restart it.</p></li></ol>'
            '<details><summary>API key and certificate, for other tools</summary>'
            '<span class="key" id="key">{key}</span><button onclick="copy(\'key\')">Copy</button> '
            '<form class="inline" method="post" action="/key/regenerate"><button>Regenerate</button></form>'
            '<p>API: <code>{base}/v1</code> · certificate: <a href="/ca.pem">ca.pem</a></p>'
            '<p class="muted">Keep the key private. Regenerating it disables the old one immediately; then run '
            'step 2 again.</p></details></div>').format(
        chat=chat, inst_sh=copyable("inst-sh", "curl -fsSL https://opencode.ai/v2/install | bash"),
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


def copyable(element_id: str, text: str) -> str:
    return '<div class="copy"><pre id="{i}">{t}</pre><button onclick="copy(\'{i}\')">Copy</button></div>'.format(
        i=element_id, t=html.escape(text))


def admin_cards(me: int) -> str:
    rows = query("SELECT * FROM users ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, username")
    pending = sum(1 for r in rows if r["status"] == "pending")
    status = ('<div class="card"><h2>Service</h2><div class="stats" id="stats">loading…</div>'
              '<p class="muted" id="top"></p></div>')
    actions = {"pending": [("approve", "Approve"), ("delete", "Reject")],
               "active": [("block", "Block"), ("regen", "New key"), ("promote", "Make admin")],
               "blocked": [("unblock", "Unblock"), ("delete", "Delete")]}
    trs = []
    for r in rows:
        buttons = [(a, l) for a, l in actions[r["status"]]]
        if r["role"] == "admin" and r["status"] == "active":
            buttons = [(a, l) for a, l in buttons if a not in ("promote",)] + [("demote", "Remove admin")]
        if r["id"] == me:
            buttons = [(a, l) for a, l in buttons if a not in ("block", "delete", "demote")]
        forms = " ".join('<form class="inline" method="post" action="/admin/{}/{}"><button>{}</button></form>'.format(
            r["id"], a, l) for a, l in buttons)
        trs.append('<tr><td><b>{}</b>{}<br><span class="muted">{}</span></td><td><span class="badge {}">{}</span></td>'
                   '<td class="muted">{}</td><td>{}</td></tr>'.format(
                       html.escape(r["username"]), " · admin" if r["role"] == "admin" else "",
                       "" if r["username"] == r["email"].lower() else html.escape(r["email"]), r["status"], r["status"], r["created_at"][:16].replace("T", " "), forms))
    bulk = ('<form class="inline" method="post" action="/admin/approve-all"><button class="primary">'
            'Approve all ({})</button></form>'.format(pending) if pending else "")
    people = ('<div class="card"><h2>People {}</h2><table><tr><th>User</th><th>Status</th><th>Registered (UTC)</th>'
              '<th></th></tr>{}</table></div>').format(bulk, "".join(trs))
    script = """<script>
async function refresh(){try{const s=await (await fetch('/api/status')).json();
const f=v=>v===null||v===undefined?'–':v;
document.getElementById('stats').innerHTML=
`<div class=stat><b class=${s.vllm=='up'?'active':'blocked'}>${s.vllm}</b><span>vLLM</span></div>
<div class=stat><b>${f(s.running)}</b><span>running</span></div><div class=stat><b>${f(s.waiting)}</b><span>waiting</span></div>
<div class=stat><b>${s.kv===null?'–':s.kv+'%'}</b><span>KV cache</span></div>`;
document.getElementById('top').textContent=s.top.length?'Most requests, last 10 min: '+s.top.map(t=>t[0]+' ('+t[1]+')').join(', '):'';
}catch(e){}}refresh();setInterval(refresh,5000);</script>"""
    return status + people + script


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "codendum-web"

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
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if SESSION_COOKIE not in jar:
            return None
        rows = query("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires > ?",
                     (jar[SESSION_COOKIE].value, time.time()))
        return rows[0] if rows else None

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
            self.send(200, page("Log in", login_form(next_url=self.safe_next(
                urllib.parse.parse_qs(qs).get("next", [""])[0]))))
        elif path == "/register":
            setup = urllib.parse.parse_qs(qs).get("setup", [""])[0]
            self.send(200, page("Register", login_form(register=True, setup=setup)))
        elif self.path == "/auth/verify":
            # For nginx auth_request in front of Open WebUI (trusted header login).
            if user and user["status"] == "active":
                self.send(200, b"", "text/plain", {"X-Codendum-User": user["username"],
                                                   "X-Codendum-Email": user["email"].lower()})
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
            body = user_card(user, self.headers.get("Host", ""))
            if user["role"] == "admin" and user["status"] == "active":
                body += admin_cards(user["id"])
            self.send(200, page("Home", body, user))
        else:
            self.send(404, page("Not found", '<div class="card">Not found.</div>'))

    # ---- POST --------------------------------------------------------------
    def do_POST(self) -> None:
        global SETUP_TOKEN
        data = self.form()
        user = self.current_user()
        if self.path == "/register":
            email, password = data.get("email", "").lower(), data.get("password", "")
            username, setup = email, data.get("setup", "")
            problem = email_problem(email) or ("" if len(password) >= 8 else "Use a password of 8 or more characters.")
            if problem:
                self.send(400, page("Register", login_form(problem, True, setup)))
                return
            first = bool(setup)
            if first and not (SETUP_TOKEN and hmac.compare_digest(setup, SETUP_TOKEN)):
                self.send(400, page("Register", login_form("This setup link is not valid any more.", True)))
                return
            try:
                query("INSERT INTO users (username, email, password, role, status, api_key, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (username, email, hash_password(password), "admin" if first else "user",
                       "active" if first else "pending", new_key() if first else None, now_iso()))
            except sqlite3.IntegrityError:
                self.send(400, page("Register", login_form("This e-mail is already registered: log in instead.",
                                                           True, setup)))
                return
            if first:
                SETUP_TOKEN = ""
                write_key_map()
            new = query("SELECT id FROM users WHERE username = ?", (username,))[0]
            self.redirect("/", self.session_cookie(new["id"]))
        elif self.path == "/login":
            username = data.get("email", "").lower()
            ip = self.client_ip()
            keys = [((ip, username), LOGIN_MAX_PER_USER), (ip, LOGIN_MAX_PER_CLIENT)]
            if too_many_failures(keys):
                self.send(429, page("Log in", login_form("Too many failed attempts. Try again in a few minutes.",
                                                        next_url=self.safe_next(data.get("next", "")))))
                return
            rows = query("SELECT * FROM users WHERE username = ? OR lower(email) = ? ORDER BY id LIMIT 1",
                         (username, username))
            if not rows or not check_password(data.get("password", ""), rows[0]["password"]):
                record_failure(keys)
                self.send(401, page("Log in", login_form("Wrong e-mail or password.",
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
            self.admin_action(user)
        else:
            self.send(403, page("Forbidden", '<div class="card">Not allowed.</div>'))

    def admin_action(self, admin: sqlite3.Row) -> None:
        if self.path == "/admin/approve-all":
            for row in query("SELECT id FROM users WHERE status = 'pending'"):
                query("UPDATE users SET status = 'active', api_key = ? WHERE id = ?", (new_key(), row["id"]))
            write_key_map()
            self.redirect("/")
            return
        match = re.match(r"^/admin/(\d+)/(approve|block|unblock|delete|regen|promote|demote)$", self.path)
        if not match:
            self.send(404, page("Not found", '<div class="card">Not found.</div>'))
            return
        target, action = int(match.group(1)), match.group(2)
        if target == admin["id"] and action in ("block", "delete", "demote"):
            self.send(400, page("Not allowed", '<div class="card">You cannot do this to your own account.</div>', admin))
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
        sql, args = statements[action]
        query(sql, args)
        if action in ("block", "delete"):
            query("DELETE FROM sessions WHERE user_id = ?", (target,))
        write_key_map()
        self.redirect("/")


def main() -> None:
    global SETUP_TOKEN
    host, _, port = LISTEN.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    print("codendum-web (prototype) on http://{}  db={}  map={}".format(LISTEN, DB_PATH, MAP_PATH), flush=True)
    if not query("SELECT 1 FROM users WHERE role = 'admin'"):
        # Only readable from the container log. A new link is printed on every
        # start until an administrator exists.
        SETUP_TOKEN = secrets.token_urlsafe(24)
        print("No administrator yet. Create one at {}/register?setup={}".format(
            public_url(API_URL, "HOST"), SETUP_TOKEN), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
