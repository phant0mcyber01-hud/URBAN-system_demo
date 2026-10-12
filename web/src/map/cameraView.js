// Camera window: a second view of the same twin from a camera's position.
// Drag to look around, arrows to step to the next camera; it is created on first use.
import maplibregl from 'maplibre-gl';
import { MapboxOverlay } from '@deck.gl/mapbox';
import { lighting, mapStyle, pixelRatio } from './view.js';

const $ = (id) => document.getElementById(id);
const VIEW = { zoom: 17.8, pitch: 70 };

export function createCameraView(scene, { onChange }) {
  let map = null, overlay = null, current = null, look = { bearing: 0, pitch: VIEW.pitch };

  function aim() {
    map.jumpTo({ center: current.target, zoom: VIEW.zoom, bearing: current.bearing + look.bearing, pitch: look.pitch });
  }

  function init() {
    map = new maplibregl.Map({
      container: 'camview', style: mapStyle, interactive: false, maxPitch: 85, attributionControl: false,
      pixelRatio: pixelRatio(),
    });
    overlay = new MapboxOverlay({ interleaved: false, layers: [], effects: [lighting()], useDevicePixels: pixelRatio() });
    map.addControl(overlay);

    const el = $('camview');
    let drag = null;
    el.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY, ...look }; el.setPointerCapture(e.pointerId); });
    el.addEventListener('pointermove', (e) => {
      if (!drag) return;
      look.bearing = Math.max(-70, Math.min(70, drag.bearing - (e.clientX - drag.x) * 0.25));
      look.pitch = Math.max(45, Math.min(82, drag.pitch - (e.clientY - drag.y) * 0.2));
      aim();
    });
    el.addEventListener('pointerup', () => { drag = null; });
    el.addEventListener('dblclick', () => { look = { bearing: 0, pitch: VIEW.pitch }; aim(); });
  }

  const api = {
    get id() { return current?.id ?? null; },
    get overlay() { return current ? overlay : null; },
    open(id) {
      const cam = scene.cams.find((c) => c.id === id);
      if (!cam) return;
      current = cam;
      look = { bearing: 0, pitch: VIEW.pitch };
      $('cam').hidden = false;
      if (!map) init();
      map.resize();
      aim();
      $('cam-title').textContent = `${cam.id} · ${cam.name}`;
      onChange(cam.id);
    },
    close() {
      current = null;
      $('cam').hidden = true;
      onChange(null);
    },
    step(delta) {
      const i = scene.cams.indexOf(current);
      if (i >= 0) api.open(scene.cams[(i + delta + scene.cams.length) % scene.cams.length].id);
    },
    setHud(text) { $('cam-hud').textContent = text; },
  };
  $('cam-close').onclick = api.close;
  $('cam-prev').onclick = () => api.step(-1);
  $('cam-next').onclick = () => api.step(1);
  return api;
}
