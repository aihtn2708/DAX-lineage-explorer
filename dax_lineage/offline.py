"""The explorer as one HTML file that runs with no server and no network.

Everything the site in docs/ loads (stylesheet, scripts, the sample files the
"Try the sample" buttons open, the downloadable samples and queries) is put
inside the page. Its Content-Security-Policy allows inline code and nothing
else: no request can leave the page, not even to the machine it came from.

    python3 scripts/build_offline.py      # regenerate the committed copies
    daxlin offline -o explorer.html       # write it out from an installed package
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

PACKAGED = Path(__file__).with_name("web") / "dax-lineage-explorer.html"

CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src data: blob:; connect-src 'none'; font-src 'none'; object-src 'none'; frame-src 'none'; "
       "worker-src 'none'; base-uri 'none'; form-action 'none'")

MIME = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".json": "application/json", ".dax": "text/plain", ".csv": "text/csv"}


def _inline_safe(text: str) -> str:
    # a script or style body must not close its own element
    return re.sub(r"</(script|style)", r"<\\/\1", text, flags=re.I)


def build(docs: Path) -> str:
    html = (docs / "index.html").read_text(encoding="utf-8")

    # the site's policy allows its own files; the offline page has none to load
    html = re.sub(r'<meta http-equiv="Content-Security-Policy" content="[^"]*">',
                  f'<meta http-equiv="Content-Security-Policy" content="{CSP}">', html)

    css = (docs / "css" / "app.css").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="css/app.css">', f"<style>\n{_inline_safe(css)}</style>")

    samples = {}
    for path in sorted((docs / "samples").iterdir()):
        samples[f"samples/{path.name}"] = base64.b64encode(path.read_bytes()).decode("ascii")
    embedded = f"<script>window.DAX_LINEAGE_EMBEDDED = {json.dumps(samples)};</script>\n"

    def inline_script(match):
        src = match.group(1)
        code = (docs / src).read_text(encoding="utf-8")
        return f"<script>\n/* {src} */\n{_inline_safe(code)}\n</script>"

    first = html.index('<script src="js/')
    html = html[:first] + embedded + html[first:]
    html = re.sub(r'<script src="(js/[^"]+)"></script>', inline_script, html)

    # download links point at data: URLs, so they work from disk too
    def data_url(match):
        attr, rel = match.group(1), match.group(2)
        path = docs / rel
        if not path.is_file():
            return match.group(0)
        mime = MIME.get(path.suffix.lower(), "application/octet-stream")
        return f'{attr}="data:{mime};base64,{base64.b64encode(path.read_bytes()).decode("ascii")}"'

    html = re.sub(r'(href)="((?:samples|queries)/[^"]+)"', data_url, html)
    # the page offering itself for download makes no sense once you have it
    html = re.sub(r'\s*<a href="dax-lineage-explorer.html" download>dax-lineage-explorer.html</a>\s*'
                  r'<small>[^<]*</small>', "", html)
    html = html.replace(""" <a href="dax-lineage-explorer.html" download>Download it</a> to run offline.""",
                        " This copy runs from your disk.")
    leftover = re.findall(r'(?:src|href)="(?!data:|#|https://github\.com/)([^"]+)"', html)
    if leftover:
        raise ValueError(f"offline page still refers to outside files: {leftover}")
    return html


def packaged() -> str:
    if not PACKAGED.exists():
        raise SystemExit("the offline page is not in this install; build it with scripts/build_offline.py")
    return PACKAGED.read_text(encoding="utf-8")
