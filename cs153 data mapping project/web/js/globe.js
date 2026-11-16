// 3D globe (globe.gl): extruded choropleth, pulse rings on priority regions, point "spikes".
import { loadGeo } from './geo.js';
import { esc } from './util.js';

const TEXTURES = {
  midnight: null,
  night: 'https://unpkg.com/three-globe@2.31.0/example/img/earth-night.jpg',
  marble: 'https://unpkg.com/three-globe@2.31.0/example/img/earth-blue-marble.jpg',
  topo: 'https://unpkg.com/three-globe@2.31.0/example/img/earth-topology.png',
};

// On a sphere a ring's winding decides inside vs outside; a ring that claims more than a
// hemisphere is really a small polygon wound the wrong way, so flip it (otherwise it paints the globe).
function fixWinding(f) {
  const g = f.geometry;
  if (!g) return f;
  const parts = g.type === 'Polygon' ? [g.coordinates] : g.type === 'MultiPolygon' ? g.coordinates : [];
  const fixed = parts.map((rings) => {
    const area = d3.geoArea({ type: 'Polygon', coordinates: rings });
    return area > 2 * Math.PI ? rings.map((r) => r.slice().reverse()) : rings;
  });
  return { ...f, geometry: g.type === 'Polygon' ? { type: 'Polygon', coordinates: fixed[0] } : { type: 'MultiPolygon', coordinates: fixed } };
}

export class GlobeView {
  constructor(el, { onHover, onClick, labelFor, theme }) {
    this.el = el;
    this.onHover = onHover;
    this.onClick = onClick;
    this.labelFor = labelFor;
    this.theme = theme;
    this.colors = new Map();
    this.hovered = null;
    this.selected = null;
    this.extrude = true;
    this.heightScale = 1;
    this.geo = 'countries';
    this.ready = this._init();
  }

  async _init() {
    const g = Globe({ rendererConfig: { preserveDrawingBuffer: true, antialias: true, alpha: true } })(this.el);
    this.g = g;
    g.backgroundColor('rgba(0,0,0,0)')
      .showAtmosphere(true).atmosphereColor('#6ea8ff').atmosphereAltitude(0.2)
      .showGraticules(true)
      .polygonCapCurvatureResolution(3)
      .polygonsTransitionDuration(700)
      .polygonSideColor((f) => this._side(f))
      .polygonStrokeColor((f) => (this._key(f) === this.selected ? '#ffffff' : this.theme === 'dark' ? 'rgba(170,190,255,0.35)' : 'rgba(30,40,70,0.35)'))
      .polygonCapColor((f) => this._cap(f))
      .polygonAltitude((f) => this._alt(f))
      .polygonLabel((f) => this.labelFor({ key: this._key(f), geo: f.__geo, props: f.properties }))
      .onPolygonHover((f) => {
        this.hovered = f ? this._key(f) : null;
        this._refresh();
        this.el.style.cursor = f ? 'pointer' : '';
      })
      .onPolygonClick((f) => f && this.onClick({ key: this._key(f), geo: f.__geo }))
      .ringColor(() => (t) => `rgba(255, 79, 163, ${1 - t})`)
      .ringMaxRadius(3.2).ringPropagationSpeed(2.2).ringRepeatPeriod(1300).ringAltitude(0.012)
      .pointAltitude((p) => p.alt).pointColor((p) => p.color).pointRadius((p) => p.rad).pointsMerge(false).pointResolution(8)
      .pointLabel((p) => `<div class="tip"><div class="tip-title">${esc(p.label || 'Point')}</div><div class="tip-value">${esc(p.display ?? '')}</div></div>`)
      .pointsTransitionDuration(600);
    const mat = g.globeMaterial();
    mat.color.set('#070c1d');
    mat.emissive?.set('#03060f');
    mat.shininess = 8;
    g.pointOfView({ lat: 18, lng: 15, altitude: 2.35 }, 0);
    const controls = g.controls();
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.35;
    controls.enableDamping = true;
    controls.minDistance = 130;
    controls.maxDistance = 700;
    this.el.addEventListener('pointerdown', () => (this.interacted = true));
    const [countries] = await Promise.all([loadGeo('countries-lo')]);
    this.countries = countries.features.filter((f) => !f.properties.antarctica).map((f) => ({ ...fixWinding(f), __geo: 'countries' }));
    g.polygonsData(this.countries);
    this.resize();
  }

  // Fresh accessor functions force globe.gl to re-evaluate every polygon.
  _refresh() {
    this.g.polygonCapColor((f) => this._cap(f)).polygonSideColor((f) => this._side(f)).polygonAltitude((f) => this._alt(f))
      .polygonStrokeColor((f) => (this._key(f) === this.selected ? '#ffffff' : this.theme === 'dark' ? 'rgba(170,190,255,0.35)' : 'rgba(30,40,70,0.35)'));
  }

  _key(f) {
    return f.__geo === 'admin1' ? f.properties.code : f.properties.iso3;
  }

  _cap(f) {
    const d = this.colors.get(this._key(f));
    const base = this.theme === 'dark' ? 'rgba(36,44,70,0.95)' : 'rgba(225,222,212,0.95)';
    if (f.__geo !== this.geo) return base;
    if (!d) return f.__geo === 'admin1' ? 'rgba(0,0,0,0)' : base;
    if (d.dim) return 'rgba(80,90,120,0.35)';
    return this._key(f) === this.hovered ? d3.color(d.color).brighter(0.6).formatRgb() : d.color;
  }

  _side(f) {
    const d = this.colors.get(this._key(f));
    if (!d || f.__geo !== this.geo) return 'rgba(20,30,60,0.3)';
    const c = d3.color(d.color).darker(0.8);
    c.opacity = 0.92;
    return c.formatRgb();
  }

  _alt(f) {
    const k = this._key(f);
    const d = this.colors.get(k);
    const lift = k === this.hovered ? 0.02 : 0;
    if (f.__geo === 'admin1') return 0.0075 + (d && this.extrude ? 0.004 + (d.h ?? d.t) * 0.13 * this.heightScale : 0) + lift;
    if (!d || f.__geo !== this.geo) return 0.006;
    return (this.extrude ? 0.008 + (d.h ?? d.t) * 0.17 * this.heightScale : 0.008) + lift;
  }

  async render(spec) {
    await this.ready;
    const g = this.g;
    this.extrude = spec.extrude;
    this.heightScale = spec.heightScale || 1;
    this.colors = spec.colors || new Map();
    if (spec.geo === 'admin1' && spec.colors?.size) {
      const admin1 = await loadGeo('admin1');
      const countries = new Set([...spec.colors.keys()].map((k) => spec.countryOf(k)).filter(Boolean));
      const key = [...countries].sort().join(',');
      if (key !== this.admin1Key) {
        this.admin1Key = key;
        const subs = admin1.features.filter((f) => countries.has(f.properties.iso3)).map((f) => ({ ...fixWinding(f), __geo: 'admin1' }));
        g.polygonsData([...this.countries.filter((f) => !countries.has(f.properties.iso3)), ...subs]);
      }
    } else if (this.admin1Key !== undefined || this.geo !== spec.geo) {
      this.admin1Key = undefined;
      g.polygonsData(this.countries);
    }
    this.geo = spec.geo;
    this._refresh();
    g.ringsData(spec.rings || []);
    g.pointsData((spec.points || []).map((p) => ({ ...p, alt: 0.01 + (p.t ?? 0.3) * 0.3 * this.heightScale, rad: 0.28 })));
  }

  setTheme(theme) {
    this.theme = theme;
    if (!this.g) return;
    this.g.atmosphereColor(theme === 'dark' ? '#6ea8ff' : '#7aa7e0');
    if (!this.texture) this.g.globeMaterial().color.set(theme === 'dark' ? '#070c1d' : '#b9d3ee');
    this._refresh();
  }

  setTexture(name) {
    this.texture = TEXTURES[name] || null;
    this.g.globeImageUrl(this.texture);
    this.g.globeMaterial().color.set(this.texture ? '#ffffff' : this.theme === 'dark' ? '#070c1d' : '#b9d3ee');
  }

  setAutoRotate(on) {
    if (this.g) this.g.controls().autoRotate = on;
  }

  setSelected(sel) {
    this.selected = sel?.key ?? null;
    if (this.g) this._refresh();
  }

  flyTo(lon, lat, altitude = 1.5) {
    this.g.pointOfView({ lat, lng: lon, altitude }, 1400);
  }

  resetView() {
    this.g.pointOfView({ lat: 18, lng: 15, altitude: 2.35 }, 1200);
  }

  resize() {
    if (!this.g) return;
    this.g.width(this.el.clientWidth).height(this.el.clientHeight);
  }

  canvas() {
    return this.g.renderer().domElement;
  }

  pause(on) {
    if (!this.g) return;
    on ? this.g.pauseAnimation() : this.g.resumeAnimation();
  }
}
