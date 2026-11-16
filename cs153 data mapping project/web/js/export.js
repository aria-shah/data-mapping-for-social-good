// Exports: poster PNG (map + title + legend + story), merged CSV, standalone HTML impact brief.
import { download, slug, fmt, esc, flagEmoji } from './util.js';

function wrapText(ctx, text, x, y, maxW, lineH, maxLines = 4) {
  const words = text.split(/\s+/);
  let line = '', lines = 0;
  for (const w of words) {
    const test = line ? line + ' ' + w : w;
    if (ctx.measureText(test).width > maxW && line) {
      ctx.fillText(line, x, y);
      y += lineH;
      line = w;
      if (++lines >= maxLines - 1) { line = words.slice(words.indexOf(w)).join(' '); break; }
    } else line = test;
  }
  if (ctx.measureText(line).width > maxW) {
    while (line.length > 3 && ctx.measureText(line + '…').width > maxW) line = line.slice(0, -1);
    line += '…';
  }
  ctx.fillText(line, x, y);
  return y + lineH;
}

function drawLegend(ctx, legend, x, y, w, unit, dark) {
  ctx.fillStyle = dark ? '#cbd5e1' : '#334155';
  ctx.font = '600 18px Inter, system-ui, sans-serif';
  if (legend.type === 'continuous') {
    const g = ctx.createLinearGradient(x, 0, x + w, 0);
    legend.gradient.forEach((c, i) => g.addColorStop(i / (legend.gradient.length - 1), c));
    ctx.fillStyle = g;
    roundRect(ctx, x, y, w, 16, 8);
    ctx.fill();
    ctx.fillStyle = dark ? '#cbd5e1' : '#334155';
    ctx.fillText(fmt(legend.lo), x, y + 42);
    ctx.textAlign = 'right';
    ctx.fillText(fmt(legend.hi) + (unit && unit.length < 10 ? ' ' + unit : ''), x + w, y + 42);
    ctx.textAlign = 'left';
  } else if (legend.type === 'classes') {
    const k = legend.colors.length;
    const bw = w / k;
    legend.colors.forEach((c, i) => {
      ctx.fillStyle = c;
      roundRect(ctx, x + i * bw + 2, y, bw - 4, 16, 5);
      ctx.fill();
    });
    ctx.fillStyle = dark ? '#cbd5e1' : '#334155';
    ctx.font = '500 15px Inter, system-ui, sans-serif';
    legend.breaks.forEach((b, i) => {
      if (k > 6 && i % 2 && i !== legend.breaks.length - 1) return;
      ctx.textAlign = i === 0 ? 'left' : i === legend.breaks.length - 1 ? 'right' : 'center';
      ctx.fillText(fmt(b), x + i * bw, y + 40);
    });
    ctx.textAlign = 'left';
  }
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

export function posterCanvas({ mapCanvas, layer, legend, yearLabel, story, theme }) {
  const W = 1920, H = 1080;
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const ctx = c.getContext('2d');
  const dark = theme === 'dark';
  const bg = ctx.createRadialGradient(W * 0.6, H * 0.45, 100, W * 0.6, H * 0.5, W * 0.8);
  bg.addColorStop(0, dark ? '#101a3d' : '#ffffff');
  bg.addColorStop(1, dark ? '#03050f' : '#e5ecf6');
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, W, H);
  // map image, fit into right area
  const mx = 520, my = 40, mw = W - mx - 40, mh = H - 80;
  const s = Math.min(mw / mapCanvas.width, mh / mapCanvas.height);
  const dw = mapCanvas.width * s, dh = mapCanvas.height * s;
  try {
    ctx.drawImage(mapCanvas, mx + (mw - dw) / 2, my + (mh - dh) / 2, dw, dh);
  } catch { /* tainted canvas (external tiles) */ }
  // text column
  const grad = ctx.createLinearGradient(60, 0, 420, 0);
  ['#a78bfa', '#38bdf8', '#34d399'].forEach((col, i) => grad.addColorStop(i / 2, col));
  ctx.fillStyle = grad;
  ctx.font = '700 20px "Space Grotesk", Inter, sans-serif';
  ctx.fillText('COMMON GROUND ATLAS', 60, 90);
  ctx.fillStyle = dark ? '#f8fafc' : '#0f172a';
  ctx.font = '700 46px "Space Grotesk", Inter, sans-serif';
  let y = wrapText(ctx, layer.title, 60, 160, 420, 54, 4);
  ctx.fillStyle = dark ? '#94a3b8' : '#475569';
  ctx.font = '500 20px Inter, system-ui, sans-serif';
  y = wrapText(ctx, [yearLabel, layer.unit].filter(Boolean).join(' · '), 60, y + 6, 420, 28, 2);
  drawLegend(ctx, legend, 60, y + 20, 400, layer.unit, dark);
  y += 110;
  ctx.fillStyle = dark ? '#e2e8f0' : '#1e293b';
  ctx.font = '400 21px Inter, system-ui, sans-serif';
  for (const p of story.slice(0, 4)) {
    const text = p.replace(/<[^>]+>/g, '');
    y = wrapText(ctx, text, 60, y, 420, 30, 5) + 14;
    if (y > H - 140) break;
  }
  ctx.fillStyle = dark ? '#64748b' : '#64748b';
  ctx.font = '400 15px Inter, system-ui, sans-serif';
  wrapText(ctx, `Source: ${layer.source || 'user data'}. Boundaries: Natural Earth.`, 60, H - 70, 420, 22, 2);
  return c;
}

export function exportPNG(args) {
  const c = posterCanvas(args);
  c.toBlob((b) => download(`${slug(args.layer.title)}.png`, b), 'image/png');
}

export function exportCSV(layers, sliceFor, nameOf) {
  const region = layers.filter((l) => l.geo !== 'points');
  if (!region.length) {
    const pts = layers.find((l) => l.geo === 'points');
    if (!pts) return;
    const rows = [['latitude', 'longitude', 'label', pts.title]];
    pts.points.forEach((p) => rows.push([p.lat, p.lon, p.label, p.v]));
    return download(`${slug(pts.title)}.csv`, toCsv(rows), 'text/csv');
  }
  const geo = region[0].geo;
  const same = region.filter((l) => l.geo === geo);
  const slices = same.map(sliceFor);
  const keys = [...new Set(slices.flatMap((s) => s.entries.map((e) => e.key)))].sort();
  const rows = [[geo === 'admin1' ? 'iso_3166_2' : 'iso3', 'name', ...same.map((l) => l.title)]];
  keys.forEach((k) => rows.push([k, nameOf(k, geo), ...slices.map((s) => s.byKey.get(k)?.v ?? '')]));
  download(`atlas-${slug(same[0].title)}.csv`, toCsv(rows), 'text/csv');
}

function toCsv(rows) {
  return rows.map((r) => r.map((c) => {
    const s = String(c ?? '');
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }).join(',')).join('\n');
}

export function exportBrief({ mapCanvas, layer, legend, yearLabel, story, slice, theme, benchmark, bmResult }) {
  const png = posterCanvas({ mapCanvas, layer, legend, yearLabel, story, theme: 'light' }).toDataURL('image/png');
  const u = layer.unit || '';
  const dirLow = layer.higherIsBetter !== false;
  const need = (dirLow ? [...slice.entries].reverse() : slice.entries).slice(0, 10);
  const best = (dirLow ? slice.entries : [...slice.entries].reverse()).slice(0, 10);
  const row = (e) => `<tr><td>${flagEmoji(e.info?.iso2)} ${esc(e.name)}</td><td class="n">${fmt(e.v, u)}</td></tr>`;
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Impact brief: ${esc(layer.title)}</title>
<style>
body{font-family:Inter,system-ui,-apple-system,sans-serif;color:#0f172a;max-width:880px;margin:40px auto;padding:0 24px;line-height:1.6;background:#fff}
h1{font-size:30px;line-height:1.2;margin:4px 0 6px}.kicker{letter-spacing:.14em;font-size:12px;font-weight:700;color:#7c3aed}
.meta{color:#475569;font-size:14px}img{width:100%;border-radius:14px;margin:20px 0;border:1px solid #e2e8f0}
.story p{margin:0 0 10px}.hl{background:#fde2f0;padding:1px 4px;border-radius:4px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px}table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:6px 4px;border-bottom:1px solid #eef2f7}td.n{text-align:right;font-variant-numeric:tabular-nums}
h3{font-size:15px;margin:22px 0 6px}footer{margin-top:30px;color:#64748b;font-size:12px}
@media print{body{margin:0}}
</style></head><body>
<div class="kicker">IMPACT BRIEF · COMMON GROUND ATLAS</div>
<h1>${esc(layer.title)}</h1>
<div class="meta">${esc(yearLabel || '')}${u ? ' · ' + esc(u) : ''} · Source: ${layer.sourceUrl ? `<a href="${esc(layer.sourceUrl)}">${esc(layer.source)}</a>` : esc(layer.source || 'user data')}</div>
<img src="${png}" alt="Map of ${esc(layer.title)}">
<h3>Key findings</h3><div class="story">${story.map((p) => `<p>${p}</p>`).join('')}</div>
${benchmark != null ? `<h3>Benchmark</h3><p>${bmResult.hits.length} places fall ${bmResult.dir === 'low' ? 'below' : 'above'} ${fmt(benchmark, u)}: ${bmResult.hits.slice(0, 30).map((e) => esc(e.name)).join(', ')}${bmResult.hits.length > 30 ? '…' : ''}.</p>` : ''}
<div class="cols"><div><h3>${layer.higherIsBetter == null ? 'Lowest values' : 'Greatest need'}</h3><table>${need.map(row).join('')}</table></div>
<div><h3>${layer.higherIsBetter == null ? 'Highest values' : 'Strongest outcomes'}</h3><table>${best.map(row).join('')}</table></div></div>
${layer.description ? `<h3>About the data</h3><p class="meta">${esc(layer.description)}</p>` : ''}
<footer>Generated ${new Date().toLocaleDateString()} with Common Ground Atlas. Figures are as reported by the source; check recency and methodology before citing. Boundaries: Natural Earth (public domain); boundary depiction does not imply endorsement.</footer>
</body></html>`;
  download(`impact-brief-${slug(layer.title)}.html`, html, 'text/html');
}
