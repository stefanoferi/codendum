# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
"""Sphinx configuration for the Codendum documentation.

Build locally:
    python3 -m venv .venv && .venv/bin/pip install --require-hashes -r docs/requirements.txt
    .venv/bin/sphinx-build -W --keep-going -b html docs docs/_build/html
"""

project = "Codendum"
author = "Stefano Noferi"
copyright = "2026, Stefano Noferi"
release = "0.1.1"

extensions = ["myst_parser", "sphinx_sitemap", "sphinxext.opengraph"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build", "requirements.txt", "requirements.in"]

# Anchors for "page.md#section" links, as GitHub renders them.
myst_heading_anchors = 3

# Published on GitHub Pages; used for the sitemap and OpenGraph metadata.
html_baseurl = "https://stefanoferi.github.io/codendum/"
sitemap_url_scheme = "{link}"
ogp_site_url = html_baseurl
ogp_site_name = "Codendum"
ogp_type = "website"
ogp_social_cards = {"enable": False}

html_theme = "furo"
html_title = "Codendum"
html_static_path = ["_static"]
html_logo = "_static/codendum-logo.png"
html_favicon = "_static/codendum-icon.png"
html_copy_source = False
html_show_sourcelink = False
html_theme_options = {
    "sidebar_hide_name": True,  # the logo already shows the name
    "source_repository": "https://github.com/stefanoferi/codendum/",
    "source_branch": "main",
    "source_directory": "docs/",
}
