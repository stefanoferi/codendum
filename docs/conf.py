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
release = "0.1.0"

extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build", "requirements.txt", "requirements.in"]

# Anchors for "page.md#section" links, as GitHub renders them.
myst_heading_anchors = 3

html_theme = "furo"
html_title = "Codendum"
html_copy_source = False
html_show_sourcelink = False
html_theme_options = {
    "source_repository": "https://github.com/stefanoferi/codendum/",
    "source_branch": "main",
    "source_directory": "docs/",
}
