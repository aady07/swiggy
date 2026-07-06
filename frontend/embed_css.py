#!/usr/bin/env python3
"""Inline styles.css into index.html."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent
html_path = ROOT / "index.html"
css_path = ROOT / "styles.css"

css = css_path.read_text(encoding="utf-8")
block = f"<style>\n{css}\n</style>"

html = html_path.read_text(encoding="utf-8")

# Strip broken legacy CSS (raw CSS without tags, or old style blocks)
if "<!-- EMBED_CSS -->" in html:
    html = re.sub(r"<!-- EMBED_CSS -->.*?</head>", f"{block}\n</head>", html, flags=re.DOTALL)
elif re.search(r"<style>.*?</style>", html, flags=re.DOTALL):
    html = re.sub(r"<style>.*?</style>", block, html, count=1, flags=re.DOTALL)
else:
    html = html.replace("</head>", f"{block}\n</head>")

html_path.write_text(html, encoding="utf-8")
print(f"✓ Embedded CSS ({html_path.stat().st_size} bytes)")
