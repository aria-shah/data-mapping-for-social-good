// Flat map (MapLibre): choropleth, 3D extruded prisms, points/heatmap, raster basemaps.
import { loadGeo } from './geo.js';

const BASEMAPS = {
  none: null,
  dark: { tiles: ['a', 'b', 'c'].map((s) => `https://${s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png`), attribution: '© OpenStreetMap © CARTO' },
  light: { tiles: ['a', 'b', 'c'].map((s) => `https://${s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}.png`), attribution: '© OpenStreetMap © CARTO' },
  satellite: { tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'], attribution: 'Imagery © Esri, Maxar, Earthstar Geographics' },
};

function graticule(step = 20) {
  const features = [];
  for (let lon = -180; lon <= 180; lon += step) features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: d3.range(-85, 86, 5).map((lat) => [lon, lat]) } });
  for (let lat = -80; lat <= 80; lat += step) features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: d3.range(-180, 181, 5).map((lon) => [lon, lat]) } });
  return { type: 'FeatureCollection', features };
}

export class FlatMap {
  constructor(el, { onHover, onClick, theme }) {
    this.el = el;
    this.onHover = onHover;
    this.onClick = onClick;
    this.theme = theme;
    this.geo = 'countries';
    this.states = { countries: new Set(), admin1: new Set() };
    this.hovered = null;
    this.tilt = false;
    this.ready = this._init();
  }

  colors() {
    const dark = this.theme === 'dark';
    return {
      ocean: dark ? '#060b1a' : '#dfe9f3', land: dark ? '#1a2238' : '#f4f1ea', nodata: dark ? '#262e47' : '#e4e1d9',
      border: dark ? 'rgba(160,180,255,0.22)' : 'rgba(40,50,80,0.28)', grat: dark ? 'rgba(120,150,255,0.08)' : 'rgba(30,60,120,0.08)',
    };
  }

  async _init() {
    const c = this.colors();
    this.map = new maplibregl.Map({
      container: this.el,
      style: { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': c.ocean } }] },
      center: [12, 22], zoom: 1.35, minZoom: 0.6, maxZoom: 9, attributionControl: false,
      preserveDrawingBuffer: true, dragRotate: true, renderWorldCopies: false,
    });
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right');
    this.map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: 'Boundaries: Natural Earth' }), 'bottom-right');
    await new Promise((r) => this.map.on('load', r));
    const countries = await loadGeo('countries');
    const m = this.map;
    m.addSource('graticule', { type: 'geojson', data: graticule() });
    m.addLayer({ id: 'graticule', type: 'line', source: 'graticule', paint: { 'line-color': c.grat, 'line-width': 1 } });
    m.addSource('countries', { type: 'geojson', data: countries, promoteId: 'iso3' });
    m.addLayer({ id: 'land', type: 'fill', source: 'countries', paint: { 'fill-color': c.land, 'fill-opacity': 1 } });
    this._addChoroLayers('countries');
    m.addSource('points', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    m.addLayer({ id: 'points-heat', type: 'heatmap', source: 'points', layout: { visibility: 'none' }, paint: {
      'heatmap-weight': ['coalesce', ['get', 't'], 0.5], 'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 0, 0.8, 8, 3],
      'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 0, 12, 8, 40], 'heatmap-opacity': 0.85,
      'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'], 0, 'rgba(0,0,0,0)', 0.15, '#3d2a8a', 0.35, '#b5367a', 0.6, '#ea5f5a', 0.8, '#f9a03f', 1, '#fde68a'],
    } });
    m.addLayer({ id: 'points', type: 'circle', source: 'points', paint: {
      'circle-color': ['get', 'color'], 'circle-radius': ['interpolate', ['linear'], ['zoom'], 0, ['*', ['get', 'r'], 0.8], 6, ['*', ['get', 'r'], 2.2]],
      'circle-opacity': 0.85, 'circle-stroke-color': 'rgba(255,255,255,0.75)', 'circle-stroke-width': 0.7,
    } });
    this._wireEvents('countries');
    this._wireEvents('points');
  }

  _addChoroLayers(src) {
    const m = this.map;
    const c = this.colors();
    const before = m.getLayer('points-heat') ? 'points-heat' : undefined;
    m.addLayer({ id: `${src}-fill`, type: 'fill', source: src, paint: {
      'fill-color': ['coalesce', ['feature-state', 'color'], c.nodata],
      'fill-opacity': ['case', ['boolean', ['feature-state', 'dim'], false], 0.18, ['boolean', ['feature-state', 'has'], false], ['coalesce', ['feature-state', 'op'], 0.9], src === 'admin1' ? 0 : 0.85],
    } }, before);
    m.addLayer({ id: `${src}-ext`, type: 'fill-extrusion', source: src, layout: { visibility: 'none' }, paint: {
      'fill-extrusion-color': ['coalesce', ['feature-state', 'color'], c.nodata],
      'fill-extrusion-height': ['coalesce', ['feature-state', 'h'], 0],
      'fill-extrusion-opacity': 0.94, 'fill-extrusion-vertical-gradient': true,
    } }, before);
    m.addLayer({ id: `${src}-line`, type: 'line', source: src, paint: {
      'line-color': ['case', ['boolean', ['feature-state', 'priority'], false], '#ff4fa3', c.border],
      'line-width': ['case', ['boolean', ['feature-state', 'priority'], false], 2.2, src === 'admin1' ? ['case', ['boolean', ['feature-state', 'has'], false], 0.5, 0] : 0.6],
    } }, before);
    m.addLayer({ id: `${src}-hover`, type: 'line', source: src, paint: {
      'line-color': this.theme === 'dark' ? '#ffffff' : '#111827',
      'line-width': ['case', ['boolean', ['feature-state', 'hover'], false], 2.4, ['boolean', ['feature-state', 'selected'], false], 3, 0],
    } }, before);
  }

  _wireEvents(src) {
    const m = this.map;
    const layers = src === 'points' ? ['points'] : [`${src}-fill`, `${src}-ext`];
    for (const layer of layers) {
      m.on('mousemove', layer, (e) => {
        const f = e.features[0];
        if (!f) return;
        m.getCanvas().style.cursor = 'pointer';
        if (src !== 'points') this._setHover(src, f.id);
        this.onHover(src === 'points' ? { point: f.properties } : { key: f.id, geo: src, props: f.properties }, e.originalEvent);
      });
      m.on('mouseleave', layer, () => {
        m.getCanvas().style.cursor = '';
        this._setHover(null, null);
        this.onHover(null);
      });
      m.on('click', layer, (e) => {
        const f = e.features[0];
        if (f && src !== 'points') this.onClick({ key: f.id, geo: src });
      });
    }
  }

  _setHover(src, id) {
    if (this.hovered) this.map.setFeatureState(this.hovered, { hover: false });
    this.hovered = src && id != null ? { source: src, id } : null;
    if (this.hovered) this.map.setFeatureState(this.hovered, { hover: true });
  }

  async ensureAdmin1() {
    if (this.map.getSource('admin1')) return;
    const data = await loadGeo('admin1');
    if (this.map.getSource('admin1')) return;
    this.map.addSource('admin1', { type: 'geojson', data, promoteId: 'code' });
    this._addChoroLayers('admin1');
    this._wireEvents('admin1');
  }

  setTheme(theme) {
    this.theme = theme;
    if (!this.map?.getLayer('land')) return;
    const c = this.colors();
    this.map.setPaintProperty('bg', 'background-color', c.ocean);
    this.map.setPaintProperty('land', 'fill-color', c.land);
    this.map.setPaintProperty('graticule', 'line-color', c.grat);
    for (const src of ['countries', 'admin1']) {
      if (!this.map.getLayer(`${src}-line`)) continue;
      this.map.setPaintProperty(`${src}-fill`, 'fill-color', ['coalesce', ['feature-state', 'color'], c.nodata]);
      this.map.setPaintProperty(`${src}-ext`, 'fill-extrusion-color', ['coalesce', ['feature-state', 'color'], c.nodata]);
      this.map.setPaintProperty(`${src}-line`, 'line-color', ['case', ['boolean', ['feature-state', 'priority'], false], '#ff4fa3', c.border]);
      this.map.setPaintProperty(`${src}-hover`, 'line-color', theme === 'dark' ? '#ffffff' : '#111827');
    }
  }

  setBasemap(name) {
    const m = this.map;
    if (m.getLayer('basemap')) m.removeLayer('basemap');
    if (m.getSource('basemap')) m.removeSource('basemap');
    const b = BASEMAPS[name];
    if (b) {
      m.addSource('basemap', { type: 'raster', tiles: b.tiles, tileSize: 256, attribution: b.attribution, maxzoom: 18 });
      m.addLayer({ id: 'basemap', type: 'raster', source: 'basemap', paint: { 'raster-opacity': 1 } }, 'graticule');
    }
    m.setPaintProperty('land', 'fill-opacity', b ? 0 : 1);
    this.basemap = name;
  }

  // render({geo, colors: Map(key -> {color, t, priority}), points, tilt, heightScale, pointStyle, opacity})
  async render(spec) {
    await this.ready;
    const m = this.map;
    if (spec.geo === 'admin1') await this.ensureAdmin1();
    this.geo = spec.geo;
    for (const src of ['countries', 'admin1']) {
      if (!m.getSource(src)) continue;
      const active = spec.geo === src && spec.colors;
      const prev = this.states[src];
      const next = new Set(active ? spec.colors.keys() : []);
      for (const id of prev) if (!next.has(id)) m.setFeatureState({ source: src, id }, { color: null, has: false, h: 0, priority: false, dim: false });
      if (active) {
        const scale = (src === 'admin1' ? 600000 : 1500000) * (spec.heightScale || 1);
        for (const [id, d] of spec.colors) {
          m.setFeatureState({ source: src, id }, { color: d.color, has: true, op: spec.opacity ?? 0.9, h: spec.tilt ? 20000 + (d.h ?? d.t) * scale : 0, priority: !!d.priority, dim: !!d.dim });
        }
      }
      this.states[src] = next;
      const vis = (id, on) => m.getLayer(id) && m.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
      vis(`${src}-ext`, spec.tilt && spec.geo === src);
      vis(`${src}-fill`, !(spec.tilt && spec.geo === src));
    }
    const pts = spec.points || [];
    m.getSource('points').setData({ type: 'FeatureCollection', features: pts.map((p, i) => ({
      type: 'Feature', id: i, geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
      properties: { color: p.color, r: p.r, t: p.t, label: p.label, v: p.v, layer: p.layer },
    })) });
    m.setLayoutProperty('points', 'visibility', spec.pointStyle === 'heat' ? 'none' : 'visible');
    m.setLayoutProperty('points-heat', 'visibility', spec.pointStyle === 'heat' || spec.pointStyle === 'both' ? 'visible' : 'none');
    if (spec.pointStyle === 'both') m.setLayoutProperty('points', 'visibility', 'visible');
    if (spec.tilt !== this.tilt) {
      this.tilt = spec.tilt;
      m.easeTo({ pitch: spec.tilt ? 52 : 0, bearing: spec.tilt ? -12 : 0, duration: 1200 });
    }
  }

  setSelected(sel) {
    if (this.selected) this.map.setFeatureState(this.selected, { selected: false });
    this.selected = sel && this.map.getSource(sel.geo) ? { source: sel.geo, id: sel.key } : null;
    if (this.selected) this.map.setFeatureState(this.selected, { selected: true });
  }

  flyTo(lon, lat, zoom = 3.2) {
    this.map.flyTo({ center: [lon, lat], zoom, duration: 1400, essential: true });
  }

  fitKeys(geoData, keys, idProp) {
    const set = new Set(keys);
    let b = null;
    for (const f of geoData.features) {
      if (!set.has(f.properties[idProp])) continue;
      const walk = (c) => (typeof c[0] === 'number' ? (b = b ? [Math.min(b[0], c[0]), Math.min(b[1], c[1]), Math.max(b[2], c[0]), Math.max(b[3], c[1])] : [c[0], c[1], c[0], c[1]]) : c.forEach(walk));
      walk(f.geometry.coordinates);
    }
    if (b) this.map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 80, duration: 1300, maxZoom: 5.5 });
  }

  _fitZoom() {
    const w = this.el.clientWidth - (this.padding?.left || 0) - (this.padding?.right || 0);
    return Math.max(0.6, Math.log2(Math.max(w, 300) / 512) + 0.1);
  }

  resetView() {
    this.map.flyTo({ center: [12, 22], zoom: this._fitZoom(), pitch: this.tilt ? 52 : 0, bearing: this.tilt ? -12 : 0, duration: 1200 });
  }

  resize() {
    this.map?.resize();
  }

  // Keep the visual center between the floating side panels.
  setPadding(p) {
    this.padding = p;
    this.map?.easeTo({ padding: p, duration: 300 });
  }

  canvas() {
    return this.map.getCanvas();
  }
}
