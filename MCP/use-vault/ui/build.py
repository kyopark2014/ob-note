#!/usr/bin/env python3
"""Build self-contained MCP App HTML files (stdlib only).

Each app is ``src/<name>.js`` + ``src/<name>.css``; ``build.py`` inlines the official
MCP Apps client (vendor/), the shared kit and the app into one HTML document in ``dist/``.
An MCP App runs under a strict CSP, so nothing is loaded from a CDN.

    python3 MCP/use-vault/ui/build.py            # build every app
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor", "ext-apps-app-with-deps.js")
APPS = {"vault-viewer": "Vault"}


def _read(*parts: str) -> str:
    with open(os.path.join(HERE, *parts), "r", encoding="utf-8") as f:
        return f.read()


def _vendor_module() -> str:
    """Turn the ESM bundle's trailing `export{a as b,...}` into `window.__ext = {b: a,...}`."""
    src = _read("vendor", "ext-apps-app-with-deps.js").strip()
    m = re.search(r"export\s*\{([^}]*)\}\s*;?\s*$", src)
    if not m:
        raise SystemExit("vendor bundle: trailing export clause not found")
    pairs = []
    for part in (p.strip() for p in m.group(1).split(",") if p.strip()):
        local, _, exported = part.partition(" as ")
        pairs.append(f"{(exported or local).strip()}:{local.strip()}")
    return src[: m.start()] + "\nwindow.__ext={" + ",".join(pairs) + "};\n"


def build(name: str, title: str) -> str:
    vendor = _vendor_module().replace("</script", "<\\/script")
    kit_js = _read("src", "kit.js")
    app_js = _read("src", f"{name}.js")
    css = _read("src", "kit.css") + "\n" + _read("src", f"{name}.css")
    # Kit + app share one IIFE so their identifiers cannot collide with the minified client.
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<div id="root" class="k-root"></div>
<script type="module">
{vendor}
(async () => {{
{kit_js}
{app_js}
}})();
</script>
</body>
</html>
"""


def main() -> None:
    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    for name, title in APPS.items():
        html = build(name, title)
        out = os.path.join(HERE, "dist", f"{name}.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"built {os.path.relpath(out, HERE)}  {len(html) / 1024:.0f} KB")


if __name__ == "__main__":
    sys.exit(main())
