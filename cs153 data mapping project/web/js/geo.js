// Boundaries + gazetteer: turns messy place names/codes into Natural Earth keys.
import { api } from './util.js';

const cache = {};
export function loadGeo(kind) {
  // kind: 'countries' (detailed), 'countries-lo' (globe), 'admin1', 'admin1-index'
  const url = {
    countries: '/api/geo/countries',
    'countries-lo': '/api/geo/countries?res=lo',
    admin1: '/api/geo/admin1',
    'admin1-index': '/api/geo/admin1-index',
  }[kind];
  if (!cache[kind]) cache[kind] = api(url).catch((e) => { delete cache[kind]; throw e; });
  return cache[kind];
}

export function norm(s) {
  return String(s ?? '')
    .normalize('NFD').replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/\[[^\]]*\]/g, ' ')
    .replace(/[*†‡§¶#]+/g, ' ')
    .replace(/&/g, ' and ')
    .replace(/\bst\.?\s/g, 'saint ')
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\bthe\b/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// Names used by the World Bank, UN, OWID, Wikipedia and colloquially, mapped to ISO3.
const COUNTRY_ALIASES = {
  COD: ['congo dem rep', 'democratic republic of congo', 'dr congo', 'drc', 'congo kinshasa', 'dem rep congo', 'congo democratic republic', 'zaire'],
  COG: ['congo rep', 'republic of congo', 'congo brazzaville', 'congo', 'congo republic'],
  CIV: ['cote d ivoire', 'cote divoire', 'ivory coast'],
  KOR: ['korea rep', 'south korea', 'republic of korea', 'korea south', 'korea'],
  PRK: ['korea dem people s rep', 'korea dem rep', 'north korea', 'dprk', 'korea north', 'democratic people s republic of korea'],
  RUS: ['russian federation', 'russia'],
  VNM: ['viet nam', 'vietnam'],
  LAO: ['lao pdr', 'laos', 'lao people s democratic republic'],
  IRN: ['iran islamic rep', 'iran', 'islamic republic of iran'],
  EGY: ['egypt arab rep', 'egypt'],
  YEM: ['yemen rep', 'yemen'],
  VEN: ['venezuela rb', 'venezuela', 'bolivarian republic of venezuela'],
  GMB: ['gambia'],
  BHS: ['bahamas'],
  FSM: ['micronesia fed sts', 'micronesia', 'federated states of micronesia'],
  SYR: ['syrian arab republic', 'syria'],
  KGZ: ['kyrgyz republic', 'kyrgyzstan'],
  SVK: ['slovak republic', 'slovakia'],
  TUR: ['turkiye', 'turkey', 'republic of turkiye'],
  CZE: ['czechia', 'czech republic'],
  SWZ: ['eswatini', 'swaziland'],
  CPV: ['cabo verde', 'cape verde'],
  TLS: ['timor leste', 'east timor'],
  MMR: ['myanmar', 'burma'],
  BRN: ['brunei darussalam', 'brunei'],
  PSE: ['west bank and gaza', 'palestine', 'state of palestine', 'palestinian territories', 'palestinian territory', 'gaza strip'],
  HKG: ['hong kong sar china', 'hong kong'],
  MAC: ['macao sar china', 'macau', 'macao'],
  MKD: ['north macedonia', 'macedonia', 'fyr macedonia'],
  USA: ['united states', 'usa', 'us', 'u s', 'u s a', 'united states of america', 'america'],
  GBR: ['uk', 'u k', 'united kingdom', 'great britain', 'britain', 'england and wales'],
  ARE: ['uae', 'united arab emirates'],
  LCA: ['saint lucia'],
  KNA: ['saint kitts and nevis'],
  VCT: ['saint vincent and grenadines', 'saint vincent'],
  STP: ['sao tome and principe'],
  XKX: ['kosovo'],
  BIH: ['bosnia', 'bosnia and herzegovina', 'bosnia herzegovina'],
  CUW: ['curacao'],
  TWN: ['taiwan', 'taiwan province of china', 'chinese taipei', 'republic of china'],
  TZA: ['tanzania', 'united republic of tanzania'],
  BOL: ['bolivia', 'plurinational state of bolivia'],
  MDA: ['moldova', 'republic of moldova'],
  VAT: ['vatican', 'holy see', 'vatican city'],
  MNE: ['montenegro'],
  SRB: ['serbia'],
  NLD: ['netherlands', 'holland', 'kingdom of netherlands'],
  CHN: ['china', 'people s republic of china', 'prc', 'mainland china'],
  IND: ['india', 'republic of india'],
  ATG: ['antigua and barbuda', 'antigua'],
  TTO: ['trinidad and tobago', 'trinidad'],
  GNB: ['guinea bissau'],
  GNQ: ['equatorial guinea'],
  CAF: ['central african republic', 'car'],
  DOM: ['dominican republic'],
  SSD: ['south sudan'],
  SDN: ['sudan'],
  PNG: ['papua new guinea'],
  SLB: ['solomon islands'],
  MHL: ['marshall islands'],
  NZL: ['new zealand'],
  ESH: ['western sahara'],
  FLK: ['falkland islands', 'falkland islands malvinas'],
  SOL: ['somaliland'],
  CYN: ['northern cyprus'],
};

let countryIndex = null;
export async function countryGazetteer() {
  if (countryIndex) return countryIndex;
  const geo = await loadGeo('countries');
  const idx = new Map();
  const info = new Map();
  const add = (k, iso) => { const n = norm(k); if (n && !idx.has(n)) idx.set(n, iso); };
  for (const f of geo.features) {
    const p = f.properties;
    info.set(p.iso3, p);
    idx.set(norm(p.iso3), p.iso3);
  }
  for (const [iso, names] of Object.entries(COUNTRY_ALIASES)) names.forEach((n) => idx.set(norm(n), iso));
  for (const f of geo.features) {
    const p = f.properties;
    if (p.iso2) add(p.iso2, p.iso3);
    add(p.name, p.iso3);
    (p.aliases || []).forEach((a) => add(a, p.iso3));
    (p.codes || []).forEach((c) => add(c, p.iso3));
  }
  countryIndex = { idx, info };
  return countryIndex;
}

export function matchCountry(raw, gaz) {
  if (raw == null) return null;
  const s = String(raw).trim();
  if (!s) return null;
  const n = norm(s);
  if (gaz.idx.has(n)) return gaz.idx.get(n);
  const noParen = norm(s.replace(/\([^)]*\)/g, ' '));
  if (noParen && gaz.idx.has(noParen)) return gaz.idx.get(noParen);
  const comma = s.split(',');
  if (comma.length === 2) {
    const flipped = norm(`${comma[1]} ${comma[0]}`);
    if (gaz.idx.has(flipped)) return gaz.idx.get(flipped);
  }
  const noRepublic = n.replace(/^(republic of|kingdom of|state of|commonwealth of) /, '');
  if (gaz.idx.has(noRepublic)) return gaz.idx.get(noRepublic);
  return null;
}

let admin1Index = null;
const ADMIN_SUFFIX = /\s(state|province|region|oblast|prefecture|governorate|department|district|territory|county|municipality|autonomous region|division|krai|okrug|voivodeship|canton|emirate|parish)$/;
const ADMIN_PREFIX = /^(state of|province of|provincia de|provincia del|departamento de|estado de|region of|regione|nct of|national capital territory of) /;

export async function admin1Gazetteer() {
  if (admin1Index) return admin1Index;
  const rows = await loadGeo('admin1-index');
  const byCode = new Map();
  const byName = new Map(); // norm name -> [code]
  const byPostal = new Map(); // ISO3|postal -> code
  const info = new Map();
  const push = (m, k, code) => {
    if (!k) return;
    const arr = m.get(k) || [];
    if (!arr.includes(code)) arr.push(code);
    m.set(k, arr);
  };
  for (const r of rows) {
    info.set(r.code, r);
    byCode.set(r.code.toUpperCase(), r.code);
    if (r.hasc) byCode.set(r.hasc.toUpperCase().replace('.', '-'), r.code);
    for (const n of r.names) {
      const k = norm(n);
      push(byName, k, r.code);
      push(byName, k.replace(ADMIN_SUFFIX, '').replace(ADMIN_PREFIX, ''), r.code);
    }
    if (r.postal) byPostal.set(`${r.iso3}|${r.postal.toUpperCase()}`, r.code);
  }
  // Common English names Natural Earth spells differently.
  const extra = { 'US-DC': ['washington dc', 'washington d c', 'dc', 'district of columbia'], 'IN-DL': ['delhi', 'nct delhi', 'new delhi'],
    'IN-OR': ['odisha', 'orissa'], 'IN-UT': ['uttarakhand', 'uttaranchal'], 'IN-PY': ['puducherry', 'pondicherry'] };
  for (const [code, names] of Object.entries(extra)) if (info.has(code)) names.forEach((n) => push(byName, n, code));
  admin1Index = { byCode, byName, byPostal, info };
  return admin1Index;
}

// Match a column of raw values to admin-1 codes, using an optional country per row
// and otherwise the dominant country to break ties ("Punjab", "Georgia", "CA").
export function matchAdmin1Column(values, gaz, countryHints = null) {
  const cands = values.map((raw, i) => {
    if (raw == null || String(raw).trim() === '') return [];
    const s = String(raw).trim();
    const up = s.toUpperCase().replace(/[._ ]/, '-');
    if (gaz.byCode.has(up)) return [gaz.byCode.get(up)];
    const n = norm(s);
    let c = gaz.byName.get(n) || gaz.byName.get(n.replace(ADMIN_SUFFIX, '').replace(ADMIN_PREFIX, '')) || gaz.byName.get(norm(s.replace(/\([^)]*\)/g, ''))) || [];
    const hint = countryHints && countryHints[i];
    if (hint && c.length) c = c.filter((code) => gaz.info.get(code)?.iso3 === hint);
    return c;
  });
  const counts = new Map();
  for (const c of cands) if (c.length === 1) { const iso = gaz.info.get(c[0]).iso3; counts.set(iso, (counts.get(iso) || 0) + 1); }
  for (const c of cands) if (c.length > 1) c.forEach((code) => { const iso = gaz.info.get(code).iso3; counts.set(iso, (counts.get(iso) || 0) + 0.25); });
  const dominant = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  return values.map((raw, i) => {
    const c = cands[i];
    if (c.length === 1) return c[0];
    if (c.length > 1) return c.find((code) => gaz.info.get(code).iso3 === dominant) || c[0];
    // Postal abbreviations (CA, TX, ON) only make sense once we know the country.
    const s = String(raw ?? '').trim().toUpperCase();
    const iso = (countryHints && countryHints[i]) || dominant;
    if (iso && /^[A-Z]{1,3}$/.test(s) && gaz.byPostal.has(`${iso}|${s}`)) return gaz.byPostal.get(`${iso}|${s}`);
    return null;
  });
}

export function geoLabel(geo) {
  return { countries: 'Countries', admin1: 'States & provinces', points: 'Points (lat/lon)' }[geo] || geo;
}
