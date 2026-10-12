// Layers that never change after load. Built once per Deck instance and again only when
// the layer menu toggles something, so per-frame work never touches them.
import { ColumnLayer, PathLayer, PolygonLayer, ScatterplotLayer, SolidPolygonLayer, TextLayer } from '@deck.gl/layers';
import { AREA, GROUND, MAJOR, MINOR, POI, ROAD, ROAD_DEFAULT } from '../config.js';
import { toward } from './scene.js';

export const fadeColors = (d, rgb, alpha = 255) => d.v.map((v) => [...rgb, alpha * v]);
export const roadWidth = (d) => 2.4 + 3.1 * d.lanes;

const TEXT = {
  characterSet: 'auto', fontFamily: 'system-ui, sans-serif', fontWeight: 600,
  fontSettings: { sdf: true, fontSize: 48, buffer: 6 }, outlineWidth: 3, outlineColor: [12, 15, 20, 255],
};

export function staticLayers(scene, show = {}, { pickable = true } = {}) {
  const on = (key) => show[key] !== false;
  return [
    new SolidPolygonLayer({ id: 'plate', data: scene.plate, getPolygon: (d) => d.polygon, getFillColor: (d) => d.color, material: false }),
    new PathLayer({ id: 'rim', data: scene.rim, getPath: (d) => d.path, getColor: [92, 200, 255, 70], widthUnits: 'pixels', getWidth: 1.2 }),
    new SolidPolygonLayer({
      id: 'areas', data: scene.areas, getPolygon: (d) => d.polygon, material: false,
      getFillColor: (d) => toward(AREA[d.kind], GROUND, d.v),
    }),
    new PathLayer({
      id: 'minor', data: scene.minor, getPath: (d) => d.path, widthUnits: 'meters', widthMinPixels: 0.6,
      getWidth: (d) => MINOR[d.kind].width, getColor: (d) => fadeColors(d, MINOR[d.kind].color),
      capRounded: true, jointRounded: true,
    }),
    new PathLayer({
      id: 'road-casing', data: scene.roads, getPath: (d) => d.path, widthUnits: 'meters',
      getWidth: (d) => roadWidth(d) + 2.4, getColor: (d) => fadeColors(d, [14, 17, 23]),
      capRounded: true, jointRounded: true,
    }),
    new PathLayer({
      id: 'roads', data: scene.roads, getPath: (d) => d.path, widthUnits: 'meters', widthMinPixels: 1,
      getWidth: roadWidth, getColor: (d) => fadeColors(d, ROAD[d.cls] ?? ROAD_DEFAULT),
      capRounded: true, jointRounded: true, pickable,
    }),
    new PathLayer({
      id: 'road-axis', data: scene.roads.filter((r) => MAJOR.has(r.cls) && r.lanes >= 2), getPath: (d) => d.path,
      widthUnits: 'meters', getWidth: 0.35, getColor: (d) => fadeColors(d, [236, 240, 246], 150),
    }),
    new ScatterplotLayer({
      id: 'stops', data: scene.stops, visible: on('transit'), getPosition: (d) => d.p, radiusUnits: 'meters',
      getRadius: 5, radiusMinPixels: 2, getFillColor: (d) => [255, 198, 92, 255 * d.v], stroked: true,
      getLineColor: (d) => [20, 24, 30, 255 * d.v], lineWidthMinPixels: 1, pickable,
    }),
    new ScatterplotLayer({
      id: 'signals', data: scene.signals, getPosition: (d) => d.p, radiusUnits: 'meters', getRadius: 2.2,
      radiusMinPixels: 1.5, getFillColor: (d) => [255, 110, 110, 230 * d.v],
    }),
    new ColumnLayer({
      id: 'trees', data: scene.trees, getPosition: (d) => d.p, diskResolution: 6, radius: 2.6, extruded: true,
      getElevation: (d) => 7 * d.s * d.v, getFillColor: (d) => toward([48, 104, 70], GROUND, d.v),
      material: { ambient: 0.5, diffuse: 0.6, shininess: 4 },
    }),
    new PolygonLayer({
      id: 'buildings', data: scene.buildings, getPolygon: (d) => d.polygon, extruded: on('buildings'), wireframe: on('buildings'),
      getElevation: (d) => d.h, getFillColor: (d) => d.color, getLineColor: (d) => toward([150, 164, 188], GROUND, d.v * 0.55),
      material: { ambient: 0.35, diffuse: 0.75, shininess: 12, specularColor: [40, 46, 60] }, pickable,
    }),
    new ScatterplotLayer({
      id: 'pois', data: scene.pois, visible: on('pois'), getPosition: (d) => d.p, radiusUnits: 'meters', getRadius: 5, radiusMaxPixels: 8,
      radiusMinPixels: 4, getFillColor: (d) => [...POI[d.kind].color, 255 * d.v], stroked: true,
      getLineColor: [12, 15, 20, 255], lineWidthMinPixels: 1.5, pickable,
    }),
    new TextLayer({
      ...TEXT, id: 'poi-labels', data: scene.pois.filter((d) => d.name), visible: on('pois') && show.poiLabels === true,
      getPosition: (d) => d.p, getText: (d) => d.name, getSize: 11, getPixelOffset: [0, -16],
      getColor: (d) => [232, 238, 246, 255 * d.v],
    }),
    new TextLayer({
      ...TEXT, id: 'labels', data: scene.labels, visible: on('labels'), getPosition: (d) => d.p, getText: (d) => d.text,
      getAngle: (d) => d.ang, billboard: false, sizeUnits: 'meters', getSize: 15, sizeMinPixels: 0, sizeMaxPixels: 20,
      getColor: (d) => [226, 233, 242, 235 * d.v],
    }),
  ];
}
