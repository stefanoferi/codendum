#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
"""Tiny stand-in for the parts of the Open WebUI API that codendum-web uses.

It follows Open WebUI 0.12.0 with WEBUI_AUTH_TRUSTED_EMAIL_HEADER,
WEBUI_AUTH_TRUSTED_NAME_HEADER and WEBUI_AUTH_TRUSTED_ROLE_HEADER set to the
X-Codendum-* headers:

- POST /api/v1/auths/signin: creates the account on first use, applies the
  role header (admin, user or pending) and answers a session token, or
  {"next_step": "pending"} for pending accounts;
- GET /api/v1/retrieval/config and POST /api/v1/retrieval/config/update:
  administrators only; only the web search switch is kept;
- GET /_state: the accounts, their roles and the switch, for the tests.

It must never be used for anything but tests.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"users": {}, "web_search": True}
TOKENS = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # keep test output quiet
        pass

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _admin(self) -> bool:
        email = TOKENS.get(self.headers.get("Authorization", "")[len("Bearer "):])
        return bool(email) and STATE["users"].get(email) == "admin"

    def do_GET(self) -> None:
        if self.path == "/_state":
            self._json(200, STATE)
        elif self.path == "/api/v1/retrieval/config":
            if not self._admin():
                self._json(401, {"detail": "unauthorized"})
                return
            self._json(200, {"status": True, "web": {"ENABLE_WEB_SEARCH": STATE["web_search"]}})
        else:
            self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/v1/auths/signin":
            email = self.headers.get("X-Codendum-Email", "").lower()
            if not email:
                self._json(400, {"detail": "missing trusted header"})
                return
            role = self.headers.get("X-Codendum-Role", "").lower().strip()
            STATE["users"].setdefault(email, "user")
            if role in ("admin", "user", "pending"):
                STATE["users"][email] = role
            if STATE["users"][email] == "pending":
                self._json(200, {"next_step": "pending"})
                return
            token = secrets.token_hex(16)
            TOKENS[token] = email
            self._json(200, {"token": token, "token_type": "Bearer", "id": email, "email": email,
                             "name": self.headers.get("X-Codendum-User", email), "role": STATE["users"][email],
                             "profile_image_url": ""})
        elif self.path == "/api/v1/retrieval/config/update":
            if not self._admin():
                self._json(401, {"detail": "unauthorized"})
                return
            web = body.get("web") or {}
            if "ENABLE_WEB_SEARCH" in web:
                STATE["web_search"] = bool(web["ENABLE_WEB_SEARCH"])
            self._json(200, {"status": True, "web": {"ENABLE_WEB_SEARCH": STATE["web_search"]}})
        else:
            self._json(404, {"detail": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--port-file", help="write the bound port to this file")
    options = parser.parse_args()
    server = ThreadingHTTPServer((options.host, options.port), Handler)
    server.daemon_threads = True
    if options.port_file:
        with open(options.port_file + ".tmp", "w", encoding="ascii") as handle:
            handle.write(str(server.server_address[1]))
        os.replace(options.port_file + ".tmp", options.port_file)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
