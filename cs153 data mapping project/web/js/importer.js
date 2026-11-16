// Import wizard: pick a table, map columns, check matches, add layers.
import { h, esc, api, debounce } from './util.js';
import { analyzeTable, buildLayers, smartTitle } from './table.js';
import { geoLabel } from './geo.js';

let current = null;

export function closeImporter() {
  if (current) current.resolve(null);
  current = null;
  document.querySelector('#importer')?.classList.remove('open');
}

// result: server response {title, source, sourceUrl, description, unit, ref, tables, links, hint, message}
// meta: catalog extras {sdg, hib, title}
export function openImporter(result, meta = {}, { onFollowLink } = {}) {
  closeImporter();
  return new Promise((resolve) => {
    current = { resolve };
    const modal = document.querySelector('#importer');
    modal.classList.add('open');
    render(modal, result, meta, resolve, onFollowLink);
  });
}

async function render(modal, result, meta, resolve, onFollowLink) {
  const box = modal.querySelector('.importer-box');
  box.innerHTML = '';
  const tables = result.tables || [];
  const state = { ti: 0, analysis: null, mapping: null, hib: meta.hib ?? result.higherIsBetter ?? null, title: '' };

  const close = () => { closeImporter(); };
  const header = h('div', { class: 'imp-head' },
    h('div', {}, h('div', { class: 'imp-kicker' }, 'Import data'), h('h2', {}, result.title || 'Imported data'),
      h('div', { class: 'muted' }, result.sourceUrl ? h('a', { href: result.sourceUrl, target: '_blank', rel: 'noopener' }, result.source || result.sourceUrl) : result.source || '')),
    h('button', { class: 'icon-btn', 'aria-label': 'Close', onclick: close }, '✕'));
  const side = h('div', { class: 'imp-side' });
  const main = h('div', { class: 'imp-main' });
  const footer = h('div', { class: 'imp-foot' });
  box.append(header, h('div', { class: 'imp-body' }, side, main), footer);

  // Side: tables + discovered data links
  if (tables.length) {
    side.append(h('div', { class: 'imp-side-title' }, `${tables.length} table${tables.length > 1 ? 's' : ''} found`));
    tables.forEach((t, i) => side.append(h('button', { class: `tbl-item ${i === 0 ? 'active' : ''}`, 'data-i': i, onclick: () => selectTable(i) },
      h('div', { class: 'tbl-title' }, t.title || `Table ${i + 1}`),
      h('div', { class: 'tbl-meta' }, `${t.nrows.toLocaleString()} rows · ${t.headers.length} columns`, h('span', { class: 'tbl-badge', 'data-badge': i })))));
  }
  if (result.links?.length) {
    side.append(h('div', { class: 'imp-side-title' }, `Data files linked from this page`));
    result.links.slice(0, 30).forEach((l) => side.append(h('button', { class: 'link-item', onclick: () => onFollowLink?.(l.url) },
      h('span', { class: `kind kind-${l.kind}` }, l.kind), h('span', {}, l.text || l.url))));
  }
  if (!tables.length) {
    main.append(h('div', { class: 'imp-empty' }, h('div', { class: 'empty-orb' }), h('h3', {}, result.message || 'No tables here'), h('p', { class: 'muted' }, 'Pick one of the linked data files on the left to scrape it.')));
    footer.append(h('button', { class: 'btn-ghost', onclick: close }, 'Close'));
    return;
  }

  // Score each table's "mappability" in the background for the side badges.
  tables.forEach(async (t, i) => {
    const a = await analyzeTable(t, i === 0 ? result.hint : null);
    const best = Math.max(...a.cols.map((c) => Math.max(c.countryRate, c.admin1Rate)), a.mapping.geo === 'points' ? 1 : 0);
    const el = side.querySelector(`[data-badge="${i}"]`);
    if (el) {
      el.textContent = best >= 0.6 ? 'mappable' : best >= 0.3 ? 'partial' : 'no places';
      el.className = `tbl-badge ${best >= 0.6 ? 'good' : best >= 0.3 ? 'mid' : 'bad'}`;
    }
  });

  const addBtn = h('button', { class: 'btn-primary', onclick: async () => {
    addBtn.disabled = true;
    const t = tables[state.ti];
    const { layers } = await buildLayers(t, state.mapping, {
      ...result, title: state.title || t.title, higherIsBetter: state.hib, sdg: meta.sdg || [],
      titleFor: state.mapping.values.length > 1 && !state.mapping.wide ? (c) => smartTitle(t.title, c) : null,
    });
    if (!layers.length) { addBtn.disabled = false; return; }
    current = null;
    modal.classList.remove('open');
    resolve(layers);
  } }, 'Add to map');
  footer.append(h('div', { class: 'imp-foot-info' }), h('button', { class: 'btn-ghost', onclick: close }, 'Cancel'), addBtn);

  const update = debounce(async () => {
    const t = tables[state.ti];
    const m = state.mapping;
    const info = footer.querySelector('.imp-foot-info');
    const meter = main.querySelector('.match');
    const ok = m.geo === 'points' ? m.lat && m.lon : m.loc && m.values.length;
    if (!ok) {
      addBtn.disabled = true;
      meter.innerHTML = `<div class="match-text warn">${m.geo === 'points' ? 'Choose latitude and longitude columns.' : 'Choose a location column and at least one value column.'}</div>`;
      info.textContent = '';
      return;
    }
    const { layers, report } = await buildLayers(t, m, { ...result, title: state.title || t.title });
    const pct = report.rows ? Math.round((report.matched / report.rows) * 100) : 0;
    const n = layers.length;
    addBtn.disabled = !n;
    addBtn.textContent = n > 1 ? `Add ${n} layers to map` : 'Add to map';
    const places = m.geo === 'points' ? `${report.matched.toLocaleString()} points with valid coordinates` : `${new Set(layers.flatMap((l) => Object.keys(l.values))).size} ${m.geo === 'admin1' ? 'states/provinces' : 'countries'} matched`;
    meter.innerHTML = '';
    meter.append(
      h('div', { class: 'match-bar' }, h('i', { style: { width: `${pct}%`, background: pct > 80 ? 'var(--good)' : pct > 40 ? 'var(--warn)' : 'var(--bad)' } })),
      h('div', { class: 'match-text' }, h('b', {}, `${pct}% of rows matched`), ` · ${places}`, layers[0]?.years?.length > 1 ? ` · ${layers[0].years.length} years (${layers[0].years[0]}–${layers[0].years.at(-1)})` : '', report.aggregated ? ` · ${report.aggregated} duplicate rows averaged` : ''),
      report.unmatched?.length ? h('details', { class: 'unmatched' }, h('summary', {}, `${report.unmatched.length} unmatched names (often totals, regions or typos)`),
        h('div', { class: 'chips' }, report.unmatched.slice(0, 40).map(([name, c]) => h('span', { class: 'chip' }, name, c > 1 ? h('small', {}, ` ×${c}`) : null)))) : null);
    info.textContent = n ? `${n} layer${n > 1 ? 's' : ''} · ${geoLabel(m.geo)}` : 'Nothing to add yet';
    paintPreview();
  }, 120);

  function paintPreview() {
    const t = tables[state.ti];
    const m = state.mapping;
    const role = (hd) => (hd === m.loc || hd === m.lat || hd === m.lon ? 'loc' : m.values.includes(hd) ? 'val' : hd === m.year ? 'year' : hd === m.countryCol || hd === m.label ? 'aux' : '');
    const grid = main.querySelector('.preview');
    grid.innerHTML = '';
    const table = h('table', {},
      h('thead', {}, h('tr', {}, t.headers.map((hd) => h('th', { class: `role-${role(hd)}` }, hd)))),
      h('tbody', {}, t.rows.slice(0, 14).map((r) => h('tr', {}, t.headers.map((hd, i) => h('td', { class: `role-${role(hd)}`, title: String(r[i] ?? '').length > 28 ? r[i] : null }, r[i] ?? ''))))));
    grid.append(table);
  }

  async function selectTable(i) {
    state.ti = i;
    side.querySelectorAll('.tbl-item').forEach((b) => b.classList.toggle('active', +b.dataset.i === i));
    main.innerHTML = '<div class="imp-loading"><span class="spinner"></span> Analyzing columns…</div>';
    const t = tables[i];
    const a = await analyzeTable(t, i === 0 ? result.hint : null);
    state.analysis = a;
    state.mapping = { ...a.mapping };
    if (meta.points) {
      if (meta.points.value && t.headers.includes(meta.points.value)) state.mapping.values = [meta.points.value];
      if (meta.points.label && t.headers.includes(meta.points.label)) state.mapping.label = meta.points.label;
    }
    state.titleEdited = false;
    state.title = defaultTitle();
    buildForm();
    update();
  }

  function defaultTitle() {
    const t = tables[state.ti];
    const m = state.mapping;
    if (meta.title) return meta.title;
    if (m.wide || m.values.length !== 1) return smartTitle(t.title, null) || result.title || t.title;
    return smartTitle(tables.length === 1 && !/^table \d+$/i.test(t.title) ? t.title : t.title || result.title, m.values[0]) || result.title;
  }

  function buildForm() {
    const t = tables[state.ti];
    const a = state.analysis;
    const m = state.mapping;
    main.innerHTML = '';
    const opt = (cols, sel, allowNone, noneLabel = '— none —') => [allowNone ? h('option', { value: '' }, noneLabel) : null, ...cols.map((c) => h('option', { value: c, selected: c === sel }, c))];
    const set = (k) => (e) => { m[k] = e.target.value || null; update(); };
    const textCols = t.headers;
    const geoSel = h('div', { class: 'seg' }, ['countries', 'admin1', 'points'].map((g) => h('button', { class: m.geo === g ? 'active' : '', onclick: () => { m.geo = g; buildForm(); update(); } }, geoLabel(g))));
    const fields = h('div', { class: 'imp-grid' });
    fields.append(field('Geography', geoSel, 'What do the rows describe?'));
    if (m.geo === 'points') {
      fields.append(field('Latitude', h('select', { class: 'select', onchange: set('lat') }, opt(textCols, m.lat, true))));
      fields.append(field('Longitude', h('select', { class: 'select', onchange: set('lon') }, opt(textCols, m.lon, true))));
      fields.append(field('Label', h('select', { class: 'select', onchange: set('label') }, opt(textCols, m.label, true))));
    } else {
      fields.append(field('Location column', h('select', { class: 'select', onchange: set('loc') }, opt(textCols, m.loc, true)), m.geo === 'admin1' ? 'State/province names or ISO 3166-2 codes (US-CA)' : 'Country names, ISO2 or ISO3 codes'));
      if (m.geo === 'admin1') fields.append(field('Country column', h('select', { class: 'select', onchange: set('countryCol') }, opt(textCols, m.countryCol, true, '— auto-detect —')), 'Optional: disambiguates e.g. “Punjab”'));
      fields.append(field('Year column', h('select', { class: 'select', onchange: set('year') }, opt(textCols, m.year, true, '— no years —')), 'Enables the timeline'));
    }
    const valueCols = [...new Set([...a.numericCols, ...m.values])];
    const chips = h('div', { class: 'chips pick' }, valueCols.map((c) => h('button', { class: `chip ${m.values.includes(c) ? 'on' : ''}`, onclick: (e) => {
      m.values = m.values.includes(c) ? m.values.filter((x) => x !== c) : [...m.values, c];
      if (m.wide && !a.yearCols.includes(c)) m.wide = false;
      e.currentTarget.classList.toggle('on');
      if (!state.titleEdited) { state.title = defaultTitle(); const ti = main.querySelector('.title-input'); if (ti) ti.value = state.title; }
      buildFormWideToggle();
      update();
    } }, c)));
    const valuesField = field(m.geo === 'points' ? 'Size / color by' : 'Values to map', chips, m.geo === 'points' ? 'Optional numeric column' : 'Each selected column becomes its own layer');
    valuesField.classList.add('span2');
    fields.append(valuesField);
    const wideBox = h('div', { class: 'wide-box span2' });
    fields.append(wideBox);
    function buildFormWideToggle() {
      wideBox.innerHTML = '';
      if (a.yearCols.length >= 3 && m.geo !== 'points') {
        wideBox.append(h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: m.wide, onchange: (e) => {
          m.wide = e.target.checked;
          if (m.wide) { m.values = [...a.yearCols]; m.year = null; }
          buildForm(); update();
        } }), h('span', {}, `Treat the ${a.yearCols.length} year columns (${a.yearCols[0]}–${a.yearCols.at(-1)}) as one time series`)));
      }
    }
    buildFormWideToggle();
    fields.append(field('Layer title', h('input', { class: 'input title-input', value: state.title, oninput: (e) => { state.title = e.target.value; state.titleEdited = true; } }), m.values.length > 1 && !m.wide ? 'Used when one column is selected; each layer is named after its column' : null));
    fields.append(field('Higher values are…', h('div', { class: 'seg' }, [[true, 'Better'], [null, 'Neutral'], [false, 'Worse']].map(([v, l]) => h('button', { class: state.hib === v ? 'active' : '', onclick: (e) => {
      state.hib = v;
      e.currentTarget.parentNode.querySelectorAll('button').forEach((b) => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
    } }, l))), 'Sets color direction and how the story describes need'));
    main.append(fields, h('div', { class: 'match' }), h('div', { class: 'preview-wrap' }, h('div', { class: 'preview-title' }, 'Preview', h('span', { class: 'legend-roles' }, h('i', { class: 'role-loc' }), 'location', h('i', { class: 'role-val' }), 'value', h('i', { class: 'role-year' }), 'year')), h('div', { class: 'preview' })));
  }

  function field(label, control, hint) {
    return h('div', { class: 'field' }, h('label', {}, label), control, hint ? h('div', { class: 'field-hint' }, hint) : null);
  }

  selectTable(0);
}

// Decide whether a fetched result can be added without showing the wizard.
// With a server hint (World Bank, OWID) we trust the mapping; with autoAll (samples, CLI files)
// every numeric column becomes a layer as long as the places match well.
export async function autoLayers(result, meta = {}) {
  if (!result.tables || result.tables.length !== 1 || (!result.hint && !meta.autoAll)) return null;
  const t = result.tables[0];
  const a = await analyzeTable(t, result.hint);
  const mapping = { ...a.mapping };
  if (!result.hint) {
    const plain = a.numericCols.filter((c) => !a.yearCols.includes(c));
    if (!mapping.wide && plain.length) mapping.values = plain.slice(0, 8);
  }
  if (!mapping.values.length && mapping.geo !== 'points') return null;
  const { layers, report } = await buildLayers(t, mapping, { ...result, higherIsBetter: meta.hib ?? null, sdg: meta.sdg || [],
    titleFor: !result.hint && mapping.values.length > 1 && !mapping.wide ? (c) => c.replace(/_/g, ' ').replace(/^./, (x) => x.toUpperCase()) : null });
  if (!layers.length || report.matched / Math.max(1, report.rows) < (result.hint ? 0.6 : 0.8)) return null;
  return layers;
}

export { esc };
