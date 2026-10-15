// Client-side copy of the platform state: a snapshot from /api/state, then live frames.
const RANK = { viewer: 0, operator: 1, admin: 2 }; // the server enforces this; the UI only hides buttons

export function createState(scene) {
  const S = {
    t: 0, stats: null, events: [], shadow: [], alerts: [], infra: [], cams: {}, cmd: [], link: true, log: [],
    audit: { count: 0, head: '', applied: 0 },
    // derived, with stable identities between frames so map layers are not rebuilt needlessly
    jamRoads: [], infraBad: [], activeIncidents: [], infraVersion: 0, camStatus: {}, camVersion: 0, dev: {}, devVersion: 0, devAlerts: [],
    // UI
    tab: 'overview', cam: null, pin: null,
    user: null, // {name, role} of the signed-in account
    history: null, // from /api/history*, loaded when the tab opens
    mode: 'simulation', // 'simulation' (SUMO, emulated equipment) or 'field' (real URBAN Edge units)
    corridor: false, // green corridor proposals switched on (GREEN_CORRIDOR)
    can: (role) => (RANK[S.user?.role] ?? -1) >= RANK[role],
  };

  function derive() {
    const byId = new Map();
    for (const e of S.events) if (e.type === 'incident') byId.set(e.id, e);
    S.activeIncidents = [...byId.values()].filter((e) => e.status === 'active' && scene.vis([e.lon, e.lat]) > 0.03);
  }
  function setInfra(list) {
    S.infra = list;
    S.infraBad = list.filter((a) => a.status !== 'ok');
    S.infraVersion++;
  }
  function setCmd(list) {
    S.cmd = list;
    const next = {};
    for (const c of list) {
      if (c.status === 'pending') next[c.cam] = next[c.cam] ?? 'problem';
      if (['approved', 'active', 'revoking'].includes(c.status)) next[c.cam] = 'control';
    }
    if (JSON.stringify(next) !== JSON.stringify(S.camStatus)) { S.camStatus = next; S.camVersion++; }
  }

  // dev: junction id -> brief of its URBAN Edge unit: {id, o: online, s: status, v, t, d, c, l, u, a, n, al, lock}
  function setDev(dev) {
    const sig = (d) => Object.entries(d).map(([k, v]) => `${k}:${v.o}${v.s}${v.lock}${v.al.length}`).join();
    if (sig(dev) !== sig(S.dev)) S.devVersion++;
    S.dev = dev;
  }
  S.online = (junction) => (S.dev[junction]?.o ?? 1) === 1;
  S.devLevel = (junction) => {
    const d = S.dev[junction];
    if (!d) return null;
    if (d.lock || d.s === 'revoked') return 'alert';
    return d.al.length ? d.al[0][0] : d.o ? null : 'warn';
  };

  S.applySnapshot = (snap = {}) => {
    S.events = snap.events ?? [];
    S.shadow = snap.shadow ?? [];
    S.alerts = snap.alerts ?? [];
    S.audit = snap.audit ?? S.audit;
    S.link = snap.link ?? true;
    S.log = snap.log ?? S.log;
    setInfra(snap.infra ?? []);
    setCmd(snap.cmd ?? []);
    if (snap.dev) setDev(snap.dev);
    S.devAlerts = snap.devalerts ?? S.devAlerts;
    derive();
  };

  S.ingest = (f) => {
    if (f.reset) S.applySnapshot({ audit: S.audit });
    S.t = f.t;
    S.stats = f.stats;
    S.cams = f.cams ?? S.cams;
    S.link = f.link ?? S.link;
    const jam = (f.jam ?? []).map((id) => scene.roadById.get(id)).filter(Boolean);
    if (jam.length !== S.jamRoads.length || jam.some((r, i) => r !== S.jamRoads[i])) S.jamRoads = jam;
    if (f.events.length) { S.events.push(...f.events); derive(); }
    if (f.shadow) S.shadow.push(...f.shadow);
    if (f.alerts) S.alerts.push(...f.alerts);
    if (f.infra) setInfra(f.infra);
    if (f.cmd) setCmd(f.cmd);
    if (f.dev) setDev(f.dev);
    if (f.devalerts) S.devAlerts = S.devAlerts.concat(f.devalerts).slice(-100);
    if (f.log) S.log = S.log.concat(f.log).slice(-50);
    if (f.audit) S.audit = f.audit;
  };

  S.incidents = () => {
    const byId = new Map();
    for (const e of S.events) if (e.type === 'incident') byId.set(e.id, e);
    return [...byId.values()].reverse();
  };
  S.dispatches = () => {
    const byId = new Map();
    for (const e of S.events) if (e.type === 'dispatch') byId.set(e.id, e);
    return [...byId.values()];
  };
  S.pending = () => S.cmd.filter((c) => c.status === 'pending');
  return S;
}
