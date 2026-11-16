// Color palettes, classification and legend specs. Uses d3 (global) for interpolation.
import { quantile } from './util.js';

const custom = (stops) => d3.interpolateRgbBasis(stops);

export const PALETTES = {
  aurora: { name: 'Aurora', fn: custom(['#5b2a9e', '#4a55c9', '#2a8fd0', '#1fbcaa', '#6bdc8a', '#d9f56b']) },
  sunset: { name: 'Sunset', fn: custom(['#4b1a7a', '#8a2a8c', '#c63f7c', '#ef6a5a', '#f9a53f', '#fde68a']) },
  ocean: { name: 'Ocean', fn: custom(['#24408f', '#2166b0', '#1f97c4', '#3cc8c4', '#b0f2dc']) },
  ember: { name: 'Ember', fn: (t) => d3.interpolateYlOrRd(0.08 + t * 0.92) },
  viridis: { name: 'Viridis', fn: (t) => d3.interpolateViridis(0.1 + t * 0.9) },
  magma: { name: 'Magma', fn: (t) => d3.interpolateMagma(0.22 + t * 0.76) },
  plasma: { name: 'Plasma', fn: (t) => d3.interpolatePlasma(0.08 + t * 0.9) },
  turbo: { name: 'Turbo', fn: (t) => d3.interpolateTurbo(0.05 + t * 0.9) },
  cividis: { name: 'Cividis', fn: (t) => d3.interpolateCividis(0.12 + t * 0.88) },
  rose: { name: 'Rose', fn: (t) => d3.interpolateRdPu(0.1 + t * 0.9) },
  mint: { name: 'Mint', fn: (t) => d3.interpolateBuGn(0.12 + t * 0.88) },
  glacier: { name: 'Glacier', fn: (t) => d3.interpolateBlues(0.12 + t * 0.88) },
  cool: { name: 'Neon', fn: d3.interpolateCool },
  spectral: { name: 'Spectral', fn: (t) => d3.interpolateSpectral(1 - t), diverging: true },
  redblue: { name: 'Red–Blue', fn: (t) => d3.interpolateRdYlBu(t), diverging: true },
  pinkgreen: { name: 'Pink–Green', fn: d3.interpolatePiYG, diverging: true },
  earth: { name: 'Earth', fn: d3.interpolateBrBG, diverging: true },
};

// Pick a palette that reads correctly: when "higher is better", high values get calm/cool colors.
export function defaultPalette(layer, index = 0) {
  if (layer.geo === 'points') return 'sunset';
  if (layer.higherIsBetter === true) return ['aurora', 'viridis', 'ocean', 'mint'][index % 4];
  if (layer.higherIsBetter === false) return ['ember', 'magma', 'sunset', 'rose'][index % 4];
  return ['aurora', 'sunset', 'ocean', 'plasma', 'turbo', 'rose'][index % 6];
}

export function paletteFn(style) {
  const p = PALETTES[style.palette] || PALETTES.aurora;
  return style.reverse ? (t) => p.fn(1 - t) : p.fn;
}

export function paletteCss(name, reverse = false, angle = 90, steps = 8) {
  const p = PALETTES[name] || PALETTES.aurora;
  const stops = d3.range(steps).map((i) => p.fn(reverse ? 1 - i / (steps - 1) : i / (steps - 1)));
  return `linear-gradient(${angle}deg, ${stops.join(',')})`;
}

function skew(sorted) {
  const n = sorted.length;
  if (n < 5) return 0;
  const mean = d3.mean(sorted);
  const sd = d3.deviation(sorted) || 1;
  return d3.sum(sorted, (v) => ((v - mean) / sd) ** 3) / n;
}

// Jenks natural breaks (Fisher), fine for a few thousand values.
function jenks(data, k) {
  const n = data.length;
  if (n <= k) return data.slice();
  const lower = Array.from({ length: n + 1 }, () => new Array(k + 1).fill(0));
  const vari = Array.from({ length: n + 1 }, () => new Array(k + 1).fill(Infinity));
  for (let i = 1; i <= k; i++) { lower[1][i] = 1; vari[1][i] = 0; }
  for (let l = 2; l <= n; l++) {
    let s1 = 0, s2 = 0, w = 0, v = 0;
    for (let m = 1; m <= l; m++) {
      const i3 = l - m + 1;
      const val = data[i3 - 1];
      s2 += val * val; s1 += val; w++;
      v = s2 - (s1 * s1) / w;
      const i4 = i3 - 1;
      if (i4 !== 0) for (let j = 2; j <= k; j++) if (vari[l][j] >= v + vari[i4][j - 1]) { lower[l][j] = i3; vari[l][j] = v + vari[i4][j - 1]; }
    }
    lower[l][1] = 1; vari[l][1] = v;
  }
  const breaks = new Array(k + 1);
  breaks[k] = data[n - 1];
  breaks[0] = data[0];
  let c = n;
  for (let j = k; j >= 2; j--) { const id = lower[c][j] - 2; breaks[j - 1] = data[id]; c = lower[c][j] - 1; }
  return breaks;
}

// Build a color scale for an array of numbers given a layer style.
// Returns { color(v), t(v) in [0,1], legend, log, domain }
export function buildScale(values, style) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  const pal = paletteFn(style);
  const empty = { color: () => null, t: () => 0, legend: { type: 'none' }, domain: [0, 1], log: false };
  if (!sorted.length) return empty;
  const lo = sorted[0], hi = sorted[sorted.length - 1];
  const log = style.scale === 'log' ? lo > 0 : style.scale === 'auto' ? lo > 0 && hi / lo > 50 && skew(sorted) > 1.6 : false;
  const tf = log ? (v) => Math.log10(v) : (v) => v;
  const tlo = tf(lo), thi = tf(hi);
  const tLinear = (v) => (thi === tlo ? 0.5 : Math.max(0, Math.min(1, (tf(Math.max(v, log ? lo : -Infinity)) - tlo) / (thi - tlo))));

  if (style.classes === 'continuous' || sorted.length < 4) {
    const ticks = log ? d3.scaleLog().domain([lo, hi]).ticks(4).filter((x) => x >= lo && x <= hi) : d3.ticks(lo, hi, 4);
    return {
      color: (v) => (Number.isFinite(v) ? pal(tLinear(v)) : null), t: tLinear, log, domain: [lo, hi],
      legend: { type: 'continuous', lo, hi, ticks: ticks.map((v) => ({ v, t: tLinear(v) })), gradient: d3.range(12).map((i) => pal(i / 11)) },
    };
  }
  const n = Math.max(3, Math.min(9, style.n || 6));
  let breaks;
  if (style.classes === 'equal') {
    breaks = d3.range(n + 1).map((i) => (log ? 10 ** (tlo + ((thi - tlo) * i) / n) : lo + ((hi - lo) * i) / n));
  } else if (style.classes === 'jenks' && sorted.length <= 4000) {
    breaks = jenks(sorted, n);
  } else {
    breaks = d3.range(n + 1).map((i) => quantile(sorted, i / n));
  }
  breaks = breaks.filter((b, i) => i === 0 || b > breaks[i - 1]);
  const k = breaks.length - 1;
  const colors = d3.range(k).map((i) => pal(k === 1 ? 0.5 : i / (k - 1)));
  const cls = (v) => {
    for (let i = 1; i < breaks.length; i++) if (v <= breaks[i]) return i - 1;
    return k - 1;
  };
  return {
    color: (v) => (Number.isFinite(v) ? colors[cls(v)] : null), t: tLinear, log, domain: [lo, hi], classOf: cls,
    legend: { type: 'classes', breaks, colors },
  };
}

// 3x3 bivariate palette (Joshua Stevens-style), colored by two terciles.
export const BIVARIATE = [
  ['#e8e8e8', '#e4acac', '#c85a5a'],
  ['#b0d5df', '#ad9ea5', '#985356'],
  ['#64acbe', '#627f8c', '#574249'],
];
export const BIVARIATE_VIVID = [
  ['#f3f3f3', '#f3b0d8', '#e8459a'],
  ['#a8ddf0', '#b19ad0', '#ab3c95'],
  ['#2fb5e8', '#4b7fc4', '#5a2d91'],
];

export function bivariateScale(xs, ys) {
  const sx = xs.filter(Number.isFinite).sort((a, b) => a - b);
  const sy = ys.filter(Number.isFinite).sort((a, b) => a - b);
  const bx = [quantile(sx, 1 / 3), quantile(sx, 2 / 3)];
  const by = [quantile(sy, 1 / 3), quantile(sy, 2 / 3)];
  const ter = (v, b) => (v <= b[0] ? 0 : v <= b[1] ? 1 : 2);
  return { color: (x, y) => (Number.isFinite(x) && Number.isFinite(y) ? BIVARIATE_VIVID[ter(y, by)][ter(x, bx)] : null), bx, by };
}
