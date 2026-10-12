// Entry point: loads the scene, wires map, camera window, search, place card and the panel,
// then runs one animation loop that only updates what moves.
import './style.css';
import { loadScene, getJson } from './scene/scene.js';
import { staticLayers } from './scene/staticLayers.js';
import { overlayLayers } from './scene/overlays.js';
import { Live } from './scene/live.js';
import { createMap } from './map/view.js';
import { createCameraView } from './map/cameraView.js';
import { createSearch } from './map/search.js';
import { createPlaceCard, describe } from './map/placeCard.js';
import { createPanel, clock } from './ui/panel.js';
import { createState } from './state.js';
import * as api from './api.js';

const $ = (id) => document.getElementById(id);

const scene = await loadScene();
const S = createState(scene);
const live = new Live(scene);

// ---- layer menu ----
const LAYERS = [
  ['buildings', 'Здания в 3D'], ['traffic', 'Транспортный поток'], ['transit', 'Остановки'],
  ['cameras', 'Камеры'], ['infra', 'Инфраструктура'], ['pois', 'Службы и больницы'], ['labels', 'Названия улиц'],
];
const show = Object.fromEntries(LAYERS.map(([k]) => [k, true]));
let base = staticLayers(scene, show);
let camBase = null;
$('layers-menu').innerHTML = LAYERS.map(([k, label]) => `<label><input type="checkbox" data-layer="${k}" checked> ${label}</label>`).join('');
$('layers-menu').onchange = (e) => {
  show[e.target.dataset.layer] = e.target.checked;
  base = staticLayers(scene, show);
  camBase = null;
};
$('layers-btn').onclick = () => { $('layers-menu').hidden = !$('layers-menu').hidden; };

// ---- map, camera window, place card, search, panel ----
const card = createPlaceCard({
  onClose: () => { S.pin = null; },
  onAction: (action) => panel.setTab(action.tab),
});

function select(kind, object, lonlat) {
  const info = describe(kind, object, S);
  if (!info) return;
  S.pin = lonlat;
  card.show(info);
}

const { map, overlay, flyTo } = createMap(scene, {
  onClick: ({ object, layer, coordinate }) => {
    if (!object || !layer) { card.hide(); S.pin = null; return; }
    if (layer.id === 'cams') { panel.setTab('cameras'); cameras.open(object.id); return; }
    select(layer.id, object, coordinate ? [coordinate[0], coordinate[1]] : null);
  },
});

// facility names only when zoomed in, so the overview stays clean
map.on('zoom', () => {
  const near = map.getZoom() >= 15.8;
  if (near !== (show.poiLabels === true)) { show.poiLabels = near; base = staticLayers(scene, show); }
});

const cameras = createCameraView(scene, { onChange: (id) => { S.cam = id; panel.render(); } });
const panel = createPanel(scene, S, {
  onCamera: (id) => (id === S.cam ? cameras.close() : cameras.open(id)),
  onAction: (act, id) => run(() => api.post(`/api/commands/${id}/${act}`)),
});

// ---- operator: viewing is open, deciding needs the token ----
function toast(text) {
  let el = $('toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'glass'; document.body.append(el); }
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
}
async function run(action) {
  try { await action(); } catch (e) { toast(e.message); }
}
function renderOperator() {
  $('op-login').hidden = S.operator || !$('op-token').hidden;
  $('op-tools').hidden = !S.operator;
  if (S.operator) $('op-token').hidden = true;
  const link = $('link');
  link.textContent = S.link ? 'связь с оборудованием' : 'связь отключена';
  link.className = `chip ${S.link ? 'ok' : 'bad'}`;
  $('op-link').textContent = S.link ? 'Отключить связь' : 'Восстановить связь';
  $('op-link').classList.toggle('on', !S.link);
  panel.render();
}
$('op-login').onclick = () => { $('op-token').hidden = false; $('op-login').hidden = true; $('op-token').focus(); };
$('op-token').onkeydown = async (e) => {
  if (e.key === 'Escape') { $('op-token').hidden = true; renderOperator(); }
  if (e.key !== 'Enter') return;
  S.operator = await api.verify($('op-token').value.trim());
  $('op-token').value = '';
  $('op-token').classList.toggle('bad', !S.operator);
  if (!S.operator) toast('токен не подошёл');
  renderOperator();
};
$('op-logout').onclick = () => { api.logout(); S.operator = false; renderOperator(); };
$('op-revoke').onclick = () => run(() => api.post('/api/revoke-all'));
$('op-link').onclick = () => run(() => api.post('/api/link', { up: !S.link }));
S.operator = await api.verify();

createSearch(scene, {
  getAssets: () => S.infra,
  onSelect: (entry) => {
    flyTo(entry.p, entry.zoom);
    if (entry.type === 'cam') { panel.setTab('cameras'); cameras.open(entry.ref.id); S.pin = null; card.hide(); return; }
    const kind = { poi: 'pois', asset: 'infra', street: 'street' }[entry.type];
    select(kind, entry.ref ?? entry, entry.p);
  },
});

window.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape') return;
  if (card.open) { card.hide(); S.pin = null; } else if (S.cam) cameras.close();
});

// ---- render loop ----
let frames = 0, fpsAt = performance.now(), busy = 0;
function frame(now) {
  const started = performance.now();
  live.update(now);
  const pulse = 0.5 + 0.5 * Math.sin(now / 300);
  overlay.setProps({
    layers: [...base, ...overlayLayers(scene, S, show, { main: true, pulse }), ...live.layers(show.traffic)],
  });
  const camOverlay = cameras.overlay;
  if (camOverlay) {
    camBase ??= staticLayers(scene, { ...show, labels: false }, { pickable: false }).filter((l) => l.id !== 'rim');
    camOverlay.setProps({
      layers: [...camBase, ...overlayLayers(scene, S, show, { main: false, pulse }), ...live.layers(show.traffic)],
    });
  }
  frames++;
  busy += performance.now() - started;
  if (now - fpsAt >= 1000) {
    $('perf').textContent = `${Math.round((frames * 1000) / (now - fpsAt))} к/с · ${(busy / frames).toFixed(2)} мс на кадр · ${live.n} ТС`;
    frames = 0; busy = 0; fpsAt = now;
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---- live data ----
function onFrame(f) {
  live.ingest(f.v);
  S.ingest(f);
  renderOperator();
  if (S.cam) {
    const [n, speed, queue] = S.cams[S.cam] ?? [0, 0, 0];
    cameras.setHud(`${clock(S.t)} · ТС в кадре: ${n} · очередь: ${queue} · ср. скорость: ${speed} км/ч · обезличено`);
  }
}

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = async () => { S.applySnapshot((await getJson('/api/state')) ?? {}); renderOperator(); };
  ws.onmessage = (m) => onFrame(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connect, 1500);
}
panel.setTab('overview');
connect();
