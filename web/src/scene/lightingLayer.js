// Street-lighting cabinets on the map (only while the lighting module is open): a lit cabinet is warm yellow,
// an off one is dark steel, one that needs a visit is amber, one that is offline is red; trouble pulses.
import { ScatterplotLayer } from '@deck.gl/layers';
import { cabinetTone } from '../ui/lighting.js';

const FILL = { on: [255, 214, 120, 255], off: [20, 30, 38, 255], warn: [255, 181, 71, 255], bad: [255, 84, 104, 255] };
const LINE = { on: [255, 240, 200, 255], off: [112, 150, 168, 255], warn: [255, 222, 168, 255], bad: [255, 160, 170, 255] };

export function lightingLayers(S, pulse) {
  const cabs = (S.lighting?.cabinets ?? []).filter((c) => Number.isFinite(c.lon) && Number.isFinite(c.lat));
  const trouble = cabs.filter((c) => ['warn', 'bad'].includes(cabinetTone(c)));
  return [
    new ScatterplotLayer({
      id: 'lighting-ring', data: trouble, getPosition: (d) => [d.lon, d.lat], radiusUnits: 'meters', getRadius: 24,
      radiusScale: 1 + 0.6 * pulse, filled: false, stroked: true, lineWidthMinPixels: 2,
      getLineColor: (d) => [...FILL[cabinetTone(d)].slice(0, 3), 230],
    }),
    new ScatterplotLayer({
      id: 'lighting', data: cabs, getPosition: (d) => [d.lon, d.lat], radiusUnits: 'meters', getRadius: 8, radiusMinPixels: 6,
      stroked: true, lineWidthMinPixels: 2, pickable: true,
      getFillColor: (d) => FILL[cabinetTone(d)], getLineColor: (d) => LINE[cabinetTone(d)],
      updateTriggers: { getFillColor: S.lightingVersion, getLineColor: S.lightingVersion },
    }),
  ];
}
