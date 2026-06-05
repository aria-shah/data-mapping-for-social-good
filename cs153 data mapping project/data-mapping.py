#!/usr/bin/env python3
"""
Data Mapper - Social impact mapping: visualize data and get actionable takeaways.

Maps location-based data (e.g. life expectancy, health, equity metrics) and
automatically adds key takeaways and recommendations so users can focus resources,
advocate for change, or use in reports and grant proposals.

Features:
  - Color-coded map (countries or points) from CSV
  - Key takeaways: range, disparity, top/bottom locations
  - Priority recommendations: areas to focus support (e.g. bottom quartile)
  - Optional benchmark: flag locations below a target value
  - Export impact report (--report) for sharing or grant writing

Usage:
  python data-mapping.py data.csv
  python data-mapping.py data.csv --open          # generate and open map in browser (default)
  python data-mapping.py data.csv --serve        # generate and run map as live local website
  python data-mapping.py data.csv --report impact_report.txt
  python data-mapping.py data.csv --benchmark 70 --title "Life expectancy"
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import pandas as pd
import folium
from folium.plugins import FloatImage
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np

# Optional geocoding for city names
try:
    from geopy.geocoders import Nominatim
    from geopy.extra.rate_limiter import RateLimiter
    HAS_GEOPY = True
except ImportError:
    HAS_GEOPY = False

# Bump when the injected UI/JS behavior changes.
# This is used so the HTML template can't silently go stale.
DM_TEMPLATE_VERSION = "2026-06-04-point-radius"

# Hosts allowed through GET /dm-proxy?url= when using --serve (browser CORS bypass).
DM_PROXY_ALLOWED_NETLOCS = frozenset(
    {
        "api.worldbank.org",
        "data.worldbank.org",
        "ourworldindata.org",
        "raw.githubusercontent.com",
        "gist.githubusercontent.com",
    }
)
DM_PROXY_MAX_BYTES = 12 * 1024 * 1024


def _dm_proxy_host_allowed(hostname: str) -> bool:
    if not hostname:
        return False
    hn = hostname.lower().rstrip(".")
    if hn.startswith("www."):
        hn = hn[4:]
    if hn in DM_PROXY_ALLOWED_NETLOCS:
        return True
    if hn.endswith(".ourworldindata.org") or hn.endswith(".worldbank.org"):
        return True
    return False


def create_data_mapper_request_handler(directory: str):
    """HTTP handler: static files, GET /dm-proxy (allowlisted URLs), POST /dm-openai (OpenAI relay for browsers)."""
    from http.server import SimpleHTTPRequestHandler

    class DataMapperRequestHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            # partial(..., directory=...) forwards directory in kwargs; do not pass it twice to super().
            kwargs.pop("directory", None)
            super().__init__(*args, directory=directory, **kwargs)

        def do_OPTIONS(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/dm-openai":
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/dm-openai":
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            auth = (self.headers.get("Authorization") or "").strip()
            if not auth.startswith("Bearer ") or len(auth) < 24:
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(
                    b'{"error":{"message":"Missing or invalid Authorization: Bearer <OpenAI key>"}}'
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 650_000:
                self.send_response(413)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"Request body empty or too large"}}')
                return
            body = self.rfile.read(length)
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": auth,
                    "User-Agent": "DataMapper/1.0 (local relay)",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    out = resp.read()
                    ctype = resp.headers.get("Content-Type") or "application/json"
            except urllib.error.HTTPError as e:
                err_b = e.read() if e.fp else b""
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(err_b if err_b else str(e).encode("utf-8", errors="replace"))
                return
            except Exception as e:
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"error": {"message": str(e)}}).encode("utf-8", errors="replace")
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", (ctype or "application/json").split(";")[0].strip())
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/dm-proxy":
                self._dm_serve_proxy(parsed)
                return
            super().do_GET()

        def _dm_serve_proxy(self, parsed):
            qs = urllib.parse.parse_qs(parsed.query or "")
            urls = qs.get("url") or []
            if not urls:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"missing url query parameter")
                return
            target = urllib.parse.unquote(urls[0])
            t = urllib.parse.urlparse(target)
            if t.scheme not in ("http", "https"):
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"only http(s) URLs are allowed")
                return
            if not _dm_proxy_host_allowed(t.hostname or ""):
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"host not allowed for proxy")
                return

            req = urllib.request.Request(
                target,
                headers={"User-Agent": "DataMapper/1.0 (local proxy; https://github.com/)"},
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                    body = resp.read(DM_PROXY_MAX_BYTES + 1)
            except urllib.error.HTTPError as e:
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"upstream HTTP {e.code}".encode("utf-8", errors="replace"))
                return
            except Exception as e:
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8", errors="replace"))
                return

            if len(body) > DM_PROXY_MAX_BYTES:
                self.send_response(413)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"response too large")
                return

            self.send_response(200)
            self.send_header("Content-Type", ctype.split(";")[0].strip())
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            try:
                if self.path.startswith("/dm-proxy") or self.path.startswith("/dm-openai"):
                    return
            except Exception:
                pass
            super().log_message(fmt, *args)

    return DataMapperRequestHandler

# Template file: UI lives here; Python only injects map + data. Edit the template without it being overwritten.
def _get_template_path() -> Path:
    """Path to data_map_template.html next to this script."""
    return Path(__file__).resolve().parent / "data_map_template.html"


def _get_folium_head_deps() -> str:
    """Script and link tags required for Folium/Leaflet (used in template head)."""
    return """
    <script>L_NO_TOUCH = false; L_DISABLE_3D = false;</script>
    <script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js"></script>
    <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.2.2/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Leaflet.awesome-markers/2.0.2/leaflet.awesome-markers.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.css"/>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.2.2/dist/css/bootstrap.min.css"/>
    <link rel="stylesheet" href="https://netdna.bootstrapcdn.com/bootstrap/3.0.0/css/bootstrap.min.css"/>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;0,9..40,800;1,9..40,400&display=swap"/>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.2.0/css/all.min.css"/>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/Leaflet.awesome-markers/2.0.2/leaflet.awesome-markers.css"/>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/python-visualization/folium/folium/templates/leaflet.awesome.rotate.min.css"/>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
"""


# Natural Earth public domain  -  country (admin-0) and admin-1 boundaries.
NATURAL_EARTH_ADMIN0_110M_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_110m_admin_0_countries.geojson"
)
# Admin-1 at 50m (~2.3MB) - under /dm-proxy cap, with far better small territories than 110m. (10m ~40MB hits 413.)
NATURAL_EARTH_ADMIN1_50M_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_50m_admin_1_states_provinces.geojson"
)

# Rotating colormaps for per-metric layers (must also exist in the JS COLORMAPS object)
LAYER_COLORMAPS = ["YlOrRd", "Blues", "Greens", "PuOr", "viridis", "plasma", "RdYlGn", "coolwarm"]

# Subnational detection uses Natural Earth admin-1 worldwide (ISO 3166-2 keys).
SUBNATIONAL_GEOJSON_PROFILES = {
    "_GLOBAL_ADMIN1": {
        "url": NATURAL_EARTH_ADMIN1_50M_URL,
        "key_on": "feature.properties.iso_3166_2",
        "name_property": "name",
    },
}


def _client_geo_profiles() -> dict[str, dict]:
    """Geography presets: Natural Earth admin-0 (countries) and admin-1 (all ISO 3166-2 regions)."""
    g1 = SUBNATIONAL_GEOJSON_PROFILES["_GLOBAL_ADMIN1"]
    return {
        "countries": {
            "label": "World  -  countries (Natural Earth, ISO 3166-1 alpha-3)",
            "url": NATURAL_EARTH_ADMIN0_110M_URL,
            "key_on": "feature.properties.ISO_A3",
            "name_property": "ADMIN",
            "center": [20.0, 0.0],
            "zoom": 2,
        },
        "admin1_global": {
            "label": "World  -  states / provinces (Natural Earth, ISO 3166-2)",
            "url": g1["url"],
            "key_on": g1["key_on"],
            "name_property": g1["name_property"],
            "center": [20.0, 0.0],
            "zoom": 2,
        },
    }


# ---- UI theme: vibrant and bold ----
def _get_ui_css() -> str:
    """Return global CSS for the map UI."""
    return """
<style id="dm-ui-styles">

  :root {
    --bg: #faf7f2;
    --sidebar: #fffdf9;
    --surface: #fff;
    --surface-warm: #fef6ee;
    --surface-sage: #f0f5f0;
    --border: #ece5da;
    --border-strong: #d4c9bc;
    --coral: #e86c47;
    --coral-bg: #fef0eb;
    --coral-dim: #f9c4b2;
    --sage: #6b8f71;
    --sage-bg: #eef4ee;
    --amber: #c47a2a;
    --amber-bg: #fdf3e4;
    --red: #c44b4b;
    --red-bg: #fdf0f0;
    --plum: #7c5c8a;
    --plum-bg: #f5f0f8;
    --text: #2c2418;
    --muted: #7a6e5d;
    --faint: #b0a594;
    --sidebar-w: 380px;
    --radius: 14px;
    --radius-sm: 10px;
    --radius-lg: 20px;
    --radius-pill: 999px;
    --shadow-sm: 0 1px 4px rgba(44,36,24,0.06);
    --shadow: 0 4px 16px rgba(44,36,24,0.08);
    --shadow-lg: 0 8px 32px rgba(44,36,24,0.1);
    --font: 'DM Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  html, body { margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; box-sizing: border-box; }
  *, *::before, *::after { box-sizing: inherit; }
  body {
    font-family: var(--font);
    font-size: 13px; line-height: 1.55; color: var(--text);
    background: var(--bg); -webkit-font-smoothing: antialiased;
    display: flex; flex-direction: row; overflow: hidden;
  }
  #dm-app { display: flex; flex-direction: row; width: 100%; height: 100%; min-width: 0; overflow: hidden; }

  /* ── Sidebar ── */
  #dm-sidebar {
    flex-shrink: 0; width: var(--sidebar-w); min-width: 300px; max-width: min(520px, 90vw);
    height: 100%; background: var(--sidebar);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column; overflow: hidden;
    position: relative; z-index: 100;
    transition: width 0.22s ease, min-width 0.22s ease;
  }
  #dm-sidebar.dm-sidebar-resizing { transition: none !important; }
  #dm-sidebar-resize-handle { position: absolute; top: 0; right: 0; width: 4px; height: 100%; cursor: ew-resize; z-index: 30; background: transparent; touch-action: none; }
  #dm-sidebar-resize-handle:hover { background: var(--coral); opacity: 0.3; }
  #dm-sidebar.dm-sidebar-closed #dm-sidebar-resize-handle { display: none; }
  #dm-sidebar.dm-sidebar-closed { width: 48px !important; min-width: 48px !important; max-width: 48px !important; }
  #dm-sidebar.dm-sidebar-closed .dm-sidebar-toggle .dm-toggle-label { display: none !important; }
  #dm-sidebar.dm-sidebar-closed .dm-sidebar-content { opacity: 0; pointer-events: none; }

  #dm-sidebar .dm-sidebar-toggle {
    position: absolute; top: 10px; left: 8px; right: 8px; height: 32px;
    padding: 0 12px; border: 1px solid var(--border); border-radius: var(--radius-pill);
    background: var(--surface); color: var(--muted);
    font-size: 12px; font-weight: 600; cursor: pointer; z-index: 10;
    display: flex; align-items: center; justify-content: center; gap: 6px;
    font-family: var(--font); transition: all 0.15s; box-shadow: var(--shadow-sm);
  }
  #dm-sidebar .dm-sidebar-toggle:hover { background: var(--surface-warm); color: var(--text); border-color: var(--coral-dim); }
  #dm-sidebar .dm-sidebar-toggle .dm-toggle-icon { font-size: 14px; }

  #dm-sidebar .dm-sidebar-content {
    flex: 1; overflow-y: auto; overflow-x: hidden;
    padding: 50px 0 16px 0; display: flex; flex-direction: column;
    scrollbar-width: thin; scrollbar-color: var(--border) transparent;
    transition: opacity 0.15s;
  }
  #dm-sidebar .dm-sidebar-content::-webkit-scrollbar { width: 5px; }
  #dm-sidebar .dm-sidebar-content::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  #dm-sidebar .dm-sidebar-header {
    display: flex; align-items: center; gap: 8px;
    padding: 0 16px 12px 16px; border-bottom: 1px solid var(--border);
    margin-bottom: 4px;
  }
  #dm-sidebar .dm-sidebar-appname { font-size: 15px; font-weight: 800; color: var(--coral); letter-spacing: -0.02em; }
  #dm-sidebar .dm-sidebar-mapname { font-size: 12px; color: var(--muted); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* Tabs */
  #dm-sidebar .dm-sidebar-tabs { display: flex; gap: 4px; padding: 8px 12px; border-bottom: 1px solid var(--border); }
  #dm-sidebar .dm-tab {
    flex: 1; padding: 7px 4px; border: none; border-radius: var(--radius-pill);
    background: transparent; color: var(--muted);
    font-size: 11px; font-weight: 600; cursor: pointer; font-family: var(--font);
    transition: all 0.15s; text-align: center;
  }
  #dm-sidebar .dm-tab:hover { background: var(--surface-warm); color: var(--text); }
  #dm-sidebar .dm-tab.active { background: var(--coral); color: #fff; }
  #dm-sidebar .dm-tab-panel { display: none; flex-direction: column; padding: 12px 16px; }
  #dm-sidebar .dm-tab-panel.active { display: flex; }

  #dm-sidebar .dm-sub-tabs { display: flex; gap: 3px; margin-bottom: 12px; padding: 3px; background: var(--surface-warm); border-radius: var(--radius-pill); }
  #dm-sidebar .dm-sub-tab {
    flex: 1; padding: 6px 8px; border: none; border-radius: var(--radius-pill);
    background: transparent; color: var(--muted);
    font-size: 11px; font-weight: 600; cursor: pointer; font-family: var(--font);
    transition: all 0.15s; text-align: center;
  }
  #dm-sidebar .dm-sub-tab:hover { color: var(--text); }
  #dm-sidebar .dm-sub-tab.active { background: var(--surface); color: var(--text); box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-sub-panel { display: none; flex-direction: column; flex: 1; }
  #dm-sidebar .dm-sub-panel.active { display: flex; }

  #dm-sidebar .dm-section-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--faint); margin: 0 0 6px 0; }
  #dm-sidebar .dm-section { margin-bottom: 16px; }
  #dm-sidebar .dm-hint { font-size: 12px; color: var(--muted); line-height: 1.55; margin: 0 0 10px 0; }
  #dm-sidebar .dm-hint em { color: var(--coral); font-style: normal; font-weight: 600; }
  #dm-sidebar .dm-hint code { font-size: 10px; background: var(--surface-warm); padding: 2px 6px; border-radius: 6px; color: var(--text); font-family: ui-monospace, monospace; }

  /* ── Buttons ── */
  button { font-family: var(--font); }
  .dm-btn-primary, #dm-sidebar .dm-btn-primary-style, .dm-modal .dm-btn-primary {
    background: var(--coral); color: #fff; border: none;
    padding: 9px 18px; border-radius: var(--radius-pill);
    font-size: 12px; font-weight: 700; font-family: var(--font);
    cursor: pointer; transition: all 0.15s; box-shadow: 0 2px 8px rgba(232,108,71,0.25);
  }
  .dm-btn-primary:hover, #dm-sidebar .dm-btn-primary-style:hover, .dm-modal .dm-btn-primary:hover { opacity: 0.88; transform: translateY(-0.5px); }
  .dm-btn-secondary, #dm-sidebar .dm-btn-secondary-style, .dm-modal .dm-btn-secondary {
    background: var(--surface); color: var(--text);
    border: 1.5px solid var(--border); padding: 8px 16px; border-radius: var(--radius-pill);
    font-size: 12px; font-weight: 600; font-family: var(--font); cursor: pointer; transition: all 0.15s;
  }
  .dm-btn-secondary:hover, #dm-sidebar .dm-btn-secondary-style:hover, .dm-modal .dm-btn-secondary:hover { background: var(--surface-warm); border-color: var(--coral-dim); }
  #dm-sidebar .dm-btn-ghost { background: transparent; color: var(--muted); border: none; padding: 6px 10px; font-size: 11px; cursor: pointer; font-family: var(--font); border-radius: var(--radius-sm); }
  #dm-sidebar .dm-btn-ghost:hover { background: var(--surface-warm); color: var(--text); }
  #dm-sidebar .dm-btn-green { background: var(--sage); color: #fff; border: none; padding: 9px 18px; border-radius: var(--radius-pill); font-weight: 700; cursor: pointer; }

  /* Inputs */
  #dm-sidebar input[type="text"], #dm-sidebar input[type="url"], #dm-sidebar input[type="password"],
  #dm-sidebar input[type="search"], #dm-sidebar select, #dm-sidebar textarea,
  #dm-sidebar .dm-ai-key-input, .dm-modal input[type="text"], .dm-modal select {
    width: 100%; padding: 10px 14px; font-family: var(--font); font-size: 13px;
    background: var(--surface); color: var(--text);
    border: 1.5px solid var(--border); border-radius: var(--radius-sm);
    outline: none; transition: border-color 0.15s;
  }
  #dm-sidebar input:focus, #dm-sidebar select:focus, #dm-sidebar textarea:focus,
  .dm-modal input:focus, .dm-modal select:focus { border-color: var(--coral); box-shadow: 0 0 0 3px rgba(232,108,71,0.1); }
  #dm-sidebar textarea { resize: vertical; min-height: 70px; }
  #dm-sidebar input[type="file"] { background: transparent; border: none; color: var(--muted); font-size: 11px; padding: 0; }
  #dm-sidebar input[type="range"] { accent-color: var(--coral); width: 100%; }
  #dm-sidebar input[type="color"] { width: 36px; height: 30px; border: 1.5px solid var(--border); border-radius: 8px; cursor: pointer; padding: 2px; background: var(--surface); }

  /* ── Search ── */
  #dm-search-wrap { display: flex; gap: 6px; align-items: stretch; margin-bottom: 10px; }
  #dm-finddata-query {
    flex: 1; padding: 11px 16px; font-size: 14px;
    background: var(--surface); border: 1.5px solid var(--border);
    border-radius: var(--radius-lg); color: var(--text); box-shadow: var(--shadow-sm);
  }
  #dm-finddata-query:focus { border-color: var(--coral); box-shadow: 0 0 0 3px rgba(232,108,71,0.1); }
  #dm-finddata-query::placeholder { color: var(--faint); }
  #dm-btn-finddata-search {
    padding: 11px 20px; background: var(--coral); color: #fff;
    border: none; border-radius: var(--radius-lg); font-weight: 700; font-size: 13px;
    white-space: nowrap; flex-shrink: 0; box-shadow: 0 2px 8px rgba(232,108,71,0.3);
    cursor: pointer; font-family: var(--font); transition: all 0.15s;
  }
  #dm-btn-finddata-search:hover { opacity: 0.88; transform: translateY(-0.5px); }
  #dm-btn-ai-datasets {
    padding: 11px 14px; background: var(--plum-bg); color: var(--plum);
    border: 1.5px solid #ddd0e6; border-radius: var(--radius-lg);
    font-size: 12px; font-weight: 700; white-space: nowrap; flex-shrink: 0;
    cursor: pointer; font-family: var(--font); transition: all 0.15s;
  }
  #dm-btn-ai-datasets:hover { background: #ede4f3; border-color: #c9b8d8; }
  #dm-btn-ai-datasets:disabled { opacity: 0.5; cursor: default; }

  /* Quick search buttons */
  .dm-quick-search-btn {
    padding: 6px 14px; border: 1.5px solid var(--border); border-radius: var(--radius-pill);
    background: var(--surface); color: var(--coral); font-size: 12px; font-weight: 600;
    cursor: pointer; font-family: var(--font); transition: all 0.15s; box-shadow: var(--shadow-sm);
  }
  .dm-quick-search-btn:hover { background: var(--coral-bg); border-color: var(--coral-dim); transform: translateY(-1px); box-shadow: var(--shadow); }

  /* Dataset cards */
  .dm-ds-card {
    background: var(--surface); border: 1.5px solid var(--border);
    border-radius: var(--radius); padding: 12px 14px; margin-bottom: 8px;
    box-shadow: var(--shadow-sm); transition: all 0.15s;
  }
  .dm-ds-card:hover { box-shadow: var(--shadow); border-color: var(--border-strong); transform: translateY(-1px); }
  .dm-ds-card-header { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 4px; }
  .dm-ds-card-title { font-size: 13px; font-weight: 700; color: var(--text); flex: 1; line-height: 1.3; }
  .dm-ds-card-badge { font-size: 10px; padding: 3px 10px; border-radius: var(--radius-pill); font-weight: 700; white-space: nowrap; flex-shrink: 0; margin-top: 1px; }
  .dm-ds-badge-wb { background: var(--coral-bg); color: var(--coral); }
  .dm-ds-badge-owid { background: var(--sage-bg); color: var(--sage); }
  .dm-ds-badge-csv { background: var(--surface-warm); color: var(--amber); }
  .dm-ds-badge-ai { background: var(--plum-bg); color: var(--plum); }
  .dm-ds-card-desc { font-size: 11px; color: var(--muted); line-height: 1.45; margin-bottom: 8px; }
  .dm-ds-card-status { font-size: 10px; color: var(--muted); margin: 4px 0; min-height: 14px; }
  .dm-ds-card-actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .dm-ds-year-sel { font-size: 11px; padding: 5px 8px; background: var(--surface-warm); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text); flex-shrink: 0; max-width: 90px; font-family: var(--font); }
  .dm-ds-preview-btn { padding: 6px 12px; background: var(--surface); border: 1.5px solid var(--border); border-radius: var(--radius-pill); color: var(--text); font-size: 11px; font-weight: 600; font-family: var(--font); cursor: pointer; transition: all 0.12s; }
  .dm-ds-preview-btn:hover { border-color: var(--coral); color: var(--coral); }
  .dm-ds-add-btn { padding: 6px 12px; background: var(--coral); color: #fff; border: none; border-radius: var(--radius-pill); font-size: 11px; font-weight: 700; font-family: var(--font); cursor: pointer; box-shadow: 0 2px 6px rgba(232,108,71,0.2); transition: all 0.12s; }
  .dm-ds-add-btn:hover { opacity: 0.88; }
  .dm-ds-layer-btn { padding: 6px 12px; background: var(--surface); border: 1.5px solid var(--border); border-radius: var(--radius-pill); color: var(--muted); font-size: 11px; font-family: var(--font); cursor: pointer; }
  .dm-ds-layer-btn:hover { border-color: var(--coral); color: var(--coral); }

  /* ── Map area ── */
  #dm-map-area { flex: 1; min-width: 0; height: 100%; position: relative; display: flex; flex-direction: column; overflow: hidden; }
  #dm-map-area .folium-map { flex: 1; min-height: 0; min-width: 0; width: 100%; position: relative; background: #e8e2d8; }
  #dm-map-area .folium-map > div { position: absolute !important; inset: 0 !important; width: 100% !important; height: 100% !important; }
  #dm-map-area .folium-map .leaflet-container { width: 100% !important; height: 100% !important; font-size: 13px; font-family: var(--font); }
  .leaflet-control-zoom { border: 1.5px solid var(--border) !important; box-shadow: var(--shadow) !important; border-radius: var(--radius) !important; overflow: hidden; }
  .leaflet-control-zoom a { width: 34px !important; height: 34px !important; line-height: 34px !important; font-size: 17px !important; font-weight: 700 !important; color: var(--text) !important; background: var(--surface) !important; border: none !important; }
  .leaflet-control-zoom a:hover { background: var(--surface-warm) !important; color: var(--coral) !important; }
  .leaflet-control-attribution { font-size: 10px !important; color: var(--muted) !important; background: rgba(255,253,249,0.92) !important; padding: 3px 8px !important; border-radius: 8px !important; }
  .foliumtooltip { font-family: var(--font) !important; font-size: 13px !important; font-weight: 600 !important; padding: 10px 14px !important; border-radius: var(--radius) !important; box-shadow: var(--shadow-lg) !important; border: 1.5px solid var(--border) !important; background: var(--surface) !important; color: var(--text) !important; }
  .leaflet-control-layers { border: 1.5px solid var(--border) !important; border-radius: var(--radius) !important; box-shadow: var(--shadow) !important; overflow: hidden !important; background: var(--surface) !important; }
  .leaflet-control-layers-expanded { padding: 10px 14px !important; background: var(--surface) !important; min-width: 140px !important; font-family: var(--font) !important; font-size: 12px !important; color: var(--text) !important; }
  .leaflet-control-layers label { font-weight: 500 !important; color: var(--text) !important; }
  .leaflet-control-layers-selector { accent-color: var(--coral) !important; }
  .leaflet-bottom.leaflet-left .leaflet-control { margin-left: 16px !important; margin-bottom: 80px !important; }
  .leaflet-control img[src^="data:image"] { border-radius: 8px !important; box-shadow: var(--shadow-sm) !important; border: 1.5px solid var(--border) !important; }

  /* ── Header ── */
  .dm-app-header {
    flex-shrink: 0; height: 48px; padding: 0 20px;
    background: var(--sidebar); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    z-index: 50;
  }
  .dm-app-header .dm-app-name { font-size: 15px; font-weight: 800; color: var(--coral); }
  .dm-app-header .dm-map-title { flex: 1; font-size: 13px; color: var(--muted); font-weight: 500; text-align: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dm-app-header .dm-header-actions { display: flex; gap: 6px; }
  .dm-app-header .dm-header-btn { padding: 6px 14px; border-radius: var(--radius-pill); border: 1.5px solid var(--border); background: var(--surface); color: var(--muted); font-size: 11px; font-weight: 600; cursor: pointer; font-family: var(--font); transition: all 0.15s; }
  .dm-app-header .dm-header-btn:hover { background: var(--surface-warm); color: var(--text); }

  /* ── Toolbar ── */
  #dm-map-area .dm-map-toolbar { position: absolute; bottom: 18px; left: 18px; z-index: 1000; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .dm-map-toolbar .dm-toolbar-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 8px 14px; border: 1.5px solid var(--border); border-radius: var(--radius-pill);
    background: rgba(255,253,249,0.96); color: var(--text);
    font-size: 11px; font-weight: 600; cursor: pointer; font-family: var(--font);
    box-shadow: var(--shadow); transition: all 0.15s; backdrop-filter: blur(8px);
  }
  .dm-map-toolbar .dm-toolbar-btn:hover { background: var(--surface); box-shadow: var(--shadow-lg); transform: translateY(-1px); }
  .dm-map-toolbar .dm-toolbar-btn-data { background: var(--coral); color: #fff; border-color: transparent; font-weight: 800; font-size: 12px; box-shadow: 0 4px 14px rgba(232,108,71,0.35); }
  .dm-map-toolbar .dm-toolbar-btn-data:hover { opacity: 0.88; }
  .dm-map-toolbar .dm-btn-export { color: var(--sage); }
  .dm-map-toolbar .dm-btn-fullscreen { color: var(--plum); }
  .dm-map-toolbar .dm-btn-print { color: var(--amber); }

  /* ── Modals ── */
  .dm-modal-backdrop { position: fixed; inset: 0; background: rgba(44,36,24,0.35); z-index: 2000; display: flex; align-items: center; justify-content: center; padding: 20px; backdrop-filter: blur(6px); }
  .dm-modal { background: var(--surface); border: 1.5px solid var(--border); border-radius: var(--radius-lg); max-width: 440px; width: 100%; max-height: 90vh; overflow-y: auto; padding: 32px; box-shadow: var(--shadow-lg); }
  .dm-modal h3 { margin: 0 0 8px 0; font-size: 20px; font-weight: 800; color: var(--text); letter-spacing: -0.02em; }
  .dm-modal p { margin: 0 0 16px 0; font-size: 13px; color: var(--muted); line-height: 1.55; }
  .dm-modal label { display: block; font-size: 11px; font-weight: 700; color: var(--faint); margin-bottom: 5px; text-transform: uppercase; letter-spacing: 0.06em; }
  .dm-modal input[type="text"], .dm-modal select { margin-bottom: 16px; background: var(--surface); border: 1.5px solid var(--border); }
  .dm-modal .dm-modal-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 20px; }
  .dm-modal .dm-btn-primary { padding: 12px 24px; font-size: 14px; font-weight: 700; }
  .dm-modal .dm-checkbox-wrap { display: flex; align-items: center; gap: 8px; margin-top: 10px; font-size: 12px; color: var(--muted); }
  .dm-modal .dm-checkbox-wrap input { width: auto; margin: 0; accent-color: var(--coral); }

  /* ── Insights ── */
  #dm-sidebar .dm-metric { background: var(--surface); border: 1.5px solid var(--border); border-radius: var(--radius); padding: 12px 14px; margin-bottom: 10px; box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-metric-title { font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.07em; color: var(--coral); margin: 0 0 8px 0; }
  #dm-sidebar .dm-metric ul { margin: 0; padding-left: 1.2em; font-size: 12px; }
  #dm-sidebar .dm-metric li { margin-bottom: 4px; color: var(--text); }
  #dm-sidebar .dm-metric li strong { color: var(--coral); }

  #dm-ai-overview-wrap { background: linear-gradient(135deg, var(--coral-bg), var(--sage-bg)); border: 1.5px solid var(--coral-dim); border-radius: var(--radius); padding: 14px; margin-bottom: 14px; }
  #dm-ai-overview-wrap .dm-overview-label { font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.08em; color: var(--coral); margin-bottom: 8px; display: flex; align-items: center; gap: 5px; }
  #dm-ai-overview-text { font-size: 12px; color: var(--text); line-height: 1.65; white-space: pre-wrap; }
  #dm-ai-overview-text.dm-loading { color: var(--muted); font-style: italic; }

  #dm-sidebar .dm-callout { padding: 10px 12px; border-radius: var(--radius-sm); margin-top: 10px; border-left: 3px solid var(--amber); background: var(--amber-bg); }
  #dm-sidebar .dm-callout-title { font-size: 10px; font-weight: 800; color: var(--amber); margin: 0 0 3px 0; text-transform: uppercase; letter-spacing: 0.06em; }
  #dm-sidebar .dm-callout-text { font-size: 12px; margin: 0; color: var(--text); font-weight: 500; line-height: 1.45; }
  #dm-sidebar .dm-callout.dm-callout-blue { border-left-color: var(--sage); background: var(--sage-bg); }
  #dm-sidebar .dm-callout.dm-callout-blue .dm-callout-title { color: var(--sage); }
  #dm-sidebar .dm-callout.dm-callout-red { border-left-color: var(--red); background: var(--red-bg); }
  #dm-sidebar .dm-callout.dm-callout-red .dm-callout-title { color: var(--red); }
  #dm-sidebar .dm-key-message { background: var(--surface); border: 1.5px solid var(--border); border-left: 3px solid var(--coral); border-radius: var(--radius-sm); padding: 10px 12px; margin-bottom: 12px; box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-key-message-text { margin: 0; font-size: 13px; font-weight: 500; color: var(--text); line-height: 1.45; }
  #dm-sidebar .dm-correlation-list { margin: 0 0 0 1em; padding: 0; font-size: 12px; line-height: 1.6; }
  #dm-sidebar .dm-correlation-list li { margin-bottom: 5px; color: var(--text); }

  /* Ask */
  #dm-sidebar .dm-ai-question-input { width: 100%; min-height: 60px; padding: 10px 14px; border-radius: var(--radius-sm); border: 1.5px solid var(--border); background: var(--surface); font-family: var(--font); font-size: 13px; color: var(--text); resize: vertical; margin-bottom: 8px; }
  #dm-sidebar .dm-ai-answer { margin-top: 10px; padding: 12px 14px; border-radius: var(--radius-sm); background: var(--surface-warm); color: var(--text); font-size: 12px; line-height: 1.6; white-space: pre-wrap; display: none; border: 1.5px solid var(--border); }
  #dm-sidebar .dm-ai-answer.dm-visible { display: block; }
  #dm-sidebar .dm-suggested-questions { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }
  #dm-sidebar .dm-q-btn { padding: 6px 12px; border: 1.5px solid var(--border); border-radius: var(--radius-pill); background: var(--surface); color: var(--muted); font-size: 10px; font-weight: 600; cursor: pointer; font-family: var(--font); transition: all 0.12s; line-height: 1.3; }
  #dm-sidebar .dm-q-btn:hover { border-color: var(--coral-dim); color: var(--coral); background: var(--coral-bg); }

  /* Charts */
  #dm-sidebar .dm-stat-chart-wrap { background: var(--surface); border: 1.5px solid var(--border); border-radius: var(--radius); padding: 12px; margin-bottom: 10px; box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-stat-chart-title { font-size: 11px; font-weight: 700; color: var(--text); margin-bottom: 8px; }
  #dm-sidebar #dm-statistics-charts-root canvas { display: block; width: 100% !important; max-width: 100%; height: auto !important; }

  /* Settings */
  #dm-sidebar .dm-settings-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  #dm-sidebar .dm-settings-label { font-size: 11px; color: var(--muted); font-weight: 600; min-width: 70px; flex-shrink: 0; }
  #dm-sidebar .dm-range-val { font-size: 11px; color: var(--muted); min-width: 32px; text-align: right; font-weight: 500; }
  #dm-sidebar .dm-settings-divider { border: none; border-top: 1px solid var(--border); margin: 10px 0; }
  #dm-sidebar .dm-settings-toggle { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--muted); cursor: pointer; margin-bottom: 8px; padding: 9px 12px; border-radius: var(--radius-sm); background: var(--surface-warm); border: 1px solid var(--border); transition: all 0.15s; }
  #dm-sidebar .dm-settings-toggle:hover { background: var(--coral-bg); }
  #dm-sidebar .dm-settings-toggle input { accent-color: var(--coral); flex-shrink: 0; }
  #dm-sidebar .dm-color-select { width: 100%; padding: 9px 12px; border-radius: var(--radius-sm); border: 1.5px solid var(--border); background: var(--surface); font-family: var(--font); font-size: 12px; color: var(--text); }
  #dm-sidebar .dm-color-preview { display: flex; height: 22px; border-radius: 8px; overflow: hidden; margin-bottom: 6px; border: 1.5px solid var(--border); }
  #dm-sidebar .dm-color-direction { display: flex; justify-content: space-between; font-size: 10px; color: var(--faint); margin-bottom: 6px; font-weight: 600; }

  /* Layers */
  #dm-sidebar .dm-layer-control { background: var(--surface); border: 1.5px solid var(--border); border-radius: var(--radius); padding: 12px 14px; margin-bottom: 8px; box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-layer-control-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }
  #dm-sidebar .dm-layer-control-header input[type="checkbox"] { accent-color: var(--coral); width: 16px; height: 16px; }
  #dm-sidebar .dm-layer-control-name { font-weight: 700; font-size: 13px; color: var(--text); flex: 1; }
  #dm-sidebar .dm-layer-control-options { display: flex; flex-direction: column; gap: 6px; }
  #dm-sidebar .dm-layer-control-row { display: flex; align-items: center; gap: 8px; }
  #dm-sidebar .dm-layer-control-row label { font-size: 10px; color: var(--faint); font-weight: 700; min-width: 44px; text-transform: uppercase; letter-spacing: 0.05em; }
  #dm-sidebar .dm-layer-control-row select { flex: 1; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface-warm); font-size: 11px; font-family: var(--font); color: var(--text); }

  /* Basemap */
  #dm-sidebar .dm-basemap-cards { display: flex; flex-direction: column; gap: 6px; }
  #dm-sidebar .dm-basemap-card { display: flex; flex-direction: column; padding: 12px 14px; background: var(--surface); border: 1.5px solid var(--border); border-radius: var(--radius); font-size: 13px; font-weight: 700; color: var(--text); cursor: pointer; font-family: var(--font); text-align: left; transition: all 0.15s; box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-basemap-card:hover { border-color: var(--coral-dim); box-shadow: var(--shadow); }
  #dm-sidebar .dm-basemap-card.active { border-color: var(--coral); background: var(--coral-bg); color: var(--coral); }
  #dm-sidebar .dm-basemap-desc { font-size: 11px; font-weight: 400; color: var(--muted); margin-top: 2px; }

  /* Action / Vision */
  #dm-sidebar .dm-action-step { display: flex; align-items: center; gap: 8px; padding: 9px 12px; background: var(--surface); border-radius: var(--radius-sm); border: 1.5px solid var(--border); margin-bottom: 6px; font-size: 13px; border-left: 3px solid var(--coral); box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-action-step.done { opacity: 0.5; text-decoration: line-through; border-left-color: var(--sage); }
  #dm-sidebar .dm-action-step input[type="checkbox"] { accent-color: var(--coral); }
  #dm-sidebar .dm-action-input { width: 100%; padding: 9px 14px; border-radius: var(--radius-sm); border: 1.5px solid var(--border); background: var(--surface); font-family: var(--font); font-size: 13px; color: var(--text); margin-bottom: 8px; }
  #dm-sidebar .dm-brainstorm-input { width: 100%; min-height: 80px; padding: 10px 14px; border-radius: var(--radius-sm); border: 1.5px solid var(--border); background: var(--surface); font-family: var(--font); font-size: 13px; color: var(--text); resize: vertical; margin-bottom: 8px; }
  #dm-sidebar .dm-vision-card { padding: 12px 14px; background: var(--surface); border-radius: var(--radius-sm); margin-bottom: 8px; font-size: 13px; color: var(--text); border: 1.5px solid var(--border); border-left: 4px solid var(--coral); box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-vision-card[data-category="Ideas"] { border-left-color: var(--plum); }
  #dm-sidebar .dm-vision-card[data-category="In Progress"] { border-left-color: var(--amber); }
  #dm-sidebar .dm-vision-card[data-category="Done"] { border-left-color: var(--sage); }
  #dm-sidebar .dm-vision-card[data-category="Later"] { border-left-color: var(--faint); }
  #dm-sidebar .dm-vision-card-title { font-weight: 700; margin-bottom: 3px; }
  #dm-sidebar .dm-vision-card-priority { font-size: 10px; padding: 2px 8px; border-radius: var(--radius-pill); font-weight: 700; }
  #dm-sidebar .dm-vision-card-priority.high { background: var(--red-bg); color: var(--red); }
  #dm-sidebar .dm-vision-card-priority.medium { background: var(--amber-bg); color: var(--amber); }
  #dm-sidebar .dm-vision-card-priority.low { background: var(--sage-bg); color: var(--sage); }
  #dm-sidebar .dm-vision-card-desc { font-size: 11px; color: var(--muted); margin-top: 3px; }
  #dm-sidebar .dm-vision-card-actions button { padding: 4px 8px; border: 1.5px solid var(--border); border-radius: 6px; background: transparent; color: var(--muted); font-size: 10px; cursor: pointer; font-family: var(--font); }
  #dm-sidebar .dm-vision-filters { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
  #dm-sidebar .dm-vision-filter { padding: 5px 12px; border: 1.5px solid var(--border); border-radius: var(--radius-pill); background: var(--surface); color: var(--muted); font-size: 10px; font-weight: 600; cursor: pointer; font-family: var(--font); transition: all 0.12s; }
  #dm-sidebar .dm-vision-filter:hover { color: var(--text); border-color: var(--border-strong); }
  #dm-sidebar .dm-vision-filter.active { background: var(--coral); color: #fff; border-color: var(--coral); }
  #dm-sidebar .dm-vision-input, #dm-sidebar .dm-vision-desc, #dm-sidebar .dm-vision-category { width: 100%; padding: 9px 12px; border-radius: var(--radius-sm); border: 1.5px solid var(--border); background: var(--surface); font-family: var(--font); font-size: 12px; color: var(--text); margin-bottom: 6px; }

  /* Research */
  #dm-sidebar .dm-research-item { padding: 12px 14px; background: var(--surface); border-radius: var(--radius-sm); margin-bottom: 8px; font-size: 12px; border: 1.5px solid var(--border); border-left: 4px solid var(--sage); box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-research-item[data-type="Report"] { border-left-color: var(--amber); }
  #dm-sidebar .dm-research-item[data-type="Dataset"] { border-left-color: var(--sage); }
  #dm-sidebar .dm-research-item[data-type="Website"] { border-left-color: var(--plum); }
  #dm-sidebar .dm-research-item-title { font-weight: 700; color: var(--text); }
  #dm-sidebar .dm-research-item-type { font-size: 10px; padding: 2px 8px; border-radius: var(--radius-pill); background: var(--surface-warm); color: var(--muted); font-weight: 700; }
  #dm-sidebar .dm-research-item-meta { font-size: 10px; color: var(--muted); margin-top: 2px; }
  #dm-sidebar .dm-research-item-url a { color: var(--coral); text-decoration: none; font-size: 10px; }
  #dm-sidebar .dm-research-item-url a:hover { text-decoration: underline; }
  #dm-sidebar .dm-research-item-actions button { padding: 4px 8px; border: 1.5px solid var(--border); border-radius: 6px; background: transparent; color: var(--muted); font-size: 10px; cursor: pointer; font-family: var(--font); }
  #dm-sidebar .dm-research-filters { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
  #dm-sidebar .dm-research-filter { padding: 5px 12px; border: 1.5px solid var(--border); border-radius: var(--radius-pill); background: var(--surface); color: var(--muted); font-size: 10px; font-weight: 600; cursor: pointer; font-family: var(--font); }
  #dm-sidebar .dm-research-filter:hover { color: var(--text); }
  #dm-sidebar .dm-research-filter.active { background: var(--coral); color: #fff; border-color: var(--coral); }
  #dm-sidebar .dm-url-input { width: 100%; padding: 9px 12px; border-radius: var(--radius-sm); border: 1.5px solid var(--border); background: var(--surface); font-family: var(--font); font-size: 12px; color: var(--text); margin-bottom: 6px; }
  #dm-sidebar .dm-highlight-card { padding: 10px 12px; border-radius: var(--radius-sm); margin-bottom: 6px; font-size: 12px; border-left: 4px solid; }
  #dm-sidebar .dm-highlight-card[data-color="yellow"] { background: #fef9e8; border-left-color: #d4a826; }
  #dm-sidebar .dm-highlight-card[data-color="green"] { background: var(--sage-bg); border-left-color: var(--sage); }
  #dm-sidebar .dm-highlight-card[data-color="blue"] { background: #edf4fb; border-left-color: #5b8db8; }
  #dm-sidebar .dm-highlight-card[data-color="pink"] { background: #fdf2f6; border-left-color: #c66b8e; }
  #dm-sidebar .dm-highlight-card[data-color="orange"] { background: var(--coral-bg); border-left-color: var(--coral); }

  /* Misc */
  #dm-sidebar .dm-fetch-banner { font-size: 11px; line-height: 1.55; color: var(--amber); background: var(--amber-bg); border: 1.5px solid #f0d8a8; border-radius: var(--radius-sm); padding: 10px 12px; margin-bottom: 10px; }
  #dm-sidebar .dm-fetch-banner code { font-size: 10px; background: rgba(0,0,0,0.04); padding: 2px 6px; border-radius: 4px; }
  #dm-sidebar details.dm-finddata-collapsible { margin-bottom: 10px; border-radius: var(--radius); background: var(--surface); border: 1.5px solid var(--border); overflow: hidden; box-shadow: var(--shadow-sm); }
  #dm-sidebar details.dm-finddata-collapsible > summary.dm-finddata-summary { cursor: pointer; list-style: none; display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; font-size: 11px; font-weight: 800; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); user-select: none; background: var(--surface-warm); border-bottom: 1px solid var(--border); }
  #dm-sidebar details.dm-finddata-collapsible > summary::-webkit-details-marker { display: none; }
  #dm-sidebar .dm-finddata-collapsible-body { padding: 10px 14px 12px; }
  #dm-sidebar .dm-finddata-scroll { max-height: min(40vh, 300px); overflow-y: auto; overflow-x: hidden; }

  #dm-toast-container { position: fixed; bottom: 20px; right: 20px; z-index: 3000; display: flex; flex-direction: column; gap: 8px; pointer-events: none; }
  .dm-toast { padding: 12px 20px; border-radius: var(--radius-pill); background: var(--text); color: var(--bg); font-size: 13px; font-weight: 600; box-shadow: var(--shadow-lg); pointer-events: auto; font-family: var(--font); animation: dm-toast-in 0.2s ease; }
  @keyframes dm-toast-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

  #dm-sidebar .dm-vision-board-cards { max-height: 300px; overflow-y: auto; margin-bottom: 10px; }
  #dm-sidebar .dm-research-list { max-height: 300px; overflow-y: auto; margin-top: 8px; }
  #dm-sidebar .dm-highlights-list { max-height: 250px; overflow-y: auto; margin-bottom: 8px; }
  #dm-sidebar .dm-finddata-results { margin-top: 10px; }
  #dm-sidebar .dm-your-goal { font-size: 12px; color: var(--muted); margin-bottom: 12px; padding: 10px 14px; background: var(--surface-warm); border-radius: var(--radius-sm); border: 1.5px solid var(--border); }
  #dm-sidebar .dm-your-goal strong { color: var(--text); }
  #dm-sidebar .dm-reflection-wrap textarea { width: 100%; min-height: 60px; padding: 10px 14px; border-radius: var(--radius-sm); border: 1.5px solid var(--border); background: var(--surface); font-family: var(--font); font-size: 12px; color: var(--text); resize: vertical; margin-bottom: 8px; }
  #dm-sidebar .dm-share-note { font-size: 11px; color: var(--muted); margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border); }
  #dm-sidebar .dm-csv-upload-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 10px 0; }
  #dm-sidebar .dm-add-from-web { margin-bottom: 8px; }
  #dm-sidebar .dm-notes-toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
  #dm-sidebar .dm-notes-wordcount { font-size: 10px; color: var(--faint); }
  #dm-sidebar .dm-research-notes-textarea { min-height: 160px; }
  #dm-sidebar .dm-action-steps-shell, #dm-sidebar .dm-action-scratch-shell, #dm-sidebar .dm-action-share-shell { margin-bottom: 12px; padding: 12px; border-radius: var(--radius-sm); background: var(--surface-warm); border: 1.5px solid var(--border); }
  #dm-sidebar .dm-action-section-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  #dm-sidebar .dm-action-section-icon { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; border-radius: 6px; font-size: 10px; font-weight: 800; color: #fff; background: var(--coral); margin: 0 !important; padding: 0 !important; border: none !important; }
  #dm-sidebar .dm-action-hub-hero { background: linear-gradient(135deg, var(--coral-bg), var(--surface-warm)); border: 1.5px solid var(--border); border-radius: var(--radius); padding: 14px; margin-bottom: 12px; }
  #dm-sidebar .dm-action-hub-badge { display: inline-block; font-size: 10px; font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase; color: var(--coral); margin-bottom: 6px; }
  #dm-sidebar .dm-action-hub-tagline { margin: 0 0 10px 0; font-size: 13px; color: var(--text); line-height: 1.4; font-weight: 500; }
  #dm-sidebar .dm-action-hub-tagline em { color: var(--coral); font-style: normal; font-weight: 700; }
  #dm-sidebar .dm-action-goal-line { margin: 0 0 12px 0; font-size: 12px; color: var(--muted); padding: 8px 10px; border-radius: 8px; background: var(--surface); border-left: 3px solid var(--amber); border: 1.5px solid var(--border); border-left-width: 3px; }
  #dm-sidebar .dm-action-progress-meta { display: flex; justify-content: space-between; font-size: 10px; font-weight: 600; color: var(--muted); margin-bottom: 5px; }
  #dm-sidebar .dm-action-progress-bar { height: 8px; border-radius: var(--radius-pill); background: var(--surface-warm); overflow: hidden; border: 1px solid var(--border); }
  #dm-sidebar .dm-action-progress-fill { display: block; height: 100%; width: 0%; border-radius: var(--radius-pill); background: linear-gradient(90deg, var(--coral), var(--plum)); transition: width 0.3s; }
  #dm-sidebar .dm-action-starters { margin-bottom: 12px; padding: 10px 12px; border-radius: var(--radius-sm); background: var(--surface-warm); border: 1.5px dashed var(--border-strong); }
  #dm-sidebar .dm-action-starters-label { display: block; font-size: 10px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); margin-bottom: 8px; }
  #dm-sidebar .dm-action-starter-row { display: flex; flex-wrap: wrap; gap: 6px; }
  #dm-sidebar .dm-action-starter { padding: 6px 12px; border: 1.5px solid var(--border); border-radius: var(--radius-pill); font-size: 11px; font-weight: 600; cursor: pointer; font-family: var(--font); color: var(--text); background: var(--surface); transition: all 0.12s; box-shadow: var(--shadow-sm); }
  #dm-sidebar .dm-action-starter:hover { border-color: var(--coral); color: var(--coral); transform: translateY(-1px); }

  @media print {
    @page { size: landscape; margin: 10mm; }
    .dm-map-toolbar, .dm-app-header { display: none !important; }
    #dm-welcome-modal, #dm-about-modal { display: none !important; }
    #dm-sidebar-resize-handle { display: none !important; }
    html, body { background: #fff !important; height: auto !important; overflow: visible !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    #dm-app { display: flex !important; flex-direction: row !important; width: 100% !important; height: auto !important; overflow: visible !important; }
    #dm-sidebar { position: relative !important; flex: 0 0 34% !important; width: 34% !important; min-width: 220px !important; max-width: none !important; height: auto !important; overflow: visible !important; background: #faf7f2 !important; border-right: 1px solid #ece5da !important; box-shadow: none !important; }
    #dm-sidebar.dm-sidebar-closed { width: 34% !important; }
    #dm-sidebar .dm-sidebar-content { opacity: 1 !important; pointer-events: none !important; overflow: visible !important; padding: 10px 12px !important; }
    #dm-sidebar .dm-sidebar-toggle { display: none !important; }
    #dm-sidebar .dm-tab-panel { display: block !important; }
    #dm-sidebar .dm-tab-panel:not(.active) { display: none !important; }
    #dm-map-area { flex: 1 1 66% !important; min-height: 500px !important; height: 70vh !important; page-break-inside: avoid; }
    .folium-map { break-inside: avoid; }
  }
</style>
"""

def _escape_attr(s: str) -> str:
    """Escape for HTML attribute value."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _get_app_header_html(default_title: str = "Data map") -> str:
    """Return the app header bar (name + map title + About)."""
    t = _escape_attr(default_title)
    return f"""
<div class="dm-app-header" id="dm-app-header">
  <span class="dm-app-name">Data Mapper</span>
  <span class="dm-map-title" id="dm-map-title">{t}</span>
  <div class="dm-header-actions">
    <button type="button" class="dm-header-btn" id="dm-btn-about">About</button>
  </div>
</div>
"""


def _get_welcome_modal_html(default_title: str = "Data map") -> str:
    """Return the welcome/onboarding modal (prompts for map name and goal)."""
    t = _escape_attr(default_title)
    return f"""
<div class="dm-modal-backdrop" id="dm-welcome-modal" style="display:none;">
  <div class="dm-modal">
    <h3>Welcome to Data Mapper</h3>
    <p>Name your map and set a goal to get the most out of your data. Use the <strong>Data</strong> tab (or the <strong>Data</strong> button on the map) to search public datasets or paste your own coordinates.</p>
    <label for="dm-welcome-map-name">Name your map</label>
    <input type="text" id="dm-welcome-map-name" value="{t}" placeholder="e.g. Life expectancy by country">
    <label for="dm-welcome-goal">What's your goal?</label>
    <select id="dm-welcome-goal">
      <option value="Just exploring">Just exploring</option>
      <option value="Advocacy">Advocacy</option>
      <option value="Grant writing">Grant writing</option>
      <option value="Research">Research</option>
      <option value="Teaching">Teaching</option>
      <option value="Policy">Policy</option>
    </select>
    <div class="dm-modal-actions">
      <button type="button" class="dm-btn-primary" id="dm-welcome-start">Get started</button>
    </div>
    <div class="dm-checkbox-wrap">
      <input type="checkbox" id="dm-welcome-skip" />
      <label for="dm-welcome-skip">Don't show this again</label>
    </div>
  </div>
</div>
"""


def _get_about_modal_html() -> str:
    """Return the About modal (hidden by default)."""
    return """
<div class="dm-modal-backdrop" id="dm-about-modal" style="display:none;">
  <div class="dm-modal">
    <h3>About Data Mapper</h3>
    <p>Data Mapper helps you visualize location-based data and turn it into action. Use it for advocacy, grant proposals, research, or teaching.</p>
    <p><strong>Features:</strong> Start in the <strong>Data</strong> tab to discover datasets or paste CSVs. Use <strong>Settings  ->  Layers</strong> to toggle stacked datasets, colors, and patterns. Export <strong>PNG</strong>, <strong>CSV</strong>, or <strong>Save PDF</strong> (print dialog). <strong>Insights  ->  Ask</strong> gives instant stats; add an OpenAI key for AI text when the map is served over <code>http://127.0.0.1</code> (local <code>/dm-openai</code> relay).</p>
    <p>Share your map to advocate for change or cite it in reports and proposals.</p>
    <div class="dm-modal-actions">
      <button type="button" class="dm-btn-primary" id="dm-about-close">Close</button>
    </div>
  </div>
</div>
"""


def _get_map_toolbar_html() -> str:
    """Return HTML for the floating map toolbar (Export, Fullscreen, Copy link, Print, Locate, Download CSV)."""
    return """
<div class="dm-map-toolbar" id="dm-map-toolbar" aria-label="Map tools">
  <button type="button" class="dm-toolbar-btn dm-toolbar-btn-data" id="dm-btn-data-tab" title="Open Data: discover datasets or paste your own">Data</button>
  <button type="button" class="dm-toolbar-btn dm-btn-export" id="dm-btn-export" title="Save map as PNG image">Export PNG</button>
  <button type="button" class="dm-toolbar-btn" id="dm-btn-export-pdf" title="Opens print  -  choose Save as PDF for a readable document">Save PDF</button>
  <button type="button" class="dm-toolbar-btn" id="dm-btn-download-csv" title="Download map data as CSV">Download CSV</button>
  <button type="button" class="dm-toolbar-btn dm-btn-fullscreen" id="dm-btn-fullscreen" title="Fullscreen map">Fullscreen</button>
  <button type="button" class="dm-toolbar-btn" id="dm-btn-copy-link" title="Copy page link to share">Copy link</button>
  <button type="button" class="dm-toolbar-btn dm-btn-print" id="dm-btn-print" title="Print map and insights">Print</button>
  <button type="button" class="dm-toolbar-btn" id="dm-btn-locate" title="Center on my location">Locate me</button>
</div>
"""


def _get_map_ui_js() -> str:
    """Return script: sidebar toggle, map resize, toolbar, color scale, add-data. Uses event delegation so buttons work immediately."""
    _root = Path(__file__).resolve().parent
    try:
        _india_csv = (_root / "india_full_dataset.csv").read_text(encoding="utf-8")
    except OSError:
        _india_csv = ""
    try:
        _us_states_csv = (_root / "sample_us_states_le.csv").read_text(encoding="utf-8")
    except OSError:
        _us_states_csv = ""
    try:
        _canada_provinces_csv = (_root / "canada_provinces_population.csv").read_text(encoding="utf-8")
    except OSError:
        _canada_provinces_csv = ""
    try:
        _australia_states_csv = (_root / "australia_states_population.csv").read_text(encoding="utf-8")
    except OSError:
        _australia_states_csv = ""
    _embed_csv_vars = (
        "  var _DM_EMBED_INDIA_MULTI = "
        + json.dumps(_india_csv)
        + ";\n  var _DM_EMBED_US_STATES_MULTI = "
        + json.dumps(_us_states_csv)
        + ";\n  var _DM_EMBED_CANADA_PROVINCES = "
        + json.dumps(_canada_provinces_csv)
        + ";\n  var _DM_EMBED_AUSTRALIA_STATES = "
        + json.dumps(_australia_states_csv)
        + ";\n"
    )
    return (
        r"""
<script>
(function() {
  'use strict';
  var _DM_EMBED_LIFE_EXPECTANCY_WORLD = "iso3,life_expectancy\nABW,76.5\nAFE,65.3\nAFG,66.3\nAFW,59.0\nAGO,64.8\nALB,79.8\nAND,84.2\nARB,72.7\nARE,83.1\nARG,77.5\nARM,78.3\nASM,73.0\nATG,77.8\nAUS,83.1\nAUT,82.0\nAZE,74.6\nBDI,63.8\nBEL,82.3\nBEN,61.0\nBFA,61.3\nBGD,74.9\nBGR,75.8\nBHR,81.4\nBHS,74.7\nBIH,78.0\nBLR,74.4\nBLZ,73.7\nBMU,82.5\nBOL,68.7\nBRA,76.0\nBRB,76.3\nBRN,75.5\nBTN,73.3\nBWA,69.3\nCAF,57.7\nCAN,82.1\nCEB,77.9\nCHE,84.4\nCHI,81.2\nCHL,81.4\nCHN,78.0\nCIV,62.1\nCMR,64.0\nCOD,62.1\nCOG,66.0\nCOL,77.9\nCOM,67.0\nCPV,76.2\nCRI,81.0\nCSS,73.3\nCUB,78.3\nCUW,77.0\nCYM,80.5\nCYP,81.8\nCZE,80.0\nDEU,80.8\nDJI,66.2\nDMA,71.3\nDNK,82.3\nDOM,73.9\nDZA,76.5\nEAP,76.0\nEAR,72.0\nEAS,76.7\nECA,75.1\nECS,78.6\nECU,77.6\nEGY,71.8\nEMU,82.4\nERI,68.9\nESP,83.9\nEST,79.3\nETH,67.6\nEUU,81.6\nFCS,63.6\nFIN,82.3\nFJI,67.5\nFRA,83.0\nFRO,83.1\nFSM,67.4\nGAB,68.5\nGBR,81.4\nGEO,74.7\nGHA,65.7\nGIB,83.6\nGIN,60.9\nGMB,66.1\nGNB,64.3\nGNQ,63.9\nGRC,81.8\nGRD,75.4\nGRL,70.3\nGTM,72.7\nGUM,77.4\nGUY,70.3\nHKG,85.4\nHND,73.0\nHPC,64.9\nHRV,78.9\nHTI,65.1\nHUN,76.7\nIBD,74.7\nIBT,72.1\nIDA,65.6\nIDB,62.6\nIDN,71.3\nIDX,67.2\nIMN,81.1\nIND,72.2\nIRL,83.0\nIRN,77.9\nIRQ,72.4\nISL,82.8\nISR,83.2\nITA,84.0\nJAM,71.6\nJOR,78.0\nJPN,84.0\nKAZ,74.5\nKEN,63.8\nKGZ,72.4\nKHM,70.8\nKIR,66.6\nKNA,72.3\nKOR,83.6\nKWT,84.6\nLAC,75.7\nLAO,69.2\nLBN,77.9\nLBR,62.3\nLBY,71.1\nLCA,72.8\nLCN,75.8\nLDC,66.7\nLIE,84.2\nLKA,77.7\nLMY,72.1\nLSO,57.8\nLTE,77.2\nLTU,77.2\nLUX,83.2\nLVA,76.4\nMAC,83.3\nMAF,80.4\nMAR,75.5\nMCO,86.5\nMDA,71.3\nMDG,63.8\nMDV,81.3\nMEA,72.3\nMEX,75.3\nMHL,67.1\nMIC,72.9\nMKD,76.6\nMLI,60.7\nMLT,83.0\nMMR,67.1\nMNA,71.5\nMNE,77.9\nMNG,72.4\nMNP,78.9\nMOZ,63.8\nMRT,68.7\nMUS,73.8\nMWI,67.6\nMYS,76.8\nNAC,79.2\nNAM,67.5\nNCL,78.9\nNER,61.4\nNGA,54.6\nNIC,75.1\nNLD,82.0\nNOR,83.2\nNPL,70.6\nNRU,62.3\nNZL,82.0\nOED,80.4\nOMN,80.2\nOSS,73.8\nPAK,67.8\nPAN,79.8\nPER,77.9\nPHL,69.9\nPLW,69.4\nPNG,66.3\nPOL,78.4\nPRE,62.4\nPRI,81.9\nPRK,73.7\nPRT,82.4\nPRY,74.0\nPSE,69.2\nPSS,69.4\nPST,81.1\nPYF,84.2\nQAT,82.5\nROU,76.5\nRUS,73.4\nRWA,68.0\nSAS,72.6\nSAU,79.0\nSDN,66.5\nSEN,68.9\nSGP,83.3\nSLB,70.7\nSLE,62.0\nSLV,72.3\nSMR,85.8\nSOM,59.0\nSRB,76.0\nSSA,62.3\nSSD,57.7\nSSF,62.8\nSST,73.1\nSTP,69.9\nSUR,73.8\nSVK,78.4\nSVN,82.3\nSWE,84.1\nSWZ,64.3\nSXM,76.5\nSYC,76.3\nSYR,72.6\nTCA,78.2\nTCD,55.2\nTEA,76.0\nTEC,75.0\nTGO,62.9\nTHA,76.6\nTJK,71.9\nTKM,70.2\nTLA,75.7\nTLS,67.9\nTMN,71.5\nTON,73.1\nTSA,72.6\nTSS,62.8\nTTO,73.6\nTUN,76.7\nTUR,77.4\nTUV,67.3\nTZA,67.2\nUGA,68.5\nUKR,74.7\nURY,78.3\nUSA,78.9\nUZB,72.5\nVCT,71.4\nVEN,72.7\nVGB,77.4\nVIR,80.8\nVNM,74.7\nVUT,71.7\nWLD,73.5\nWSM,71.8\nXKX,78.2\nYEM,69.4\nZAF,66.3\nZMB,66.5\nZWE,63.1";
  var _DM_EMBED_CHILD_MORTALITY_WORLD = "iso3,child_mortality_per_1000\nAFE,57.2\nAFG,53\nAFW,91.3\nAGO,49\nALB,9.2\nAND,2.5\nARB,33.7\nARE,4.8\nARG,9.5\nARM,9.6\nATG,9.1\nAUS,3.6\nAUT,3.3\nAZE,17.9\nBDI,47.1\nBEL,3.7\nBEN,74.7\nBFA,74.9\nBGD,30.5\nBGR,5.7\nBHR,8.8\nBHS,12.4\nBIH,6.9\nBLR,2.3\nBLZ,12.7\nBOL,15.7\nBRA,14.2\nBRB,9.8\nBRN,9.9\nBTN,17.2\nBWA,33.3\nCAF,89.7\nCAN,5.4\nCEB,4.8\nCHE,3.9\nCHL,6.8\nCHN,5.7\nCIV,64.5\nCMR,64.8\nCOD,89.7\nCOG,39.1\nCOL,11.5\nCOM,39\nCPV,11.1\nCRI,10.4\nCSS,18.2\nCUB,8.6\nCYP,5\nCZE,2.6\nDEU,3.7\nDJI,48.9\nDMA,35.7\nDNK,4\nDOM,30.6\nDZA,21.6\nEAP,13.9\nEAR,29.4\nEAS,13.2\nECA,13.5\nECS,7.8\nECU,12.9\nEGY,22.4\nEMU,3.7\nERI,34.3\nESP,3.2\nEST,2\nETH,44.5\nEUU,3.9\nFCS,74.0\nFIN,2.4\nFJI,29.1\nFRA,4.3\nFSM,22.4\nGAB,32.6\nGBR,4.7\nGEO,8.6\nGHA,35.9\nGIN,92.1\nGMB,42.4\nGNB,67.3\nGNQ,68\nGRC,3.8\nGRD,18\nGTM,20.5\nGUY,25.2\nHND,15\nHPC,62.4\nHRV,5.6\nHTI,52.8\nHUN,3.7\nIBD,19.6\nIBT,40.3\nIDA,62.7\nIDB,75.9\nIDN,17.7\nIDX,55.9\nIND,26.6\nIRL,3.9\nIRN,11.3\nIRQ,21.8\nISL,2.8\nISR,3.3\nITA,2.7\nJAM,17.8\nJOR,12.9\nJPN,2.4\nKAZ,9.4\nKEN,38.8\nKGZ,16.8\nKHM,18.4\nKIR,53.1\nKNA,15.9\nKOR,2.8\nKWT,8.1\nLAC,15.1\nLAO,29.4\nLBN,17.6\nLBR,86.4\nLBY,9.8\nLCA,17.2\nLCN,15.4\nLDC,58.0\nLKA,5.9\nLMY,40.7\nLSO,60\nLTE,9.0\nLTU,3.4\nLUX,2.2\nLVA,2.5\nMAR,15.7\nMCO,2.7\nMDA,16.5\nMDG,62.4\nMDV,5.4\nMEA,36.4\nMEX,13.1\nMHL,26.7\nMIC,34.3\nMKD,2.8\nMLI,73\nMLT,5.3\nMMR,36.9\nMNA,38.2\nMNE,2.4\nMNG,13.4\nMOZ,59.4\nMRT,36.9\nMUS,15.4\nMWI,48.8\nMYS,8.3\nNAC,6.4\nNAM,39.5\nNER,110.7\nNGA,115.6\nNIC,11.2\nNLD,3.9\nNOR,2.5\nNPL,25.1\nNRU,8.3\nNZL,4.7\nOED,6.7\nOMN,10.3\nOSS,28.8\nPAK,56\nPAN,15.3\nPER,12.9\nPHL,26.5\nPLW,21.4\nPNG,39.4\nPOL,4.2\nPRE,74.5\nPRK,16.7\nPRT,3.2\nPRY,16.3\nPSE,37.9\nPSS,23.1\nPST,4.9\nQAT,5.9\nROU,7.3\nRUS,5.2\nRWA,37.7\nSAS,26.8\nSAU,6\nSDN,61.6\nSEN,36.5\nSGP,2.7\nSLB,20\nSLE,90.5\nSLV,10\nSMR,1.3\nSOM,101.1\nSRB,5.4\nSSA,74.0\nSSD,96.7\nSSF,71.2\nSST,25.6\nSTP,13.6\nSUR,15.8\nSVK,6.3\nSVN,2.3\nSWE,2.4\nSWZ,45.1\nSYC,14\nSYR,18.4\nTCA,4.7\nTCD,97.3\nTEA,13.9\nTEC,10.7\nTGO,56.1\nTHA,9\nTJK,28.1\nTKM,39\nTLA,15.4\nTLS,47.6\nTMN,38.2\nTON,9.5\nTSA,26.8\nTSS,71.1\nTTO,18.8\nTUN,12.1\nTUR,9.6\nTUV,19.2\nTZA,37\nUGA,48.7\nUKR,7.9\nURY,7.4\nUSA,6.5\nUZB,13.4\nVCT,11.3\nVEN,24.2\nVGB,12.4\nVNM,17.3\nVUT,17.1\nWLD,37.4\nWSM,14.9\nXKX,8.7\nYEM,38.1\nZAF,35.1\nZMB,48.4\nZWE,64.7";
  var _DM_EMBED_GDP_WORLD = "iso3,gdp_per_capita_usd\nABW,39499\nAFE,1615\nAFW,1411\nAGO,2666\nALB,11378\nAND,49304\nARB,7584\nARE,50274\nARG,13970\nARM,8556\nATG,23542\nAUS,64604\nAUT,58269\nAZE,7284\nBDI,219\nBEL,56615\nBEN,1485\nBFA,982\nBGD,2593\nBGR,17596\nBHR,29654\nBHS,39455\nBIH,9359\nBLR,8318\nBLZ,7681\nBMU,142855\nBOL,4421\nBRA,10311\nBRB,26545\nBRN,33153\nBWA,7696\nCAF,516\nCAN,54340\nCEB,24605\nCHE,103998\nCHL,16710\nCHN,13303\nCIV,2728\nCMR,1830\nCOD,649\nCOG,2482\nCOL,7919\nCOM,1663\nCPV,5192\nCRI,18587\nCSS,19904\nCUW,22833\nCYP,38674\nCZE,31823\nDEU,56104\nDJI,3553\nDMA,10405\nDNK,71026\nDOM,10876\nDZA,5753\nEAP,10397\nEAR,4450\nEAS,13406\nECA,9794\nECS,31556\nECU,6875\nEGY,3338\nEMU,46945\nESP,35327\nEST,31428\nETH,1134\nEUU,43305\nFCS,1528\nFIN,53150\nFJI,6426\nFRA,46103\nFRO,74120\nFSM,4166\nGAB,8230\nGBR,53246\nGEO,9241\nGHA,2391\nGIN,1695\nGMB,871\nGNB,1008\nGNQ,6745\nGRC,24626\nGRD,11705\nGTM,6150\nGUY,29675\nHKG,54075\nHND,3426\nHPC,1216\nHRV,24050\nHTI,2143\nHUN,23292\nIBD,8220\nIBT,6327\nIDA,1486\nIDB,1651\nIDN,4925\nIDX,1398\nIND,2695\nIRL,112895\nIRN,5190\nIRQ,6074\nISL,86041\nISR,54177\nITA,40385\nJAM,7754\nJOR,4618\nJPN,32487\nKAZ,14155\nKEN,2132\nKGZ,2420\nKHM,2628\nKIR,2289\nKNA,23961\nKOR,36239\nKWT,32718\nLAC,10382\nLAO,2124\nLBR,851\nLBY,6569\nLCA,14182\nLCN,10738\nLDC,1293\nLKA,4516\nLMY,6002\nLSO,972\nLTE,12874\nLTU,29384\nLUX,137782\nLVA,23409\nMAC,72005\nMAR,4153\nMCO,288001\nMDA,7576\nMDG,545\nMDV,13379\nMEA,6265\nMEX,14186\nMHL,7726\nMIC,6554\nMKD,9292\nMLI,1095\nMLT,43899\nMMR,1359\nMNA,2971\nMNE,13263\nMNG,6751\nMOZ,657\nMRT,2110\nMUS,11991\nMWI,523\nMYS,11874\nNAC,81276\nNAM,4413\nNCL,29213\nNER,735\nNGA,1084\nNIC,2848\nNLD,67520\nNOR,86785\nNPL,1447\nNRU,13609\nNZL,49205\nOED,48322\nOMN,20285\nOSS,16094\nPAK,1479\nPAN,19161\nPER,8452\nPHL,3985\nPNG,3007\nPOL,25104\nPRE,1366\nPRI,39344\nPRT,29292\nPRY,6416\nPSE,2592\nPSS,4454\nPST,55644\nPYF,22440\nQAT,76689\nROU,20080\nRUS,14889\nRWA,1000\nSAS,2690\nSAU,35122\nSDN,985\nSEN,1773\nSGP,90674\nSLB,1934\nSLE,807\nSLV,5580\nSOM,630\nSRB,13679\nSSA,1577\nSSF,1533\nSST,15383\nSTP,3491\nSUR,6962\nSVK,25993\nSVN,34301\nSWE,57117\nSWZ,3910\nSXM,41473\nSYC,17859\nTCA,37507\nTCD,962\nTEA,10514\nTEC,13337\nTGO,1119\nTHA,7347\nTJK,1341\nTKM,6857\nTLA,10474\nTLS,1332\nTMN,2974\nTSA,2690\nTSS,1533\nTTO,18733\nTUN,4181\nTUR,15893\nTZA,1187\nUGA,1078\nUKR,5389\nURY,23907\nUSA,84534\nUZB,3162\nVCT,11501\nVEN,4218\nVNM,4717\nVUT,3411\nWLD,13631\nWSM,5393\nXKX,7023\nZAF,6267\nZMB,1187\nZWE,2497";
"""
        + _embed_csv_vars
        + r"""
  var _dmLeafletMapCache = null;
  function getMap() {
    if (_dmLeafletMapCache && _dmLeafletMapCache.eachLayer && _dmLeafletMapCache.getZoom) return _dmLeafletMapCache;
    /* Strategy 0: Leaflet attaches map on container as _leaflet_map (often non-enumerable) */
    try {
      var el0 = document.querySelector('.folium-map');
      if (el0 && el0._leaflet_map && el0._leaflet_map.eachLayer && el0._leaflet_map.getZoom) {
        _dmLeafletMapCache = el0._leaflet_map;
        return el0._leaflet_map;
      }
    } catch (e0) {}
    /* Strategy 1: find the Leaflet map instance from the folium-map container */
    try {
      var containers = document.querySelectorAll('.folium-map');
      for (var i = 0; i < containers.length; i++) {
        if (containers[i]._leaflet_id) {
          for (var k in containers[i]) {
            if (k.indexOf('_leaflet_map') === 0 || (containers[i][k] && containers[i][k]._zoom !== undefined && containers[i][k].eachLayer)) {
              _dmLeafletMapCache = containers[i][k];
              return containers[i][k];
            }
          }
        }
      }
    } catch (e) {}
    /* Strategy 2: scan window for Folium's global map_xxx variable */
    try {
      var keys = Object.keys(window);
      for (var j = 0; j < keys.length; j++) {
        if (/^map_[a-f0-9]+$/i.test(keys[j])) {
          var candidate = window[keys[j]];
          if (candidate && candidate.eachLayer && candidate.getZoom) {
            _dmLeafletMapCache = candidate;
            return candidate;
          }
        }
      }
    } catch (e) {}
    return null;
  }
  function openSidebarIfClosed() {
    var sidebar = document.getElementById('dm-sidebar');
    var btn = document.getElementById('dm-sidebar-toggle');
    if (sidebar && sidebar.classList.contains('dm-sidebar-closed')) {
      sidebar.classList.remove('dm-sidebar-closed');
      var iconSpan = btn ? btn.querySelector('.dm-toggle-icon') : null;
      var labelSpan = btn ? btn.querySelector('.dm-toggle-label') : null;
      if (iconSpan) iconSpan.textContent = 'X';
      if (labelSpan) labelSpan.textContent = 'Close';
    }
  }
  function activateMainTab(tabId) {
    document.querySelectorAll('.dm-tab').forEach(function(bt) {
      var t = bt.getAttribute('data-tab');
      var on = t === tabId;
      bt.classList.toggle('active', on);
      bt.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.dm-tab-panel').forEach(function(p) {
      p.classList.toggle('active', p.id === 'dm-panel-' + tabId);
    });
  }
  function activateSubTabInPanel(panelId, subId) {
    var parent = document.getElementById('dm-panel-' + panelId);
    if (!parent) return;
    parent.querySelectorAll('.dm-sub-tab').forEach(function(b) { b.classList.remove('active'); });
    parent.querySelectorAll('.dm-sub-panel').forEach(function(p) { p.classList.remove('active'); });
    var subTab = parent.querySelector('.dm-sub-tab[data-sub="' + subId + '"]');
    var subPanel = document.getElementById('dm-' + panelId + '-' + subId);
    if (subTab) subTab.classList.add('active');
    if (subPanel) subPanel.classList.add('active');
  }
  function openDataWorkspace(sub) {
    openSidebarIfClosed();
    activateMainTab('data');
    activateSubTabInPanel('data', sub || 'discover');
  }
  function parseCSVLine(line) {
    var out = [], cur = '', inQ = false;
    var s = String(line || '');
    for (var i = 0; i < s.length; i++) {
      var c = s.charAt(i);
      if (c === '"') { inQ = !inQ; continue; }
      if (!inQ && c === ',') { out.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    out.push(cur.trim());
    return out;
  }
  function splitCsvLines(text) {
    return String(text || '').split(/\r?\n/).filter(function(l) { return l.trim(); });
  }
  function findLatLonColumnIndices(headers) {
    var latIdx = -1, lonIdx = -1;
    for (var i = 0; i < headers.length; i++) {
      var h = headers[i];
      if (latIdx < 0 && (h === 'lat' || h === 'latitude' || h.indexOf('latitude') === 0)) latIdx = i;
      if (lonIdx < 0 && (h === 'lon' || h === 'lng' || h === 'long' || h === 'longitude' || h.indexOf('longitude') === 0)) lonIdx = i;
    }
    return { latIdx: latIdx, lonIdx: lonIdx };
  }
  function getPointMarkerRadius() {
    var el = document.getElementById('dm-point-radius');
    if (el && el.value != null && el.value !== '') return parseFloat(el.value) || 10;
    var stored = getStored('point-radius');
    return stored ? (parseFloat(stored) || 10) : 10;
  }
  function applyPointMarkerRadius(radius) {
    var r = radius != null ? Number(radius) : getPointMarkerRadius();
    if (!isFinite(r)) r = 10;
    r = Math.max(2, Math.min(60, r));
    try { setStored('point-radius', String(r)); } catch (eSt) {}
    var grp = window._dmUserLayer;
    if (!grp || !grp.eachLayer) return;
    grp.eachLayer(function(layer) {
      if (layer && typeof layer.setRadius === 'function') layer.setRadius(r);
    });
  }
  function addPointsFromCSVText(csvText) {
    var map = getMap();
    var addDataText = document.getElementById('dm-add-data-csv');
    if (typeof L === 'undefined') {
      showToast('Map library not loaded yet. Wait a moment and try again.');
      return 0;
    }
    if (!map) {
      showToast('Could not find the map. Refresh the page, or wait until the map finishes loading.');
      return 0;
    }
    if (!addDataText) {
      showToast('Add-data form is missing from the page. Regenerate the map (run data-mapping.py again).');
      return 0;
    }
    if (!window._dmUserLayer) { window._dmUserLayer = L.layerGroup().addTo(map); }
    var csv = (csvText != null ? csvText : addDataText.value || '').trim();
    if (!csv) { showToast('Paste CSV with headers: lat, lon, name (optional), value (optional)'); return 0; }
    var lines = splitCsvLines(csv);
    if (lines.length < 2) { showToast('Need a header row and at least one data row.'); return 0; }
    var headers = parseCSVLine(lines[0]).map(function(h){ return h.toLowerCase().replace(/[^a-z0-9]/g,''); });
    var latLon = findLatLonColumnIndices(headers);
    var latIdx = latLon.latIdx;
    var lonIdx = latLon.lonIdx;
    if (latIdx < 0 || lonIdx < 0) { showToast('CSV must include lat/latitude and lon/lng/longitude columns.'); return 0; }
    var nameIdx = headers.indexOf('name') >= 0 ? headers.indexOf('name') : (headers.indexOf('location') >= 0 ? headers.indexOf('location') : -1);
    var valueIdx = -1;
    for (var hi = 0; hi < headers.length; hi++) {
      if (headers[hi] === 'value' || (headers[hi] && headers[hi].indexOf('val') === 0)) { valueIdx = hi; break; }
    }
    if (valueIdx < 0) valueIdx = headers.length > 3 ? 3 : 2;
    var ptRadius = getPointMarkerRadius();
    var added = 0;
    for (var r = 1; r < lines.length; r++) {
      var parts = parseCSVLine(lines[r]);
      if (parts.length <= Math.max(latIdx, lonIdx)) continue;
      var lat = parseFloat(parts[latIdx]), lon = parseFloat(parts[lonIdx]);
      if (isNaN(lat) || isNaN(lon)) continue;
      var name = nameIdx >= 0 ? (parts[nameIdx] || '').trim() : '';
      var val = valueIdx >= 0 ? parseFloat(parts[valueIdx]) : 50;
      if (isNaN(val)) val = 50;
      var color = valueToColor(val, 0, 100, 'YlOrRd');
      L.circleMarker([lat, lon], { radius: ptRadius, color: '#333', fillColor: color, fillOpacity: 0.8, weight: 1, _dmName: (name || (lat.toFixed(2) + ', ' + lon.toFixed(2))), _dmValue: val })
        .bindPopup((name || lat.toFixed(2) + ', ' + lon.toFixed(2)) + ': ' + val)
        .addTo(window._dmUserLayer);
      added++;
    }
    if (added === 0 && lines.length > 1) {
      showToast('No points added. Check each row has valid lat,lon numbers.');
    }
    return added;
  }
  function getMapContainer() { return document.querySelector('.folium-map'); }
  function getEmbeddedMapData() {
    var el = document.getElementById('dm-map-data');
    if (!el || !el.textContent) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  function getStored(key) { try { return localStorage.getItem('dm-' + key); } catch (e) { return null; } }
  function setStored(key, val) { try { localStorage.setItem('dm-' + key, val); } catch (e) {} }
  /** Never persist full GeoJSON in localStorage (quota ~5MB; admin-1 alone is tens of MB). Boundaries stay in window._dmGeoJsonData. */
  function stringifyLiveMapData(md) {
    var o = {};
    if (!md || typeof md !== 'object') return '{}';
    for (var k in md) {
      if (!Object.prototype.hasOwnProperty.call(md, k)) continue;
      if (k === 'geo_json') continue;
      o[k] = md[k];
    }
    if (o.type == null || o.type === '') o.type = 'choropleth';
    try { return JSON.stringify(o); } catch (e) { return '{}'; }
  }
  function defaultTitle() {
    var app = document.getElementById('dm-app');
    return (app && app.getAttribute('data-default-title')) || 'Data map';
  }
  function showToast(msg) {
    var container = document.getElementById('dm-toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'dm-toast-container';
      document.body.appendChild(container);
    }
    var t = document.createElement('div');
    t.className = 'dm-toast';
    t.textContent = msg;
    container.appendChild(t);
    setTimeout(function() { if (t.parentNode) t.parentNode.removeChild(t); }, 3000);
  }

  function regionCsvHints() {
    return {
      iso_column: (document.getElementById('dm-region-iso-col') && document.getElementById('dm-region-iso-col').value.trim()) || '',
      value_column: (document.getElementById('dm-region-value-col') && document.getElementById('dm-region-value-col').value.trim()) || '',
      year_column: (document.getElementById('dm-region-year-col') && document.getElementById('dm-region-year-col').value.trim()) || '',
      geo_keys: (document.getElementById('dm-region-key-mode') && document.getElementById('dm-region-key-mode').value) || 'auto'
    };
  }

  function loadRegionCsvIntoForm(csvText, fileName) {
    var ta = document.getElementById('dm-region-csv');
    var statusEl = document.getElementById('dm-region-import-status');
    var txt = String(csvText || '');
    if (ta) ta.value = txt;
    var lines = splitCsvLines(txt);
    var n = Math.max(0, lines.length - 1);
    if (statusEl) {
      statusEl.textContent = fileName
        ? ('Loaded ' + fileName + ' (' + n + ' data row' + (n === 1 ? '' : 's') + '). Preview or Add to map.')
        : '';
    }
    try {
      if (txt.trim().length > 0) populateRegionYearSelect(parseGenericIso3Csv(txt, regionCsvHints()));
    } catch (e) {
      if (statusEl && fileName) statusEl.textContent = 'Loaded ' + fileName + ' — check column hints, then Preview.';
    }
    if (typeof openDataWorkspace === 'function') openDataWorkspace('regions');
    if (fileName) showToast('Loaded ' + fileName);
  }

  function readCsvFile(file, onOk, onErr) {
    if (!file) { if (onErr) onErr('No file selected'); return; }
    var reader = new FileReader();
    reader.onload = function(ev) {
      if (onOk) onOk(String(ev.target && ev.target.result != null ? ev.target.result : ''), file.name);
    };
    reader.onerror = function() {
      if (onErr) onErr('Could not read ' + (file.name || 'file'));
    };
    reader.readAsText(file);
  }

  function setupCsvFileUpload(inputId, onLoaded) {
    var inp = document.getElementById(inputId);
    if (!inp || inp._dmCsvWired) return;
    inp._dmCsvWired = true;
    inp.addEventListener('change', function() {
      var f = inp.files && inp.files[0];
      inp.value = '';
      if (!f) return;
      readCsvFile(f, function(text, name) {
        if (onLoaded) onLoaded(text, name);
      }, function(msg) {
        showToast(msg || 'Could not read file');
      });
    });
  }

  function wireCsvUploadInputs() {
    setupCsvFileUpload('dm-region-csv-file', function(text, name) {
      loadRegionCsvIntoForm(text, name);
      window._dmRegionCsvUploadThenAdd = false;
      setTimeout(function() { importUserRegionChoropleth(true, false); }, 80);
    });
    setupCsvFileUpload('dm-add-data-csv-file', function(text, name) {
      var taP = document.getElementById('dm-add-data-csv');
      if (taP) taP.value = text;
      if (typeof openDataWorkspace === 'function') openDataWorkspace('points');
      window._dmPointsCsvUploadThenAdd = false;
      setTimeout(function() {
        var n = addPointsFromCSVText(text);
        showToast(n ? ('Mapped ' + n + ' point(s) from ' + name) : ('No points in ' + name + ' — need lat & lon columns'));
        if (n && typeof buildLayerControls === 'function') buildLayerControls();
      }, 80);
    });
  }

  /** Fetch remote CSV/JSON. Order: same-origin /dm-proxy (when served), direct browser fetch, then public CORS GET bridge so file:// and flaky networks usually still work. */
  function dmFetch(url, opts) {
    opts = opts || {};
    var u = String(url || '');
    var method = String((opts && opts.method) || 'GET').toUpperCase();
    if (!u || (u.indexOf('http://') !== 0 && u.indexOf('https://') !== 0)) return fetch(u, opts);

    function direct() {
      return fetch(u, Object.assign({ mode: 'cors', cache: 'no-store' }, opts));
    }
    function sameOriginProxy() {
      if (location.protocol !== 'http:' && location.protocol !== 'https:') {
        return Promise.reject(new Error('no same-origin proxy'));
      }
      return fetch('/dm-proxy?url=' + encodeURIComponent(u), Object.assign({ cache: 'no-store' }, opts)).then(function(r) {
        if (!r.ok && (r.status === 404 || r.status === 403 || r.status === 413 || r.status === 502 || r.status === 503 || r.status >= 500)) return direct();
        return r;
      }).catch(function() { return direct(); });
    }
    function publicCorsBridge() {
      if (method !== 'GET') return Promise.reject(new Error('bridge GET only'));
      var bridge = 'https://api.allorigins.win/raw?url=' + encodeURIComponent(u);
      return fetch(bridge, { cache: 'no-store' });
    }
    function firstOk(fns) {
      var i = 0;
      function next() {
        if (i >= fns.length) return Promise.reject(new Error('All fetch routes failed'));
        return Promise.resolve(fns[i++]()).then(function(r) {
          if (r && r.ok) return r;
          return next();
        }).catch(function() { return next(); });
      }
      return next();
    }

    if (method !== 'GET') {
      if (location.protocol === 'http:' || location.protocol === 'https:') {
        return firstOk([ function() { return sameOriginProxy(); }, function() { return direct(); } ]);
      }
      return direct();
    }
    if (location.protocol === 'http:' || location.protocol === 'https:') {
      return firstOk([
        function() { return sameOriginProxy(); },
        function() { return direct(); },
        function() { return publicCorsBridge(); }
      ]);
    }
    return firstOk([ function() { return direct(); }, function() { return publicCorsBridge(); } ]);
  }

  /** OpenAI from the browser hits CORS; when served via data-mapping.py --serve, POST same-origin to /dm-openai relay. */
  function dmOpenAiChatCompletions(bodyObj, apiKey) {
    var payload = JSON.stringify(bodyObj);
    var headers = { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + apiKey };
    var url = (location.protocol === 'http:' || location.protocol === 'https:')
      ? '/dm-openai'
      : 'https://api.openai.com/v1/chat/completions';
    return fetch(url, { method: 'POST', headers: headers, body: payload }).then(function(r) {
      return r.text().then(function(t) {
        var data;
        try {
          data = JSON.parse(t);
        } catch (e) {
          data = { error: { message: (t && t.slice ? t.slice(0, 300) : 'Non-JSON response') + ' (HTTP ' + r.status + ')' } };
        }
        return { ok: r.ok, status: r.status, data: data };
      });
    });
  }

  /** Parse JSON from OpenAI message.content (handles optional ```json fences or leading prose). */
  function tryParseAiJsonContent(content) {
    var s = String(content || '').trim();
    if (!s) throw new Error('Empty AI response body');
    var fence = s.match(/```(?:json)?\\s*([\\s\\S]*?)```/);
    if (fence) s = fence[1].trim();
    var brace = s.indexOf('{');
    if (brace > 0) s = s.slice(brace);
    return JSON.parse(s);
  }

  function getInsightsData() {
    var md = getMapData();
    if (md && ((md.metrics && Object.keys(md.metrics).length > 0) || (md.values && typeof md.values === 'object' && Object.keys(md.values).length > 0))) {
      var computed = computeLiveInsightsFromMapData(md);
      if (computed && computed.metrics && Object.keys(computed.metrics).length > 0) return computed;
    }
    try {
      var live = getStored('live-insights');
      if (live) {
        var parsed = _safeJSONParse(live, null);
        if (parsed && parsed.metrics && Object.keys(parsed.metrics).length > 0) return parsed;
      }
    } catch (e) {}
    var el = document.getElementById('dm-insights-data');
    if (!el || !el.textContent) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  function _pearsonCorrelation(xs, ys) {
    var n = xs.length;
    if (n < 3) return NaN;
    var mx = 0, my = 0, i;
    for (i = 0; i < n; i++) { mx += xs[i]; my += ys[i]; }
    mx /= n; my /= n;
    var num = 0, dx = 0, dy = 0;
    for (i = 0; i < n; i++) {
      var vx = xs[i] - mx, vy = ys[i] - my;
      num += vx * vy; dx += vx * vx; dy += vy * vy;
    }
    if (dx <= 0 || dy <= 0) return NaN;
    return num / Math.sqrt(dx * dy);
  }

  function _uniqueNumericCount(arr) {
    var o = {};
    for (var i = 0; i < arr.length; i++) {
      if (!isFinite(arr[i])) continue;
      o[String(arr[i])] = 1;
    }
    return Object.keys(o).length;
  }

  function buildMetricInsightBlock(valuesByKey, metricDisplay) {
    var entries = [];
    for (var k in valuesByKey) {
      if (!Object.prototype.hasOwnProperty.call(valuesByKey, k)) continue;
      var v = Number(valuesByKey[k]);
      if (isFinite(v)) entries.push({ location: k, value: v });
    }
    entries.sort(function(a, b) { return b.value - a.value; });
    var n = entries.length;
    if (!n) return null;
    var min = entries[n - 1].value;
    var max = entries[0].value;
    var mean = entries.reduce(function(acc, x) { return acc + x.value; }, 0) / n;
    var median = (n % 2 === 1) ? entries[(n - 1) / 2].value : (entries[n / 2 - 1].value + entries[n / 2].value) / 2;
    /* entries is sorted descending, so index 0=max, n-1=min.
       Q3 (75th pct, high end) = entries[floor(n*0.25)]
       Q1 (25th pct, low end)  = entries[floor(n*0.75)] */
    var q3Idx = Math.min(n - 1, Math.floor(n * 0.25));
    var q1Idx = Math.min(n - 1, Math.floor(n * 0.75));
    var q3 = entries[q3Idx].value;
    var q1 = entries[q1Idx].value;
    var disparity = (min > 0) ? (max / min) : null;
    var top5 = entries.slice(0, 5).map(function(x) { return { location: x.location, value: x.value }; });
    var bottom5 = entries.slice(Math.max(0, n - 5)).map(function(x) { return { location: x.location, value: x.value }; });
    var q1Cut = entries[Math.floor(n * 0.75)] ? entries[Math.floor(n * 0.75)].value : entries[Math.max(0, Math.floor(n * 0.75) - 1)].value;
    var priority = entries.filter(function(x) { return x.value <= q1Cut; }).slice(0, 15).map(function(x) { return { location: x.location, value: x.value }; });
    var belowMedianPct = entries.filter(function(x) { return x.value < median; }).length / n * 100;
    var bottomQuartPct = priority.length / n * 100;
    var varSum = 0;
    for (var ii = 0; ii < n; ii++) varSum += Math.pow(entries[ii].value - mean, 2);
    var std = n > 1 ? Math.sqrt(varSum / n) : null;
    var cv_ratio = (mean !== 0 && std != null) ? std / Math.abs(mean) : null;
    return {
      n: n,
      min: min,
      max: max,
      median: median,
      mean: mean,
      std: std,
      cv_ratio: cv_ratio,
      disparity_ratio: disparity,
      top5: top5,
      bottom5: bottom5,
      below_median: [],
      priority_list: priority,
      below_benchmark: null,
      benchmark: null,
      metric_display: metricDisplay || 'Metric',
      q1: q1,
      q3: q3,
      iqr: (q3 - q1),
      below_median_pct: Math.round(belowMedianPct * 10) / 10,
      bottom_quartile_pct: Math.round(bottomQuartPct * 10) / 10
    };
  }

  function computeLiveInsightsFromMapData(md) {
    md = md || {};
    var series = [];
    if (md.metrics && typeof md.metrics === 'object') {
      Object.keys(md.metrics).forEach(function(mk) {
        var m = md.metrics[mk];
        if (!m || !m.values || typeof m.values !== 'object') return;
        var block = buildMetricInsightBlock(m.values, m.display || mk);
        if (block) series.push({ key: mk, display: m.display || mk, values: m.values, block: block });
      });
    }
    if (!series.length && md.values && typeof md.values === 'object' && Object.keys(md.values).length) {
      var b0 = buildMetricInsightBlock(md.values, md.title || md.name || 'Map values');
      if (b0) series.push({ key: '_values', display: b0.metric_display, values: md.values, block: b0 });
    }
    if (!series.length) return null;

    var metrics = {};
    series.forEach(function(s) { metrics[s.key] = s.block; });

    var correlations = [];
    var i, j, k, keys, xs, ys, c, absC, nPair, minR, minN;
    for (i = 0; i < series.length; i++) {
      for (j = i + 1; j < series.length; j++) {
        var v1 = series[i].values, v2 = series[j].values;
        keys = [];
        for (k in v1) {
          if (!Object.prototype.hasOwnProperty.call(v1, k)) continue;
          if (!Object.prototype.hasOwnProperty.call(v2, k)) continue;
          var a = Number(v1[k]), b = Number(v2[k]);
          if (isFinite(a) && isFinite(b)) keys.push(k);
        }
        nPair = keys.length;
        minN = nPair >= 12 ? 8 : 5;
        minR = nPair >= 15 ? 0.32 : (nPair >= 10 ? 0.38 : 0.48);
        if (nPair < minN) continue;
        xs = []; ys = [];
        for (k = 0; k < keys.length; k++) {
          xs.push(Number(v1[keys[k]]));
          ys.push(Number(v2[keys[k]]));
        }
        if (_uniqueNumericCount(xs) < 2 || _uniqueNumericCount(ys) < 2) continue;
        c = _pearsonCorrelation(xs, ys);
        if (c == null || isNaN(c)) continue;
        absC = Math.abs(c);
        if (absC < minR) continue;
        var strength = absC >= 0.7 ? 'strong' : (absC >= 0.5 ? 'moderate' : 'notable');
        correlations.push({
          metric1: series[i].key,
          metric2: series[j].key,
          metric1_display: series[i].display,
          metric2_display: series[j].display,
          correlation: Math.round(c * 1000) / 1000,
          direction: c > 0 ? 'positive' : 'negative',
          strength: strength,
          n: nPair
        });
      }
    }
    correlations.sort(function(a, b) { return Math.abs(b.correlation) - Math.abs(a.correlation); });
    correlations = correlations.slice(0, 12);

    var key_messages = [];
    series.forEach(function(s) {
      var d = s.block;
      var label = d.metric_display || s.display;
      var meanStd = (d.mean != null && d.std != null) ? (' Mean ' + fmtNum(d.mean, 2) + ', stdev ' + fmtNum(d.std, 2) + '.') : '';
      if (d.disparity_ratio && d.disparity_ratio > 2) {
        key_messages.push(label + ' varies ' + fmtNum(d.disparity_ratio, 1) + 'x across ' + d.n + ' locations; ' + (d.priority_list && d.priority_list.length ? (d.priority_list.length + ' priority areas.') : '') + meanStd);
      } else {
        key_messages.push(label + ': range ' + fmtNum(d.min, 1) + '-' + fmtNum(d.max, 1) + ' across ' + d.n + ' locations.' + meanStd);
      }
    });
    correlations.slice(0, 4).forEach(function(corr) {
      key_messages.push(
        corr.metric1_display + ' and ' + corr.metric2_display + ' show a ' + corr.strength + ' ' + corr.direction +
        ' relationship (r~' + fmtNum(corr.correlation, 2) + ', n=' + corr.n + ').'
      );
    });

    return { metrics: metrics, key_messages: key_messages.slice(0, 10), correlations: correlations };
  }

  /** Generate and display AI overview in the Analysis tab. */
  function generateAiOverview(insights, forceRegenerate) {
    var wrap = document.getElementById('dm-ai-overview-wrap');
    var textEl = document.getElementById('dm-ai-overview-text');
    if (!wrap || !textEl) return;
    if (!insights || !insights.metrics || !Object.keys(insights.metrics).length) {
      wrap.style.display = 'none';
      return;
    }
    wrap.style.display = '';
    /* If we already have text and not forcing, keep it */
    if (!forceRegenerate && textEl.textContent && !textEl.classList.contains('dm-loading')) return;

    /* Build stats summary for the AI prompt */
    var parts = [];
    var mkeys = Object.keys(insights.metrics);
    mkeys.forEach(function(mk) {
      var d = insights.metrics[mk];
      if (!d) return;
      var nm = d.metric_display || mk;
      parts.push(nm + ': n=' + d.n + ', mean=' + fmtNum(d.mean, 1) + ', range ' + fmtNum(d.min, 1) + '-' + fmtNum(d.max, 1) +
        (d.disparity_ratio ? ', disparity ' + fmtNum(d.disparity_ratio, 1) + 'x' : '') +
        (d.priority_list && d.priority_list.length ? ', bottom areas: ' + d.priority_list.slice(0, 6).map(function(x){ return x.location; }).join(', ') : ''));
    });
    var corrParts = [];
    if (insights.correlations) {
      insights.correlations.slice(0, 4).forEach(function(c) {
        corrParts.push(c.metric1_display + ' & ' + c.metric2_display + ': ' + c.strength + ' ' + c.direction + ' (r=' + fmtNum(c.correlation, 2) + ', n=' + c.n + ')');
      });
    }
    var statsSummary = parts.join('\n') + (corrParts.length ? '\n\nCorrelations:\n' + corrParts.join('\n') : '');
    var mapTitle = (document.getElementById('dm-map-title-sidebar') && document.getElementById('dm-map-title-sidebar').textContent) || 'data map';

    /* Try AI first, fall back to computed overview */
    var apiKey = (document.getElementById('dm-ai-api-key') && document.getElementById('dm-ai-api-key').value.trim()) || getStored('openai-api-key') || '';

    if (!apiKey) {
      /* AI-style computed overview — rich vulnerability analysis */
      var lines = [];
      
      /* Header */
      lines.push('📋 DATA OVERVIEW');
      lines.push('Analyzing ' + mkeys.length + ' indicator' + (mkeys.length !== 1 ? 's' : '') + ' across up to ' + (insights.metrics[mkeys[0]] ? insights.metrics[mkeys[0]].n : '?') + ' locations.\n');
      
      /* Find worst disparities */
      var disparities = [];
      mkeys.forEach(function(mk) {
        var d = insights.metrics[mk] || {};
        if (d.disparity_ratio && d.disparity_ratio > 1.5) {
          disparities.push({ name: d.metric_display || mk, ratio: d.disparity_ratio, d: d });
        }
      });
      disparities.sort(function(a, b) { return b.ratio - a.ratio; });
      
      if (disparities.length > 0) {
        lines.push('🔴 VULNERABILITY HOTSPOTS');
        disparities.slice(0, 5).forEach(function(dp) {
          var d = dp.d;
          lines.push('• ' + dp.name + ': ' + fmtNum(dp.ratio, 1) + 'x disparity (range ' + fmtNum(d.min) + ' – ' + fmtNum(d.max) + ')');
          if (d.priority_list && d.priority_list.length) {
            lines.push('  Most vulnerable: ' + d.priority_list.slice(0, 6).map(function(x){ return x.location + ' (' + fmtNum(x.value) + ')'; }).join(', '));
          }
        });
        lines.push('');
      }
      
      /* Correlations as story */
      if (insights.correlations && insights.correlations.length) {
        lines.push('🔗 KEY RELATIONSHIPS');
        insights.correlations.slice(0, 6).forEach(function(c) {
          var arrow = c.direction === 'positive' ? '↑↑' : '↑↓';
          var meaning = c.direction === 'positive'
            ? 'Countries with higher ' + c.metric1_display + ' also tend to have higher ' + c.metric2_display
            : 'Countries with higher ' + c.metric1_display + ' tend to have lower ' + c.metric2_display;
          lines.push('• ' + c.metric1_display + ' ' + arrow + ' ' + c.metric2_display + ' (r=' + fmtNum(c.correlation, 2) + ', ' + c.strength + ')');
          lines.push('  → ' + meaning + '.');
        });
        lines.push('');
      }
      
      /* Action recommendations */
      lines.push('💡 RECOMMENDED ACTIONS');
      if (disparities.length > 0) {
        var worst = disparities[0];
        lines.push('1. Focus resources on ' + worst.name + ' — the largest gap at ' + fmtNum(worst.ratio, 1) + 'x disparity.');
        if (worst.d.priority_list && worst.d.priority_list.length >= 3) {
          lines.push('2. Priority countries: ' + worst.d.priority_list.slice(0, 5).map(function(x){ return x.location; }).join(', ') + '.');
        }
      }
      if (insights.correlations && insights.correlations.length > 0) {
        var topCorr = insights.correlations[0];
        if (topCorr.direction === 'positive') {
          lines.push('3. Improving ' + topCorr.metric1_display + ' may also improve ' + topCorr.metric2_display + ' (strong r=' + fmtNum(topCorr.correlation, 2) + ' link).');
        } else {
          lines.push('3. Reducing ' + topCorr.metric1_display + ' is linked to improving ' + topCorr.metric2_display + ' (r=' + fmtNum(topCorr.correlation, 2) + ').');
        }
      }
      lines.push('4. Use the Layers tab to overlay metrics and visually compare patterns.');
      lines.push('5. Export the map as PNG or PDF for presentations and grant proposals.');
      
      textEl.textContent = lines.join('\n');
      textEl.classList.remove('dm-loading');
      return;
    }

    setStored('openai-api-key', apiKey);
    textEl.textContent = 'Generating overview…';
    textEl.classList.add('dm-loading');

    var prompt = 'You are a social impact data analyst. The user has a map called "' + mapTitle + '" with this data:\n\n' + statsSummary + '\n\n' +
      'Write a concise 3-5 sentence overview for a social good researcher:\n' +
      '1. What does this data reveal about human wellbeing or vulnerability?\n' +
      '2. Which specific locations show the most critical need (name them)?\n' +
      '3. What correlations exist and what do they imply about root causes?\n' +
      '4. What 2-3 concrete interventions or policy actions are most needed?\n' +
      'Be specific, actionable, and direct. No generic filler. Focus on vulnerable populations.';

    dmOpenAiWithRetry({
      messages: [
        { role: 'system', content: 'You are a social impact analyst. Be specific, concise, and action-oriented. Use plain text, no markdown headers.' },
        { role: 'user', content: prompt }
      ],
      max_tokens: 400,
      temperature: 0.4
    }, apiKey).then(function(res) {
      if (!res.ok) throw new Error((res.data && res.data.error && res.data.error.message) || 'AI error');
      var text = res.data && res.data.choices && res.data.choices[0] && res.data.choices[0].message && res.data.choices[0].message.content;
      if (!text) throw new Error('Empty response');
      textEl.textContent = text.trim();
      textEl.classList.remove('dm-loading');
    }).catch(function(err) {
      textEl.textContent = '(AI overview failed: ' + (err && err.message ? err.message.slice(0, 80) : 'unknown error') + '. Add key in Analysis → Ask AI)';
      textEl.classList.remove('dm-loading');
    });
  }

  function renderInsightsLiveRoot(insights) {
    var root = document.getElementById('dm-insights-live-root');
    if (!root) return;
    root.innerHTML = '';
    if (!insights || !insights.metrics || !Object.keys(insights.metrics).length) {
      var p = document.createElement('p');
      p.className = 'dm-add-data-hint';
      p.style.marginTop = '4px';
      p.innerHTML = 'Insights follow the data on your map. Use the <strong>Data</strong> tab to add or layer datasets; when regions have numeric values, takeaways and correlations update automatically. For downloads, run <code>python3 data-mapping.py --serve</code> and open the map over <code>http://</code>.';
      root.appendChild(p);
      return;
    }
    var secStats = document.createElement('div');
    secStats.className = 'dm-section dm-stats-glance';
    var labS = document.createElement('div');
    labS.className = 'dm-section-label';
    labS.textContent = 'Statistics at a glance';
    secStats.appendChild(labS);
    var ulG = document.createElement('ul');
    ulG.className = 'dm-correlation-list';
    Object.keys(insights.metrics).slice(0, 8).forEach(function(mk) {
      var d = insights.metrics[mk] || {};
      var li = document.createElement('li');
      var nm = d.metric_display || mk;
      var parts = [nm + ': n=' + (d.n != null ? d.n : '?')];
      if (d.mean != null) parts.push('mean ' + fmtNum(d.mean));
      if (d.median != null) parts.push('median ' + fmtNum(d.median));
      if (d.std != null) parts.push('stdev ' + fmtNum(d.std));
      if (d.min != null && d.max != null) parts.push('range ' + fmtNum(d.min) + ' – ' + fmtNum(d.max));
      li.textContent = parts.join(', ');
      ulG.appendChild(li);
    });
    secStats.appendChild(ulG);
    root.appendChild(secStats);
    var km = insights.key_messages && insights.key_messages[0];
    if (km) {
      var secK = document.createElement('div');
      secK.className = 'dm-section dm-key-message';
      var lab = document.createElement('div');
      lab.className = 'dm-section-label';
      lab.textContent = 'Key message';
      var pt = document.createElement('p');
      pt.className = 'dm-key-message-text';
      pt.textContent = km;
      secK.appendChild(lab);
      secK.appendChild(pt);
      root.appendChild(secK);
    }
    var secC = document.createElement('div');
    secC.className = 'dm-section dm-key-correlations';
    var labC = document.createElement('div');
    labC.className = 'dm-section-label';
    labC.textContent = 'Key correlations';
    secC.appendChild(labC);
    var cors = insights.correlations || [];
    if (cors.length) {
      var ul = document.createElement('ul');
      ul.className = 'dm-correlation-list';
      cors.slice(0, 8).forEach(function(c) {
        var li = document.createElement('li');
        var m1 = c.metric1_display || c.metric1 || 'A';
        var m2 = c.metric2_display || c.metric2 || 'B';
        var r = c.correlation != null ? Number(c.correlation) : 0;
        li.textContent = m1 + ' and ' + m2 + ': ' + (c.strength || 'notable') + ' ' + (c.direction || 'positive') +
          ' relationship (r=' + fmtNum(r, 2) + ', n=' + (c.n != null ? c.n : '') + ').';
        ul.appendChild(li);
      });
      secC.appendChild(ul);
    } else {
      var hint = document.createElement('p');
      hint.className = 'dm-add-data-hint';
      hint.textContent = Object.keys(insights.metrics).length >= 2
        ? 'No clear linear correlation between layers on the same regions (often sparse overlap, flat values, or different geographies). Try two country-level datasets for the same year, or add layers with the same region codes.'
        : 'Add a second dataset as a layer (Add as layer) so we can compare metrics across the same regions.';
      secC.appendChild(hint);
    }
    root.appendChild(secC);

    var secM = document.createElement('div');
    secM.className = 'dm-section';
    var mkList = Object.keys(insights.metrics);
    var maxDisplay = 10;
    var head = document.createElement('div');
    head.className = 'dm-section-label';
    head.textContent = mkList.length > maxDisplay ? ('Key takeaways (' + maxDisplay + ' of ' + mkList.length + ' metrics)') : 'Key takeaways';
    secM.appendChild(head);
    for (var mi = 0; mi < Math.min(maxDisplay, mkList.length); mi++) {
      var mk = mkList[mi];
      var d = insights.metrics[mk] || {};
      var name = d.metric_display || mk;
      var wrap = document.createElement('div');
      wrap.className = 'dm-metric';
      var h3 = document.createElement('h3');
      h3.className = 'dm-metric-title';
      h3.textContent = name;
      wrap.appendChild(h3);
      var ulm = document.createElement('ul');
      function addLi(label, text) {
        var li = document.createElement('li');
        li.innerHTML = '<strong>' + escapeHtml(label) + '</strong> ' + escapeHtml(String(text));
        ulm.appendChild(li);
      }
      addLi('Range:', fmtNum(d.min) + ' - ' + fmtNum(d.max) + ' across ' + (d.n != null ? d.n : 'n/a') + ' locations.');
      if (d.disparity_ratio != null && d.disparity_ratio > 1.5) {
        addLi('Disparity:', 'Highest is ' + fmtNum(d.disparity_ratio, 1) + 'x the lowest.');
      }
      addLi('Median:', fmtNum(d.median) + '.');
      if (d.mean != null) addLi('Mean:', fmtNum(d.mean) + '.');
      if (d.std != null) addLi('Std dev:', fmtNum(d.std) + (d.cv_ratio != null ? ' (CV ' + fmtNum(d.cv_ratio, 2) + ')' : '') + '.');
      if (d.iqr != null && !isNaN(d.iqr)) addLi('IQR (middle 50%):', fmtNum(d.iqr) + '.');
      var topStr = formatLocationValueList(d.top5 || [], 5);
      var botStr = formatLocationValueList(d.bottom5 || [], 5);
      if (topStr && topStr !== 'n/a') addLi('Highest:', topStr + '.');
      if (botStr && botStr !== 'n/a') addLi('Lowest:', botStr + '.');
      if (d.below_median_pct != null) addLi('Below median:', fmtNum(d.below_median_pct, 1) + '% of locations.');
      wrap.appendChild(ulm);
      var call = document.createElement('div');
      call.className = 'dm-callout';
      var ctitle = document.createElement('p');
      ctitle.className = 'dm-callout-title';
      ctitle.textContent = 'Focus areas';
      call.appendChild(ctitle);
      var ctext = document.createElement('p');
      ctext.className = 'dm-callout-text';
      var pl = d.priority_list || [];
      if (pl.length) ctext.textContent = pl.slice(0, 12).map(function(x) { return x.location != null ? String(x.location) : ''; }).filter(Boolean).join(', ');
      else ctext.textContent = 'No bottom-quartile locations.';
      call.appendChild(ctext);
      wrap.appendChild(call);
      secM.appendChild(wrap);
    }
    root.appendChild(secM);
  }

  function _dmDestroyStatCharts() {
    var arr = window._dmStatCharts || [];
    for (var i = 0; i < arr.length; i++) {
      try { if (arr[i] && typeof arr[i].destroy === 'function') arr[i].destroy(); } catch (eD) {}
    }
    window._dmStatCharts = [];
  }

  /** Insights  ->  Statistics: Chart.js bar, histogram, optional scatter from current map metrics. */
  function renderStatisticsCharts() {
    var root = document.getElementById('dm-statistics-charts-root');
    if (!root) return;
    _dmDestroyStatCharts();
    if (!window._dmStatCharts) window._dmStatCharts = [];
    root.innerHTML = '';
    if (typeof Chart === 'undefined') {
      root.innerHTML = '<p class="dm-add-data-hint">Charts could not load (Chart.js). Check your network and refresh the page.</p>';
      return;
    }
    var md = getMapData() || {};
    var series = [];
    if (md.metrics && typeof md.metrics === 'object') {
      Object.keys(md.metrics).forEach(function(mk) {
        var m = md.metrics[mk];
        if (m && m.values && typeof m.values === 'object' && Object.keys(m.values).length) {
          series.push({ key: mk, display: (m.display || mk).slice(0, 80), values: m.values });
        }
      });
    }
    if (!series.length && md.values && typeof md.values === 'object' && Object.keys(md.values).length) {
      var disp = 'Map values';
      if (md.active_metric && md.metrics && md.metrics[md.active_metric]) disp = md.metrics[md.active_metric].display || disp;
      series.push({ key: '_values', display: disp.slice(0, 80), values: md.values });
    }
    if (!series.length && md.type === 'points' && md.points && md.points.length) {
      var pv = [];
      for (var pi = 0; pi < md.points.length; pi++) {
        var p = md.points[pi];
        if (!p) continue;
        var vv = p.value != null ? Number(p.value) : NaN;
        if (!isFinite(vv)) continue;
        var nm = (p.name != null && String(p.name).trim()) ? String(p.name).trim() : ('Point ' + (pi + 1));
        pv.push({ loc: nm.slice(0, 40), v: vv });
      }
      if (pv.length) {
        pv.sort(function(a, b) { return b.v - a.v; });
        var valsObj = {};
        for (var pj = 0; pj < pv.length; pj++) valsObj['p' + pj] = pv[pj].v;
        series.push({ key: '_points', display: 'Point values', values: valsObj, _pointOrder: pv });
      }
    }
    if (!series.length) {
      root.innerHTML = '<p class="dm-add-data-hint">Add a choropleth dataset or points to the map, then open this tab again.</p>';
      return;
    }

    var palette = ['#667eea', '#764ba2', '#48bb78', '#ed8936', '#38b2ac', '#d53f8c', '#4299e1'];
    function pushChart(title, canvas, config) {
      var wrap = document.createElement('div');
      wrap.className = 'dm-stat-chart-wrap';
      var h = document.createElement('div');
      h.className = 'dm-stat-chart-title';
      h.textContent = title;
      wrap.appendChild(h);
      wrap.appendChild(canvas);
      root.appendChild(wrap);
      var ch = new Chart(canvas, config);
      window._dmStatCharts.push(ch);
    }

    series.forEach(function(s, idx) {
      var entries = [];
      if (s._pointOrder) {
        entries = s._pointOrder.map(function(row) { return { loc: row.loc, v: row.v }; });
      } else {
        for (var k in s.values) {
          if (!Object.prototype.hasOwnProperty.call(s.values, k)) continue;
          var v = Number(s.values[k]);
          if (isFinite(v)) entries.push({ loc: k, v: v });
        }
      }
      entries.sort(function(a, b) { return b.v - a.v; });
      if (!entries.length) return;
      var top = entries.slice(0, 15);
      var c1 = document.createElement('canvas');
      c1.height = 220;
      pushChart(s.display + ' - top ' + Math.min(15, entries.length) + ' (by value)', c1, {
        type: 'bar',
        data: {
          labels: top.map(function(e) {
            var t = String(e.loc || '');
            return t.length > 16 ? t.slice(0, 14) + '..' : t;
          }),
          datasets: [{
            label: s.display,
            data: top.map(function(e) { return e.v; }),
            backgroundColor: (palette[idx % palette.length]) + 'cc'
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#4a5568', maxRotation: 60, minRotation: 45, font: { size: 9 } }, grid: { display: false } },
            y: { ticks: { color: '#4a5568' }, grid: { color: 'rgba(0,0,0,0.06)' } }
          }
        }
      });

      var vals = entries.map(function(e) { return e.v; });
      var vmin = Math.min.apply(null, vals);
      var vmax = Math.max.apply(null, vals);
      var bins = Math.min(14, Math.max(6, Math.floor(Math.sqrt(vals.length)) + 3));
      var counts = [];
      for (var bi = 0; bi < bins; bi++) counts.push(0);
      var span = (vmax - vmin) || 1;
      for (var bj = 0; bj < vals.length; bj++) {
        var t = (vals[bj] - vmin) / span;
        if (t >= 1) t = 0.99999;
        var ix = Math.floor(t * bins);
        if (ix < 0) ix = 0;
        if (ix >= bins) ix = bins - 1;
        counts[ix]++;
      }
      var binLabels = [];
      for (var b = 0; b < bins; b++) {
        var lo = vmin + (span * b / bins);
        var hi = vmin + (span * (b + 1) / bins);
        binLabels.push(fmtNum(lo, 1) + ' to ' + fmtNum(hi, 1));
      }
      var c2 = document.createElement('canvas');
      c2.height = 200;
      pushChart(s.display + ' - distribution (histogram)', c2, {
        type: 'bar',
        data: {
          labels: binLabels,
          datasets: [{ label: 'Count', data: counts, backgroundColor: 'rgba(102,126,234,0.7)' }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#4a5568', maxRotation: 70, font: { size: 8 } }, grid: { display: false } },
            y: { ticks: { color: '#4a5568' }, grid: { color: 'rgba(0,0,0,0.06)' } }
          }
        }
      });
    });

    if (series.length >= 2 && !series[0]._pointOrder && !series[1]._pointOrder) {
      var v1 = series[0].values;
      var v2 = series[1].values;
      var xs = [], ys = [], lab = [];
      for (var key in v1) {
        if (!Object.prototype.hasOwnProperty.call(v1, key)) continue;
        if (!Object.prototype.hasOwnProperty.call(v2, key)) continue;
        var a = Number(v1[key]);
        var b = Number(v2[key]);
        if (isFinite(a) && isFinite(b)) {
          xs.push(a);
          ys.push(b);
          lab.push(key);
        }
      }
      if (xs.length >= 5) {
        var c3 = document.createElement('canvas');
        c3.height = 260;
        pushChart(series[0].display + ' vs ' + series[1].display + ' (same region)', c3, {
          type: 'scatter',
          data: {
            datasets: [{
              label: 'Regions',
              data: xs.map(function(x, i) { return { x: x, y: ys[i] }; }),
              backgroundColor: 'rgba(237,137,54,0.78)',
              pointRadius: 5,
              pointHoverRadius: 7
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: function(ctx) {
                    var i = ctx.dataIndex;
                    return (lab[i] || '') + ': (' + fmtNum(xs[i], 2) + ', ' + fmtNum(ys[i], 2) + ')';
                  }
                }
              }
            },
            scales: {
              x: {
                title: { display: true, text: series[0].display, color: '#2d3748' },
                ticks: { color: '#4a5568' },
                grid: { color: 'rgba(0,0,0,0.06)' }
              },
              y: {
                title: { display: true, text: series[1].display, color: '#2d3748' },
                ticks: { color: '#4a5568' },
                grid: { color: 'rgba(0,0,0,0.06)' }
              }
            }
          }
        });
      }
    }
  }

  function refreshInsightsFromMap() {
    var ins = computeLiveInsightsFromMapData(getMapData());
    if (ins && ins.metrics && Object.keys(ins.metrics).length) {
      try { setStored('live-insights', JSON.stringify(ins)); } catch (e) {}
    } else {
      try { setStored('live-insights', ''); } catch (e) {}
    }
    renderInsightsLiveRoot(ins);
    generateAiOverview(ins, false);
    try {
      var st = document.getElementById('dm-insights-statistics');
      if (st && st.classList.contains('active') && typeof renderStatisticsCharts === 'function') renderStatisticsCharts();
    } catch (eR) {}
  }

  function updateActionHubHero() {
    var steps = [];
    try { steps = JSON.parse(getStored('action-steps') || '[]'); } catch (e) {}
    var n = steps.length;
    var done = 0;
    for (var si = 0; si < n; si++) if (steps[si] && steps[si].done) done++;
    var pct = n ? Math.min(100, Math.round((done / n) * 100)) : 0;
    var fill = document.getElementById('dm-action-progress-fill');
    if (fill) fill.style.width = pct + '%';
    var lab = document.getElementById('dm-action-progress-label');
    if (lab) lab.textContent = n ? (done + ' of ' + n + ' steps done') : 'Add steps below to track momentum';
    var bar = document.getElementById('dm-action-progress-bar');
    if (bar) {
      bar.setAttribute('aria-valuenow', String(pct));
      bar.setAttribute('aria-valuetext', done + ' of ' + n + ' complete');
    }
    var goalLine = document.getElementById('dm-action-goal-line');
    if (goalLine) {
      var g = getStored('map-goal') || '';
      var mt = getStored('map-title') || '';
      if (g && g !== 'Just exploring') goalLine.textContent = 'Goal: ' + g + (mt ? ' · Map: ' + mt : '');
      else goalLine.textContent = 'Set a concrete goal in Insights  ->  Goal & share (or re-open welcome) so this strip reflects what you are driving toward.';
    }
    var mini = document.getElementById('dm-action-board-count');
    if (mini) {
      var vc = 0;
      try { vc = (JSON.parse(getStored('vision-cards') || '[]') || []).length; } catch (e2) {}
      mini.textContent = vc ? (' · ' + vc + ' vision card' + (vc !== 1 ? 's' : '')) : '';
    }
  }

  function appendActionStep(text) {
    text = (text || '').trim();
    if (!text) return false;
    var list = [];
    try { list = JSON.parse(getStored('action-steps') || '[]'); } catch (e) { list = []; }
    list.push({ text: text, done: false });
    setStored('action-steps', JSON.stringify(list));
    var container = document.getElementById('dm-action-steps');
    var idx = list.length - 1;
    if (container) {
      var row = document.createElement('label');
      row.className = 'dm-action-step';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.addEventListener('change', function() {
        row.classList.toggle('done', cb.checked);
        var d = JSON.parse(getStored('action-steps') || '[]');
        if (d[idx]) d[idx].done = cb.checked;
        setStored('action-steps', JSON.stringify(d));
        updateActionHubHero();
      });
      row.appendChild(cb);
      row.appendChild(document.createTextNode(text));
      container.appendChild(row);
    }
    updateActionHubHero();
    return true;
  }

  /* ===========================
   * Data Finder (sources -> preview -> add to map)
   * =========================== */

  function normalizeQuery(q) {
    return String(q || '').toLowerCase().replace(/[^a-z0-9\\s]/g, ' ').replace(/\\s+/g, ' ').trim();
  }

  /** Token  ->  World Bank country scope (ISO2). Broader than the old India/US-only list. Map boundaries stay worldwide when you pick admin1. */
  var _DM_COUNTRY_TOKEN_TO_ISO2 = {
    'india': 'IN', 'mexico': 'MX', 'canada': 'CA', 'brazil': 'BR', 'argentina': 'AR', 'chile': 'CL',
    'colombia': 'CO', 'peru': 'PE', 'ecuador': 'EC', 'venezuela': 'VE', 'bolivia': 'BO', 'paraguay': 'PY',
    'uruguay': 'UY', 'guatemala': 'GT', 'honduras': 'HN', 'nicaragua': 'NI', 'panama': 'PA', 'cuba': 'CU',
    'jamaica': 'JM', 'haiti': 'HT', 'france': 'FR', 'germany': 'DE', 'spain': 'ES', 'italy': 'IT',
    'portugal': 'PT', 'netherlands': 'NL', 'belgium': 'BE', 'luxembourg': 'LU', 'switzerland': 'CH',
    'austria': 'AT', 'poland': 'PL', 'hungary': 'HU', 'romania': 'RO', 'bulgaria': 'BG', 'greece': 'GR',
    'turkey': 'TR', 'norway': 'NO', 'sweden': 'SE', 'finland': 'FI', 'denmark': 'DK', 'iceland': 'IS',
    'russia': 'RU', 'ukraine': 'UA', 'belarus': 'BY', 'japan': 'JP', 'china': 'CN', 'taiwan': 'TW',
    'thailand': 'TH', 'vietnam': 'VN', 'indonesia': 'ID', 'malaysia': 'MY', 'philippines': 'PH',
    'singapore': 'SG', 'australia': 'AU', 'nigeria': 'NG', 'kenya': 'KE', 'egypt': 'EG', 'morocco': 'MA',
    'algeria': 'DZ', 'tunisia': 'TN', 'israel': 'IL', 'pakistan': 'PK', 'bangladesh': 'BD', 'nepal': 'NP',
    'iran': 'IR', 'iraq': 'IQ', 'afghanistan': 'AF', 'ethiopia': 'ET', 'tanzania': 'TZ', 'uganda': 'UG',
    'ghana': 'GH', 'cameroon': 'CM', 'senegal': 'SN', 'ireland': 'IE', 'croatia': 'HR', 'serbia': 'RS',
    'slovakia': 'SK', 'slovenia': 'SI', 'lithuania': 'LT', 'latvia': 'LV', 'estonia': 'EE', 'world': 'all',
    'global': 'all', 'worldwide': 'all'
  };

  function detectCountryIso2(qNorm) {
    if (!qNorm) return null;
    if (/\bworld\b|\bglobal\b|\bentire world\b/.test(qNorm)) return 'all';
    if (qNorm.indexOf('united states') >= 0 || /\busa\b/.test(qNorm)) return 'US';
    if (qNorm.indexOf('united kingdom') >= 0) return 'GB';
    if (/\buk\b/.test(qNorm) && qNorm.indexOf('ukraine') < 0) return 'GB';
    if (qNorm.indexOf('south korea') >= 0) return 'KR';
    if (qNorm.indexOf('north korea') >= 0) return 'KP';
    if (qNorm.indexOf('south africa') >= 0) return 'ZA';
    if (qNorm.indexOf('new zealand') >= 0) return 'NZ';
    if (qNorm.indexOf('czech republic') >= 0 || qNorm.indexOf('czechia') >= 0) return 'CZ';
    if (qNorm.indexOf('sri lanka') >= 0) return 'LK';
    if (qNorm.indexOf('saudi arabia') >= 0) return 'SA';
    if (qNorm.indexOf('costa rica') >= 0) return 'CR';
    if (qNorm.indexOf('el salvador') >= 0) return 'SV';
    if (qNorm.indexOf('dominican republic') >= 0) return 'DO';
    if (qNorm.indexOf('united arab emirates') >= 0) return 'AE';
    var compact = qNorm.replace(/\s+/g, '');
    if (_DM_COUNTRY_TOKEN_TO_ISO2[compact] === 'all') return 'all';
    if (_DM_COUNTRY_TOKEN_TO_ISO2[compact]) return _DM_COUNTRY_TOKEN_TO_ISO2[compact];
    var q = ' ' + qNorm + ' ';
    for (var t in _DM_COUNTRY_TOKEN_TO_ISO2) {
      var iso = _DM_COUNTRY_TOKEN_TO_ISO2[t];
      if (iso === 'all') continue;
      if (q.indexOf(' ' + t + ' ') >= 0) return iso;
    }
    return null;
  }

  function getScopeFromQuery(qNorm) {
    var scope = detectCountryIso2(qNorm);
    if (scope) return scope;
    if (qNorm.indexOf(' in ') >= 0) {
      var parts = qNorm.split(' in ');
      var tail = parts[parts.length - 1].trim();
      var tailNorm = tail.replace(/\s+/g, ' ');
      var fake = detectCountryIso2(tailNorm);
      if (fake) return fake;
      var tailCompact = tail.replace(/\s+/g, '');
      if (_DM_COUNTRY_TOKEN_TO_ISO2[tailCompact]) return _DM_COUNTRY_TOKEN_TO_ISO2[tailCompact];
    }
    return 'all';
  }

  /** Legacy profile ids from older builds  ->  Natural Earth admin-1 worldwide. */
  function mapLegacyGeoProfileId(id) {
    if (id == null || id === '') return id;
    if (id === 'in_states' || id === 'us_states') return 'admin1_global';
    return id;
  }

  function inferGeoProfileFromQuery(qNorm) {
    if (!qNorm || typeof qNorm !== 'string') return null;
    var q = qNorm.trim();
    var stateCue = /\bstates?\b/.test(q) || /\bprovinces?\b/.test(q) || q.indexOf('subnational') >= 0 || q.indexOf('admin1') >= 0 || q.indexOf('admin 1') >= 0;
    var districtCue = /\bdistricts?\b/.test(q);
    var utCue = q.indexOf('union territory') >= 0;
    if (stateCue || districtCue || utCue) return 'admin1_global';
    if (q.indexOf('iso 3166') >= 0 || q.indexOf('iso3166') >= 0) return 'admin1_global';
    return null;
  }

  function applyInferredGeoProfile(qNorm, forcedProfileId) {
    var pid = mapLegacyGeoProfileId(forcedProfileId || inferGeoProfileFromQuery(qNorm));
    if (!pid) return Promise.resolve(null);
    var prev = getStored('dm-active-geo-profile') || 'countries';
    var sel = document.getElementById('dm-geo-profile');
    return ensureGeoProfile(pid).then(function() {
      if (sel && sel.querySelector('option[value="' + pid + '"]')) sel.value = pid;
      setStored('dm-active-geo-profile', pid);
      if (prev !== pid) {
        var label = (sel && sel.options && sel.selectedIndex >= 0) ? sel.options[sel.selectedIndex].text : pid;
        showToast('Geography: ' + label);
      }
      return pid;
    });
  }

  function rankDatasetCandidates(qNorm) {
    // Curated "database" connectors (start with World Bank; we can add OWID, OECD, UNData, etc.).
    // Return a list of dataset descriptors.
    var candidates = [];
    function add(ds) { candidates.push(ds); }

    // World Bank indicators (https://data.worldbank.org/indicator)
    add({
      id: 'wb-life-expectancy',
      title: 'Life expectancy at birth (World Bank)',
      description: 'Latest available year, country-level.',
      provider: 'World Bank',
      indicator: 'SP.DYN.LE00.IN',
      unit: 'years',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });
    add({
      id: 'wb-gdp-per-capita',
      title: 'GDP per capita (current US$) (World Bank)',
      description: 'Latest available year, country-level.',
      provider: 'World Bank',
      indicator: 'NY.GDP.PCAP.CD',
      unit: 'USD',
      defaultColormap: 'plasma',
      granularity: 'Country'
    });
    add({
      id: 'wb-gni-per-capita',
      title: 'GNI per capita (current US$) (World Bank)',
      description: 'Often used as a proxy for average income.',
      provider: 'World Bank',
      indicator: 'NY.GNP.PCAP.CD',
      unit: 'USD',
      defaultColormap: 'plasma',
      granularity: 'Country'
    });
    add({
      id: 'wb-population',
      title: 'Population, total (World Bank)',
      description: 'Latest available year, country-level.',
      provider: 'World Bank',
      indicator: 'SP.POP.TOTL',
      unit: 'people',
      defaultColormap: 'YlOrRd',
      granularity: 'Country'
    });

    // Our World in Data grapher datasets (https://ourworldindata.org/grapher/{slug}.csv)
    add({
      id: 'owid-life-expectancy',
      title: 'Life expectancy (Our World in Data)',
      description: 'OWID grapher dataset. Supports year selection.',
      provider: 'Our World in Data',
      owid_slug: 'life-expectancy',
      unit: 'years',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });
    add({
      id: 'owid-gdp-per-capita',
      title: 'GDP per capita (OWID, World Bank)',
      description: 'World map, country codes. Pick a year or Latest.',
      provider: 'Our World in Data',
      owid_slug: 'gdp-per-capita-worldbank',
      unit: 'USD',
      defaultColormap: 'plasma',
      granularity: 'Country'
    });
    add({
      id: 'owid-child-mortality',
      title: 'Child mortality (Our World in Data)',
      description: 'Country-level time series from OWID.',
      provider: 'Our World in Data',
      owid_slug: 'child-mortality',
      unit: 'deaths',
      defaultColormap: 'plasma_r',
      granularity: 'Country'
    });
    add({
      id: 'owid-maternal-mortality',
      title: 'Maternal mortality (Our World in Data)',
      description: 'Country-level time series from OWID.',
      provider: 'Our World in Data',
      owid_slug: 'maternal-mortality',
      unit: 'per 100k',
      defaultColormap: 'plasma_r',
      granularity: 'Country'
    });
    add({
      id: 'owid-co2-capita',
      title: 'CO₂ emissions per capita (Our World in Data)',
      description: 'Country-level time series from OWID.',
      provider: 'Our World in Data',
      owid_slug: 'co2-emissions-per-capita',
      unit: 't',
      defaultColormap: 'YlOrRd',
      granularity: 'Country'
    });
    add({
      id: 'owid-undernourishment',
      title: 'Prevalence of undernourishment (Our World in Data)',
      description: 'Country-level time series from OWID.',
      provider: 'Our World in Data',
      owid_slug: 'prevalence-of-undernourishment',
      unit: '%',
      defaultColormap: 'YlOrRd',
      granularity: 'Country'
    });
    add({
      id: 'owid-literacy',
      title: 'Literacy rate (Our World in Data)',
      description: 'Country-level where available.',
      provider: 'Our World in Data',
      owid_slug: 'literacy-rate',
      unit: '%',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });
    add({
      id: 'owid-fertility',
      title: 'Fertility rate (Our World in Data)',
      description: 'Births per woman, country-level.',
      provider: 'Our World in Data',
      owid_slug: 'children-born-per-woman',
      unit: 'births',
      defaultColormap: 'plasma',
      granularity: 'Country'
    });

    add({
      id: 'wb-infant-mortality',
      title: 'Mortality rate, infant (per 1,000 live births) (World Bank)',
      description: 'Country-level; lower values mean fewer infant deaths.',
      provider: 'World Bank',
      indicator: 'SP.DYN.IMRT.IN',
      unit: 'per 1,000',
      defaultColormap: 'plasma_r',
      granularity: 'Country'
    });
    add({
      id: 'wb-under5-mortality',
      title: 'Mortality rate, under-5 (per 1,000) (World Bank)',
      description: 'Country-level child survival indicator.',
      provider: 'World Bank',
      indicator: 'SH.DYN.MORT',
      unit: 'per 1,000',
      defaultColormap: 'plasma_r',
      granularity: 'Country'
    });
    add({
      id: 'wb-fertility',
      title: 'Fertility rate, total (births per woman) (World Bank)',
      description: 'Country-level.',
      provider: 'World Bank',
      indicator: 'SP.DYN.TFRT.IN',
      unit: 'births per woman',
      defaultColormap: 'plasma',
      granularity: 'Country'
    });
    add({
      id: 'wb-birth-rate',
      title: 'Birth rate, crude (per 1,000 people) (World Bank)',
      description: 'Country-level.',
      provider: 'World Bank',
      indicator: 'SP.DYN.CBRT.IN',
      unit: 'per 1,000',
      defaultColormap: 'YlOrRd',
      granularity: 'Country'
    });
    add({
      id: 'wb-death-rate',
      title: 'Death rate, crude (per 1,000 people) (World Bank)',
      description: 'Country-level.',
      provider: 'World Bank',
      indicator: 'SP.DYN.CDRT.IN',
      unit: 'per 1,000',
      defaultColormap: 'PuOr',
      granularity: 'Country'
    });
    add({
      id: 'wb-unemployment',
      title: 'Unemployment, total (% of labor force) (World Bank)',
      description: 'Country-level ILO estimate.',
      provider: 'World Bank',
      indicator: 'SL.UEM.TOTL.ZS',
      unit: '%',
      defaultColormap: 'YlOrRd',
      granularity: 'Country'
    });
    add({
      id: 'wb-literacy',
      title: 'Adult literacy rate (% ages 15+) (World Bank)',
      description: 'Country-level; many gaps in early years.',
      provider: 'World Bank',
      indicator: 'SE.ADT.LITR.ZS',
      unit: '%',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });
    add({
      id: 'wb-poverty',
      title: 'Poverty headcount at $2.15/day (2017 PPP) (% of population) (World Bank)',
      description: 'Country-level extreme poverty measure.',
      provider: 'World Bank',
      indicator: 'SI.POV.DDAY',
      unit: '%',
      defaultColormap: 'YlOrRd',
      granularity: 'Country'
    });
    add({
      id: 'wb-internet-users',
      title: 'Individuals using the Internet (% of population) (World Bank)',
      description: 'Country-level ICT access.',
      provider: 'World Bank',
      indicator: 'IT.NET.USER.ZS',
      unit: '%',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });
    add({
      id: 'wb-electricity-access',
      title: 'Access to electricity (% of population) (World Bank)',
      description: 'Country-level energy access.',
      provider: 'World Bank',
      indicator: 'EG.ELC.ACCS.ZS',
      unit: '%',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });
    add({
      id: 'wb-forest-area',
      title: 'Forest area (% of land area) (World Bank)',
      description: 'Country-level environment indicator.',
      provider: 'World Bank',
      indicator: 'AG.LND.FRST.ZS',
      unit: '%',
      defaultColormap: 'Greens',
      granularity: 'Country'
    });
    add({
      id: 'wb-co2-capita',
      title: 'CO₂ emissions (metric tons per capita) (World Bank)',
      description: 'Country-level climate indicator.',
      provider: 'World Bank',
      indicator: 'EN.ATM.CO2E.PC',
      unit: 'metric tons',
      defaultColormap: 'YlOrRd',
      granularity: 'Country'
    });
    add({
      id: 'wb-health-expenditure',
      title: 'Current health expenditure (% of GDP) (World Bank)',
      description: 'Country-level health financing.',
      provider: 'World Bank',
      indicator: 'SH.XPD.CHEX.GD.ZS',
      unit: '% GDP',
      defaultColormap: 'plasma',
      granularity: 'Country'
    });
    add({
      id: 'wb-school-enrollment-primary',
      title: 'School enrollment, primary (% gross) (World Bank)',
      description: 'Country-level education access.',
      provider: 'World Bank',
      indicator: 'SE.PRM.ENRR',
      unit: '% gross',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });
    add({
      id: 'wb-urban-pop',
      title: 'Urban population (% of total) (World Bank)',
      description: 'Country-level urbanization.',
      provider: 'World Bank',
      indicator: 'SP.URB.TOTL.IN.ZS',
      unit: '%',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });
    add({
      id: 'wb-gdp-growth',
      title: 'GDP growth (annual %) (World Bank)',
      description: 'Country-level macro indicator.',
      provider: 'World Bank',
      indicator: 'NY.GDP.MKTP.KD.ZG',
      unit: '%',
      defaultColormap: 'RdYlGn',
      granularity: 'Country'
    });
    add({
      id: 'wb-life-expectancy-female',
      title: 'Life expectancy at birth, female (years) (World Bank)',
      description: 'Country-level.',
      provider: 'World Bank',
      indicator: 'SP.DYN.LE00.FE.IN',
      unit: 'years',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });
    add({
      id: 'wb-life-expectancy-male',
      title: 'Life expectancy at birth, male (years) (World Bank)',
      description: 'Country-level.',
      provider: 'World Bank',
      indicator: 'SP.DYN.LE00.MA.IN',
      unit: 'years',
      defaultColormap: 'viridis',
      granularity: 'Country'
    });

    if (typeof _DM_EMBED_INDIA_MULTI === 'string' && _DM_EMBED_INDIA_MULTI.length > 40) {
      add({
        id: 'in-states-life-expectancy',
        title: 'India  -  life expectancy by state (bundled sample CSV)',
        description: 'States and union territories as ISO 3166-2 (IN-...). Choose World  -  states / provinces (auto-selected when you add).',
        provider: 'Sample data (project file)',
        kind: 'embedded_csv',
        embedded_csv: _DM_EMBED_INDIA_MULTI,
        iso_column: 'location',
        value_column: 'life_expectancy',
        geo_keys: 'iso3166_2',
        geo_profile: 'admin1_global',
        granularity: 'State / UT (India)',
        defaultColormap: 'viridis'
      });
      add({
        id: 'in-states-poverty',
        title: 'India  -  poverty rate by state (bundled sample CSV)',
        description: 'Same geography keys as the life expectancy table.',
        provider: 'Sample data (project file)',
        kind: 'embedded_csv',
        embedded_csv: _DM_EMBED_INDIA_MULTI,
        iso_column: 'location',
        value_column: 'poverty_rate',
        geo_keys: 'iso3166_2',
        geo_profile: 'admin1_global',
        granularity: 'State / UT (India)',
        defaultColormap: 'YlOrRd'
      });
      add({
        id: 'in-states-internet',
        title: 'India  -  internet penetration by state (bundled sample CSV)',
        description: 'Illustrative percentages for map coloring.',
        provider: 'Sample data (project file)',
        kind: 'embedded_csv',
        embedded_csv: _DM_EMBED_INDIA_MULTI,
        iso_column: 'location',
        value_column: 'internet_penetration',
        geo_keys: 'iso3166_2',
        geo_profile: 'admin1_global',
        granularity: 'State / UT (India)',
        defaultColormap: 'plasma'
      });
      add({
        id: 'in-states-health-index',
        title: 'India  -  composite health index by state (bundled sample CSV)',
        description: 'Illustrative index for teaching / demos.',
        provider: 'Sample data (project file)',
        kind: 'embedded_csv',
        embedded_csv: _DM_EMBED_INDIA_MULTI,
        iso_column: 'location',
        value_column: 'health_index',
        geo_keys: 'iso3166_2',
        geo_profile: 'admin1_global',
        granularity: 'State / UT (India)',
        defaultColormap: 'viridis'
      });
    }
    if (typeof _DM_EMBED_US_STATES_MULTI === 'string' && _DM_EMBED_US_STATES_MULTI.length > 40) {
      add({
        id: 'us-states-life-expectancy-sample',
        title: 'United States  -  life expectancy by state (sample CSV)',
        description: 'All states + DC as US-... ISO codes. Uses bundled sample values for coloring (not official CDC figures).',
        provider: 'Sample data (project file)',
        kind: 'embedded_csv',
        embedded_csv: _DM_EMBED_US_STATES_MULTI,
        iso_column: 'location',
        value_column: 'life_expectancy',
        geo_keys: 'iso3166_2',
        geo_profile: 'admin1_global',
        granularity: 'State (US)',
        defaultColormap: 'viridis'
      });
    }
    if (typeof _DM_EMBED_CANADA_PROVINCES === 'string' && _DM_EMBED_CANADA_PROVINCES.length > 30) {
      add({
        id: 'statcan-canada-provinces-population',
        title: 'Canada  -  population by province and territory (StatCan-style, ISO 3166-2)',
        description: 'Provinces/territories as CA-ON, CA-QC, ... Rounded July 1 estimates for map demos (see Statistics Canada official tables for reporting).',
        provider: 'Statistics Canada (derived bundle)',
        kind: 'embedded_csv',
        embedded_csv: _DM_EMBED_CANADA_PROVINCES,
        iso_column: 'iso_3166_2',
        value_column: 'population',
        year_column: 'year',
        geo_keys: 'iso3166_2',
        geo_profile: 'admin1_global',
        granularity: 'Province / territory (Canada)',
        defaultColormap: 'YlOrRd'
      });
    }
    if (typeof _DM_EMBED_AUSTRALIA_STATES === 'string' && _DM_EMBED_AUSTRALIA_STATES.length > 30) {
      add({
        id: 'abs-australia-states-population',
        title: 'Australia  -  population by state and territory (ABS-style, ISO 3166-2)',
        description: 'States/territories as AU-NSW, AU-VIC, ... Rounded estimates for map demos (see Australian Bureau of Statistics official releases for reporting).',
        provider: 'Australian Bureau of Statistics (derived bundle)',
        kind: 'embedded_csv',
        embedded_csv: _DM_EMBED_AUSTRALIA_STATES,
        iso_column: 'iso_3166_2',
        value_column: 'population',
        year_column: 'year',
        geo_keys: 'iso3166_2',
        geo_profile: 'admin1_global',
        granularity: 'State / territory (Australia)',
        defaultColormap: 'plasma'
      });
    }

    add({
      id: 'demo-in-states-sample',
      title: 'Demo: India  -  sample states (name keys)',
      description: 'Built-in CSV for Karnataka, Tamil Nadu, Maharashtra, Goa. Uses Natural Earth admin-1 (name keys). Search e.g. "India state ..." to match geography.',
      provider: 'Demo',
      kind: 'embedded_csv',
      embedded_csv: ["State,Value", "Karnataka,72", "Tamil Nadu,75", "Maharashtra,68", "Goa,80"].join('\\n'),
      demo_geo_keys: 'name',
      geo_profile: 'admin1_global',
      granularity: 'State (India)',
      defaultColormap: 'viridis'
    });
    add({
      id: 'demo-us-states-sample',
      title: 'Demo: US  -  sample states (ISO 3166-2)',
      description: 'Built-in CSV for CA, NY, TX, FL (ISO 3166-2). Natural Earth admin-1 worldwide. Search e.g. "US state ...".',
      provider: 'Demo',
      kind: 'embedded_csv',
      embedded_csv: ["code,value", "US-CA,82", "US-NY,78", "US-TX,71", "US-FL,76"].join('\\n'),
      demo_geo_keys: 'iso3166_2',
      geo_profile: 'admin1_global',
      granularity: 'State (US)',
      defaultColormap: 'plasma'
    });

    // Simple query-to-indicator scoring (no OpenAI required).
    function score(ds) {
      var s = 2;
      var hay = ((ds.title || '') + ' ' + (ds.description || '') + ' ' + (ds.id || '')).toLowerCase();
      var tw = qNorm.split(/\s+/).filter(function(w) { return w.length > 2; });
      for (var wi = 0; wi < tw.length; wi++) {
        if (hay.indexOf(tw[wi]) >= 0) s += 4;
      }
      if (qNorm.indexOf('life expectancy') >= 0 || qNorm.indexOf('lifespan') >= 0 || qNorm.indexOf('how long people live') >= 0) {
        if (ds.indicator === 'SP.DYN.LE00.IN' || ds.indicator === 'SP.DYN.LE00.FE.IN' || ds.indicator === 'SP.DYN.LE00.MA.IN') s += 10;
        if (ds.owid_slug === 'life-expectancy') s += 10;
        if (ds.id && ds.id.indexOf('life-expectancy') >= 0) s += 8;
      }
      if (qNorm.indexOf('income') >= 0 || qNorm.indexOf('earning') >= 0 || qNorm.indexOf('earnings') >= 0 || qNorm.indexOf('wealth') >= 0) {
        if (ds.indicator === 'NY.GNP.PCAP.CD') s += 10;
        if (ds.indicator === 'NY.GDP.PCAP.CD') s += 7;
        if (ds.owid_slug === 'gdp-per-capita-worldbank') s += 6;
      }
      if (qNorm.indexOf('gdp') >= 0 || qNorm.indexOf('economy') >= 0 || qNorm.indexOf('economic') >= 0) {
        if (ds.indicator === 'NY.GDP.PCAP.CD' || ds.indicator === 'NY.GDP.MKTP.KD.ZG') s += 8;
        if (ds.owid_slug === 'gdp-per-capita-worldbank') s += 10;
      }
      if (qNorm.indexOf('gni') >= 0) s += (ds.indicator === 'NY.GNP.PCAP.CD') ? 10 : 0;
      if (qNorm.indexOf('population') >= 0 || qNorm.indexOf('people') >= 0) s += (ds.indicator === 'SP.POP.TOTL') ? 10 : 0;
      if (qNorm.indexOf('unemployment') >= 0 || qNorm.indexOf('jobless') >= 0 || qNorm.indexOf('jobs') >= 0) s += (ds.indicator === 'SL.UEM.TOTL.ZS') ? 12 : 0;
      if (qNorm.indexOf('poverty') >= 0 || qNorm.indexOf('poor') >= 0) s += (ds.indicator === 'SI.POV.DDAY') ? 12 : 0;
      if (qNorm.indexOf('literacy') >= 0 || qNorm.indexOf('reading') >= 0 || qNorm.indexOf('education') >= 0 || qNorm.indexOf('school') >= 0) {
        if (ds.indicator === 'SE.ADT.LITR.ZS' || ds.indicator === 'SE.PRM.ENRR') s += 10;
        if (ds.owid_slug === 'literacy-rate') s += 10;
      }
      if (qNorm.indexOf('internet') >= 0 || qNorm.indexOf('online') >= 0 || qNorm.indexOf('digital') >= 0) s += (ds.indicator === 'IT.NET.USER.ZS') ? 12 : 0;
      if (qNorm.indexOf('electric') >= 0 || qNorm.indexOf('power') >= 0 || qNorm.indexOf('energy access') >= 0) s += (ds.indicator === 'EG.ELC.ACCS.ZS') ? 12 : 0;
      if (qNorm.indexOf('forest') >= 0 || qNorm.indexOf('tree') >= 0 || qNorm.indexOf('deforest') >= 0) s += (ds.indicator === 'AG.LND.FRST.ZS') ? 12 : 0;
      if (qNorm.indexOf('co2') >= 0 || qNorm.indexOf('carbon') >= 0 || qNorm.indexOf('emission') >= 0 || qNorm.indexOf('climate') >= 0) {
        if (ds.indicator === 'EN.ATM.CO2E.PC' || ds.owid_slug === 'co2-emissions-per-capita') s += 12;
      }
      if (qNorm.indexOf('health') >= 0 || qNorm.indexOf('hospital') >= 0 || qNorm.indexOf('doctor') >= 0) {
        if (ds.indicator === 'SH.XPD.CHEX.GD.ZS') s += 10;
        if (ds.id && ds.id.indexOf('health') >= 0) s += 8;
      }
      if (qNorm.indexOf('child') >= 0 || qNorm.indexOf('infant') >= 0 || qNorm.indexOf('baby') >= 0 || qNorm.indexOf('under five') >= 0 || qNorm.indexOf('under-5') >= 0) {
        if (ds.indicator === 'SP.DYN.IMRT.IN' || ds.indicator === 'SH.DYN.MORT') s += 12;
        if (ds.owid_slug === 'child-mortality') s += 12;
      }
      if (qNorm.indexOf('maternal') >= 0 || qNorm.indexOf('mother') >= 0) s += (ds.owid_slug === 'maternal-mortality') ? 12 : 0;
      if (qNorm.indexOf('hunger') >= 0 || qNorm.indexOf('malnutrition') >= 0 || qNorm.indexOf('undernourish') >= 0) s += (ds.owid_slug === 'prevalence-of-undernourishment') ? 12 : 0;
      if (qNorm.indexOf('fertility') >= 0 || qNorm.indexOf('birth rate') >= 0 || qNorm.indexOf('births per') >= 0) {
        if (ds.indicator === 'SP.DYN.TFRT.IN' || ds.indicator === 'SP.DYN.CBRT.IN') s += 10;
        if (ds.owid_slug === 'children-born-per-woman') s += 10;
      }
      if (qNorm.indexOf('death rate') >= 0 || qNorm.indexOf('mortality crude') >= 0) s += (ds.indicator === 'SP.DYN.CDRT.IN') ? 10 : 0;
      if (qNorm.indexOf('urban') >= 0) s += (ds.indicator === 'SP.URB.TOTL.IN.ZS') ? 8 : 0;
      if (qNorm.indexOf('world') >= 0 || qNorm.indexOf('global') >= 0) s += 1;
      if (qNorm.indexOf('india') >= 0) {
        s += 1;
        if (ds.id && String(ds.id).indexOf('in-states') === 0) s += 14;
        if (ds.id === 'demo-in-states-sample') s += 6;
      }
      if (qNorm.indexOf('united states') >= 0 || qNorm.indexOf(' us ') >= 0 || qNorm === 'usa' || qNorm.indexOf('u s ') >= 0 || qNorm.indexOf('america') >= 0 || qNorm.startsWith('us ') || qNorm === 'us' || /\busa\b/.test(qNorm)) {
        if (ds.id === 'us-states-life-expectancy-sample' || ds.id === 'demo-us-states-sample') s += 14;
      }
      if (qNorm.indexOf('canada') >= 0 || qNorm.indexOf('canadian') >= 0 || qNorm.indexOf('statcan') >= 0 || qNorm.indexOf('statistics canada') >= 0) {
        if (ds.id === 'statcan-canada-provinces-population') s += 16;
      }
      if (qNorm.indexOf('australia') >= 0 || qNorm.indexOf('australian') >= 0 || qNorm.indexOf(' abs ') >= 0 || qNorm.indexOf('australian bureau') >= 0) {
        if (ds.id === 'abs-australia-states-population') s += 16;
      }
      if ((qNorm.indexOf('population') >= 0 || qNorm.indexOf('people') >= 0) && qNorm.indexOf('canada') >= 0) {
        if (ds.id === 'statcan-canada-provinces-population') s += 8;
      }
      if ((qNorm.indexOf('population') >= 0 || qNorm.indexOf('people') >= 0) && qNorm.indexOf('australia') >= 0) {
        if (ds.id === 'abs-australia-states-population') s += 8;
      }
      if (/\bstates?\b/.test(qNorm) || /\bprovinces?\b/.test(qNorm) || /\bterritories?\b/.test(qNorm) || /\bregions?\b/.test(qNorm)) {
        if (ds.kind === 'generic_csv') s += 4;
        if (ds.geo_profile === 'admin1_global' || (ds.granularity && String(ds.granularity).toLowerCase().indexOf('country') < 0)) s += 5;
        if (ds.id && (String(ds.id).indexOf('states') >= 0 || String(ds.id).indexOf('admin1') >= 0)) s += 6;
        if (ds.geo_profile === 'admin1_global') {
          var _stc = ['mexico', 'brazil', 'australia', 'nigeria', 'canada', 'germany', 'france', 'japan', 'china', 'indonesia', 'south africa', 'kenya', 'ethiopia', 'egypt', 'argentina', 'colombia', 'peru', 'chile', 'thailand', 'vietnam', 'philippines', 'pakistan', 'bangladesh', 'malaysia', 'turkey', 'poland', 'ukraine', 'spain', 'italy', 'united kingdom', 'ghana', 'morocco', 'tanzania', 'uganda', 'nepal', 'sri lanka', 'new zealand'];
          for (var _si = 0; _si < _stc.length; _si++) {
            if (_stc[_si] && qNorm.indexOf(_stc[_si]) >= 0) { s += 6; break; }
          }
        }
      }
      if (qNorm.indexOf('city') >= 0 || qNorm.indexOf('cities') >= 0 || qNorm.indexOf('neighborhood') >= 0 || qNorm.indexOf('neighbourhood') >= 0 || qNorm.indexOf('local') >= 0 || qNorm.indexOf('microdata') >= 0) {
        if (ds.kind === 'points_csv') s += 14;
      }
      if (ds.kind === 'embedded_csv') {
        if (qNorm.indexOf('demo') >= 0 || qNorm.indexOf('sample') >= 0 || qNorm.indexOf('test') >= 0) s += 8;
        if (ds.id === 'demo-in-states-sample' && qNorm.indexOf('india') >= 0 && (/\bstates?\b/.test(qNorm) || /\bprovinces?\b/.test(qNorm) || /\bdistricts?\b/.test(qNorm))) s += 16;
        if (ds.id === 'demo-us-states-sample' && (qNorm.indexOf('us ') >= 0 || qNorm.indexOf('united states') >= 0 || qNorm === 'usa') && /\bstates?\b/.test(qNorm)) s += 16;
        if (ds.id === 'statcan-canada-provinces-population' && (qNorm.indexOf('canada') >= 0 || qNorm.indexOf('province') >= 0)) s += 10;
        if (ds.id === 'abs-australia-states-population' && qNorm.indexOf('australia') >= 0) s += 10;
      /* Boost embedded instant datasets to top of results */
      if (ds.kind === 'embedded_csv' && ds.id && ds.id.indexOf('embed-') === 0) s += 20;
      }
      return s;
    }


    /* ── Embedded world datasets (instant, no network) ── */
    add({
      id: 'embed-life-expectancy-world',
      title: 'Life expectancy at birth (all countries)',
      description: 'World Bank 2024 data. 260+ countries. Instant — no download needed.',
      provider: 'World Bank (embedded)',
      kind: 'embedded_csv',
      embedded_csv: _DM_EMBED_LIFE_EXPECTANCY_WORLD,
      iso_column: 'iso3',
      value_column: 'life_expectancy',
      geo_keys: 'iso3',
      geo_profile: 'countries',
      unit: 'years',
      defaultColormap: 'viridis',
      granularity: 'Country',
      tags: 'life expectancy health longevity age years'
    });
    add({
      id: 'embed-child-mortality-world',
      title: 'Child mortality, under-5 (all countries)',
      description: 'World Bank 2024 data. Deaths per 1,000 live births. 240 countries. Instant.',
      provider: 'World Bank (embedded)',
      kind: 'embedded_csv',
      embedded_csv: _DM_EMBED_CHILD_MORTALITY_WORLD,
      iso_column: 'iso3',
      value_column: 'child_mortality_per_1000',
      geo_keys: 'iso3',
      geo_profile: 'countries',
      unit: 'per 1,000',
      defaultColormap: 'YlOrRd',
      granularity: 'Country',
      tags: 'child mortality infant death children health'
    });
    add({
      id: 'embed-gdp-per-capita-world',
      title: 'GDP per capita (all countries)',
      description: 'World Bank 2024, current US$. 235+ countries. Instant.',
      provider: 'World Bank (embedded)',
      kind: 'embedded_csv',
      embedded_csv: _DM_EMBED_GDP_WORLD,
      iso_column: 'iso3',
      value_column: 'gdp_per_capita_usd',
      geo_keys: 'iso3',
      geo_profile: 'countries',
      unit: 'USD',
      defaultColormap: 'plasma',
      granularity: 'Country',
      tags: 'gdp income economy wealth per capita money'
    });

    candidates.forEach(function(ds) { ds._score = score(ds); });
    try {
      window._dmDatasetCatalog = candidates.map(function(d) {
        var o = {};
        for (var k in d) {
          if (Object.prototype.hasOwnProperty.call(d, k) && k !== '_score') o[k] = d[k];
        }
        return o;
      });
    } catch (eCat) { window._dmDatasetCatalog = []; }
    candidates.sort(function(a, b) { return (b._score || 0) - (a._score || 0); });
    var ranked = candidates.slice(0, 28);
    var extras = window._dmAiExtraDatasets || [];
    var seen = {};
    var merged = [];
    extras.forEach(function(d) {
      if (d && d.id && !seen[d.id]) { seen[d.id] = true; merged.push(d); }
    });
    ranked.forEach(function(d) {
      if (d && d.id && !seen[d.id]) { seen[d.id] = true; merged.push(d); }
    });
    return merged.slice(0, 28);
  }

  /** Resolve a dataset card by id even if it is outside the top-N ranked list or the search box changed. */
  function findDatasetDescriptorById(dsId) {
    if (!dsId) return null;
    var ex = window._dmAiExtraDatasets || [];
    for (var i = 0; i < ex.length; i++) {
      if (ex[i] && ex[i].id === dsId) return ex[i];
    }
    if (!window._dmDatasetCatalog || !window._dmDatasetCatalog.length) rankDatasetCandidates('');
    var cat = window._dmDatasetCatalog || [];
    for (var j = 0; j < cat.length; j++) {
      if (cat[j] && cat[j].id === dsId) return cat[j];
    }
    return null;
  }

  function owidDatasetLink(slug) {
    return 'https://ourworldindata.org/grapher/' + encodeURIComponent(slug);
  }

  function owidCsvUrl(slug) {
    // useColumnShortNames adds `Entity`, `Code`, `Year`, and a short value column.
    return owidDatasetLink(slug) + '.csv?useColumnShortNames=true';
  }

  function fetchOwidDataset(slug) {
    return dmFetch(owidCsvUrl(slug)).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' loading OWID CSV');
      return r.text();
    }).then(function(csvText) {
      var lines = splitCsvLines(csvText);
      if (lines.length < 2) throw new Error('Unexpected OWID CSV response');
      var headers = parseCSVLine(lines[0]).map(function(h){ return String(h || '').trim(); });
      function colIdx(name) {
        var nl = String(name || '').toLowerCase();
        for (var i = 0; i < headers.length; i++) {
          if (String(headers[i] || '').trim().toLowerCase() === nl) return i;
        }
        return -1;
      }
      var entityIdx = colIdx('entity');
      var codeIdx = colIdx('code');
      var yearIdx = colIdx('year');
      var valueIdx = -1;
      for (var i = 0; i < headers.length; i++) {
        if (i !== entityIdx && i !== codeIdx && i !== yearIdx) { valueIdx = i; break; }
      }
      if (codeIdx < 0 || yearIdx < 0 || valueIdx < 0) throw new Error('Could not parse OWID CSV columns');

      var byYear = {};
      var years = {};
      for (var li = 1; li < lines.length; li++) {
        var parts = parseCSVLine(lines[li]);
        if (parts.length <= Math.max(codeIdx, yearIdx, valueIdx)) continue;
        var codeRaw = (parts[codeIdx] || '').trim();
        var code = codeRaw.toUpperCase().replace(/\s+/g, '');
        var year = (parts[yearIdx] || '').trim();
        var val = Number(String(parts[valueIdx] || '').replace(/,/g, ''));
        if (!code) continue;
        var iso3166 = /^[A-Z]{2}-[A-Z0-9]{1,4}$/.test(code);
        var iso3 = /^[A-Z]{3}$/.test(code);
        if (!iso3 && !iso3166) continue;
        if (!year) continue;
        if (!isFinite(val)) continue;
        if (!byYear[year]) byYear[year] = {};
        if (byYear[year][code] == null) byYear[year][code] = val;
        years[year] = true;
      }
      var yearList = Object.keys(years).sort().reverse();
      if (!yearList.length) throw new Error('No usable rows in OWID CSV (expected ISO3 country codes or ISO 3166-2 region codes such as US-CA).');
      return {
        provider: 'Our World in Data',
        slug: slug,
        url: owidDatasetLink(slug),
        years: yearList,
        byYear: byYear
      };
    });
  }

  function worldBankIndicatorLink(indicator) {
    return 'https://data.worldbank.org/indicator/' + encodeURIComponent(indicator);
  }

  function worldBankApiUrl(countryIso2OrAll, indicator) {
    var c = countryIso2OrAll || 'all';
    return 'https://api.worldbank.org/v2/country/' + encodeURIComponent(c) +
      '/indicator/' + encodeURIComponent(indicator) + '?format=json&per_page=20000';
  }

  function fetchWorldBankValues(countryIso2OrAll, indicator) {
    var primary = countryIso2OrAll || 'all';
    function load(scope, attempt) {
      attempt = attempt || 0;
      return dmFetch(worldBankApiUrl(scope, indicator)).then(function(r) {
        if (!r.ok) {
          if (attempt < 2 && (r.status === 429 || r.status === 503 || r.status >= 500)) {
            return new Promise(function(res) {
              setTimeout(function() { res(load(scope, attempt + 1)); }, 450 * (attempt + 1));
            });
          }
          throw new Error('World Bank API HTTP ' + r.status);
        }
        return r.json();
      }).then(function(json) {
        if (!json || !json[1] || !Array.isArray(json[1])) throw new Error('Unexpected World Bank response');
        var rows = json[1];
        var meta = { source: 'World Bank', indicator: indicator, url: worldBankIndicatorLink(indicator) };
        var byYear = {};
        var years = {};
        for (var i = 0; i < rows.length; i++) {
          var row = rows[i];
          if (!row || row.value == null) continue;
          /* WB country.id is often 2-letter ISO2; countryiso3code is always ISO3 */
          var iso3 = (row.countryiso3code && /^[A-Za-z]{3}$/.test(row.countryiso3code.trim()))
            ? String(row.countryiso3code).trim().toUpperCase()
            : (row.country && row.country.id && /^[A-Za-z]{3}$/.test(String(row.country.id).trim())
              ? String(row.country.id).trim().toUpperCase()
              : null);
          if (!iso3) continue;
          var year = row.date != null ? String(row.date) : '';
          if (!year) continue;
          if (!byYear[year]) byYear[year] = {};
          if (byYear[year][iso3] == null) byYear[year][iso3] = Number(row.value);
          years[year] = true;
        }
        var yearList = Object.keys(years).sort().reverse();
        if (!yearList.length) throw new Error('No values found for this indicator.');
        return { byYear: byYear, years: yearList, meta: meta };
      });
    }
    return load(primary, 0).catch(function(err) {
      if (primary !== 'all') return load('all', 0);
      throw err;
    });
  }

  /** Parse CSV with ISO3 (or similar) + numeric value into byYear / years for choropleth. */
  function parseGenericIso3Csv(csvText, hints) {
    hints = hints || {};
    var lines = splitCsvLines(csvText);
    if (lines.length < 2) throw new Error('CSV needs a header row and at least one data row');
    var headers = parseCSVLine(lines[0]).map(function(h){ return String(h || '').trim(); });
    var low = headers.map(function(h){ return String(h || '').trim().toLowerCase(); });

    function colByName(name) {
      var n = String(name || '').toLowerCase().trim();
      for (var i = 0; i < low.length; i++) {
        if (low[i] === n) return i;
      }
      return -1;
    }

    var codeIdx = -1;
    if (hints.iso_column) codeIdx = colByName(hints.iso_column);
    if (codeIdx < 0) {
      var tryCodes = ['iso_3166_2', 'iso3166', 'code', 'iso3', 'iso_a3', 'adm0_a3', 'alpha3', 'country code', 'country', 'state_code', 'province_code', 'admin1', 'admin_1', 'adm1', 'subdivision', 'nuts', 'location', 'state', 'province', 'region'];
      for (var t = 0; t < tryCodes.length && codeIdx < 0; t++) codeIdx = colByName(tryCodes[t]);
    }
    if (codeIdx < 0) {
      for (var j = 0; j < low.length; j++) {
        if (low[j].indexOf('iso') === 0 && low[j].indexOf('3') >= 0) { codeIdx = j; break; }
      }
    }

    var yearIdx = -1;
    if (hints.year_column) yearIdx = colByName(hints.year_column);
    if (yearIdx < 0) {
      var tryY = ['year', 'time', 'date', 'yr', 'period'];
      for (var ty = 0; ty < tryY.length && yearIdx < 0; ty++) yearIdx = colByName(tryY[ty]);
    }

    var skipIdx = {};
    function markSkip(j) { if (j >= 0) skipIdx[j] = true; }
    markSkip(codeIdx);
    markSkip(yearIdx);
    for (var j = 0; j < low.length; j++) {
      if (low[j] === 'entity' || low[j] === 'country' || low[j] === 'name' || low[j] === 'continent' || low[j] === 'region') markSkip(j);
    }

    var valueIdx = -1;
    if (hints.value_column) {
      valueIdx = colByName(hints.value_column);
      if (valueIdx < 0) {
        for (var j = 0; j < headers.length; j++) {
          if (headers[j] === hints.value_column) { valueIdx = j; break; }
        }
      }
    }
    if (valueIdx < 0) {
      for (var j = 0; j < headers.length; j++) {
        if (skipIdx[j]) continue;
        var ok = 0, total = 0;
        for (var rr = 1; rr < Math.min(lines.length, 35); rr++) {
          var parts = parseCSVLine(lines[rr]);
          if (j >= parts.length) continue;
          total++;
          if (isFinite(Number(String(parts[j] || '').replace(/,/g, '')))) ok++;
        }
        if (total > 0 && ok / total >= 0.55) { valueIdx = j; break; }
      }
    }

    if (codeIdx < 0 || valueIdx < 0) {
      throw new Error('Could not detect region key and value columns. Try another CSV or add column hints.');
    }

    var keyMode = hints.geo_keys ? String(hints.geo_keys).toLowerCase().replace(/-/g, '_') : 'auto';
    if (keyMode === 'iso31662') keyMode = 'iso3166_2';
    if (keyMode === 'auto' && codeIdx >= 0) {
      var iso3n = 0, iso2n = 0, sampled = 0;
      for (var si = 1; si < Math.min(lines.length, 45); si++) {
        var pr = parseCSVLine(lines[si]);
        if (codeIdx >= pr.length) continue;
        var rawk = (pr[codeIdx] || '').trim();
        if (!rawk) continue;
        sampled++;
        if (/^[A-Za-z]{2}-[A-Za-z0-9]{1,4}$/.test(rawk)) iso2n++;
        else if (/^[A-Za-z]{3}$/.test(rawk)) iso3n++;
      }
      if (sampled && iso2n / sampled >= 0.45) keyMode = 'iso3166_2';
      else if (sampled && iso3n / sampled >= 0.45) keyMode = 'iso3';
      else keyMode = 'name';
    }

    var byYear = {};
    var years = {};
    var singleMap = {};

    for (var li = 1; li < lines.length; li++) {
      var parts = parseCSVLine(lines[li]);
      if (parts.length <= Math.max(codeIdx, valueIdx)) continue;
      var code = (parts[codeIdx] || '').trim();
      if (!code) continue;
      if (keyMode === 'iso3166_2' || keyMode === 'iso_3166_2') {
        if (!/^[A-Za-z]{2}-[A-Za-z0-9]{1,4}$/.test(code)) continue;
        code = code.toUpperCase();
      } else if (keyMode === 'iso3') {
        if (!/^[A-Za-z]{3}$/.test(code)) continue;
        code = code.toUpperCase();
      } else {
        code = code.replace(/\s+/g, ' ').trim();
        if (code.length < 2) continue;
      }
      var val = Number(String((parts[valueIdx] || '')).replace(/,/g, ''));
      if (!isFinite(val)) continue;

      if (yearIdx >= 0) {
        var year = (parts[yearIdx] || '').trim();
        if (!year) continue;
        if (!byYear[year]) byYear[year] = {};
        if (byYear[year][code] == null) byYear[year][code] = val;
        years[year] = true;
      } else {
        singleMap[code] = val;
      }
    }

    if (yearIdx >= 0) {
      var yearList = Object.keys(years).sort();
      if (!yearList.length) throw new Error('No valid rows for this region key type and numeric values');
      return { byYear: byYear, years: yearList };
    }
    if (Object.keys(singleMap).length === 0) throw new Error('No valid region rows in CSV for this key type');
    return { byYear: { latest: singleMap }, years: ['latest'] };
  }

  function fetchGenericCsvChoropleth(csvUrl, hints) {
    return dmFetch(csvUrl).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' loading CSV');
      return r.text();
    }).then(function(text) {
      var parsed = parseGenericIso3Csv(text, hints || {});
      return { byYear: parsed.byYear, years: parsed.years, url: csvUrl };
    });
  }


  function wireFindDataListFilters(root) {
    if (!root) return;
    root.querySelectorAll('input.dm-finddata-filter').forEach(function(inp) {
      var tid = inp.getAttribute('data-dm-filter-target');
      if (!tid) return;
      inp.addEventListener('input', function() {
        var list = document.getElementById(tid);
        if (!list) return;
        var q = (inp.value || '').toLowerCase().trim();
        list.querySelectorAll('.dm-finddata-item').forEach(function(el) {
          var hay = (el.getAttribute('data-dm-filter-text') || '').toLowerCase();
          el.style.display = !q || hay.indexOf(q) >= 0 ? '' : 'none';
        });
      });
    });
  }

  function renderFindDataResults(results) {
    var container = document.getElementById('dm-finddata-results');
    if (!container) return;
    container.innerHTML = '';
    var md = getMapData() || {};
    var metrics = (md && md.metrics) ? md.metrics : {};
    var metricKeys = Object.keys(metrics);
    function finishFilters() { wireFindDataListFilters(container); }

    if (metricKeys.length) {
      var detY = document.createElement('details');
      detY.className = 'dm-finddata-collapsible dm-finddata-section';
      detY.open = true;
      var sumY = document.createElement('summary');
      sumY.className = 'dm-finddata-summary';
      sumY.innerHTML = '<span class="dm-finddata-summary-title">Your datasets on the map</span>' +
        '<span class="dm-finddata-count">(' + metricKeys.length + ')</span>';
      detY.appendChild(sumY);
      var bodyY = document.createElement('div');
      bodyY.className = 'dm-finddata-collapsible-body';
      var filterY = document.createElement('input');
      filterY.type = 'search';
      filterY.className = 'dm-finddata-filter dm-ai-key-input';
      filterY.placeholder = 'Filter by name...';
      filterY.setAttribute('data-dm-filter-target', 'dm-finddata-yours-list');
      bodyY.appendChild(filterY);
      var scrollY = document.createElement('div');
      scrollY.className = 'dm-finddata-scroll';
      scrollY.id = 'dm-finddata-yours-list';
      metricKeys.forEach(function(mk) {
        var m = metrics[mk] || {};
        var pinned = (m && m.pinned) ? '⭐ ' : '';
        var display = m.display || mk;
        var row = document.createElement('div');
        row.className = 'dm-ds-card dm-finddata-item';
        row.setAttribute('data-dm-filter-text', (mk + ' ' + display).toLowerCase());
        row.innerHTML =
          '<div class="dm-ds-card-header">' +
          '<span class="dm-ds-card-title">' + (pinned ? '⭐ ' : '') + escapeHtml(display) + '</span>' +
          '</div>' +
          '<div class="dm-ds-card-actions" style="margin-top:6px;">' +
          '<button type="button" class="dm-ds-add-btn" data-dm-activate-metric="' + escapeHtml(mk) + '">Use</button>' +
          '<button type="button" class="dm-ds-preview-btn" data-dm-pin-metric="' + escapeHtml(mk) + '">' + (m.pinned ? 'Unpin' : 'Pin') + '</button>' +
          '<button type="button" class="dm-ds-preview-btn" data-dm-rename-metric="' + escapeHtml(mk) + '">Rename</button>' +
          '<button type="button" class="dm-ds-preview-btn" data-dm-export-metric="' + escapeHtml(mk) + '">Export CSV</button>' +
          '<button type="button" class="dm-ds-layer-btn" data-dm-remove-metric="' + escapeHtml(mk) + '">Remove</button>' +
          '</div>';
        scrollY.appendChild(row);
      });
      bodyY.appendChild(scrollY);
      detY.appendChild(bodyY);
      container.appendChild(detY);
    }
    if (!results || !results.length) {
      if (!metricKeys.length) {
        var empty = document.createElement('p');
        empty.style.cssText = 'color:var(--muted);font-size:12px;margin-top:8px;';
        empty.textContent = 'Nothing matched that exact phrase. Try shorter words (for example: poverty, electricity, India states, US states). If downloads fail, open the map with python3 data-mapping.py --serve and use http://127.0.0.1:8080 so World Bank and Our World in Data can load.';
        container.appendChild(empty);
      } else {
        var hint = document.createElement('p');
        hint.style.cssText = 'color:var(--muted);font-size:12px;margin-top:8px;';
        hint.textContent = 'No new matches for this query. Use the list above, try broader keywords, or paste your own table under Your regions (CSV).';
        container.appendChild(hint);
      }
      finishFilters();
      return;
    }
    var sugDetails = document.createElement('details');
    sugDetails.className = 'dm-finddata-collapsible dm-finddata-section';
    sugDetails.open = true;
    var sumS = document.createElement('summary');
    sumS.className = 'dm-finddata-summary';
    sumS.innerHTML = '<span class="dm-finddata-summary-title">Suggested datasets</span>' +
      '<span class="dm-finddata-count">(' + results.length + ')</span>';
    sugDetails.appendChild(sumS);
    var bodyS = document.createElement('div');
    bodyS.className = 'dm-finddata-collapsible-body';
    var filterS = document.createElement('input');
    filterS.type = 'search';
    filterS.className = 'dm-finddata-filter dm-ai-key-input';
    filterS.placeholder = 'Filter suggestions...';
    filterS.setAttribute('data-dm-filter-target', 'dm-finddata-suggested-list');
    bodyS.appendChild(filterS);
    var scrollS = document.createElement('div');
    scrollS.className = 'dm-finddata-scroll';
    scrollS.id = 'dm-finddata-suggested-list';
    results.forEach(function(ds) {
      var div = document.createElement('div');
      div.className = 'dm-ds-card dm-finddata-item';
      div.setAttribute('data-dm-filter-text',
        [String(ds.id || ''), String(ds.title || ''), String(ds.description || ''), String(ds.provider || ''), String(ds.granularity || '')].join(' ').toLowerCase());
      var sourceUrl = '#';
      if (ds.kind === 'generic_csv' && ds.csv_url) sourceUrl = ds.csv_url;
      else if (ds.provider === 'Our World in Data' && ds.owid_slug) sourceUrl = owidDatasetLink(ds.owid_slug);
      else if (ds.indicator) sourceUrl = worldBankIndicatorLink(ds.indicator);
      var yearUi = (ds.kind === 'points_csv')
        ? ''
        : ('<div style="display:flex;gap:6px;align-items:center;margin:6px 0 0 0;">' +
        '<label style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;">Year</label>' +
        '<select data-dm-year-for="' + escapeHtml(ds.id) + '" class="dm-ds-year-sel">' +
        '<option value="latest" selected>Latest</option>' +
        '</select>' +
        '</div>');
      var gran = ds.granularity ? ds.granularity : '';
      var _dsIsAi = ds.id && (ds.id.indexOf('ai-') === 0);
      var providerBadgeClass = _dsIsAi ? 'dm-ds-badge-ai' : (ds.provider === 'World Bank') ? 'dm-ds-badge-wb' : (ds.provider === 'Our World in Data') ? 'dm-ds-badge-owid' : 'dm-ds-badge-csv';
      var providerLabel = escapeHtml(ds.provider || 'Dataset');
      div.innerHTML =
        '<div class="dm-ds-card-header">' +
        '<span class="dm-ds-card-title">' + escapeHtml(ds.title) + (gran ? ' <span style="font-size:10px;color:var(--muted);font-weight:400;">' + gran + '</span>' : '') + '</span>' +
        '<span class="dm-ds-card-badge ' + providerBadgeClass + '">' + providerLabel + '</span>' +
        '</div>' +
        '<div class="dm-ds-card-desc">' + escapeHtml(ds.description || '') + '</div>' +
        '<div class="dm-ds-card-actions">' +
        '<button type="button" class="dm-ds-preview-btn" data-dm-preview-ds="' + escapeHtml(ds.id) + '">Preview</button>' +
        '<button type="button" class="dm-ds-add-btn" data-dm-add-ds="' + escapeHtml(ds.id) + '">Add to map</button>' +
        '<button type="button" class="dm-ds-layer-btn" data-dm-add-multi="' + escapeHtml(ds.id) + '">+ Layer</button>' +
        (sourceUrl !== '#' ? '<a href="' + escapeHtml(sourceUrl) + '" target="_blank" rel="noopener noreferrer" style="color:var(--muted);font-size:10px;text-decoration:underline;margin-left:auto;">Source</a>' : '') +
        '</div>' +
        yearUi +
        '<div class="dm-ds-card-status" id="dm-ds-status-' + escapeHtml(ds.id) + '"></div>';
      scrollS.appendChild(div);
    });
    bodyS.appendChild(scrollS);
    sugDetails.appendChild(bodyS);
    container.appendChild(sugDetails);
    finishFilters();
  }

  function getFindDataCache() { return getStoredJson('finddata-cache', {}); }
  function setFindDataCache(cache) { setStored('finddata-cache', JSON.stringify(cache || {})); }

  function _dmCachePayloadUsable(c) {
    if (!c || typeof c !== 'object') return false;
    if (c.byYear && typeof c.byYear === 'object') {
      var yk = Object.keys(c.byYear);
      for (var yi = 0; yi < yk.length; yi++) {
        var m = c.byYear[yk[yi]];
        if (m && typeof m === 'object' && Object.keys(m).length > 0) return true;
      }
    }
    if (c.valuesByKey && typeof c.valuesByKey === 'object' && Object.keys(c.valuesByKey).length > 0) return true;
    return false;
  }

  function _getSelectedYearFor(dsId) {
    var sel = document.querySelector('select[data-dm-year-for="' + dsId + '"]');
    if (!sel) return 'latest';
    return sel.value || 'latest';
  }

  function _populateYearSelect(dsId, years) {
    var sel = document.querySelector('select[data-dm-year-for="' + dsId + '"]');
    if (!sel) return;
    var cur = sel.value || 'latest';
    // keep existing options (latest) and add years
    var has = {};
    for (var i = 0; i < sel.options.length; i++) has[sel.options[i].value] = true;
    (years || []).forEach(function(y) {
      if (!has[y]) {
        var opt = document.createElement('option');
        opt.value = y;
        opt.textContent = y;
        sel.appendChild(opt);
      }
    });
    if (has[cur]) sel.value = cur;
  }

  function _pickValuesForYear(payload, year) {
    if (!payload) return {};
    if (year === 'latest') {
      if (payload.years && payload.years.length && payload.byYear) {
        for (var i = 0; i < payload.years.length; i++) {
          var y = payload.years[i];
          var m = payload.byYear[y];
          if (m && Object.keys(m).length) return m;
        }
      }
      return payload.valuesByKey || {};
    }
    if (payload.byYear && payload.byYear[year]) return payload.byYear[year];
    return payload.valuesByKey || {};
  }

  function _dmDefaultGeoProfiles() {
    return {
      countries: {
        label: 'World  -  countries (Natural Earth, ISO 3166-1 alpha-3)',
        url: 'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson',
        key_on: 'feature.properties.ISO_A3',
        name_property: 'ADMIN',
        center: [20, 0],
        zoom: 2
      },
      admin1_global: {
        label: 'World  -  states / provinces (Natural Earth, ISO 3166-2)',
        url: 'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces.geojson',
        key_on: 'feature.properties.iso_3166_2',
        name_property: 'name',
        center: [20, 0],
        zoom: 2
      }
    };
  }

  function updateAdmin1FocusUiVisibility() {
    var wrap = document.getElementById('dm-admin1-focus-wrap');
    var selGeo = document.getElementById('dm-geo-profile');
    if (!wrap) return;
    var show = selGeo && selGeo.value === 'admin1_global';
    wrap.style.display = show ? 'block' : 'none';
  }

  function populateAdmin1CountryFilterSelect() {
    var sel = document.getElementById('dm-admin1-focus');
    if (!sel) return;
    var full = window._dmGeoJsonDataFull;
    sel.innerHTML = '<option value="">All countries (world map)</option>';
    if (!full || !full.features || !full.features.length) return;
    var byIso = {};
    for (var fi = 0; fi < full.features.length; fi++) {
      var f = full.features[fi];
      var p = f && f.properties ? f.properties : {};
      var a2 = String(p.iso_a2 || '').trim().toUpperCase();
      if (!a2 || a2 === '-99' || a2.length !== 2) continue;
      var adm = String(p.admin || p.admin_name || p.adm0_a3 || a2).trim();
      if (!byIso[a2]) byIso[a2] = adm;
    }
    var keys = Object.keys(byIso).sort(function(a, b) {
      return byIso[a].localeCompare(byIso[b], undefined, { sensitivity: 'base' });
    });
    for (var ki = 0; ki < keys.length; ki++) {
      var iso = keys[ki];
      var opt = document.createElement('option');
      opt.value = iso;
      opt.textContent = byIso[iso] + ' (' + iso + ')';
      sel.appendChild(opt);
    }
    var cur = getStored('dm-admin1-focus-iso2') || '';
    if (cur && sel.querySelector('option[value="' + cur + '"]')) sel.value = cur;
    else sel.value = '';
  }

  function applyAdmin1CountryFilter(iso2, skipFitBounds) {
    var full = window._dmGeoJsonDataFull;
    if (!full || !full.features) return;
    iso2 = String(iso2 || '').trim().toUpperCase();
    var feats;
    if (!iso2) {
      feats = full.features.slice();
    } else {
      feats = full.features.filter(function(f) {
        var p = f && f.properties ? f.properties : {};
        return String(p.iso_a2 || '').trim().toUpperCase() === iso2;
      });
    }
    window._dmGeoJsonData = { type: full.type || 'FeatureCollection', features: feats };
    window._dmAdmin1FilterIso2 = iso2 || '';
    try { setStored('dm-admin1-focus-iso2', iso2 || ''); } catch (eS) {}
    rebuildAdmin1NameLookup(window._dmGeoJsonData);
    var map = getMap();
    if (map && window._dmMetricLayers) {
      Object.keys(window._dmMetricLayers).forEach(function(k) {
        var lyr = window._dmMetricLayers[k];
        try { if (lyr && map.hasLayer(lyr)) map.removeLayer(lyr); } catch (eL) {}
      });
      window._dmMetricLayers = {};
    }
    if (typeof buildLayerControls === 'function') buildLayerControls();
    var mdLive = getMapData();
    if (map && mdLive && typeof applyColorScale === 'function') applyColorScale(map, mdLive, getActiveColormap());
    if (map && !skipFitBounds) {
      if (iso2 && feats.length && typeof L !== 'undefined' && L.geoJSON) {
        try {
          var tmp = L.geoJSON({ type: 'FeatureCollection', features: feats });
          var b = tmp.getBounds();
          if (b && b.isValid && b.isValid()) map.fitBounds(b, { padding: [20, 20], maxZoom: 9 });
        } catch (eB) {}
      } else if (!iso2) {
        try { map.setView([20, 0], 2); } catch (eV) {}
      }
    }
  }

  /** If most keys look like XX-YYY (ISO 3166-2), suggest zooming that country. */
  function inferIso2FromIso3166_2Keys(keys) {
    if (!keys || !keys.length) return '';
    var counts = {};
    var tot = 0;
    for (var i = 0; i < keys.length; i++) {
      var k = String(keys[i] || '').trim().toUpperCase();
      var m = k.match(/^([A-Z]{2})-[A-Z0-9]{1,4}$/);
      if (!m) continue;
      tot++;
      counts[m[1]] = (counts[m[1]] || 0) + 1;
    }
    if (tot < 3) return '';
    var best = '', bestn = 0;
    for (var c in counts) {
      if (counts[c] > bestn) { bestn = counts[c]; best = c; }
    }
    if (best && bestn / tot >= 0.82) return best;
    return '';
  }

  function maybeAutoFocusAdmin1Country(valuesByKey) {
    var geoSel = document.getElementById('dm-geo-profile');
    if (!geoSel || geoSel.value !== 'admin1_global') return;
    var keys = Object.keys(valuesByKey || {});
    var iso2 = inferIso2FromIso3166_2Keys(keys);
    if (!iso2) return;
    var sel = document.getElementById('dm-admin1-focus');
    if (!sel || !sel.querySelector('option[value="' + iso2 + '"]')) return;
    sel.value = iso2;
    try { setStored('dm-admin1-focus-iso2', iso2); } catch (e2) {}
    applyAdmin1CountryFilter(iso2, false);
    showToast('Zoomed to ' + iso2 + ' (from region codes in your data)');
  }

  function ensureGeoProfile(profileId) {
    var id = mapLegacyGeoProfileId(profileId || 'countries') || 'countries';
    var md = getMapData() || {};
    var profiles = md.geo_profiles || _dmDefaultGeoProfiles();
    var prof = profiles[id];
    if (!prof || !prof.url) prof = profiles.countries;
    if (!prof || !prof.url) return Promise.reject(new Error('No geography profiles'));
    var canReuse = window._dmActiveGeoProfileId === id && window._dmGeoJsonData && md.key_on === prof.key_on;
    if (canReuse && id === 'admin1_global' && !window._dmGeoJsonDataFull) canReuse = false;
    if (canReuse) {
      if (id === 'admin1_global') rebuildAdmin1NameLookup(window._dmGeoJsonData);
      else { window._dmAdmin1UniqueNormNameToIso = null; window._dmAdmin1NormNameToCodes = null; }
      if (typeof updateAdmin1FocusUiVisibility === 'function') updateAdmin1FocusUiVisibility();
      return Promise.resolve(getMapData() || md);
    }
    return dmFetch(prof.url).then(function(r) {
      if (!r.ok) throw new Error('Geography HTTP ' + r.status);
      return r.json();
    }).then(function(geo) {
      var md2 = getMapData() || {};
      if (!md2.geo_profiles) md2.geo_profiles = profiles;
      window._dmActiveGeoProfileId = id;
      if (id === 'admin1_global') {
        window._dmGeoJsonDataFull = { type: geo.type || 'FeatureCollection', features: (geo.features || []).slice() };
        window._dmGeoJsonData = { type: window._dmGeoJsonDataFull.type, features: window._dmGeoJsonDataFull.features.slice() };
        rebuildAdmin1NameLookup(window._dmGeoJsonData);
        try {
          populateAdmin1CountryFilterSelect();
          updateAdmin1FocusUiVisibility();
          applyAdmin1CountryFilter(getStored('dm-admin1-focus-iso2') || '', false);
        } catch (eAd) {}
      } else {
        window._dmGeoJsonDataFull = null;
        window._dmGeoJsonData = geo;
        window._dmAdmin1UniqueNormNameToIso = null;
        window._dmAdmin1NormNameToCodes = null;
      }
      md2.geo_json = null;
      md2.key_on = prof.key_on;
      md2.name_property = prof.name_property || 'name';
      md2.geo_level = id;
      md2.type = md2.type || 'choropleth';
      setStored('live-map-data', stringifyLiveMapData(md2));
      if (typeof updateAdmin1FocusUiVisibility === 'function') updateAdmin1FocusUiVisibility();
      var map = getMap();
      if (map && prof.center && prof.zoom != null && map.setView && id !== 'admin1_global') {
        var c = prof.center;
        map.setView([c[0], c[1]], prof.zoom);
      }
      return md2;
    });
  }

  /** Guess countries vs admin-1 from value keys (ISO3 vs ISO 3166-2). */
  function inferGeoProfileFromKeys(keys) {
    if (!keys || !keys.length) return null;
    var iso2 = 0, iso3 = 0, n = 0;
    for (var i = 0; i < keys.length; i++) {
      var k = String(keys[i] || '').trim();
      if (!k) continue;
      n++;
      if (/^[A-Za-z]{2}-[A-Za-z0-9]{1,4}$/.test(k)) iso2++;
      else if (/^[A-Za-z]{3}$/.test(k)) iso3++;
    }
    if (!n) return null;
    if (iso2 / n >= 0.45) return 'admin1_global';
    if (iso3 / n >= 0.45) return 'countries';
    return null;
  }

  function populateRegionYearSelect(parsed) {
    var sel = document.getElementById('dm-region-year-select');
    if (!sel || !parsed || !parsed.years) return;
    sel.innerHTML = '';
    parsed.years.forEach(function(y) {
      var m = parsed.byYear[y];
      if (!m || !Object.keys(m).length) return;
      var opt = document.createElement('option');
      opt.value = y;
      opt.textContent = y === 'latest' ? 'Latest (no year column)' : String(y);
      sel.appendChild(opt);
    });
    if (sel.options.length > 1) {
      sel.style.display = 'block';
      sel.selectedIndex = 0;
    } else {
      sel.style.display = 'none';
    }
  }

  function createChoroplethLayer(map, metricKey, valuesByKey, vmin, vmax, cmap, keyOn, nameProp) {
    if (!window._dmGeoJsonData || typeof L === 'undefined' || !map) return null;
    if (!window._dmMetricLayers) window._dmMetricLayers = {};
    var prev = window._dmMetricLayers[metricKey];
    if (prev) {
      try { if (map.hasLayer(prev)) map.removeLayer(prev); } catch (eRm) {}
    }
    var layer = L.geoJSON(window._dmGeoJsonData, {
      style: function(feature) {
        var val = resolveValue({ feature: feature }, valuesByKey, keyOn);
        var color = (val != null) ? valueToColor(Number(val), vmin, vmax, cmap) : '#dddddd';
        return { fillColor: color, color: 'rgba(255,255,255,0.8)', weight: 1, fillOpacity: 0.78 };
      },
      onEachFeature: function(feature, lyr) {
        var props = feature.properties || {};
        var nm = props[nameProp] || props.name || props.NAME_1 || props.NAME || feature.id || '';
        var val = resolveValue({ feature: feature }, valuesByKey, keyOn);
        var tip = nm ? String(nm) : String(feature.id || '');
        if (val != null) tip += ': ' + fmtNum(val);
        lyr.bindTooltip(tip, { className: 'foliumtooltip' });
      }
    });
    window._dmMetricLayers[metricKey] = layer;
    return layer;
  }

  function runChoroplethPreviewAdd(mdBase, payload, year, ds, dsId, addToMap, keepExisting, scope, statusEl) {
    var valuesByKey = _pickValuesForYear(payload, year) || {};
    var md = getMapData() || mdBase;
    var keyOn = md.key_on || 'feature.id';
    var nameProp = md.name_property || 'name';

    if (!window._dmGeoJsonData) {
      if (statusEl) statusEl.textContent = 'Could not load boundaries for this geography.';
      return;
    }

    if (String(keyOn).indexOf('iso_3166_2') >= 0 && window._dmAdmin1UniqueNormNameToIso) {
      valuesByKey = enrichAdmin1ValuesByNameAliases(valuesByKey);
    }

    var keys = Object.keys(valuesByKey);
    var vals = keys.map(function(k){ return Number(valuesByKey[k]); }).filter(function(v){ return isFinite(v); });
    if (!vals.length) {
      if (statusEl) statusEl.textContent = 'No numeric values for this year/selection.';
      showToast('No values to map for this selection');
      return;
    }
    if (!keepExisting) {
      try { if (typeof maybeAutoFocusAdmin1Country === 'function') maybeAutoFocusAdmin1Country(valuesByKey); } catch (eAf) {}
    }
    var vmin = Math.min.apply(null, vals);
    var vmax = Math.max.apply(null, vals);
    var coverage = keys.length;

    if (!window._dmMetricLayers) window._dmMetricLayers = {};
    var map = getMap();
    if (!map || typeof L === 'undefined') { if (statusEl) statusEl.textContent = 'Map not ready.'; return; }

    var metricKey = keepExisting ? ('layer_' + dsId + '_' + (year === 'latest' ? 'latest' : year)) : ('ds_' + dsId);
    var cmap = ds.defaultColormap || 'YlOrRd';
    var layer = createChoroplethLayer(map, metricKey, valuesByKey, vmin, vmax, cmap, keyOn, nameProp);
    if (!layer) {
      if (statusEl) statusEl.textContent = 'Could not create map layer.';
      showToast('Could not draw on map (check geography + data)');
      return;
    }

    if (!keepExisting) {
      Object.keys(window._dmMetricLayers).forEach(function(k) {
        if (k === metricKey) return;
        var other = window._dmMetricLayers[k];
        if (other && map.hasLayer(other)) {
          try { map.removeLayer(other); } catch (eHide) {}
        }
      });
    }
    if (!map.hasLayer(layer)) layer.addTo(map);

    md.type = md.type || 'choropleth';
    var mdColorPreview = { type: 'choropleth', values: valuesByKey, min: vmin, max: vmax, key_on: keyOn, name_property: nameProp };
    if (map) applyColorScale(map, mdColorPreview, cmap || getActiveColormap());

    if (statusEl) statusEl.textContent =
      (addToMap ? 'Adding to map...' : 'Preview loaded.') +
      ' Coverage: ' + coverage + ' locations.' +
      (year ? ' Year: ' + year + '.' : '');
    showToast(addToMap ? 'Adding dataset...' : 'Preview loaded');

    if (!addToMap) return;

    if (!md.metrics) md.metrics = {};
    md.metrics[metricKey] = {
      values: valuesByKey,
      min: vmin,
      max: vmax,
      display: ds.title,
      colormap: cmap,
      source: payload.meta,
      year: year,
      scope: scope,
      pinned: false
    };
    if (!keepExisting) {
      md.values = valuesByKey;
      md.min = vmin;
      md.max = vmax;
      md.active_metric = metricKey;
    }
    setStored('live-map-data', stringifyLiveMapData(md));

    if (typeof refreshInsightsFromMap === 'function') refreshInsightsFromMap();

    var metricSelect = document.getElementById('dm-metric-select');
    if (metricSelect) {
      var opt = document.createElement('option');
      opt.value = metricKey;
      opt.textContent = ds.title + (year && year !== 'latest' ? (' (' + year + ')') : '');
      metricSelect.appendChild(opt);
      if (!keepExisting) metricSelect.value = metricKey;
    }

    var mdLive = getMapData() || md;
    if (map) applyColorScale(map, mdLive, getActiveColormap());
    if (typeof buildLayerControls === 'function') buildLayerControls();

    if (statusEl) statusEl.textContent = 'Added. ' + (keepExisting ? 'Layer added.' : 'This is now your active dataset.') + ' You can use Insights + Ask.';
    showToast('Dataset added to map');
  }

  function importUserRegionChoropleth(addToMap, keepExisting) {
    var ta = document.getElementById('dm-region-csv');
    var statusEl = document.getElementById('dm-region-import-status');
    var csv = (ta && ta.value || '').trim();
    if (!csv) { showToast('Paste or upload a CSV with a region column and numeric values'); return; }
    var hints = regionCsvHints();
    var parsed;
    try {
      parsed = parseGenericIso3Csv(csv, hints);
    } catch (e) {
      if (statusEl) statusEl.textContent = String(e && e.message ? e.message : e);
      showToast('Could not parse CSV');
      return;
    }
    populateRegionYearSelect(parsed);
    var yearSel = document.getElementById('dm-region-year-select');
    var year = (yearSel && yearSel.style.display !== 'none' && yearSel.value) ? yearSel.value : 'latest';

    var payload = {
      byYear: parsed.byYear,
      years: parsed.years,
      meta: { source: 'Your CSV', url: '' },
      title: 'Your region data',
      display: 'Your region data'
    };
    var vk = _pickValuesForYear(payload, year) || {};
    var inferredGeo = inferGeoProfileFromKeys(Object.keys(vk));
    var km = String(hints.geo_keys || 'auto').toLowerCase().replace(/-/g, '_');
    var wantGeo = mapLegacyGeoProfileId((document.getElementById('dm-geo-profile') && document.getElementById('dm-geo-profile').value) || getStored('dm-active-geo-profile')) || 'countries';
    if (km === 'iso3166_2' || km === 'iso_3166_2') wantGeo = 'admin1_global';
    else if (km === 'iso3') wantGeo = 'countries';
    else if (km === 'name') wantGeo = 'admin1_global';
    else if (inferredGeo) wantGeo = inferredGeo;

    var geoSel = document.getElementById('dm-geo-profile');
    if (geoSel && geoSel.querySelector('option[value="' + wantGeo + '"]') && geoSel.value !== wantGeo) {
      geoSel.value = wantGeo;
      setStored('dm-active-geo-profile', wantGeo);
      showToast('Geography set to match your CSV keys');
    }

    var dsId = 'user_region_' + Date.now();
    var ds = { id: dsId, title: 'Your region data', defaultColormap: 'viridis' };
    var scope = 'all';
    if (statusEl) statusEl.textContent = 'Loading boundaries...';

    ensureGeoProfile(wantGeo).then(function(mdBase) {
      runChoroplethPreviewAdd(mdBase || {}, payload, year, ds, dsId, addToMap, keepExisting, scope, statusEl);
    }).catch(function(err) {
      if (statusEl) statusEl.textContent = 'Error: ' + (err && err.message ? err.message : err);
      showToast('Could not load geography');
    });
  }

  function previewOrAddDataset(dsId, addToMap, keepExisting) {
    var queryEl = document.getElementById('dm-finddata-query');
    var q = (queryEl && queryEl.value) ? queryEl.value : '';
    var qNorm = normalizeQuery(q);
    var ds = findDatasetDescriptorById(dsId);
    if (!ds) {
      try { rankDatasetCandidates(qNorm || ''); } catch (eRank) {}
      ds = findDatasetDescriptorById(dsId);
    }
    if (!ds) { showToast('Dataset not found. Click Search to refresh the list.'); return; }

    var statusEl = document.getElementById('dm-ds-status-' + dsId);
    if (statusEl) statusEl.textContent = 'Fetching data...';

    if (typeof L === 'undefined') {
      showToast('Map library still loading — wait a moment and try again.');
      return;
    }
    var mapEarly = getMap();
    if (!mapEarly) {
      showToast('Map still loading — wait 2 seconds and try Preview again.');
      return;
    }

    if (ds.kind === 'points_csv' && ds.csv_url) {
      dmFetch(ds.csv_url).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      }).then(function(txt) {
        if (!addToMap) {
          var ta = document.getElementById('dm-add-data-csv');
          if (ta) ta.value = txt;
          if (statusEl) statusEl.textContent = 'CSV placed in Your points  -  review columns then click Add points to map.';
          showToast('Preview: check Your points (CSV) tab');
          if (typeof openDataWorkspace === 'function') openDataWorkspace('points');
          return;
        }
        var n = addPointsFromCSVText(txt);
        if (statusEl) statusEl.textContent = n ? ('Loaded ' + n + ' point(s).') : 'No points  -  CSV needs lat, lon columns.';
        showToast(n ? ('Imported ' + n + ' point(s)') : 'No points parsed');
        if (typeof buildLayerControls === 'function') buildLayerControls();
      }).catch(function(err) {
        if (statusEl) statusEl.textContent = 'Point data did not load — tap Preview again.';
        showToast('Point data — tap Preview again');
      });
      return;
    }

    var scope = getScopeFromQuery(qNorm) || 'all';
    var year = _getSelectedYearFor(dsId);
    var cacheKey = dsId + '::' + scope;
    var geoSel = document.getElementById('dm-geo-profile');
    var geoProfileId;
    if (ds.geo_profile) {
      geoProfileId = mapLegacyGeoProfileId(ds.geo_profile) || 'countries';
    } else if (ds.indicator) {
      geoProfileId = 'countries';
    } else if (ds.provider === 'Our World in Data' && ds.owid_slug) {
      geoProfileId = ds.owid_geo_profile ? mapLegacyGeoProfileId(ds.owid_geo_profile) : 'countries';
    } else if (ds.kind === 'generic_csv') {
      var gk = String(ds.geo_keys || '').toLowerCase().replace(/-/g, '_');
      if (gk === 'iso3166_2' || gk === 'iso_3166_2') geoProfileId = 'admin1_global';
      else geoProfileId = mapLegacyGeoProfileId((geoSel && geoSel.value) || getStored('dm-active-geo-profile')) || 'countries';
    } else {
      geoProfileId = mapLegacyGeoProfileId((geoSel && geoSel.value) || getStored('dm-active-geo-profile')) || 'countries';
    }
    if (geoSel && geoSel.querySelector('option[value="' + geoProfileId + '"]') && geoSel.value !== geoProfileId) {
      geoSel.value = geoProfileId;
      setStored('dm-active-geo-profile', geoProfileId);
    }

    function buildDataP() {
      var cacheL = getFindDataCache();
      if (_dmCachePayloadUsable(cacheL[cacheKey])) {
        return Promise.resolve(cacheL[cacheKey]);
      }
      if (ds.provider === 'Our World in Data' && ds.owid_slug) {
        return fetchOwidDataset(ds.owid_slug).then(function(res) {
          var payload = { byYear: res.byYear, years: res.years, meta: { source: 'Our World in Data', slug: ds.owid_slug, url: res.url }, title: ds.title, display: ds.title };
          cacheL[cacheKey] = payload;
          setFindDataCache(cacheL);
          _populateYearSelect(dsId, res.years.slice().reverse());
          return payload;
        });
      }
      if (ds.kind === 'generic_csv' && ds.csv_url) {
        return fetchGenericCsvChoropleth(ds.csv_url, {
          iso_column: ds.iso_column,
          year_column: ds.year_column,
          value_column: ds.value_column,
          geo_keys: ds.geo_keys
        }).then(function(res) {
          var payload = {
            byYear: res.byYear,
            years: res.years,
            meta: { source: ds.provider || 'External CSV', url: ds.csv_url },
            title: ds.title,
            display: ds.title
          };
          cacheL[cacheKey] = payload;
          setFindDataCache(cacheL);
          _populateYearSelect(dsId, res.years.slice().reverse());
          return payload;
        });
      }
      if (ds.kind === 'embedded_csv' && ds.embedded_csv) {
        return Promise.resolve().then(function() {
          var parsed = parseGenericIso3Csv(ds.embedded_csv, {
            geo_keys: ds.demo_geo_keys || ds.geo_keys || 'auto',
            iso_column: ds.demo_iso_column || ds.iso_column || '',
            year_column: ds.year_column || '',
            value_column: ds.value_column || ''
          });
          var payload = {
            byYear: parsed.byYear,
            years: parsed.years,
            meta: { source: ds.provider || 'Embedded', url: '' },
            title: ds.title,
            display: ds.title
          };
          cacheL[cacheKey] = payload;
          setFindDataCache(cacheL);
          _populateYearSelect(dsId, (parsed.years || []).slice().reverse());
          return payload;
        });
      }
      if (ds.indicator) {
        return fetchWorldBankValues(scope, ds.indicator).then(function(res) {
          var payload = { byYear: res.byYear, years: res.years, meta: res.meta, title: ds.title, display: ds.title };
          cacheL[cacheKey] = payload;
          setFindDataCache(cacheL);
          _populateYearSelect(dsId, res.years);
          return payload;
        });
      }
      return Promise.reject(new Error('Unsupported dataset type'));
    }

    function handleChoroError(err, isFinal) {
      var m = (err && err.message) ? String(err.message) : String(err || '');
      var geoProb = /Geography HTTP|No geography profiles/i.test(m);
      var unsupported = /Unsupported dataset/i.test(m);
      if (statusEl) {
        if (unsupported) statusEl.textContent = 'This suggestion cannot load on the map.';
        else if (geoProb) statusEl.textContent = isFinal ? 'Map outline did not load — tap Preview again.' : 'Loading outline… retrying.';
        else statusEl.textContent = isFinal ? 'Tap Preview or Add again in a moment.' : 'Loading data… retrying.';
      }
      if (isFinal) {
        if (unsupported) showToast('This dataset type is not supported here');
        else showToast(geoProb ? 'Map outline — tap Preview again' : 'Dataset — tap Preview again');
      }
    }

    function loadChoroPair() {
      return Promise.all([ensureGeoProfile(geoProfileId), buildDataP()]).then(function(arr) {
        runChoroplethPreviewAdd(arr[0] || {}, arr[1], year, ds, dsId, addToMap, keepExisting, scope, statusEl);
      });
    }

    if (statusEl) statusEl.textContent = 'Loading data...';
    loadChoroPair().catch(function(e1) {
      if (e1 && e1.message && e1.message.indexOf('Unsupported dataset') >= 0) {
        handleChoroError(e1, true);
        return Promise.reject(e1);
      }
      /* Auto-retry once after clearing cache */
      var cache2 = getFindDataCache();
      delete cache2[cacheKey];
      setFindDataCache(cache2);
      if (statusEl) statusEl.textContent = 'Loading... (retrying)';
      return new Promise(function(res) {
        setTimeout(function() { res(loadChoroPair()); }, 1200);
      });
    }).catch(function(e2) {
      if (e2 && e2.message && e2.message.indexOf('Unsupported dataset') >= 0) {
        if (statusEl) statusEl.textContent = 'This dataset type is not supported.';
        return;
      }
      /* Auto-retry one more time */
      if (statusEl) statusEl.textContent = 'Almost there...';
      setTimeout(function() {
        loadChoroPair().catch(function(e3) {
          if (statusEl) statusEl.textContent = 'Could not load. Try a different dataset.';
          showToast('Dataset failed to load — try another one');
        });
      }, 2000);
    });
  }

  function _embeddedHasChoroValues(emb) {
    if (!emb || emb.type !== 'choropleth') return false;
    if (emb.values && typeof emb.values === 'object' && Object.keys(emb.values).length > 0) return true;
    if (emb.metrics && typeof emb.metrics === 'object' && Object.keys(emb.metrics).length > 0) return true;
    return false;
  }

  function getMapData() {
    var emb = getEmbeddedMapData();
    try {
      var live = getStored('live-map-data');
      if (live) {
        var parsed = _safeJSONParse(live, null);
        if (parsed) {
          if (emb && emb.geo_profiles && !parsed.geo_profiles) parsed.geo_profiles = emb.geo_profiles;
          if (emb && emb.geo_level && !parsed.geo_level) parsed.geo_level = emb.geo_level;
          if (emb && emb.key_on && !parsed.key_on) parsed.key_on = emb.key_on;
          if (emb && emb.name_property && !parsed.name_property) parsed.name_property = emb.name_property;
          if (_embeddedHasChoroValues(emb) && !_embeddedHasChoroValues(parsed)) {
            var merged = {};
            for (var ek in emb) {
              if (Object.prototype.hasOwnProperty.call(emb, ek)) merged[ek] = emb[ek];
            }
            for (var pk in parsed) {
              if (!Object.prototype.hasOwnProperty.call(parsed, pk)) continue;
              if (pk === 'values' || pk === 'metrics' || pk === 'min' || pk === 'max' || pk === 'active_metric') continue;
              merged[pk] = parsed[pk];
            }
            return merged;
          }
          return parsed;
        }
      }
    } catch (e) {}
    return emb;
  }

  /** After CLI import: load boundaries if needed and paint embedded values on the map. */
  function hydrateEmbeddedChoroplethOnLoad(md) {
    if (!md || md.type !== 'choropleth' || md.start_blank === true) return Promise.resolve();
    if (!_embeddedHasChoroValues(md)) return Promise.resolve();
    var map = getMap();
    if (!map) return Promise.resolve();
    var geoLevel = md.geo_level || (md.key_on && String(md.key_on).indexOf('iso_3166_2') >= 0 ? 'admin1_global' : 'countries');
    var geoSel = document.getElementById('dm-geo-profile');
    if (geoSel && geoSel.querySelector('option[value="' + geoLevel + '"]')) {
      geoSel.value = geoLevel;
      setStored('dm-active-geo-profile', geoLevel);
    }
    return ensureGeoProfile(geoLevel).then(function(md2) {
      var mdUse = getMapData() || md2 || md;
      if (mdUse.metrics && Object.keys(mdUse.metrics).length) {
        if (typeof buildLayerControls === 'function') buildLayerControls();
      } else if (mdUse.values && Object.keys(mdUse.values).length) {
        var mk = 'cli_embedded';
        if (!mdUse.metrics) mdUse.metrics = {};
        mdUse.metrics[mk] = {
          values: mdUse.values,
          min: mdUse.min,
          max: mdUse.max,
          display: mdUse.active_metric || defaultTitle() || 'Imported data',
          colormap: mdUse.colormap || 'YlOrRd'
        };
        mdUse.active_metric = mk;
        setStored('live-map-data', stringifyLiveMapData(mdUse));
        if (typeof buildLayerControls === 'function') buildLayerControls();
      }
      if (map && mdUse) applyColorScale(map, mdUse, getActiveColormap());
      if (typeof refreshInsightsFromMap === 'function') refreshInsightsFromMap();
    }).catch(function(err) {
      console.warn('hydrateEmbeddedChoroplethOnLoad', err);
      if (map && md) applyColorScale(map, md, getActiveColormap());
    });
  }

  function _metricDisplayNorm(s) {
    return String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
  }

  /** Wire server-rendered Folium metric groups into _dmMetricLayers so toggles work. */
  function linkFoliumLayersToMetrics(m, md) {
    if (!m || !md || md.type !== 'choropleth' || !md.metrics) return false;
    if (!window._dmMetricLayers) window._dmMetricLayers = {};
    var byDisplay = {};
    Object.keys(md.metrics).forEach(function(mk) {
      var d = md.metrics[mk];
      var label = (d && d.display) ? d.display : mk;
      byDisplay[_metricDisplayNorm(label)] = mk;
      byDisplay[_metricDisplayNorm(mk)] = mk;
    });
    var linked = false;
    m.eachLayer(function(layer) {
      if (!layer || !layer.eachLayer) return;
      var lname = (layer.options && layer.options.name) || layer._name || '';
      var norm = _metricDisplayNorm(lname);
      var mk = byDisplay[norm];
      if (!mk) return;
      var hasGeo = false;
      layer.eachLayer(function(sub) { if (sub.feature) hasGeo = true; });
      if (!hasGeo) return;
      window._dmMetricLayers[mk] = layer;
      linked = true;
    });
    return linked;
  }

  /** Preload boundaries, hydrate CLI data, populate Discover, then build layer UI. */
  function bootMapDataLayers(attempt) {
    attempt = attempt || 0;
    var map = getMap();
    if (!map) {
      if (attempt < 50) {
        setTimeout(function() { bootMapDataLayers(attempt + 1); }, 200);
      } else {
        console.warn('bootMapDataLayers: Leaflet map not found after retries');
      }
      return;
    }
    try {
      var initial = rankDatasetCandidates('');
      if (typeof renderFindDataResults === 'function' && initial && initial.length) {
        renderFindDataResults(initial);
      }
    } catch (eCat) { console.warn('bootMapDataLayers catalog', eCat); }

    var md = getMapData();
    if (!md) {
      buildLayerControls();
      return;
    }

    var geoLevel = md.geo_level || (md.key_on && String(md.key_on).indexOf('iso_3166_2') >= 0 ? 'admin1_global' : 'countries');
    var needGeo = md.type === 'choropleth' && (
      md.start_blank === true ||
      _embeddedHasChoroValues(md) ||
      (md.metrics && Object.keys(md.metrics).length > 0)
    );
    var geoP = (needGeo && !window._dmGeoJsonData && !(md.geo_json))
      ? ensureGeoProfile(geoLevel)
      : Promise.resolve(md);

    geoP.then(function() {
      if (_embeddedHasChoroValues(md)) return hydrateEmbeddedChoroplethOnLoad(md);
      return Promise.resolve();
    }).then(function() {
      buildLayerControls();
      var m = getMap();
      var d = getMapData();
      if (m && d && _embeddedHasChoroValues(d)) {
        applyColorScale(m, d, getActiveColormap());
        if (typeof refreshInsightsFromMap === 'function') refreshInsightsFromMap();
      }
      /* Re-build layers again after a delay to catch late GeoJSON loads */
      setTimeout(function() { buildLayerControls(); }, 3000);
    }).catch(function(err) {
      console.warn('bootMapDataLayers', err);
      buildLayerControls();
      var m2 = getMap();
      if (m2 && md) applyColorScale(m2, md, getActiveColormap());
      /* Retry GeoJSON download after failure */
      setTimeout(function() {
        ensureGeoProfile(geoLevel).then(function() {
          buildLayerControls();
        }).catch(function() {});
      }, 2000);
    });
  }

  function _safeJSONParse(str, fallback) {
    try { return JSON.parse(str); } catch (e) { return fallback; }
  }

  function getStoredJson(key, fallback) {
    var raw = getStored(key);
    if (!raw) return fallback;
    return _safeJSONParse(raw, fallback);
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined) return 'n/a';
    var n = Number(v);
    if (!isFinite(n)) return 'n/a';
    if (digits != null) return n.toFixed(digits);
    /* smart default: trim trailing zeros, cap at 2 decimal places */
    if (Math.abs(n) >= 1000) return n.toLocaleString('en-US', { maximumFractionDigits: 1 });
    if (Number.isInteger(n)) return String(n);
    return parseFloat(n.toFixed(2)).toString();
  }

  function pickRecordValue(rec) {
    if (!rec || typeof rec !== 'object') return null;
    for (var k in rec) {
      if (k !== 'location') return rec[k];
    }
    return null;
  }

  function formatLocationValueList(list, maxItems) {
    maxItems = maxItems || 3;
    if (!list || !list.length) return 'n/a';
    return list.slice(0, maxItems).map(function(x) {
      var v = pickRecordValue(x);
      var loc = x && x.location != null ? String(x.location) : '';
      if (v === null || v === undefined || isNaN(Number(v))) return loc;
      return loc + ' (' + fmtNum(v, 2) + ')';
    }).filter(Boolean).join(', ');
  }

  function buildObservationsFromInsights(insights) {
    if (!insights || !insights.metrics) {
      return 'No data loaded yet. Click a Quick Start topic or search for a dataset to add data to the map.';
    }
    var metrics = insights.metrics || {};
    var keyMsgs = insights.key_messages || [];
    var correlations = insights.correlations || [];
    
    /* Build a rich narrative automatically */
    var mkList = Object.keys(metrics);
    if (mkList.length > 0) {
      var parts = [];
      parts.push('📊 ANALYSIS OF ' + mkList.length + ' INDICATORS\n');
      mkList.slice(0, 8).forEach(function(mk) {
        var d = metrics[mk];
        if (!d) return;
        var nm = d.metric_display || mk;
        parts.push('▸ ' + nm + ': ' + d.n + ' countries, mean ' + fmtNum(d.mean) + ', median ' + fmtNum(d.median) + 
          ' (range ' + fmtNum(d.min) + ' – ' + fmtNum(d.max) + ')');
        if (d.disparity_ratio && d.disparity_ratio > 2) {
          parts.push('  ⚠️ High disparity: ' + fmtNum(d.disparity_ratio, 1) + 'x between highest and lowest');
        }
        if (d.top5 && d.top5.length) {
          parts.push('  Top: ' + d.top5.slice(0, 3).map(function(x){ return x.location + ' (' + fmtNum(x.value) + ')'; }).join(', '));
        }
        if (d.bottom5 && d.bottom5.length) {
          parts.push('  Bottom: ' + d.bottom5.slice(0, 3).map(function(x){ return x.location + ' (' + fmtNum(x.value) + ')'; }).join(', '));
        }
        parts.push('');
      });
      if (correlations.length) {
        parts.push('🔗 CORRELATIONS FOUND:');
        correlations.slice(0, 6).forEach(function(c) {
          parts.push('• ' + c.metric1_display + ' ↔ ' + c.metric2_display + ': ' + c.strength + ' ' + c.direction + ' (r=' + fmtNum(c.correlation, 2) + ')');
        });
      }
      return parts.join('\n');
    }

    var lines = [];
    lines.push('Observations from your data:');

    var mkList = Object.keys(metrics);
    mkList.sort(function(a, b) {
      var da = metrics[a] || {};
      var db = metrics[b] || {};
      var sa = (da.disparity_ratio || 0) + ((da.bottom_quartile_pct || 0) / 100);
      var sb = (db.disparity_ratio || 0) + ((db.bottom_quartile_pct || 0) / 100);
      return sb - sa;
    });

    var maxMetrics = 5;
    if (!mkList.length) {
      lines.push('- No metrics available.');
      return lines.join('\\n');
    }

    mkList.slice(0, maxMetrics).forEach(function(mk) {
      var d = metrics[mk] || {};
      var metricName = d.metric_display || mk;
      lines.push('');
      lines.push('* ' + metricName);
      lines.push('  - Range: ' + fmtNum(d.min) + ' to ' + fmtNum(d.max) + ' across ' + (d.n != null ? d.n : 'n/a') + ' locations.');
      if (d.disparity_ratio) lines.push('  - Disparity (max/min): ' + fmtNum(d.disparity_ratio, 1) + 'x');
      if (d.q1 != null && d.q3 != null) lines.push('  - Middle 50% (Q1-Q3): ' + fmtNum(d.q1) + ' - ' + fmtNum(d.q3));
      if (d.below_median_pct != null) lines.push('  - Below median: ' + fmtNum(d.below_median_pct, 1) + '% of locations.');

      var top = formatLocationValueList(d.top5 || [], 3);
      var bottom = formatLocationValueList(d.bottom5 || [], 3);
      if (top && top !== 'n/a') lines.push('  - Highest: ' + top);
      if (bottom && bottom !== 'n/a') lines.push('  - Lowest: ' + bottom);

      var priority = d.priority_list || [];
      if (priority.length) {
        var pLocs = priority.slice(0, 8).map(function(x) { return x.location; }).filter(Boolean).join(', ');
        lines.push('  - Priority areas: ' + pLocs);
      }
    });

    if (keyMsgs.length) {
      lines.push('');
      lines.push('Key takeaway: ' + keyMsgs[0]);
    }
    if (correlations && correlations.length) {
      lines.push('');
      lines.push('Cross-metric signals:');
      correlations.slice(0, 5).forEach(function(c) {
        var r = (c && c.correlation != null) ? Number(c.correlation) : null;
        var rTxt = (r === null || isNaN(r)) ? 'r=n/a' : ('r~' + fmtNum(r, 2));
        lines.push('- ' + c.metric1_display + ' vs ' + c.metric2_display + ': ' + (c.strength || 'notable') + ' ' + (c.direction || 'positive') + ' (' + rTxt + ', n=' + (c.n != null ? c.n : 'n/a') + ').');
      });
    }
    return lines.join('\\n');
  }

  function renderChatHistory(answerEl, history) {
    if (!answerEl) return;
    history = history || [];
    var chunks = [];
    history.forEach(function(m) {
      var role = m && m.role === 'user' ? 'You' : 'AI';
      var content = (m && m.content != null) ? String(m.content) : '';
      chunks.push(role + ':\\n' + content);
    });
    answerEl.textContent = chunks.join('\\n\\n');
    answerEl.classList.add('dm-visible');
  }

  function getChatHistory() { return getStoredJson('ai-chat-history', []); }
  function setChatHistory(history) { setStored('ai-chat-history', JSON.stringify(history || [])); }

  function clearChatHistory() {
    setChatHistory([]);
    return [];
  }

  function ruleBasedAnswer(question) {
    var q = (question || '').toLowerCase();
    var insights = getInsightsData();
    if (!insights || !insights.metrics) {
      return 'No insights data available for this map. Add data and run the script with insights enabled.';
    }
    var metrics = insights.metrics || {};
    var keyMsgs = insights.key_messages || [];
    var observations = buildObservationsFromInsights(insights);

    if (q.indexOf('key message') >= 0 || q.indexOf('summary') >= 0) {
      if (keyMsgs.length) return 'Key message: ' + keyMsgs.join(' ');
      return observations;
    }
    if (q.indexOf('support') >= 0 || q.indexOf('priority') >= 0 || q.indexOf('focus') >= 0) {
      var out = [];
      for (var m in metrics) {
        var d = metrics[m];
        var pl = d.priority_list || [];
        if (pl.length) out.push(d.metric_display + ': ' + pl.slice(0, 8).map(function(x) { return x.location; }).join(', '));
      }
      if (out.length) return out.join('\\n');
      return observations;
    }
    if (q.indexOf('range') >= 0 || q.indexOf('disparity') >= 0) {
      var out = [];
      for (var m in metrics) {
        var d = metrics[m];
        out.push(d.metric_display + ': ' + fmtNum(d.min) + ' - ' + fmtNum(d.max) + (d.disparity_ratio ? ', disparity ' + fmtNum(d.disparity_ratio, 1) + 'x' : ''));
      }
      return out.join('\\n');
    }
    if (q.indexOf('average') >= 0 || q.indexOf('mean') >= 0 || q.indexOf('median') >= 0 || q.indexOf('statistics') >= 0 || q.indexOf('std') >= 0) {
      var lines = [];
      for (var mk in metrics) {
        var d = metrics[mk];
        lines.push('- ' + (d.metric_display || mk) + ': n=' + (d.n != null ? d.n : '?') +
          ', mean ' + fmtNum(d.mean) + ', median ' + fmtNum(d.median) +
          (d.std != null ? ', stdev ' + fmtNum(d.std) : ''));
      }
      return lines.length ? lines.join('\\n') : observations;
    }
    if (q.indexOf('correlat') >= 0 || q.indexOf('relationship between') >= 0 || (q.indexOf('relationship') >= 0 && q.indexOf('metric') >= 0)) {
      var cors2 = insights.correlations || [];
      if (cors2.length) {
        return cors2.slice(0, 8).map(function(c) {
          var r = (c && c.correlation != null) ? Number(c.correlation) : 0;
          return '- ' + (c.metric1_display || c.metric1) + ' vs ' + (c.metric2_display || c.metric2) + ': ' +
            (c.strength || 'notable') + ' ' + (c.direction || 'positive') + ' (r=' + fmtNum(r, 2) + ', n=' + (c.n != null ? c.n : '') + ')';
        }).join('\\n');
      }
      return 'No clear pairwise correlations yet on overlapping regions. Add two layers with the same geography keys (e.g. two World Bank maps) and pick the same year.\\n\\n' + observations;
    }
    if (q.indexOf('grant') >= 0 || q.indexOf('proposal') >= 0) {
      var base = keyMsgs.length ? keyMsgs.join(' ') : 'Use the key takeaways in the sidebar for your narrative.';
      // Give a short "what to say" scaffold.
      return base + '\\n\\nSuggested focus areas: ' + (function(){
        var all = [];
        for (var m in metrics) {
          var d = metrics[m];
          var pl = d.priority_list || [];
          if (pl.length) {
            pl.slice(0, 3).forEach(function(x){ if (x && x.location) all.push(x.location); });
          }
        }
        return all.length ? all.slice(0, 10).join(', ') : 'See priority areas in the Key takeaways.';
      })();
    }
    // Default: show the grounded observations so the user still gets value.
    return observations;
  }
  var COLORMAPS = {
    YlOrRd: ['#ffffcc','#ffeda0','#fed976','#feb24c','#fd8d3c','#fc4e2a','#e31a1c','#bd0026','#800026'],
    viridis: ['#440154','#482878','#3e4a89','#31688e','#26838f','#1f9e89','#35b779','#6dcd59','#b4de2c','#fde725'],
    plasma: ['#0d0887','#47039f','#7001a8','#9a179b','#bc3754','#db5c68','#f89441','#fdc328','#f0f921'],
    plasma_r: ['#f0f921','#fdc328','#f89441','#db5c68','#bc3754','#9a179b','#7001a8','#47039f','#0d0887'],
    Blues: ['#f7fbff','#deebf7','#c6dbef','#9ecae1','#6baed6','#4292c6','#2171b5','#08519c','#08306b'],
    Greens: ['#f7fcf5','#e5f5e0','#c7e9c0','#a1d99b','#74c476','#41ab5d','#238b45','#006d2c','#00441b'],
    RdYlGn: ['#a50026','#d73027','#f46d43','#fdae61','#ffffbf','#d9ef8b','#a6d96a','#66bd63','#1a9850','#006837'],
    PuOr: ['#7f3b08','#b35806','#e08214','#fdb863','#f7f7f7','#d8daeb','#b2abd2','#8073ac','#542788','#2d004b'],
    coolwarm: ['#3b4cc0','#5977e3','#8ba3f0','#bcc8f5','#e8e8e8','#f5c4b8','#ea967b','#d75448','#b40426']
  };
  function hexToRgb(h) {
    var r = parseInt(h.slice(1,3),16), g = parseInt(h.slice(3,5),16), b = parseInt(h.slice(5,7),16);
    return [r,g,b];
  }
  function rgbToHex(r,g,b) {
    return '#' + [r,g,b].map(function(x){ var h = Math.round(x).toString(16); return h.length===1?'0'+h:h; }).join('');
  }
  function interpolateColor(t, hexList) {
    if (t <= 0) return hexList[0];
    if (t >= 1) return hexList[hexList.length-1];
    var i = t * (hexList.length - 1), j = Math.floor(i), k = Math.min(j+1, hexList.length-1), u = i - j;
    var a = hexToRgb(hexList[j]), b = hexToRgb(hexList[k]);
    return rgbToHex(a[0]+u*(b[0]-a[0]), a[1]+u*(b[1]-a[1]), a[2]+u*(b[2]-a[2]));
  }
  function valueToColor(value, vmin, vmax, colormapName) {
    var list = COLORMAPS[colormapName] || COLORMAPS.YlOrRd;
    var t = (vmax > vmin) ? (value - vmin) / (vmax - vmin) : 0.5;
    return interpolateColor(t, list);
  }
  /* ---- SVG fill patterns for layers ---- */
  var FILL_PATTERNS = {
    solid: null,
    stripes: function(color) {
      var id = 'dm-pat-stripes-' + color.replace('#','');
      if (!document.getElementById(id)) {
        var ns = 'http://www.w3.org/2000/svg';
        var svg = document.querySelector('svg.leaflet-zoom-animated') || document.createElementNS(ns, 'svg');
        var defs = svg.querySelector('defs') || svg.insertBefore(document.createElementNS(ns, 'defs'), svg.firstChild);
        var pat = document.createElementNS(ns, 'pattern');
        pat.setAttribute('id', id); pat.setAttribute('width', '8'); pat.setAttribute('height', '8');
        pat.setAttribute('patternUnits', 'userSpaceOnUse'); pat.setAttribute('patternTransform', 'rotate(45)');
        var r = document.createElementNS(ns, 'rect');
        r.setAttribute('width', '4'); r.setAttribute('height', '8'); r.setAttribute('fill', color);
        pat.appendChild(r); defs.appendChild(pat);
      }
      return 'url(#' + id + ')';
    },
    dots: function(color) {
      var id = 'dm-pat-dots-' + color.replace('#','');
      if (!document.getElementById(id)) {
        var ns = 'http://www.w3.org/2000/svg';
        var svg = document.querySelector('svg.leaflet-zoom-animated') || document.createElementNS(ns, 'svg');
        var defs = svg.querySelector('defs') || svg.insertBefore(document.createElementNS(ns, 'defs'), svg.firstChild);
        var pat = document.createElementNS(ns, 'pattern');
        pat.setAttribute('id', id); pat.setAttribute('width', '10'); pat.setAttribute('height', '10');
        pat.setAttribute('patternUnits', 'userSpaceOnUse');
        var c = document.createElementNS(ns, 'circle');
        c.setAttribute('cx', '5'); c.setAttribute('cy', '5'); c.setAttribute('r', '2.5'); c.setAttribute('fill', color);
        pat.appendChild(c); defs.appendChild(pat);
      }
      return 'url(#' + id + ')';
    },
    checkers: function(color) {
      var id = 'dm-pat-check-' + color.replace('#','');
      if (!document.getElementById(id)) {
        var ns = 'http://www.w3.org/2000/svg';
        var svg = document.querySelector('svg.leaflet-zoom-animated') || document.createElementNS(ns, 'svg');
        var defs = svg.querySelector('defs') || svg.insertBefore(document.createElementNS(ns, 'defs'), svg.firstChild);
        var pat = document.createElementNS(ns, 'pattern');
        pat.setAttribute('id', id); pat.setAttribute('width', '10'); pat.setAttribute('height', '10');
        pat.setAttribute('patternUnits', 'userSpaceOnUse');
        var r1 = document.createElementNS(ns, 'rect');
        r1.setAttribute('width', '5'); r1.setAttribute('height', '5'); r1.setAttribute('fill', color);
        var r2 = document.createElementNS(ns, 'rect');
        r2.setAttribute('x', '5'); r2.setAttribute('y', '5'); r2.setAttribute('width', '5'); r2.setAttribute('height', '5'); r2.setAttribute('fill', color);
        pat.appendChild(r1); pat.appendChild(r2); defs.appendChild(pat);
      }
      return 'url(#' + id + ')';
    }
  };
  function normalizeKey(v) {
    if (v == null) return '';
    return String(v).toLowerCase().replace(/[^a-z0-9]/g, '');
  }
  function stripDiacritics(s) {
    try {
      return String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    } catch (e) { return String(s || ''); }
  }
  function nameNormForAdmin1Lookup(s) {
    return normalizeKey(stripDiacritics(String(s || '')));
  }
  /** Build name / alt-name  ->  ISO 3166-2 indexes from Natural Earth admin-1 (for CSV name matching). */
  function rebuildAdmin1NameLookup(geo) {
    window._dmAdmin1UniqueNormNameToIso = {};
    window._dmAdmin1NormNameToCodes = {};
    if (!geo || !geo.features) return;
    var codeSetByNk = {};
    function addName(nm, iso) {
      if (!nm || !iso) return;
      var nk = nameNormForAdmin1Lookup(nm);
      if (!nk) return;
      if (!codeSetByNk[nk]) codeSetByNk[nk] = {};
      codeSetByNk[nk][iso] = true;
    }
    geo.features.forEach(function(f) {
      var p = f.properties || {};
      var iso = String(p.iso_3166_2 || '').trim().toUpperCase();
      if (!iso || iso.indexOf('-') < 0) return;
      addName(p.name, iso);
      var alt = p.name_alt;
      if (alt) String(alt).split(/[|\\/;]+/).forEach(function(x) { addName(String(x || '').trim(), iso); });
      if (p.name_local) addName(p.name_local, iso);
    });
    for (var nk in codeSetByNk) {
      var isos = Object.keys(codeSetByNk[nk]);
      window._dmAdmin1NormNameToCodes[nk] = isos;
      if (isos.length === 1) window._dmAdmin1UniqueNormNameToIso[nk] = isos[0];
    }
  }
  /** Duplicate CSV values under canonical ISO 3166-2 keys when names uniquely map to one region. */
  function enrichAdmin1ValuesByNameAliases(valuesByKey) {
    var u = window._dmAdmin1UniqueNormNameToIso;
    if (!u || !valuesByKey) return valuesByKey;
    var out = {};
    for (var k in valuesByKey) {
      if (!Object.prototype.hasOwnProperty.call(valuesByKey, k)) continue;
      var v = valuesByKey[k];
      out[k] = v;
      var raw = String(k).trim();
      if (/^[A-Za-z]{2}-[A-Za-z0-9]{1,4}$/.test(raw)) {
        out[raw.toUpperCase()] = v;
        continue;
      }
      var iso = u[nameNormForAdmin1Lookup(raw)];
      if (iso) out[iso] = v;
    }
    return out;
  }
  function getFeatureValueByPath(feature, keyOn) {
    if (!feature || !keyOn) return null;
    var parts = String(keyOn).split('.').filter(Boolean);
    if (parts.length && parts[0] === 'feature') parts = parts.slice(1);
    var cur = feature;
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (cur && typeof cur === 'object' && Object.prototype.hasOwnProperty.call(cur, p)) cur = cur[p];
      else return null;
    }
    return cur;
  }
  function resolveValue(layer, vals, keyOn) {
    if (!layer.feature) return null;
    var customKey = getFeatureValueByPath(layer.feature, keyOn);
    if (customKey != null) {
      var v0 = vals[customKey];
      if (v0 == null) v0 = vals[String(customKey)];
      if (v0 == null) {
        var nk0 = normalizeKey(customKey);
        for (var k0 in vals) { if (normalizeKey(k0) === nk0) { v0 = vals[k0]; break; } }
      }
      if (v0 != null) return v0;
    }
    var id = layer.feature.id;
    var val = vals[id];
    if (val == null && id && id.length === 3) val = vals[id.substring(0, 2)];
    var p = layer.feature.properties || {};
    if (val == null) val = vals[p.ISO_A2];
    if (val == null && p.iso_3166_2) { var i2 = String(p.iso_3166_2).trim().toUpperCase(); val = vals[i2]; if (val == null) val = vals[String(p.iso_3166_2).trim()]; }
    if (val == null && p.code_hasc) {
      var ch = String(p.code_hasc).trim().toUpperCase().replace(/\./g, '-');
      if (vals[ch] != null) val = vals[ch];
    }
    if (val == null) val = vals[p.id];
    if (val == null && p.ISO_A3) { val = vals[p.ISO_A3]; if (val == null) val = vals[p.ISO_A3.substring(0, 2)]; }
    if (val == null && p.name) {
      val = vals[p.name];
      if (val == null && p.adm0_a3) val = vals[p.adm0_a3];
      if (val == null) { for (var k in vals) { if (k.toLowerCase() === p.name.toLowerCase()) { val = vals[k]; break; } } }
    }
    if (val == null && window._dmAdmin1UniqueNormNameToIso && (p.name || p.name_alt)) {
      var tryNames = [p.name, p.name_alt, p.name_local].filter(Boolean);
      outerNm:
      for (var ti = 0; ti < tryNames.length; ti++) {
        var tn = tryNames[ti];
        var partsFr = String(tn).split(/[|\\/;]+/);
        for (var fi = 0; fi < partsFr.length; fi++) {
          var frag = String(partsFr[fi] || '').trim();
          if (!frag) continue;
          var nk = nameNormForAdmin1Lookup(frag);
          var isoU = window._dmAdmin1UniqueNormNameToIso[nk];
          if (isoU != null) {
            if (vals[isoU] != null) val = vals[isoU];
            else if (vals[frag] != null) val = vals[frag];
            else {
              for (var vk in vals) {
                if (!Object.prototype.hasOwnProperty.call(vals, vk)) continue;
                if (nameNormForAdmin1Lookup(vk) === nk) { val = vals[vk]; break; }
              }
            }
            if (val != null) break outerNm;
          }
        }
      }
    }
    return val;
  }
  function getActiveChoroSlice(md) {
    if (!md) return { values: {}, min: 0, max: 1, key_on: 'feature.id', name_property: 'name' };
    var vals = md.values || {};
    var vmin = md.min;
    var vmax = md.max;
    if (vals && Object.keys(vals).length) {
      /* fall through to numeric guard below */
    } else {
      var am = md.active_metric;
      var mInfo = (am && md.metrics && md.metrics[am]) ? md.metrics[am] : null;
      if (!mInfo && md.metrics) {
        var keys = Object.keys(md.metrics);
        if (keys.length === 1) mInfo = md.metrics[keys[0]];
      }
      if (mInfo) {
        vals = mInfo.values || {};
        vmin = mInfo.min != null ? mInfo.min : vmin;
        vmax = mInfo.max != null ? mInfo.max : vmax;
      }
    }
    vals = vals || {};
    if (vmin == null || vmax == null || !isFinite(Number(vmin)) || !isFinite(Number(vmax)) || vmin === vmax) {
      var nums = Object.keys(vals).map(function(k) { return Number(vals[k]); }).filter(function(v) { return isFinite(v); });
      if (nums.length) {
        vmin = Math.min.apply(null, nums);
        vmax = Math.max.apply(null, nums);
      } else {
        vmin = 0;
        vmax = 1;
      }
    }
    return { values: vals, min: vmin, max: vmax, key_on: md.key_on, name_property: md.name_property };
  }

  function applyColorScale(map, mapData, colormapName) {
    if (!map || !mapData) return;
    var slice = getActiveChoroSlice(mapData);
    var vmin = slice.min, vmax = slice.max;
    var vals = slice.values;
    var keyOn = slice.key_on || mapData.key_on;
    var applied = 0;
    function styleOne(layer) {
      if (!layer.feature || !layer.setStyle) return;
      var val = resolveValue(layer, vals, keyOn);
      if (val != null) {
        layer.setStyle({ fillColor: valueToColor(val, vmin, vmax, colormapName), fillOpacity: 0.75 });
        applied++;
      }
    }
    if (mapData.type === 'choropleth') {
      map.eachLayer(function(layer) {
        if (layer.eachLayer) {
          layer.eachLayer(function(sublayer) { styleOne(sublayer); });
        } else {
          styleOne(layer);
        }
      });
    } else if (mapData.type === 'points' && mapData.points) {
      var pts = mapData.points;
      map.eachLayer(function(layer) {
        if (layer._latlng && (layer._radius != null || layer.setRadius)) {
          var ll = layer.getLatLng();
          for (var i = 0; i < pts.length; i++) {
            var p = pts[i];
            if (Math.abs(p.lat - ll.lat) < 1e-4 && Math.abs(p.lon - ll.lng) < 1e-4) {
              var v = typeof p.value === 'number' ? p.value : parseFloat(p.value);
              if (!isNaN(v)) { layer.setStyle({ fillColor: valueToColor(v, vmin, vmax, colormapName), fillOpacity: 0.8 }); applied++; }
              break;
            }
          }
        }
      });
    }
  }
  var COLOR_DIRECTIONS = {
    YlOrRd: ['Low', 'High'], viridis: ['Low', 'High'], plasma: ['Low', 'High'],
    Blues: ['Low', 'High'], Greens: ['Low', 'High'],
    RdYlGn: ['Worst', 'Best'], PuOr: ['End A', 'End B'], coolwarm: ['Cool', 'Warm']
  };
  var _dmColorReversed = false;
  function getActiveColormap() {
    var sel = document.getElementById('dm-color-scale');
    var name = (sel && sel.value) || 'YlOrRd';
    if (_dmColorReversed) {
      var rev = COLORMAPS[name].slice().reverse();
      COLORMAPS['_rev_' + name] = rev;
      return '_rev_' + name;
    }
    return name;
  }
  function updateColorDirection() {
    var el = document.getElementById('dm-color-direction');
    if (!el) return;
    var sel = document.getElementById('dm-color-scale');
    var name = (sel && sel.value) || 'YlOrRd';
    var dir = COLOR_DIRECTIONS[name] || ['Low', 'High'];
    if (_dmColorReversed) dir = [dir[1], dir[0]];
    el.innerHTML = '<span>' + dir[0] + '</span><span>&rarr;</span><span>' + dir[1] + '</span>';
  }
  function switchBasemap(name) {
    var m = getMap();
    if (!m) return;
    var urls = {
      voyager: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
      light: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
      dark: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'
    };
    m.eachLayer(function(l) { if (l._url && l._url.indexOf('basemaps.cartocdn') >= 0) m.removeLayer(l); });
    if (urls[name]) L.tileLayer(urls[name], { attribution: '&copy; CARTO', subdomains: 'abcd', maxZoom: 20 }).addTo(m);
    showToast('Basemap: ' + name);
  }
  function setNativeLayerControlHidden(hidden) {
    var controls = document.querySelectorAll('.leaflet-control-layers');
    controls.forEach(function(ctrl) {
      ctrl.style.display = hidden ? 'none' : '';
    });
    setStored('layer-control-hidden', hidden ? '1' : '0');
    var btn = document.getElementById('dm-btn-hide-layers');
    if (btn) btn.textContent = hidden ? 'Show layers' : 'Hide layers';
  }
  function toggleNativeLayerControl() {
    var ctrl = document.querySelector('.leaflet-control-layers');
    if (!ctrl) {
      showToast('No layer control found');
      return;
    }
    var hidden = ctrl.style.display === 'none';
    setNativeLayerControlHidden(!hidden);
    showToast(!hidden ? 'Layer control hidden' : 'Layer control shown');
  }
  function getFeatureLabel(feature) {
    if (!feature) return '';
    var props = feature.properties || {};
    return (
      props.name || props.NAME || props.name_en || props.admin || props.region || feature.id || ''
    );
  }
  function getDisplayPrefs() {
    var showLabels = !!((document.getElementById('dm-show-labels') || {}).checked);
    var showValues = !!((document.getElementById('dm-show-values') || {}).checked);
    return { showLabels: showLabels, showValues: showValues };
  }
  function applyTooltipSettings(map, mapData, prefs) {
    if (!map) return;
    function applyToLayer(layer) {
      if (!layer) return;
      if (layer.eachLayer && !layer.feature && !layer._latlng) {
        layer.eachLayer(function(sub) { applyToLayer(sub); });
        return;
      }
      if (typeof layer.bindTooltip !== 'function') return;
      if (!prefs.showLabels) {
        if (layer.getTooltip && layer.getTooltip()) {
          if (!layer._dmTooltipRaw) {
            var raw = layer.getTooltip().getContent();
            if (typeof raw === 'string') {
              layer._dmTooltipRaw = raw.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
            }
          }
          layer.unbindTooltip();
        }
        return;
      }

      var label = '';
      var value = null;
      if (layer.feature) {
        label = String(getFeatureLabel(layer.feature) || '').trim();
        if (prefs.showValues && mapData && mapData.values) {
          value = resolveValue(layer, mapData.values, mapData.key_on);
        }
      } else if (layer._latlng) {
        label = String((layer.options && layer.options._dmName) || '').trim();
        if (prefs.showValues && layer.options && layer.options._dmValue != null) {
          value = layer.options._dmValue;
        }
      }

      var text = label;
      if (prefs.showValues && value != null && !isNaN(Number(value))) {
        var vText = Number(value).toFixed(2);
        text = text ? (text + ': ' + vText) : vText;
      }
      if (!text && layer._dmTooltipRaw) {
        text = prefs.showValues ? layer._dmTooltipRaw : layer._dmTooltipRaw.split(':')[0].trim();
      }
      if (!text) {
        if (layer.getTooltip && layer.getTooltip()) layer.unbindTooltip();
        return;
      }
      layer.bindTooltip(text, { className: 'foliumtooltip', sticky: true });
    }
    map.eachLayer(function(layer) { applyToLayer(layer); });
  }
  function updateRangeLabels() {
    var pairs = [
      ['dm-fill-opacity', 'dm-fill-opacity-val', function(v) { return Math.round(v * 100) + '%'; }],
      ['dm-border-weight', 'dm-border-weight-val', function(v) { return v; }],
      ['dm-border-opacity', 'dm-border-opacity-val', function(v) { return Math.round(v * 100) + '%'; }],
      ['dm-font-size', 'dm-font-size-val', function(v) { return v + 'px'; }],
      ['dm-point-radius', 'dm-point-radius-val', function(v) { return Math.round(v) + 'px'; }]
    ];
    pairs.forEach(function(p) {
      var inp = document.getElementById(p[0]);
      var lbl = document.getElementById(p[1]);
      if (inp && lbl) lbl.textContent = p[2](parseFloat(inp.value));
    });
  }
  function applyDisplaySettings() {
    var m = getMap();
    if (!m) return;
    var bw = parseFloat((document.getElementById('dm-border-weight') || {}).value || 1);
    var bc = (document.getElementById('dm-border-color') || {}).value || '#ffffff';
    var bo = parseFloat((document.getElementById('dm-border-opacity') || {}).value || 0.8);
    var fo = parseFloat((document.getElementById('dm-fill-opacity') || {}).value || 0.75);
    var fs = parseInt((document.getElementById('dm-font-size') || {}).value || 13, 10);
    m.eachLayer(function(layer) {
      if (layer.setStyle && !layer._url) {
        layer.setStyle({ weight: bw, color: bc, opacity: bo, fillOpacity: fo });
      }
      if (layer.eachLayer) {
        layer.eachLayer(function(sub) {
          if (sub.setStyle) sub.setStyle({ weight: bw, color: bc, opacity: bo, fillOpacity: fo });
        });
      }
    });
    var sidebar = document.getElementById('dm-sidebar');
    if (sidebar) sidebar.style.fontSize = fs + 'px';
    applyTooltipSettings(m, getMapData(), getDisplayPrefs());
    applyPointMarkerRadius();
    updateRangeLabels();
  }
  function on(id, event, fn) {
    var el = document.getElementById(id);
    if (el) el.addEventListener(event, fn);
  }

  var _dmResearchFilter = 'all';
  function renderResearchSources() {
    var container = document.getElementById('dm-research-sources-list');
    if (!container) return;
    var list = [];
    try { list = JSON.parse(getStored('research-sources') || '[]'); } catch (e) {}
    container.innerHTML = '';
    var counts = { total: list.length };
    list.forEach(function(s) { var t = s.type || 'Article'; counts[t] = (counts[t] || 0) + 1; });
    var statsEl = document.getElementById('dm-research-stats');
    if (statsEl) {
      statsEl.innerHTML = '<span class="dm-research-stat"><strong>' + counts.total + '</strong> sources</span>';
      ['Article','Report','Dataset','Website','Book','Other'].forEach(function(t) {
        if (counts[t]) statsEl.innerHTML += '<span class="dm-research-stat"><strong>' + counts[t] + '</strong> ' + t.toLowerCase() + (counts[t] > 1 ? 's' : '') + '</span>';
      });
    }
    var shown = 0;
    list.forEach(function(s, idx) {
      var sType = s.type || 'Article';
      if (_dmResearchFilter !== 'all' && sType !== _dmResearchFilter) return;
      shown++;
      var div = document.createElement('div');
      div.className = 'dm-research-item';
      div.setAttribute('data-type', sType);
      var title = (s.title || s.url || '').trim() || 'Source ' + (idx + 1);
      var metaParts = [];
      if (s.author) metaParts.push(escapeHtml(s.author));
      if (s.year) metaParts.push(escapeHtml(s.year));
      div.innerHTML =
        '<div class="dm-research-item-header"><span class="dm-research-item-title">' + escapeHtml(title) + '</span>' +
        '<span class="dm-research-item-type">' + escapeHtml(sType) + '</span></div>' +
        (metaParts.length ? '<div class="dm-research-item-meta">' + metaParts.join(' &middot; ') + '</div>' : '') +
        (s.url ? '<div class="dm-research-item-url"><a href="' + escapeHtml(s.url) + '" target="_blank" rel="noopener">' + escapeHtml(s.url) + '</a></div>' : '') +
        (s.notes ? '<div class="dm-research-item-notes">' + escapeHtml(s.notes) + '</div>' : '') +
        '<div class="dm-research-item-actions"><button type="button" data-dm-edit-source="' + idx + '">Edit</button><button type="button" data-dm-delete-source="' + idx + '">Del</button></div>';
      container.appendChild(div);
    });
    if (list.length === 0) container.innerHTML = '<p style="color:rgba(255,255,255,0.6);font-size:12px;text-align:center;padding:16px 0;">No sources yet. Click "+ Add new source" below.</p>';
    else if (shown === 0) container.innerHTML = '<p style="color:rgba(255,255,255,0.6);font-size:12px;text-align:center;padding:16px 0;">No sources match this filter.</p>';
  }
  function renderHighlights() {
    var container = document.getElementById('dm-highlights-list');
    if (!container) return;
    var list = [];
    try { list = JSON.parse(getStored('highlights') || '[]'); } catch (e) {}
    container.innerHTML = '';
    list.forEach(function(h, idx) {
      var div = document.createElement('div');
      div.className = 'dm-highlight-card';
      div.setAttribute('data-color', h.color || 'yellow');
      div.innerHTML = '<span class="dm-highlight-tag">' + escapeHtml(h.tag || 'Finding') + '</span>' +
        escapeHtml(h.text || '') +
        '<div class="dm-highlight-actions"><button type="button" data-dm-copy-highlight="' + idx + '">Copy</button><button type="button" data-dm-delete-highlight="' + idx + '">Del</button></div>';
      container.appendChild(div);
    });
    if (list.length === 0) container.innerHTML = '<p style="color:rgba(255,255,255,0.6);font-size:12px;text-align:center;padding:16px 0;">No highlights yet. Add key findings below.</p>';
  }
  function updateNotesWordCount() {
    var ta = document.getElementById('dm-research-notes-text');
    var wc = document.getElementById('dm-notes-wordcount');
    if (!ta || !wc) return;
    var text = ta.value.trim();
    var words = text ? text.split(/\s+/).length : 0;
    wc.textContent = words + ' word' + (words !== 1 ? 's' : '');
  }
  function escapeHtml(s) {
    if (!s) return '';
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
  var _dmVisionFilter = 'all';
  function renderVisionCards() {
    var container = document.getElementById('dm-vision-cards');
    if (!container) return;
    var list = [];
    try { list = JSON.parse(getStored('vision-cards') || '[]'); } catch (e) {}
    container.innerHTML = '';
    var counts = { total: list.length, Goals: 0, Ideas: 0, 'In Progress': 0, Done: 0, Later: 0 };
    list.forEach(function(c) { var cat = (c && c.category) || 'Goals'; if (counts[cat] != null) counts[cat]++; });
    var statsEl = document.getElementById('dm-vision-stats');
    if (statsEl) {
      statsEl.innerHTML = '<span class="dm-vision-stat"><strong>' + counts.total + '</strong> total</span>' +
        '<span class="dm-vision-stat"><strong>' + (counts['In Progress'] || 0) + '</strong> in progress</span>' +
        '<span class="dm-vision-stat"><strong>' + (counts.Done || 0) + '</strong> done</span>';
    }
    list.forEach(function(c, idx) {
      var title = (typeof c === 'string') ? c : (c && c.title ? c.title : '');
      var desc = (c && c.description) ? c.description : '';
      var cat = (c && c.category) ? c.category : 'Goals';
      var pri = (c && c.priority) ? c.priority : 'medium';
      if (_dmVisionFilter !== 'all' && cat !== _dmVisionFilter) return;
      var div = document.createElement('div');
      div.className = 'dm-vision-card';
      div.setAttribute('data-category', cat);
      div.innerHTML =
        '<div class="dm-vision-card-header"><span class="dm-vision-card-title">' + escapeHtml(title) + '</span>' +
        '<span class="dm-vision-card-priority ' + pri + '">' + pri + '</span></div>' +
        (desc ? '<div class="dm-vision-card-desc">' + escapeHtml(desc) + '</div>' : '') +
        '<div class="dm-vision-card-meta">' +
        '<span class="dm-vision-card-category">' + escapeHtml(cat) + '</span>' +
        '<div class="dm-vision-card-actions">' +
        (cat !== 'Done' ? '<button type="button" data-dm-move-vision="' + idx + '">Move &rarr;</button>' : '') +
        '<button type="button" data-dm-edit-vision="' + idx + '">Edit</button>' +
        '<button type="button" data-dm-delete-vision="' + idx + '">Del</button></div></div>';
      container.appendChild(div);
    });
    if (list.length === 0) container.innerHTML = '<p style="color:rgba(255,255,255,0.6);font-size:12px;text-align:center;padding:20px 0;">No cards yet. Add your first vision card below.</p>';
    if (typeof updateActionHubHero === 'function') updateActionHubHero();
  }

  var fullscreen = false;
  function handleClick(e) {
    try {
    var el = e && e.target;
    if (el && el.nodeType !== 1) el = el.parentElement;
    if (!el || !el.closest) return;

    /* Discover Preview / Add — must run before parent [id] routing */
    var prevBtnEarly = el.closest('[data-dm-preview-ds]');
    if (prevBtnEarly) {
      var dsIdPrev = prevBtnEarly.getAttribute('data-dm-preview-ds');
      if (dsIdPrev) { previewOrAddDataset(dsIdPrev, false, false); return; }
    }
    var addBtnEarly = el.closest('[data-dm-add-ds]');
    if (addBtnEarly) {
      var dsIdAdd = addBtnEarly.getAttribute('data-dm-add-ds');
      if (dsIdAdd) { previewOrAddDataset(dsIdAdd, true, false); return; }
    }
    var addMultiEarly = el.closest('[data-dm-add-multi]');
    if (addMultiEarly) {
      var dsIdMulti = addMultiEarly.getAttribute('data-dm-add-multi');
      if (dsIdMulti) { previewOrAddDataset(dsIdMulti, true, true); return; }
    }

    /* Quick-search buttons (handled via id-based delegation below) */
    var actStarter = el.closest && el.closest('.dm-action-starter');
    if (actStarter && actStarter.getAttribute('data-dm-starter')) {
      if (appendActionStep(actStarter.getAttribute('data-dm-starter'))) showToast('Step added');
      return;
    }
    var id = (el.id) || (el.closest && el.closest('button[id]') && el.closest('button[id]').id) || (el.closest('[id]') && el.closest('[id]').id);
    if (!id) {
      var delSource = el.closest('[data-dm-delete-source]');
      if (delSource) {
        var idx = parseInt(delSource.getAttribute('data-dm-delete-source'), 10);
        if (!isNaN(idx)) {
          var list = JSON.parse(getStored('research-sources') || '[]');
          list.splice(idx, 1);
          setStored('research-sources', JSON.stringify(list));
          renderResearchSources();
          showToast('Source removed');
        }
        return;
      }
      var editSource = el.closest('[data-dm-edit-source]');
      if (editSource) {
        var idx = parseInt(editSource.getAttribute('data-dm-edit-source'), 10);
        if (!isNaN(idx)) {
          var list = JSON.parse(getStored('research-sources') || '[]');
          var s = list[idx];
          if (s) {
            var urlEl = document.getElementById('dm-research-url');
            var titleEl = document.getElementById('dm-research-title');
            var authorEl = document.getElementById('dm-research-author');
            var yearEl = document.getElementById('dm-research-year');
            var typeEl = document.getElementById('dm-research-type');
            var notesEl = document.getElementById('dm-research-source-notes');
            if (urlEl) urlEl.value = s.url || '';
            if (titleEl) titleEl.value = s.title || '';
            if (authorEl) authorEl.value = s.author || '';
            if (yearEl) yearEl.value = s.year || '';
            if (typeEl) typeEl.value = s.type || 'Article';
            if (notesEl) notesEl.value = s.notes || '';
            window._dmSourceEditIndex = idx;
            var details = document.querySelector('.dm-source-form-toggle');
            if (details) details.open = true;
            showToast('Edit source then click Add source');
          }
        }
        return;
      }
      var rFilter = el.closest('.dm-research-filter');
      if (rFilter) {
        _dmResearchFilter = rFilter.getAttribute('data-rfilter') || 'all';
        document.querySelectorAll('.dm-research-filter').forEach(function(b) { b.classList.remove('active'); });
        rFilter.classList.add('active');
        renderResearchSources();
        return;
      }
      var delHighlight = el.closest('[data-dm-delete-highlight]');
      if (delHighlight) {
        var idx = parseInt(delHighlight.getAttribute('data-dm-delete-highlight'), 10);
        if (!isNaN(idx)) {
          var list = JSON.parse(getStored('highlights') || '[]');
          list.splice(idx, 1);
          setStored('highlights', JSON.stringify(list));
          renderHighlights();
          showToast('Highlight removed');
        }
        return;
      }
      var copyHighlight = el.closest('[data-dm-copy-highlight]');
      if (copyHighlight) {
        var idx = parseInt(copyHighlight.getAttribute('data-dm-copy-highlight'), 10);
        var list = JSON.parse(getStored('highlights') || '[]');
        if (list[idx] && navigator.clipboard) {
          navigator.clipboard.writeText(list[idx].text).then(function() { showToast('Highlight copied'); });
        }
        return;
      }
      var basemapCard = el.closest('.dm-basemap-card');
      if (basemapCard) {
        var bm = basemapCard.getAttribute('data-basemap');
        switchBasemap(bm);
        document.querySelectorAll('.dm-basemap-card').forEach(function(c) { c.classList.remove('active'); });
        basemapCard.classList.add('active');
        return;
      }
      var delVision = el.closest('[data-dm-delete-vision]');
      if (delVision) {
        var idx = parseInt(delVision.getAttribute('data-dm-delete-vision'), 10);
        if (!isNaN(idx)) {
          var list = JSON.parse(getStored('vision-cards') || '[]');
          list.splice(idx, 1);
          setStored('vision-cards', JSON.stringify(list));
          renderVisionCards();
          showToast('Card removed');
        }
        return;
      }
      var moveVision = el.closest('[data-dm-move-vision]');
      if (moveVision) {
        var idx = parseInt(moveVision.getAttribute('data-dm-move-vision'), 10);
        if (!isNaN(idx)) {
          var list = JSON.parse(getStored('vision-cards') || '[]');
          var c = list[idx];
          if (c) {
            var flow = ['Goals', 'Ideas', 'In Progress', 'Done', 'Later'];
            var ci = flow.indexOf(c.category || 'Goals');
            c.category = flow[(ci + 1) % flow.length];
            list[idx] = c;
            setStored('vision-cards', JSON.stringify(list));
            renderVisionCards();
            showToast('Moved to ' + c.category);
          }
        }
        return;
      }
      var filterBtn = el.closest('.dm-vision-filter');
      if (filterBtn) {
        _dmVisionFilter = filterBtn.getAttribute('data-filter') || 'all';
        document.querySelectorAll('.dm-vision-filter').forEach(function(b) { b.classList.remove('active'); });
        filterBtn.classList.add('active');
        renderVisionCards();
        return;
      }
      var editVision = el.closest('[data-dm-edit-vision]');
      if (editVision) {
        var idx = parseInt(editVision.getAttribute('data-dm-edit-vision'), 10);
        if (!isNaN(idx)) {
          var list = JSON.parse(getStored('vision-cards') || '[]');
          var c = list[idx];
          if (c) {
            var title = (typeof c === 'string') ? c : (c.title || '');
            var desc = (c && c.description) ? c.description : '';
            var cat = (c && c.category) ? c.category : 'Goals';
            var pri = (c && c.priority) ? c.priority : 'medium';
            var inp = document.getElementById('dm-vision-input');
            var descEl = document.getElementById('dm-vision-desc');
            var catEl = document.getElementById('dm-vision-category');
            var priEl = document.getElementById('dm-vision-priority');
            if (inp) inp.value = title;
            if (descEl) descEl.value = desc;
            if (catEl) catEl.value = cat;
            if (priEl) priEl.value = pri;
            window._dmVisionEditIndex = idx;
            showToast('Edit card then click Add card to save');
          }
        }
        return;
      }
      return;
    }
    if (id && id.indexOf('dm-qs-') === 0) {
      var qsBtn = document.getElementById(id);
      if (!qsBtn) return;
      var qqsv = qsBtn.getAttribute('data-q');
      if (!qqsv) return;
      /* Map quick-start IDs to embedded dataset IDs for instant add */
      var qsMap = {
        'dm-qs-life': 'embed-life-expectancy-world',
        'dm-qs-child': 'embed-child-mortality-world',
        'dm-qs-gdp': 'embed-gdp-per-capita-world',
        'dm-qs-india': 'in-states-multi',
        'dm-qs-us': 'us-states-life-expectancy'
      };
      var directDsId = qsMap[id];
      if (directDsId) {
        showToast('Loading ' + qqsv + '...');
        previewOrAddDataset(directDsId, true, true);
        return;
      }
      /* For others, run search */
      var si2 = document.getElementById('dm-finddata-query');
      if (si2) si2.value = qqsv;
      window._dmAiExtraDatasets = [];
      var qNormQs = normalizeQuery(qqsv);
      var results = rankDatasetCandidates(' ' + qNormQs + ' ');
      renderFindDataResults(results);
      var sc2 = document.querySelector('#dm-sidebar .dm-sidebar-content');
      if (sc2) sc2.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }
        if (id === 'dm-sidebar-toggle') {
      var sidebar = document.getElementById('dm-sidebar');
      var btn = document.getElementById('dm-sidebar-toggle');
      var iconSpan = btn ? btn.querySelector('.dm-toggle-icon') : null;
      var labelSpan = btn ? btn.querySelector('.dm-toggle-label') : null;
      if (sidebar) {
        sidebar.classList.toggle('dm-sidebar-closed');
        if (iconSpan) iconSpan.textContent = sidebar.classList.contains('dm-sidebar-closed') ? '&#9776;' : '&#10005;';
        if (labelSpan) labelSpan.textContent = sidebar.classList.contains('dm-sidebar-closed') ? 'Open panel' : 'Close';
      }
      return;
    }
    if (id === 'dm-welcome-start') {
      var nameInput = document.getElementById('dm-welcome-map-name');
      var goalSelect = document.getElementById('dm-welcome-goal');
      var skipCheck = document.getElementById('dm-welcome-skip');
      var welcomeModal = document.getElementById('dm-welcome-modal');
      var title = (nameInput && nameInput.value.trim()) ? nameInput.value.trim() : defaultTitle();
      var goal = (goalSelect && goalSelect.value) ? goalSelect.value : 'Just exploring';
      setStored('map-title', title);
      setStored('map-goal', goal);
      if (skipCheck && skipCheck.checked) setStored('welcome-done', '1');
      if (welcomeModal) welcomeModal.style.display = 'none';
      var titleEl = document.getElementById('dm-map-title');
      if (titleEl) titleEl.textContent = title;
      var goalEl = document.getElementById('dm-your-goal');
      if (goalEl) goalEl.innerHTML = '<strong>' + goal + '</strong>';
      if (typeof updateActionHubHero === 'function') updateActionHubHero();
      return;
    }
    if (id === 'dm-btn-about') {
      var aboutModal = document.getElementById('dm-about-modal');
      if (aboutModal) aboutModal.style.display = 'flex';
      return;
    }
    if (id === 'dm-about-close') {
      var aboutModal = document.getElementById('dm-about-modal');
      if (aboutModal) aboutModal.style.display = 'none';
      return;
    }
    if (id === 'dm-btn-save-reflection') {
      var reflectionEl = document.getElementById('dm-reflection');
      if (reflectionEl) {
        setStored('reflection', reflectionEl.value);
        var b = document.getElementById('dm-btn-save-reflection');
        if (b) { var orig = b.textContent; b.textContent = 'Saved!'; setTimeout(function(){ b.textContent = orig; }, 1500); }
      }
      return;
    }
    if (id === 'dm-btn-copy-key-message') {
      var msgEl = document.querySelector('.dm-key-message-text');
      var insights = getInsightsData();
      var text = (msgEl && msgEl.textContent && msgEl.textContent.trim()) || (insights && insights.key_messages && insights.key_messages[0]) || '';
      if (!text) { showToast('No key message available'); return; }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() { showToast('Key message copied'); });
      }
      return;
    }
    if (id === 'dm-btn-share-impact') {
      var t = getStored('map-title') || defaultTitle();
      var g = getStored('map-goal') || '';
      var link = window.location.href;
      var text = 'I am using Data Mapper to explore: ' + t + (g ? ' (Goal: ' + g + ').' : '.') + ' View the map: ' + link;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() {
          var b = document.getElementById('dm-btn-share-impact');
          if (b) { var orig = b.textContent; b.textContent = 'Copied!'; setTimeout(function(){ b.textContent = orig; }, 2500); }
        });
      }
      return;
    }
    if (id === 'dm-btn-export') {
      var container = getMapContainer();
      var el = container ? (container.querySelector('.leaflet-container') || container) : document.querySelector('.folium-map');
      if (el && window.html2canvas) {
        html2canvas(el, {
          useCORS: true,
          allowTaint: true,
          scale: Math.min(2.5, (window.devicePixelRatio || 1) * 1.5),
          logging: false,
          backgroundColor: '#aad3df',
          imageTimeout: 0,
          removeContainer: false
        }).then(function(canvas) {
          var a = document.createElement('a');
          a.download = 'data-map-export.png';
          a.href = canvas.toDataURL('image/png');
          a.click();
          showToast('PNG saved');
        }).catch(function() {
          showToast('PNG failed  -  try Save PDF or your browser screenshot');
          window.print();
        });
      } else {
        showToast('PNG unavailable  -  use Save PDF or Print');
        window.print();
      }
      return;
    }
    if (id === 'dm-btn-export-pdf') {
      showToast('Print dialog: choose Destination  ->  Save as PDF, then Save');
      setTimeout(function() { window.print(); }, 400);
      return;
    }
    if (id === 'dm-btn-fullscreen') {
      var area = document.getElementById('dm-map-area');
      var container = getMapContainer();
      var target = area || container;
      if (!fullscreen && target && target.requestFullscreen) {
        target.requestFullscreen(); fullscreen = true;
        var b = document.getElementById('dm-btn-fullscreen'); if (b) b.innerHTML = 'Exit fullscreen';
      } else if (document.exitFullscreen) {
        document.exitFullscreen(); fullscreen = false;
        var b = document.getElementById('dm-btn-fullscreen'); if (b) b.innerHTML = 'Fullscreen';
      }
      return;
    }
    if (id === 'dm-btn-copy-link') {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        var url = window.location.href;
        navigator.clipboard.writeText(url).then(function() {
          showToast('Link copied to clipboard');
          var b = document.getElementById('dm-btn-copy-link');
          if (b) { var orig = b.textContent; b.textContent = 'Copied!'; setTimeout(function(){ b.textContent = orig; }, 2000); }
        });
      }
      return;
    }
    if (id === 'dm-btn-download-csv') {
      var mapData = getMapData();
      if (!mapData) { showToast('No map data to download'); return; }
      var rows = [];
      if (mapData.type === 'choropleth') {
        var vals = null;
        var active = mapData.active_metric;
        if (active && mapData.metrics && mapData.metrics[active] && mapData.metrics[active].values) {
          vals = mapData.metrics[active].values;
        } else if (mapData.values && typeof mapData.values === 'object') {
          vals = mapData.values;
        } else if (mapData.metrics && typeof mapData.metrics === 'object') {
          var mk0 = Object.keys(mapData.metrics)[0];
          if (mk0 && mapData.metrics[mk0] && mapData.metrics[mk0].values) vals = mapData.metrics[mk0].values;
        }
        if (vals && typeof vals === 'object') {
          rows.push('location,value');
          for (var loc in vals) {
            if (!Object.prototype.hasOwnProperty.call(vals, loc)) continue;
            rows.push(loc + ',' + vals[loc]);
          }
        }
      } else if (mapData.type === 'points' && mapData.points && mapData.points.length) {
        rows.push('lat,lon,name,value');
        mapData.points.forEach(function(p) { rows.push([p.lat, p.lon, (p.name || ''), (p.value != null ? p.value : '')].join(',')); });
      }
      if (!rows.length) { showToast('No data to export  -  add a layer or pick an active dataset'); return; }
      var blob = new Blob([rows.join('\\n')], { type: 'text/csv;charset=utf-8' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'data-map-export.csv';
      a.click();
      URL.revokeObjectURL(a.href);
      showToast('CSV downloaded');
      return;
    }
    if (id === 'dm-btn-print') {
      window.print();
      return;
    }
    if (id === 'dm-btn-locate') {
      var map = getMap();
      if (!map) return;
      if (!navigator.geolocation) { alert('Geolocation is not supported.'); return; }
      navigator.geolocation.getCurrentPosition(
        function(pos) {
          map.setView([pos.coords.latitude, pos.coords.longitude], Math.max(map.getZoom(), 10));
          if (typeof L !== 'undefined') {
            var m = L.circleMarker([pos.coords.latitude, pos.coords.longitude], { radius: 8, color: '#667eea', fillColor: '#667eea', fillOpacity: 0.9 }).addTo(map);
            setTimeout(function() { map.removeLayer(m); }, 3000);
          }
        },
        function() { alert('Could not get your location.'); }
      );
      return;
    }
    if (id === 'dm-about-modal' && e.target.id === 'dm-about-modal') {
      e.target.style.display = 'none';
      return;
    }
    if (id === 'dm-btn-data-tab') {
      openDataWorkspace('discover');
      showToast('Open the Data tab to add or find data');
      return;
    }
    if (id === 'dm-btn-add-data') {
      var addDataText = document.getElementById('dm-add-data-csv');
      var added = addPointsFromCSVText(addDataText ? addDataText.value : '');
      if (added > 0) {
        var btn = document.getElementById('dm-btn-add-data');
        if (btn) { btn.textContent = 'Added ' + added + ' point(s). Add more?'; setTimeout(function(){ btn.textContent = 'Add points to map'; }, 3000); }
        showToast('Added ' + added + ' point(s)');
        setTimeout(function() { if (typeof buildLayerControls === 'function') buildLayerControls(); }, 200);
      }
      return;
    }
    if (id === 'dm-btn-region-upload-add') {
      var taR = document.getElementById('dm-region-csv');
      if (taR && taR.value.trim()) {
        importUserRegionChoropleth(true, false);
        return;
      }
      var finp = document.getElementById('dm-region-csv-file');
      if (finp) {
        window._dmRegionCsvUploadThenAdd = true;
        finp.click();
      } else {
        showToast('Choose a CSV file first');
      }
      return;
    }
    if (id === 'dm-btn-points-upload-add') {
      var taP0 = document.getElementById('dm-add-data-csv');
      if (taP0 && taP0.value.trim()) {
        var n0 = addPointsFromCSVText(taP0.value);
        if (n0 > 0) {
          showToast('Added ' + n0 + ' point(s)');
          if (typeof buildLayerControls === 'function') buildLayerControls();
        } else showToast('Need lat, lon columns in your CSV');
        return;
      }
      var finpP = document.getElementById('dm-add-data-csv-file');
      if (finpP) {
        window._dmPointsCsvUploadThenAdd = true;
        finpP.click();
      } else {
        showToast('Choose a CSV file first');
      }
      return;
    }
    if (id === 'dm-btn-region-preview') {
      importUserRegionChoropleth(false, false);
      return;
    }
    if (id === 'dm-btn-region-add') {
      importUserRegionChoropleth(true, false);
      return;
    }
    if (id === 'dm-btn-region-add-layer') {
      importUserRegionChoropleth(true, true);
      return;
    }
    if (id === 'dm-btn-region-fetch-url') {
      var urlInputR = document.getElementById('dm-region-csv-url');
      var urlR = (urlInputR && urlInputR.value && urlInputR.value.trim()) || '';
      if (!urlR) { showToast('Enter a CSV URL'); return; }
      var btnR = document.getElementById('dm-btn-region-fetch-url');
      if (btnR) btnR.disabled = true;
      dmFetch(urlR).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      }).then(function(csv) {
        var taR = document.getElementById('dm-region-csv');
        if (taR) { taR.value = csv; showToast('CSV loaded  -  preview or add to map'); }
        if (btnR) btnR.disabled = false;
      }).catch(function() {
        showToast('URL did not load — try again or use a raw CSV link');
        if (btnR) btnR.disabled = false;
      });
      return;
    }
    if (id === 'dm-btn-regenerate-overview') {
      var ins = computeLiveInsightsFromMapData(getMapData());
      generateAiOverview(ins, true);
      return;
    }

    if (id === 'dm-btn-generate-observations') {
      var answerEl = document.getElementById('dm-ai-answer');
      if (!answerEl) return;
      var insights = getInsightsData();
      var observationsText = buildObservationsFromInsights(insights);
      var history = [{ role: 'assistant', content: observationsText }];
      setChatHistory(history);
      renderChatHistory(answerEl, history);
      showToast('Observations generated');
      return;
    }
    if (id === 'dm-btn-refresh-statistics') {
      if (typeof renderStatisticsCharts === 'function') renderStatisticsCharts();
      showToast('Charts updated');
      return;
    }

    if (id === 'dm-btn-finddata-search') {
      window._dmAiExtraDatasets = [];
      var qEl = document.getElementById('dm-finddata-query');
      var q = (qEl && qEl.value) ? qEl.value : '';
      var qNorm = normalizeQuery(q);
      if (!qNorm) { showToast('Type a search query'); return; }
      var inferred = inferGeoProfileFromQuery(qNorm);
      function finishSearch() {
        var results = rankDatasetCandidates(' ' + qNorm + ' ');
        renderFindDataResults(results);
        /* Auto-trigger AI search if key is available */
        var storedKey = getStored('openai-api-key');
        if (storedKey) {
          setTimeout(function() {
            var aiBtn = document.getElementById('dm-btn-ai-datasets');
            if (aiBtn && !aiBtn.disabled) aiBtn.click();
          }, 200);
        }
      }
      if (inferred) applyInferredGeoProfile(qNorm, inferred).then(finishSearch).catch(finishSearch);
      else finishSearch();
      return;
    }

    if (id === 'dm-btn-ai-datasets') {
      var qElAi = document.getElementById('dm-finddata-query');
      var qAi = (qElAi && qElAi.value) ? qElAi.value.trim() : '';
      if (!qAi) { showToast('Describe what data you need in the search box first'); return; }
      var keyInputAi = document.getElementById('dm-ai-api-key');
      var apiKeyAi = (keyInputAi && keyInputAi.value.trim()) || getStored('openai-api-key') || '';
      if (!apiKeyAi) {
        /* Show inline prompt to add key */
        var keyPrompt = prompt('To use AI dataset search, paste your OpenAI API key below.\n\nThis lets the AI find and suggest real datasets from World Bank, Our World in Data, and other sources based on your query.\n\nYour key is stored locally in your browser only.');
        if (!keyPrompt || !keyPrompt.trim()) { showToast('No key provided — use the catalog search instead'); return; }
        apiKeyAi = keyPrompt.trim();
        setStored('openai-api-key', apiKeyAi);
        if (keyInputAi) keyInputAi.value = apiKeyAi;
        showToast('API key saved! Searching with AI...');
      }
      setStored('openai-api-key', apiKeyAi);
      var btnAi = document.getElementById('dm-btn-ai-datasets');
      if (btnAi) { btnAi.disabled = true; btnAi.textContent = '⏳ Searching...'; }
      var proxyHosts =
        'For generic_csv or points_csv, csv_url must use ONLY hosts the app can fetch: raw.githubusercontent.com, gist.githubusercontent.com, ourworldindata.org, api.worldbank.org, data.worldbank.org (https). No other domains.';
      var promptUser =
        'User query (map data discovery): ' + qAi + '\\n\\n' +
        'You help users of the Data Mapper app (choropleth by country or state/province, or lat/lon points).\\n' +
        'Return JSON: { \"datasets\": [ 3 to 10 objects ] }. Each object:\\n' +
        '- title, description (concise), provider (string)\\n' +
        '- geo_granularity: country | state_province | city_points | other\\n' +
        '- dataset_kind: world_bank | owid | generic_csv | points_csv | embedded_csv\\n' +
        '- world_bank: world_bank_indicator (real id, e.g. SP.DYN.LE00.IN). Country-level ISO3 only.\\n' +
        '- owid: owid_slug (real OWID grapher slug). Usually country-level.\\n' +
        '- generic_csv: csv_url, iso_column, value_column, optional year_column, geo_keys (iso3 | iso3166_2 | name | auto), geo_profile (countries | admin1_global). ' +
        proxyHosts + '\\n' +
        '- points_csv: csv_url with lat,lon columns (optional name,value). Same URL host rules.\\n' +
        '- embedded_csv: use when no allowlisted URL is available OR for a small teaching example. Include csv_text (full CSV as one string: header + at most 30 data rows). Comma-separated fields only; no commas inside cells. Region keys must be ISO 3166-2 (e.g. MX-AGU, BR-SP), or ISO3 (USA), or names that match Natural Earth admin-1 labels. Include iso_column, value_column, year_column if present, geo_keys, geo_profile. Say in description if numbers are synthetic.\\n\\n' +
        'Prefer real World Bank / OWID / GitHub raw URLs over embedded_csv. Never invent csv_url on hosts outside the allowlist.';
      dmOpenAiChatCompletions({
        model: 'gpt-4o-mini',
        response_format: { type: 'json_object' },
        temperature: 0.2,
        max_tokens: 3800,
        messages: [
          { role: 'system', content: 'You return only one JSON object for data mapping. No markdown fences, no text outside JSON.' },
          { role: 'user', content: promptUser }
        ]
      }, apiKeyAi).then(function(res) {
        if (!res.ok) {
          var em = (res.data && res.data.error && res.data.error.message) ? res.data.error.message : ('HTTP ' + res.status);
          throw new Error(em);
        }
        var data = res.data;
        var content = data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content;
        if (!content) {
          var em2 = (data.error && data.error.message) ? data.error.message : 'Empty AI response';
          throw new Error(em2);
        }
        var parsed = tryParseAiJsonContent(content);
        var items = parsed.datasets || parsed.items || [];
        if (!items.length) throw new Error('No datasets in AI response');
        var out = [];
        function hsh(s) {
          var x = 0, str = String(s || '');
          for (var i = 0; i < str.length; i++) x = ((x << 5) - x) + str.charCodeAt(i) | 0;
          return ('00000000' + Math.abs(x).toString(16)).slice(-8);
        }
        items.forEach(function(it, idx) {
          var kind = String(it.dataset_kind || it.kind || '').toLowerCase();
          var title = it.title || ('Suggestion ' + (idx + 1));
          var desc = it.description || '';
          var prov = it.provider || 'Suggested';
          var wbInd = it.world_bank_indicator || it.indicator;
          var gran = it.geo_granularity || it.granularity || '';
          if ((kind === 'world_bank' || wbInd) && wbInd) {
            out.push({
              id: 'ai-wb-' + hsh(title + wbInd),
              title: title,
              description: desc,
              provider: 'World Bank',
              indicator: String(wbInd).trim(),
              defaultColormap: 'plasma',
              granularity: gran || 'Country',
              geo_profile: it.geo_profile || 'countries',
              geo_keys: it.geo_keys || ''
            });
          } else if ((kind === 'owid' || it.owid_slug) && it.owid_slug) {
            out.push({
              id: 'ai-owid-' + hsh(title + it.owid_slug),
              title: title,
              description: desc,
              provider: 'Our World in Data',
              owid_slug: String(it.owid_slug).trim().replace(/[^a-z0-9\\-]/gi, ''),
              defaultColormap: 'viridis',
              granularity: gran || 'Country',
              geo_profile: it.geo_profile || 'countries',
              geo_keys: it.geo_keys || ''
            });
          } else if ((kind === 'embedded_csv' || it.embedded_csv || it.csv_text || (kind === 'generic_csv' && it.csv_text && !it.csv_url)) && (it.embedded_csv || it.csv_text)) {
            var emb = String(it.embedded_csv || it.csv_text || '').trim();
            if (emb.length < 12) return;
            var gkEmb = String(it.geo_keys || it.demo_geo_keys || 'auto').toLowerCase().replace(/-/g, '_');
            if (gkEmb === 'iso31662') gkEmb = 'iso3166_2';
            var gpEmb = String(it.geo_profile || '').trim();
            if (!gpEmb || (gpEmb !== 'countries' && gpEmb !== 'admin1_global')) {
              gpEmb = (gkEmb === 'iso3') ? 'countries' : 'admin1_global';
            }
            var descEmb = desc;
            if (descEmb && !/synthetic|demo|illustrat|fake|approximate|estimate/i.test(descEmb)) {
              descEmb += ' (Verify values; AI-generated tables may be synthetic.)';
            }
            out.push({
              id: 'ai-embed-' + hsh(title + emb.slice(0, 160)),
              kind: 'embedded_csv',
              embedded_csv: emb,
              iso_column: it.iso_column || '',
              value_column: it.value_column || '',
              year_column: it.year_column || '',
              geo_keys: gkEmb || 'auto',
              demo_geo_keys: gkEmb || 'auto',
              demo_iso_column: it.iso_column || '',
              geo_profile: gpEmb,
              title: title,
              description: descEmb,
              provider: prov || 'AI-generated CSV',
              defaultColormap: 'viridis',
              granularity: gran || 'Embedded CSV'
            });
          } else if (kind === 'generic_csv' && it.csv_url) {
            out.push({
              id: 'ai-csv-' + hsh(title + it.csv_url),
              kind: 'generic_csv',
              csv_url: String(it.csv_url).trim(),
              iso_column: it.iso_column || '',
              year_column: it.year_column || '',
              value_column: it.value_column || '',
              geo_keys: it.geo_keys || '',
              geo_profile: it.geo_profile || '',
              title: title,
              description: desc,
              provider: prov || 'External CSV',
              defaultColormap: 'YlOrRd',
              granularity: gran || 'Region / CSV'
            });
          } else if (kind === 'points_csv' && it.csv_url) {
            out.push({
              id: 'ai-pt-' + hsh(title + it.csv_url),
              kind: 'points_csv',
              csv_url: String(it.csv_url).trim(),
              title: title,
              description: desc,
              provider: prov || 'Points CSV',
              defaultColormap: 'YlOrRd',
              granularity: gran || 'City / points'
            });
          }
        });
        if (!out.length) throw new Error('AI returned no usable datasets (need indicators, OWID slugs, csv_url on allowed hosts, or embedded_csv with csv_text)');
        window._dmAiExtraDatasets = out;
        var qNormAi = normalizeQuery(qAi);
        var geoHint = null;
        for (var gi = 0; gi < out.length; gi++) {
          if (out[gi] && out[gi].geo_profile && out[gi].geo_profile !== 'countries') {
            geoHint = out[gi].geo_profile;
            break;
          }
        }
        var qInf = inferGeoProfileFromQuery(qNormAi);
        var useGeo = geoHint || qInf;
        function finishAiRender() {
          renderFindDataResults(rankDatasetCandidates(' ' + qNormAi + ' '));
          showToast('AI found ' + out.length + ' dataset(s)!');
        /* Re-render search results with AI datasets at top, and scroll to them */
        var qElRefresh = document.getElementById('dm-finddata-query');
        var qRefresh = (qElRefresh && qElRefresh.value) ? qElRefresh.value : '';
        var refreshed = rankDatasetCandidates(' ' + normalizeQuery(qRefresh) + ' ');
        renderFindDataResults(refreshed);
        /* Scroll sidebar to top so AI results are visible */
        var sidebarContent = document.querySelector('#dm-sidebar .dm-sidebar-content');
        if (sidebarContent) sidebarContent.scrollTo({ top: 0, behavior: 'smooth' });
        }
        if (useGeo) applyInferredGeoProfile(qNormAi, useGeo).then(finishAiRender).catch(finishAiRender);
        else finishAiRender();
      }).catch(function(err) {
        var msg = err && err.message ? err.message : String(err);
        if (location.protocol === 'file:') msg += '  -  use python3 data-mapping.py --serve (OpenAI needs the /dm-openai relay).';
        showToast('AI search failed: ' + msg);
      }).finally(function() {
        if (btnAi) { btnAi.disabled = false; btnAi.textContent = '✨ AI'; }
      });
      return;
    }

    if (id === 'dm-btn-ai-generate-region-csv') {
      var qGen = (document.getElementById('dm-finddata-query') && document.getElementById('dm-finddata-query').value.trim()) || '';
      if (!qGen) {
        showToast('Type what you want in the Discover search box first (topic + geography)');
        return;
      }
      var keyInG = document.getElementById('dm-ai-api-key');
      var apiKG = (keyInG && keyInG.value.trim()) || getStored('openai-api-key') || '';
      if (!apiKG) { showToast('Add an OpenAI API key under Insights  ->  Ask'); return; }
      setStored('openai-api-key', apiKG);
      var btnG = document.getElementById('dm-btn-ai-generate-region-csv');
      if (btnG) { btnG.disabled = true; btnG.textContent = 'Generating...'; }
      var userG =
        'User instruction for a map-ready CSV: ' + qGen + '\\n\\n' +
        'Return ONE JSON object with keys:\\n' +
        '- output_kind: regions | points\\n' +
        '- csv_text: full CSV (header row + at most 35 data rows). Comma-separated fields only; no commas inside cells.\\n' +
        '- regions: include a region column (ISO 3166-2 like MX-AGU, IN-MH, or ISO3 like USA, or names) and numeric values.\\n' +
        '- points: lat,lon columns (decimal degrees WGS84), optional name,value.\\n' +
        '- iso_column, value_column, year_column (optional; exact header names from csv_text)\\n' +
        '- geo_keys: iso3 | iso3166_2 | name | auto (regions only)\\n' +
        '- geo_profile: countries | admin1_global (regions only)\\n' +
        '- note: one sentence; say if numbers are synthetic / for layout demo only\\n\\n' +
        'Prefer real region codes. If unsure, use a small multi-row demo for one country.';
      dmOpenAiChatCompletions({
        model: 'gpt-4o-mini',
        response_format: { type: 'json_object' },
        temperature: 0.25,
        max_tokens: 2400,
        messages: [
          { role: 'system', content: 'You output only valid JSON for the Data Mapper app. No markdown, no text outside JSON.' },
          { role: 'user', content: userG }
        ]
      }, apiKG).then(function(res) {
        if (!res.ok) {
          var emg = (res.data && res.data.error && res.data.error.message) ? res.data.error.message : ('HTTP ' + res.status);
          throw new Error(emg);
        }
        var datg = res.data;
        var cg = datg.choices && datg.choices[0] && datg.choices[0].message && datg.choices[0].message.content;
        if (!cg) throw new Error('Empty AI response');
        var jo = tryParseAiJsonContent(cg);
        var csvT = String(jo.csv_text || jo.embedded_csv || '').trim();
        if (!csvT) throw new Error('AI did not return csv_text');
        var ok = String(jo.output_kind || 'regions').toLowerCase();
        var msg = String(jo.note || '').trim() || 'Review the CSV, then Preview on map';
        if (ok === 'points') {
          var taP = document.getElementById('dm-add-data-csv');
          if (taP) taP.value = csvT;
          openDataWorkspace('points');
          showToast(msg + '  -  Your points tab');
        } else {
          var taR = document.getElementById('dm-region-csv');
          if (taR) taR.value = csvT;
          var isoI = document.getElementById('dm-region-iso-col');
          var valI = document.getElementById('dm-region-value-col');
          var yrI = document.getElementById('dm-region-year-col');
          var km = document.getElementById('dm-region-key-mode');
          if (isoI && jo.iso_column) isoI.value = String(jo.iso_column).trim();
          if (valI && jo.value_column) valI.value = String(jo.value_column).trim();
          if (yrI && jo.year_column) yrI.value = String(jo.year_column).trim();
          if (km && jo.geo_keys) {
            var gkx = String(jo.geo_keys).toLowerCase().replace(/-/g, '_');
            if (gkx === 'iso31662') gkx = 'iso3166_2';
            if (km.querySelector('option[value="' + gkx + '"]')) km.value = gkx;
          }
          var geoSel = document.getElementById('dm-geo-profile');
          var gp = String(jo.geo_profile || '').trim().toLowerCase();
          if (geoSel && gp && (gp === 'countries' || gp === 'admin1_global') && geoSel.querySelector('option[value="' + gp + '"]')) {
            geoSel.value = gp;
            setStored('dm-active-geo-profile', gp);
          }
          openDataWorkspace('regions');
          if (geoSel && gp && (gp === 'countries' || gp === 'admin1_global')) {
            ensureGeoProfile(gp).then(function() { showToast(msg); }).catch(function() { showToast(msg); });
          } else {
            showToast(msg);
          }
        }
      }).catch(function(err) {
        var msg = err && err.message ? err.message : String(err);
        if (location.protocol === 'file:') msg += '  -  use python3 data-mapping.py --serve for /dm-openai';
        showToast('AI CSV failed: ' + msg);
      }).finally(function() {
        if (btnG) { btnG.disabled = false; btnG.textContent = 'AI: fill regions CSV (uses Discover search box)'; }
      });
      return;
    }

    // Preview/Add buttons inside the results list.
    var prevBtn = el && el.closest && el.closest('[data-dm-preview-ds]');
    if (prevBtn) {
      var dsId = prevBtn.getAttribute('data-dm-preview-ds');
      if (dsId) previewOrAddDataset(dsId, false, false);
      return;
    }
    var addBtn = el && el.closest && el.closest('[data-dm-add-ds]');
    if (addBtn) {
      var dsId2 = addBtn.getAttribute('data-dm-add-ds');
      if (dsId2) previewOrAddDataset(dsId2, true, false);
      return;
    }
    var addMultiBtn = el && el.closest && el.closest('[data-dm-add-multi]');
    if (addMultiBtn) {
      var dsId3 = addMultiBtn.getAttribute('data-dm-add-multi');
      if (dsId3) previewOrAddDataset(dsId3, true, true);
      return;
    }

    // Dataset library actions
    var actBtn = el && el.closest && el.closest('[data-dm-activate-metric]');
    if (actBtn) {
      var mk = actBtn.getAttribute('data-dm-activate-metric');
      var md = getMapData() || {};
      if (md.metrics && md.metrics[mk]) {
        md.active_metric = mk;
        md.values = md.metrics[mk].values || {};
        md.min = md.metrics[mk].min;
        md.max = md.metrics[mk].max;
        setStored('live-map-data', stringifyLiveMapData(md));
        if (typeof refreshInsightsFromMap === 'function') refreshInsightsFromMap();
        var map = getMap();
        if (map) applyColorScale(map, md, getActiveColormap());
        showToast('Active dataset: ' + (md.metrics[mk].display || mk));
      }
      return;
    }
    var pinBtn = el && el.closest && el.closest('[data-dm-pin-metric]');
    if (pinBtn) {
      var mk2 = pinBtn.getAttribute('data-dm-pin-metric');
      var md2 = getMapData() || {};
      if (md2.metrics && md2.metrics[mk2]) {
        md2.metrics[mk2].pinned = !md2.metrics[mk2].pinned;
        setStored('live-map-data', stringifyLiveMapData(md2));
        showToast(md2.metrics[mk2].pinned ? 'Pinned' : 'Unpinned');
        // Re-render current results to refresh library UI
        var qEl = document.getElementById('dm-finddata-query');
        var q = (qEl && qEl.value) ? qEl.value : '';
        renderFindDataResults(rankDatasetCandidates(' ' + normalizeQuery(q) + ' '));
      }
      return;
    }
    var renameBtn = el && el.closest && el.closest('[data-dm-rename-metric]');
    if (renameBtn) {
      var mk3 = renameBtn.getAttribute('data-dm-rename-metric');
      var md3 = getMapData() || {};
      if (md3.metrics && md3.metrics[mk3]) {
        var nm = prompt('Rename dataset', md3.metrics[mk3].display || mk3);
        if (nm && nm.trim()) {
          md3.metrics[mk3].display = nm.trim();
          setStored('live-map-data', stringifyLiveMapData(md3));
          if (typeof refreshInsightsFromMap === 'function') refreshInsightsFromMap();
          showToast('Renamed');
          var qEl2 = document.getElementById('dm-finddata-query');
          var q2 = (qEl2 && qEl2.value) ? qEl2.value : '';
          renderFindDataResults(rankDatasetCandidates(' ' + normalizeQuery(q2) + ' '));
        }
      }
      return;
    }
    var removeBtn = el && el.closest && el.closest('[data-dm-remove-metric]');
    if (removeBtn) {
      var mk4 = removeBtn.getAttribute('data-dm-remove-metric');
      var md4 = getMapData() || {};
      if (md4.metrics && md4.metrics[mk4]) {
        if (confirm('Remove dataset "' + (md4.metrics[mk4].display || mk4) + '"?')) {
          delete md4.metrics[mk4];
          if (md4.active_metric === mk4) {
            var remaining = Object.keys(md4.metrics);
            md4.active_metric = remaining.length ? remaining[0] : '';
            if (md4.active_metric) {
              md4.values = md4.metrics[md4.active_metric].values || {};
              md4.min = md4.metrics[md4.active_metric].min;
              md4.max = md4.metrics[md4.active_metric].max;
            } else {
              md4.values = {};
              md4.min = 0;
              md4.max = 1;
              setStored('live-insights', '');
            }
          }
          setStored('live-map-data', stringifyLiveMapData(md4));
          if (typeof refreshInsightsFromMap === 'function') refreshInsightsFromMap();
          var map2 = getMap();
          if (map2) applyColorScale(map2, md4, getActiveColormap());
          showToast('Removed');
          var qEl3 = document.getElementById('dm-finddata-query');
          var q3 = (qEl3 && qEl3.value) ? qEl3.value : '';
          renderFindDataResults(rankDatasetCandidates(' ' + normalizeQuery(q3) + ' '));
        }
      }
      return;
    }
    var exportBtn = el && el.closest && el.closest('[data-dm-export-metric]');
    if (exportBtn) {
      var mk5 = exportBtn.getAttribute('data-dm-export-metric');
      var md5 = getMapData() || {};
      if (md5.metrics && md5.metrics[mk5] && md5.metrics[mk5].values) {
        var rows = ['location,value'];
        var vals = md5.metrics[mk5].values;
        for (var k in vals) rows.push(k + ',' + vals[k]);
        var blob = new Blob([rows.join('\\n')], { type: 'text/csv;charset=utf-8' });
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = (md5.metrics[mk5].display || mk5).replace(/[^a-z0-9\\-_]+/gi,'_').toLowerCase() + '.csv';
        a.click();
        URL.revokeObjectURL(a.href);
        showToast('CSV exported');
      }
      return;
    }

    if (id === 'dm-btn-clear-chat') {
      var answerEl = document.getElementById('dm-ai-answer');
      clearChatHistory();
      if (answerEl) {
        answerEl.textContent = '';
        answerEl.classList.remove('dm-visible');
      }
      showToast('Chat cleared');
      return;
    }

    if (id === 'dm-btn-ask') {
      var input = document.getElementById('dm-ai-question');
      var q = (input && input.value.trim()) || '';
      var answerEl = document.getElementById('dm-ai-answer');
      if (!answerEl) return;
      answerEl.classList.add('dm-visible');

      var modeSel = document.getElementById('dm-ai-mode');
      var mode = (modeSel && modeSel.value) ? modeSel.value : 'both';

      var keyInput = document.getElementById('dm-ai-api-key');
      var apiKey = (keyInput && keyInput.value.trim()) || getStored('openai-api-key') || '';

      var insights = getInsightsData();
      var observationsText = buildObservationsFromInsights(insights);

      if (!q) {
        // If the user didn't type anything, just show grounded observations.
        var emptyHistory = [{ role: 'assistant', content: ruleBasedAnswer('observations') }];
        setChatHistory(emptyHistory);
        renderChatHistory(answerEl, emptyHistory);
        return;
      }

      var history = getChatHistory();
      history.push({ role: 'user', content: q });
      if (history.length > 18) history = history.slice(history.length - 18);
      setChatHistory(history);
      renderChatHistory(answerEl, history);

      var wantsLLM = (mode === 'llm' || mode === 'both');
      var shouldUseLLM = wantsLLM && apiKey;

      if (mode === 'observations' || !shouldUseLLM) {
        var responseText = mode === 'observations'
          ? ruleBasedAnswer(q)
          : (observationsText + '\\n\\n(Provide an OpenAI API key to enable deeper AI narratives.)');
        history.push({ role: 'assistant', content: responseText });
        if (history.length > 20) history = history.slice(history.length - 20);
        setChatHistory(history);
        renderChatHistory(answerEl, history);
        if (input) input.value = q;
        return;
      }

      // OpenAI mode (key provided). Persist key and keep the response grounded in observations.
      setStored('openai-api-key', apiKey);

      if (mode === 'both') {
        history.push({ role: 'assistant', content: observationsText });
        if (history.length > 22) history = history.slice(history.length - 22);
        setChatHistory(history);
        renderChatHistory(answerEl, history);
      }

      var placeholderIndex = history.length;
      history.push({ role: 'assistant', content: 'Asking AI...' });
      setChatHistory(history);
      renderChatHistory(answerEl, history);

      // Build concise user context for better, grounded outputs.
      var goal = getStored('map-goal') || defaultTitle();
      var reflection = getStored('reflection') || '';
      var researchNotes = getStored('research-notes') || '';
      var brainstorm = getStored('brainstorm') || '';
      var highlights = getStoredJson('highlights', []) || [];
      var visionCards = getStoredJson('vision-cards', []) || [];
      var actionSteps = getStoredJson('action-steps', []) || [];
      var sources = getStoredJson('research-sources', []) || [];

      var highlightsText = highlights.slice(0, 5).map(function(h) {
        var tag = h && h.tag ? String(h.tag) : 'Finding';
        var t = h && h.text ? String(h.text) : '';
        return '- [' + tag + '] ' + t;
      }).join('\\n');

      var visionText = visionCards.slice(0, 6).map(function(c) {
        var title = c && c.title ? String(c.title) : '';
        var cat = c && c.category ? String(c.category) : 'Goals';
        return '- ' + title + ' (' + cat + ')';
      }).join('\\n');

      var stepsText = actionSteps.slice(0, 8).map(function(s) {
        var done = s && s.done ? '[DONE] ' : '';
        var t = s && s.text ? String(s.text) : '';
        return '- ' + done + t;
      }).join('\\n');

      var sourcesText = sources.slice(0, 3).map(function(s) {
        var label = s && (s.title || s.url) ? String(s.title || s.url) : 'Source';
        var year = s && s.year ? String(s.year) : '';
        return '- ' + label + (year ? ' (' + year + ')' : '');
      }).join('\\n');

      var systemPrompt =
        'You are an AI assistant embedded in a data-mapping tool. You must ground answers in the provided observations, stats, and cross-metric correlations; they always reflect the user\'s current map layers and geography. ' +
        'If the user requests something not supported by the data, say so clearly. ' +
        'If they ask how to add CSVs or compatible datasets, explain: use the Data tab  -  Discover  -  **AI suggestions** (finds World Bank / OWID / allowlisted GitHub CSVs or small embedded tables) or **AI: fill regions CSV** from the Discover search box for map-ready comma-separated text (verify before publishing); choropleth keys are ISO3 countries or ISO 3166-2 subnational codes (or names); points need lat/lon. ' +
        'Output in markdown with: (1) key takeaways, (2) suggested actions, (3) a short narrative paragraph suitable for reports/grants.' +
        '\\n\\nMap title: ' + defaultTitle() + '.' +
        '\\nGoal: ' + goal + '.' +
        '\\n\\nUser notes (may be empty):' +
        '\\nReflection: ' + (reflection ? reflection.slice(0, 800) : 'n/a') +
        '\\nBrainstorm: ' + (brainstorm ? brainstorm.slice(0, 800) : 'n/a') +
        '\\nResearch notes: ' + (researchNotes ? researchNotes.slice(0, 800) : 'n/a') +
        '\\nKey highlights:\\n' + (highlightsText || 'n/a') +
        '\\nVision board:\\n' + (visionText || 'n/a') +
        '\\nAction steps:\\n' + (stepsText || 'n/a') +
        '\\nSources:\\n' + (sourcesText || 'n/a') +
        '\\n\\nGrounded observations (use these as your factual basis):\\n' + observationsText;

      var trimmedHistory = history.filter(function(m) { return m && m.content && m.content !== 'Asking AI...'; });
      trimmedHistory = trimmedHistory.slice(-10);
      var modelMessages = [{ role: 'system', content: systemPrompt }].concat(trimmedHistory.map(function(m) {
        return { role: m.role, content: m.content };
      }));

      dmOpenAiChatCompletions({
        model: 'gpt-4o-mini',
        messages: modelMessages,
        temperature: 0.3,
        max_tokens: 900
      }, apiKey).then(function(res) {
        if (!res.ok) {
          var em = (res.data && res.data.error && res.data.error.message) ? res.data.error.message : ('HTTP ' + res.status);
          throw new Error(em);
        }
        var data = res.data;
        var text = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content)
          ? data.choices[0].message.content
          : ((data.error && data.error.message) ? data.error.message : 'Could not get a response.');
        history[placeholderIndex].content = text;
        setChatHistory(history);
        renderChatHistory(answerEl, history);
      }).catch(function(err) {
        var base = err.message || 'Request failed. Check API key and network.';
        if (location.protocol === 'file:') {
          base += ' Open the map with: python3 data-mapping.py --serve (browsers block OpenAI from file://; the local server relays /dm-openai).';
        }
        history[placeholderIndex].content = 'Error: ' + base;
        setChatHistory(history);
        renderChatHistory(answerEl, history);
      });

      return;
    }
    if ((el && el.classList && el.classList.contains('dm-q-btn')) || (el && el.getAttribute && el.getAttribute('data-q'))) {
      var qBtn = el.closest && el.closest('.dm-q-btn') || el;
      var q = qBtn.getAttribute && qBtn.getAttribute('data-q');
      if (q) {
        var aiInput = document.getElementById('dm-ai-question');
        if (aiInput) aiInput.value = q;
        var askBtn = document.getElementById('dm-btn-ask');
        if (askBtn) askBtn.click();
      }
      return;
    }
    if (el && el.classList && el.classList.contains('dm-tab')) {
      var tab = el.closest && el.closest('.dm-tab') || el;
      var t = tab.getAttribute && tab.getAttribute('data-tab');
      if (t) {
        document.querySelectorAll('.dm-tab').forEach(function(bt) { bt.classList.remove('active'); bt.setAttribute('aria-selected', 'false'); });
        document.querySelectorAll('.dm-tab-panel').forEach(function(p) { p.classList.remove('active'); });
        tab.classList.add('active'); tab.setAttribute('aria-selected', 'true');
        var panel = document.getElementById('dm-panel-' + t);
        if (panel) panel.classList.add('active');
        if (t === 'insights' && typeof refreshInsightsFromMap === 'function') refreshInsightsFromMap();
        if (t === 'action' && typeof updateActionHubHero === 'function') updateActionHubHero();
        if (t === 'settings' && typeof buildLayerControls === 'function') setTimeout(buildLayerControls, 50);
      }
      return;
    }
    if (el && el.classList && el.classList.contains('dm-sub-tab')) {
      var subTab = el.closest && el.closest('.dm-sub-tab') || el;
      var parentPanel = subTab.closest && subTab.closest('.dm-tab-panel');
      var sub = subTab.getAttribute && subTab.getAttribute('data-sub');
      var dataFor = (subTab.closest && subTab.closest('.dm-sub-tabs') && subTab.closest('.dm-sub-tabs').getAttribute('data-for')) || '';
      if (parentPanel && sub) {
        parentPanel.querySelectorAll('.dm-sub-tab').forEach(function(b) { b.classList.remove('active'); });
        parentPanel.querySelectorAll('.dm-sub-panel').forEach(function(p) { p.classList.remove('active'); });
        subTab.classList.add('active');
        var subPanel = document.getElementById('dm-' + dataFor + '-' + sub);
        if (subPanel) subPanel.classList.add('active');
        if (dataFor === 'insights' && sub === 'takeaways' && typeof refreshInsightsFromMap === 'function') {
          refreshInsightsFromMap();
        }
        if (dataFor === 'insights' && sub === 'statistics' && typeof renderStatisticsCharts === 'function') {
          renderStatisticsCharts();
        }
        if (dataFor === 'action' && typeof updateActionHubHero === 'function') updateActionHubHero();
        if (dataFor === 'settings' && sub === 'layers' && typeof buildLayerControls === 'function') {
          setTimeout(buildLayerControls, 50);
        }
      }
      return;
    }
    if (id === 'dm-btn-fetch-data') {
      var urlInput = document.getElementById('dm-add-data-url');
      var url = (urlInput && urlInput.value && urlInput.value.trim()) || '';
      if (!url) { showToast('Enter a CSV URL'); return; }
      var btn = document.getElementById('dm-btn-fetch-data');
      if (btn) btn.disabled = true;
      dmFetch(url).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      }).then(function(csv) {
        var ta = document.getElementById('dm-add-data-csv');
        if (ta) { ta.value = csv; showToast('Data loaded  -  click Add points to map'); }
        if (btn) btn.disabled = false;
      }).catch(function() {
        showToast('URL did not load — try again or paste CSV text');
        if (btn) btn.disabled = false;
      });
      return;
    }
    if (id === 'dm-btn-add-vision') {
      var inp = document.getElementById('dm-vision-input');
      var descEl = document.getElementById('dm-vision-desc');
      var catEl = document.getElementById('dm-vision-category');
      var priEl = document.getElementById('dm-vision-priority');
      var title = (inp && inp.value && inp.value.trim()) || '';
      if (!title) { showToast('Enter a card title'); return; }
      var description = (descEl && descEl.value && descEl.value.trim()) || '';
      var category = (catEl && catEl.value) || 'Goals';
      var priority = (priEl && priEl.value) || 'medium';
      var list = JSON.parse(getStored('vision-cards') || '[]');
      if (window._dmVisionEditIndex != null && !isNaN(window._dmVisionEditIndex) && list[window._dmVisionEditIndex]) {
        list[window._dmVisionEditIndex] = { title: title, description: description, category: category, priority: priority };
        window._dmVisionEditIndex = null;
        showToast('Card updated');
      } else {
        list.push({ title: title, description: description, category: category, priority: priority });
        showToast('Card added');
      }
      setStored('vision-cards', JSON.stringify(list));
      if (inp) inp.value = '';
      if (descEl) descEl.value = '';
      if (catEl) catEl.value = 'Goals';
      if (priEl) priEl.value = 'medium';
      renderVisionCards();
      return;
    }
    if (id === 'dm-btn-clear-vision') {
      if (confirm('Clear all vision cards?')) {
        setStored('vision-cards', '[]');
        renderVisionCards();
        showToast('Vision board cleared');
      }
      return;
    }
    if (id === 'dm-btn-refresh-layers') {
      buildLayerControls();
      showToast('Layers refreshed');
      return;
    }
    if (id === 'dm-btn-hide-layers') {
      toggleNativeLayerControl();
      return;
    }
    if (id === 'dm-btn-apply-colors') {
      var m = getMap(), d = getMapData();
      if (m && d) { applyColorScale(m, d, getActiveColormap()); showToast('Color scale applied'); }
      else { showToast('No map data to apply colors to'); }
      return;
    }
    if (id === 'dm-btn-add-step') {
      var inpS = document.getElementById('dm-action-input');
      var txtS = (inpS && inpS.value && inpS.value.trim()) || '';
      if (!txtS) return;
      if (appendActionStep(txtS)) {
        if (inpS) inpS.value = '';
        showToast('Step added');
      }
      return;
    }
    if (id === 'dm-btn-save-brainstorm') {
      var ta = document.getElementById('dm-brainstorm');
      if (ta) { setStored('brainstorm', ta.value); showToast('Brainstorm saved'); }
      return;
    }
    if (id === 'dm-btn-copy-view') {
      var map = getMap();
      if (map) {
        var c = map.getCenter();
        var z = map.getZoom();
        var link = window.location.origin + window.location.pathname + '#' + [c.lat.toFixed(4), c.lng.toFixed(4), z].join(',');
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(link).then(function() { showToast('View link copied'); });
        }
      }
      return;
    }
    if (id === 'dm-btn-add-source') {
      var urlEl = document.getElementById('dm-research-url');
      var titleEl = document.getElementById('dm-research-title');
      var authorEl = document.getElementById('dm-research-author');
      var yearEl = document.getElementById('dm-research-year');
      var typeEl = document.getElementById('dm-research-type');
      var notesEl = document.getElementById('dm-research-source-notes');
      var url = (urlEl && urlEl.value && urlEl.value.trim()) || '';
      var title = (titleEl && titleEl.value && titleEl.value.trim()) || '';
      var author = (authorEl && authorEl.value && authorEl.value.trim()) || '';
      var year = (yearEl && yearEl.value && yearEl.value.trim()) || '';
      var sType = (typeEl && typeEl.value) || 'Article';
      var notes = (notesEl && notesEl.value && notesEl.value.trim()) || '';
      if (!url && !title) { showToast('Enter a URL or title'); return; }
      var list = JSON.parse(getStored('research-sources') || '[]');
      if (window._dmSourceEditIndex != null && !isNaN(window._dmSourceEditIndex) && list[window._dmSourceEditIndex]) {
        list[window._dmSourceEditIndex] = { url: url, title: title || url, author: author, year: year, type: sType, notes: notes };
        window._dmSourceEditIndex = null;
        showToast('Source updated');
      } else {
        list.push({ url: url, title: title || url, author: author, year: year, type: sType, notes: notes });
        showToast('Source added');
      }
      setStored('research-sources', JSON.stringify(list));
      [urlEl, titleEl, authorEl, yearEl, notesEl].forEach(function(el) { if (el) el.value = ''; });
      if (typeEl) typeEl.value = 'Article';
      renderResearchSources();
      return;
    }
    if (id === 'dm-btn-clear-sources') {
      if (confirm('Clear all research sources?')) {
        setStored('research-sources', '[]');
        renderResearchSources();
        showToast('All sources cleared');
      }
      return;
    }
    if (id === 'dm-btn-save-research-notes') {
      var ta = document.getElementById('dm-research-notes-text');
      if (ta) { setStored('research-notes', ta.value); showToast('Research notes saved'); updateNotesWordCount(); }
      return;
    }
    if (id === 'dm-btn-clear-notes') {
      if (confirm('Clear all research notes?')) {
        var ta = document.getElementById('dm-research-notes-text');
        if (ta) ta.value = '';
        setStored('research-notes', '');
        updateNotesWordCount();
        showToast('Notes cleared');
      }
      return;
    }
    if (id === 'dm-btn-add-highlight') {
      var hInput = document.getElementById('dm-highlight-input');
      var hColor = document.getElementById('dm-highlight-color');
      var hTag = document.getElementById('dm-highlight-tag');
      var text = (hInput && hInput.value && hInput.value.trim()) || '';
      if (!text) { showToast('Enter a highlight'); return; }
      var list = JSON.parse(getStored('highlights') || '[]');
      list.push({ text: text, color: (hColor && hColor.value) || 'yellow', tag: (hTag && hTag.value) || 'Finding' });
      setStored('highlights', JSON.stringify(list));
      if (hInput) hInput.value = '';
      renderHighlights();
      showToast('Highlight added');
      return;
    }
    if (id === 'dm-btn-clear-highlights') {
      if (confirm('Clear all highlights?')) {
        setStored('highlights', '[]');
        renderHighlights();
        showToast('Highlights cleared');
      }
      return;
    }
    if (id === 'dm-btn-export-highlights') {
      var list = JSON.parse(getStored('highlights') || '[]');
      var text = list.map(function(h, i) { return (i + 1) + '. [' + (h.tag || 'Finding') + '] ' + h.text; }).join('\\n');
      if (navigator.clipboard && text) { navigator.clipboard.writeText(text).then(function() { showToast('Highlights copied'); }); }
      else { showToast('No highlights to copy'); }
      return;
    }
    if (id === 'dm-btn-export-notes-only') {
      var notes = getStored('research-notes') || '';
      if (navigator.clipboard && notes) { navigator.clipboard.writeText(notes).then(function() { showToast('Notes copied'); }); }
      else { showToast('No notes to copy'); }
      return;
    }
    if (id === 'dm-btn-apply-display') {
      applyDisplaySettings();
      showToast('Display settings applied');
      return;
    }
    if (id === 'dm-btn-reset-display') {
      document.getElementById('dm-border-weight').value = 1;
      document.getElementById('dm-border-color').value = '#ffffff';
      document.getElementById('dm-border-opacity').value = 0.8;
      document.getElementById('dm-fill-opacity').value = 0.75;
      document.getElementById('dm-font-size').value = 13;
      var prEl = document.getElementById('dm-point-radius');
      if (prEl) prEl.value = 10;
      document.getElementById('dm-show-labels').checked = false;
      document.getElementById('dm-show-values').checked = false;
      setStored('show-labels', '0');
      setStored('show-values', '0');
      setStored('point-radius', '10');
      updateRangeLabels();
      applyDisplaySettings();
      showToast('Reset to defaults');
      return;
    }
    if (id === 'dm-btn-export-research') {
      var sources = JSON.parse(getStored('research-sources') || '[]');
      var notes = getStored('research-notes') || '';
      var highlights = JSON.parse(getStored('highlights') || '[]');
      var lines = ['# Research Export', '# Generated by Data Mapper', '', '## Sources (' + sources.length + ')', ''];
      sources.forEach(function(s, i) {
        lines.push((i + 1) + '. ' + (s.title || s.url));
        if (s.author || s.year) lines.push('   ' + [s.author, s.year].filter(Boolean).join(', '));
        if (s.type) lines.push('   Type: ' + s.type);
        if (s.url) lines.push('   URL: ' + s.url);
        if (s.notes) lines.push('   Notes: ' + s.notes);
        lines.push('');
      });
      if (highlights.length) {
        lines.push('## Key Highlights (' + highlights.length + ')', '');
        highlights.forEach(function(h, i) {
          lines.push((i + 1) + '. [' + (h.tag || 'Finding') + '] ' + h.text);
        });
        lines.push('');
      }
      lines.push('## Research Notes', '', notes);
      var blob = new Blob([lines.join('\\n')], { type: 'text/plain;charset=utf-8' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'research-export.txt';
      a.click();
      URL.revokeObjectURL(a.href);
      showToast('Full research exported');
      return;
    }
    if (id === 'dm-btn-export-bib') {
      var sources = JSON.parse(getStored('research-sources') || '[]');
      var text = sources.map(function(s, i) {
        var parts = [(i + 1) + '.'];
        if (s.author) parts.push(s.author + '.');
        parts.push((s.title || s.url) + '.');
        if (s.year) parts.push('(' + s.year + ').');
        if (s.type) parts.push('[' + s.type + '].');
        if (s.url) parts.push(s.url);
        return parts.join(' ');
      }).join('\\n');
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() { showToast('Bibliography copied'); });
      }
      return;
    }
    if (id === 'dm-btn-export-vision') {
      var cards = JSON.parse(getStored('vision-cards') || '[]');
      var lines = ['# Vision board', ''];
      var byCat = {};
      cards.forEach(function(c) {
        var cat = (c && c.category) ? c.category : 'Goals';
        if (!byCat[cat]) byCat[cat] = [];
        byCat[cat].push(c);
      });
      ['Goals', 'Ideas', 'Later'].forEach(function(cat) {
        if (byCat[cat] && byCat[cat].length) {
          lines.push('## ' + cat);
          byCat[cat].forEach(function(c) {
            var title = (c && c.title) ? c.title : (typeof c === 'string' ? c : '');
            lines.push('- ' + title);
            if (c && c.description) lines.push('  ' + c.description);
          });
          lines.push('');
        }
      });
      var blob = new Blob([lines.join('\\n')], { type: 'text/plain;charset=utf-8' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'vision-board.txt';
      a.click();
      URL.revokeObjectURL(a.href);
      showToast('Vision board exported');
      return;
    }
    if (id === 'dm-btn-export-action-plan') {
      var steps = JSON.parse(getStored('action-steps') || '[]');
      var brainstorm = getStored('brainstorm') || '';
      var lines = ['# Action plan', '', '## Steps', ''];
      steps.forEach(function(s, i) {
        lines.push((i + 1) + '. ' + (s.done ? '[DONE] ' : '') + (s.text || ''));
      });
      lines.push('', '## Brainstorm / notes', '', brainstorm);
      var blob = new Blob([lines.join('\\n')], { type: 'text/plain;charset=utf-8' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'action-plan.txt';
      a.click();
      URL.revokeObjectURL(a.href);
      showToast('Action plan exported');
      return;
    }
    } catch (err) { console.error('Data Mapper click error', id, err); }
  }
  function initClickListeners() {
    document.body.addEventListener('click', handleClick);
    /* Quick-search pill buttons */
    document.querySelectorAll('.dm-quick-search-btn').forEach(function(btn) {
      btn.addEventListener('click', function(e) {
        e.stopPropagation();
        var q = btn.getAttribute('data-q');
        if (!q) return;
        var si = document.getElementById('dm-finddata-query');
        if (si) si.value = q;
        /* Scroll sidebar back to top so results are visible */
        var sc = document.querySelector('#dm-sidebar .dm-sidebar-content');
        if (sc) sc.scrollTo({ top: 0, behavior: 'smooth' });
        /* Trigger the Search button which has its own handleClick registration */
        var sb = document.getElementById('dm-btn-finddata-search');
        if (sb) sb.click();
      });
    });
    var buttonIds = ['dm-sidebar-toggle','dm-welcome-start','dm-about-close','dm-btn-about','dm-btn-data-tab','dm-btn-save-reflection','dm-btn-copy-key-message','dm-btn-share-impact','dm-btn-export','dm-btn-export-pdf','dm-btn-fullscreen','dm-btn-copy-link','dm-btn-download-csv','dm-btn-print','dm-btn-locate','dm-btn-add-data','dm-btn-region-preview','dm-btn-region-add','dm-btn-region-add-layer','dm-btn-region-fetch-url','dm-btn-region-upload-add','dm-btn-points-upload-add','dm-btn-finddata-search','dm-btn-ai-datasets','dm-btn-ai-generate-region-csv','dm-btn-generate-observations','dm-btn-refresh-statistics','dm-btn-clear-chat','dm-btn-ask','dm-btn-fetch-data','dm-btn-add-vision','dm-btn-add-step','dm-btn-save-brainstorm','dm-btn-copy-view','dm-btn-add-source','dm-btn-save-research-notes','dm-btn-export-research','dm-btn-export-bib','dm-btn-export-vision','dm-btn-export-action-plan','dm-btn-clear-vision','dm-btn-refresh-layers','dm-btn-hide-layers','dm-btn-apply-colors','dm-btn-clear-sources','dm-btn-clear-notes','dm-btn-add-highlight','dm-btn-clear-highlights','dm-btn-export-highlights','dm-btn-export-notes-only','dm-btn-apply-display','dm-btn-reset-display','dm-btn-regenerate-overview','dm-qs-life','dm-qs-child','dm-qs-gdp','dm-qs-poverty','dm-qs-co2','dm-qs-india','dm-qs-us','dm-qs-canada'];
    buttonIds.forEach(function(buttonId) {
      var btn = document.getElementById(buttonId);
      if (btn) btn.addEventListener('click', function(e) { e.stopPropagation(); handleClick({ target: btn }); });
    });
    wireCsvUploadInputs();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initClickListeners);
  } else {
    initClickListeners();
  }
  window._dmVisionEditIndex = null;
  document.addEventListener('fullscreenchange', function() {
    if (!document.fullscreenElement) {
      fullscreen = false;
      var b = document.getElementById('dm-btn-fullscreen');
      if (b) b.innerHTML = 'Fullscreen';
    }
  });

  function whenReady(fn) {
    function run() { setTimeout(fn, 150); }
    if (document.readyState === 'complete') run();
    else window.addEventListener('load', run);
  }
  whenReady(function() {
    try {
    var container = getMapContainer();
    var map = getMap();
    var mapData = getMapData();
    if (mapData && mapData.type === 'choropleth' && _embeddedHasChoroValues(mapData)) {
      try { setStored('live-map-data', stringifyLiveMapData(mapData)); } catch (eInit) {}
    }

    var welcomeModal = document.getElementById('dm-welcome-modal');
    if (welcomeModal) {
      if (getStored('welcome-done') !== '1') { welcomeModal.style.display = 'flex'; }
      else { welcomeModal.style.display = 'none'; }
    }

    if (location.protocol === 'file:') {
      var fban = document.getElementById('dm-fetch-banner');
      if (fban) fban.style.display = 'block';
    }

    var savedTitle = getStored('map-title');
    var savedGoal = getStored('map-goal');
    var titleEl = document.getElementById('dm-map-title');
    if (titleEl) titleEl.textContent = savedTitle || defaultTitle();
    var goalEl = document.getElementById('dm-your-goal');
    if (goalEl && savedGoal) goalEl.innerHTML = '<strong>' + savedGoal + '</strong>';

    var reflectionEl = document.getElementById('dm-reflection');
    if (reflectionEl && getStored('reflection')) reflectionEl.value = getStored('reflection');

    var geoProfileSel = document.getElementById('dm-geo-profile');
    if (geoProfileSel) {
      var savedGeo = getStored('dm-active-geo-profile');
      if (savedGeo && geoProfileSel.querySelector('option[value="' + savedGeo + '"]')) geoProfileSel.value = savedGeo;
      geoProfileSel.addEventListener('change', function() {
        setStored('dm-active-geo-profile', geoProfileSel.value);
        if (typeof updateAdmin1FocusUiVisibility === 'function') updateAdmin1FocusUiVisibility();
        ensureGeoProfile(geoProfileSel.value).then(function() {
          showToast('Map geography updated (applies to new previews and joins)');
          var m2 = getMap(), d2 = getMapData();
          if (m2 && d2) { applyColorScale(m2, d2, getActiveColormap()); }
          if (typeof buildLayerControls === 'function') buildLayerControls();
          if (typeof updateAdmin1FocusUiVisibility === 'function') updateAdmin1FocusUiVisibility();
        }).catch(function(e) { showToast(e.message || String(e)); });
      });
    }
    if (typeof updateAdmin1FocusUiVisibility === 'function') updateAdmin1FocusUiVisibility();
    var admin1FocusSel = document.getElementById('dm-admin1-focus');
    if (admin1FocusSel) {
      admin1FocusSel.addEventListener('change', function() {
        var v = String(admin1FocusSel.value || '').trim().toUpperCase();
        try { setStored('dm-admin1-focus-iso2', v); } catch (eSt) {}
        if (typeof applyAdmin1CountryFilter === 'function') applyAdmin1CountryFilter(v, false);
      });
    }

    if (map && container) {
      function invalidateMapSize() {
        try {
          map.invalidateSize();
          var c = container.querySelector('.leaflet-container');
          if (c) { map.setView(map.getCenter(), map.getZoom()); }
        } catch (e) {}
      }
      if (typeof ResizeObserver !== 'undefined') {
        var ro = new ResizeObserver(function() { invalidateMapSize(); });
        var area = document.getElementById('dm-map-area');
        if (area) ro.observe(area);
      }
      window.addEventListener('resize', function() {
        clearTimeout(window._dmResize);
        window._dmResize = setTimeout(invalidateMapSize, 100);
      });
    }

    (function initSidebarResize() {
      var handle = document.getElementById('dm-sidebar-resize-handle');
      var sidebar = document.getElementById('dm-sidebar');
      if (!handle || !sidebar) return;
      var stored = getStored('dm-sidebar-width');
      if (stored) {
        var nw = parseInt(stored, 10);
        var maxW = Math.min(900, Math.floor(window.innerWidth * 0.88));
        if (nw >= 260 && nw <= maxW) sidebar.style.setProperty('--dm-sidebar-width', nw + 'px');
      }
      var dragging = false;
      var startX = 0;
      var startW = 0;
      handle.addEventListener('mousedown', function(ev) {
        if (sidebar.classList.contains('dm-sidebar-closed')) return;
        dragging = true;
        startX = ev.clientX;
        startW = sidebar.getBoundingClientRect().width;
        sidebar.classList.add('dm-sidebar-resizing');
        ev.preventDefault();
        document.body.style.cursor = 'ew-resize';
        document.body.style.userSelect = 'none';
      });
      function onMove(ev) {
        if (!dragging) return;
        var maxW = Math.min(900, Math.floor(window.innerWidth * 0.88));
        var dx = ev.clientX - startX;
        var newW = Math.round(Math.max(260, Math.min(maxW, startW + dx)));
        sidebar.style.setProperty('--dm-sidebar-width', newW + 'px');
      }
      function onUp() {
        if (!dragging) return;
        dragging = false;
        sidebar.classList.remove('dm-sidebar-resizing');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        if (!sidebar.classList.contains('dm-sidebar-closed')) {
          var w = Math.round(sidebar.getBoundingClientRect().width);
          setStored('dm-sidebar-width', String(w));
        }
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    })();

    function updateColorPreview(cmName) {
      var prev = document.getElementById('dm-color-preview');
      if (!prev) return;
      var colors = COLORMAPS[cmName] || COLORMAPS.YlOrRd;
      prev.innerHTML = '';
      colors.forEach(function(c) {
        var s = document.createElement('div');
        s.style.flex = '1';
        s.style.background = c;
        prev.appendChild(s);
      });
    }
    /* Metric selector: populate from mapData.metrics */
    var metricSelect = document.getElementById('dm-metric-select');
    if (metricSelect && mapData && mapData.metrics) {
      metricSelect.innerHTML = '';
      var mkeys = Object.keys(mapData.metrics);
      mkeys.forEach(function(mk) {
        var opt = document.createElement('option');
        opt.value = mk;
        opt.textContent = mapData.metrics[mk].display || mk;
        metricSelect.appendChild(opt);
      });
      if (mapData.active_metric && metricSelect.querySelector('option[value="' + mapData.active_metric + '"]')) {
        metricSelect.value = mapData.active_metric;
      }
      metricSelect.addEventListener('change', function() {
        var mk = this.value;
        var md = getMapData();
        if (md && md.metrics && md.metrics[mk]) {
          var mInfo = md.metrics[mk];
          md.values = mInfo.values;
          md.min = mInfo.min;
          md.max = mInfo.max;
          md.active_metric = mk;
          try { setStored('live-map-data', stringifyLiveMapData(md)); } catch (e) {}
          if (typeof refreshInsightsFromMap === 'function') refreshInsightsFromMap();
          var m = getMap();
          if (m) { applyColorScale(m, md, getActiveColormap()); showToast('Metric: ' + mInfo.display); }
        }
      });
    } else if (metricSelect) {
      metricSelect.innerHTML = '<option value="">Single metric</option>';
    }
    var colorSelect = document.getElementById('dm-color-scale');
    if (colorSelect) {
      var cm = (mapData && mapData.colormap) || 'YlOrRd';
      if (colorSelect.querySelector('option[value="' + cm + '"]')) colorSelect.value = cm;
      updateColorPreview(colorSelect.value);
      updateColorDirection();
      if (map && mapData) {
        setTimeout(function() { applyColorScale(map, mapData, getActiveColormap()); }, 300);
      }
      colorSelect.addEventListener('change', function() {
        updateColorPreview(this.value);
        updateColorDirection();
        var m = getMap(), d = getMapData();
        if (m && d) { applyColorScale(m, d, getActiveColormap()); showToast('Color scale updated'); }
      });
    }
    var reverseCheck = document.getElementById('dm-color-reverse');
    if (reverseCheck) {
      reverseCheck.addEventListener('change', function() {
        _dmColorReversed = this.checked;
        updateColorDirection();
        var sel = document.getElementById('dm-color-scale');
        if (sel) updateColorPreview(sel.value);
        var m = getMap(), d = getMapData();
        if (m && d) { applyColorScale(m, d, getActiveColormap()); showToast(_dmColorReversed ? 'Colors reversed' : 'Colors normal'); }
      });
    }
    var showLabelsEl = document.getElementById('dm-show-labels');
    var showValuesEl = document.getElementById('dm-show-values');
    if (showLabelsEl) {
      showLabelsEl.checked = getStored('show-labels') === '1';
      showLabelsEl.addEventListener('change', function() {
        setStored('show-labels', this.checked ? '1' : '0');
        applyDisplaySettings();
      });
    }
    if (showValuesEl) {
      showValuesEl.checked = getStored('show-values') === '1';
      showValuesEl.addEventListener('change', function() {
        setStored('show-values', this.checked ? '1' : '0');
        applyDisplaySettings();
      });
    }
    /* Range sliders live update */
    ['dm-fill-opacity','dm-border-weight','dm-border-opacity','dm-font-size','dm-point-radius'].forEach(function(rid) {
      var inp = document.getElementById(rid);
      if (inp) inp.addEventListener('input', updateRangeLabels);
    });
    var pointRadiusEl = document.getElementById('dm-point-radius');
    if (pointRadiusEl) {
      var savedPr = getStored('point-radius');
      if (savedPr && !isNaN(parseFloat(savedPr))) pointRadiusEl.value = savedPr;
      pointRadiusEl.addEventListener('input', function() {
        applyPointMarkerRadius(parseFloat(pointRadiusEl.value));
      });
    }

    var apiKeyInput = document.getElementById('dm-ai-api-key');
    if (apiKeyInput && getStored('openai-api-key')) apiKeyInput.placeholder = 'OpenAI key (saved)';

    if (map) {
      var hash = window.location.hash.slice(1);
      if (hash) {
        try {
          var parts = decodeURIComponent(hash).split(',');
          if (parts.length >= 3) {
            var lat = parseFloat(parts[0]), lon = parseFloat(parts[1]), zoom = parseInt(parts[2], 10);
            if (!isNaN(lat) && !isNaN(lon)) map.setView([lat, lon], isNaN(zoom) ? map.getZoom() : zoom);
          }
        } catch (e) {}
      }
      map.on('moveend', function() {
        var c = map.getCenter();
        var z = map.getZoom();
        window.history.replaceState(null, '', '#' + [c.lat.toFixed(4), c.lng.toFixed(4), z].join(','));
      });
    }
    setNativeLayerControlHidden(getStored('layer-control-hidden') === '1');
    applyDisplaySettings();

    document.addEventListener('keydown', function(e) {
      if (e.key === '?' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        var sidebar = document.getElementById('dm-sidebar');
        if (sidebar) { sidebar.classList.remove('dm-sidebar-closed'); showToast('Press ? to toggle this panel'); }
      }
    });

    var visionCards = document.getElementById('dm-vision-cards');
    if (visionCards) {
      try {
        var saved = JSON.parse(getStored('vision-cards') || '[]');
        var migrated = saved.map(function(c) {
          if (typeof c === 'string') return { title: c, description: '', category: 'Goals' };
          return c;
        });
        if (JSON.stringify(migrated) !== JSON.stringify(saved)) setStored('vision-cards', JSON.stringify(migrated));
        renderVisionCards();
      } catch (e) { renderVisionCards(); }
    }
    var researchNotesEl = document.getElementById('dm-research-notes-text');
    if (researchNotesEl) {
      if (getStored('research-notes')) researchNotesEl.value = getStored('research-notes');
      updateNotesWordCount();
      var researchSaveTimer;
      researchNotesEl.addEventListener('input', function() {
        updateNotesWordCount();
        clearTimeout(researchSaveTimer);
        researchSaveTimer = setTimeout(function() {
          setStored('research-notes', researchNotesEl.value);
          showToast('Notes auto-saved');
        }, 1200);
      });
    }
    renderResearchSources();
    renderHighlights();
    var actionSteps = document.getElementById('dm-action-steps');
    if (actionSteps) {
      try {
        var steps = JSON.parse(getStored('action-steps') || '[]');
        steps.forEach(function(s, idx) {
          var row = document.createElement('label');
          row.className = 'dm-action-step' + (s.done ? ' done' : '');
          var cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.checked = !!s.done;
          cb.addEventListener('change', function() {
            row.classList.toggle('done', cb.checked);
            var d = JSON.parse(getStored('action-steps') || '[]');
            if (d[idx]) d[idx].done = cb.checked;
            setStored('action-steps', JSON.stringify(d));
            if (typeof updateActionHubHero === 'function') updateActionHubHero();
          });
          row.appendChild(cb);
          row.appendChild(document.createTextNode(s.text || ''));
          actionSteps.appendChild(row);
        });
      } catch (e) {}
    }
    if (typeof updateActionHubHero === 'function') updateActionHubHero();
    var brainstormEl = document.getElementById('dm-brainstorm');
    if (brainstormEl && getStored('brainstorm')) brainstormEl.value = getStored('brainstorm');

    if (typeof refreshInsightsFromMap === 'function') refreshInsightsFromMap();

    function buildLayerControls() {
      var container = document.getElementById('dm-layer-controls');
      if (!container) return;
      var m = getMap();
      if (!m) return;
      container.innerHTML = '';
      var layerIdx = 0;
      if (!window._dmMetricLayers) window._dmMetricLayers = {};
      var md = getMapData();
      linkFoliumLayersToMetrics(m, md);
      var cmNames = Object.keys(COLORMAPS);
      var patNames = ['solid', 'stripes', 'dots', 'checkers'];
      var defaultPats = ['solid', 'stripes', 'dots', 'checkers'];
      var defaultCmaps = ['YlOrRd', 'Blues', 'Greens', 'PuOr', 'viridis', 'plasma', 'RdYlGn', 'coolwarm'];

      /* ---- helper: add a UI control row for one layer ---- */
      function addLayerControl(name, layer, isGroup, metricKey, defCmap, defPat, isVisible) {
        var idx = layerIdx++;
        var div = document.createElement('div');
        div.className = 'dm-layer-control';
        var headerDiv = document.createElement('div');
        headerDiv.className = 'dm-layer-control-header';
        var cb = document.createElement('input');
        cb.type = 'checkbox'; cb.checked = !!isVisible;
        headerDiv.appendChild(cb);
        var nameSpan = document.createElement('span');
        nameSpan.className = 'dm-layer-control-name';
        nameSpan.textContent = name;
        headerDiv.appendChild(nameSpan);
        div.appendChild(headerDiv);
        var opts = document.createElement('div');
        opts.className = 'dm-layer-control-options';
        /* color select */
        var colorRow = document.createElement('div');
        colorRow.className = 'dm-layer-control-row';
        var colorLabel = document.createElement('label');
        colorLabel.textContent = 'Color';
        colorRow.appendChild(colorLabel);
        var colorSel = document.createElement('select');
        cmNames.forEach(function(cn) {
          var opt = document.createElement('option');
          opt.value = cn; opt.textContent = cn;
          if (defCmap && cn === defCmap) opt.selected = true;
          colorSel.appendChild(opt);
        });
        colorRow.appendChild(colorSel);
        opts.appendChild(colorRow);
        /* pattern / fill select */
        var patRow = document.createElement('div');
        patRow.className = 'dm-layer-control-row';
        var patLabel = document.createElement('label');
        patLabel.textContent = 'Fill';
        patRow.appendChild(patLabel);
        var patSel = document.createElement('select');
        patNames.forEach(function(pn) {
          var opt = document.createElement('option');
          opt.value = pn; opt.textContent = pn.charAt(0).toUpperCase() + pn.slice(1);
          if (defPat && pn === defPat) opt.selected = true;
          patSel.appendChild(opt);
        });
        patRow.appendChild(patSel);
        opts.appendChild(patRow);
        div.appendChild(opts);
        container.appendChild(div);

        /* --- toggle handler (lazy-creates metric layers) --- */
        cb.addEventListener('change', function() {
          if (cb.checked) {
            /* lazy-create the layer for this metric if it does not exist yet */
            if (metricKey && !window._dmMetricLayers[metricKey]) {
              var mInfo = md && md.metrics ? md.metrics[metricKey] : null;
              var gj = window._dmGeoJsonData;
              if (mInfo && gj) {
                var newL = _createMetricLayer(gj, mInfo.values, mInfo.min, mInfo.max, colorSel.value, patSel.value);
                window._dmMetricLayers[metricKey] = newL;
                layer = newL; /* update closure reference */
              }
            }
            var lyr = metricKey ? window._dmMetricLayers[metricKey] : layer;
            if (lyr && !m.hasLayer(lyr)) m.addLayer(lyr);
          } else {
            var lyr2 = metricKey ? window._dmMetricLayers[metricKey] : layer;
            if (lyr2) m.removeLayer(lyr2);
          }
        });
        /* --- style change handlers --- */
        function onStyleChange() {
          var lyr = metricKey ? window._dmMetricLayers[metricKey] : layer;
          if (lyr) applyLayerColor(lyr, colorSel.value, patSel.value, isGroup, metricKey);
        }
        colorSel.addEventListener('change', onStyleChange);
        patSel.addEventListener('change', onStyleChange);
      }

      /* ---- restyle an existing layer ---- */
      function applyLayerColor(layer, cmName, patName, isGroup, metricKey) {
        var vmin, vmax, vals;
        if (metricKey && md && md.metrics && md.metrics[metricKey]) {
          vmin = md.metrics[metricKey].min;
          vmax = md.metrics[metricKey].max;
          vals = md.metrics[metricKey].values || {};
        } else {
          vmin = md ? md.min : 0; vmax = md ? md.max : 100;
          vals = md ? (md.values || {}) : {};
        }
        function styleL(l) {
          if (!l.setStyle) return;
          var val = resolveValue(l, vals, md && md.key_on);
          var color = (val != null) ? valueToColor(val, vmin, vmax, cmName) : '#dddddd';
          var fill = (patName && patName !== 'solid' && FILL_PATTERNS[patName]) ? FILL_PATTERNS[patName](color) : color;
          l.setStyle({ fillColor: fill || color, fillOpacity: 0.75, color: 'rgba(255,255,255,0.8)', weight: 1 });
        }
        if (isGroup || layer.eachLayer) {
          layer.eachLayer(function(sl) { styleL(sl); });
        } else {
          styleL(layer);
        }
      }

      /* ---- create an L.geoJSON layer for one metric ---- */
      function _createMetricLayer(geoJson, vals, vmin, vmax, cmName, patName) {
        return L.geoJSON(geoJson, {
          style: function(feature) {
            var val = resolveValue({feature: feature}, vals, md && md.key_on);
            var color = (val != null) ? valueToColor(val, vmin, vmax, cmName) : '#dddddd';
            var fill = (patName && patName !== 'solid' && FILL_PATTERNS[patName]) ? FILL_PATTERNS[patName](color) : color;
            return { fillColor: fill || color, color: 'rgba(255,255,255,0.8)', weight: 1, fillOpacity: 0.75 };
          },
          onEachFeature: function(feature, lyr) {
            var np = (md && md.name_property) || 'name';
            var props = feature.properties || {};
            var nm = props[np] || props.name || props.NAME_1 || props.ADMIN || feature.id || '';
            var val = resolveValue({feature: feature}, vals, md && md.key_on);
            var tip = nm;
            if (val != null) tip += ': ' + Number(val).toFixed(2);
            lyr.bindTooltip(tip, {className: 'foliumtooltip'});
          }
        });
      }

      /* =============== Per-metric choropleth layers =============== */
      var hasExistingMetricGroups = false;
      if (md && md.type === 'choropleth' && md.metrics) {
        var wantedMetricNames = {};
        Object.keys(md.metrics).forEach(function(k) {
          var d = md.metrics[k];
          wantedMetricNames[(d && d.display) ? d.display : k] = true;
        });
        m.eachLayer(function(layer) {
          if (
            layer &&
            layer.eachLayer &&
            layer.options &&
            layer.options.name &&
            wantedMetricNames[layer.options.name]
          ) {
            hasExistingMetricGroups = true;
          }
        });
      }

      var geoForLayers = window._dmGeoJsonData || (md && md.geo_json);
      if (md && md.type === 'choropleth' && md.metrics && geoForLayers && !hasExistingMetricGroups) {
        if (!window._dmGeoJsonData && md.geo_json) window._dmGeoJsonData = md.geo_json;

        /* Remove the original single folium GeoJSON layer */
        var toRemove = [];
        m.eachLayer(function(layer) {
          if (layer._url) return;
          if (layer._heat) return;
          if (layer.eachLayer) {
            var hasGeo = false;
            layer.eachLayer(function(sub) { if (sub.feature) hasGeo = true; });
            if (hasGeo) toRemove.push(layer);
          } else if (layer.feature && layer.setStyle) {
            toRemove.push(layer);
          }
        });
        /* keep any previously-created metric layers on the map */
        if (!window._dmMetricLayers) window._dmMetricLayers = {};
        var hadPrev = Object.keys(window._dmMetricLayers).length > 0;
        if (!hadPrev) { toRemove.forEach(function(l) { m.removeLayer(l); }); }

        /* Build a control for each metric */
        var metricKeys = Object.keys(md.metrics);
        metricKeys.forEach(function(mk, idx) {
          var mInfo = md.metrics[mk];
          var cmName = mInfo.colormap || defaultCmaps[idx % defaultCmaps.length];
          var patName = defaultPats[idx % defaultPats.length];

          var existingLayer = window._dmMetricLayers[mk];
          var visible;

          if (hadPrev) {
            /* re-use previous visibility */
            visible = existingLayer ? m.hasLayer(existingLayer) : false;
          } else {
            /* first build: only first metric visible */
            visible = (idx === 0);
            if (visible) {
              if (!existingLayer) {
                existingLayer = _createMetricLayer(window._dmGeoJsonData, mInfo.values, mInfo.min, mInfo.max, cmName, patName);
                window._dmMetricLayers[mk] = existingLayer;
              }
              if (!m.hasLayer(existingLayer)) existingLayer.addTo(m);
            }
          }

          addLayerControl(mInfo.display, existingLayer, true, mk, cmName, patName, visible);
        });

        if (window._dmUserLayer) {
          addLayerControl('Added points', window._dmUserLayer, true, null, 'YlOrRd', 'solid', true);
        }
      } else {
        /* ---- Fallback: discover existing layers (point maps, single-metric, etc.) ---- */
        m.eachLayer(function(layer) {
          if (layer._url) return;
          if (layer._heat) return;
          var name = '';
          if (layer.options && layer.options.name) name = layer.options.name;
          else if (layer._name) name = layer._name;
          if (layer.eachLayer && !layer._url) {
            var count = 0;
            layer.eachLayer(function() { count++; });
            if (count === 0) return;
            if (!name) name = 'Layer (group, ' + count + ' features)';
            addLayerControl(name, layer, true, null, 'YlOrRd', 'solid', true);
          } else if (layer.feature && layer.setStyle) {
            if (!name) name = (layer.feature.properties && layer.feature.properties.name) || 'Feature';
          }
        });
        if (window._dmUserLayer) {
          addLayerControl('Added points', window._dmUserLayer, true, null, 'YlOrRd', 'solid', true);
        }
      }

      if (container.innerHTML === '') {
        if (md && md.type === 'choropleth' && md.metrics && Object.keys(md.metrics).length && !window._dmGeoJsonData && !(md && md.geo_json)) {
          container.innerHTML = '<p style="font-size:11px;color:var(--dm-muted);padding:6px 0;">Boundaries not loaded yet — search for a dataset or preview one to trigger a download, then Refresh.</p>';
        } else {
          container.innerHTML = '<p style="font-size:11px;color:var(--dm-muted);padding:6px 0;">No data layers found. Add a dataset from the Search tab.</p>';
        }
      }
    }
    /* Show default search results on load */
    try {
      var initResults = rankDatasetCandidates('');
      if (typeof renderFindDataResults === 'function' && initResults && initResults.length) {
        renderFindDataResults(initResults.slice(0, 12));
      }
    } catch (eInit) {}
    bootMapDataLayers();
    window.bootMapDataLayers = bootMapDataLayers;
    window.buildLayerControls = buildLayerControls;
    /* Wire quick-search buttons via data attribute delegation from body listener */
    window._dmRunQuickSearch = function(q) {
      var searchInput = document.getElementById('dm-finddata-query');
      if (searchInput) searchInput.value = q;
      openSidebarIfClosed();
      activateMainTab('data');
      window._dmAiExtraDatasets = [];
      var qNorm = normalizeQuery(q);
      var inferred = inferGeoProfileFromQuery(qNorm);
      function doQuickSearch() {
        var results = rankDatasetCandidates(' ' + qNorm + ' ');
        renderFindDataResults(results);
        /* scroll sidebar to top so results are visible */
        var sidebarContent = document.querySelector('#dm-sidebar .dm-sidebar-content');
        if (sidebarContent) sidebarContent.scrollTo({ top: 0, behavior: 'smooth' });
        /* Auto-trigger AI if key available */
        var storedKey = getStored('openai-api-key');
        if (storedKey) {
          setTimeout(function() {
            var aiBtn = document.getElementById('dm-btn-ai-datasets');
            if (aiBtn && !aiBtn.disabled) { aiBtn.click(); }
          }, 300);
        }
      }
      if (inferred) applyInferredGeoProfile(qNorm, inferred).then(doQuickSearch).catch(doQuickSearch);
      else doQuickSearch();
    };
    wireCsvUploadInputs();
    var addDataBtn = document.getElementById('dm-btn-add-data');
    if (addDataBtn) addDataBtn.addEventListener('click', function() { setTimeout(buildLayerControls, 300); });

    function onActionAddVision() {
      var inp = document.getElementById('dm-vision-input');
      var descEl = document.getElementById('dm-vision-desc');
      var catEl = document.getElementById('dm-vision-category');
      var priEl = document.getElementById('dm-vision-priority');
      var title = (inp && inp.value && inp.value.trim()) || '';
      if (!title) return;
      var description = (descEl && descEl.value && descEl.value.trim()) || '';
      var category = (catEl && catEl.value) || 'Goals';
      var priority = (priEl && priEl.value) || 'medium';
      var list = JSON.parse(getStored('vision-cards') || '[]');
      if (window._dmVisionEditIndex != null && !isNaN(window._dmVisionEditIndex) && list[window._dmVisionEditIndex]) {
        list[window._dmVisionEditIndex] = { title: title, description: description, category: category, priority: priority };
        window._dmVisionEditIndex = null;
        showToast('Card updated');
      } else {
        list.push({ title: title, description: description, category: category, priority: priority });
        showToast('Card added');
      }
      setStored('vision-cards', JSON.stringify(list));
      if (inp) inp.value = '';
      if (descEl) descEl.value = '';
      if (catEl) catEl.value = 'Goals';
      if (priEl) priEl.value = 'medium';
      renderVisionCards();
    }
    function onActionAddStep() {
      var inp = document.getElementById('dm-action-input');
      var text = (inp && inp.value && inp.value.trim()) || '';
      if (!text) return;
      if (appendActionStep(text)) {
        if (inp) inp.value = '';
        showToast('Step added');
      }
    }
    var btnVision = document.getElementById('dm-btn-add-vision');
    if (btnVision) btnVision.addEventListener('click', onActionAddVision);
    var btnStep = document.getElementById('dm-btn-add-step');
    if (btnStep) btnStep.addEventListener('click', onActionAddStep);
    var actInputEl = document.getElementById('dm-action-input');
    if (actInputEl) actInputEl.addEventListener('keydown', function(ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); onActionAddStep(); }
    });
    var btnBrainstorm = document.getElementById('dm-btn-save-brainstorm');
    if (btnBrainstorm) btnBrainstorm.addEventListener('click', function() {
      var ta = document.getElementById('dm-brainstorm');
      if (ta) { setStored('brainstorm', ta.value); showToast('Brainstorm saved'); }
    });
    var btnCopyView = document.getElementById('dm-btn-copy-view');
    if (btnCopyView) btnCopyView.addEventListener('click', function() {
      var map = getMap();
      if (map) {
        var c = map.getCenter();
        var z = map.getZoom();
        var link = window.location.origin + window.location.pathname + '#' + [c.lat.toFixed(4), c.lng.toFixed(4), z].join(',');
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(link).then(function() { showToast('View link copied'); });
        }
      }
    });

    } catch (err) { console.error('Data Mapper UI:', err); }
  });
})();
</script>
"""
    )


# ---- Social impact: insights and recommendations ----

def _insights_to_json_serializable(insights: dict) -> dict:
    """Convert insights dict to JSON-serializable form (DataFrames -> list of dicts)."""
    out = {}
    for k, v in insights.items():
        if not isinstance(v, dict):
            continue
        row = {}
        for key, val in v.items():
            if hasattr(val, "to_dict"):
                row[key] = val.to_dict(orient="records") if not val.empty else []
            elif isinstance(val, (pd.DataFrame, pd.Series)):
                row[key] = val.to_list() if hasattr(val, "to_list") else []
            else:
                row[key] = val
        out[k] = row
    return out


def compute_insights(
    df: pd.DataFrame,
    value_columns: list[str],
    benchmark: float | None = None,
    priority_quartile: bool = True,
) -> dict:
    """Compute key takeaways, priority recommendations, key message, and inequality snapshot for social impact."""
    insights = {}
    key_messages = []
    for metric in value_columns:
        if metric not in df.columns:
            continue
        s = df[metric].dropna()
        if s.empty or len(s) < 2:
            continue
        rank_df = df[["location", metric]].drop_duplicates(subset=["location"], keep="first").dropna(subset=[metric])
        rank_df = rank_df.sort_values(metric, ascending=False).reset_index(drop=True)
        n = len(rank_df)
        if n == 0:
            continue
        vmin, vmax = float(s.min()), float(s.max())
        median_val = float(s.median())
        mean_val = float(s.mean())
        std_val = float(s.std(ddof=0)) if len(s) > 1 else 0.0
        cv_ratio = (std_val / mean_val) if mean_val not in (0, 0.0) else None
        disparity = (vmax / vmin) if vmin and vmin > 0 else None
        top5 = rank_df.head(5)
        bottom5 = rank_df.tail(5)
        q1 = rank_df[metric].quantile(0.25)
        q3 = rank_df[metric].quantile(0.75)
        iqr = float(q3 - q1) if q3 is not None and q1 is not None else None
        below_median = rank_df[rank_df[metric] < median_val]
        below_q1 = rank_df[rank_df[metric] <= q1]
        if benchmark is not None:
            below_benchmark = rank_df[rank_df[metric] < benchmark]
        else:
            below_benchmark = None
        priority = below_q1.head(15) if priority_quartile else below_median.head(15)
        below_median_pct = (len(below_median) / n * 100) if n else 0
        # Simple inequality: share of locations in bottom quartile (higher = more inequality of outcomes)
        bottom_quartile_pct = (len(below_q1) / n * 100) if n else 0
        name_display = metric.replace("_", " ").title()
        # One-line key message for advocacy
        if disparity and disparity > 2:
            key_messages.append(
                f"{name_display} varies {disparity:.1f}x across {n} locations; "
                f"{len(priority)} priority areas for support."
            )
        else:
            key_messages.append(
                f"{name_display}: range {vmin:.1f}-{vmax:.1f} across {n} locations; "
                f"focus on {len(priority)} areas."
            )
        insights[metric] = {
            "n": n,
            "min": vmin,
            "max": vmax,
            "median": median_val,
            "mean": mean_val,
            "std": std_val,
            "cv_ratio": cv_ratio,
            "disparity_ratio": disparity,
            "top5": top5,
            "bottom5": bottom5,
            "below_median": below_median,
            "priority_list": priority,
            "below_benchmark": below_benchmark,
            "benchmark": benchmark,
            "metric_display": name_display,
            "q1": float(q1) if q1 is not None else None,
            "q3": float(q3) if q3 is not None else None,
            "iqr": iqr,
            "below_median_pct": round(below_median_pct, 1),
            "bottom_quartile_pct": round(bottom_quartile_pct, 1),
        }
    # Cross-metric correlations (keep only strongest, meaningful relationships)
    correlations = []
    numeric_cols = [m for m in value_columns if m in df.columns and np.issubdtype(df[m].dtype, np.number)]
    if len(numeric_cols) >= 2:
        try:
            for i, m1 in enumerate(numeric_cols):
                for m2 in numeric_cols[i + 1 :]:
                    pair_df = df[[m1, m2]].dropna()
                    n_pairs = len(pair_df)
                    if n_pairs < 10:
                        continue
                    if pair_df[m1].nunique() < 2 or pair_df[m2].nunique() < 2:
                        continue
                    c = pair_df[m1].corr(pair_df[m2])
                    if pd.isna(c):
                        continue
                    abs_c = abs(float(c))
                    if abs_c < 0.45:
                        continue
                    if abs_c >= 0.75:
                        strength = "strong"
                    elif abs_c >= 0.60:
                        strength = "moderate"
                    else:
                        strength = "notable"
                    correlations.append(
                        {
                            "metric1": m1,
                            "metric2": m2,
                            "metric1_display": m1.replace("_", " ").title(),
                            "metric2_display": m2.replace("_", " ").title(),
                            "correlation": round(float(c), 3),
                            "direction": "positive" if c > 0 else "negative",
                            "strength": strength,
                            "n": int(n_pairs),
                        }
                    )
            correlations = sorted(correlations, key=lambda x: abs(x["correlation"]), reverse=True)[:8]
            for corr in correlations[:2]:
                key_messages.append(
                    f"{corr['metric1_display']} and {corr['metric2_display']} show a {corr['strength']} "
                    f"{corr['direction']} relationship (r~{corr['correlation']:.2f}, n={corr['n']})."
                )
        except Exception:
            pass
    return {
        "metrics": insights,
        "key_messages": key_messages[:5],
        "correlations": correlations,
    }


def build_sidebar_html(insights: dict | None, title: str = "Data map", has_map_data: bool = False) -> str:
    """Build the sidebar HTML."""
    p = []
    p.append('<button type="button" class="dm-sidebar-toggle" id="dm-sidebar-toggle" aria-label="Toggle panel">')
    p.append('<span class="dm-toggle-icon">&#9776;</span><span class="dm-toggle-label">Close</span></button>')
    p.append('<div class="dm-sidebar-content">')
    p.append('  <div class="dm-sidebar-header">')
    p.append('    <span class="dm-sidebar-appname">Data Mapper</span>')
    p.append('    <span class="dm-sidebar-mapname" id="dm-map-title-sidebar">' + _escape_attr(title) + '</span>')
    p.append('  </div>')

    # Main tabs
    p.append('  <div class="dm-sidebar-tabs" role="tablist">')
    p.append('    <button type="button" class="dm-tab active" data-tab="data" aria-selected="true">Search</button>')
    p.append('    <button type="button" class="dm-tab" data-tab="insights" aria-selected="false">Analysis</button>')
    p.append('    <button type="button" class="dm-tab" data-tab="settings" aria-selected="false">Layers</button>')
    p.append('    <button type="button" class="dm-tab" data-tab="action" aria-selected="false">Plan</button>')
    p.append('    <button type="button" class="dm-tab" data-tab="research" aria-selected="false">Research</button>')
    p.append('  </div>')

    # ── DATA / SEARCH TAB ──────────────────────────────────────────
    p.append('  <div id="dm-panel-data" class="dm-tab-panel active" role="tabpanel">')

    p.append('    <div id="dm-fetch-banner" class="dm-fetch-banner" style="display:none;">')
    p.append('      Opened as <code>file://</code> — live data fetches may fail. Run <code>python3 data-mapping.py --serve</code> and open the <code>http://127.0.0.1:8080</code> link.</div>')

    # Search
    p.append('    <div class="dm-section">')
    p.append('      <div id="dm-search-wrap">')
    p.append('        <input type="search" id="dm-finddata-query" placeholder="Search: child mortality, GDP, India states, CO₂..." autocomplete="off" />')
    p.append('        <button type="button" id="dm-btn-finddata-search">Search</button>')
    p.append('        <button type="button" id="dm-btn-ai-datasets" title="AI-powered dataset suggestions (needs OpenAI key)">✨ AI</button>')
    p.append('      </div>')
    p.append('      <div id="dm-finddata-results" class="dm-finddata-results"></div>')
    p.append('    </div>')

    # Geography
    p.append('    <div class="dm-section">')
    p.append('      <div class="dm-section-label">Map geography</div>')
    p.append('      <select id="dm-geo-profile" style="margin-bottom:6px;">')
    p.append('        <option value="countries">World — countries (Natural Earth)</option>')
    p.append('        <option value="admin1_global">World — states / provinces (Natural Earth)</option>')
    p.append('      </select>')
    p.append('      <div id="dm-admin1-focus-wrap" style="display:none;margin-top:6px;">')
    p.append('        <select id="dm-admin1-focus">')
    p.append('          <option value="">All countries (world map)</option>')
    p.append('        </select>')
    p.append('      </div>')
    p.append('    </div>')

    # Sub-tabs for data input
    p.append('    <div class="dm-sub-tabs" data-for="data">')
    p.append('      <button type="button" class="dm-sub-tab active" data-sub="discover">Discover</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="regions">Regions CSV</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="points">Points CSV</button>')
    p.append('    </div>')

    # Discover sub-panel (active datasets list)
    p.append('    <div id="dm-data-discover" class="dm-sub-panel active">')
    p.append('      <p class="dm-hint" style="margin-bottom:8px;">Click a topic below to instantly add data to the map, or type a search. The built-in datasets cover <em>all countries</em> with real World Bank data.</p>')
    p.append('      <div style="margin-bottom:10px;">')
    p.append('        <div class="dm-section-label" style="margin-bottom:6px;">Quick starts</div>')
    p.append('        <div style="display:flex;flex-wrap:wrap;gap:5px;">')
    p.append('          <button type="button" class="dm-quick-search-btn" id="dm-qs-life" data-q="life expectancy">Life expectancy</button>')
    p.append('          <button type="button" class="dm-quick-search-btn" id="dm-qs-child" data-q="child mortality">Child mortality</button>')
    p.append('          <button type="button" class="dm-quick-search-btn" id="dm-qs-gdp" data-q="GDP per capita">GDP per capita</button>')
    p.append('          <button type="button" class="dm-quick-search-btn" id="dm-qs-poverty" data-q="poverty">Poverty</button>')
    p.append('          <button type="button" class="dm-quick-search-btn" id="dm-qs-co2" data-q="CO2 emissions">CO₂ emissions</button>')
    p.append('          <button type="button" class="dm-quick-search-btn" id="dm-qs-india" data-q="India states">India states</button>')
    p.append('          <button type="button" class="dm-quick-search-btn" id="dm-qs-us" data-q="US states">US states</button>')
    p.append('          <button type="button" class="dm-quick-search-btn" id="dm-qs-canada" data-q="Canada provinces">Canada</button>')
    p.append('        </div>')
    p.append('      </div>')
    p.append('    </div>')

    # Regions CSV sub-panel
    p.append('    <div id="dm-data-regions" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Upload region CSV</div>')
    p.append('        <p class="dm-hint">Header required. Keys: <code>US-CA</code> (admin-1 ISO 3166-2), <code>USA</code> (ISO3), or region names.</p>')
    p.append('        <div class="dm-csv-upload-row">')
    p.append('          <input type="file" id="dm-region-csv-file" accept=".csv,.tsv,.txt,text/csv" />')
    p.append('          <button type="button" class="dm-btn-primary-style" id="dm-btn-region-upload-add">Upload &amp; add</button>')
    p.append('        </div>')
    p.append('        <textarea id="dm-region-csv" placeholder="code,value&#10;US-CA,72&#10;US-NY,81"></textarea>')
    p.append('        <div class="dm-section-label" style="margin-top:8px;">Column hints (optional)</div>')
    p.append('        <input type="text" id="dm-region-iso-col" class="dm-ai-key-input" placeholder="Region column (e.g. code, state)" style="margin-top:4px;" />')
    p.append('        <input type="text" id="dm-region-value-col" class="dm-ai-key-input" placeholder="Value column (auto-detected if empty)" style="margin-top:6px;" />')
    p.append('        <input type="text" id="dm-region-year-col" class="dm-ai-key-input" placeholder="Year column (optional)" style="margin-top:6px;" />')
    p.append('        <select id="dm-region-key-mode" class="dm-ai-key-input" style="margin-top:6px;">')
    p.append('          <option value="auto">Auto-detect key type</option>')
    p.append('          <option value="iso3166_2">ISO 3166-2 (states/provinces)</option>')
    p.append('          <option value="iso3">ISO3 (countries)</option>')
    p.append('          <option value="name">Names (match boundary labels)</option>')
    p.append('        </select>')
    p.append('        <select id="dm-region-year-select" class="dm-ai-key-input" style="margin-top:6px;display:none;"></select>')
    p.append('        <div class="dm-add-from-web" style="margin-top:8px;">')
    p.append('          <input type="url" id="dm-region-csv-url" class="dm-url-input" placeholder="Or paste a CSV URL" />')
    p.append('          <button type="button" class="dm-btn-secondary-style" id="dm-btn-region-fetch-url">Load from URL</button>')
    p.append('        </div>')
    p.append('        <button type="button" class="dm-btn-secondary-style" id="dm-btn-ai-generate-region-csv" style="margin-top:6px;width:100%;">✨ AI: generate region CSV</button>')
    p.append('        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;">')
    p.append('          <button type="button" class="dm-btn-secondary-style" id="dm-btn-region-preview">Preview</button>')
    p.append('          <button type="button" class="dm-btn-primary-style" id="dm-btn-region-add">Add to map</button>')
    p.append('          <button type="button" class="dm-btn-secondary-style" id="dm-btn-region-add-layer">Add as layer</button>')
    p.append('        </div>')
    p.append('        <div id="dm-region-import-status" style="margin-top:8px;font-size:11px;color:var(--dm-muted);"></div>')
    p.append('      </div>')
    p.append('    </div>')

    # Points CSV sub-panel
    p.append('    <div id="dm-data-points" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Add point locations</div>')
    p.append('        <p class="dm-hint">Headers required. Needs <code>lat</code> and <code>lon</code> columns. Optional: <code>name</code>, <code>value</code>.</p>')
    p.append('        <div class="dm-csv-upload-row">')
    p.append('          <input type="file" id="dm-add-data-csv-file" accept=".csv,.tsv,.txt,text/csv" />')
    p.append('          <button type="button" class="dm-btn-primary-style" id="dm-btn-points-upload-add">Upload &amp; add</button>')
    p.append('        </div>')
    p.append('        <textarea id="dm-add-data-csv" placeholder="lat,lon,name,value&#10;40.7,-74.0,New York,85"></textarea>')
    p.append('        <div class="dm-add-from-web">')
    p.append('          <input type="url" id="dm-add-data-url" class="dm-url-input" placeholder="Or paste a CSV URL" />')
    p.append('          <button type="button" class="dm-btn-secondary-style" id="dm-btn-fetch-data">Load from URL</button>')
    p.append('        </div>')
    p.append('        <button type="button" class="dm-btn-primary-style" id="dm-btn-add-data" style="width:100%;">Add points to map</button>')
    p.append('      </div>')
    p.append('    </div>')

    p.append('  </div>')  # end #dm-panel-data

    # ── ANALYSIS / INSIGHTS TAB ────────────────────────────────────
    p.append('  <div id="dm-panel-insights" class="dm-tab-panel" role="tabpanel">')
    p.append('    <div class="dm-sub-tabs" data-for="insights">')
    p.append('      <button type="button" class="dm-sub-tab active" data-sub="takeaways">Overview</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="statistics">Charts</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="ask">Ask AI</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="goal">Goal</button>')
    p.append('    </div>')

    # Takeaways sub-panel with AI overview
    p.append('    <div id="dm-insights-takeaways" class="dm-sub-panel active">')
    p.append('      <div id="dm-ai-overview-wrap" style="display:none;">')
    p.append('        <div class="dm-overview-label">✨ AI Overview</div>')
    p.append('        <div id="dm-ai-overview-text"></div>')
    p.append('        <button type="button" class="dm-btn-secondary-style" id="dm-btn-regenerate-overview" style="margin-top:8px;width:100%;font-size:11px;">Regenerate</button>')
    p.append('      </div>')
    p.append('      <div id="dm-insights-live-root" class="dm-insights-live-root"></div>')
    p.append('    </div>')

    # Statistics / Charts sub-panel
    p.append('    <div id="dm-insights-statistics" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Charts from map data</div>')
    p.append('        <p class="dm-hint">Bar chart shows top values. Histogram shows distribution. With 2+ layers, scatter plot compares correlation.</p>')
    p.append('        <button type="button" class="dm-btn-secondary-style" id="dm-btn-refresh-statistics" style="width:100%;margin-bottom:8px;">Refresh charts</button>')
    p.append('        <div id="dm-statistics-charts-root"></div>')
    p.append('      </div>')
    p.append('    </div>')

    # Ask AI sub-panel
    p.append('    <div id="dm-insights-ask" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Ask about the data</div>')
    p.append('        <p class="dm-hint">Instant answers use map stats. For narrative AI responses, add an OpenAI key. Run with <code>--serve</code> for the relay to work.</p>')
    p.append('        <input type="password" id="dm-ai-api-key" class="dm-ai-key-input" placeholder="OpenAI API key (optional)" autocomplete="off" style="margin-bottom:8px;" />')
    p.append('        <select id="dm-ai-mode" class="dm-ai-key-input" style="margin-bottom:8px;">')
    p.append('          <option value="observations">Stats only (no OpenAI)</option>')
    p.append('          <option value="llm">AI response (OpenAI)</option>')
    p.append('          <option value="both" selected>Stats + AI</option>')
    p.append('        </select>')
    p.append('        <div class="dm-suggested-questions">')
    p.append('          <button type="button" class="dm-q-btn" data-q="What is the key message?">Key message</button>')
    p.append('          <button type="button" class="dm-q-btn" data-q="Which locations need the most support?">Most vulnerable</button>')
    p.append('          <button type="button" class="dm-q-btn" data-q="What is the range and disparity?">Disparity</button>')
    p.append('          <button type="button" class="dm-q-btn" data-q="Summarize for a grant proposal">Grant summary</button>')
    p.append('          <button type="button" class="dm-q-btn" data-q="What interventions would help the most?">Interventions</button>')
    p.append('          <button type="button" class="dm-q-btn" data-q="Find correlations between all datasets on the map">Find correlations</button>')
    p.append('        </div>')
    p.append('        <button type="button" class="dm-btn-secondary-style" id="dm-btn-generate-observations" style="width:100%;margin-top:8px;">Generate observations</button>')
    p.append('        <button type="button" class="dm-btn-secondary-style" id="dm-btn-clear-chat" style="width:100%;margin-top:5px;">Clear</button>')
    p.append('        <textarea id="dm-ai-question" class="dm-ai-question-input" placeholder="Ask anything about the map data..." rows="2" style="margin-top:8px;"></textarea>')
    p.append('        <button type="button" class="dm-btn-primary-style" id="dm-btn-ask" style="width:100%;">Ask</button>')
    p.append('        <div id="dm-ai-answer" class="dm-ai-answer"></div>')
    p.append('      </div>')
    p.append('    </div>')

    # Goal & share
    p.append('    <div id="dm-insights-goal" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Your goal</div>')
    p.append('        <p class="dm-your-goal" id="dm-your-goal"><strong>—</strong> Set in welcome</p>')
    p.append('        <button type="button" class="dm-btn-secondary-style" id="dm-btn-copy-key-message" style="margin-top:6px;width:100%;">Copy key message</button>')
    p.append('      </div>')
    p.append('      <div class="dm-section dm-reflection-wrap">')
    p.append('        <div class="dm-section-label">How will you use this map?</div>')
    p.append('        <textarea id="dm-reflection" placeholder="e.g. Annual report to show regional disparities..."></textarea>')
    p.append('        <button type="button" class="dm-btn-primary-style" id="dm-btn-save-reflection" style="width:100%;">Save note</button>')
    p.append('      </div>')
    p.append('      <button type="button" class="dm-btn-primary-style" id="dm-btn-share-impact" style="width:100%;margin-top:6px;">Share for impact</button>')
    p.append('      <p class="dm-share-note">Share this map to advocate for change or include in reports and grant proposals.</p>')
    p.append('    </div>')

    p.append('  </div>')  # end #dm-panel-insights

    # ── LAYERS / SETTINGS TAB ──────────────────────────────────────
    p.append('  <div id="dm-panel-settings" class="dm-tab-panel" role="tabpanel">')
    p.append('    <div class="dm-sub-tabs" data-for="settings">')
    p.append('      <button type="button" class="dm-sub-tab active" data-sub="layers">Layers</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="colors">Colors</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="display">Display</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="basemap">Basemap</button>')
    p.append('    </div>')

    # Layers sub-panel
    p.append('    <div id="dm-settings-layers" class="dm-sub-panel active">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Data layers</div>')
    p.append('        <p class="dm-hint">Toggle datasets, change color ramps and fill patterns. After adding data, click <strong>Refresh</strong> if the list is empty.</p>')
    p.append('        <div id="dm-layer-controls"></div>')
    p.append('        <div style="display:flex;gap:6px;margin-top:8px;">')
    p.append('          <button type="button" class="dm-btn-secondary-style" id="dm-btn-refresh-layers" style="flex:1;">Refresh</button>')
    p.append('          <button type="button" class="dm-btn-secondary-style" id="dm-btn-hide-layers" style="flex:1;">Hide all</button>')
    p.append('        </div>')
    p.append('      </div>')
    p.append('    </div>')

    # Colors sub-panel
    p.append('    <div id="dm-settings-colors" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Color scale</div>')
    p.append('        <div class="dm-settings-row" style="margin-bottom:8px;">')
    p.append('          <label class="dm-settings-label">Metric</label>')
    p.append('          <select class="dm-color-select" id="dm-metric-select"><option value="">Loading...</option></select>')
    p.append('        </div>')
    p.append('        <div id="dm-color-preview" class="dm-color-preview"></div>')
    p.append('        <div class="dm-color-direction" id="dm-color-direction"></div>')
    p.append('        <select class="dm-color-select" id="dm-color-scale">')
    p.append('          <option value="YlOrRd">Yellow → Orange → Red</option>')
    p.append('          <option value="viridis">Viridis</option>')
    p.append('          <option value="plasma">Plasma</option>')
    p.append('          <option value="Blues">Blues</option>')
    p.append('          <option value="Greens">Greens</option>')
    p.append('          <option value="RdYlGn">Red → Yellow → Green</option>')
    p.append('          <option value="PuOr">Purple ↔ Orange</option>')
    p.append('          <option value="coolwarm">Cool ↔ Warm</option>')
    p.append('        </select>')
    p.append('        <label class="dm-settings-toggle" style="margin-top:8px;">')
    p.append('          <input type="checkbox" id="dm-color-reverse" /> <span>Reverse (flip high/low)</span>')
    p.append('        </label>')
    p.append('        <div class="dm-settings-row" style="margin-top:8px;">')
    p.append('          <label class="dm-settings-label">Opacity</label>')
    p.append('          <input type="range" id="dm-fill-opacity" min="0.1" max="1" step="0.05" value="0.75" class="dm-range-input" />')
    p.append('          <span id="dm-fill-opacity-val" class="dm-range-val">75%</span>')
    p.append('        </div>')
    p.append('        <button type="button" class="dm-btn-primary-style" id="dm-btn-apply-colors" style="margin-top:10px;width:100%;">Apply</button>')
    p.append('      </div>')
    p.append('    </div>')

    # Display sub-panel
    p.append('    <div id="dm-settings-display" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Display options</div>')
    p.append('        <div class="dm-settings-row">')
    p.append('          <label class="dm-settings-label">Border weight</label>')
    p.append('          <input type="range" id="dm-border-weight" min="0" max="5" step="0.5" value="1" class="dm-range-input" />')
    p.append('          <span id="dm-border-weight-val" class="dm-range-val">1</span>')
    p.append('        </div>')
    p.append('        <div class="dm-settings-row">')
    p.append('          <label class="dm-settings-label">Border color</label>')
    p.append('          <input type="color" id="dm-border-color" value="#ffffff" class="dm-color-input" />')
    p.append('        </div>')
    p.append('        <div class="dm-settings-row">')
    p.append('          <label class="dm-settings-label">Border opacity</label>')
    p.append('          <input type="range" id="dm-border-opacity" min="0" max="1" step="0.1" value="0.8" class="dm-range-input" />')
    p.append('          <span id="dm-border-opacity-val" class="dm-range-val">80%</span>')
    p.append('        </div>')
    p.append('        <hr class="dm-settings-divider" />')
    p.append('        <div class="dm-settings-row">')
    p.append('          <label class="dm-settings-label">Point dot size (lat/lon)</label>')
    p.append('          <input type="range" id="dm-point-radius" min="3" max="40" step="1" value="10" class="dm-range-input" />')
    p.append('          <span id="dm-point-radius-val" class="dm-range-val">10px</span>')
    p.append('        </div>')
    p.append('        <hr class="dm-settings-divider" />')
    p.append('        <label class="dm-settings-toggle"><input type="checkbox" id="dm-show-labels" /> <span>Show labels on hover</span></label>')
    p.append('        <label class="dm-settings-toggle"><input type="checkbox" id="dm-show-values" /> <span>Show values in tooltips</span></label>')
    p.append('        <div style="display:flex;gap:6px;margin-top:10px;">')
    p.append('          <button type="button" class="dm-btn-primary-style" id="dm-btn-apply-display" style="flex:1;">Apply</button>')
    p.append('          <button type="button" class="dm-btn-secondary-style" id="dm-btn-reset-display" style="flex:1;">Reset</button>')
    p.append('        </div>')
    p.append('      </div>')
    p.append('    </div>')

    # Basemap sub-panel
    p.append('    <div id="dm-settings-basemap" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Base map</div>')
    p.append('        <div class="dm-basemap-cards">')
    p.append('          <button type="button" class="dm-basemap-card" data-basemap="voyager">Voyager<span class="dm-basemap-desc">Clean, modern</span></button>')
    p.append('          <button type="button" class="dm-basemap-card" data-basemap="light">Positron<span class="dm-basemap-desc">Light, minimal</span></button>')
    p.append('          <button type="button" class="dm-basemap-card" data-basemap="dark">Dark Matter<span class="dm-basemap-desc">Dark theme</span></button>')
    p.append('        </div>')
    p.append('      </div>')
    p.append('    </div>')

    p.append('  </div>')  # end #dm-panel-settings

    # ── ACTION / PLAN TAB ──────────────────────────────────────────
    p.append('  <div id="dm-panel-action" class="dm-tab-panel dm-action-hub" role="tabpanel">')
    p.append('    <div class="dm-sub-tabs dm-action-subtabs" data-for="action">')
    p.append('      <button type="button" class="dm-sub-tab active" data-sub="plan">Checklist</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="board">Vision board</button>')
    p.append('    </div>')

    # Action plan sub-panel
    p.append('    <div id="dm-action-plan" class="dm-sub-panel active">')
    p.append('      <div class="dm-action-hub-hero">')
    p.append('        <span class="dm-action-hub-badge">Action workspace</span>')
    p.append('        <p class="dm-action-hub-tagline">Turn the map into <em>real follow-through</em></p>')
    p.append('        <div id="dm-action-goal-line" class="dm-action-goal-line">Set a goal in welcome to track your purpose here.</div>')
    p.append('        <div class="dm-action-progress-meta">')
    p.append('          <span id="dm-action-progress-label">Add steps below</span>')
    p.append('          <span id="dm-action-board-count" aria-live="polite"></span>')
    p.append('        </div>')
    p.append('        <div id="dm-action-progress-bar" class="dm-action-progress-bar" role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100">')
    p.append('          <span id="dm-action-progress-fill" class="dm-action-progress-fill"></span>')
    p.append('        </div>')
    p.append('      </div>')
    p.append('      <div class="dm-action-starters">')
    p.append('        <span class="dm-action-starters-label">Quick-add steps</span>')
    p.append('        <div class="dm-action-starter-row">')
    p.append('          <button type="button" class="dm-action-starter" data-dm-starter="Write a one-paragraph summary of what the map shows">Stakeholder summary</button>')
    p.append('          <button type="button" class="dm-action-starter" data-dm-starter="List 3 policy actions implied by the data">3 policy moves</button>')
    p.append('          <button type="button" class="dm-action-starter" data-dm-starter="Identify top 2 priority geographies and why">Priority places</button>')
    p.append('          <button type="button" class="dm-action-starter" data-dm-starter="Draft metrics to track over the next 90 days">90-day metrics</button>')
    p.append('        </div>')
    p.append('      </div>')
    p.append('      <div class="dm-action-steps-shell">')
    p.append('        <div class="dm-action-section-head"><span class="dm-action-section-icon">1</span><div class="dm-section-label">Checklist</div></div>')
    p.append('        <div id="dm-action-steps"></div>')
    p.append('        <input type="text" id="dm-action-input" class="dm-action-input" placeholder="Add a concrete step..." />')
    p.append('        <button type="button" class="dm-btn-primary-style" id="dm-btn-add-step" style="width:100%;">Add step</button>')
    p.append('      </div>')
    p.append('      <div class="dm-action-scratch-shell">')
    p.append('        <div class="dm-action-section-head"><span class="dm-action-section-icon">2</span><div class="dm-section-label">Scratchpad</div></div>')
    p.append('        <textarea id="dm-brainstorm" class="dm-brainstorm-input" placeholder="Notes, risks, narrative angles..."></textarea>')
    p.append('        <button type="button" class="dm-btn-primary-style" id="dm-btn-save-brainstorm" style="width:100%;">Save notes</button>')
    p.append('      </div>')
    p.append('      <div class="dm-action-share-shell">')
    p.append('        <div class="dm-action-section-head"><span class="dm-action-section-icon">3</span><div class="dm-section-label">Share</div></div>')
    p.append('        <button type="button" class="dm-btn-primary-style" id="dm-btn-copy-view" style="width:100%;">Copy current view link</button>')
    p.append('        <button type="button" class="dm-btn-secondary-style" id="dm-btn-export-action-plan" style="margin-top:6px;width:100%;">Export action plan</button>')
    p.append('      </div>')
    p.append('    </div>')

    # Vision board sub-panel
    p.append('    <div id="dm-action-board" class="dm-sub-panel">')
    p.append('      <div class="dm-action-board-wrap">')
    p.append('        <div class="dm-vision-board-section">')
    p.append('          <div class="dm-vision-stats" id="dm-vision-stats"></div>')
    p.append('          <div class="dm-vision-filters" id="dm-vision-filters">')
    p.append('            <button type="button" class="dm-vision-filter active" data-filter="all">All</button>')
    p.append('            <button type="button" class="dm-vision-filter" data-filter="Goals">Goals</button>')
    p.append('            <button type="button" class="dm-vision-filter" data-filter="Ideas">Ideas</button>')
    p.append('            <button type="button" class="dm-vision-filter" data-filter="In Progress">In Progress</button>')
    p.append('            <button type="button" class="dm-vision-filter" data-filter="Done">Done</button>')
    p.append('          </div>')
    p.append('          <div id="dm-vision-cards" class="dm-vision-board-cards"></div>')
    p.append('          <div class="dm-vision-add-row">')
    p.append('            <input type="text" id="dm-vision-input" class="dm-vision-input" placeholder="Card title..." />')
    p.append('            <textarea id="dm-vision-desc" class="dm-vision-desc" rows="2" placeholder="Description (optional)"></textarea>')
    p.append('            <div style="display:flex;gap:6px;">')
    p.append('              <select id="dm-vision-category" class="dm-vision-category" style="flex:1;">')
    p.append('                <option value="Goals">Goals</option>')
    p.append('                <option value="Ideas">Ideas</option>')
    p.append('                <option value="In Progress">In Progress</option>')
    p.append('                <option value="Done">Done</option>')
    p.append('                <option value="Later">Later</option>')
    p.append('              </select>')
    p.append('              <select id="dm-vision-priority" class="dm-vision-category" style="flex:1;">')
    p.append('                <option value="medium">Medium</option>')
    p.append('                <option value="high">High</option>')
    p.append('                <option value="low">Low</option>')
    p.append('              </select>')
    p.append('            </div>')
    p.append('          </div>')
    p.append('          <div style="display:flex;gap:6px;margin-top:8px;">')
    p.append('            <button type="button" class="dm-btn-primary-style" id="dm-btn-add-vision" style="flex:1;">Add card</button>')
    p.append('            <button type="button" class="dm-btn-secondary-style" id="dm-btn-export-vision">Export</button>')
    p.append('            <button type="button" class="dm-btn-ghost" id="dm-btn-clear-vision">Clear</button>')
    p.append('          </div>')
    p.append('        </div>')
    p.append('      </div>')
    p.append('    </div>')

    p.append('  </div>')  # end #dm-panel-action

    # ── RESEARCH TAB ───────────────────────────────────────────────
    p.append('  <div id="dm-panel-research" class="dm-tab-panel" role="tabpanel">')
    p.append('    <div class="dm-sub-tabs" data-for="research">')
    p.append('      <button type="button" class="dm-sub-tab active" data-sub="sources">Sources</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="notes">Notes</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="highlights">Highlights</button>')
    p.append('      <button type="button" class="dm-sub-tab" data-sub="export-research">Export</button>')
    p.append('    </div>')

    p.append('    <div id="dm-research-sources" class="dm-sub-panel active">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Sources</div>')
    p.append('        <div class="dm-research-filters" id="dm-research-filters">')
    p.append('          <button type="button" class="dm-research-filter active" data-rfilter="all">All</button>')
    p.append('          <button type="button" class="dm-research-filter" data-rfilter="Article">Article</button>')
    p.append('          <button type="button" class="dm-research-filter" data-rfilter="Report">Report</button>')
    p.append('          <button type="button" class="dm-research-filter" data-rfilter="Dataset">Dataset</button>')
    p.append('          <button type="button" class="dm-research-filter" data-rfilter="Website">Website</button>')
    p.append('        </div>')
    p.append('        <div id="dm-research-sources-list" class="dm-research-list"></div>')
    p.append('        <details class="dm-source-form-toggle">')
    p.append('          <summary style="cursor:pointer;font-size:11px;color:var(--dm-blue);padding:4px 0;list-style:none;">+ Add new source</summary>')
    p.append('          <div class="dm-source-form" style="margin-top:8px;display:flex;flex-direction:column;gap:6px;">')
    p.append('            <input type="url" id="dm-research-url" class="dm-url-input" placeholder="Source URL" />')
    p.append('            <input type="text" id="dm-research-title" class="dm-vision-input" placeholder="Title" />')
    p.append('            <input type="text" id="dm-research-author" class="dm-vision-input" placeholder="Author(s)" />')
    p.append('            <input type="text" id="dm-research-year" class="dm-vision-input" placeholder="Year" style="max-width:100px;" />')
    p.append('            <select id="dm-research-type" class="dm-vision-category">')
    p.append('              <option>Article</option><option>Report</option><option>Dataset</option><option>Website</option><option>Book</option><option>Other</option>')
    p.append('            </select>')
    p.append('            <textarea id="dm-research-source-notes" class="dm-brainstorm-input" rows="2" placeholder="Notes"></textarea>')
    p.append('            <div style="display:flex;gap:6px;">')
    p.append('              <button type="button" class="dm-btn-primary-style" id="dm-btn-add-source" style="flex:1;">Add</button>')
    p.append('              <button type="button" class="dm-btn-ghost" id="dm-btn-clear-sources">Clear all</button>')
    p.append('            </div>')
    p.append('          </div>')
    p.append('        </details>')
    p.append('      </div>')
    p.append('    </div>')

    p.append('    <div id="dm-research-notes" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-notes-toolbar">')
    p.append('          <div class="dm-section-label">Research notes</div>')
    p.append('          <span id="dm-notes-wordcount" class="dm-notes-wordcount"></span>')
    p.append('        </div>')
    p.append('        <textarea id="dm-research-notes-text" class="dm-research-notes-textarea" placeholder="Research notes, narrative, quotes from sources..."></textarea>')
    p.append('        <div style="display:flex;gap:6px;margin-top:6px;">')
    p.append('          <button type="button" class="dm-btn-primary-style" id="dm-btn-save-research-notes" style="flex:1;">Save</button>')
    p.append('          <button type="button" class="dm-btn-ghost" id="dm-btn-export-notes-only">Export</button>')
    p.append('          <button type="button" class="dm-btn-ghost" id="dm-btn-clear-notes">Clear</button>')
    p.append('        </div>')
    p.append('      </div>')
    p.append('    </div>')

    p.append('    <div id="dm-research-highlights" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Highlights</div>')
    p.append('        <div id="dm-highlights-list" class="dm-highlights-list"></div>')
    p.append('        <div style="display:flex;gap:6px;margin-top:6px;align-items:center;">')
    p.append('          <input type="text" id="dm-highlight-text" class="dm-ai-key-input" placeholder="Text to highlight..." style="flex:1;margin:0;" />')
    p.append('          <select id="dm-highlight-color" style="width:80px;padding:7px 6px;border-radius:5px;border:1px solid var(--dm-border);background:var(--dm-surface);color:var(--dm-text);font-size:11px;font-family:inherit;">')
    p.append('            <option value="yellow">Yellow</option><option value="green">Green</option><option value="blue">Blue</option><option value="pink">Pink</option><option value="orange">Orange</option>')
    p.append('          </select>')
    p.append('        </div>')
    p.append('        <div style="display:flex;gap:6px;margin-top:6px;">')
    p.append('          <button type="button" class="dm-btn-primary-style" id="dm-btn-add-highlight" style="flex:1;">Add highlight</button>')
    p.append('          <button type="button" class="dm-btn-ghost" id="dm-btn-export-highlights">Export</button>')
    p.append('          <button type="button" class="dm-btn-ghost" id="dm-btn-clear-highlights">Clear</button>')
    p.append('        </div>')
    p.append('      </div>')
    p.append('    </div>')

    p.append('    <div id="dm-research-export-research" class="dm-sub-panel">')
    p.append('      <div class="dm-section">')
    p.append('        <div class="dm-section-label">Export research</div>')
    p.append('        <p class="dm-hint">Export all research materials for use in reports or grant proposals.</p>')
    p.append('        <button type="button" class="dm-btn-primary-style" id="dm-btn-export-research" style="width:100%;margin-bottom:6px;">Export all research</button>')
    p.append('        <button type="button" class="dm-btn-secondary-style" id="dm-btn-export-bib" style="width:100%;">Export bibliography</button>')
    p.append('      </div>')
    p.append('    </div>')

    p.append('  </div>')  # end #dm-panel-research

    p.append('</div>')  # end .dm-sidebar-content
    p.append('<div id="dm-sidebar-resize-handle" aria-hidden="true"></div>')

    return "\n".join(p)



def _write_initial_template(template_path: Path) -> None:
    """Create data_map_template.html from Python UI (CSS, structure, script). Only run when template is missing."""
    import re
    default_pl = "{{DM_DEFAULT_TITLE}}"
    welcome_html = _get_welcome_modal_html("Data map").replace(
        'value="' + _escape_attr("Data map") + '"', 'value="' + default_pl + '"'
    )
    body_parts = [
        '<div id="dm-app" data-default-title="' + default_pl + '">',
        "{{DM_SIDEBAR_HTML}}",
        welcome_html,
        _get_about_modal_html(),
        '\n<div id="dm-map-area">',
        '<div class="dm-app-header" id="dm-app-header">',
        '  <span class="dm-app-name">Data Mapper</span>',
        '  <span class="dm-map-title" id="dm-map-title">' + default_pl + "</span>",
        '  <div class="dm-header-actions">',
        '    <button type="button" class="dm-header-btn" id="dm-btn-about">About</button>',
        "  </div>",
        "</div>",
        _get_map_toolbar_html(),
        "{{DM_FOLIUM_MAP_AND_SCRIPT}}",
        "</div></div>",
        "{{DM_MAP_DATA_SCRIPT}}",
        "{{DM_INSIGHTS_SCRIPT}}",
        "{{DM_UI_SCRIPT}}",
    ]
    body = "\n".join(body_parts)
    html = (
        "<!DOCTYPE html>\n<html>\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">\n'
        + _get_ui_css()
        + '\n<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js" crossorigin="anonymous"></script>'
        + '\n<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" crossorigin="anonymous"></script>'
        + _get_folium_head_deps()
        + "</head>\n<body>\n"
        + "<!-- DM_TEMPLATE_VERSION: " + DM_TEMPLATE_VERSION + " -->\n"
        + body
        + "\n</body>\n</html>\n"
    )
    template_path.write_text(html, encoding="utf-8")
    print(f"Created template: {template_path}")


def _extract_folium_map_and_script(folium_html: str) -> str:
    """Extract map div + script from Folium output (div and script that follows </body>)."""
    import re
    div_m = re.search(
        r'<div class="folium-map" id="(map_[a-f0-9]+)"\s*></div>', folium_html
    )
    if not div_m:
        return ""
    body_pos = folium_html.find("</body>")
    if body_pos == -1:
        return div_m.group(0)
    after = folium_html[body_pos:]
    script_start = after.find("<script>")
    if script_start == -1:
        return div_m.group(0)
    start_abs = body_pos + script_start
    end_script = folium_html.find("</script>", start_abs + len("<script>"))
    if end_script == -1:
        return div_m.group(0)
    script_block = folium_html[start_abs : end_script + len("</script>")]
    if "L.map(" not in script_block or "addTo(map_" not in script_block:
        return div_m.group(0)
    return div_m.group(0) + "\n" + script_block


def _merge_template_with_folium(
    template_path: Path,
    folium_temp_path: Path,
    output_path: Path,
    sidebar_html: str,
    map_data: dict | None,
    title: str,
    insights: dict | None = None,
) -> None:
    """Read template, inject Folium map+script, data, and insights JSON. Template is never overwritten."""
    import json
    template = template_path.read_text(encoding="utf-8")
    folium_html = folium_temp_path.read_text(encoding="utf-8")
    folium_content = _extract_folium_map_and_script(folium_html)
    map_data_script = (
        '<script type="application/json" id="dm-map-data">'
        + json.dumps(map_data)
        + "</script>\n"
        if map_data
        else ""
    )
    if insights:
        metrics = insights.get("metrics", insights)
        serializable = {
            "key_messages": insights.get("key_messages", []),
            "metrics": _insights_to_json_serializable(metrics) if metrics else {},
            "correlations": insights.get("correlations", []),
        }
        insights_script = '<script type="application/json" id="dm-insights-data">' + json.dumps(serializable) + "</script>\n"
    else:
        insights_script = ""
    default_title = _escape_attr(title)
    template = template.replace("{{DM_DEFAULT_TITLE}}", default_title)
    template = template.replace("{{DM_SIDEBAR_HTML}}", sidebar_html)
    template = template.replace("{{DM_FOLIUM_MAP_AND_SCRIPT}}", folium_content)
    template = template.replace("{{DM_MAP_DATA_SCRIPT}}", map_data_script)
    template = template.replace("{{DM_INSIGHTS_SCRIPT}}", insights_script)
    template = template.replace("{{DM_UI_SCRIPT}}", _get_map_ui_js())
    output_path.write_text(template, encoding="utf-8")


def inject_insights_into_html(
    html_path: str,
    sidebar_html: str,
    map_data: dict | None = None,
    title: str = "Data map",
) -> None:
    """Insert UI styles, app wrapper (sidebar, modals, map area, header, toolbar), map data, and scripts.
    Used only when template is missing (fallback: patch Folium output in place)."""
    import re
    path = Path(html_path)
    content = path.read_text(encoding="utf-8")
    ui_css = _get_ui_css()
    content = re.sub(
        r"(<head[^>]*>)",
        "\\1\n"
        '<meta charset="utf-8">\n'
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">\n'
        + ui_css
        + '\n<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js" crossorigin="anonymous"></script>'
        + '\n<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" crossorigin="anonymous"></script>',
        content,
        count=1,
    )
    default_title = _escape_attr(title)
    app_start = (
        '<div id="dm-app" data-default-title="' + default_title + '">'
        + sidebar_html
        + _get_welcome_modal_html(title)
        + _get_about_modal_html()
        + '\n<div id="dm-map-area">'
        + _get_app_header_html(title)
        + _get_map_toolbar_html()
    )
    content = re.sub(r"<body[^>]*>", "\\g<0>\n" + app_start, content, count=1)
    before_body_close = "</div></div>\n" + _get_map_data_script(map_data) + _get_map_ui_js()
    content = re.sub(r"</body>", before_body_close + "\n</body>", content, count=1)
    content = _move_folium_script_into_body(content)
    path.write_text(content, encoding="utf-8")


def _move_folium_script_into_body(content: str) -> str:
    """Move the Folium map script from after </body> to right after the folium-map div."""
    import re
    folium_div = re.search(r'<div class="folium-map" id="(map_[a-f0-9]+)"\s*></div>', content)
    if not folium_div:
        return content
    body_pos = content.find("</body>")
    if body_pos == -1:
        return content
    after_body = content[body_pos:]
    script_start = after_body.find("<script>")
    if script_start == -1:
        return content
    start_abs = body_pos + script_start
    end_script = content.find("</script>", start_abs + len("<script>"))
    if end_script == -1:
        return content
    folium_script = content[start_abs : end_script + len("</script>")]
    if "L.map(" not in folium_script or "addTo(map_" not in folium_script:
        return content
    insert_after = folium_div.end()
    part1 = content[:insert_after]
    part2 = content[insert_after:body_pos]
    part3_tail = content[end_script + len("</script>"):].strip()
    new_tail = "</body>\n" + (part3_tail if part3_tail else "</html>") + "\n"
    return part1 + "\n" + folium_script + "\n" + part2 + new_tail


def write_impact_report(
    report_path: str,
    df: pd.DataFrame,
    value_columns: list[str],
    insights: dict,
    title: str = "Data map",
) -> None:
    """Write a standalone impact report (narrative + recommendations) for sharing or grant writing."""
    path = Path(report_path)
    lines = []
    lines.append("# Impact report: " + title)
    lines.append("")
    lines.append("This report summarizes key findings and recommendations from the data. Use it for advocacy, reporting, or funding proposals.")
    lines.append("")
    key_msgs = insights.get("key_messages", []) if isinstance(insights, dict) else []
    if key_msgs:
        lines.append("### Key messages")
        for msg in key_msgs:
            lines.append("- " + msg)
        lines.append("")
    metrics_data = insights.get("metrics", insights) if isinstance(insights, dict) else insights
    for metric, d in (metrics_data or {}).items():
        name = d["metric_display"]
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"- **Coverage:** {d['n']} locations. Range: {d['min']:.2f} - {d['max']:.2f}.")
        if d["disparity_ratio"] is not None:
            lines.append(f"- **Disparity ratio (max/min):** {d['disparity_ratio']:.1f}x.")
        lines.append(f"- **Median:** {d['median']:.2f}.")
        lines.append("")
        lines.append("### Recommended focus areas (priority for support)")
        priority = d["priority_list"]
        if not priority.empty:
            for _, row in priority.iterrows():
                lines.append(f"- {row['location']}: {row[metric]:.2f}")
        lines.append("")
        if d["below_benchmark"] is not None and not d["below_benchmark"].empty:
            lines.append(f"### Locations below benchmark ({d['benchmark']})")
            for _, row in d["below_benchmark"].iterrows():
                lines.append(f"- {row['location']}: {row[metric]:.2f}")
            lines.append("")
    lines.append("---")
    lines.append("Generated by Data Mapper (social impact). Share the map and this report to drive change.")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Impact report saved to {report_path}")


def _get_location_column(df: pd.DataFrame) -> str:
    """Return the name of the location column."""
    loc_candidates = [
        "location", "city", "cities", "country", "country_code", "place",
        "region", "name", "id", "geo", "area"
    ]
    for c in loc_candidates:
        for col in df.columns:
            if col.strip().lower() == c or col.strip().lower().replace(" ", "_") == c:
                return col
    return df.columns[0]


def _get_value_columns(df: pd.DataFrame, loc_col: str) -> list[str]:
    """Return list of numeric column names (excluding lat/lon and location)."""
    value_cols = []
    for c in df.columns:
        if c == loc_col:
            continue
        if "lat" in c.lower() or "lon" in c.lower() or "lng" in c.lower() or "longitude" in c.lower():
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            value_cols.append(c)
        else:
            try:
                pd.to_numeric(df[c], errors="raise")
                value_cols.append(c)
            except (TypeError, ValueError):
                pass
    return value_cols if value_cols else [df.columns[1]] if len(df.columns) > 1 else [df.columns[0]]


def create_blank_map(output_html: str, title: str = "Data map", center: tuple[float, float] | None = None, zoom_start: int | None = None) -> None:
    """Create a basemap-only Folium map (no choropleth polygons). Same tile stack as multi-layer maps."""
    if center is None:
        center = (20.0, 0.0)
    if zoom_start is None:
        zoom_start = 2
    m = folium.Map(
        location=list(center),
        zoom_start=zoom_start,
        tiles=None,
        max_bounds=True,
        control_scale=True,
    )
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
        name="Dark",
        overlay=False,
        control=True,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
        name="Light",
        overlay=False,
        control=True,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
        name="Voyager",
        overlay=False,
        control=True,
    ).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(output_html)


def normalize_dataframe(
    df: pd.DataFrame,
    value_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Detect location and value columns; return (df with location + value cols, list of value column names)."""
    df = df.dropna(how="all").copy()
    loc_col = _get_location_column(df)
    value_cols = value_columns or _get_value_columns(df, loc_col)
    if not value_cols:
        value_cols = [df.columns[1]] if len(df.columns) > 1 else [df.columns[0]]

    out = pd.DataFrame({"location": df[loc_col].astype(str).str.strip()})
    for vc in value_cols:
        if vc in df.columns:
            out[vc] = pd.to_numeric(df[vc], errors="coerce")
    out = out.dropna(subset=value_cols, how="all")
    return out, value_cols


def has_lat_lon(df: pd.DataFrame) -> bool:
    """Check if dataframe has latitude and longitude columns."""
    lower = [c.strip().lower() for c in df.columns]
    return "lat" in lower or "latitude" in lower and ("lon" in lower or "lng" in lower or "longitude" in lower)


def get_lat_lon_df(df: pd.DataFrame, country_context: str | None = None) -> pd.DataFrame:
    """Return dataframe with lat, lon, value; geocode location if needed.

    country_context improves geocoding for subnational rows (e.g., "Springfield, US").
    """
    lower = {c.strip().lower(): c for c in df.columns}
    if "lat" in lower or "latitude" in lower:
        lat_col = lower.get("lat") or lower.get("latitude")
        lon_col = lower.get("lon") or lower.get("lng") or lower.get("longitude")
        value_col = None
        for c in df.columns:
            if c in (lat_col, lon_col):
                continue
            if pd.api.types.is_numeric_dtype(df[c]):
                value_col = c
                break
        if value_col is None:
            value_col = df.columns[2] if len(df.columns) > 2 else df.columns[0]
        out = pd.DataFrame({
            "lat": pd.to_numeric(df[lat_col], errors="coerce"),
            "lon": pd.to_numeric(df[lon_col], errors="coerce"),
            "value": pd.to_numeric(df[value_col], errors="coerce"),
        })
        out = out.dropna()
        return out

    # If data looks like country codes, skip geocoding entirely
    if "location" in df.columns:
        locs = df["location"].astype(str).str.strip()
        if locs.str.match(r"^[A-Za-z]{2,3}$").all():
            raise SystemExit(
                "Data appears to be country codes. Use --mode countries or ensure "
                "is_country_data() detects it. Cannot geocode country codes as points."
            )

    # Geocode location strings (city/place names only)
    if not HAS_GEOPY:
        raise SystemExit("For city/place names install geopy: pip install geopy")
    value_cols = [c for c in df.columns if c != "location" and pd.api.types.is_numeric_dtype(df[c])]
    if not value_cols:
        value_cols = [c for c in df.columns if c != "location"][:1]
    geolocator = Nominatim(user_agent="data-mapping-app")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.1)
    lats, lons = [], []
    for loc in df["location"]:
        try:
            query = f"{loc}, {country_context}" if country_context else str(loc)
            g = geocode(query)
            if g:
                lats.append(g.latitude)
                lons.append(g.longitude)
            else:
                lats.append(np.nan)
                lons.append(np.nan)
        except Exception:
            lats.append(np.nan)
            lons.append(np.nan)
    out = pd.DataFrame({
        "lat": lats,
        "lon": lons,
        "location": df["location"].values,
    })
    for vc in value_cols:
        out[vc] = pd.to_numeric(df[vc], errors="coerce").values
    if "value" not in out.columns and len(value_cols) == 1:
        out["value"] = out[value_cols[0]]
    out = out.dropna(subset=["lat", "lon"])
    return out


def is_country_data(df: pd.DataFrame) -> bool:
    """Heuristic: locations look like 3-letter (or 2-letter) country codes."""
    locs = df["location"].str.upper().str.strip()
    # 3-letter ISO
    if locs.str.match(r"^[A-Z]{3}$").all():
        return True
    # 2-letter ISO
    if locs.str.match(r"^[A-Z]{2}$").all():
        return True
    # Mix of 2 and 3
    if locs.str.match(r"^[A-Z]{2,3}$").all():
        return True
    return False


def two_to_three_letter(code: str) -> str:
    """Convert 2-letter ISO to 3-letter for folium world-countries GeoJSON. Complete mapping for all 249 ISO codes."""
    mapping = {
        "AF": "AFG", "AL": "ALB", "DZ": "DZA", "AS": "ASM", "AD": "AND", "AO": "AGO",
        "AG": "ATG", "AR": "ARG", "AM": "ARM", "AW": "ABW", "AU": "AUS", "AT": "AUT",
        "AZ": "AZE", "BS": "BHS", "BH": "BHR", "BD": "BGD", "BB": "BRB", "BY": "BLR",
        "BE": "BEL", "BZ": "BLZ", "BJ": "BEN", "BM": "BMU", "BT": "BTN", "BO": "BOL",
        "BA": "BIH", "BW": "BWA", "BR": "BRA", "BN": "BRN", "BG": "BGR", "BF": "BFA",
        "BI": "BDI", "CV": "CPV", "KH": "KHM", "CM": "CMR", "CA": "CAN", "KY": "CYM",
        "CF": "CAF", "TD": "TCD", "CL": "CHL", "CN": "CHN", "CO": "COL", "KM": "COM",
        "CG": "COG", "CD": "COD", "CR": "CRI", "CI": "CIV", "HR": "HRV", "CU": "CUB",
        "CY": "CYP", "CZ": "CZE", "DK": "DNK", "DJ": "DJI", "DM": "DMA", "DO": "DOM",
        "EC": "ECU", "EG": "EGY", "SV": "SLV", "GQ": "GNQ", "ER": "ERI", "EE": "EST",
        "SZ": "SWZ", "ET": "ETH", "FJ": "FJI", "FI": "FIN", "FR": "FRA", "GA": "GAB",
        "GM": "GMB", "GE": "GEO", "DE": "DEU", "GH": "GHA", "GR": "GRC", "GD": "GRD",
        "GT": "GTM", "GN": "GIN", "GW": "GNB", "GY": "GUY", "HT": "HTI", "HN": "HND",
        "HU": "HUN", "IS": "ISL", "IN": "IND", "ID": "IDN", "IR": "IRN", "IQ": "IRQ",
        "IE": "IRL", "IL": "ISR", "IT": "ITA", "JM": "JAM", "JP": "JPN", "JO": "JOR",
        "KZ": "KAZ", "KE": "KEN", "KI": "KIR", "KP": "PRK", "KR": "KOR", "KW": "KWT",
        "KG": "KGZ", "LA": "LAO", "LV": "LVA", "LB": "LBN", "LS": "LSO", "LR": "LBR",
        "LY": "LBY", "LI": "LIE", "LT": "LTU", "LU": "LUX", "MG": "MDG", "MW": "MWI",
        "MY": "MYS", "MV": "MDV", "ML": "MLI", "MT": "MLT", "MH": "MHL", "MR": "MRT",
        "MU": "MUS", "MX": "MEX", "FM": "FSM", "MD": "MDA", "MC": "MCO", "MN": "MNG",
        "ME": "MNE", "MA": "MAR", "MZ": "MOZ", "MM": "MMR", "NA": "NAM", "NR": "NRU",
        "NP": "NPL", "NL": "NLD", "NZ": "NZL", "NI": "NIC", "NE": "NER", "NG": "NGA",
        "MK": "MKD", "NO": "NOR", "OM": "OMN", "PK": "PAK", "PW": "PLW", "PS": "PSE",
        "PA": "PAN", "PG": "PNG", "PY": "PRY", "PE": "PER", "PH": "PHL", "PL": "POL",
        "PT": "PRT", "QA": "QAT", "RO": "ROU", "RU": "RUS", "RW": "RWA", "KN": "KNA",
        "LC": "LCA", "VC": "VCT", "WS": "WSM", "SM": "SMR", "ST": "STP", "SA": "SAU",
        "SN": "SEN", "RS": "SRB", "SC": "SYC", "SL": "SLE", "SG": "SGP", "SK": "SVK",
        "SI": "SVN", "SB": "SLB", "SO": "SOM", "ZA": "ZAF", "SS": "SSD", "ES": "ESP",
        "LK": "LKA", "SD": "SDN", "SR": "SUR", "SE": "SWE", "CH": "CHE", "SY": "SYR",
        "TW": "TWN", "TJ": "TJK", "TZ": "TZA", "TH": "THA", "TL": "TLS", "TG": "TGO",
        "TO": "TON", "TT": "TTO", "TN": "TUN", "TR": "TUR", "TM": "TKM", "TV": "TUV",
        "UG": "UGA", "UA": "UKR", "AE": "ARE", "GB": "GBR", "US": "USA", "UY": "URY",
        "UZ": "UZB", "VU": "VUT", "VE": "VEN", "VN": "VNM", "YE": "YEM", "ZM": "ZMB",
        "ZW": "ZWE", "XK": "XKX", "HK": "HKG", "MO": "MAC", "PR": "PRI", "RE": "REU",
        "GP": "GLP", "MQ": "MTQ", "GF": "GUF", "NC": "NCL", "PF": "PYF", "CW": "CUW",
        "SX": "SXM", "BQ": "BES", "AX": "ALA", "FO": "FRO", "GI": "GIB", "GL": "GRL",
        "GG": "GGY", "IM": "IMN", "JE": "JEY", "AI": "AIA", "TC": "TCA", "VG": "VGB",
        "VI": "VIR", "MS": "MSR", "BL": "BLM", "MF": "MAF", "PM": "SPM", "WF": "WLF",
        "EH": "ESH", "IO": "IOT", "CX": "CXR", "CC": "CCK", "NF": "NFK", "TK": "TKL",
        "NU": "NIU", "CK": "COK", "PN": "PCN", "SH": "SHN", "FK": "FLK", "GS": "SGS",
        "AQ": "ATA", "BV": "BVT", "HM": "HMD", "TF": "ATF", "UM": "UMI",
    }
    if not isinstance(code, str):
        return str(code) if code == code else ""
    code = code.upper().strip()
    if len(code) == 3:
        return code
    return mapping.get(code, code)


def _normalize_location_key(value: object) -> str:
    """Normalize labels/ids for resilient matching (state/city/county names)."""
    s = str(value).strip().lower()
    if not s:
        return ""
    return "".join(ch for ch in s if ch.isalnum())


def _get_feature_value(feature: dict, key_on: str) -> object:
    """Read a feature value using key path syntax (e.g. feature.properties.STATE)."""
    if not isinstance(feature, dict):
        return None
    if not key_on:
        return None
    path = [p for p in key_on.split(".") if p]
    if path and path[0] == "feature":
        path = path[1:]
    cur: object = feature
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _lookup_value(raw_key: object, lookup: dict[str, float], lookup_norm: dict[str, float]) -> float | None:
    """Find metric value by exact key first, then normalized key."""
    if raw_key is None:
        return None
    k = str(raw_key).strip()
    if not k:
        return None
    if k in lookup:
        return lookup[k]
    nk = _normalize_location_key(k)
    if nk in lookup_norm:
        return lookup_norm[nk]
    return None


def _detect_subnational_profile(locs: pd.Series) -> dict | None:
    """Detect ISO 3166-2 style rows (e.g. IN-AP, US-CA, MX-BCM, CA-ON) for Natural Earth admin-1."""
    cleaned = locs.astype(str).str.strip().str.upper()
    if cleaned.empty:
        return None
    if not cleaned.str.match(r"^[A-Z]{2}-[A-Z0-9]{1,4}$").all():
        return None
    profile = SUBNATIONAL_GEOJSON_PROFILES.get("_GLOBAL_ADMIN1")
    if not profile:
        return None
    out = dict(profile)
    prefixes = cleaned.str.split("-", n=1, expand=True)[0].unique().tolist()
    out["country"] = prefixes[0] if len(prefixes) == 1 else None
    return out


def _map_subnational_location(country: str | None, raw_location: object) -> str:
    """Normalize toward ISO 3166-2 codes for Natural Earth admin-1 (e.g. US-CA, IN-KA, MX-BCM)."""
    s = str(raw_location).strip()
    if not s:
        return s
    up = s.upper()
    if len(up) >= 4 and "-" in up:
        return up
    c = (country or "").upper()
    if len(up) == 2 and up.isalpha() and c == "US":
        return f"US-{up}"
    if len(up) == 2 and up.isalpha() and c == "IN":
        return f"IN-{up}"
    return s


def value_to_color(values: pd.Series, colormap: str = "YlOrRd") -> tuple:
    """Map numeric values to hex colors and return (series of hex, norm, cmap)."""
    vmin, vmax = values.min(), values.max()
    if vmin == vmax:
        vmin = vmax - 1
    norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
    cmap = plt.get_cmap(colormap)
    def to_hex(v):
        return mcolors.to_hex(cmap(norm(v)))
    return values.map(to_hex), norm, cmap


def build_legend_png(colormap: str = "YlOrRd", vmin: float = 0, vmax: float = 100, title: str = "Value") -> bytes:
    """Create a small colorbar legend as PNG bytes."""
    import io
    fig, ax = plt.subplots(figsize=(1.2, 4))
    fig.subplots_adjust(right=0.3)
    cmap = plt.get_cmap(colormap)
    norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
    plt.colorbar(
        plt.cm.ScalarMappable(norm=norm, cmap=cmap),
        cax=ax, orientation="vertical", label=title
    )
    b = io.BytesIO()
    fig.savefig(b, format="png", bbox_inches="tight", dpi=80)
    plt.close(fig)
    return b.getvalue()

def create_choropleth_map(
    df: pd.DataFrame,
    output_path: str,
    value_columns: list[str],
    title: str = "Data map",
    colormap: str = "YlOrRd",
    *,
    center: tuple[float, float] | None = None,
    zoom_start: int | None = None,
    geo_data: dict | None = None,
    key_on: str = "feature.id",
    name_property: str = "name",
) -> None:
    import urllib.request
    import json

    df = df.copy()

    if geo_data is None:
        df["location"] = df["location"].str.upper().str.strip().apply(two_to_three_letter)
        # Cache GeoJSON locally to avoid re-downloading every run
        cache_path = Path(__file__).parent / ".ne_110m_admin0_countries_cache.json"
        if cache_path.exists():
            geo_data = json.loads(cache_path.read_text(encoding="utf-8"))
        else:
            raw = urllib.request.urlopen(NATURAL_EARTH_ADMIN0_110M_URL).read().decode()
            geo_data = json.loads(raw)
            try:
                cache_path.write_text(raw, encoding="utf-8")
            except Exception:
                pass
        key_on = "feature.properties.ISO_A3"
        name_property = "ADMIN"

    map_center = center or (20.0, 0.0)
    map_zoom = zoom_start if zoom_start is not None else 2

    m = folium.Map(
        location=list(map_center),
        zoom_start=map_zoom,
        tiles=None,
        max_bounds=True,
    )
    # Multiple tile layers: add Dark/Light first so Voyager (added last) is default visible
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
        name="Dark",
        overlay=False,
        control=True,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
        name="Light",
        overlay=False,
        control=True,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
        name="Voyager",
        overlay=False,
        control=True,
    ).add_to(m)

    # One choropleth layer per metric so layer toggles are native/reliable.
    metrics_in_df = [vc for vc in value_columns if vc in df.columns]
    if not metrics_in_df:
        metrics_in_df = [c for c in df.columns if c not in ("location",)]

    for i, metric in enumerate(metrics_in_df):
        vals = df[["location", metric]].drop_duplicates(subset=["location"]).dropna()
        if vals.empty:
            continue
        values = vals.set_index("location")[metric]
        value_lookup = {str(k).strip(): float(v) for k, v in values.items()}
        value_lookup_norm = {
            _normalize_location_key(k): v
            for k, v in value_lookup.items()
            if _normalize_location_key(k)
        }
        vmin, vmax = values.min(), values.max()
        layer_colormap = LAYER_COLORMAPS[i % len(LAYER_COLORMAPS)]
        norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
        cmap = plt.get_cmap(layer_colormap)

        def style_function(
            feature,
            _lookup=value_lookup,
            _lookup_norm=value_lookup_norm,
            _norm=norm,
            _cmap=cmap,
            _key_on=key_on,
            _name_prop=name_property,
        ):
            loc_key = _get_feature_value(feature, _key_on)
            if loc_key is None:
                loc_key = feature.get("id")
            if loc_key is None:
                loc_key = (feature.get("properties") or {}).get(_name_prop)
            v = _lookup_value(loc_key, _lookup, _lookup_norm)
            if v is not None:
                color = mcolors.to_hex(_cmap(_norm(v)))
            else:
                color = "#dddddd"
            return {
                "fillColor": color,
                "color": "rgba(255,255,255,0.8)",
                "weight": 1,
                "fillOpacity": 0.75,
            }

        layer_name = metric.replace("_", " ").title()
        fg = folium.FeatureGroup(name=layer_name, show=(i == 0))
        folium.GeoJson(
            geo_data,
            style_function=style_function,
            tooltip=folium.GeoJsonTooltip(
                fields=[name_property],
                aliases=["Region: "],
            ),
        ).add_to(fg)
        fg.add_to(m)

        # Keep a single legend to avoid clutter.
        if i == 0:
            import base64
            legend_bytes = build_legend_png(layer_colormap, float(vmin), float(vmax), layer_name)
            b64 = base64.b64encode(legend_bytes).decode()
            FloatImage(
                f"data:image/png;base64,{b64}",
                bottom=5,
                left=5,
                width=80,
            ).add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    m.save(output_path)

def create_point_map(
    df: pd.DataFrame,
    output_path: str,
    value_columns: list[str],
    title: str = "Data map",
    colormap: str = "YlOrRd",
    raw_df: pd.DataFrame | None = None,
    *,
    center: tuple[float, float] | None = None,
    zoom_start: int | None = None,
    country_context: str | None = None,
) -> None:
    """Create a map with colored circle markers; one layer per metric if multiple."""
    import base64
    import io

    if raw_df is not None and has_lat_lon(raw_df):
        point_df = get_lat_lon_df(raw_df, country_context=country_context)
    else:
        point_df = get_lat_lon_df(df, country_context=country_context)
    # Which metrics we actually have on point_df
    metrics_in_df = [vc for vc in value_columns if vc in point_df.columns]
    if not metrics_in_df:
        metrics_in_df = ["value"] if "value" in point_df.columns else list(point_df.columns)
    if not metrics_in_df:
        metrics_in_df = [c for c in point_df.columns if c not in ("lat", "lon", "location")]

    map_center = center or (float(point_df["lat"].mean()), float(point_df["lon"].mean()))
    if zoom_start is not None:
        map_zoom = zoom_start
    else:
        n = len(point_df)
        map_zoom = 6 if n < 10 else (5 if n < 30 else (4 if n < 100 else 3))
    # Smaller markers when there are many points (very big datasets)
    radius = 10 if len(point_df) < 200 else (6 if len(point_df) < 1000 else 4)

    m = folium.Map(location=map_center, zoom_start=map_zoom, tiles=None, max_bounds=True)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        attr='&copy; OpenStreetMap &copy; CARTO',
        name="Dark",
        overlay=False,
        control=True,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        attr='&copy; OpenStreetMap &copy; CARTO',
        name="Light",
        overlay=False,
        control=True,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
        attr='&copy; OpenStreetMap &copy; CARTO',
        name="Voyager",
        overlay=False,
        control=True,
    ).add_to(m)

    for metric in metrics_in_df:
        if metric not in point_df.columns:
            continue
        vals = point_df[metric].dropna()
        if vals.empty:
            continue
        color_series, _, _ = value_to_color(point_df[metric], colormap)
        vmin, vmax = float(vals.min()), float(vals.max())
        name = metric.replace("_", " ").title()
        fg = folium.FeatureGroup(name=name)
        for i, row in point_df.iterrows():
            if pd.isna(row.get(metric)):
                continue
            hex_color = color_series.loc[i]
            loc_name = str(row.get("location", ""))
            tooltip = f"{loc_name}: {row[metric]:.2f}" if loc_name else f"{row[metric]:.2f}"
            folium.CircleMarker(
                location=[row["lat"], row["lon"]],
                radius=radius,
                color="#333",
                fill=True,
                fill_color=hex_color,
                fill_opacity=0.8,
                weight=1,
                tooltip=tooltip,
            ).add_to(fg)
        fg.add_to(m)
        # Legend only for first layer when multiple (to avoid clutter)
        if len(metrics_in_df) == 1 or metric == metrics_in_df[0]:
            legend_bytes = build_legend_png(colormap, vmin, vmax, name)
            b64 = base64.b64encode(legend_bytes).decode()
            FloatImage(
                f"data:image/png;base64,{b64}",
                bottom=5,
                left=5,
                width=80,
            ).add_to(m)

    if len(metrics_in_df) > 1:
        folium.LayerControl(collapsed=False).add_to(m)

    m.save(output_path)


def _parse_center(s: str) -> tuple[float, float]:
    """Parse 'lat,lon' or 'lat, lon' into (lat, lon)."""
    parts = s.replace(" ", "").split(",")
    if len(parts) != 2:
        raise ValueError("--center must be lat,lon (e.g. 40.7,-74.0)")
    return float(parts[0]), float(parts[1])


def main():
    parser = argparse.ArgumentParser(description="Map location-based data with color scale.")
    parser.add_argument("input", nargs="?", help="Input CSV file (location/place, value)")
    parser.add_argument("-o", "--output", default="data_map.html", help="Output HTML file")
    parser.add_argument("--title", default="Value", help="Title for legend/colormap")
    parser.add_argument("--colormap", default="YlOrRd", help="Matplotlib colormap (e.g. viridis, plasma, YlOrRd)")
    parser.add_argument("--metric", metavar="COLUMN", help="Map only this column (default: all numeric columns)")
    parser.add_argument("--mode", choices=["auto", "countries", "points"], default="auto",
                        help="Map type: auto (detect), countries/subnational choropleth, or points (cities)")
    parser.add_argument("--zoom", type=int, help="Map zoom (1=world, 6=region, 10=city, 15=neighborhood). Default: auto.")
    parser.add_argument("--center", metavar="LAT,LON", help="Map center as lat,lon (e.g. 40.7,-74.0 for NYC). Default: auto from data.")
    parser.add_argument("--geojson", metavar="FILE", help="Custom GeoJSON for regions (states, counties, etc.). CSV location column must match the selected --key-on field.")
    parser.add_argument("--key-on", default="feature.id", metavar="PATH", help="GeoJSON key path (e.g. feature.properties.STATE_ID). Use with --geojson.")
    parser.add_argument("--name-property", default="name", metavar="PROP", help="GeoJSON property for tooltip name (e.g. NAME). Use with --geojson.")
    parser.add_argument("--country-context", metavar="COUNTRY", help="Country hint for geocoding city/state names in point mode (e.g. --country-context India).")
    parser.add_argument("--no-insights", action="store_true", help="Do not add key takeaways and recommendations to the map page.")
    parser.add_argument("--benchmark", type=float, metavar="N", help="Flag locations below this value as priority (e.g. --benchmark 70 for life expectancy).")
    parser.add_argument("--report", metavar="FILE", help="Also write an impact report (narrative + recommendations) for sharing or grant proposals.")
    parser.add_argument("--open", dest="open_browser", action="store_true", default=True,
                        help="Open the map in your default browser after generating (default).")
    parser.add_argument("--no-open", dest="open_browser", action="store_false",
                        help="Do not open the map in the browser.")
    parser.add_argument("--serve", action="store_true",
                        help="Run a local web server and open the map as a live page (stays open until Ctrl+C).")
    parser.add_argument("--port", type=int, default=8080, metavar="PORT",
                        help="Port for --serve (default: 8080).")
    args = parser.parse_args()

    raw_df = None
    df = None
    value_columns: list[str] = []
    use_choropleth = True
    has_ll = False
    is_blank = False

    if not args.input:
        # Blank-map mode: user will use the in-app "Find data" feature to add datasets.
        is_blank = True
    else:
        if not Path(args.input).exists():
            print(f"Error: input CSV not found: {args.input}", file=sys.stderr)
            sys.exit(1)
        raw_df = pd.read_csv(args.input)
        requested = [args.metric] if args.metric and args.metric in raw_df.columns else None
        df, value_columns = normalize_dataframe(raw_df, value_columns=requested)
        if df.empty:
            print("No valid rows after parsing.")
            sys.exit(1)
        if args.metric and args.metric not in value_columns and args.metric in df.columns:
            value_columns = [args.metric]
        use_choropleth = args.mode == "countries" or (
            args.mode == "auto" and is_country_data(df)
        )
        has_ll = has_lat_lon(raw_df)

    center = None
    if args.center:
        try:
            center = _parse_center(args.center)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    geo_data = None
    key_on = args.key_on
    name_property = args.name_property
    subnational_profile = None
    if is_blank:
        geo_data = None
    elif args.geojson:
        if not Path(args.geojson).exists():
            print(f"Error: GeoJSON file not found: {args.geojson}", file=sys.stderr)
            sys.exit(1)
        with open(args.geojson, encoding="utf-8") as f:
            import json
            geo_data = json.load(f)
        use_choropleth = True  # force choropleth when custom GeoJSON provided
    elif args.mode in ("auto", "countries") and not has_ll:
        # Auto support for one-country subnational datasets (e.g., IN-AP, US-CA).
        detected = _detect_subnational_profile(df["location"])
        if detected:
            import urllib.request
            import json
            subnational_profile = detected
            cache_path = Path(__file__).parent / ".subnational_global_admin1_cache.json"
            if cache_path.exists():
                geo_data = json.loads(cache_path.read_text(encoding="utf-8"))
            else:
                raw = urllib.request.urlopen(detected["url"]).read().decode()
                geo_data = json.loads(raw)
                try:
                    cache_path.write_text(raw, encoding="utf-8")
                except Exception:
                    pass
            key_on = detected["key_on"]
            name_property = detected["name_property"]
            use_choropleth = True
            # Convert location codes to keys expected by selected GeoJSON profile.
            df["location"] = df["location"].apply(
                lambda x: _map_subnational_location(detected["country"], x)
            )

    # Folium writes to a temp file; we merge with the template into args.output so the template is never overwritten
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
        folium_temp_path = Path(f.name)

    try:
        if is_blank:
            create_blank_map(str(folium_temp_path), title=args.title, center=center, zoom_start=args.zoom)
        elif use_choropleth and not has_ll:
            create_choropleth_map(
                df, str(folium_temp_path), value_columns=value_columns,
                title=args.title, colormap=args.colormap,
                center=center, zoom_start=args.zoom,
                geo_data=geo_data, key_on=key_on, name_property=name_property,
            )
        else:
            create_point_map(
                df, str(folium_temp_path), value_columns=value_columns,
                title=args.title, colormap=args.colormap, raw_df=raw_df,
                center=center, zoom_start=args.zoom, country_context=args.country_context,
            )

        # Insights and map data for UI (sidebar, color scale, add-data)
        insights = None
        if (not is_blank) and (not args.no_insights):
            insights = compute_insights(
                df, value_columns,
                benchmark=args.benchmark,
                priority_quartile=True,
            )
            if args.report and insights:
                write_impact_report(args.report, df, value_columns, insights, title=args.title)

        # Build map_data for client-side color scale and add-data
        map_data = None
        if is_blank:
            # Blank map: no embedded polygons until the user picks geography or previews data.
            map_data = {
                "type": "choropleth",
                "values": {},
                "min": 0.0,
                "max": 1.0,
                "colormap": args.colormap,
                "active_metric": "",
                "metrics": {},
                "geo_json": None,
                "geo_profiles": _client_geo_profiles(),
                "geo_level": "countries",
                "key_on": "feature.properties.ISO_A3",
                "name_property": "ADMIN",
                "start_blank": True,
            }
        elif use_choropleth and not has_ll:
            # World map expects ISO2/ISO3; custom GeoJSON keeps user keys as-is.
            df_for_data = df.copy()
            if geo_data is None:
                df_for_data["location"] = df_for_data["location"].str.upper().str.strip().apply(two_to_three_letter)
            else:
                df_for_data["location"] = df_for_data["location"].astype(str).str.strip()
            vcol = value_columns[0] if value_columns else df.columns[1]
            vals = df_for_data.set_index("location")[vcol].dropna()
            if not vals.empty:
                # Build per-metric values so client-side can switch metrics
                all_metrics = {}
                for i, vc in enumerate(value_columns):
                    if vc in df_for_data.columns:
                        mv = df_for_data.set_index("location")[vc].dropna()
                        if not mv.empty:
                            all_metrics[vc] = {
                                "values": mv.to_dict(),
                                "min": float(mv.min()),
                                "max": float(mv.max()),
                                "display": vc.replace("_", " ").title(),
                                "colormap": LAYER_COLORMAPS[i % len(LAYER_COLORMAPS)],
                            }
                if geo_data is None:
                    client_key_on = "feature.properties.ISO_A3"
                    client_name_prop = "ADMIN"
                    geo_level = "countries"
                else:
                    client_key_on = key_on
                    client_name_prop = name_property
                    geo_level = "admin1_global"
                if args.geojson and geo_data is not None:
                    geo_json_for_client = geo_data
                else:
                    geo_json_for_client = None
                map_data = {
                    "type": "choropleth",
                    "values": vals.to_dict(),
                    "min": float(vals.min()),
                    "max": float(vals.max()),
                    "colormap": args.colormap,
                    "active_metric": vcol,
                    "metrics": all_metrics,
                    "geo_json": geo_json_for_client,
                    "geo_profiles": _client_geo_profiles(),
                    "geo_level": geo_level,
                    "key_on": client_key_on,
                    "name_property": client_name_prop,
                    "start_blank": False,
                }
        elif has_ll:
            try:
                point_df = get_lat_lon_df(raw_df, country_context=args.country_context)
                vcol = next((c for c in value_columns if c in point_df.columns), "value")
                if vcol not in point_df.columns:
                    vcol = point_df.columns[2] if len(point_df.columns) > 2 else point_df.columns[0]
                vals = point_df[vcol].dropna()
                if not vals.empty:
                    name_ser = point_df["location"] if "location" in point_df.columns else point_df.index.astype(str)
                    map_data = {
                        "type": "points",
                        "points": point_df[["lat", "lon"]].assign(value=point_df[vcol], name=name_ser).to_dict("records"),
                        "min": float(vals.min()),
                        "max": float(vals.max()),
                        "colormap": args.colormap,
                    }
            except Exception:
                map_data = None

        sidebar_html = '<div id="dm-sidebar">' + build_sidebar_html(
            insights, title=args.title, has_map_data=bool(map_data)
        ) + "</div>"

        template_path = _get_template_path()
        template_needs_update = False
        if not template_path.exists():
            template_needs_update = True
        else:
            try:
                _tpl = template_path.read_text(encoding="utf-8")
            except Exception:
                _tpl = ""
            if "{{DM_UI_SCRIPT}}" not in _tpl or "{{DM_FOLIUM_MAP_AND_SCRIPT}}" not in _tpl:
                template_needs_update = True
            elif ("DM_TEMPLATE_VERSION: " + DM_TEMPLATE_VERSION) not in _tpl:
                template_needs_update = True
        if template_needs_update:
            _write_initial_template(template_path)
        _merge_template_with_folium(
            template_path,
            folium_temp_path,
            Path(args.output),
            sidebar_html,
            map_data,
            args.title,
            insights=insights,
        )
    finally:
        try:
            folium_temp_path.unlink(missing_ok=True)
        except Exception:
            pass

    # Open map in browser and/or run local server so it pops up live
    output_path = Path(args.output).resolve()
    print(f"Map saved to {output_path}")
    if args.serve:
        from functools import partial
        from http.server import HTTPServer, ThreadingHTTPServer

        directory = str(output_path.parent)
        Handler = create_data_mapper_request_handler(directory)
        handler = partial(Handler, directory=directory)
        server = None
        bound_port = None
        for cand in [args.port] + [args.port + i for i in range(1, 30)] + [0]:
            try:
                server = ThreadingHTTPServer(("", cand), handler)
                bound_port = server.server_address[1]
                break
            except OSError as e:
                if cand == 0:
                    print(f"Error: could not bind HTTP server: {e}", file=sys.stderr)
                    sys.exit(1)
                continue
        if bound_port != args.port:
            print(f"Note: port {args.port} was busy; using {bound_port} instead.", file=sys.stderr)
        url = f"http://127.0.0.1:{bound_port}/{output_path.name}"
        webbrowser.open(url)
        print(
            f"Map is live at {url} - press Ctrl+C to stop the server.\n"
            "  GET /dm-proxy?url=...  -  World Bank, Our World in Data, GitHub raw/gist (browser-safe).\n"
            "  POST /dm-openai  -  relays OpenAI chat completions (Insights  ->  Ask and AI dataset search)."
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
    elif args.open_browser:
        webbrowser.open(output_path.as_uri())
        print(f"Map opened in your browser: {output_path}")


if __name__ == "__main__":
    main()
