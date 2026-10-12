// Main map: familiar web-map navigation (drag to pan, wheel to zoom at the cursor, right-drag or
// Ctrl+drag to rotate and tilt, double-click to zoom, keyboard arrows and +/-), plus compass,
// 2D/3D switch, home button, scale bar and a shareable position in the URL.
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { MapboxOverlay } from '@deck.gl/mapbox';
import { AmbientLight, DirectionalLight, LightingEffect } from '@deck.gl/core';
import { BG, MAX_PIXEL_RATIO } from '../config.js';

export const mapStyle = {
  version: 8, sources: {},
  layers: [{ id: 'bg', type: 'background', paint: { 'background-color': `rgb(${BG})` } }],
};
export const pixelRatio = () => Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO);
export const lighting = () => new LightingEffect({
  ambient: new AmbientLight({ color: [255, 255, 255], intensity: 1.15 }),
  sun: new DirectionalLight({ color: [255, 240, 220], intensity: 1.5, direction: [-1.5, -2.5, -3] }),
  fill: new DirectionalLight({ color: [150, 180, 255], intensity: 0.6, direction: [2, 1.5, -1] }),
});

const PITCH_3D = 56;

class ViewButtons {
  constructor(home) { this.home = home; }

  onAdd(map) {
    this.el = document.createElement('div');
    this.el.className = 'maplibregl-ctrl maplibregl-ctrl-group view-buttons';
    const button = (text, title, onClick) => {
      const b = document.createElement('button');
      b.type = 'button'; b.textContent = text; b.title = title; b.onclick = onClick;
      this.el.append(b);
      return b;
    };
    const mode = button('2D', 'Переключить 2D / 3D', () => map.easeTo({ pitch: map.getPitch() > 5 ? 0 : PITCH_3D, duration: 600 }));
    map.on('pitchend', () => { mode.textContent = map.getPitch() > 5 ? '2D' : '3D'; });
    button('⌂', 'Весь район', () => map.flyTo({ ...this.home, duration: 900 }));
    return this.el;
  }

  onRemove() { this.el.remove(); }
}

export function createMap(scene, { onClick }) {
  const { C, R, mLon, mLat } = scene;
  const home = { center: C, zoom: 14.6, pitch: PITCH_3D, bearing: -18 };
  const pad = R * 2.2;
  const map = new maplibregl.Map({
    container: 'map', style: mapStyle, ...home, hash: 'map', attributionControl: false,
    maxPitch: 72, minZoom: 13.2, maxZoom: 19.5, pixelRatio: pixelRatio(),
    maxBounds: [[C[0] - pad / mLon, C[1] - pad / mLat], [C[0] + pad / mLon, C[1] + pad / mLat]],
  });
  const overlay = new MapboxOverlay({
    interleaved: false, layers: [], effects: [lighting()], useDevicePixels: pixelRatio(), pickingRadius: 4, onClick,
    getCursor: ({ isDragging, isHovering }) => (isDragging ? 'grabbing' : isHovering ? 'pointer' : 'grab'),
  });
  map.addControl(overlay);
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right');
  map.addControl(new ViewButtons(home), 'bottom-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric', maxWidth: 120 }), 'bottom-right');

  const flyTo = (lonlat, zoom = 17) => map.flyTo({
    center: lonlat, zoom: Math.max(map.getZoom(), zoom), speed: 1.4, curve: 1.3, essential: true,
  });
  return { map, overlay, flyTo };
}
