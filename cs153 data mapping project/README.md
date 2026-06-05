# CS 153 — Data Mapping Project

**Data Mapper** turns location-based CSVs into an interactive choropleth or point map with search, layered datasets, insights, and export tools. It is built for social-impact work: comparing regions, spotting disparities, and sharing findings in reports or grant proposals.

This repository contains the Python generator (`data-mapping.py`), the HTML UI shell, sample datasets, and scripts to build Natural Earth–aligned reference CSVs.

**Maintaining this README:** When you change behavior, file layout, CLI flags, or dependencies, update this document in the same change so it stays accurate.

---

## Table of contents

- [Quick start](#quick-start)
- [Recommended workflow (`--serve`)](#recommended-workflow--serve)
- [Architecture](#architecture)
- [Command-line reference](#command-line-reference)
- [Using the map UI](#using-the-map-ui)
- [Upload your own CSV](#upload-your-own-csv)
- [Discover & search datasets](#discover--search-datasets)
- [Geography profiles](#geography-profiles)
- [Layers, colors & display](#layers-colors--display)
- [Analysis, AI & exports](#analysis-ai--exports)
- [Sample & reference data](#sample--reference-data)
- [Reference CSV generator](#reference-csv-generator)
- [Project files](#project-files)
- [Dependencies](#dependencies)
- [Troubleshooting](#troubleshooting)
- [Credits & data sources](#credits--data-sources)
- [AI disclosure](#ai-disclosure)

---

## Quick start

From this directory:

```bash
python3 data-mapping.py sample_countries.csv
```

This generates `data_map.html` and opens it in your browser. Use `--no-open` to skip opening the browser.

For the **full experience** (dataset search, CSV uploads with boundaries, OpenAI relay), use the local server:

```bash
python3 data-mapping.py sample_countries.csv --serve --port 8080
```

Then open **http://127.0.0.1:8080/data_map.html** (hard refresh with `Cmd+Shift+R` / `Ctrl+Shift+R` after regenerating).

Run with **no CSV argument** for a blank map and in-app data discovery:

```bash
python3 data-mapping.py --serve --port 8080
```

---

## Recommended workflow (`--serve`)

| Open as | What works |
|--------|------------|
| `http://127.0.0.1:8080/...` | World Bank, Our World in Data, Natural Earth boundaries via **`/dm-proxy`**, OpenAI via **`/dm-openai`**, CSV upload, choropleth preview/add |
| `file://.../data_map.html` | Static map only; **live fetches and boundaries often fail** |

The built-in server provides:

- **GET `/dm-proxy?url=...`** — browser-safe proxy for allowed hosts (World Bank, Our World in Data, GitHub raw/gist, Natural Earth GeoJSON)
- **POST `/dm-openai`** — relays OpenAI chat completions so **Analysis → Ask AI** works without browser CORS errors

Press **Ctrl+C** in the terminal to stop the server.

If port 8080 is busy, the server may pick another port (e.g. 8081) and print the URL.

---

## Architecture

| Piece | Role |
|-------|------|
| **`data-mapping.py`** | Main tool: reads CSV, builds Folium/Leaflet map, injects sidebar HTML + JavaScript, writes merged HTML |
| **`data_map_template.html`** | Shell template; recreated when `DM_TEMPLATE_VERSION` in Python is newer than the template comment |
| **`data_map.html`** | **Generated output** — safe to regenerate; not the source of truth for UI logic |
| **Injected JS** | Large UI block inside `_get_map_ui_js()` in `data-mapping.py` (Discover, upload, layers, insights, settings) |

**Template version:** `DM_TEMPLATE_VERSION` in `data-mapping.py` (currently tracks UI/JS behavior). Bumping it forces template regeneration on the next run.

Most sidebar structure lives in `build_sidebar_html()`; interactive behavior lives in the injected JavaScript string.

---

## Command-line reference

### Typical commands

| Command | Purpose |
|--------|---------|
| `python3 data-mapping.py your.csv` | Build `data_map.html` and open it |
| `python3 data-mapping.py your.csv -o out.html` | Custom output path |
| `python3 data-mapping.py your.csv --no-open` | Generate only |
| `python3 data-mapping.py your.csv --serve` | Local web server until Ctrl+C |
| `python3 data-mapping.py your.csv --serve --port 8080` | Server on a specific port |
| `python3 data-mapping.py your.csv --report report.txt` | Also write a narrative impact report |
| `python3 data-mapping.py your.csv --benchmark 70 --title "Life expectancy"` | Flag values below a target |

### Useful flags

| Flag | Description |
|------|-------------|
| `--title` | Legend / metric title |
| `--colormap` | Matplotlib colormap (e.g. `YlOrRd`, `viridis`, `plasma`) |
| `--metric COLUMN` | Map only one numeric column |
| `--mode auto\|countries\|points` | Choropleth vs point map (auto detects from CSV) |
| `--zoom`, `--center LAT,LON` | Map view |
| `--geojson FILE` + `--key-on` + `--name-property` | Custom region boundaries |
| `--country-context COUNTRY` | Geocoding hint for place names (needs `geopy`) |
| `--no-insights` | Skip embedded takeaways in generated HTML |
| `--benchmark N` | Highlight locations below threshold |
| `--report FILE` | Export text impact report |

---

## Using the map UI

The sidebar has five main tabs:

| Tab | Purpose |
|-----|---------|
| **Search** | Find datasets, set geography, upload CSVs |
| **Analysis** | Takeaways, charts, Ask AI, goal & sharing |
| **Layers** | Toggle layers, colors, display, basemap |
| **Plan** | Action checklist & vision board |
| **Research** | Sources, notes, highlights |

### Search tab sub-panels

- **Discover** — Quick-start topic buttons + search results with **Preview**, **Add to map**, and **Add as layer**
- **Regions CSV** — Upload or paste choropleth data (countries or states/provinces)
- **Points CSV** — Upload or paste lat/lon point data

Toolbar actions (top of map): export PNG, PDF/print, download CSV, fullscreen, locate, share link.

---

## Upload your own CSV

Upload works when the map is served over **`http://`** (see [Recommended workflow](#recommended-workflow--serve)). After picking a file, data is loaded and added to the map automatically.

### Region CSV (choropleth — countries or states)

**Search → Regions CSV → Upload & add**

Header row required. The app auto-detects region and value columns.

#### ISO3 countries (world map)

```csv
ISO3,value
USA,78.2
CAN,82.1
IND,70.4
```

Or use a `country` column with three-letter codes (see `world_FULL_all_countries_map_ready.csv`).

#### US states / provinces (ISO 3166-2)

Use codes like **`US-CA`**, **`US-NY`**, **`US-TX`** (not bare `CA` or full state names unless you choose name matching).

```csv
location,life_expectancy
US-CA,81.0
US-NY,78.5
US-TX,75.2
US-FL,79.2
```

See `sample_us_states_le.csv` for a full 50-state + DC example.

#### India / Canada / Australia samples

Bundled examples use ISO 3166-2 style codes (`IN-...`, `CA-...`, etc.) on the **states / provinces** geography.

#### Optional column hints

If auto-detection fails, fill in:

- **Region column** — e.g. `code`, `location`, `state`
- **Value column** — e.g. `life_expectancy`, `value`
- **Year column** — if multiple years per region
- **Key mode** — Auto, ISO 3166-2, ISO3, or Names

Then use **Preview**, **Add to map**, or **Add as layer** (keeps existing layers).

### Points CSV (lat/lon dots)

**Search → Points CSV → Upload & add**

```csv
lat,lon,name,value
40.7128,-74.0060,New York,85
34.0522,-118.2437,Los Angeles,72
41.8781,-87.6298,Chicago,68
```

Supported column names:

- Latitude: `lat`, `latitude`
- Longitude: `lon`, `lng`, `long`, `longitude`
- Optional: `name` / `location`, `value`

You can also paste CSV, load from URL, or click **Add points to map** after editing the textarea.

### Adjust point dot size

**Layers → Display → Point dot size (lat/lon)** — slider from 3px to 40px. Updates live for existing and new points; preference is saved in the browser.

---

## Discover & search datasets

The **Search** box queries a curated catalog:

| Source | Geography | Key format |
|--------|-----------|------------|
| **World Bank** indicators | Countries | ISO3 |
| **Our World in Data** grapher CSVs | Countries (mostly) | ISO3 / ISO 3166-2 |
| **Bundled samples** | India, US, Canada, Australia, Mexico states | ISO 3166-2 |

**Quick start buttons** (life expectancy, GDP, poverty, CO₂, India states, US states, Canada) run a search and scroll results into view.

For each result:

- **Preview on map** — colors regions without saving as the active dataset
- **Add to map** — replaces the active choropleth layer
- **Add as layer (keep existing)** — stacks another metric

**Year** dropdown appears when the dataset has multiple years.

**Important:** World Bank and OWID datasets are primarily **country-level**. State/province data should use ISO 3166-2 on the **World — states / provinces** geography.

---

## Geography profiles

**Search → Map geography**

| Profile | Boundaries | Best for |
|---------|------------|----------|
| **World — countries** | Natural Earth admin-0 | ISO3 codes (`USA`, `IND`, …) |
| **World — states / provinces** | Natural Earth admin-1 | ISO 3166-2 (`US-CA`, `IN-MH`, …) |

When you upload or add a dataset, geography often switches automatically to match your key type.

For admin-1, you can optionally **focus on one country** (e.g. show only US states) via the country filter dropdown.

Boundaries are fetched through `/dm-proxy` when served; they are kept in memory as `window._dmGeoJsonData` (not stored in `localStorage` due to size).

---

## Layers, colors & display

**Layers tab** sub-panels:

### Layers

Toggle datasets on/off, change per-layer color ramp and fill pattern. Click **Refresh** if the list is empty after adding data.

### Colors

Switch active metric, colormap (YlOrRd, viridis, Blues, …), reverse scale, and fill opacity.

### Display

- Border weight, color, opacity (choropleth regions)
- **Point dot size** for lat/lon markers
- Tooltip options (labels / values on hover)

### Basemap

Voyager, Positron (light), or Dark Matter.

---

## Analysis, AI & exports

### Analysis tab

- **Overview** — Live takeaways from mapped data (range, disparity, top/bottom regions)
- **Charts** — Bar charts, histograms, scatter plots from active layers
- **Ask AI** — Questions about the map; optional OpenAI key for narrative answers
- **Goal** — Set impact goal, reflection note, share-for-impact

### OpenAI setup

1. Add API key under **Analysis → Ask AI** (stored locally in the browser)
2. Run with **`--serve`** so requests go to **`/dm-openai`** on the same host
3. Choose mode: Observations only, AI, or both

AI dataset suggestions and AI-generated region CSVs also use this relay when served.

### Exports

- **Export PNG** — snapshot via html2canvas
- **Save PDF** — opens print dialog (Save as PDF)
- **Download CSV** — active metric values
- **Impact report** — via CLI `--report` flag

Research tab supports sources, notes, highlights, and bibliography export.

---

## Sample & reference data

| File | Description |
|------|-------------|
| `sample_countries.csv` | Minimal country example: `ISO3`, `value` |
| `sample_us_states_le.csv` | All US states + DC: `location` (`US-XX`), `life_expectancy` |
| `india_full_dataset.csv` | India states with multiple numeric columns (`IN-XX` codes) |
| `australia_states_population.csv` | Australian states sample |
| `canada_provinces_population.csv` | Canadian provinces sample |
| `world_countries_natural_earth.csv` | All countries keyed by `iso3` (template from generator) |
| `world_admin1_natural_earth.csv` | All admin-1 regions keyed by `iso_3166_2` |
| `world_FULL_all_countries_map_ready.csv` | Wide country dataset (`country` + many indicators) |

---

## Reference CSV generator

`generate_world_coverage_csvs.py` builds two CSVs aligned with Natural Earth geometry (same family as the mapper’s world layers):

1. `world_countries_natural_earth.csv` — admin-0, keyed by **`iso3`**
2. `world_admin1_natural_earth.csv` — admin-1, keyed by **`iso_3166_2`**

```bash
python3 generate_world_coverage_csvs.py
```

Uses on-disk JSON caches when present; otherwise downloads once. Replace the placeholder **`value`** column with your real indicator.

---

## Project files

| File | Role |
|------|------|
| `data-mapping.py` | Generator + injected UI JavaScript |
| `data_map_template.html` | HTML shell (auto-refreshed on version bump) |
| `data_map.html` | Generated map output |
| `blank_map.html` | Alternate blank-map HTML |
| `generate_world_coverage_csvs.py` | Build world coverage CSV templates |
| `.ne_*_cache.json`, `.subnational_*_cache.json`, `.world_countries_cache.json` | Downloaded GeoJSON caches (large; optional in git) |

---

## Dependencies

There is no `requirements.txt` yet. `data-mapping.py` expects at least:

```bash
pip install pandas folium matplotlib numpy
```

Optional:

```bash
pip install geopy   # geocoding place names in point mode when lat/lon are missing
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|--------|--------------|-----|
| Preview / Add does nothing | Opened as `file://` | Use `--serve` and `http://127.0.0.1:...` |
| “Map outline did not load” | Proxy/boundaries blocked | Same as above; check terminal for errors |
| CSV upload parses 0 rows | Old cached HTML | Regenerate map, hard refresh |
| US states not coloring | Wrong codes or geography | Use `US-CA` format; set geography to **states / provinces** |
| OpenAI / Ask fails | No relay | `--serve`; key in Ask AI panel |
| Layers panel empty | Timing / no data yet | Add a dataset, click **Refresh** |
| Port in use | Another server on 8080 | Use `--port 8081` or stop the other process |

After changing `data-mapping.py`, regenerate:

```bash
python3 data-mapping.py sample_countries.csv --serve --port 8080
```

---

## Credits & data sources

- **[Natural Earth](https://www.naturalearthdata.com/)** — world country and admin-1 boundaries (via cached GeoJSON, e.g. [nvkelso/natural-earth-vector](https://github.com/nvkelso/natural-earth-vector))
- **[World Bank Open Data](https://data.worldbank.org/)** — indicator API
- **[Our World in Data](https://ourworldindata.org/)** — grapher CSV exports
- **Leaflet / Folium** — interactive mapping

When using `--serve`, proxied hosts are restricted; see `DM_PROXY_ALLOWED_NETLOCS` in `data-mapping.py` for the allowlist.

---

## AI disclosure

This project was developed for **CS 153** with substantial assistance from AI coding tools:

- **[Cursor](https://cursor.com/)** — IDE, agent workflows, and in-editor AI assistance used to implement, debug, and iterate on `data-mapping.py`, the injected map UI, CSV upload pipeline, Discover/search flow, choropleth preview/add, layer controls, and documentation.
- **Claude** (via Cursor and **Claude Code**) — used for architecture suggestions, bug fixes (including CSV line parsing and map layer rendering), UI polish, and README updates.

**Human role:** Problem framing, requirements, testing in the browser, feedback on what worked or failed, and final review of features and data formats.

**Limitations:** AI-generated code can miss edge cases. Always verify maps against known data, use `--serve` for live features, and treat exported insights as drafts for human review before publication or grant submission.

If you extend this project, document your own tool use and validate geographic joins (ISO3 vs ISO 3166-2) on real datasets.

---

*Last README update: 2026-06-04 — CSV upload, choropleth preview/add, point dot size, and AI disclosure.*
