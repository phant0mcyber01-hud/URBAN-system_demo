// Pump stations on the map (only while the water module is open), coloured by the station's worst alert:
// healthy is water blue, a warning is amber, an alert (pump fault, pressure out of range, open cabinet) is red;
// trouble pulses. Read only: clicking a station only shows its row in the panel.
import { ScatterplotLayer } from '@deck.gl/layers';
import { stationTone } from '../ui/water.js';

const FILL = { ok: [64, 170, 255, 255], warn: [255, 181, 71, 255], bad: [255, 84, 104, 255] };
const LINE = { ok: [190, 228, 255, 255], warn: [255, 222, 168, 255], bad: [255, 160, 170, 255] };
const at = (d) => [Number(d.lon), Number(d.lat)];
const placed = (d) => d && d.lon != null && d.lat != null && Number.isFinite(Number(d.lon)) && Number.isFinite(Number(d.lat));

export function waterLayers(S, pulse) {
  const sites = (Array.isArray(S.water?.sites) ? S.water.sites : []).filter(placed);
  const trouble = sites.filter((s) => stationTone(s) !== 'ok');
  return [
    new ScatterplotLayer({
      id: 'water-ring', data: trouble, getPosition: at, radiusUnits: 'meters', getRadius: 28,
      radiusScale: 1 + 0.6 * pulse, filled: false, stroked: true, lineWidthMinPixels: 2,
      getLineColor: (d) => [...FILL[stationTone(d)].slice(0, 3), 230],
    }),
    new ScatterplotLayer({
      id: 'water', data: sites, getPosition: at, radiusUnits: 'meters', getRadius: 10, radiusMinPixels: 7,
      stroked: true, lineWidthMinPixels: 2, pickable: true,
      getFillColor: (d) => FILL[stationTone(d)], getLineColor: (d) => LINE[stationTone(d)],
      updateTriggers: { getFillColor: S.waterVersion, getLineColor: S.waterVersion },
    }),
  ];
}
