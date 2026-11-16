// Table analysis: number parsing, column role detection, and turning a table into map layers.
import { countryGazetteer, admin1Gazetteer, matchCountry, matchAdmin1Column } from './geo.js';
import { uid } from './util.js';

const NULLS = new Set(['', '-', '–', '—', '..', '...', 'n/a', 'na', 'nan', 'null', 'none', 'n.a.', 'not available', '?', 'x', '*', 'no data']);
const MULT = { k: 1e3, thousand: 1e3, m: 1e6, mn: 1e6, million: 1e6, millions: 1e6, b: 1e9, bn: 1e9, billion: 1e9, billions: 1e9, t: 1e12, tn: 1e12, trillion: 1e12 };

export function parseNumber(v) {
  if (v == null) return null;
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  let s = String(v).trim();
  if (NULLS.has(s.toLowerCase())) return null;
  s = s.replace(/\[[^\]]*\]/g, '').replace(/[−‒–]/g, '-').replace(/^[~≈<>≤≥+]+/, '').replace(/[$€£¥₹%‰*†‡]/g, '').trim();
  s = s.replace(/^(us\$|usd|eur|gbp)\s*/i, '');
  let mult = 1;
  const m = s.match(/^(-?[\d.,\s]+)\s*([a-z]+)\.?$/i);
  if (m && MULT[m[2].toLowerCase()]) {
    mult = MULT[m[2].toLowerCase()];
    s = m[1];
  }
  s = s.replace(/(\d)[\s  ](?=\d{3}\b)/g, '$1');
  if (/^-?\d{1,3}(,\d{3})+(\.\d+)?$/.test(s)) s = s.replace(/,/g, '');
  else if (/^-?\d+,\d{1,2}$/.test(s)) s = s.replace(',', '.');
  else if (/^-?\d{1,3}(\.\d{3})+,\d+$/.test(s)) s = s.replace(/\./g, '').replace(',', '.');
  // Ranges like "12-15" -> midpoint
  const range = s.match(/^(-?\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$/);
  if (range) return ((+range[1] + +range[2]) / 2) * mult;
  if (!/^-?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(s)) return null;
  const n = parseFloat(s) * mult;
  return Number.isFinite(n) ? n : null;
}

const YEAR_HEADER = /^\s*(?:fy\s*)?((?:18|19|20)\d{2})\s*$/i;
const RANK_HEADER = /^(rank|#|no\.?|n°|pos(ition)?|change|δ|delta)\b/i;
const LOC_HEADER = /\b(iso|iso3|iso_?a3|code|country|countries|nation|entity|location|region|state|province|territory|area|name|place|county|district|geo)\b/i;

function isYearValue(v) {
  const n = parseNumber(v);
  return n != null && Number.isInteger(n) && n >= 1800 && n <= 2100;
}

// Score each column and propose a mapping (location, year, values, points).
export async function analyzeTable(table, hint = null) {
  const { headers, rows } = table;
  const sample = rows.slice(0, 600);
  const gaz = await countryGazetteer();
  let a1 = null;
  const cols = headers.map((hd, ci) => {
    const vals = sample.map((r) => r[ci]);
    const nonEmpty = vals.filter((v) => v != null && String(v).trim() !== '');
    const nums = nonEmpty.map(parseNumber);
    const numericRate = nonEmpty.length ? nums.filter((n) => n != null).length / nonEmpty.length : 0;
    const distinct = new Set(nonEmpty.map(String)).size;
    const countryRate = nonEmpty.length ? nonEmpty.filter((v) => matchCountry(v, gaz)).length / nonEmpty.length : 0;
    const finite = nums.filter((n) => n != null);
    return {
      index: ci, header: hd, numericRate, distinct, filled: nonEmpty.length / Math.max(1, vals.length), countryRate, admin1Rate: 0,
      yearLike: nonEmpty.length > 0 && nonEmpty.filter(isYearValue).length / nonEmpty.length > 0.95 && /year|date|period|time|yr/i.test(hd || 'year'),
      yearHeader: YEAR_HEADER.test(hd || ''),
      rankLike: RANK_HEADER.test((hd || '').trim()),
      latLike: /^(lat|latitude|y)$/i.test((hd || '').trim()) && finite.every((n) => n >= -90 && n <= 90),
      lonLike: /^(lon|lng|long|longitude|x)$/i.test((hd || '').trim()) && finite.every((n) => n >= -180 && n <= 180),
    };
  });
  // Admin-1 matching is costlier; only test text-ish columns.
  const textCols = cols.filter((c) => c.numericRate < 0.5 && c.distinct >= 3);
  if (textCols.length) {
    a1 = await admin1Gazetteer();
    for (const c of textCols) {
      const vals = sample.map((r) => r[c.index]).filter((v) => v != null && String(v).trim() !== '');
      const m = matchAdmin1Column(vals, a1);
      c.admin1Rate = vals.length ? m.filter(Boolean).length / vals.length : 0;
    }
  }
  const byHeader = (name) => (name ? cols.find((c) => c.header === name) : null);

  const lat = cols.find((c) => c.latLike);
  const lon = cols.find((c) => c.lonLike);
  const bestCountry = [...cols].sort((a, b) => b.countryRate - a.countryRate || +LOC_HEADER.test(b.header) - +LOC_HEADER.test(a.header))[0];
  const bestAdmin = [...cols].sort((a, b) => b.admin1Rate - a.admin1Rate)[0];

  let geo, loc;
  if (hint?.geo === 'countries' && byHeader(hint.loc)) {
    geo = 'countries'; loc = byHeader(hint.loc);
  } else if (lat && lon) {
    geo = 'points';
  } else if (bestAdmin && bestAdmin.admin1Rate > 0.5 && bestAdmin.admin1Rate > (bestCountry?.countryRate || 0) + 0.15) {
    geo = 'admin1'; loc = bestAdmin;
  } else if (bestCountry && bestCountry.countryRate >= 0.3) {
    geo = 'countries'; loc = bestCountry;
  } else if (lat && lon) {
    geo = 'points';
  } else if (bestAdmin && bestAdmin.admin1Rate >= 0.3) {
    geo = 'admin1'; loc = bestAdmin;
  } else {
    geo = 'countries'; loc = cols.find((c) => LOC_HEADER.test(c.header) && c.numericRate < 0.5) || cols.find((c) => c.numericRate < 0.5) || cols[0];
  }
  const year = byHeader(hint?.year) || cols.find((c) => c.yearLike);
  const countryCol = geo === 'admin1' ? cols.find((c) => c !== loc && c.countryRate > 0.8) : null;
  const used = new Set([loc?.index, year?.index, lat?.index, lon?.index, countryCol?.index]);
  // Columns that only hold years (e.g. "Year of estimate") describe when, not what; never map them as values.
  const yearValued = (c) => !c.yearHeader && sample.map((r) => r[c.index]).filter((v) => v != null && String(v).trim() !== '').every(isYearValue);
  const numeric = cols.filter((c) => !used.has(c.index) && c.numericRate >= 0.6 && !c.rankLike && !c.yearLike && c.filled > 0.2 && !yearValued(c));
  const yearCols = numeric.filter((c) => c.yearHeader);
  const wide = !year && yearCols.length >= 3;
  let values;
  if (hint?.values?.length) values = hint.values.map(byHeader).filter(Boolean);
  else if (wide) values = yearCols;
  else values = numeric.filter((c) => !c.yearHeader || numeric.length === yearCols.length).slice(0, 1);
  const label = geo === 'points' ? cols.find((c) => c.numericRate < 0.3 && /name|title|place|label|site|city|location/i.test(c.header)) || cols.find((c) => c.numericRate < 0.3 && c.distinct > 3) : null;
  return {
    cols,
    mapping: {
      geo,
      loc: loc ? loc.header : null,
      year: year ? year.header : null,
      countryCol: countryCol ? countryCol.header : null,
      lat: lat ? lat.header : null,
      lon: lon ? lon.header : null,
      label: label ? label.header : null,
      values: values.map((c) => c.header),
      wide,
    },
    numericCols: numeric.map((c) => c.header),
    yearCols: yearCols.map((c) => c.header),
  };
}

// Build layers from a table and a mapping. Returns {layers, report}.
export async function buildLayers(table, mapping, meta = {}) {
  const { headers, rows } = table;
  const ix = (name) => headers.indexOf(name);
  const report = { rows: rows.length, matched: 0, unmatched: new Map(), aggregated: 0 };
  const baseMeta = {
    source: meta.source || 'Imported data', sourceUrl: meta.sourceUrl || '', description: meta.description || '',
    ref: meta.ref || null, sdg: meta.sdg || [], higherIsBetter: meta.higherIsBetter ?? null,
  };

  if (mapping.geo === 'points') {
    const li = ix(mapping.lat), oi = ix(mapping.lon), lbl = ix(mapping.label);
    const vcols = mapping.values.length ? mapping.values : [null];
    const layers = vcols.map((vc) => {
      const vi = vc ? ix(vc) : -1;
      const points = [];
      for (const r of rows) {
        const lat = parseNumber(r[li]), lon = parseNumber(r[oi]);
        if (lat == null || lon == null || Math.abs(lat) > 90 || Math.abs(lon) > 180) continue;
        const v = vi >= 0 ? parseNumber(r[vi]) : 1;
        points.push({ lat, lon, v: v ?? null, label: lbl >= 0 ? String(r[lbl] || '') : '' });
      }
      const title = vcols.length > 1 ? meta.titleFor?.(vc) || vc : meta.title || vc || 'Points';
      return newLayer({ ...baseMeta, title, unit: meta.unit || '', geo: 'points', points });
    });
    report.matched = layers[0]?.points.length || 0;
    return { layers, report };
  }

  const li = ix(mapping.loc);
  const raw = rows.map((r) => r[li]);
  let keys;
  if (mapping.geo === 'admin1') {
    const gaz = await admin1Gazetteer();
    let hints = null;
    if (mapping.countryCol) {
      const cg = await countryGazetteer();
      const ci = ix(mapping.countryCol);
      hints = rows.map((r) => matchCountry(r[ci], cg));
    }
    keys = matchAdmin1Column(raw, gaz, hints);
  } else {
    const gaz = await countryGazetteer();
    keys = raw.map((v) => matchCountry(v, gaz));
  }
  keys.forEach((k, i) => {
    if (k) report.matched++;
    else if (raw[i] != null && String(raw[i]).trim()) report.unmatched.set(String(raw[i]).trim(), (report.unmatched.get(String(raw[i]).trim()) || 0) + 1);
  });

  const yi = mapping.year ? ix(mapping.year) : -1;
  const series = mapping.wide
    ? [{ title: meta.title || 'Value', cols: mapping.values.map((h) => ({ i: ix(h), year: +h.match(/(18|19|20)\d{2}/)[0] })) }]
    : mapping.values.map((h) => ({ title: meta.titleFor?.(h) || (mapping.values.length > 1 || !meta.title ? h : meta.title), cols: [{ i: ix(h), year: null }] }));

  const layers = series.map((s) => {
    const acc = new Map(); // key -> year -> [sum, n]
    const names = {};
    rows.forEach((r, ri) => {
      const k = keys[ri];
      if (!k) return;
      if (!names[k]) names[k] = String(raw[ri]).trim();
      for (const c of s.cols) {
        const v = parseNumber(r[c.i]);
        if (v == null) continue;
        let y = c.year ?? (yi >= 0 ? parseNumber(r[yi]) : null);
        if (y != null && !(Number.isInteger(y) && y > 1000 && y < 2200)) {
          const m = String(r[yi]).match(/(18|19|20)\d{2}/);
          y = m ? +m[0] : null;
        }
        const yk = y == null ? 'NA' : String(y);
        const ym = acc.get(k) || new Map();
        const cell = ym.get(yk) || [0, 0];
        cell[0] += v;
        cell[1] += 1;
        ym.set(yk, cell);
        acc.set(k, ym);
      }
    });
    const values = {};
    const years = new Set();
    for (const [k, ym] of acc) {
      values[k] = {};
      for (const [yk, [sum, n]] of ym) {
        if (n > 1) report.aggregated += n - 1;
        values[k][yk] = sum / n;
        if (yk !== 'NA') years.add(+yk);
      }
    }
    return newLayer({ ...baseMeta, title: s.title, unit: meta.unit || guessUnit(s.title), geo: mapping.geo, values, names, years: [...years].sort((a, b) => a - b) });
  });
  report.unmatched = [...report.unmatched.entries()].sort((a, b) => b[1] - a[1]);
  return { layers: layers.filter((l) => Object.keys(l.values).length), report };
}

const GENERIC = /^(total|all|overall|value|values|rate|number|count|percent|%|score|index|data|amount|estimate|both sexes)$/i;

// A readable layer title from a table title ("List of countries by literacy rate") and column ("Literacy rate – Female").
export function smartTitle(tableTitle, col) {
  const base = String(tableTitle || '')
    .replace(/^list of (the )?(un member( and observer)? states|sovereign states( and dependencies)?|countries( and (dependencies|territories))?|u\.?s\.? states( and territories)?|states( and territories)?( of [a-z .]+)?|indian states( and union territories)?|[a-z ]+? provinces( and territories)?)( by| and)? /i, '')
    .replace(/\s*\(.*\)\s*$/, '')
    .trim();
  const nice = base ? base[0].toUpperCase() + base.slice(1) : '';
  if (!col) return nice;
  const parts = String(col).split(' – ').map((s) => s.trim());
  const last = parts.at(-1);
  if (!nice) return col;
  if (/^column \d+$/i.test(col) || GENERIC.test(col) || (parts.length > 1 && GENERIC.test(last))) return nice;
  if (parts.length > 1) return `${nice} – ${last}`;
  return col.length <= 3 ? `${nice} (${col})` : col;
}

function guessUnit(title) {
  const m = String(title || '').match(/\(([^()]{1,40})\)\s*$/);
  if (m) return m[1];
  if (/%|percent|share|rate \(%/i.test(title)) return '%';
  return '';
}

export function newLayer(props) {
  return {
    id: uid(),
    visible: true,
    style: { palette: null, reverse: false, classes: 'quantile', n: 6, scale: 'auto', opacity: 0.92 },
    years: [],
    names: {},
    ...props,
  };
}
