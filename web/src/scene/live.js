// Live vehicles. Frames arrive at 1 Hz; positions are interpolated every animation frame
// into preallocated typed arrays and drawn as one instanced mesh, so a frame costs a few
// hundred float writes and no allocations, tessellation or per-vehicle objects.
import { COORDINATE_SYSTEM } from '@deck.gl/core';
import { ScatterplotLayer } from '@deck.gl/layers';
import { SimpleMeshLayer } from '@deck.gl/mesh-layers';
import { CubeGeometry } from '@luma.gl/engine';
import { EMG_BLINK, KINDS, VEHICLE_COLOR, VEHICLE_SIZE } from '../config.js';

const cube = new CubeGeometry();
const HALF = VEHICLE_SIZE.map(([l, w, h]) => [l / 2, w / 2, h / 2]);
const GLOW_RADIUS = [2.2, 5, 9];
const EMG = KINDS.indexOf('emg');

export class Live {
  constructor(scene) {
    this.scene = scene;
    this.vehicles = new Map(); // id -> {ax, ay, bx, by, a0, a1, k, gen}
    this.generation = 0;
    this.frameAt = performance.now();
    this.n = 0;
    this.tick = 0;
    this.blink = EMG_BLINK[0];
    this._grow(1024);
  }

  _grow(capacity) {
    this.capacity = capacity;
    this.pos = new Float32Array(capacity * 3); // metres east/north/up of the scene centre
    this.yaw = new Float32Array(capacity);
    this.kind = new Uint8Array(capacity);
    this.alpha = new Uint8Array(capacity);
  }

  /** frame.v: [id, x dm, y dm, angle deg, speed m/s, kind] */
  ingest(list) {
    this.frameAt = performance.now();
    const gen = ++this.generation;
    for (const [id, xd, yd, ang, , kind] of list) {
      const x = xd / 10, y = yd / 10;
      const cur = this.vehicles.get(id);
      if (cur) {
        cur.ax = cur.bx; cur.ay = cur.by; cur.bx = x; cur.by = y; cur.a0 = cur.a1; cur.a1 = ang; cur.gen = gen;
      } else {
        this.vehicles.set(id, { ax: x, ay: y, bx: x, by: y, a0: ang, a1: ang, k: Math.max(0, KINDS.indexOf(kind)), gen });
      }
    }
    for (const [id, v] of this.vehicles) if (v.gen !== gen) this.vehicles.delete(id);
    if (this.vehicles.size > this.capacity) this._grow(this.vehicles.size * 2);
  }

  /** Interpolates all vehicles for the current moment. Call once per animation frame. */
  update(now = performance.now()) {
    const k = Math.min(1, (now - this.frameAt) / 1000);
    const { pos, yaw, kind, alpha } = this;
    const visM = this.scene.visM;
    let i = 0;
    for (const v of this.vehicles.values()) {
      const x = v.ax + (v.bx - v.ax) * k, y = v.ay + (v.by - v.ay) * k;
      const f = visM(x, y);
      if (f < 0.03) continue;
      // SUMO angle: degrees clockwise from north; the reported position is the front bumper
      const deg = v.a0 + ((((v.a1 - v.a0 + 540) % 360) - 180) * k);
      const rad = (deg * Math.PI) / 180;
      const half = HALF[v.k];
      pos[i * 3] = x - Math.sin(rad) * half[0];
      pos[i * 3 + 1] = y - Math.cos(rad) * half[0];
      pos[i * 3 + 2] = half[2];
      yaw[i] = 90 - deg;
      kind[i] = v.k;
      alpha[i] = 255 * f;
      i++;
    }
    this.n = i;
    this.tick++;
    this.blink = EMG_BLINK[Math.floor(now / 220) % 2];
  }

  layers(visible = true) {
    const { pos, yaw, kind, alpha, tick, blink } = this;
    const data = { length: this.n };
    const origin = [this.scene.C[0], this.scene.C[1], 0];
    const getPosition = (_, { index: i, target: t }) => { t[0] = pos[i * 3]; t[1] = pos[i * 3 + 1]; t[2] = pos[i * 3 + 2]; return t; };
    const color = (a) => (_, { index: i, target: t }) => {
      const c = kind[i] === EMG ? blink : VEHICLE_COLOR[kind[i]];
      t[0] = c[0]; t[1] = c[1]; t[2] = c[2]; t[3] = alpha[i] * a;
      return t;
    };
    const shared = {
      data, visible, coordinateSystem: COORDINATE_SYSTEM.METER_OFFSETS, coordinateOrigin: origin, getPosition,
    };
    return [
      new ScatterplotLayer({
        ...shared, id: 'veh-glow', radiusUnits: 'meters', radiusMinPixels: 2.2,
        getRadius: (_, { index: i }) => GLOW_RADIUS[kind[i]], getFillColor: color(0.55),
        updateTriggers: { getPosition: tick, getFillColor: tick, getRadius: tick },
      }),
      new SimpleMeshLayer({
        ...shared, id: 'veh', mesh: cube, getColor: color(1),
        getOrientation: (_, { index: i }) => [0, yaw[i], 0], getScale: (_, { index: i }) => HALF[kind[i]],
        material: { ambient: 0.6, diffuse: 0.6, shininess: 30 },
        updateTriggers: { getPosition: tick, getColor: tick, getOrientation: tick, getScale: tick },
      }),
    ];
  }
}
