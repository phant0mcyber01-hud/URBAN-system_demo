// Loads the static map layers once and prepares everything the renderers need.
// The scene is a disc around the centre; `vis` is 1 inside and fades smoothly to 0 at the rim.
import { BG, BUILDING, BUILDING_DEFAULT, FADE, GROUND, MAJOR } from '../config.js';

export const getJson = (url) => fetch(url).then((r) => (r.ok ? r.json() : null)).catch(() => null);
export const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
export const toward = (color, base, v) => mix(base, color, v); // v=0 -> base, v=1 -> color

const feats = (fc) => fc?.features ?? [];
const hash01 = (n) => { const x = Math.sin(n * 127.1) * 43758.5453; return x - Math.floor(x); };
const centroid = (ring) => {
  let x = 0, y = 0;
  for (const p of ring) { x += p[0]; y += p[1]; }
  return [x / ring.length, y / ring.length];
};
function inside(p, ring) {
  let hit = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > p[1]) !== (yj > p[1]) && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

export async function loadScene() {
  const files = ['meta.json', 'roads.geojson', 'buildings.geojson', 'stops.geojson', 'areas.geojson',
    'lines.geojson', 'points.geojson', 'cameras.geojson', 'pois.geojson', 'signals.json'];
  const [meta, roadsFc, buildingsFc, stopsFc, areasFc, linesFc, pointsFc, camsFc, poisFc, signals] =
    await Promise.all(files.map((f) => getJson(`/data/${f}`)));

  const C = meta?.center ?? [71.785, 40.385];
  const R = meta?.radius ?? 1200;
  const mLat = 111320;
  const mLon = mLat * Math.cos((C[1] * Math.PI) / 180);
  const toM = (p) => [(p[0] - C[0]) * mLon, (p[1] - C[1]) * mLat];
  const toLL = (x, y) => [C[0] + x / mLon, C[1] + y / mLat];
  const visD = (d) => { const t = Math.min(1, Math.max(0, (R - d) / (R * FADE))); return t * t * (3 - 2 * t); };
  const visM = (x, y) => visD(Math.hypot(x, y));
  const vis = (p) => visD(Math.hypot((p[0] - C[0]) * mLon, (p[1] - C[1]) * mLat));
  const distM = (a, b) => Math.hypot((a[0] - b[0]) * mLon, (a[1] - b[1]) * mLat);
  const circle = (radius, n = 96) => Array.from({ length: n + 1 }, (_, i) => {
    const a = (i / n) * 2 * Math.PI;
    return toLL(Math.cos(a) * radius, Math.sin(a) * radius);
  });

  const line = (f) => {
    const path = f.geometry.coordinates;
    const v = path.map(vis);
    return { path, v, vmax: Math.max(...v), ...f.properties };
  };
  const roads = feats(roadsFc).map(line).filter((r) => r.vmax > 0.01);
  const minor = feats(linesFc).map(line).filter((r) => r.vmax > 0.01);

  const buildings = feats(buildingsFc).map((f, i) => {
    const ring = f.geometry.coordinates[0];
    const c = centroid(ring);
    const v = vis(c);
    const tint = 0.86 + 0.28 * hash01(i + 1);
    const base = BUILDING[f.properties.kind] ?? BUILDING_DEFAULT;
    return { ...f.properties, polygon: ring, c, v, h: f.properties.h * v, h0: f.properties.h, color: toward(base.map((x) => x * tint), GROUND, v) };
  }).filter((b) => b.v > 0.02);

  const areas = feats(areasFc).map((f) => {
    const ring = f.geometry.coordinates[0];
    return { polygon: ring, v: vis(centroid(ring)), ...f.properties };
  }).filter((a) => a.v > 0.02);

  // decorative trees scattered inside green areas (OSM has almost no mapped trees here)
  const trees = [];
  areas.filter((a) => a.kind === 'green').forEach((a, ai) => {
    const xs = a.polygon.map((p) => p[0]), ys = a.polygon.map((p) => p[1]);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const n = Math.min(400, Math.round(((x1 - x0) * mLon * (y1 - y0) * mLat) / 260));
    for (let k = 0; k < n; k++) {
      const p = [x0 + (x1 - x0) * hash01(ai * 977 + k * 2 + 1), y0 + (y1 - y0) * hash01(ai * 977 + k * 2 + 2)];
      const v = vis(p);
      if (v > 0.05 && inside(p, a.polygon)) trees.push({ p, v, s: 0.7 + 0.6 * hash01(ai * 31 + k) });
    }
  });

  const point = (f) => ({ p: f.geometry.coordinates, v: vis(f.geometry.coordinates), ...f.properties });
  const visible = (d) => d.v > 0.05;
  const points = feats(pointsFc).map(point).filter(visible);
  for (const t of points.filter((d) => d.kind === 'tree')) trees.push({ p: t.p, v: t.v, s: 1 });
  const signalDots = points.filter((d) => d.kind === 'signal');
  const stops = feats(stopsFc).map(point).filter(visible);
  const pois = feats(poisFc).map(point).filter(visible);
  const cams = feats(camsFc).map(point);
  // signalised junctions that have a queue detector but no camera window
  const detectors = (signals ?? []).filter((s) => !s.id.startsWith('CAM'))
    .map((s) => ({ id: s.id, name: s.name, p: s.target, v: vis(s.target) })).filter(visible);

  // street names: one label per name every ~450 m, along the street direction
  const labels = [];
  const streets = new Map(); // name -> midpoints of its segments, for search
  for (const r of roads) {
    if (!r.name || r.path.length < 2) continue;
    const a = r.path[0], b = r.path[r.path.length - 1];
    const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    if (vis(mid) > 0.3) (streets.get(r.name) ?? streets.set(r.name, []).get(r.name)).push(mid);
    if (!MAJOR.has(r.cls) || distM(a, b) < 60 || vis(mid) < 0.5) continue;
    if (labels.some((l) => l.text === r.name && distM(l.p, mid) < 450)) continue;
    let ang = (Math.atan2((b[1] - a[1]) * mLat, (b[0] - a[0]) * mLon) * 180) / Math.PI;
    if (ang > 90) ang -= 180;
    if (ang < -90) ang += 180;
    labels.push({ p: mid, text: r.name, ang, v: vis(mid) });
  }

  // ground plate: concentric rings fading into the background
  const RINGS = 28;
  const plate = [];
  for (let i = RINGS; i >= 1; i--) {
    const t = 1 - i / RINGS;
    plate.push({ polygon: circle(R * (1 - FADE + (FADE * 1.15 * i) / RINGS)), color: mix(BG, GROUND, t * t * (3 - 2 * t)) });
  }
  plate.push({ polygon: circle(R * (1 - FADE)), color: GROUND });

  return {
    C, R, mLon, mLat, toM, toLL, vis, visM, distM, circle,
    roads, minor, buildings, areas, trees, signals: signalDots, stops, pois, cams, detectors, labels, streets, plate,
    rim: [{ path: circle(R * 1.03, 180) }],
    roadById: new Map(roads.map((r) => [r.id, r])),
  };
}
