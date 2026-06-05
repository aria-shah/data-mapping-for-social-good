# CS 153 — Data Mapping Project

**Data Mapper** turns location-based CSVs into an interactive choropleth or point map with search, layered datasets, insights, and export tools. It is built for social-impact work: comparing regions, spotting disparities, and sharing findings in reports or grant proposals.

This repository contains the Python generator (`data-mapping.py`), the HTML UI shell, sample datasets, and scripts to build Natural Earth–aligned reference CSVs.

**Maintaining this README:** When you change behavior, file layout, CLI flags, or dependencies, update this document in the same change so it stays accurate.

---

## Project overview

If you are new to this repo, start here for the problem, what was built, how it was tested, and how AI was used. Step-by-step usage is in [Quick start](#quick-start) below.

```bash
pip install pandas folium matplotlib numpy
python3 data-mapping.py sample_countries.csv --serve --port 8080
# Open http://127.0.0.1:8080/data_map.html
# Try: Search → "life expectancy" → Preview → Add to map
# Try: Search → Regions CSV → Upload sample_us_states_le.csv
```

---

## Problem & insight

### Meaningful problem

Public-interest researchers, students, and advocates often have **tabular data tied to places** (countries, states, cities) but lack a simple way to **see spatial patterns**, **compare regions**, and **communicate disparities** without learning GIS software or standing up a full web app.

Typical pain points this project targets:

- CSVs use inconsistent geography keys (ISO3 vs ISO 3166-2 vs place names)
- Public datasets live on different APIs (World Bank, Our World in Data) with browser CORS barriers
- Choropleth tools are either too simple (static plots) or too heavy (full-stack dashboards)
- Impact work needs **takeaways and exportable artifacts**, not just a pretty map

### Motivation

The goal is to lower the barrier between **“I have a spreadsheet”** and **“I can show where help is needed most”** — for grant writing, policy briefs, classroom analysis, and advocacy. Maps make inequality visible; the app adds lightweight analysis (range, disparity, charts, optional AI narrative) on top of the visualization.

### Approach

Rather than a one-off notebook or static Folium export, this project combines:

1. **CLI + generated interactive HTML** — one Python script produces a self-contained map page anyone can open
2. **In-browser data workspace** — search curated catalogs, preview/add layers, upload custom CSVs, stack multiple metrics
3. **Dual geography model** — world countries (ISO3) and world admin-1 (ISO 3166-2) with automatic profile switching
4. **Local dev server** — `/dm-proxy` and `/dm-openai` relays so browsers can fetch external data safely when `--serve` is used
5. **Impact-oriented sidebar** — not just mapping: takeaways, benchmarks (`--benchmark`), impact reports (`--report`), research notes, action planning

The stack is intentionally **HTML + injected JavaScript inside Python** (Phase 1) for fast iteration without a separate frontend build step — a pragmatic tradeoff documented under [limitations](#known-limitations).

---

## Execution & technical work

### What was built

| Component | Description |
|-----------|-------------|
| **`data-mapping.py`** (~8,500+ lines) | CSV ingestion, choropleth/point detection, Folium map generation, embedded HTTP server, proxy allowlist, OpenAI relay, sidebar HTML builder, ~6,000+ lines of injected client JS |
| **Interactive UI** | Search/Discover, Preview/Add/layer stacking, region & point CSV upload, geography selector, admin-1 country focus, layer controls, colormaps, basemaps, charts, Ask AI, research & plan tabs |
| **`generate_world_coverage_csvs.py`** | Builds Natural Earth–aligned country and admin-1 template CSVs |
| **Sample datasets** | Country, US-state, India, Canada, Australia examples for testing and demos |

### How to use it

- **Entry point:** `python3 data-mapping.py [csv] --serve` → open the printed `http://127.0.0.1:...` URL
- **Primary flows:** Discover dataset → Preview/Add; upload region CSV; upload lat/lon CSV; adjust layers/colors; export PNG/CSV/report
- **Documented below:** [Quick start](#quick-start), [Upload your own CSV](#upload-your-own-csv), [Troubleshooting](#troubleshooting)

### Technical scope

Scope matches a **full-stack-ish data app** implemented as a single-repo generator:

- Python: pandas normalization, Folium/Leaflet maps, matplotlib colormaps, HTTP server with proxy
- JavaScript: Leaflet layer management, async fetch pipelines, CSV parsing, localStorage state, chart rendering, OpenAI client relay
- Geo: Natural Earth GeoJSON join on `ISO_A3` / `iso_3166_2`, admin-1 name aliasing, country filter

### Iteration over time

Development proceeded in visible phases (tracked via `DM_TEMPLATE_VERSION` in `data-mapping.py`):

| Phase | Focus | Examples |
|-------|--------|----------|
| Core | CSV → choropleth map, CLI flags, impact report | `--benchmark`, `--report`, Folium output |
| Data workspace | Discover search, World Bank / OWID / bundled samples | Preview, Add, multi-layer |
| Subnational | Admin-1 global profile, ISO 3166-2 matching | US/India/Canada samples, auto geography |
| Stability | Preview/add pipeline, CSV newline parsing, layer `addTo(map)` | Choropleth visible on first Preview |
| UX | CSV file upload, point dot size slider, UI refresh | Regions/Points upload & add |
| Docs | README for reproducibility and onboarding | This file |

Each phase responded to **real failures** (e.g. maps not coloring, CSVs parsing as one line, `file://` blocking fetches) rather than feature creep alone.

---

## Evaluation & evidence

### How claims were validated

| Claim | Validation method |
|-------|-------------------|
| Choropleth joins work for ISO3 | `sample_countries.csv` → map colors ~15 countries; World Bank life expectancy via Discover |
| Admin-1 joins work for US states | `sample_us_states_le.csv` upload → states color by `US-XX` codes |
| Custom CSV upload works | File picker + paste; fixed `\r?\n` line splitting bug; auto-detect columns |
| External APIs reachable | `--serve` + `/dm-proxy` → World Bank JSON, Natural Earth GeoJSON, OWID CSV |
| Preview vs Add behavior | Manual browser testing: Preview colors map; Add persists metric + Insights update |
| Point maps work | CSV with `lat`/`lon` → circle markers; size slider in Layers → Display |

### Benchmarks & comparisons

- **`--benchmark N`** — flags regions below a threshold (e.g. life expectancy &lt; 70) for priority messaging
- **Side-by-side metrics** — “Add as layer” stacks datasets; scatter plot in Analysis → Charts when ≥2 layers share region keys
- **Reference CSVs** — `world_countries_natural_earth.csv` / `world_admin1_natural_earth.csv` align row keys with boundary properties for join sanity checks

### Failure analysis

See [Known limitations](#known-limitations) and [Troubleshooting](#troubleshooting). Major issues encountered and addressed:

1. **`file://` vs `http://`** — browsers block cross-origin fetches; solution: require `--serve` for live data (banner shown in UI)
2. **Broken CSV split regex** — `\r?\n` was emitted incorrectly; entire files parsed as one row; fixed with `splitCsvLines()`
3. **Preview without visible layer** — GeoJSON layer created but never `addTo(map)`; fixed in choropleth pipeline
4. **localStorage quota** — full GeoJSON cannot persist; boundaries kept in `window._dmGeoJsonData` only
5. **OWID CSV format drift** — dual URL fallback and stricter column detection

### Open questions

- Formal user studies or expert review sessions (project validated via developer testing and sample datasets)
- Automated end-to-end browser test suite (manual QA documented here)
- Production deployment / multi-user hosting (local server only)

---

## Documentation & demo

This README is written for **someone outside the project** — a collaborator, reviewer, or future contributor — with:

- **Reproducible commands** (install, run, demo paths)
- **CSV format examples** copy-paste ready
- **Architecture table** explaining which file does what
- **Troubleshooting** for common failures
- **Table of contents** for navigation

**Suggested demo script (2–3 min video or live):**

1. Start server with `sample_countries.csv`
2. Search “life expectancy” → Preview → Add
3. Upload `sample_us_states_le.csv` under Regions CSV
4. Open Analysis → Overview and Charts
5. Layers → Display → resize point dots (if points loaded)
6. Export PNG or Download CSV

---

## Process, integrity & disclosure

### AI usage

See full [AI disclosure](#ai-disclosure). Summary:

- **Cursor** and **Claude (including Claude Code)** assisted with implementation, debugging, UI iteration, and documentation
- **Human work:** problem definition, requirements, browser testing, feedback when features failed, CSV format decisions, final review

AI was used as an **accelerator**, not a substitute for verification — geographic joins and served-vs-file behavior were validated manually.

### Sources & borrowed code (cited)

This project **does not fork a single upstream app repo**. It builds on **documented libraries and public data**, cited in [Credits & data sources](#credits--data-sources):

| Dependency | Role | License / terms |
|------------|------|-----------------|
| [Folium](https://python-visualization.github.io/folium/) / [Leaflet](https://leafletjs.com/) | Map rendering | BSD-style (see project pages) |
| [Natural Earth](https://www.naturalearthdata.com/) / [nvkelso/natural-earth-vector](https://github.com/nvkelso/natural-earth-vector) | Boundary GeoJSON | Public domain / see repo |
| [World Bank API](https://data.worldbank.org/) | Indicator data | Open data terms |
| [Our World in Data](https://ourworldindata.org/) | Grapher CSVs | CC BY (see OWID) |
| pandas, matplotlib, numpy | Data & color scales | BSD / standard OSS |

**Substantial original work:** `data-mapping.py` UI injection, Discover catalog, proxy server, choropleth/layer pipeline, CSV upload UX, insights sidebar, and sample datasets — integrated and extended beyond any single tutorial.

### Major decisions

| Decision | Rationale |
|----------|-----------|
| HTML + injected JS (not React) | Faster iteration for a course timeline; single artifact output |
| `--serve` required for live features | Browser CORS; local proxy is minimal and auditable |
| ISO3 vs ISO 3166-2 profiles | Matches Natural Earth properties; explicit user/upload hints reduce join errors |
| Strip `geo_json` from localStorage | Avoid 5MB quota failures on admin-1 boundaries |
| Template version bump | Forces HTML shell refresh when JS behavior changes |

### Known limitations

- **Local server only** — not deployed as a hosted SaaS
- **Proxy allowlist** — only approved hosts via `/dm-proxy` (see `DM_PROXY_ALLOWED_NETLOCS`)
- **AI features optional** — require user OpenAI key + `--serve`; narratives should be human-reviewed
- **Name-based region matching** — less reliable than ISO codes for admin-1
- **No git history in this folder at time of README update** — development tracked via file versions (`DM_TEMPLATE_VERSION`), sample data, and iterative README; initialize git for commit history if submitting a public repo

### Development history

- Multiple `DM_TEMPLATE_VERSION` strings in `data-mapping.py` (e.g. csv-upload-fix, point-radius, preview-add-fix)
- Growing sample CSV library (countries, US states, India, Canada, Australia, world templates)
- README expanded from quick-start to full project and operator documentation
- Iterative bug fixes documented in [Evaluation & evidence](#evaluation--evidence)

**If sharing publicly:** initialize a git repository, push commits reflecting the phases above, and add the repo URL here.

---

## Table of contents

- [Project overview](#project-overview)
- [Problem & insight](#problem--insight)
- [Execution & technical work](#execution--technical-work)
- [Evaluation & evidence](#evaluation--evidence)
- [Documentation & demo](#documentation--demo)
- [Process, integrity & disclosure](#process-integrity--disclosure)
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

This project was developed for **CS 153** with substantial assistance from AI coding tools.

### Tools used

| Tool | How it was used |
|------|-----------------|
| **[Cursor](https://cursor.com/)** | Primary IDE; agent-assisted editing, debugging, and multi-file changes to `data-mapping.py` and generated HTML |
| **Claude** (via Cursor agents) | Architecture suggestions, choropleth/upload bug fixes, README drafting |
| **Claude Code** | Extended debugging sessions, stability passes (Preview/Add pipeline, CSV parsing) |

### Human vs. AI responsibilities

| Human (author) | AI assistance |
|--------------|---------------|
| Defined the social-impact mapping problem and feature priorities | Implemented and refactored large JS/Python blocks from specs |
| Tested in browser; reported “Preview doesn’t work”, “upload broken”, etc. | Diagnosed root causes (regex, missing `addTo(map)`, CORS) |
| Chose CSV key formats (ISO3, US-CA) and sample datasets | Wrote parsing, UI wiring, and documentation |
| Final review of what ships and what limitations to disclose | Suggested troubleshooting tables and README structure |

### What to verify independently

- Geographic joins on **your** CSV (ISO3 vs ISO 3166-2)
- Run with **`--serve`** for live data — do not rely on `file://`
- Treat AI-generated insights and narratives as **drafts** for human review
- AI-generated code may miss edge cases; see [Evaluation & evidence](#evaluation--evidence)

If you extend this project, document your own tool use and validate results on real data.

---

*Last README update: 2026-06-04.*
