// Insight panel: KPIs, auto-written story, benchmark, distribution, rankings, regions, trend, correlation.
import { h, esc, fmt, fmtPeople, flagEmoji, quantile, pearson } from './util.js';
import { SDGS } from './catalog.js';

const CONTINENT_ORDER = ['Africa', 'Asia', 'Europe', 'North America', 'South America', 'Oceania'];
const CONT_COLORS = { Africa: '#f59e0b', Asia: '#ef4444', Europe: '#3b82f6', 'North America': '#a855f7', 'South America': '#22c55e', Oceania: '#06b6d4' };

export function priorityDirection(layer) {
  // Which end of the distribution signals need: 'low' or 'high'.
  return layer.higherIsBetter === false ? 'high' : 'low';
}

export function stats(entries) {
  const vals = entries.map((e) => e.v).sort((a, b) => a - b);
  const n = vals.length;
  if (!n) return null;
  const mean = vals.reduce((a, b) => a + b, 0) / n;
  const sd = Math.sqrt(vals.reduce((a, b) => a + (b - mean) ** 2, 0) / n);
  let pw = 0, pop = 0;
  for (const e of entries) if (e.info?.pop > 0) { pw += e.v * e.info.pop; pop += e.info.pop; }
  return {
    n, min: vals[0], max: vals[n - 1], mean, sd, median: quantile(vals, 0.5), q1: quantile(vals, 0.25), q3: quantile(vals, 0.75),
    p10: quantile(vals, 0.1), p90: quantile(vals, 0.9), popWeighted: pop ? pw / pop : null, popCovered: pop,
  };
}

function regionWord(layer, n = 2) {
  if (layer.geo === 'points') return n === 1 ? 'point' : 'points';
  if (layer.geo === 'admin1') return n === 1 ? 'region' : 'regions';
  return n === 1 ? 'country' : 'countries';
}

export function benchmarkResult(slice, bm) {
  const dir = priorityDirection(slice.layer);
  const hits = slice.entries.filter((e) => (dir === 'low' ? e.v < bm : e.v > bm));
  const pop = hits.reduce((a, e) => a + (e.info?.pop || 0), 0);
  return { hits, pop, dir };
}

// Plain-language narrative generated from the numbers.
export function buildStory(ctx) {
  const { slice, others, benchmark } = ctx;
  const L = slice.layer;
  const s = stats(slice.entries);
  if (!s) return [];
  const u = L.unit || '';
  const hi = slice.entries[0], lo = slice.entries[slice.entries.length - 1];
  const out = [];
  const what = L.title.replace(/\s*\([^)]*\)\s*$/, '');
  const gap = hi.v - lo.v;
  const ratio = lo.v > 0 ? hi.v / lo.v : null;
  out.push(`<b>${esc(what)}</b> ranges from <b>${fmt(lo.v, u)}</b> ${L.geo === 'points' ? 'at' : 'in'} ${esc(lo.name)} to <b>${fmt(hi.v, u)}</b> ${L.geo === 'points' ? 'at' : 'in'} ${esc(hi.name)}${ratio && ratio >= 1.5 ? ` — a <b>${ratio >= 10 ? Math.round(ratio) : ratio.toFixed(1)}×</b> difference` : ` — a gap of <b>${fmt(gap, u)}</b>`} across ${s.n} ${regionWord(L)}${slice.yearLabel ? ` (${esc(slice.yearLabel)})` : ''}.`);
  out.push(`The typical ${regionWord(L, 1)} sits at <b>${fmt(s.median, u)}</b>; the middle half fall between ${fmt(s.q1, u)} and ${fmt(s.q3, u)}.`);
  if (s.popWeighted != null && s.popCovered > 5e8) {
    const diff = (s.popWeighted - s.mean) / Math.abs(s.mean || 1);
    if (Math.abs(diff) > 0.05) out.push(`Weighted by population, the average is <b>${fmt(s.popWeighted, u)}</b> — ${diff > 0 ? 'higher' : 'lower'} than the simple average (${fmt(s.mean, u)}), because populous countries pull it ${diff > 0 ? 'up' : 'down'}.`);
  }
  if (benchmark != null) {
    const b = benchmarkResult(slice, benchmark);
    const word = b.dir === 'low' ? 'below' : 'above';
    if (b.hits.length) {
      out.push(`<span class="hl">${b.hits.length} ${regionWord(L, b.hits.length)}${b.pop ? `, home to about <b>${fmtPeople(b.pop)}</b> people,` : ''} fall ${word} the benchmark of ${fmt(benchmark, u)}.</span>`);
    } else out.push(`No ${regionWord(L)} fall ${word} the benchmark of ${fmt(benchmark, u)}.`);
  }
  if (L.geo === 'countries') {
    const by = groupByContinent(slice.entries);
    const meds = by.filter((g) => g.items.length >= 3).map((g) => ({ c: g.c, m: quantile(g.items.map((e) => e.v).sort((a, b) => a - b), 0.5) }));
    if (meds.length >= 3) {
      meds.sort((a, b) => a.m - b.m);
      const lowC = meds[0], highC = meds[meds.length - 1];
      out.push(`By continent, ${esc(highC.c)} has the highest median (${fmt(highC.m, u)}) and ${esc(lowC.c)} the lowest (${fmt(lowC.m, u)}).`);
    }
  }
  const t = trendSummary(slice.layer, slice.entries);
  if (t) {
    out.push(`Since ${t.y0}, the median has moved from ${fmt(t.m0, u)} to ${fmt(t.m1, u)} (${t.m1 >= t.m0 ? '+' : ''}${fmt(t.m1 - t.m0, u)}).${t.best ? ` Biggest ${L.higherIsBetter === false ? 'reduction' : 'gain'}: <b>${esc(t.best.name)}</b> (${t.best.d >= 0 ? '+' : ''}${fmt(t.best.d, u)}).` : ''}`);
  }
  const rel = others?.[0];
  if (rel && Number.isFinite(rel.r) && Math.abs(rel.r) >= 0.3) {
    const strength = Math.abs(rel.r) >= 0.7 ? 'strongly' : Math.abs(rel.r) >= 0.5 ? 'moderately' : 'weakly';
    out.push(`Across ${rel.n} ${regionWord(L)}, this is ${strength} ${rel.r > 0 ? 'positively' : 'negatively'} correlated with <b>${esc(rel.title)}</b> (r = ${rel.r.toFixed(2)}). Correlation is a clue, not proof of cause.`);
  }
  const dir = priorityDirection(L);
  const worst = (dir === 'low' ? [...slice.entries].reverse() : slice.entries).slice(0, 3);
  if (L.higherIsBetter != null) out.push(`Where need looks greatest: ${worst.map((e) => `<b>${esc(e.name)}</b> (${fmt(e.v, u)})`).join(', ')}.`);
  return out;
}

function trendSummary(layer, entries) {
  if (!layer.years || layer.years.length < 4) return null;
  const y0 = Math.max(layer.years[0], 2000);
  const y1 = layer.years[layer.years.length - 1];
  const pick = (vals, y, back = 3) => { for (let k = 0; k <= back; k++) { const v = vals[String(y - k)] ?? vals[String(y + k)]; if (v != null) return v; } return null; };
  const a = [], b = [];
  let best = null;
  for (const e of entries) {
    const vals = layer.values[e.key];
    const v0 = pick(vals, y0), v1 = pick(vals, y1);
    if (v0 == null || v1 == null) continue;
    a.push(v0); b.push(v1);
    const d = v1 - v0;
    const good = layer.higherIsBetter === false ? -d : d;
    if (!best || good > best.good) best = { name: e.name, d, good };
  }
  if (a.length < 8) return null;
  a.sort((x, y) => x - y); b.sort((x, y) => x - y);
  return { y0, y1, m0: quantile(a, 0.5), m1: quantile(b, 0.5), best: layer.higherIsBetter == null ? null : best };
}

function groupByContinent(entries) {
  const m = new Map();
  for (const e of entries) {
    const c = e.info?.continent;
    if (!c || c === 'Antarctica' || c === 'Seven seas (open ocean)') continue;
    if (!m.has(c)) m.set(c, []);
    m.get(c).push(e);
  }
  return CONTINENT_ORDER.filter((c) => m.has(c)).map((c) => ({ c, items: m.get(c) }));
}

// ---------------------------------------------------------------- rendering

export function renderInsights(root, ctx) {
  root.innerHTML = '';
  const { slice, scale, onSelect, onBenchmark } = ctx;
  if (!slice || !slice.entries.length) {
    root.append(h('div', { class: 'empty-insights' },
      h('div', { class: 'empty-orb' }),
      h('h3', {}, 'Insights appear here'),
      h('p', {}, 'Add a dataset to see who is furthest behind, how many people are affected, and how things are changing.')));
    return;
  }
  const L = slice.layer;
  const s = stats(slice.entries);
  const u = L.unit || '';

  const sdgs = (L.sdg || []).map((n) => SDGS[n]).filter(Boolean);
  root.append(h('div', { class: 'ins-head' },
    h('div', { class: 'ins-kicker' }, sdgs.map((g) => h('span', { class: 'sdg-pill', style: { background: g.color } }, `SDG ${g.n}`)), h('span', { class: 'muted' }, slice.yearLabel || '')),
    h('h2', { class: 'ins-title' }, L.title),
    h('div', { class: 'ins-source' }, L.sourceUrl ? h('a', { href: L.sourceUrl, target: '_blank', rel: 'noopener' }, L.source || 'Source') : (L.source || ''), u ? ` · ${u}` : ''),
    L.description ? h('details', { class: 'ins-desc' }, h('summary', {}, 'About this data'), h('p', {}, L.description)) : null));

  // KPIs
  const spread = s.p10 > 0 ? s.p90 / s.p10 : null;
  root.append(h('div', { class: 'kpis' },
    kpi('Covered', String(s.n), regionWord(L)),
    kpi('Median', fmt(s.median), u.length <= 14 ? u : ''),
    kpi('Range', `${fmt(s.min)}–${fmt(s.max)}`, ''),
    kpi(spread ? 'Top ÷ bottom 10%' : 'Std. dev.', spread ? `${spread >= 10 ? Math.round(spread) : spread.toFixed(1)}×` : fmt(s.sd), spread ? 'inequality between places' : '')));

  // Story
  const story = buildStory(ctx);
  const storyBox = h('div', { class: 'card story' },
    h('div', { class: 'card-head' }, h('h4', {}, '✨ The story in this data'),
      h('button', { class: 'btn-ghost sm', onclick: () => { navigator.clipboard?.writeText(storyBox.querySelector('.story-body').innerText); storyBox.querySelector('.copied').classList.add('show'); setTimeout(() => storyBox.querySelector('.copied')?.classList.remove('show'), 1500); } }, 'Copy'),
      h('span', { class: 'copied' }, 'Copied')),
    h('div', { class: 'story-body', html: story.map((p) => `<p>${p}</p>`).join('') }));
  root.append(storyBox);

  // Benchmark
  const dir = priorityDirection(L);
  const bmVal = ctx.benchmark ?? (dir === 'low' ? s.q1 : s.q3);
  const bm = benchmarkResult(slice, bmVal);
  const step = niceStep(s.max - s.min);
  const bmCard = h('div', { class: `card bench ${ctx.benchmark != null ? 'on' : ''}` },
    h('div', { class: 'card-head' }, h('h4', {}, '🎯 Benchmark & people affected'),
      h('label', { class: 'switch' }, h('input', { type: 'checkbox', checked: ctx.benchmark != null, onchange: (e) => onBenchmark(e.target.checked ? bmVal : null) }), h('span', {}))),
    h('div', { class: 'bench-row' },
      h('span', { class: 'muted' }, `Flag places ${dir === 'low' ? 'below' : 'above'}`),
      h('input', { type: 'number', class: 'bench-num', value: +bmVal.toFixed(2), step, onchange: (e) => onBenchmark(parseFloat(e.target.value)) })),
    h('input', { type: 'range', class: 'bench-range', min: s.min, max: s.max, step, value: bmVal, oninput: (e) => onBenchmark(parseFloat(e.target.value), true) }),
    h('div', { class: 'bench-out' },
      h('div', {}, h('div', { class: 'big' }, String(bm.hits.length)), h('div', { class: 'muted' }, `${regionWord(L, bm.hits.length)} ${dir === 'low' ? 'below' : 'above'}`)),
      L.geo === 'countries' ? h('div', {}, h('div', { class: 'big accent' }, bm.pop ? fmtPeople(bm.pop).replace(' billion', 'B').replace(' million', 'M').replace(' thousand', 'K') : '0'), h('div', { class: 'muted' }, 'people live there')) : null),
    h('p', { class: 'hint' }, ctx.benchmark != null ? 'Flagged places glow pink on the map (and pulse on the globe).' : 'Turn on to highlight places that miss a target, e.g. an SDG threshold.'));
  root.append(bmCard);

  // Distribution
  root.append(card('📊 Distribution', histogram(slice.entries, scale, ctx.benchmark, u)));

  // Rankings
  const rk = h('div', { class: 'card' });
  const tabs = h('div', { class: 'seg sm' });
  const list = h('div', { class: 'rank-list' });
  const showRank = (which) => {
    tabs.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.k === which));
    list.innerHTML = '';
    const arr = which === 'top' ? slice.entries.slice(0, 10) : [...slice.entries].reverse().slice(0, 10);
    const max = Math.max(...slice.entries.map((e) => Math.abs(e.v))) || 1;
    arr.forEach((e) => {
      const rank = slice.rank.get(e.key);
      list.append(h('button', { class: 'rank-row', onclick: () => onSelect(e.key) },
        h('span', { class: 'rank-n' }, `#${rank}`),
        h('span', { class: 'rank-name' }, `${flagEmoji(e.info?.iso2) || ''} ${e.name}`),
        h('span', { class: 'rank-bar' }, h('i', { style: { width: `${Math.max(2, (Math.abs(e.v) / max) * 100)}%`, background: scale.color(e.v) } })),
        h('span', { class: 'rank-v' }, fmt(e.v, u))));
    });
  };
  ['top', 'bottom'].forEach((k) => tabs.append(h('button', { 'data-k': k, onclick: () => showRank(k) }, k === 'top' ? 'Highest 10' : 'Lowest 10')));
  rk.append(h('div', { class: 'card-head' }, h('h4', {}, '🏁 Rankings'), tabs), list);
  showRank(dir === 'low' ? 'bottom' : 'top');
  root.append(rk);

  if (L.geo === 'countries') {
    const groups = groupByContinent(slice.entries);
    if (groups.length >= 3) root.append(card('🌍 By continent', stripPlot(groups, scale, u, onSelect)));
  }
  if (L.years?.length >= 3) root.append(card('📈 Over time', trendChart(L, slice, ctx.selectedKey, scale, u)));
  if (ctx.others?.length) root.append(card('🔗 Relationships', scatter(ctx, u)));
}

function niceStep(range) {
  if (!(range > 0)) return 'any';
  const p = 10 ** Math.floor(Math.log10(range / 200));
  return p;
}

function kpi(label, value, sub) {
  return h('div', { class: 'kpi' }, h('div', { class: 'kpi-label' }, label), h('div', { class: 'kpi-value' }, value), sub ? h('div', { class: 'kpi-sub' }, sub) : null);
}

function card(title, body) {
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h4', {}, title)), body);
}

const W = 340;

function histogram(entries, scale, bm, u) {
  const H = 130, m = { t: 8, r: 8, b: 22, l: 8 };
  const vals = entries.map((e) => e.v);
  const x = (scale.log ? d3.scaleLog() : d3.scaleLinear()).domain(d3.extent(vals)).range([m.l, W - m.r]);
  if (!scale.log) x.nice();
  const bins = d3.bin().domain(x.domain()).thresholds(scale.log ? d3.range(25).map((i) => 10 ** (Math.log10(x.domain()[0]) + (i / 24) * (Math.log10(x.domain()[1]) - Math.log10(x.domain()[0])))) : 22)(vals);
  const y = d3.scaleLinear().domain([0, d3.max(bins, (b) => b.length)]).range([H - m.b, m.t]);
  const svg = d3.create('svg').attr('viewBox', `0 0 ${W} ${H}`).attr('class', 'chart');
  svg.selectAll('rect').data(bins).join('rect')
    .attr('x', (b) => x(b.x0) + 0.5).attr('width', (b) => Math.max(0, x(b.x1) - x(b.x0) - 1))
    .attr('y', (b) => y(b.length)).attr('height', (b) => y(0) - y(b.length)).attr('rx', 2)
    .attr('fill', (b) => scale.color((b.x0 + b.x1) / 2) || '#888')
    .append('title').text((b) => `${fmt(b.x0)}–${fmt(b.x1)}${u.length < 4 ? u : ''}: ${b.length}`);
  svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(5, scale.log ? '~s' : '~s').tickSizeOuter(0));
  if (bm != null && Number.isFinite(bm)) {
    svg.append('line').attr('class', 'bm-line').attr('x1', x(bm)).attr('x2', x(bm)).attr('y1', m.t - 4).attr('y2', H - m.b);
    svg.append('text').attr('class', 'bm-label').attr('x', x(bm) + 4).attr('y', m.t + 8).text('benchmark');
  }
  return svg.node();
}

function stripPlot(groups, scale, u, onSelect) {
  const rowH = 26, m = { t: 6, r: 12, b: 22, l: 104 };
  const H = m.t + m.b + groups.length * rowH;
  const all = groups.flatMap((g) => g.items.map((e) => e.v));
  const x = (scale.log ? d3.scaleLog() : d3.scaleLinear()).domain(d3.extent(all)).range([m.l, W - m.r]);
  if (!scale.log) x.nice();
  const svg = d3.create('svg').attr('viewBox', `0 0 ${W} ${H}`).attr('class', 'chart');
  groups.forEach((g, i) => {
    const cy = m.t + i * rowH + rowH / 2;
    svg.append('text').attr('class', 'strip-label').attr('x', m.l - 10).attr('y', cy + 4).attr('text-anchor', 'end').text(g.c);
    svg.append('line').attr('class', 'strip-track').attr('x1', m.l).attr('x2', W - m.r).attr('y1', cy).attr('y2', cy);
    const med = quantile(g.items.map((e) => e.v).sort((a, b) => a - b), 0.5);
    svg.selectAll(null).data(g.items).join('circle').attr('class', 'strip-dot')
      .attr('cx', (e) => x(e.v)).attr('cy', (e, j) => cy + ((j % 5) - 2) * 2.2).attr('r', 4)
      .attr('fill', (e) => scale.color(e.v)).on('click', (_ev, e) => onSelect(e.key))
      .append('title').text((e) => `${e.name}: ${fmt(e.v, u)}`);
    svg.append('line').attr('class', 'strip-med').attr('x1', x(med)).attr('x2', x(med)).attr('y1', cy - 10).attr('y2', cy + 10).attr('stroke', CONT_COLORS[g.c] || '#fff');
  });
  svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(5, '~s').tickSizeOuter(0));
  return h('div', {}, svg.node(), h('p', { class: 'hint' }, 'Each dot is a country; the vertical tick marks the continent median. Click a dot to inspect.'));
}

function trendChart(L, slice, selectedKey, scale, u) {
  const H = 150, m = { t: 10, r: 10, b: 22, l: 38 };
  const years = L.years.filter((y) => y >= 1990);
  const rows = years.map((y) => {
    const vals = Object.values(L.values).map((v) => v[String(y)]).filter((v) => v != null).sort((a, b) => a - b);
    return { y, n: vals.length, q1: quantile(vals, 0.25), med: quantile(vals, 0.5), q3: quantile(vals, 0.75) };
  }).filter((r) => r.n >= Math.max(5, slice.entries.length * 0.2));
  if (rows.length < 3) return h('p', { class: 'hint' }, 'Not enough years with good coverage to draw a trend.');
  const sel = selectedKey && L.values[selectedKey] ? years.map((y) => ({ y, v: L.values[selectedKey][String(y)] })).filter((d) => d.v != null) : [];
  const x = d3.scaleLinear().domain(d3.extent(rows, (r) => r.y)).range([m.l, W - m.r]);
  const y = d3.scaleLinear().domain([d3.min([...rows.map((r) => r.q1), ...sel.map((d) => d.v)]), d3.max([...rows.map((r) => r.q3), ...sel.map((d) => d.v)])]).nice().range([H - m.b, m.t]);
  const svg = d3.create('svg').attr('viewBox', `0 0 ${W} ${H}`).attr('class', 'chart');
  const grad = 'g' + Math.random().toString(36).slice(2, 7);
  const lg = svg.append('defs').append('linearGradient').attr('id', grad).attr('x1', 0).attr('x2', 0).attr('y1', 1).attr('y2', 0);
  [0, 0.5, 1].forEach((t) => lg.append('stop').attr('offset', t).attr('stop-color', scale.color(y.invert(H - m.b - t * (H - m.b - m.t))) || '#888'));
  svg.append('path').datum(rows).attr('class', 'band').attr('fill', `url(#${grad})`).attr('d', d3.area().x((r) => x(r.y)).y0((r) => y(r.q1)).y1((r) => y(r.q3)).curve(d3.curveMonotoneX));
  svg.append('path').datum(rows).attr('class', 'med-line').attr('d', d3.line().x((r) => x(r.y)).y((r) => y(r.med)).curve(d3.curveMonotoneX));
  if (sel.length > 1) svg.append('path').datum(sel).attr('class', 'sel-line').attr('d', d3.line().x((d) => x(d.y)).y((d) => y(d.v)).curve(d3.curveMonotoneX));
  svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(5, 'd').tickSizeOuter(0));
  svg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4, '~s').tickSizeOuter(0));
  const selName = selectedKey ? slice.byKey.get(selectedKey)?.name || L.names?.[selectedKey] : null;
  return h('div', {}, svg.node(), h('div', { class: 'chart-legend' },
    h('span', {}, h('i', { class: 'lg-band' }), 'middle 50%'), h('span', {}, h('i', { class: 'lg-med' }), 'median'),
    sel.length > 1 ? h('span', {}, h('i', { class: 'lg-sel' }), selName) : h('span', { class: 'muted' }, 'Select a place to compare')));
}

function scatter(ctx, u) {
  const { others, slice, onSelect, onPickOther } = ctx;
  const wrap = h('div', {});
  const pick = h('select', { class: 'select sm', onchange: (e) => onPickOther(e.target.value) },
    others.map((o) => h('option', { value: o.id, selected: o.id === ctx.otherId }, o.title)));
  const o = others.find((x) => x.id === ctx.otherId) || others[0];
  wrap.append(h('div', { class: 'scatter-head' }, h('span', { class: 'muted' }, 'Compare with'), pick));
  const pts = o.pairs;
  if (pts.length < 5) {
    wrap.append(h('p', { class: 'hint' }, 'Too few places have both values.'));
    return wrap;
  }
  const H = 220, m = { t: 10, r: 10, b: 30, l: 42 };
  const logX = o.logX, logY = slice.logScale;
  const x = (logX ? d3.scaleLog() : d3.scaleLinear()).domain(d3.extent(pts, (p) => p.x)).range([m.l, W - m.r]);
  const y = (logY ? d3.scaleLog() : d3.scaleLinear()).domain(d3.extent(pts, (p) => p.y)).range([H - m.b, m.t]);
  if (!logX) x.nice();
  if (!logY) y.nice();
  const svg = d3.create('svg').attr('viewBox', `0 0 ${W} ${H}`).attr('class', 'chart');
  svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(5, '~s').tickSizeOuter(0));
  svg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5, '~s').tickSizeOuter(0));
  // least-squares trend line in (possibly log) space
  const tx = (v) => (logX ? Math.log10(v) : v), ty = (v) => (logY ? Math.log10(v) : v);
  const X = pts.map((p) => tx(p.x)), Y = pts.map((p) => ty(p.y));
  const mx = d3.mean(X), my = d3.mean(Y);
  const slope = d3.sum(X, (xv, i) => (xv - mx) * (Y[i] - my)) / d3.sum(X, (xv) => (xv - mx) ** 2);
  const [d0, d1] = x.domain();
  const line = [d0, d1].map((d) => ({ x: d, y: logY ? 10 ** (my + slope * (tx(d) - mx)) : my + slope * (tx(d) - mx) }));
  const yd = y.domain();
  svg.append('line').attr('class', 'trend').attr('x1', x(line[0].x)).attr('x2', x(line[1].x))
    .attr('y1', y(Math.max(Math.min(line[0].y, yd[1]), yd[0]))).attr('y2', y(Math.max(Math.min(line[1].y, yd[1]), yd[0])));
  svg.selectAll('circle').data(pts).join('circle').attr('class', 'sc-dot')
    .attr('cx', (p) => x(p.x)).attr('cy', (p) => y(p.y)).attr('r', (p) => (p.key === ctx.selectedKey ? 7 : 4.2))
    .attr('fill', (p) => CONT_COLORS[p.continent] || '#94a3b8').attr('stroke', (p) => (p.key === ctx.selectedKey ? '#fff' : 'none'))
    .on('click', (_e, p) => onSelect(p.key))
    .append('title').text((p) => `${p.name}\n${o.title}: ${fmt(p.x)}\n${slice.layer.title}: ${fmt(p.y)}`);
  svg.append('text').attr('class', 'ax-title').attr('x', W - m.r).attr('y', H - 2).attr('text-anchor', 'end').text(trim(o.title, 48) + ' →');
  svg.append('text').attr('class', 'ax-title').attr('x', m.l + 4).attr('y', m.t + 6).text('↑ ' + trim(slice.layer.title, 44));
  wrap.append(svg.node());
  const r = o.r;
  wrap.append(h('div', { class: 'corr' },
    h('div', { class: 'corr-r', style: { color: r > 0 ? '#34d399' : '#f472b6' } }, `r = ${r.toFixed(2)}`),
    h('div', { class: 'muted' }, `${pts.length} places · ${Math.abs(r) >= 0.7 ? 'strong' : Math.abs(r) >= 0.4 ? 'moderate' : 'weak'} ${r >= 0 ? 'positive' : 'negative'} relationship`)));
  if (slice.layer.geo === 'countries') wrap.append(h('div', { class: 'chart-legend' }, CONTINENT_ORDER.map((c) => h('span', {}, h('i', { class: 'dot', style: { background: CONT_COLORS[c] } }), c))));
  return wrap;
}

function trim(s, n) {
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

export function correlationsFor(slice, otherSlices) {
  return otherSlices.map((os) => {
    const pairs = [];
    for (const e of slice.entries) {
      const o = os.byKey.get(e.key);
      if (o) pairs.push({ key: e.key, name: e.name, x: o.v, y: e.v, continent: e.info?.continent });
    }
    const usable = pairs.filter((p) => (!os.logScale || p.x > 0) && (!slice.logScale || p.y > 0));
    return { id: os.layer.id, title: os.layer.title, pairs: usable, n: usable.length, logX: os.logScale,
      r: pearson(usable.map((p) => (os.logScale ? Math.log10(p.x) : p.x)), usable.map((p) => (slice.logScale ? Math.log10(p.y) : p.y))) };
  }).filter((o) => o.n >= 5);
}
