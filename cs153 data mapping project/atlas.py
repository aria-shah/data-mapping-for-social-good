#!/usr/bin/env python3
"""
Common Ground Atlas: a data-mapping studio for social good.

Runs a local web app (web/) plus a small JSON API that does what a browser cannot:
  - serves simplified Natural Earth boundaries (countries + global states/provinces)
  - federated dataset search: World Bank WDI, Our World in Data, HDX (humanitarian), Wikipedia
  - fetches indicator data (World Bank, OWID) as normalized tables
  - scrapes any public URL (CSV, TSV, XLSX, JSON, GeoJSON, HTML tables) into tables
  - parses uploaded files the same way

Standard library only; no pip install needed.

Usage:
  python3 atlas.py                         # start on http://127.0.0.1:8765 and open the browser
  python3 atlas.py sample_countries.csv    # preload one or more files as layers
  python3 atlas.py --port 9000 --no-open
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import io
import ipaddress
import json
import math
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
CACHE_DIR = ROOT / ".atlas_cache"

USER_AGENT = "CommonGroundAtlas/2.0 (local data-mapping app; educational use)"
MAX_FETCH_BYTES = 40 * 1024 * 1024
MAX_TABLE_ROWS = 25000
MAX_TABLES = 30

NE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
NE_COUNTRIES_50M = NE_BASE + "ne_50m_admin_0_countries.geojson"
NE_COUNTRIES_110M = NE_BASE + "ne_110m_admin_0_countries.geojson"
NE_ADMIN1_10M = NE_BASE + "ne_10m_admin_1_states_provinces.geojson"

# Caches the legacy app already downloaded; reused so first launch is fast and works offline.
LEGACY_COUNTRIES_110M = ROOT / ".ne_110m_admin0_countries_cache.json"
LEGACY_ADMIN1 = ROOT / ".subnational_global_admin1_cache.json"

PRELOAD_FILES: list[Path] = []


# --------------------------------------------------------------------------------------
# HTTP helpers (with SSRF protection for user-supplied URLs)
# --------------------------------------------------------------------------------------

class FetchError(Exception):
    pass


def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise FetchError(f"Could not resolve host {host!r}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError("Only http(s) URLs can be fetched")
    if not parsed.hostname:
        raise FetchError("URL has no host")
    if not _host_is_public(parsed.hostname):
        raise FetchError("That host resolves to a private or local address and cannot be fetched")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirect)


def http_get(url: str, *, timeout: float = 45, max_bytes: int = MAX_FETCH_BYTES, check: bool = True) -> tuple[bytes, str]:
    """GET a URL; returns (body, content_type). Raises FetchError with a readable message."""
    if check:
        _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(max_bytes + 1)
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                body = gzip.decompress(body)
    except urllib.error.HTTPError as e:
        raise FetchError(f"The server answered HTTP {e.code} for {url}")
    except urllib.error.URLError as e:
        raise FetchError(f"Could not reach {urllib.parse.urlparse(url).hostname}: {e.reason}")
    except (TimeoutError, socket.timeout):
        raise FetchError(f"Timed out fetching {url}")
    if len(body) > max_bytes:
        raise FetchError(f"Response is larger than {max_bytes // (1024 * 1024)} MB")
    return body, ctype


def cached_get(url: str, ttl: float, **kw) -> bytes:
    """GET with a disk cache under .atlas_cache/http (trusted API endpoints only)."""
    d = CACHE_DIR / "http"
    d.mkdir(parents=True, exist_ok=True)
    path = d / hashlib.sha1(url.encode()).hexdigest()
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return path.read_bytes()
    try:
        body, _ = http_get(url, check=False, **kw)
    except FetchError:
        if path.exists():  # stale beats nothing when offline
            return path.read_bytes()
        raise
    path.write_bytes(body)
    return body


DAY = 86400

_inflight: dict[str, concurrent.futures.Future] = {}
_inflight_lock = threading.Lock()
_bg = concurrent.futures.ThreadPoolExecutor(8)


def cached_get_future(url: str, ttl: float, **kw) -> concurrent.futures.Future:
    """Like cached_get, but deduplicates concurrent requests for the same URL and runs in the background."""
    with _inflight_lock:
        fut = _inflight.get(url)
        if fut is None:
            fut = _bg.submit(cached_get, url, ttl, **kw)
            _inflight[url] = fut
            fut.add_done_callback(lambda _f: _inflight.pop(url, None))
        return fut


def _is_cached(url: str, ttl: float) -> bool:
    path = CACHE_DIR / "http" / hashlib.sha1(url.encode()).hexdigest()
    return path.exists() and time.time() - path.stat().st_mtime < ttl


# --------------------------------------------------------------------------------------
# Boundaries: slim + simplify Natural Earth once, cache as gzip
# --------------------------------------------------------------------------------------

def _dp(points: list, tol: float) -> list:
    """Douglas-Peucker simplification of a polyline (iterative)."""
    n = len(points)
    if n < 5:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    tol2 = tol * tol
    while stack:
        a, b = stack.pop()
        ax, ay = points[a][0], points[a][1]
        bx, by = points[b][0], points[b][1]
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i][0], points[i][1]
            if seg == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if best > tol2 and idx > 0:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(points, keep) if k]


def _slim_ring(ring: list, tol: float, nd: int) -> list | None:
    pts = _dp(ring, tol) if tol > 0 else ring
    out = []
    for p in pts:
        q = [round(p[0], nd), round(p[1], nd)]
        if not out or out[-1] != q:
            out.append(q)
    if len(out) < 4:
        return None
    lons = [q[0] for q in out]
    if max(lons) - min(lons) > 180:  # ring wraps the antimeridian; renders as a smear across the map
        return None
    if out[0] != out[-1]:
        out.append(out[0])
    return out


def _slim_geometry(geom: dict, tol: float, nd: int) -> dict | None:
    if not geom:
        return None
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"] if geom["type"] == "MultiPolygon" else []
    out = []
    for poly in polys:
        rings = [_slim_ring(r, tol, nd) for r in poly]
        if rings and rings[0]:
            out.append([r for r in rings if r])
    if not out:
        return None
    return {"type": "Polygon", "coordinates": out[0]} if len(out) == 1 else {"type": "MultiPolygon", "coordinates": out}


def _country_iso3(p: dict) -> str:
    for k in ("ISO_A3", "ISO_A3_EH", "ADM0_A3"):
        v = p.get(k)
        if v and v != "-99":
            # Overseas fragments (e.g. Indian Ocean Territories -> AUS) would collide with the parent.
            if k == "ISO_A3_EH" and p.get("ADM0_A3") != v:
                continue
            return {"KOS": "XKX"}.get(v, v)
    return p.get("ADM0_A3") or ""


def _country_props(p: dict) -> dict:
    aliases = set()
    for k in ("NAME", "NAME_LONG", "ADMIN", "FORMAL_EN", "NAME_ALT", "BRK_NAME", "NAME_SORT", "NAME_CIAWF", "GEOUNIT", "SUBUNIT", "ABBREV", "NAME_EN"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            aliases.add(v.strip())
    codes = {c for c in (p.get("ISO_A2"), p.get("ISO_A2_EH"), p.get("WB_A3"), p.get("ADM0_A3"), p.get("GU_A3"), p.get("SU_A3"), p.get("WB_A2")) if c and c not in ("-99", "-9")}
    return {
        "iso3": _country_iso3(p),
        "iso2": p.get("ISO_A2_EH") if p.get("ISO_A2_EH") not in (None, "-99") else p.get("ISO_A2"),
        "name": p.get("NAME") or p.get("ADMIN"),
        "continent": p.get("CONTINENT"),
        "subregion": p.get("SUBREGION"),
        "region_wb": p.get("REGION_WB"),
        "income": p.get("INCOME_GRP"),
        "pop": p.get("POP_EST"),
        "lx": p.get("LABEL_X"),
        "ly": p.get("LABEL_Y"),
        "aliases": sorted(aliases),
        "codes": sorted(codes),
    }


_geo_lock = threading.Lock()


def _gz_cache(name: str, build) -> bytes:
    """Return gzip bytes of a cached JSON artifact, building it on first use."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / name
    with _geo_lock:
        if not path.exists():
            data = build()
            raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
            path.write_bytes(gzip.compress(raw, 6))
        return path.read_bytes()


def _load_ne(url: str, legacy: Path | None) -> dict:
    if legacy and legacy.exists():
        return json.loads(legacy.read_text())
    return json.loads(cached_get(url, 365 * DAY, timeout=120, max_bytes=200 * 1024 * 1024))


def build_countries(res: str) -> dict:
    if res == "lo":
        src = _load_ne(NE_COUNTRIES_110M, LEGACY_COUNTRIES_110M)
        tol, nd = 0.0, 3
    else:
        try:
            src = _load_ne(NE_COUNTRIES_50M, None)
        except FetchError:
            src = _load_ne(NE_COUNTRIES_110M, LEGACY_COUNTRIES_110M)
        tol, nd = 0.004, 3
    feats = []
    for f in src["features"]:
        g = _slim_geometry(f.get("geometry"), tol, nd)
        if not g:
            continue
        props = _country_props(f["properties"])
        if props["iso3"] == "ATA":  # Antarctica: huge, never has data, distorts globe extrusions
            props["antarctica"] = True
        feats.append({"type": "Feature", "id": props["iso3"], "properties": props, "geometry": g})
    return {"type": "FeatureCollection", "features": feats}


def _admin1_source() -> dict:
    return _load_ne(NE_ADMIN1_10M, LEGACY_ADMIN1)


def build_admin1() -> dict:
    src = _admin1_source()
    feats = []
    seen = set()
    for f in src["features"]:
        p = f["properties"]
        code = (p.get("iso_3166_2") or "").strip()
        if not code or code.endswith("~") or code in seen:
            code = p.get("adm1_code") or code
        seen.add(code)
        g = _slim_geometry(f.get("geometry"), 0.015, 3)
        if not g:
            continue
        feats.append({
            "type": "Feature",
            "id": code,
            "properties": {"code": code, "name": p.get("name"), "iso3": {"KOS": "XKX"}.get(p.get("adm0_a3"), p.get("adm0_a3")),
                           "country": p.get("admin"), "lx": p.get("longitude"), "ly": p.get("latitude")},
            "geometry": g,
        })
    return {"type": "FeatureCollection", "features": feats}


def build_admin1_index() -> list:
    """Names/codes for every state/province so the browser can match rows without loading geometry."""
    src = _admin1_source()
    out = []
    seen = set()
    for f in src["features"]:
        p = f["properties"]
        code = (p.get("iso_3166_2") or "").strip()
        if not code or code.endswith("~") or code in seen:
            code = p.get("adm1_code") or code
        seen.add(code)
        names = {n.strip() for n in [p.get("name"), p.get("name_en"), p.get("woe_name"), p.get("gn_name"), p.get("gns_name")] if isinstance(n, str) and n.strip()}
        for alt in (p.get("name_alt") or "").split("|"):
            if alt.strip():
                names.add(alt.strip())
        out.append({"code": code, "iso3": {"KOS": "XKX"}.get(p.get("adm0_a3"), p.get("adm0_a3")), "country": p.get("admin"),
                    "name": p.get("name"), "names": sorted(names), "postal": p.get("postal"), "hasc": p.get("code_hasc"),
                    "fips": p.get("fips")})
    return out


# --------------------------------------------------------------------------------------
# Parsing: CSV/TSV, XLSX, JSON, GeoJSON, HTML tables
# --------------------------------------------------------------------------------------

def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


class _TableParser(HTMLParser):
    """Extract <table>s from HTML, honoring colspan/rowspan and skipping footnotes/hidden sort keys."""

    SKIP_TAGS = {"style", "script", "sup", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []      # finished tables: {"caption", "heading", "rows": [[(text, is_th, rs, cs)]]}
        self.stack = []       # open tables
        self.cell = None
        self.skip = 0
        self.skip_stack = []
        self.heading = ""
        self.in_heading = False
        self.heading_buf = []
        self.in_caption = False
        self.links = []
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        style = (a.get("style") or "").replace(" ", "").lower()
        hidden = "display:none" in style or "sortkey" in cls or "reference" in cls or "mw-editsection" in cls or "noprint" in cls
        if tag in self.SKIP_TAGS or (hidden and tag not in ("table", "tr", "td", "th")):
            self.skip += 1
            self.skip_stack.append(tag)
            return
        if self.skip:
            if tag not in ("br", "img", "hr", "input", "meta", "link", "wbr"):
                self.skip_stack.append(tag)
                self.skip += 1
            return
        if tag == "title":
            self.in_title = True
        elif tag in ("h1", "h2", "h3", "h4"):
            self.in_heading = True
            self.heading_buf = []
        elif tag == "a" and a.get("href"):
            self.links.append([a["href"], ""])
        elif tag == "table":
            self.stack.append({"caption": "", "heading": self.heading, "rows": [], "row": None})
        elif tag == "caption" and self.stack:
            self.in_caption = True
        elif tag == "tr" and self.stack:
            t = self.stack[-1]
            if t["row"] is not None:
                t["rows"].append(t["row"])
            t["row"] = []
        elif tag in ("td", "th") and self.stack:
            t = self.stack[-1]
            if t["row"] is None:
                t["row"] = []

            def _int(v):
                try:
                    return max(1, min(int(re.sub(r"\D", "", v or "1") or 1), 200))
                except ValueError:
                    return 1
            self.cell = {"t": [], "th": tag == "th", "rs": _int(a.get("rowspan")), "cs": _int(a.get("colspan"))}
        elif tag == "br" and self.cell is not None:
            self.cell["t"].append(" ")

    def handle_endtag(self, tag):
        if self.skip:
            if self.skip_stack and self.skip_stack[-1] == tag:
                self.skip_stack.pop()
                self.skip -= 1
            elif tag in self.skip_stack:
                while self.skip_stack:
                    self.skip -= 1
                    if self.skip_stack.pop() == tag:
                        break
            return
        if tag == "title":
            self.in_title = False
        elif tag in ("h1", "h2", "h3", "h4") and self.in_heading:
            self.in_heading = False
            self.heading = _clean_text("".join(self.heading_buf))
        elif tag == "caption":
            self.in_caption = False
        elif tag in ("td", "th") and self.cell is not None and self.stack:
            t = self.stack[-1]
            t["row"].append((_clean_text("".join(self.cell["t"])), self.cell["th"], self.cell["rs"], self.cell["cs"]))
            self.cell = None
        elif tag == "tr" and self.stack:
            t = self.stack[-1]
            if t["row"] is not None:
                t["rows"].append(t["row"])
                t["row"] = None
        elif tag == "table" and self.stack:
            t = self.stack.pop()
            if t["row"]:
                t["rows"].append(t["row"])
            self.tables.append(t)

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title += data
        if self.in_heading:
            self.heading_buf.append(data)
        if self.links and self.links[-1][1] == "" and not self.stack:
            self.links[-1][1] = data.strip()[:120]
        if self.in_caption and self.stack:
            self.stack[-1]["caption"] += data
        elif self.cell is not None:
            self.cell["t"].append(data)


def _expand_grid(rows: list) -> tuple[list[list[str]], list[list[bool]]]:
    grid, is_th = [], []
    pending = {}  # col -> [remaining, text, th]
    for row in rows:
        out, ths = [], []
        col = 0
        cells = list(row)
        while cells or any(c >= col for c in pending):
            if col in pending:
                rem, text, th = pending[col]
                out.append(text)
                ths.append(th)
                if rem <= 1:
                    del pending[col]
                else:
                    pending[col] = [rem - 1, text, th]
                col += 1
                continue
            if not cells:
                if any(c > col for c in pending):
                    out.append("")
                    ths.append(False)
                    col += 1
                    continue
                break
            text, th, rs, cs = cells.pop(0)
            for _ in range(cs):
                out.append(text)
                ths.append(th)
                if rs > 1:
                    pending[col] = [rs - 1, text, th]
                col += 1
        grid.append(out)
        is_th.append(ths)
    return grid, is_th


def _table_from_grid(grid: list[list[str]], is_th: list[list[bool]], title: str) -> dict | None:
    grid = [r for r in grid if any(c for c in r)]
    if len(grid) < 3:
        return None
    width = max(len(r) for r in grid)
    if width < 2:
        return None
    grid = [r + [""] * (width - len(r)) for r in grid]
    is_th = [t + [False] * (width - len(t)) for t in is_th]
    # Leading rows that are mostly <th> form the header (multi-row headers are joined).
    nh = 0
    while nh < min(4, len(grid) - 2) and sum(is_th[nh]) >= max(1, len(is_th[nh]) * 0.6):
        nh += 1
    if nh == 0:
        nh = 1
    headers = []
    for c in range(width):
        parts = []
        for r in range(nh):
            v = grid[r][c]
            if v and v not in parts:
                parts.append(v)
        headers.append(" – ".join(parts) or f"Column {c + 1}")
    seen = {}
    for i, h in enumerate(headers):
        if h in seen:
            seen[h] += 1
            headers[i] = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
    body = grid[nh:]
    # Drop repeated header rows inside the body and footer "Total"/"World" rows are kept (client filters).
    body = [r for r in body if r != grid[0]]
    return {"title": title, "headers": headers, "rows": body[:MAX_TABLE_ROWS], "nrows": len(body), "truncated": len(body) > MAX_TABLE_ROWS}


DATA_LINK_RE = re.compile(r"\.(csv|tsv|xlsx|json|geojson|txt)(\?|#|$)", re.I)


def parse_html(text: str, base_url: str = "") -> dict:
    p = _TableParser()
    try:
        p.feed(text)
        p.close()
    except Exception:
        pass
    tables = []
    for i, t in enumerate(p.tables):
        grid, ths = _expand_grid(t["rows"])
        title = _clean_text(t["caption"]) or t["heading"] or f"Table {i + 1}"
        tb = _table_from_grid(grid, ths, title)
        if tb:
            tables.append(tb)
    tables.sort(key=lambda t: -min(t["nrows"], 300) * len(t["headers"]) ** 0.5)
    links, seen = [], set()
    for href, txt in p.links:
        if not DATA_LINK_RE.search(href):
            continue
        full = urllib.parse.urljoin(base_url, href)
        if full in seen or not full.startswith("http"):
            continue
        seen.add(full)
        links.append({"url": full, "text": txt or Path(urllib.parse.urlparse(full).path).name, "kind": DATA_LINK_RE.search(href).group(1).lower()})
    return {"title": _clean_text(p.title), "tables": tables[:MAX_TABLES], "links": links[:60]}


def _decode(body: bytes) -> str:
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def parse_delimited(text: str, title: str) -> dict:
    sample = text[:20000]
    lines = [l for l in sample.splitlines() if l.strip()][:30]
    best, best_score = ",", -1
    for d in (",", "\t", ";", "|"):
        counts = [l.count(d) for l in lines]
        if counts and min(counts) > 0:
            score = min(counts) * 10 - (max(counts) - min(counts))
            if score > best_score:
                best, best_score = d, score
    rows = list(csv.reader(io.StringIO(text), delimiter=best))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return {"tables": []}
    # Skip preamble lines (e.g. World Bank CSV downloads start with 4 metadata lines).
    width = max(len(r) for r in rows[:200])
    start = 0
    for i, r in enumerate(rows[:20]):
        if len(r) >= max(2, width * 0.8):
            start = i
            break
    headers = [h.strip() or f"Column {i + 1}" for i, h in enumerate(rows[start])]
    width = len(headers)
    body = [[c.strip() for c in r[:width]] + [""] * (width - len(r)) for r in rows[start + 1:]]
    return {"tables": [{"title": title, "headers": headers, "rows": body[:MAX_TABLE_ROWS], "nrows": len(body), "truncated": len(body) > MAX_TABLE_ROWS}]}


def parse_xlsx(body: bytes, title: str) -> dict:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    zf = zipfile.ZipFile(io.BytesIO(body))
    shared = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    wb = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map = {r.get("Id"): r.get("Target") for r in rels}
    tables = []
    for sheet in wb.findall("m:sheets/m:sheet", ns)[:12]:
        target = rel_map.get(sheet.get(rel_ns), "")
        path = "xl/" + target.lstrip("/").removeprefix("xl/")
        if path not in zf.namelist():
            continue
        root = ElementTree.fromstring(zf.read(path))
        grid = []
        for row in root.iter("{%s}row" % ns["m"]):
            vals = {}
            for c in row.findall("m:c", ns):
                ref = c.get("r") or ""
                letters = re.match(r"[A-Z]+", ref)
                col = 0
                if letters:
                    for ch in letters.group(0):
                        col = col * 26 + (ord(ch) - 64)
                    col -= 1
                else:
                    col = len(vals)
                t = c.get("t")
                v = c.find("m:v", ns)
                if t == "s" and v is not None:
                    text = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
                elif t == "inlineStr":
                    text = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
                else:
                    text = v.text if v is not None and v.text else ""
                    if re.fullmatch(r"-?\d+\.\d{9,}", text):
                        text = repr(round(float(text), 6))
                if col < 400:
                    vals[col] = text.strip()
            if vals:
                w = max(vals) + 1
                grid.append([vals.get(i, "") for i in range(w)])
            if len(grid) > MAX_TABLE_ROWS + 20:
                break
        if not grid:
            continue
        width = max(len(r) for r in grid[:300])
        start = next((i for i, r in enumerate(grid[:30]) if sum(1 for c in r if c) >= max(2, width * 0.6)), 0)
        headers = [h or f"Column {i + 1}" for i, h in enumerate(grid[start] + [""] * (width - len(grid[start])))]
        body_rows = [r + [""] * (width - len(r)) for r in grid[start + 1:]]
        if len(body_rows) >= 2:
            tables.append({"title": f"{title} – {sheet.get('name')}", "headers": headers, "rows": body_rows[:MAX_TABLE_ROWS],
                           "nrows": len(body_rows), "truncated": len(body_rows) > MAX_TABLE_ROWS})
    return {"tables": tables}


def _flatten(obj: dict, prefix: str = "", depth: int = 0) -> dict:
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and depth < 2:
            out.update(_flatten(v, key + ".", depth + 1))
        elif isinstance(v, list):
            if v and all(isinstance(x, dict) for x in v) and depth < 2:
                # e.g. EONET categories: [{"title": "Wildfires"}] -> "Wildfires"
                titles = [str(x.get("title") or x.get("name") or x.get("value") or "") for x in v]
                out[key] = ", ".join(t for t in titles if t)
            elif all(not isinstance(x, (dict, list)) for x in v):
                out[key] = ", ".join(str(x) for x in v)
        else:
            out[key] = "" if v is None else v
    return out


def _records_to_table(records: list[dict], title: str) -> dict:
    flat = [_flatten(r) for r in records[:MAX_TABLE_ROWS]]
    headers = []
    for r in flat[:500]:
        for k in r:
            if k not in headers:
                headers.append(k)
    rows = [[("" if r.get(h) is None else str(r.get(h))) for h in headers] for r in flat]
    return {"title": title, "headers": headers, "rows": rows, "nrows": len(records), "truncated": len(records) > MAX_TABLE_ROWS}


def _geom_point(geom: dict) -> tuple[float, float] | None:
    if not geom:
        return None
    t, c = geom.get("type"), geom.get("coordinates")
    try:
        if t == "Point":
            return float(c[0]), float(c[1])
        if t == "MultiPoint" or t == "LineString":
            return float(c[0][0]), float(c[0][1])
        if t == "Polygon":
            ring = c[0]
            return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
        if t == "MultiPolygon":
            ring = c[0][0]
            return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
        if t == "GeometryCollection":
            return _geom_point((geom.get("geometries") or [None])[0])
    except (TypeError, IndexError, ValueError, ZeroDivisionError):
        return None
    return None


def parse_json(data, title: str) -> dict:
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        records = []
        for f in data.get("features", [])[:MAX_TABLE_ROWS]:
            props = dict(f.get("properties") or {})
            pt = _geom_point(f.get("geometry"))
            if pt:
                rec = {"latitude": round(pt[1], 5), "longitude": round(pt[0], 5)}
                rec.update(props)
                records.append(rec)
        if not records:
            raise FetchError("GeoJSON has no usable features")
        t = _records_to_table(records, data.get("metadata", {}).get("title") or title)
        return {"tables": [t]}
    # World Bank style [meta, rows]
    if isinstance(data, list) and len(data) == 2 and isinstance(data[0], dict) and isinstance(data[1], list):
        data = data[1]
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data[:50]):
        return {"tables": [_records_to_table(data, title)]}
    if isinstance(data, list) and data and all(isinstance(x, list) for x in data[:50]):
        headers = [str(h) for h in data[0]]
        rows = [[("" if c is None else str(c)) for c in r] for r in data[1:MAX_TABLE_ROWS]]
        return {"tables": [{"title": title, "headers": headers, "rows": rows, "nrows": len(rows), "truncated": False}]}
    # Find the largest list of objects anywhere (e.g. {"data": [...]}, {"result": {"records": [...]}})
    best = None

    def walk(o, depth=0):
        nonlocal best
        if depth > 4:
            return
        if isinstance(o, list) and o and all(isinstance(x, dict) for x in o[:20]):
            if best is None or len(o) > len(best):
                best = o
        elif isinstance(o, dict):
            for v in o.values():
                walk(v, depth + 1)
    walk(data)
    if best:
        return {"tables": [_records_to_table(best, title)]}
    if isinstance(data, dict) and data and all(isinstance(v, (int, float)) for v in data.values()):
        rows = [[k, str(v)] for k, v in data.items()]
        return {"tables": [{"title": title, "headers": ["key", "value"], "rows": rows, "nrows": len(rows), "truncated": False}]}
    raise FetchError("JSON did not contain a table-like structure")


def parse_bytes(body: bytes, ctype: str, name: str, base_url: str = "") -> dict:
    """Turn arbitrary bytes into {"title", "tables": [...], "links": [...]}."""
    title = Path(urllib.parse.urlparse(name).path).stem.replace("_", " ").replace("-", " ").strip() or "Imported data"
    lower = name.lower().split("?")[0]
    if body[:2] == b"PK" or lower.endswith(".xlsx"):
        try:
            out = parse_xlsx(body, title)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
            raise FetchError(f"Could not read the spreadsheet ({e}). Only .xlsx (not .xls) is supported.")
        out["title"] = title
        return out
    if body[:8].startswith(b"\xd0\xcf\x11\xe0"):
        raise FetchError("Legacy .xls files are not supported. Save as .xlsx or .csv and try again.")
    text = _decode(body)
    stripped = text.lstrip()[:200].lower()
    if "html" in ctype or stripped.startswith("<!doctype html") or stripped.startswith("<html") or ("<table" in text[:200000] and "<" == stripped[:1]):
        out = parse_html(text, base_url)
        out["title"] = out.get("title") or title
        return out
    if "json" in ctype or lower.endswith((".json", ".geojson")) or stripped[:1] in ("{", "["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            out = parse_json(data, title)
            out["title"] = title
            return out
    out = parse_delimited(text, title)
    out["title"] = title
    return out


# --------------------------------------------------------------------------------------
# Search providers
# --------------------------------------------------------------------------------------

STOP = {"the", "of", "and", "in", "by", "a", "an", "to", "for", "on", "per", "rate", "data", "country", "countries", "world", "share", "with"}


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in STOP]


# Everyday words -> the vocabulary statistical agencies use in indicator titles.
SYNONYMS = {
    "child": ["under", "infant", "children", "neonatal", "youth"], "children": ["child", "under", "infant"],
    "kids": ["child", "children"], "baby": ["infant", "neonatal"], "women": ["female", "maternal", "women"],
    "girls": ["female"], "men": ["male"], "poor": ["poverty"], "poverty": ["poor", "headcount"],
    "hunger": ["undernourishment", "food", "stunting", "malnutrition"], "food": ["undernourishment", "hunger"],
    "water": ["drinking", "water"], "toilet": ["sanitation"], "toilets": ["sanitation"], "school": ["education", "enrollment", "school"],
    "schooling": ["education", "school"], "education": ["school", "literacy", "enrollment"], "internet": ["internet", "broadband"],
    "electricity": ["electricity", "power"], "power": ["electricity"], "co2": ["co2", "carbon", "emissions"],
    "carbon": ["co2", "emissions"], "pollution": ["pm2", "pollution", "particulate"], "air": ["pm2", "air"],
    "jobs": ["employment", "unemployment"], "unemployed": ["unemployment"], "wealth": ["gdp", "income"],
    "rich": ["gdp", "income"], "income": ["gdp", "income", "gni"], "doctors": ["physicians"], "nurses": ["nurses"],
    "death": ["mortality", "deaths"], "deaths": ["mortality", "death"], "dying": ["mortality"], "life": ["life"],
    "inequality": ["gini", "inequality", "share"], "vaccines": ["immunization", "vaccine"], "vaccination": ["immunization"],
    "refugees": ["refugee", "displaced"], "homicide": ["homicides", "intentional"], "murder": ["homicides"],
    "trees": ["forest"], "forests": ["forest"], "renewables": ["renewable"], "slums": ["slums", "slum"],
}


def _score(query_toks: list[str], title: str, body: str = "") -> float:
    if not query_toks:
        return 0.0
    tt = set(_tokens(title))
    bt = set(_tokens(body)) if body else set()
    s = 0.0
    for q in query_toks:
        syn = SYNONYMS.get(q, [])
        if q in tt:
            s += 1.0
        elif any(t.startswith(q) or (len(q) > 4 and q[:-1] in t) for t in tt):
            s += 0.7
        elif any(x in tt for x in syn):
            s += 0.65
        elif q in bt or any(x in bt for x in syn):
            s += 0.2
    s /= len(query_toks)
    if s > 0:
        s += 0.15 / (1 + len(tt) / 8)  # prefer concise titles
    return s


def _wb_countries() -> dict:
    body = cached_get("https://api.worldbank.org/v2/country?format=json&per_page=400", 30 * DAY)
    data = json.loads(body)[1]
    return {c["id"]: c["name"] for c in data if (c.get("region") or {}).get("id") != "NA"}


def _wb_indicators() -> list:
    body = cached_get("https://api.worldbank.org/v2/source/2/indicator?format=json&per_page=4000", 7 * DAY, timeout=90)
    return json.loads(body)[1]


def search_wb(q: str) -> list:
    toks = _tokens(q)
    out = []
    for ind in _wb_indicators():
        s = _score(toks, ind["name"], ind.get("sourceNote") or "")
        if s >= 0.5:
            out.append({"source": "wb", "id": ind["id"], "title": ind["name"], "description": (ind.get("sourceNote") or "")[:400],
                        "meta": (ind.get("sourceOrganization") or "")[:120], "geo": "countries", "score": s,
                        "url": f"https://data.worldbank.org/indicator/{ind['id']}"})
    out.sort(key=lambda r: -r["score"])
    return out[:25]


def _owid_slugs() -> list[str]:
    body = cached_get("https://ourworldindata.org/sitemap.xml", 3 * DAY, timeout=60)
    return sorted(set(re.findall(r"https://ourworldindata\.org/grapher/([a-z0-9\-]+)", body.decode("utf-8", "replace"))))


def search_owid(q: str) -> list:
    toks = _tokens(q)
    out = []
    for slug in _owid_slugs():
        title = slug.replace("-", " ")
        s = _score(toks, title)
        if s >= 0.55:
            nice = title[:1].upper() + title[1:]
            out.append({"source": "owid", "id": slug, "title": nice, "description": "Our World in Data chart (country time series).",
                        "geo": "countries", "score": s, "url": f"https://ourworldindata.org/grapher/{slug}"})
    out.sort(key=lambda r: -r["score"])
    return out[:25]


HDX_FORMATS = {"csv", "xlsx", "json", "geojson", "tsv", "txt"}


def search_hdx(q: str) -> list:
    url = "https://data.humdata.org/api/3/action/package_search?" + urllib.parse.urlencode({"q": q, "rows": 14})
    data = json.loads(cached_get(url, DAY, timeout=25))
    out = []
    for p in data.get("result", {}).get("results", []):
        res = [{"name": r.get("name") or r.get("description") or "resource", "format": (r.get("format") or "").lower(), "url": r.get("url")}
               for r in p.get("resources", []) if (r.get("format") or "").lower() in HDX_FORMATS and r.get("url")]
        if not res:
            continue
        locs = ", ".join(g.get("display_name", "") for g in (p.get("groups") or [])[:4])
        out.append({"source": "hdx", "id": p["name"], "title": p.get("title") or p["name"],
                    "description": re.sub(r"\s+", " ", re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", p.get("notes") or ""))[:400],
                    "meta": " · ".join(x for x in [(p.get("organization") or {}).get("title"), locs] if x),
                    "resources": res[:12], "score": 0.6, "url": f"https://data.humdata.org/dataset/{p['name']}"})
    return out


def search_wiki(q: str) -> list:
    queries = [f'intitle:list {q} by country', f'{q} by country', f'intitle:list {q} by state']
    seen, out = set(), []

    def run(sq):
        url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": sq, "srlimit": 10, "format": "json"})
        return json.loads(cached_get(url, DAY, timeout=20)).get("query", {}).get("search", [])

    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        results = list(ex.map(lambda sq: _safe(run, sq, []), queries))
    for rank, hit in enumerate([h for rs in results for h in rs]):
        t = hit["title"]
        if t in seen:
            continue
        seen.add(t)
        tl = t.lower()
        tabular = any(k in tl for k in ("by country", "by state", "by province", "countries", "list of", "by region", "states by", "by territory"))
        s = _score(_tokens(q), t) + (0.35 if tabular else 0)
        if s < 0.3:
            continue
        snippet = re.sub(r"<[^>]+>", "", hit.get("snippet", ""))
        out.append({"source": "wiki", "id": t, "title": t, "description": snippet, "score": s,
                    "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(t.replace(" ", "_"))})
    out.sort(key=lambda r: -r["score"])
    return out[:12]


def _safe(fn, arg, default):
    try:
        return fn(arg)
    except Exception as e:  # one provider failing must not break search
        print(f"  [search] {fn.__name__} failed: {e}", file=sys.stderr)
        return default


PROVIDERS = {"wb": search_wb, "owid": search_owid, "hdx": search_hdx, "wiki": search_wiki}


def federated_search(q: str, sources: list[str]) -> dict:
    sources = [s for s in sources if s in PROVIDERS] or list(PROVIDERS)
    out, errors = {}, []
    with concurrent.futures.ThreadPoolExecutor(len(sources)) as ex:
        futs = {s: ex.submit(PROVIDERS[s], q) for s in sources}
        for s, f in futs.items():
            try:
                out[s] = f.result(timeout=60)
            except Exception as e:
                out[s] = []
                errors.append(f"{s}: {e}")
    return {"query": q, "results": out, "errors": errors}


# --------------------------------------------------------------------------------------
# Indicator fetchers
# --------------------------------------------------------------------------------------

def _unit_from_name(name: str) -> str:
    m = re.search(r"\(([^()]*)\)\s*$", name)
    return m.group(1) if m else ""


def fetch_wb(ind: str, wait_full: bool = False) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.]+", ind):
        raise FetchError("Bad indicator id")
    meta = json.loads(cached_get(f"https://api.worldbank.org/v2/indicator/{ind}?format=json", 30 * DAY))
    if not isinstance(meta, list) or len(meta) < 2 or not meta[1]:
        raise FetchError(f"World Bank indicator {ind} not found")
    m = meta[1][0]
    url = f"https://api.worldbank.org/v2/country/all/indicator/{ind}?format=json&per_page=20000&date=1990:2030"
    partial = False
    full = cached_get_future(url, 3 * DAY, timeout=180)
    try:
        # The World Bank API sometimes takes a minute for full series; don't make people wait.
        data = json.loads(full.result(timeout=None if wait_full else 18))
    except concurrent.futures.TimeoutError:
        quick = f"https://api.worldbank.org/v2/country/all/indicator/{ind}?format=json&per_page=400&mrnev=1"
        data = json.loads(cached_get(quick, DAY, timeout=40))
        partial = True
    if not isinstance(data, list) or len(data) < 2:
        msg = (data[0].get("message") or [{}])[0].get("value") if isinstance(data, list) and data else "no data"
        raise FetchError(f"World Bank returned no data: {msg}")
    countries = _wb_countries()
    rows = []
    for r in data[1] or []:
        iso = r.get("countryiso3code")
        if r.get("value") is None or iso not in countries:
            continue
        rows.append([iso, countries[iso], r.get("date"), r["value"]])
    if not rows:
        raise FetchError("This indicator has no country-level values")
    name = m["name"]
    return {"title": name, "source": "World Bank – " + (m.get("source") or {}).get("value", "WDI"), "partial": partial,
            "sourceUrl": f"https://data.worldbank.org/indicator/{ind}", "description": m.get("sourceNote") or "",
            "unit": _unit_from_name(name), "ref": f"wb:{ind}",
            "tables": [{"title": name, "headers": ["ISO3", "Country", "Year", name], "rows": rows, "nrows": len(rows), "truncated": False}],
            "hint": {"loc": "ISO3", "year": "Year", "values": [name], "geo": "countries"}}


def fetch_owid(slug: str) -> dict:
    if not re.fullmatch(r"[a-z0-9\-]+", slug):
        raise FetchError("Bad chart id")
    base = f"https://ourworldindata.org/grapher/{slug}"
    q = "?v=1&csvType=full&useColumnShortNames=true"
    body = cached_get(base + ".csv" + q, DAY, timeout=90)
    try:
        meta = json.loads(cached_get(base + ".metadata.json" + q, DAY, timeout=60))
    except (FetchError, json.JSONDecodeError):
        meta = {}
    chart = meta.get("chart") or {}
    cols = meta.get("columns") or {}
    reader = list(csv.reader(io.StringIO(_decode(body))))
    if len(reader) < 2:
        raise FetchError("Chart has no data")
    headers = reader[0]
    lower = [h.lower() for h in headers]
    if "code" not in lower:
        raise FetchError("This Our World in Data chart is not country-based")
    ci = lower.index("code")
    drop = {i for i, h in enumerate(lower) if h.startswith("world region") or h.startswith("owid_region")}
    headers = [h for i, h in enumerate(headers) if i not in drop]
    lower = [h.lower() for h in headers]
    ci = lower.index("code")
    value_cols = [h for h in headers if h.lower() not in ("entity", "code", "year", "day")]
    vi = headers.index(value_cols[0]) if value_cols else None
    rows = []
    for r in reader[1:]:
        r = [c for i, c in enumerate(r) if i not in drop]
        if len(r) == len(headers) and len(r[ci]) == 3 and (vi is None or r[vi] != ""):
            rows.append(r)
    nice_headers = []
    for h in headers:
        c = cols.get(h) or {}
        nice_headers.append(c.get("titleShort") or c.get("title") or h)
    nice_values = [nice_headers[headers.index(h)] for h in value_cols]
    first = cols.get(value_cols[0], {}) if value_cols else {}
    title = chart.get("title") or slug.replace("-", " ").capitalize()
    year_col = "Year" if "year" in lower else ("Day" if "day" in lower else None)
    if year_col:
        nice_headers[lower.index(year_col.lower())] = year_col
    nice_headers[ci] = "Code"
    return {"title": title, "source": "Our World in Data – " + (chart.get("citation") or first.get("citationShort") or ""),
            "sourceUrl": base, "description": first.get("descriptionShort") or chart.get("subtitle") or "",
            "unit": first.get("unit") or first.get("shortUnit") or "", "ref": f"owid:{slug}",
            "tables": [{"title": title, "headers": nice_headers, "rows": rows[:MAX_TABLE_ROWS], "nrows": len(rows), "truncated": len(rows) > MAX_TABLE_ROWS}],
            "hint": {"loc": "Code", "year": year_col, "values": nice_values[:1], "geo": "countries"}}


def fetch_url(url: str) -> dict:
    url = url.strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    body, ctype = http_get(url)
    out = parse_bytes(body, ctype, url, base_url=url)
    out["title"] = re.sub(r"\s+[-–—|]\s+Wikipedia$", "", out.get("title") or "")
    out.update({"source": urllib.parse.urlparse(url).hostname, "sourceUrl": url, "ref": f"url:{url}"})
    if not out.get("tables"):
        if out.get("links"):
            out["message"] = "No tables on this page, but it links to data files you can open."
        else:
            raise FetchError("No tables or data files were found at that address")
    return out


def fetch_local(name: str) -> dict:
    """Sample files that ship next to atlas.py (by bare file name only)."""
    path = ROOT / Path(name).name
    if path.suffix.lower() not in (".csv", ".tsv", ".xlsx", ".json", ".geojson") or not path.is_file():
        raise FetchError(f"No sample file named {name}")
    out = parse_bytes(path.read_bytes(), "", path.name)
    out.update({"source": "Sample file", "sourceUrl": "", "ref": f"local:{path.name}"})
    return out


_warm_queue: list[tuple[str, str]] = []
_warm_lock = threading.Lock()


def warm_refs(refs: list[str]) -> None:
    """Fetch indicators in the background so clicking them later is instant."""
    with _warm_lock:
        start = not _warm_queue
        for r in refs:
            src, _, ident = r.partition(":")
            if src in ("wb", "owid") and (src, ident) not in _warm_queue:
                _warm_queue.append((src, ident))
    if not start:
        return

    def run():
        while True:
            with _warm_lock:
                if not _warm_queue:
                    return
                src, ident = _warm_queue[0]
            try:
                fetch_wb(ident, wait_full=True) if src == "wb" else fetch_owid(ident)
            except Exception as e:
                print(f"  [warm] {src}:{ident}: {e}", file=sys.stderr)
            with _warm_lock:
                _warm_queue.pop(0)
    threading.Thread(target=run, daemon=True).start()


def fetch_hdx(pkg: str) -> dict:
    url = "https://data.humdata.org/api/3/action/package_show?" + urllib.parse.urlencode({"id": pkg})
    p = json.loads(cached_get(url, DAY))["result"]
    links = [{"url": r["url"], "text": r.get("name") or r.get("description") or r["url"], "kind": (r.get("format") or "").lower()}
             for r in p.get("resources", []) if (r.get("format") or "").lower() in HDX_FORMATS]
    return {"title": p.get("title"), "source": "HDX – " + (p.get("organization") or {}).get("title", ""), "tables": [], "links": links,
            "sourceUrl": f"https://data.humdata.org/dataset/{pkg}", "message": "Pick a file from this humanitarian dataset."}


# --------------------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------------------

class AtlasHandler(SimpleHTTPRequestHandler):
    server_version = "CommonGroundAtlas/2.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s %s\n" % (self.command, self.path[:140]))

    def end_headers(self):
        if not (self.path or "").startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _json(self, obj, status=200):
        raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str).encode()
        self._send(raw, "application/json; charset=utf-8", status)

    def _send(self, raw: bytes, ctype: str, status=200, pre_gzipped=False):
        accepts = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if pre_gzipped and not accepts:
            raw, pre_gzipped = gzip.decompress(raw), False
        elif not pre_gzipped and accepts and len(raw) > 2048:
            raw, pre_gzipped = gzip.compress(raw, 5), True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if pre_gzipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, msg, status=400):
        self._json({"error": msg}, status)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return super().do_GET()
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        route = parsed.path[5:]
        try:
            if route == "health":
                return self._json({"ok": True, "preload": len(PRELOAD_FILES)})
            if route == "geo/countries":
                res = "lo" if q.get("res") == "lo" else "hi"
                return self._send(_gz_cache(f"countries_{res}.json.gz", lambda: build_countries(res)), "application/json", pre_gzipped=True)
            if route == "geo/admin1":
                return self._send(_gz_cache("admin1.json.gz", build_admin1), "application/json", pre_gzipped=True)
            if route == "geo/admin1-index":
                return self._send(_gz_cache("admin1_index.json.gz", build_admin1_index), "application/json", pre_gzipped=True)
            if route == "search":
                query = (q.get("q") or "").strip()
                if len(query) < 2:
                    return self._err("Type at least two characters")
                return self._json(federated_search(query[:200], (q.get("sources") or "").split(",")))
            if route == "fetch":
                src, ident = q.get("source"), q.get("id") or q.get("url") or ""
                if src == "wiki":
                    src, ident = "url", "https://en.wikipedia.org/wiki/" + urllib.parse.quote(ident.replace(" ", "_"))
                fn = {"wb": fetch_wb, "owid": fetch_owid, "url": fetch_url, "hdx": fetch_hdx, "local": fetch_local}.get(src)
                if not fn or not ident:
                    return self._err("Need source=wb|owid|url|hdx|wiki and id")
                return self._json(fn(ident))
            if route == "warm":
                warm_refs([r for r in (q.get("refs") or "").split(",") if r][:80])
                return self._json({"ok": True})
            if route == "preload":
                out = []
                for p in PRELOAD_FILES:
                    try:
                        d = parse_bytes(p.read_bytes(), "", p.name)
                        d.update({"source": "Local file", "sourceUrl": "", "ref": f"file:{p.name}"})
                        out.append(d)
                    except Exception as e:
                        out.append({"error": f"{p.name}: {e}"})
                return self._json({"datasets": out})
            return self._err("Unknown endpoint", 404)
        except FetchError as e:
            return self._err(str(e), 502)
        except Exception as e:
            print(f"  [error] {route}: {e!r}", file=sys.stderr)
            return self._err(f"Unexpected error: {e}", 500)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/parse":
            return self._err("Unknown endpoint", 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_FETCH_BYTES:
            return self._err("File is empty or larger than 40 MB", 413)
        body = self.rfile.read(length)
        name = urllib.parse.unquote(self.headers.get("X-Filename") or "upload.csv")
        try:
            out = parse_bytes(body, self.headers.get("Content-Type") or "", name)
            out.update({"source": "Uploaded file", "sourceUrl": "", "ref": f"file:{name}"})
            if not out.get("tables"):
                return self._err("No tables found in that file", 422)
            return self._json(out)
        except FetchError as e:
            return self._err(str(e), 422)
        except Exception as e:
            return self._err(f"Could not parse file: {e}", 422)


def _warm_caches():
    """Build boundary caches in the background so the first page load is quick."""
    for name, fn in (("countries_lo.json.gz", lambda: build_countries("lo")), ("countries_hi.json.gz", lambda: build_countries("hi")),
                     ("admin1_index.json.gz", build_admin1_index)):
        try:
            _gz_cache(name, fn)
        except Exception as e:
            print(f"  [warm] {name}: {e}", file=sys.stderr)
    for fn in (_wb_indicators, _owid_slugs, _wb_countries):
        try:
            fn()
        except Exception as e:
            print(f"  [warm] {fn.__name__}: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Common Ground Atlas: map data for social good.")
    ap.add_argument("files", nargs="*", help="CSV/XLSX/JSON files to preload as layers")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args()

    for f in args.files:
        p = Path(f).expanduser().resolve()
        if not p.exists():
            sys.exit(f"File not found: {f}")
        PRELOAD_FILES.append(p)

    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer((args.host, port), AtlasHandler)
            break
        except OSError:
            continue
    if not server:
        sys.exit(f"No free port between {args.port} and {args.port + 19}")
    server.daemon_threads = True
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"\n  Common Ground Atlas running at {url}\n  Press Ctrl+C to stop.\n")
    threading.Thread(target=_warm_caches, daemon=True).start()
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
