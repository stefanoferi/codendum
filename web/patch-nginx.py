#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
"""Adapt a Codendum nginx site configuration for the web panel (prototype).

- the key map also includes api-keys-web.map, written by the panel;
- "location /" proxies to the panel instead of answering 404.

Usage: web/patch-nginx.py CONF [PANEL_ADDRESS]   (default 127.0.0.1:8090)
A backup CONF.bak is written first. Running it twice changes nothing.
"""

import os
import re
import shutil
import sys

conf_path = sys.argv[1]
panel = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1:8090"
text = open(conf_path, encoding="utf-8").read()
original = text

include = "    include /etc/nginx/codendum/api-keys.map;\n"
if "api-keys-web.map" not in text:
    if include not in text:
        sys.exit("error: key map include not found in " + conf_path)
    text = text.replace(include, include + "    include /etc/nginx/codendum/api-keys-web.map;\n", 1)

panel_block = """    location / {{
        # Codendum web panel (users, keys, status).
        proxy_pass         http://{panel};
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
    }}""".format(panel=panel)
if "Codendum web panel" not in text:
    text, count = re.subn(r"    location / \{\n        return 404;\n    \}", panel_block, text, count=1)
    if count != 1:
        sys.exit("error: 'location / { return 404; }' not found in " + conf_path)

# nginx refuses to start if an included file is missing: create an empty map
# next to the proxy directory layout ($CODENDUM_PROXY_DIR/conf.d/codendum.conf).
web_map = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(conf_path))), "api-keys-web.map")
if not os.path.exists(web_map):
    fd = os.open(web_map, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    print("created empty " + web_map)

if text != original:
    shutil.copy2(conf_path, conf_path + ".bak")
    open(conf_path, "w", encoding="utf-8").write(text)
    print("patched " + conf_path)
else:
    print("already patched: " + conf_path)
