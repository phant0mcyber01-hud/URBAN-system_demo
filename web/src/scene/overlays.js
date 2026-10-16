// Platform state drawn on the map: congestion, incidents, utility assets, cameras, the search pin.
// Pulsing is done with uniform props (radiusScale / opacity), so no attribute is rebuilt per frame.
import { ColumnLayer, PathLayer, ScatterplotLayer, SolidPolygonLayer } from '@deck.gl/layers';
import { ACCENT, STATUS } from '../config.js';
import { fadeColors, roadWidth } from './staticLayers.js';

// waiting for a decision = amber (warning), under a command = blue (information), trouble = red (alarm)
const CAM_FILL = { problem: [255, 181, 71, 255], control: [...ACCENT, 255] };
const OFFLINE = [255, 84, 104, 255]; // the Edge unit of this junction is not connected or not trusted
const WARN = [255, 181, 71, 255];   // the unit reports a warning (heat, silence, version)
const ring = (S, d, normal) => {
  const level = S.devLevel(d.id);
  return level === 'alert' ? OFFLINE : level === 'warn' ? WARN : normal;
};
const CAM_LINE = { problem: [255, 222, 168, 255], control: [190, 240, 255, 255] };
const APPROVED = [...ACCENT, 255];         // a junction whose passport is approved: URBAN may command it
const DRAFT = [96, 118, 128, 255];         // a draft passport: watched only

function cone(scene, cam) {
  const a = (cam.bearing * Math.PI) / 180;
  const [x, y] = scene.toM(cam.p);
  const pt = (da, len) => scene.toLL(x + Math.sin(a + da) * len, y + Math.cos(a + da) * len);
  return [cam.p, pt(-0.45, 110), pt(0, 125), pt(0.45, 110)];
}

export function overlayLayers(scene, S, show, { main, pulse }) {
  const on = (key) => show[key] !== false;
  const layers = [
    new PathLayer({
      id: 'jam', data: S.jamRoads, visible: on('traffic'), getPath: (d) => d.path, widthUnits: 'meters', widthMinPixels: 2,
      getWidth: (d) => roadWidth(d) + 1, getColor: (d) => fadeColors(d, [227, 120, 58], 200), capRounded: true,
    }),
    new ColumnLayer({
      id: 'infra', data: S.infra, visible: on('infra'), getPosition: (d) => [d.lon, d.lat], diskResolution: 6, radius: 9,
      extruded: true, getElevation: 30, getFillColor: (d) => STATUS[d.status],
      material: { ambient: 0.7, diffuse: 0.5, shininess: 20 }, pickable: main,
      updateTriggers: { getFillColor: S.infraVersion },
    }),
    new ScatterplotLayer({
      id: 'infra-ring', data: S.infraBad, visible: on('infra'), getPosition: (d) => [d.lon, d.lat], radiusUnits: 'meters',
      getRadius: 26, radiusScale: 1 + 0.6 * pulse, filled: false, stroked: true, lineWidthMinPixels: 2,
      getLineColor: (d) => [...STATUS[d.status], 230], updateTriggers: { getLineColor: S.infraVersion },
    }),
    new ScatterplotLayer({
      id: 'incidents', data: S.activeIncidents, getPosition: (d) => [d.lon, d.lat], radiusUnits: 'meters',
      getRadius: 22, radiusScale: 1 + 0.6 * pulse, radiusMinPixels: 9, stroked: true, lineWidthMinPixels: 2,
      getFillColor: [255, 84, 104, 80], getLineColor: [255, 84, 104, 255], pickable: main,
    }),
  ];
  if (main) {
    layers.push(
      new SolidPolygonLayer({
        id: 'cam-cone', data: scene.cams.filter((c) => c.id === S.cam), visible: on('cameras'),
        getPolygon: (c) => cone(scene, c), getFillColor: [...ACCENT, 34], material: false,
      }),
      new ScatterplotLayer({
        id: 'detectors', data: scene.detectors, visible: on('junctions'), getPosition: (d) => d.p, radiusUnits: 'meters',
        getRadius: 6, radiusMinPixels: 4, stroked: true, lineWidthMinPixels: 1.6, pickable: true,
        getFillColor: (d) => CAM_FILL[S.camStatus[d.id]] ?? [4, 7, 10, 230 * d.v],
        getLineColor: (d) => ring(S, d, CAM_LINE[S.camStatus[d.id]] ?? (S.approved(d.id) ? APPROVED : DRAFT)),
        updateTriggers: { getFillColor: S.camVersion, getLineColor: [S.camVersion, S.devVersion, S.junctionsVersion] },
      }),
      new ScatterplotLayer({
        id: 'cams', data: scene.cams, visible: on('cameras'), getPosition: (d) => d.p, radiusUnits: 'meters', getRadius: 7,
        radiusMinPixels: 5,
        getFillColor: (d) => CAM_FILL[S.camStatus[d.id]] ?? (d.id === S.cam ? [228, 231, 235, 255] : [20, 22, 25, 255]),
        getLineColor: (d) => ring(S, d, CAM_LINE[S.camStatus[d.id]] ?? [200, 206, 214, 255]),
        stroked: true, lineWidthMinPixels: 2, pickable: true,
        updateTriggers: { getFillColor: [S.cam, S.camVersion], getLineColor: [S.camVersion, S.devVersion] },
      }),
      new ScatterplotLayer({
        id: 'pin', data: S.pin ? [S.pin] : [], getPosition: (d) => d, radiusUnits: 'pixels', getRadius: 9,
        radiusScale: 1 + 0.35 * pulse, filled: true, getFillColor: [...ACCENT, 50], stroked: true,
        getLineColor: [...ACCENT, 255], lineWidthMinPixels: 2,
      }),
    );
  }
  return layers;
}
