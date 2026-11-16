// Common Ground Atlas: application state and orchestration.
import { $, $$, h, esc, api, fmt, fmtPeople, flagEmoji, debounce, toast, busy, download, uid } from './util.js';
import { loadGeo, countryGazetteer, admin1Gazetteer } from './geo.js';
import { buildScale, defaultPalette, paletteCss, PALETTES, bivariateScale, BIVARIATE_VIVID } from './palettes.js';
import { CATALOG, QUICK_START, SDGS, findCatalog } from './catalog.js';
import { FlatMap } from './map2d.js';
import { GlobeView } from './globe.js';
import { renderInsights, buildStory, correlationsFor, benchmarkResult, priorityDirection } from './insights.js';
import { openImporter, closeImporter, autoLayers } from './importer.js';
import { initSearch, runSearch, renderHome } from './search.js';
import { exportPNG, exportCSV, exportBrief } from './export.js';

const STORE_KEY = 'cg-atlas-v2';

const state = {
  layers: [],
  activeId: null,
  view: 'globe', // globe | map | tilt
  year: null, // null = latest available per place
  fillGaps: true,
  heightBy: 'same', // same | none | <layerId>
  compareId: null, // bivariate partner (flat map)
  basemap: 'none',
  texture: 'midnight',
  benchmark: null,
  selected: null, // {key, geo}
  theme: 'dark',
  autoRotate: true,
  heightScale: 1,
  pointStyle: 'dots',
  otherId: null,
  leftOpen: true,
  rightOpen: true,
  tab: 'discover',
};

let flat, globe, countryInfo, admin1Info;
let playing = null;
let current = { slice: null, scale: null };

// ------------------------------------------------------------------ slicing

function infoFor(key, geo) {
  return geo === 'admin1' ? admin1Info?.get(key) : countryInfo?.get(key);
}

function nameOf(key, geo) {
  const inf = infoFor(key, geo);
  if (inf?.name) return geo === 'admin1' && inf.country ? inf.name : inf.name;
  return key;
}

function sliceFor(layer, year = state.year) {
  const entries = [];
  const used = new Set();
  if (layer.geo === 'points') {
    layer.points.forEach((p, i) => {
      if (p.v != null && Number.isFinite(p.v)) entries.push({ key: `p${i}`, v: p.v, name: p.label || `Point ${i + 1}`, info: null });
    });
  } else {
    for (const [key, vals] of Object.entries(layer.values)) {
      let v = null, y = null;
      if (!layer.years.length) v = vals.NA ?? null;
      else if (year == null) {
        for (let i = layer.years.length - 1; i >= 0; i--) { const yy = layer.years[i]; if (vals[yy] != null) { v = vals[yy]; y = yy; break; } }
      } else {
        for (let k = 0; k <= (state.fillGaps ? 3 : 0); k++) { if (vals[year - k] != null) { v = vals[year - k]; y = year - k; break; } }
      }
      if (v == null || !Number.isFinite(v)) continue;
      if (y != null) used.add(y);
      const info = infoFor(key, layer.geo);
      entries.push({ key, v, y, name: info?.name || layer.names?.[key] || key, info });
    }
  }
  entries.sort((a, b) => b.v - a.v);
  const byKey = new Map(entries.map((e) => [e.key, e]));
  const rank = new Map(entries.map((e, i) => [e.key, i + 1]));
  let yearLabel = '';
  if (layer.years?.length) {
    const ys = [...used].sort((a, b) => a - b);
    if (year != null) yearLabel = state.fillGaps && ys.length > 1 ? `${year} (gaps filled from up to 3 earlier years)` : String(year);
    else yearLabel = ys.length > 1 ? `Latest available (${ys[0]}–${ys.at(-1)})` : ys.length ? String(ys[0]) : '';
  }
  const scale = buildScale(entries.map((e) => e.v), layer.style);
  return { layer, entries, byKey, rank, yearLabel, scale, logScale: scale.log };
}

const activeLayer = () => state.layers.find((l) => l.id === state.activeId) || null;
const regionLayers = (geo) => state.layers.filter((l) => l.geo !== 'points' && (!geo || l.geo === geo));

// ------------------------------------------------------------------ rendering

function choroplethLayer() {
  const a = activeLayer();
  if (a && a.geo !== 'points' && a.visible) return a;
  return [...state.layers].reverse().find((l) => l.geo !== 'points' && l.visible) || null;
}

const renderMap = async () => {
  const L = choroplethLayer();
  const A = activeLayer();
  let colors = null, rings = [], legend = null, slice = null;
  if (L) {
    slice = sliceFor(L);
    const scale = slice.scale;
    colors = new Map();
    const partner = state.compareId && state.view !== 'globe' ? state.layers.find((l) => l.id === state.compareId && l.geo === L.geo && l.id !== L.id) : null;
    let biv = null, pslice = null;
    if (partner) {
      pslice = sliceFor(partner);
      const keys = slice.entries.filter((e) => pslice.byKey.has(e.key));
      biv = bivariateScale(keys.map((e) => pslice.byKey.get(e.key).v), keys.map((e) => e.v));
    }
    let hslice = null;
    const hl = state.heightBy !== 'same' && state.heightBy !== 'none' ? state.layers.find((l) => l.id === state.heightBy && l.geo === L.geo) : null;
    if (hl) hslice = sliceFor(hl);
    const bm = state.benchmark != null && A === L ? benchmarkResult(slice, state.benchmark) : null;
    const hits = new Set(bm ? bm.hits.map((e) => e.key) : []);
    for (const e of slice.entries) {
      let color = scale.color(e.v);
      if (biv) {
        const pv = pslice.byKey.get(e.key)?.v;
        color = biv.color(pv, e.v);
        if (!color) continue;
      }
      const hEntry = hslice ? hslice.byKey.get(e.key) : null;
      colors.set(e.key, { color, t: scale.t(e.v), h: hslice ? (hEntry ? hslice.scale.t(hEntry.v) : 0) : undefined, priority: hits.has(e.key) });
    }
    if (bm) {
      for (const e of bm.hits) {
        const inf = e.info;
        if (inf && inf.lx != null) rings.push({ lat: inf.ly, lng: inf.lx });
      }
    }
    legend = biv ? { type: 'bivariate', x: partner, y: L, biv } : scale.legend;
    current.choroSlice = slice;
  } else current.choroSlice = null;

  const points = [];
  for (const pl of state.layers.filter((l) => l.geo === 'points' && l.visible)) {
    const vals = pl.points.map((p) => p.v).filter((v) => v != null && Number.isFinite(v));
    const sc = buildScale(vals, { ...pl.style, classes: 'continuous' });
    const [lo, hi] = vals.length ? [Math.min(...vals), Math.max(...vals)] : [0, 1];
    for (const p of pl.points) {
      const t = p.v != null && hi > lo ? (p.v - lo) / (hi - lo) : 0.5;
      points.push({ lat: p.lat, lon: p.lon, color: p.v != null && vals.length ? sc.color(p.v) : '#f472b6', t, r: 3 + Math.sqrt(t) * 9, label: p.label, v: p.v, display: p.v != null ? fmt(p.v, pl.unit) : '', layer: pl.title });
    }
  }

  const extrude = state.heightBy !== 'none';
  if (state.view === 'globe') {
    await globe.render({ geo: L?.geo || 'countries', colors, rings, points, extrude, heightScale: state.heightScale, countryOf: (k) => admin1Info?.get(k)?.iso3 });
  } else {
    await flat.render({ geo: L?.geo || 'countries', colors, points, tilt: state.view === 'tilt' && extrude, heightScale: state.heightScale, pointStyle: state.pointStyle, opacity: L?.style.opacity ?? 0.9 });
  }
  renderLegend(L, legend, slice);
  const pointsOnly = A && A.geo === 'points';
  if (pointsOnly && !L) renderLegend(A, sliceFor(A).scale.legend, sliceFor(A));
};

function renderLegend(L, legend, slice) {
  const box = $('#legend');
  box.innerHTML = '';
  if (!L || !legend || legend.type === 'none') { box.classList.remove('show'); return; }
  box.classList.add('show');
  const unit = L.unit ? ` <span class="muted">(${esc(L.unit)})</span>` : '';
  if (legend.type === 'bivariate') {
    const grid = h('div', { class: 'biv-grid' });
    for (let r = 2; r >= 0; r--) for (let c = 0; c < 3; c++) grid.append(h('i', { style: { background: BIVARIATE_VIVID[r][c] } }));
    box.append(h('div', { class: 'legend-title' }, 'Bivariate comparison'),
      h('div', { class: 'biv' }, h('div', { class: 'biv-y' }, `↑ ${legend.y.title}`), grid, h('div', { class: 'biv-x' }, `${legend.x.title} →`)),
      h('div', { class: 'legend-note' }, 'Purple = high on both · pink = high → only · blue = high ↑ only'));
    return;
  }
  box.append(h('div', { class: 'legend-title', html: esc(L.title) + unit }));
  if (legend.type === 'continuous') {
    box.append(h('div', { class: 'legend-bar', style: { background: `linear-gradient(90deg, ${legend.gradient.join(',')})` } }),
      h('div', { class: 'legend-ticks' }, legend.ticks.map((t) => h('span', { style: { left: `${t.t * 100}%` } }, fmt(t.v)))));
  } else {
    const row = h('div', { class: 'legend-classes' });
    legend.colors.forEach((c, i) => {
      const n = slice ? slice.entries.filter((e) => slice.scale.classOf?.(e.v) === i).length : null;
      row.append(h('div', { class: 'legend-class', title: `${fmt(legend.breaks[i])} – ${fmt(legend.breaks[i + 1])}${n != null ? ` · ${n} places` : ''}` }, h('i', { style: { background: c } })));
    });
    box.append(row, h('div', { class: 'legend-breaks' }, legend.breaks.map((b) => h('span', {}, fmt(b)))));
  }
  const extra = [];
  if (slice?.yearLabel) extra.push(slice.yearLabel.replace(/ \(gaps.*\)/, ''));
  if (slice?.scale.log) extra.push('log scale');
  if (state.view !== 'map' && state.heightBy !== 'none') extra.push(state.heightBy === 'same' ? 'height = value' : `height = ${state.layers.find((l) => l.id === state.heightBy)?.title || ''}`);
  if (state.benchmark != null) extra.push(h('span', { class: 'pink' }, '■ benchmark'));
  box.append(h('div', { class: 'legend-note' }, h('i', { class: 'nodata' }), 'no data', extra.map((x) => [' · ', x])));
}

function renderTimeline() {
  const box = $('#timeline');
  const L = activeLayer();
  if (!L || !L.years || L.years.length < 2) { box.classList.remove('show'); document.body.classList.remove('has-timeline'); stopPlay(); return; }
  box.classList.add('show');
  document.body.classList.add('has-timeline');
  const years = L.years;
  const counts = years.map((y) => Object.values(L.values).filter((v) => v[y] != null).length);
  const maxC = Math.max(...counts);
  const idx = state.year == null ? years.length - 1 : Math.max(0, years.indexOf(state.year));
  box.innerHTML = '';
  const label = h('div', { class: 'tl-year' }, state.year == null ? 'Latest' : String(state.year));
  const slider = h('input', { type: 'range', min: 0, max: years.length - 1, value: idx, class: 'tl-range', 'aria-label': 'Year',
    oninput: (e) => { state.year = years[+e.target.value]; label.textContent = state.year; scheduleRender(true); } });
  const spark = h('div', { class: 'tl-spark' }, years.map((y, i) => h('i', { style: { height: `${8 + (counts[i] / maxC) * 22}px` }, class: y === state.year || (state.year == null && i === years.length - 1) ? 'on' : '', title: `${y}: ${counts[i]} places` })));
  box.append(
    h('button', { class: `tl-play ${playing ? 'on' : ''}`, 'aria-label': playing ? 'Pause' : 'Play', onclick: () => (playing ? stopPlay() : startPlay()) }, playing ? '❚❚' : '▶'),
    label,
    h('div', { class: 'tl-track' }, spark, slider, h('div', { class: 'tl-ends' }, h('span', {}, years[0]), h('span', {}, years.at(-1)))),
    h('div', { class: 'tl-opts' },
      h('button', { class: `chip ${state.year == null ? 'on' : ''}`, onclick: () => { state.year = null; stopPlay(); renderAll(); } }, 'Latest'),
      h('label', { class: 'check sm', title: 'Use the most recent value from up to 3 earlier years when a year is missing' }, h('input', { type: 'checkbox', checked: state.fillGaps, onchange: (e) => { state.fillGaps = e.target.checked; renderAll(); } }), h('span', {}, 'Fill gaps'))));
  document.body.style.setProperty('--tl-h', `${box.offsetHeight || 64}px`);
}

function startPlay() {
  const L = activeLayer();
  if (!L?.years?.length) return;
  const years = L.years.filter((y) => Object.values(L.values).filter((v) => v[y] != null).length >= 3);
  let i = state.year == null ? 0 : Math.max(0, years.indexOf(state.year));
  if (i >= years.length - 1) i = 0;
  playing = setInterval(() => {
    state.year = years[i];
    renderAll(true);
    i++;
    if (i >= years.length) stopPlay();
  }, 650);
  renderTimeline();
}

function stopPlay() {
  if (playing) clearInterval(playing);
  playing = null;
  if ($('#timeline .tl-play')) { $('#timeline .tl-play').textContent = '▶'; $('#timeline .tl-play').classList.remove('on'); }
}

function renderRightPanel() {
  const A = activeLayer();
  const root = $('#insights');
  if (!A) return renderInsights(root, {});
  const slice = sliceFor(A);
  current.slice = slice;
  current.scale = slice.scale;
  const others = A.geo === 'points' ? [] : correlationsFor(slice, regionLayers(A.geo).filter((l) => l.id !== A.id).map((l) => sliceFor(l)));
  if (others.length && !others.find((o) => o.id === state.otherId)) state.otherId = others[0].id;
  const ordered = others.length ? [others.find((o) => o.id === state.otherId), ...others.filter((o) => o.id !== state.otherId)] : [];
  renderInsights(root, {
    slice, scale: slice.scale, benchmark: state.benchmark, others: ordered, otherId: state.otherId, selectedKey: state.selected?.key,
    onSelect: (key) => selectRegion({ key, geo: A.geo }, true),
    onBenchmark: (v, live) => {
      state.benchmark = v == null || !Number.isFinite(v) ? null : v;
      if (live) { scheduleMapOnly(); const out = root.querySelector('.bench-num'); if (out) out.value = +v.toFixed(2); updateBenchOut(); } else renderAll();
    },
    onPickOther: (id) => { state.otherId = id; renderRightPanel(); },
  });
}

function updateBenchOut() {
  const A = activeLayer();
  if (!A || state.benchmark == null) return;
  const r = benchmarkResult(sliceFor(A), state.benchmark);
  const out = $('#insights .bench-out');
  if (!out) return;
  const bigs = out.querySelectorAll('.big');
  if (bigs[0]) bigs[0].textContent = r.hits.length;
  if (bigs[1]) bigs[1].textContent = r.pop ? fmtPeople(r.pop).replace(' billion', 'B').replace(' million', 'M').replace(' thousand', 'K') : '0';
  scheduleInsights();
}

const scheduleInsights = debounce(() => renderRightPanel(), 350);
const scheduleMapOnly = debounce(() => renderMap(), 30);
let rafPending = false;
function scheduleRender(light) {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; renderAll(light); });
}

function renderAll(light = false) {
  document.body.classList.toggle('has-data', state.layers.length > 0);
  renderMap();
  if (!light) renderTimeline();
  else {
    const L = activeLayer();
    $$('#timeline .tl-spark i').forEach((el, i) => el.classList.toggle('on', L?.years[i] === state.year));
    const lab = $('#timeline .tl-year');
    if (lab) lab.textContent = state.year ?? 'Latest';
    const sl = $('#timeline .tl-range');
    if (sl && L) sl.value = state.year == null ? L.years.length - 1 : L.years.indexOf(state.year);
  }
  light ? scheduleInsights() : renderRightPanel();
  renderRegionCard();
  persist();
}

// ------------------------------------------------------------------ layers panel

function renderLayersPanel() {
  const pane = $('#pane-layers');
  pane.innerHTML = '';
  $('#tab-layers .count').textContent = state.layers.length || '';
  if (!state.layers.length) {
    pane.append(h('div', { class: 'empty' }, h('div', { class: 'empty-orb' }), h('h3', {}, 'No layers yet'), h('p', { class: 'muted' }, 'Add datasets from Discover or Import. Stack several to compare them in 3D or as a bivariate map.'),
      h('button', { class: 'btn-primary', onclick: () => setTab('discover') }, 'Browse datasets')));
    return;
  }
  const regionLs = regionLayers();
  const L = choroplethLayer();
  const sameGeo = L ? regionLayers(L.geo).filter((l) => l.id !== L.id) : [];
  // View controls
  const viewBox = h('div', { class: 'card view-card' },
    h('div', { class: 'card-head' }, h('h4', {}, '🎛️ Visual encoding')),
    ctl('Height (3D views)', h('select', { class: 'select', onchange: (e) => { state.heightBy = e.target.value; renderAll(); } },
      h('option', { value: 'same', selected: state.heightBy === 'same' }, 'Same as color'),
      h('option', { value: 'none', selected: state.heightBy === 'none' }, 'Flat (no height)'),
      sameGeo.map((l) => h('option', { value: l.id, selected: state.heightBy === l.id }, `↕ ${l.title}`)))),
    ctl('Height scale', h('input', { type: 'range', min: 0.2, max: 3, step: 0.1, value: state.heightScale, oninput: (e) => { state.heightScale = +e.target.value; scheduleMapOnly(); persist(); } })),
    sameGeo.length ? ctl('Compare (bivariate, flat map)', h('select', { class: 'select', onchange: (e) => { state.compareId = e.target.value || null; if (state.compareId && state.view === 'globe') setView('map'); else renderAll(); } },
      h('option', { value: '' }, 'Off'), sameGeo.map((l) => h('option', { value: l.id, selected: state.compareId === l.id }, l.title)))) : null,
    state.view === 'globe'
      ? ctl('Globe surface', seg([['midnight', 'Midnight'], ['night', 'Night lights'], ['marble', 'Blue marble']], state.texture, (v) => { state.texture = v; globe.setTexture(v); persist(); }))
      : ctl('Basemap', seg([['none', 'Minimal'], ['dark', 'Dark'], ['light', 'Light'], ['satellite', 'Satellite']], state.basemap, (v) => { state.basemap = v; flat.setBasemap(v); persist(); })),
    state.layers.some((l) => l.geo === 'points') && state.view !== 'globe' ? ctl('Points', seg([['dots', 'Dots'], ['heat', 'Heatmap'], ['both', 'Both']], state.pointStyle, (v) => { state.pointStyle = v; renderAll(); })) : null);
  pane.append(viewBox);

  pane.append(h('div', { class: 'section-title' }, `Layers · ${state.layers.length}`, h('button', { class: 'link', onclick: () => { if (confirm('Remove all layers?')) { state.layers = []; state.activeId = null; state.benchmark = null; state.compareId = null; state.heightBy = 'same'; afterLayersChanged(); } } }, 'clear all')));
  state.layers.slice().reverse().forEach((l, ri) => {
    const isActive = l.id === state.activeId;
    const nPlaces = l.geo === 'points' ? l.points.length : Object.keys(l.values).length;
    const card = h('div', { class: `layer ${isActive ? 'active' : ''} ${l.visible ? '' : 'hidden'}` });
    const head = h('div', { class: 'layer-head', onclick: () => { state.activeId = l.id; state.year = l.years?.includes(state.year) ? state.year : null; state.benchmark = null; afterLayersChanged(false); } },
      h('div', { class: 'layer-ramp', style: { background: paletteCss(l.style.palette, !l.style.reverse, 180) } }),
      h('div', { class: 'layer-info' },
        h('div', { class: 'layer-title' }, l.title),
        h('div', { class: 'layer-meta' }, `${l.geo === 'points' ? '📍' : l.geo === 'admin1' ? '🗺️' : '🌐'} ${nPlaces.toLocaleString()} ${l.geo === 'points' ? 'points' : 'places'}`, l.years?.length > 1 ? ` · ${l.years[0]}–${l.years.at(-1)}` : '', l.partial ? ' · ⏳ loading full history' : '')),
      h('button', { class: 'icon-btn sm', title: l.visible ? 'Hide' : 'Show', onclick: (e) => { e.stopPropagation(); l.visible = !l.visible; afterLayersChanged(false); } }, l.visible ? '👁' : '◌'),
      h('button', { class: 'icon-btn sm', title: 'Remove', onclick: (e) => { e.stopPropagation(); removeLayer(l.id); } }, '✕'));
    card.append(head);
    if (isActive) card.append(styleEditor(l));
    pane.append(card);
  });
  if (regionLs.length > 1 && new Set(regionLs.map((l) => l.geo)).size > 1) pane.append(h('p', { class: 'hint' }, 'Country and state layers can’t be compared directly; the map shows the active one.'));
}

function styleEditor(l) {
  const box = h('div', { class: 'layer-style' });
  const pal = h('div', { class: 'palettes' }, Object.entries(PALETTES).map(([k, p]) => h('button', {
    class: `pal ${l.style.palette === k ? 'on' : ''}`, title: p.name, style: { background: paletteCss(k, l.style.reverse) },
    onclick: () => { l.style.palette = k; afterStyle(); },
  })));
  box.append(ctl('Palette', pal));
  box.append(h('div', { class: 'row2' },
    ctl('Classes', h('select', { class: 'select', onchange: (e) => { l.style.classes = e.target.value; afterStyle(); } },
      [['quantile', 'Quantiles'], ['jenks', 'Natural breaks'], ['equal', 'Equal intervals'], ['continuous', 'Continuous']].map(([v, t]) => h('option', { value: v, selected: l.style.classes === v }, t)))),
    ctl('Steps', h('select', { class: 'select', disabled: l.style.classes === 'continuous', onchange: (e) => { l.style.n = +e.target.value; afterStyle(); } },
      [3, 4, 5, 6, 7, 8, 9].map((n) => h('option', { value: n, selected: l.style.n === n }, n))))));
  box.append(h('div', { class: 'row2' },
    ctl('Scale', seg([['auto', 'Auto'], ['linear', 'Linear'], ['log', 'Log']], l.style.scale, (v) => { l.style.scale = v; afterStyle(); })),
    ctl('Direction', h('button', { class: `btn-ghost sm ${l.style.reverse ? 'on' : ''}`, onclick: () => { l.style.reverse = !l.style.reverse; afterStyle(); } }, '⇄ Reverse colors'))));
  box.append(ctl('Higher values are…', seg([[true, 'Better'], [null, 'Neutral'], [false, 'Worse']], l.higherIsBetter, (v) => { l.higherIsBetter = v; state.benchmark = null; afterStyle(); })));
  box.append(ctl('Title', h('input', { class: 'input', value: l.title, onchange: (e) => { l.title = e.target.value || l.title; afterStyle(); } })));
  if (l.geo !== 'points') box.append(ctl('Opacity', h('input', { type: 'range', min: 0.3, max: 1, step: 0.05, value: l.style.opacity, oninput: (e) => { l.style.opacity = +e.target.value; scheduleMapOnly(); persist(); } })));
  box.append(h('div', { class: 'layer-src' }, l.sourceUrl ? h('a', { href: l.sourceUrl, target: '_blank', rel: 'noopener' }, `Source: ${l.source}`) : `Source: ${l.source || 'user data'}`));
  return box;
}

function afterStyle() {
  renderLayersPanel();
  renderAll();
}

function ctl(label, control) {
  return h('div', { class: 'ctl' }, h('label', {}, label), control);
}

function seg(options, value, onPick) {
  const box = h('div', { class: 'seg sm' });
  options.forEach(([v, label]) => box.append(h('button', { class: v === value ? 'active' : '', onclick: () => {
    box.querySelectorAll('button').forEach((b) => b.classList.remove('active'));
    box.children[options.findIndex((o) => o[0] === v)].classList.add('active');
    onPick(v);
  } }, label)));
  return box;
}

function afterLayersChanged(fit = true) {
  if (!state.layers.find((l) => l.id === state.activeId)) state.activeId = state.layers.at(-1)?.id || null;
  if (state.heightBy !== 'same' && state.heightBy !== 'none' && !state.layers.find((l) => l.id === state.heightBy)) state.heightBy = 'same';
  if (state.compareId && !state.layers.find((l) => l.id === state.compareId)) state.compareId = null;
  const A = activeLayer();
  if (A && state.year != null && !A.years?.includes(state.year)) state.year = null;
  if (state.selected && A && A.geo !== state.selected.geo) state.selected = null;
  renderLayersPanel();
  renderAll();
  renderHome();
  if (fit && A?.geo === 'admin1') fitToLayer(A);
}

async function fitToLayer(L) {
  const geo = await loadGeo('admin1');
  if (state.view !== 'globe') return flat.fitKeys(geo, Object.keys(L.values), 'code');
  const feats = geo.features.filter((f) => L.values[f.properties.code]);
  const lons = feats.map((f) => f.properties.lx).filter(Number.isFinite);
  const lats = feats.map((f) => f.properties.ly).filter(Number.isFinite);
  if (!lons.length) return;
  const span = Math.max(d3.max(lons) - d3.min(lons), (d3.max(lats) - d3.min(lats)) * 1.6);
  globe.flyTo(d3.mean(lons), d3.mean(lats), Math.max(0.7, Math.min(2.2, span / 28)));
}

function removeLayer(id) {
  state.layers = state.layers.filter((l) => l.id !== id);
  if (state.activeId === id) { state.activeId = null; state.benchmark = null; }
  afterLayersChanged(false);
}

export function addLayers(layers, { quiet = false } = {}) {
  for (const l of layers) {
    const dup = l.ref && state.layers.find((x) => x.ref === l.ref && x.title === l.title);
    if (dup) {
      Object.assign(dup, { values: l.values, years: l.years, names: l.names, points: l.points, partial: l.partial });
      continue;
    }
    if (!l.style.palette) l.style.palette = defaultPalette(l, state.layers.length);
    state.layers.push(l);
  }
  const last = state.layers.find((x) => x.ref === layers.at(-1)?.ref && x.title === layers.at(-1)?.title) || state.layers.at(-1);
  state.activeId = last.id;
  state.benchmark = null;
  state.year = null;
  $('#hero').classList.add('gone');
  if (!quiet) toast(layers.length > 1 ? `Added ${layers.length} layers` : `Added “${last.title}”`, 'good', 2600);
  afterLayersChanged(true);
}

// ------------------------------------------------------------------ data loading

const isLoaded = (ref) => state.layers.some((l) => l.ref === ref);

async function addFromSource(source, id, meta = {}) {
  const cat = findCatalog(source, id);
  meta = { ...(cat ? { title: cat.title, sdg: cat.sdg, hib: cat.hib } : {}), ...meta };
  const done = busy(`Loading ${meta.title || id}…`);
  try {
    const res = await api(`/api/fetch?source=${encodeURIComponent(source)}&id=${encodeURIComponent(id)}`);
    let layers = await autoLayers(res, { ...meta, autoAll: source === 'local' });
    done();
    if (!layers) layers = await openImporter(res, meta, { onFollowLink: (url) => scrape(url, meta) });
    if (!layers) return;
    layers.forEach((l) => {
      l.sdg = meta.sdg || l.sdg || [];
      if (meta.hib !== undefined) l.higherIsBetter = meta.hib;
      if (cat && layers.length === 1) l.title = cat.title;
      l.partial = !!res.partial;
    });
    addLayers(layers);
    if (res.partial) {
      toast('Showing the latest values now. The full time series is still downloading from the World Bank and will appear automatically.', 'info', 6000);
      upgradePartial(source, id, meta, 1);
    }
  } catch (e) {
    toast(e.message, 'bad', 7000);
  } finally {
    done();
  }
}

function upgradePartial(source, id, meta, attempt) {
  setTimeout(async () => {
    try {
      const res = await api(`/api/fetch?source=${source}&id=${encodeURIComponent(id)}`);
      if (res.partial) { if (attempt < 6) upgradePartial(source, id, meta, attempt + 1); return; }
      const layers = await autoLayers(res, meta);
      const target = state.layers.find((l) => l.ref === res.ref);
      if (!layers || !target) return;
      Object.assign(target, { values: layers[0].values, years: layers[0].years, names: layers[0].names, partial: false });
      renderLayersPanel();
      renderAll();
      toast(`Full history loaded for “${target.title}” (${target.years[0]}–${target.years.at(-1)}). Press ▶ to animate.`, 'good');
    } catch { if (attempt < 6) upgradePartial(source, id, meta, attempt + 1); }
  }, 20000 * attempt);
}

async function scrape(url, meta = {}) {
  const done = busy('Scraping page for tables…');
  try {
    const res = await api(`/api/fetch?source=url&url=${encodeURIComponent(url)}`);
    if (meta.title && meta.live) res.title = meta.title;
    done();
    const layers = await openImporter(res, meta, { onFollowLink: (u) => scrape(u, meta) });
    if (layers) {
      layers.forEach((l) => { l.sdg = meta.sdg || []; });
      addLayers(layers);
    }
  } catch (e) {
    toast(e.message, 'bad', 7000);
  } finally {
    done();
  }
}

async function importFile(file) {
  if (file.size > 40 * 1024 * 1024) return toast('That file is over 40 MB.', 'bad');
  if (/\.json$/i.test(file.name) && await restoreWorkspace(file)) return;
  const done = busy(`Reading ${file.name}…`);
  try {
    const res = await api('/api/parse', { method: 'POST', headers: { 'X-Filename': encodeURIComponent(file.name), 'Content-Type': file.type || 'application/octet-stream' }, body: file });
    done();
    const layers = await openImporter(res, { title: file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ') });
    if (layers) addLayers(layers);
  } catch (e) {
    toast(e.message, 'bad', 7000);
  } finally {
    done();
  }
}

// A file written by "Save workspace" brings back all of its layers instead of going through the importer.
async function restoreWorkspace(file) {
  let ws;
  try { ws = JSON.parse(await file.text()); } catch { return false; }
  if (!ws || ws.v !== 2 || !Array.isArray(ws.layers)) return false;
  const layers = ws.layers.filter((l) => l && l.id && l.style && (l.values || l.points));
  if (!layers.length) { toast('That workspace has no layers.', 'bad'); return true; }
  for (const l of layers) if (state.layers.some((x) => x.id === l.id)) l.id = uid();
  addLayers(layers, { quiet: true });
  toast(`Restored workspace · ${layers.length} layer${layers.length > 1 ? 's' : ''}`, 'good');
  return true;
}

async function importText(text) {
  const blob = new Blob([text], { type: 'text/plain' });
  await importFile(new File([blob], 'pasted-data.csv'));
}

// ------------------------------------------------------------------ region card & tooltip

function selectRegion(sel, fly = false) {
  state.selected = sel;
  flat?.setSelected(sel);
  globe?.setSelected(sel);
  renderRegionCard();
  renderRightPanel();
  if (fly && sel) {
    const inf = infoFor(sel.key, sel.geo);
    if (inf?.lx != null) state.view === 'globe' ? globe.flyTo(inf.lx, inf.ly, 1.4) : flat.flyTo(inf.lx, inf.ly, sel.geo === 'admin1' ? 4.5 : 3.2);
    else if (sel.geo === 'admin1') loadGeo('admin1').then((g) => { const f = g.features.find((x) => x.properties.code === sel.key); if (f) state.view === 'globe' ? globe.flyTo(f.properties.lx, f.properties.ly, 1) : flat.flyTo(f.properties.lx, f.properties.ly, 4.5); });
  }
  if (sel && state.view === 'globe') { globe.setAutoRotate(false); state.autoRotate = false; syncRotateBtn(); }
}

function sparkline(L, key) {
  const pts = L.years.map((y) => ({ y, v: L.values[key]?.[y] })).filter((d) => d.v != null);
  if (pts.length < 3) return null;
  const W = 120, H = 30;
  const x = d3.scaleLinear().domain(d3.extent(pts, (d) => d.y)).range([2, W - 2]);
  const y = d3.scaleLinear().domain(d3.extent(pts, (d) => d.v)).range([H - 3, 3]);
  const svg = d3.create('svg').attr('viewBox', `0 0 ${W} ${H}`).attr('class', 'spark');
  svg.append('path').attr('d', d3.line().x((d) => x(d.y)).y((d) => y(d.v)).curve(d3.curveMonotoneX)(pts));
  const last = pts.at(-1);
  svg.append('circle').attr('cx', x(last.y)).attr('cy', y(last.v)).attr('r', 2.6);
  svg.append('title').text(`${pts[0].y}: ${fmt(pts[0].v)} → ${last.y}: ${fmt(last.v)}`);
  return svg.node();
}

function renderRegionCard() {
  const box = $('#region-card');
  const sel = state.selected;
  if (!sel) { box.classList.remove('show'); return; }
  const inf = infoFor(sel.key, sel.geo);
  const layers = regionLayers(sel.geo);
  const name = inf?.name || layers.map((l) => l.names?.[sel.key]).find(Boolean) || sel.key;
  box.innerHTML = '';
  box.classList.add('show');
  const sub = sel.geo === 'admin1' ? [inf?.country].filter(Boolean).join('') : [inf?.subregion, inf?.income?.replace(/^\d\.\s*/, '')].filter(Boolean).join(' · ');
  const iso2 = sel.geo === 'countries' ? inf?.iso2 : countryInfo && [...countryInfo.values()].find((c) => c.iso3 === inf?.iso3)?.iso2;
  box.append(h('div', { class: 'rc-head' },
    h('div', { class: 'rc-flag' }, flagEmoji(iso2) || '📍'),
    h('div', {}, h('div', { class: 'rc-name' }, name), h('div', { class: 'rc-sub' }, sub)),
    h('button', { class: 'icon-btn sm', 'aria-label': 'Close', onclick: () => selectRegion(null) }, '✕')));
  if (sel.geo === 'countries' && inf?.pop) box.append(h('div', { class: 'rc-pop' }, `👥 ${fmtPeople(inf.pop)} people`));
  const rows = h('div', { class: 'rc-rows' });
  for (const L of layers) {
    const sl = sliceFor(L);
    const e = sl.byKey.get(sel.key);
    const n = sl.entries.length;
    const rank = sl.rank.get(sel.key);
    const pct = e ? Math.round(((n - rank) / Math.max(1, n - 1)) * 100) : null;
    rows.append(h('div', { class: `rc-row ${L.id === state.activeId ? 'active' : ''}`, onclick: () => { state.activeId = L.id; afterLayersChanged(false); } },
      h('div', { class: 'rc-row-top' }, h('span', { class: 'rc-l' }, L.title), h('span', { class: 'rc-v', style: { color: e ? sl.scale.color(e.v) : '' } }, e ? fmt(e.v, L.unit) : 'no data')),
      e ? h('div', { class: 'rc-row-mid' },
        h('div', { class: 'rc-pbar', style: { background: paletteCss(L.style.palette, L.style.reverse) } }, h('i', { style: { left: `${sl.scale.t(e.v) * 100}%` } })),
        h('span', { class: 'muted' }, `#${rank} of ${n}${e.y ? ` · ${e.y}` : ''}`)) : null,
      L.years?.length > 2 ? sparkline(L, sel.key) : null));
    void pct;
  }
  box.append(rows);
}

function tooltipHtml(hit) {
  if (!hit) return '';
  if (hit.point) {
    const p = hit.point;
    return `<div class="tip"><div class="tip-title">${esc(p.label || 'Point')}</div><div class="tip-value">${p.v != null && p.v !== 'null' ? fmt(+p.v) : ''}</div><div class="tip-sub">${esc(p.layer || '')}</div></div>`;
  }
  const L = choroplethLayer();
  const inf = infoFor(hit.key, hit.geo);
  const name = inf?.name || L?.names?.[hit.key] || hit.key;
  const sub = hit.geo === 'admin1' ? inf?.country || '' : '';
  if (!L || L.geo !== hit.geo) return `<div class="tip"><div class="tip-title">${esc(name)}</div><div class="tip-sub">${esc(sub)}</div></div>`;
  const sl = current.choroSlice && current.choroSlice.layer === L ? current.choroSlice : sliceFor(L);
  const e = sl.byKey.get(hit.key);
  const col = e ? sl.scale.color(e.v) : '#64748b';
  return `<div class="tip"><div class="tip-title">${flagEmoji(inf?.iso2)} ${esc(name)}</div>${sub ? `<div class="tip-sub">${esc(sub)}</div>` : ''}
    <div class="tip-value"><i style="background:${col}"></i>${e ? fmt(e.v, L.unit) : 'No data'}</div>
    ${e ? `<div class="tip-sub">#${sl.rank.get(hit.key)} of ${sl.entries.length}${e.y ? ` · ${e.y}` : ''} · ${esc(L.title.length > 40 ? L.title.slice(0, 38) + '…' : L.title)}</div>` : ''}</div>`;
}

function onHover(hit, ev) {
  const tip = $('#tooltip');
  if (!hit) { tip.classList.remove('show'); return; }
  tip.innerHTML = tooltipHtml(hit);
  tip.classList.add('show');
  const x = ev.clientX, y = ev.clientY;
  const r = tip.getBoundingClientRect();
  tip.style.left = `${Math.min(x + 16, innerWidth - r.width - 8)}px`;
  tip.style.top = `${Math.min(y + 16, innerHeight - r.height - 8)}px`;
}

// ------------------------------------------------------------------ views, tabs, theme

function setView(v) {
  state.view = v;
  $$('#view-seg button').forEach((b) => b.classList.toggle('active', b.dataset.view === v));
  $('#globe').classList.toggle('on', v === 'globe');
  $('#flat').classList.toggle('on', v !== 'globe');
  globe.pause(v !== 'globe');
  $('#rotate-btn').style.display = v === 'globe' ? '' : 'none';
  if (v !== 'globe') requestAnimationFrame(() => flat.resize());
  else globe.resize();
  renderLayersPanel();
  renderAll();
  const A = choroplethLayer();
  if (A?.geo === 'admin1') setTimeout(() => fitToLayer(A), 150);
}

function setTab(t) {
  state.tab = t;
  $$('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === t));
  $$('.pane').forEach((p) => p.classList.toggle('on', p.id === `pane-${t}`));
  if (!state.leftOpen) togglePanel('left', true);
}

function togglePanel(side, force) {
  const key = side === 'left' ? 'leftOpen' : 'rightOpen';
  state[key] = force ?? !state[key];
  document.body.classList.toggle(`${side}-closed`, !state[key]);
  setTimeout(() => { flat?.resize(); globe?.resize(); updatePadding(); }, 320);
}

function updatePadding() {
  const tb = $('#topbar');
  document.documentElement.style.setProperty('--topbar-h', `${tb.offsetTop + tb.offsetHeight}px`);
  if (!flat?.map) return;
  const narrow = innerWidth < 900;
  const pw = $('#left').offsetWidth + 28;
  flat.setPadding({ left: state.leftOpen && !narrow ? pw : 0, right: state.rightOpen && !narrow ? pw : 0, top: narrow ? 120 : 80, bottom: 70 });
}

function setTheme(t) {
  state.theme = t;
  document.documentElement.dataset.theme = t;
  flat?.setTheme(t);
  globe?.setTheme(t);
  $('#theme-btn').textContent = t === 'dark' ? '☀︎' : '☾';
  persist();
}

function syncRotateBtn() {
  $('#rotate-btn').classList.toggle('on', state.autoRotate);
}

// ------------------------------------------------------------------ persistence & share

const persist = debounce(() => {
  const view = { view: state.view, theme: state.theme, basemap: state.basemap, texture: state.texture, heightScale: state.heightScale, heightBy: state.heightBy, pointStyle: state.pointStyle };
  const payload = { v: 2, activeId: state.activeId, ...view, layers: state.layers };
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(payload));
  } catch {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({ ...payload, layers: state.layers.filter((l) => l.ref && /^(wb|owid):/.test(l.ref)).map((l) => ({ ...l, values: {}, refOnly: true })) }));
    } catch { /* storage unavailable */ }
  }
  const refs = state.layers.filter((l) => l.ref && /^(wb|owid|local):/.test(l.ref)).map((l) => l.ref);
  const hash = refs.length ? `#data=${refs.map(encodeURIComponent).join(',')}&view=${state.view}` : '';
  if (location.hash !== hash) history.replaceState(null, '', hash || location.pathname);
}, 400);

async function restore() {
  const hash = new URLSearchParams(location.hash.slice(1));
  let saved;
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || 'null'); } catch { saved = null; }
  const refs = hash.get('data') ? hash.get('data').split(',').map(decodeURIComponent) : [];
  // A reload keeps the hash this session wrote; only a link to other data replaces the saved workspace (which may hold uploads).
  const savedRefs = new Set((saved?.layers || []).map((l) => l.ref));
  if (refs.length && !refs.every((r) => savedRefs.has(r))) {
    if (hash.get('view')) state.view = hash.get('view');
    $('#hero').classList.add('gone');
    for (const r of refs) {
      const [src, ...rest] = r.split(':');
      await addFromSource(src, rest.join(':'));
    }
    return true;
  }
  if (!saved) return false;
  Object.assign(state, { view: saved.view || 'globe', theme: saved.theme || 'dark', basemap: saved.basemap || 'none', texture: saved.texture || 'midnight', heightScale: saved.heightScale || 1, heightBy: saved.heightBy || 'same', pointStyle: saved.pointStyle || 'dots' });
  const full = (saved.layers || []).filter((l) => !l.refOnly);
  state.layers = full;
  state.activeId = saved.activeId;
  for (const l of (saved.layers || []).filter((x) => x.refOnly)) {
    const [src, ...rest] = l.ref.split(':');
    addFromSource(src, rest.join(':'), { quiet: true });
  }
  return state.layers.length > 0;
}

// ------------------------------------------------------------------ boot

function renderHero() {
  const hero = $('#hero');
  const quick = QUICK_START.map((id) => CATALOG.find((c) => c.id === id)).filter(Boolean);
  hero.innerHTML = '';
  hero.append(h('div', { class: 'hero-card' },
    h('div', { class: 'hero-kicker' }, h('span', { class: 'live-dot' }), 'Open data · 17 Sustainable Development Goals'),
    h('h1', {}, 'See where the world ', h('span', { class: 'grad' }, 'needs help most'), '.'),
    h('p', {}, 'Search thousands of public datasets, scrape any web table, or drop in your own spreadsheet. Map it in 3D, find the gaps, and share the story.'),
    heroSearch(),
    h('div', { class: 'hero-or' }, 'or start with a featured dataset'),
    h('div', { class: 'hero-quick' }, quick.map((c) => h('button', { class: 'hero-chip', style: { '--sdg': SDGS[c.sdg[0]].color }, onclick: () => addFromSource(c.source, c.id) }, h('span', {}, c.icon), c.title))),
    h('div', { class: 'hero-actions' },
      h('button', { class: 'btn-ghost lg', onclick: () => { setTab('import'); $('#hero').classList.add('gone'); } }, '⬆ Upload or scrape'),
      h('button', { class: 'btn-ghost lg', onclick: () => $('#hero').classList.add('gone') }, 'Just explore')),
    h('div', { class: 'hero-sdgs' }, SDGS.slice(1).map((g) => h('i', { style: { background: g.color }, title: `SDG ${g.n}: ${g.name}` })))));
}

// The hero has its own search field; submitting it hands off to the main search and Discover pane.
function heroSearch() {
  const input = h('input', { type: 'search', class: 'hero-input', placeholder: 'Search 10,000+ datasets: clean water, refugees, malaria…', 'aria-label': 'Search datasets', autocomplete: 'off', spellcheck: 'false' });
  const go = (q) => {
    q = (q ?? input.value).trim();
    if (!q) return input.focus();
    $('#search').value = q;
    $('#hero').classList.add('gone');
    setTab('discover');
    if (!document.body.classList.contains('left-open')) $('#left-toggle').click();
    runSearch(q);
  };
  const form = h('form', { class: 'hero-search', onsubmit: (e) => { e.preventDefault(); go(); } },
    h('span', { class: 'search-icon', 'aria-hidden': 'true' }, '⌕'), input,
    h('button', { type: 'submit', class: 'btn-primary' }, 'Search'));
  const ideas = h('div', { class: 'hero-ideas' }, 'Try:', ['clean water', 'refugees', 'girls education', 'malaria', 'air pollution'].map((q) => h('button', { type: 'button', class: 'link', onclick: () => go(q) }, q)));
  return [form, ideas];
}

function wireImportPane() {
  const pane = $('#pane-import');
  const fileIn = h('input', { type: 'file', accept: '.csv,.tsv,.txt,.xlsx,.json,.geojson', hidden: true, onchange: (e) => { const f = e.target.files[0]; if (f) importFile(f); e.target.value = ''; } });
  const urlIn = h('input', { class: 'input', placeholder: 'https://… any page with a table, or a CSV / XLSX / JSON link', onkeydown: (e) => { if (e.key === 'Enter' && e.target.value.trim()) scrape(e.target.value.trim()); } });
  const paste = h('textarea', { class: 'input', rows: 5, placeholder: 'country,value\nKenya,61.4\nIndia,67.2\n…  (CSV or tab-separated, e.g. copied from a spreadsheet)' });
  const examples = [
    ['Literacy by country', 'https://en.wikipedia.org/wiki/List_of_countries_by_literacy_rate'],
    ['US states: life expectancy', 'https://en.wikipedia.org/wiki/List_of_U.S._states_and_territories_by_life_expectancy'],
    ['Homelessness by country', 'https://en.wikipedia.org/wiki/List_of_countries_by_homeless_population'],
    ['India states: HDI', 'https://en.wikipedia.org/wiki/List_of_Indian_states_and_union_territories_by_Human_Development_Index'],
  ];
  pane.append(
    h('div', { class: 'drop', onclick: () => fileIn.click() },
      h('div', { class: 'drop-icon' }, '⬆'),
      h('div', { class: 'drop-title' }, 'Drop a file or click to upload'),
      h('div', { class: 'muted' }, 'CSV, TSV, Excel (.xlsx), JSON or GeoJSON · up to 40 MB'),
      h('div', { class: 'drop-tags' }, ['Countries', 'States & provinces', 'Lat/lon points', 'Time series'].map((t) => h('span', {}, t))), fileIn),
    h('div', { class: 'section-title' }, '🕸️ Scrape a web page'),
    h('div', { class: 'url-row' }, urlIn, h('button', { class: 'btn-primary', onclick: () => urlIn.value.trim() && scrape(urlIn.value.trim()) }, 'Scrape')),
    h('p', { class: 'hint' }, 'Finds every table on the page (Wikipedia, government sites, NGO reports) plus linked CSV/Excel files.'),
    h('div', { class: 'chips' }, examples.map(([t, u]) => h('button', { class: 'chip', onclick: () => { urlIn.value = u; scrape(u); } }, t))),
    h('div', { class: 'section-title' }, '📋 Paste data'),
    paste,
    h('div', { class: 'row-end' }, h('button', { class: 'btn-ghost', onclick: () => paste.value.trim() && importText(paste.value) }, 'Map pasted data')),
    h('div', { class: 'section-title' }, 'How matching works'),
    h('ul', { class: 'tips' },
      h('li', {}, 'Countries match by name in many spellings (“Côte d’Ivoire”, “Congo, Dem. Rep.”, “Türkiye”) or ISO2/ISO3 codes.'),
      h('li', {}, 'States/provinces match by ISO 3166-2 code (US-CA, IN-MH), name, or postal abbreviation, for 4,500+ regions worldwide.'),
      h('li', {}, 'A year column, or columns named 2010, 2011, …, unlocks the animated timeline.'),
      h('li', {}, 'Columns named lat/lon (or latitude/longitude) become points, shown as dots, a heatmap, or 3D spikes.')));
}

function wireDragDrop() {
  let depth = 0;
  addEventListener('dragenter', (e) => { if (e.dataTransfer?.types?.includes('Files')) { depth++; document.body.classList.add('dragging'); } });
  addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) document.body.classList.remove('dragging'); });
  addEventListener('dragover', (e) => e.preventDefault());
  addEventListener('drop', (e) => {
    e.preventDefault();
    depth = 0;
    document.body.classList.remove('dragging');
    const f = e.dataTransfer?.files?.[0];
    if (f) importFile(f);
  });
}

function exportArgs() {
  // The poster/brief describes what the map shows: the choropleth layer, else a point layer.
  const A = choroplethLayer() || activeLayer();
  if (!A) { toast('Add a dataset first.', 'bad'); return null; }
  const slice = sliceFor(A);
  const story = buildStory({ slice, others: correlationsFor(slice, regionLayers(A.geo).filter((l) => l.id !== A.id && A.geo !== 'points').map((l) => sliceFor(l))), benchmark: state.benchmark });
  const canvas = state.view === 'globe' ? globe.canvas() : flat.canvas();
  return { mapCanvas: canvas, layer: A, legend: slice.scale.legend, yearLabel: slice.yearLabel, story, slice, theme: state.theme, benchmark: state.benchmark, bmResult: state.benchmark != null ? benchmarkResult(slice, state.benchmark) : null };
}

function wireTopbar() {
  const search = $('#search');
  const go = debounce(() => { setTab('discover'); $('#hero').classList.add('gone'); runSearch(search.value); }, 380);
  search.addEventListener('input', go);
  search.addEventListener('keydown', (e) => { if (e.key === 'Enter') { setTab('discover'); runSearch(search.value); } if (e.key === 'Escape') search.blur(); });
  $$('#view-seg button').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
  $$('#tabs button').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
  $('#theme-btn').addEventListener('click', () => setTheme(state.theme === 'dark' ? 'light' : 'dark'));
  $('#rotate-btn').addEventListener('click', () => { state.autoRotate = !state.autoRotate; globe.setAutoRotate(state.autoRotate); syncRotateBtn(); });
  $('#reset-btn').addEventListener('click', () => (state.view === 'globe' ? globe.resetView() : flat.resetView()));
  $('#left-toggle').addEventListener('click', () => togglePanel('left'));
  $('#right-toggle').addEventListener('click', () => togglePanel('right'));
  $('#export-btn').addEventListener('click', (e) => { e.stopPropagation(); $('#export-menu').classList.toggle('open'); });
  addEventListener('click', () => $('#export-menu').classList.remove('open'));
  $('#export-png').addEventListener('click', () => { const a = exportArgs(); if (a) exportPNG(a); });
  $('#export-brief').addEventListener('click', () => { const a = exportArgs(); if (a) exportBrief(a); });
  $('#export-csv').addEventListener('click', () => { if (!state.layers.length) return toast('Add a dataset first.', 'bad'); exportCSV(state.layers.filter((l) => l.visible || l.id === state.activeId), (l) => sliceFor(l), nameOf); });
  $('#export-link').addEventListener('click', async () => {
    persist.flush?.();
    const refs = state.layers.filter((l) => l.ref && /^(wb|owid|local):/.test(l.ref)).map((l) => l.ref);
    if (!refs.length) return toast('Share links work for World Bank, Our World in Data and sample layers. Use CSV export for uploaded data.', 'info', 6000);
    const url = `${location.origin}${location.pathname}#data=${refs.map(encodeURIComponent).join(',')}&view=${state.view}`;
    try { await navigator.clipboard.writeText(url); toast('Link copied. Anyone running the Atlas can open this view.', 'good'); } catch { prompt('Copy this link:', url); }
  });
  $('#export-session').addEventListener('click', () => download('atlas-workspace.json', JSON.stringify({ v: 2, layers: state.layers }), 'application/json'));
  $('#about-btn').addEventListener('click', () => $('#about').classList.add('open'));
  $$('.modal').forEach((m) => m.addEventListener('click', (e) => { if (e.target === m) { m.id === 'importer' ? closeImporter() : m.classList.remove('open'); } }));
  $$('[data-close]').forEach((b) => b.addEventListener('click', () => b.closest('.modal').classList.remove('open')));

  addEventListener('keydown', (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
    if (e.key === 'Escape') { closeImporter(); $$('.modal').forEach((m) => m.classList.remove('open')); selectRegion(null); return; }
    if (typing) return;
    if (e.key === '/' || (e.key === 'k' && (e.metaKey || e.ctrlKey))) { e.preventDefault(); search.focus(); search.select(); }
    else if (e.key === '1') setView('globe');
    else if (e.key === '2') setView('map');
    else if (e.key === '3') setView('tilt');
    else if (e.key === ' ' && activeLayer()?.years?.length > 1) { e.preventDefault(); playing ? stopPlay() : startPlay(); }
    else if ((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && activeLayer()?.years?.length > 1) {
      const ys = activeLayer().years;
      const i = state.year == null ? ys.length - 1 : ys.indexOf(state.year);
      const j = Math.max(0, Math.min(ys.length - 1, i + (e.key === 'ArrowRight' ? 1 : -1)));
      state.year = ys[j];
      renderAll(true);
    }
  });
}

async function boot() {
  wireTopbar();
  wireImportPane();
  wireDragDrop();
  renderHero();
  const cb = { onHover, onClick: (sel) => selectRegion(sel), theme: 'dark' };
  flat = new FlatMap($('#flat'), cb);
  globe = new GlobeView($('#globe'), { ...cb, labelFor: tooltipHtml });
  new ResizeObserver(() => { globe.resize(); flat.resize(); }).observe($('#stage'));
  initSearch({ onAdd: addFromSource, onScrape: scrape, isLoaded });
  try {
    const [cg, ag] = await Promise.all([countryGazetteer(), admin1Gazetteer()]);
    countryInfo = cg.info;
    admin1Info = ag.info;
  } catch (e) {
    toast('Could not load boundaries: ' + e.message, 'bad', 10000);
  }
  if (innerWidth < 900) { togglePanel('right', false); togglePanel('left', false); }
  const restored = await restore();
  setTheme(state.theme);
  await Promise.all([flat.ready, globe.ready]).catch((e) => toast('Map failed to start: ' + e.message, 'bad', 10000));
  flat.setBasemap(state.basemap);
  updatePadding();
  flat.resetView();
  addEventListener('resize', debounce(updatePadding, 200));
  if (state.texture !== 'midnight') globe.setTexture(state.texture);
  if (restored) $('#hero').classList.add('gone');
  setView(state.view);
  renderLayersPanel();
  // Preloaded files from the command line
  api('/api/preload').then(async (d) => {
    for (const ds of d.datasets || []) {
      if (ds.error) { toast(ds.error, 'bad'); continue; }
      if (state.layers.some((l) => l.ref === ds.ref)) continue;
      const layers = await autoLayers(ds, { autoAll: true });
      if (layers) addLayers(layers);
      else openImporter(ds, {}).then((ls) => ls && addLayers(ls));
    }
  }).catch(() => {});
  // Pre-fetch featured indicators so clicks are instant later.
  setTimeout(() => api(`/api/warm?refs=${CATALOG.filter((c) => c.source !== 'url').map((c) => `${c.source}:${c.id}`).join(',')}`).catch(() => {}), 4000);
  document.body.classList.add('ready');
  window.__atlas = { state, flat, globe }; // handy for debugging in the console
}

boot();
