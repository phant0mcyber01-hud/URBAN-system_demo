// Platform state drawn on the map: congestion, incidents, utility assets, cameras, the search pin.
// Pulsing is done with uniform props (radiusScale / opacity), so no attribute is rebuilt per frame.
import { ColumnLayer, PathLayer, ScatterplotLayer, SolidPolygonLayer } from '@deck.gl/layers';
import { STATUS } from '../config.js';
import { fadeColors, roadWidth } from './staticLayers.js';

const CAM_FILL = { problem: [255, 190, 70, 255], control: [70, 190, 150, 255] };
const OFFLINE = [255, 90, 90, 255]; // the Edge unit of this junction is not connected or not trusted
const WARN = [255, 190, 70, 255];   // the unit reports a warning (heat, silence, version)
const ring = (S, d, normal) => {
  const level = S.devLevel(d.id);
  return level === 'alert' ? OFFLINE : level === 'warn' ? WARN : normal;
};
const CAM_LINE = { problem: [255, 225, 160, 255], control: [170, 240, 210, 255] };

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
      getWidth: (d) => roadWidth(d) + 1, getColor: (d) => fadeColors(d, [255, 70, 70], 190), capRounded: true,
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
      getFillColor: [255, 60, 60, 90], getLineColor: [255, 90, 90, 255], pickable: main,
    }),
  ];
  if (main) {
    layers.push(
      new SolidPolygonLayer({
        id: 'cam-cone', data: scene.cams.filter((c) => c.id === S.cam), visible: on('cameras'),
        getPolygon: (c) => cone(scene, c), getFillColor: [92, 200, 255, 55], material: false,
      }),
      new ScatterplotLayer({
        id: 'detectors', data: scene.detectors, visible: on('cameras'), getPosition: (d) => d.p, radiusUnits: 'meters',
        getRadius: 5, radiusMinPixels: 3.5, stroked: true, lineWidthMinPixels: 1.5, pickable: true,
        getFillColor: (d) => CAM_FILL[S.camStatus[d.id]] ?? [16, 20, 27, 255 * d.v],
        getLineColor: (d) => ring(S, d, CAM_LINE[S.camStatus[d.id]] ?? [120, 150, 175, 255 * d.v]),
        updateTriggers: { getFillColor: S.camVersion, getLineColor: [S.camVersion, S.devVersion] },
      }),
      new ScatterplotLayer({
        id: 'cams', data: scene.cams, visible: on('cameras'), getPosition: (d) => d.p, radiusUnits: 'meters', getRadius: 7,
        radiusMinPixels: 5,
        getFillColor: (d) => CAM_FILL[S.camStatus[d.id]] ?? (d.id === S.cam ? [92, 200, 255, 255] : [16, 20, 27, 255]),
        getLineColor: (d) => ring(S, d, CAM_LINE[S.camStatus[d.id]] ?? [92, 200, 255, 255]),
        stroked: true, lineWidthMinPixels: 2, pickable: true,
        updateTriggers: { getFillColor: [S.cam, S.camVersion], getLineColor: [S.camVersion, S.devVersion] },
      }),
      new ScatterplotLayer({
        id: 'pin', data: S.pin ? [S.pin] : [], getPosition: (d) => d, radiusUnits: 'pixels', getRadius: 9,
        radiusScale: 1 + 0.35 * pulse, filled: true, getFillColor: [255, 255, 255, 40], stroked: true,
        getLineColor: [255, 255, 255, 255], lineWidthMinPixels: 2,
      }),
    );
  }
  return layers;
}
