"""One-off: fetch the Latin woff2 files for the Beacon display design and
self-host them under display/fonts/ so the projector page works offline.
CJK (Cantonese) uses the system font, so Noto Sans HK is not bundled.
"""

import re
import urllib.request
from pathlib import Path

OUT = Path(__file__).parent / "display" / "fonts"
OUT.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# (family css name, css2 query, [(weight, italic, out-name)])
WANT = [
    ("Archivo", "Archivo:wght@400;500;600", [
        ("400", False, "archivo-400.ttf"),
        ("500", False, "archivo-500.ttf"),
        ("600", False, "archivo-600.ttf")]),
    ("IBM Plex Mono", "IBM+Plex+Mono:ital,wght@0,400;0,500;1,400", [
        ("400", False, "plexmono-400.ttf"),
        ("500", False, "plexmono-500.ttf"),
        ("400", True, "plexmono-400i.ttf")]),
]


def css_for(query):
    url = f"https://fonts.googleapis.com/css2?family={query}&display=swap"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8")


BLOCK = re.compile(r"@font-face\s*\{(.*?)\}", re.S)


def pick(css, weight, italic):
    want_style = "italic" if italic else "normal"
    for block in BLOCK.findall(css):
        if f"font-weight: {weight}" not in block:
            continue
        if f"font-style: {want_style}" not in block:
            continue
        m = re.search(r"url\((https://[^)]+\.(?:woff2|ttf))\)", block)
        if m:
            return m.group(1)
    return None


for family, query, items in WANT:
    css = css_for(query)
    for weight, italic, name in items:
        u = pick(css, weight, italic)
        if not u:
            print(f"!! no url for {family} {weight}{'i' if italic else ''}")
            continue
        data = urllib.request.urlopen(
            urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30).read()
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes")
