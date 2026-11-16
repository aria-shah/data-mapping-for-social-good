// Discover panel: curated SDG catalog, live feeds, and federated search that streams in per source.
import { h, api } from './util.js';
import { CATALOG, LIVE, SAMPLES, SDGS } from './catalog.js';

const SOURCES = {
  featured: { label: 'Featured', color: '#a78bfa' },
  wb: { label: 'World Bank', color: '#38bdf8' },
  owid: { label: 'Our World in Data', color: '#f472b6' },
  hdx: { label: 'Humanitarian (HDX)', color: '#fb923c' },
  wiki: { label: 'Wikipedia tables', color: '#a3e635' },
};

let opts;
let seq = 0;
let sdgFilter = null;
let sourceFilter = 'all';
let lastResults = {};
let lastQuery = '';

export function initSearch(o) {
  opts = o; // { onAdd(source, id, meta), onScrape(url, meta), isLoaded(ref) }
  renderHome();
}

const looksLikeUrl = (q) => /^(https?:\/\/|www\.)\S+\.\S+/i.test(q.trim()) || /^[a-z0-9-]+\.[a-z]{2,}(\/\S*)?$/i.test(q.trim());

export function runSearch(q) {
  const pane = document.querySelector('#pane-discover');
  q = q.trim();
  lastQuery = q;
  if (!q) return renderHome();
  const my = ++seq;
  lastResults = {};
  sourceFilter = 'all';
  pane.innerHTML = '';
  if (looksLikeUrl(q)) {
    const url = /^https?:/i.test(q) ? q : 'https://' + q;
    pane.append(h('div', { class: 'scrape-cta' },
      h('div', { class: 'scrape-icon' }, '🕸️'),
      h('div', { style: { minWidth: 0 } }, h('div', { class: 'scrape-title' }, 'Scrape this page for data'), h('div', { class: 'muted scrape-url' }, url)),
      h('button', { class: 'btn-primary sm', onclick: () => opts.onScrape(url) }, 'Scrape')));
    return;
  }
  const featured = CATALOG.concat(LIVE, SAMPLES).filter((c) => matches(q, `${c.title} ${c.sub} ${c.sdg.map((n) => SDGS[n].name).join(' ')}`))
    .map((c) => ({ ...c, source: c.source, featuredSource: c.source, isFeatured: true }));
  lastResults.featured = featured;
  const chipsRow = h('div', { class: 'src-chips' });
  const list = h('div', { class: 'results' });
  pane.append(h('div', { class: 'results-head' }, h('div', { class: 'muted' }, `Results for “${q}”`), h('button', { class: 'btn-ghost sm', onclick: () => { document.querySelector('#search').value = ''; renderHome(); } }, 'Clear')), chipsRow, list);
  const pending = new Set(['wb', 'owid', 'hdx', 'wiki']);
  const paint = () => {
    if (my !== seq) return;
    chipsRow.innerHTML = '';
    const total = Object.values(lastResults).reduce((a, r) => a + r.length, 0);
    chipsRow.append(srcChip('all', 'All', total, '#e2e8f0', pending.size > 0, paint));
    for (const [k, s] of Object.entries(SOURCES)) {
      if (k === 'featured' && !featured.length) continue;
      chipsRow.append(srcChip(k, s.label, lastResults[k]?.length ?? 0, s.color, pending.has(k), paint));
    }
    list.innerHTML = '';
    const order = ['featured', 'wb', 'owid', 'hdx', 'wiki'];
    let shown = 0;
    for (const k of order) {
      if (sourceFilter !== 'all' && sourceFilter !== k) continue;
      const rs = lastResults[k] || [];
      const cap = sourceFilter === 'all' ? (k === 'featured' ? 6 : 5) : 40;
      if (rs.length) {
        list.append(h('div', { class: 'group-title' }, h('i', { style: { background: SOURCES[k].color } }), SOURCES[k].label, rs.length > cap ? h('button', { class: 'link', onclick: () => { sourceFilter = k; paint(); } }, `see all ${rs.length}`) : null));
        rs.slice(0, cap).forEach((r) => list.append(k === 'featured' ? catalogCard(r) : resultCard(r)));
        shown += rs.length;
      } else if (pending.has(k) && (sourceFilter === k || sourceFilter === 'all')) {
        list.append(h('div', { class: 'group-title' }, h('i', { style: { background: SOURCES[k].color } }), SOURCES[k].label, h('span', { class: 'spinner sm' })));
        list.append(h('div', { class: 'skeleton' }), h('div', { class: 'skeleton' }));
      }
    }
    if (!shown && !pending.size) list.append(h('div', { class: 'empty' }, h('p', {}, 'No datasets found.'), h('p', { class: 'muted' }, 'Try a broader word (e.g. “water”, “education”, “poverty”), or paste a link to any web page with a table.')));
  };
  paint();
  for (const s of [...pending]) {
    api(`/api/search?q=${encodeURIComponent(q)}&sources=${s}`)
      .then((d) => { lastResults[s] = d.results[s] || []; })
      .catch(() => { lastResults[s] = []; })
      .finally(() => { pending.delete(s); paint(); });
  }
}

function srcChip(k, label, n, color, loading, repaint) {
  return h('button', { class: `src-chip ${sourceFilter === k ? 'active' : ''}`, style: { '--c': color }, onclick: () => { sourceFilter = k; repaint(); } },
    h('i', {}), label, h('span', { class: 'n' }, loading ? '…' : String(n)));
}

function matches(q, text) {
  const t = text.toLowerCase();
  return q.toLowerCase().split(/\s+/).filter((w) => w.length > 1).every((w) => t.includes(w) || t.includes(w.replace(/s$/, '')));
}

function resultCard(r) {
  const s = SOURCES[r.source];
  const ref = `${r.source}:${r.id}`;
  const loaded = opts.isLoaded(ref);
  const card = h('div', { class: 'result' },
    h('div', { class: 'result-top' }, h('span', { class: 'badge', style: { '--c': s.color } }, s.label), r.url ? h('a', { class: 'ext', href: r.url, target: '_blank', rel: 'noopener', title: 'Open source page' }, '↗') : null),
    h('div', { class: 'result-title' }, r.title),
    r.description ? h('div', { class: 'result-desc' }, r.description) : null,
    r.meta ? h('div', { class: 'result-meta' }, r.meta) : null);
  const actions = h('div', { class: 'result-actions' });
  if (r.source === 'wb' || r.source === 'owid') {
    actions.append(h('button', { class: `btn-primary sm ${loaded ? 'done' : ''}`, onclick: (e) => { e.currentTarget.classList.add('loading'); opts.onAdd(r.source, r.id, { title: r.title }).finally(() => e.target.classList?.remove('loading')); } }, loaded ? '✓ On map' : '+ Add to map'));
  } else if (r.source === 'wiki') {
    actions.append(h('button', { class: 'btn-primary sm', onclick: () => opts.onScrape(r.url, { title: r.title }) }, '🕸️ Scrape tables'));
  } else if (r.source === 'hdx') {
    const files = h('div', { class: 'files' }, (r.resources || []).map((f) => h('button', { class: 'file', onclick: () => opts.onScrape(f.url, { title: r.title }) }, h('span', { class: `kind kind-${f.format}` }, f.format), h('span', { class: 'file-name', title: f.name }, f.name))));
    actions.append(h('button', { class: 'btn-ghost sm', onclick: () => card.classList.toggle('open') }, `${(r.resources || []).length} files ▾`));
    card.append(actions, files);
    return card;
  }
  card.append(actions);
  return card;
}

function catalogCard(c) {
  const ref = `${c.source}:${c.id}`;
  const loaded = opts.isLoaded(ref);
  const sdgs = c.sdg.map((n) => SDGS[n]);
  return h('button', { class: `cat-card ${loaded ? 'loaded' : ''}`, style: { '--sdg': sdgs[0].color }, title: c.sub, onclick: (e) => {
    const el = e.currentTarget;
    el.classList.add('loading');
    const p = c.source === 'url' ? opts.onScrape(c.id, { title: c.title, sdg: c.sdg, points: c.points, live: true }) : opts.onAdd(c.source, c.id, { title: c.title, sdg: c.sdg, hib: c.hib });
    Promise.resolve(p).finally(() => el.classList.remove('loading'));
  } },
    h('div', { class: 'cat-icon' }, c.icon),
    h('div', { class: 'cat-body' }, h('div', { class: 'cat-title' }, c.title), h('div', { class: 'cat-sub' }, c.sub)),
    h('div', { class: 'cat-sdgs' }, sdgs.map((g) => h('span', { style: { background: g.color }, title: `SDG ${g.n}: ${g.name}` }, g.n))),
    loaded ? h('div', { class: 'cat-check' }, '✓') : null);
}

export function renderHome() {
  const pane = document.querySelector('#pane-discover');
  if (!pane) return;
  if (lastQuery && document.querySelector('#search').value.trim()) return runSearch(lastQuery);
  pane.innerHTML = '';
  const sdgRow = h('div', { class: 'sdg-grid' }, SDGS.slice(1).map((g) => h('button', {
    class: `sdg-tile ${sdgFilter === g.n ? 'active' : ''} ${sdgFilter && sdgFilter !== g.n ? 'faded' : ''}`, style: { background: g.color }, title: g.name,
    onclick: () => { sdgFilter = sdgFilter === g.n ? null : g.n; renderHome(); },
  }, h('b', {}, g.n), h('span', {}, g.short || g.name))));
  const items = CATALOG.filter((c) => !sdgFilter || c.sdg.includes(sdgFilter));
  pane.append(
    h('div', { class: 'section-title' }, 'Explore by Sustainable Development Goal', sdgFilter ? h('button', { class: 'link', onclick: () => { sdgFilter = null; renderHome(); } }, 'show all') : null),
    sdgRow,
    h('div', { class: 'section-title' }, sdgFilter ? `${SDGS[sdgFilter].name} · ${items.length} datasets` : `Featured datasets · ${items.length}`),
    h('div', { class: 'cat-grid' }, items.length ? items.map(catalogCard) : h('p', { class: 'muted' }, 'No featured datasets for this goal yet. Try searching above.')),
    h('div', { class: 'section-title' }, h('span', { class: 'live-dot' }), 'Live feeds'),
    h('div', { class: 'cat-grid' }, LIVE.map(catalogCard)),
    h('div', { class: 'section-title' }, 'Sample files'),
    h('div', { class: 'cat-grid' }, SAMPLES.map(catalogCard)),
    h('p', { class: 'hint center' }, 'Search above reaches 1,500 World Bank indicators, 7,000+ Our World in Data charts, the Humanitarian Data Exchange and Wikipedia tables.'));
}
