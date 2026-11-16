// Small DOM, formatting and fetch helpers shared by every module.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') el.innerHTML = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error('Cannot reach the Atlas server. Is "python3 atlas.py" still running?');
  }
  let body;
  try {
    body = await res.json();
  } catch {
    throw new Error(`Server returned an unreadable response (HTTP ${res.status})`);
  }
  if (!res.ok || body.error) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

const nf = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const nf3 = new Intl.NumberFormat('en-US', { maximumSignificantDigits: 3 });

export function fmt(v, unit = '') {
  if (v == null || !Number.isFinite(v)) return '–';
  const a = Math.abs(v);
  let s;
  if (a >= 1e12) s = nf3.format(v / 1e12) + 'T';
  else if (a >= 1e9) s = nf3.format(v / 1e9) + 'B';
  else if (a >= 1e6) s = nf3.format(v / 1e6) + 'M';
  else if (a >= 1e4) s = nf.format(Math.round(v));
  else if (a >= 100) s = nf.format(v);
  else if (a >= 1) s = nf2.format(v);
  else if (a === 0) s = '0';
  else s = nf3.format(v);
  return unit ? `${s}${unitSuffix(unit)}` : s;
}

// "%" sticks to the number; short units get a space; long descriptive units are left to the legend.
export function unitSuffix(unit) {
  if (!unit) return '';
  const u = unit.trim();
  if (/^%|^% of|percent/i.test(u)) return '%';
  if (u.length <= 12) return ' ' + u;
  return '';
}

export function fmtPeople(n) {
  if (!n) return '0';
  if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + ' billion';
  if (n >= 1e6) return Math.round(n / 1e6) + ' million';
  if (n >= 1e3) return Math.round(n / 1e3) + ' thousand';
  return String(Math.round(n));
}

export function flagEmoji(iso2) {
  if (!iso2 || !/^[A-Z]{2}$/.test(iso2)) return '';
  return String.fromCodePoint(...[...iso2].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

export function debounce(fn, ms) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

let toastTimer;
export function toast(msg, kind = 'info', ms = 4200) {
  const box = $('#toasts');
  const el = h('div', { class: `toast toast-${kind}`, role: 'status' }, msg);
  box.append(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 400);
  }, ms);
  clearTimeout(toastTimer);
}

let busyCount = 0;
// Start a busy indicator; returns an idempotent function that ends it.
export function busy(label) {
  busyCount++;
  document.body.classList.add('is-busy');
  if (label) $('#busy-label').textContent = label;
  let done = false;
  return () => {
    if (done) return;
    done = true;
    busyCount = Math.max(0, busyCount - 1);
    document.body.classList.toggle('is-busy', busyCount > 0);
  };
}

export function download(filename, content, type = 'text/plain') {
  const blob = content instanceof Blob ? content : new Blob([content], { type });
  const a = h('a', { href: URL.createObjectURL(blob), download: filename });
  document.body.append(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 1000);
}

export function slug(s) {
  return String(s || 'data').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'data';
}

export function uid() {
  return Math.random().toString(36).slice(2, 9);
}

// Quantile on a pre-sorted ascending array.
export function quantile(sorted, p) {
  if (!sorted.length) return NaN;
  const i = (sorted.length - 1) * p;
  const lo = Math.floor(i);
  const hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}

export function pearson(xs, ys) {
  const n = xs.length;
  if (n < 3) return NaN;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let num = 0, dx = 0, dy = 0;
  for (let i = 0; i < n; i++) {
    num += (xs[i] - mx) * (ys[i] - my);
    dx += (xs[i] - mx) ** 2;
    dy += (ys[i] - my) ** 2;
  }
  return num / Math.sqrt(dx * dy);
}
