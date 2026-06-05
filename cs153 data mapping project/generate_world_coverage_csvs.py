#!/usr/bin/env python3
"""
Build two reference CSVs aligned with Natural Earth (same geometry the app uses):

  1) world_countries_natural_earth.csv — admin-0 polygons keyed by ISO_A3 (+ ADMIN name).
  2) world_admin1_natural_earth.csv — every admin-1 polygon with iso_3166_2 (+ country + name).

Run from the project directory:
  python3 generate_world_coverage_csvs.py

Uses local caches if present; otherwise downloads once into the same cache files as data-mapping.py.
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 10m admin-0: many more sovereign / dependency polygons than 110m (better "every country" coverage).
ADMIN0_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_10m_admin_0_countries.geojson"
)
ADMIN1_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_10m_admin_1_states_provinces.geojson"
)

CACHE_ADMIN0 = ROOT / ".ne_10m_admin0_countries_cache.json"
CACHE_ADMIN1 = ROOT / ".subnational_global_admin1_cache.json"

OUT_COUNTRIES = ROOT / "world_countries_natural_earth.csv"
OUT_ADMIN1 = ROOT / "world_admin1_natural_earth.csv"


def _load_or_fetch(url: str, cache: Path) -> dict:
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    print(f"Downloading {url} …", file=sys.stderr)
    raw = urllib.request.urlopen(url, timeout=120).read().decode("utf-8")
    data = json.loads(raw)
    try:
        cache.write_text(raw, encoding="utf-8")
        print(f"Cached → {cache}", file=sys.stderr)
    except OSError as e:
        print(f"Warning: could not write cache {cache}: {e}", file=sys.stderr)
    return data


def _placeholder_value(key: str) -> float:
    """Stable pseudo-metric 40–99 so choropleths show contrast without implying real statistics."""
    h = abs(hash(key)) % 60
    return float(40 + h)


def write_countries(geo: dict) -> int:
    rows: list[dict[str, str]] = []
    for feat in geo.get("features") or []:
        p = feat.get("properties") or {}
        iso = (p.get("ISO_A3") or "").strip()
        if not iso or iso in ("-99", "-1"):
            iso = (p.get("ADM0_A3") or p.get("WB_A3") or "").strip()
        if not iso:
            continue
        name = (p.get("ADMIN") or p.get("NAME") or iso).strip()
        cont = (p.get("CONTINENT") or "").strip()
        region = (p.get("REGION_UN") or p.get("SUBREGION") or "").strip()
        rows.append(
            {
                "iso3": iso,
                "name": name,
                "continent": cont,
                "region_un": region,
                "value": f"{_placeholder_value(iso):.2f}",
            }
        )
    rows.sort(key=lambda r: r["iso3"])
    OUT_COUNTRIES.write_text("", encoding="utf-8")  # truncate
    with OUT_COUNTRIES.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["iso3", "name", "continent", "region_un", "value"],
            quoting=csv.QUOTE_MINIMAL,
        )
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def write_admin1(geo: dict) -> int:
    rows: list[dict[str, str]] = []
    for feat in geo.get("features") or []:
        p = feat.get("properties") or {}
        iso2 = (p.get("iso_3166_2") or "").strip().upper()
        if not iso2:
            continue
        adm0 = (p.get("adm0_a3") or "").strip()
        name = (p.get("name") or "").strip()
        alt = (p.get("name_alt") or "").replace("\n", " ").strip()
        typ = (p.get("type_en") or p.get("type") or "").strip()
        key = iso2
        rows.append(
            {
                "iso_3166_2": iso2,
                "adm0_a3": adm0,
                "name": name,
                "name_alt": alt,
                "type_en": typ,
                "value": f"{_placeholder_value(key):.2f}",
            }
        )
    rows.sort(key=lambda r: (r["adm0_a3"], r["iso_3166_2"]))
    with OUT_ADMIN1.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["iso_3166_2", "adm0_a3", "name", "name_alt", "type_en", "value"],
            quoting=csv.QUOTE_MINIMAL,
        )
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main() -> None:
    g0 = _load_or_fetch(ADMIN0_URL, CACHE_ADMIN0)
    g1 = _load_or_fetch(ADMIN1_URL, CACHE_ADMIN1)
    n0 = write_countries(g0)
    n1 = write_admin1(g1)
    print(f"Wrote {OUT_COUNTRIES} ({n0} countries / territories)")
    print(f"Wrote {OUT_ADMIN1} ({n1} admin-1 regions)")
    print(
        "\nUse with Data Mapper:\n"
        "  • Map geography → World — countries → paste/join on column iso3\n"
        "  • Map geography → World — states / provinces → paste/join on column iso_3166_2\n"
        "Replace `value` with your real indicator; placeholder is only for quick coloring tests."
    )


if __name__ == "__main__":
    main()
