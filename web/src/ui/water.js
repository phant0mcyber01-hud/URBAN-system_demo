// Module «Водоканал»: the pump-station panel. READ ONLY: the module has no command route, so this panel has no
// buttons, no inputs and no forms (docs/water-module.md, «Почему он не может ничего записать").
// Everything that decides what is on the screen is a pure function (data -> HTML string), so the gate
// (tools/gates/web-modules.mjs) can run it in node. createWater() is the thin DOM part: it polls GET /api/water/state
// and flies the map to a station when its row is clicked. Every text and number from the API is device-sourced
// (untrusted): it is escaped, and a missing value (null) is shown as «—», never guessed.
// This file imports nothing, so node can load it as is.

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const chip = (cls, text) => `<span class="chip ${cls}">${esc(text)}</span>`;
const plural = (n, one, few, many) => (n % 10 === 1 && n % 100 !== 11 ? one
  : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many);

export const NONE = '—';
/** A finite number or null: null, '', NaN, strings that are not numbers -> null (Number(null) would be 0). */
const val = (v) => (v == null || v === '' || typeof v === 'boolean' ? null : Number.isFinite(Number(v)) ? Number(v) : null);
/** A reading with its unit, or «—» when the station did not send it. */
export const fmt = (v, digits, unit = '') => { const n = val(v); return n == null ? NONE : `${n.toFixed(digits)}${unit ? ` ${unit}` : ''}`; };
const ageText = (s) => (val(s) == null ? '' : s < 60 ? `${Math.round(s)} с` : s < 3600 ? `${Math.floor(s / 60)} мин` : `${Math.floor(s / 3600)} ч`);

// ---- alerts --------------------------------------------------------------------------------------------
// Server levels: 'alert' (needs a person now) and 'warn'. UI tones: bad > warn > ok.
const RANK = { bad: 2, warn: 1, ok: 0 };
export const alertTone = (a) => (a?.level === 'alert' ? 'bad' : 'warn');   // an unknown level is not ignored
const alertsOf = (s) => (Array.isArray(s?.alerts) ? s.alerts : []).filter((a) => a && typeof a === 'object' && (a.text || a.kind));

/** The station's alerts, worst first: [{tone, kind, text}]. An offline station the server did not flag yet is a warning. */
export function stationAlerts(s) {
  const out = alertsOf(s).map((a) => ({ tone: alertTone(a), kind: String(a.kind ?? ''), text: String(a.text || a.kind) }));
  if (s?.online === false && !out.some((a) => a.kind === 'offline')) out.push({ tone: 'warn', kind: 'offline', text: 'нет связи' });
  return out.sort((a, b) => RANK[b.tone] - RANK[a.tone]);
}
/** Colour of a station on the map and in the list: its worst alert ('bad', 'warn') or 'ok'. */
export function stationTone(s) {
  return stationAlerts(s).reduce((t, a) => (RANK[a.tone] > RANK[t] ? a.tone : t), 'ok');
}

// ---- one station ---------------------------------------------------------------------------------------
const DOOR = { closed: 'шкаф закрыт', open: 'шкаф открыт', unknown: 'шкаф: нет данных' };

/** Outlet pressure against the centre's thresholds: {text, tone}. */
export function pressureText(s) {
  const p = val(s.pressure_out), lo = val(s.p_min), hi = val(s.p_max);
  const norm = lo != null && hi != null ? `норма ${fmt(lo, 2)}–${fmt(hi, 2)}` : lo != null ? `норма от ${fmt(lo, 2)}` : hi != null ? `норма до ${fmt(hi, 2)}` : 'порог не задан';
  const out = p != null && ((lo != null && p < lo) || (hi != null && p > hi));
  return { text: `давление на выходе ${fmt(p, 2, 'бар')} (${norm})`, tone: out ? 'bad' : '' };
}

/** The pumps line: one chip per pump, «Н1 работает / стоит / авария / —». */
export function pumpsHtml(s) {
  const pumps = Array.isArray(s.pumps) ? s.pumps : [];
  if (!pumps.length) return `<span>насосы: ${NONE}</span>`;
  const running = pumps.filter((p) => p?.run === true).length;
  return `<span>насосов ${pumps.length}, работает ${running}:</span> ${pumps.map((p, i) => {
    const n = `Н${i + 1}`;
    if (p?.fault === true) return chip('bad', `${n} авария`);
    if (p?.run === true) return chip('ok', `${n} работает`);
    if (p?.run === false) return chip('', `${n} стоит`);
    return chip('', `${n} ${NONE}`);
  }).join(' ')}`;
}

/** Night minimum flow against the usual one (the leak sign). */
export function nightText(s) {
  return `ночной минимум ${fmt(s.night_min_flow, 1, 'м³/ч')}, обычно ${fmt(s.night_baseline, 1, 'м³/ч')}`;
}

export function renderStation(s, { focus = null } = {}) {
  const id = String(s.id ?? s.site ?? '');
  const name = s.site ?? s.id ?? NONE;
  const alerts = stationAlerts(s);
  const tone = stationTone(s);
  const pr = pressureText(s);
  const where = val(s.lon) != null && val(s.lat) != null;
  const link = s.online === false
    ? chip('bad', `нет связи${ageText(s.age) ? `, ${ageText(s.age)}` : ''}`)
    : s.online === true ? chip('ok', `на связи${ageText(s.age) ? `, ${ageText(s.age)} назад` : ''}`) : chip('', `связь: ${NONE}`);
  return `<div class="item wt-st wt-${tone}${focus === id ? ' focus' : ''}" data-station="${esc(id)}" data-tone="${tone}"${tone !== 'ok' ? ' data-alert="1"' : ''}${where ? ` data-wt-fly="${esc(id)}" title="Показать на карте"` : ''}>
    <div class="row"><b>${esc(name)}</b><span>${link}</span></div>
    <div class="row meta"><span class="wt-p ${pr.tone}">${esc(pr.text)}</span><span>на входе ${esc(fmt(s.pressure_in, 2, 'бар'))}</span></div>
    <div class="row meta"><span>расход ${esc(fmt(s.flow, 1, 'м³/ч'))}</span><span>мощность ${esc(fmt(s.power_kw, 1, 'кВт'))}</span><span>${esc(fmt(s.kwh_per_m3, 2, 'кВт·ч/м³'))}</span></div>
    <div class="row meta"><span>счётчик ${esc(fmt(s.energy_kwh, 0, 'кВт·ч'))}</span><span>уровень ${esc(fmt(s.level, 2, 'м'))}</span><span>${esc(DOOR[s.door] ?? `шкаф: ${NONE}`)}</span></div>
    <div class="row meta wt-pumps">${pumpsHtml(s)}</div>
    <div class="row meta"><span>${esc(nightText(s))}</span></div>
    ${alerts.map((a) => `<div class="wt-alert ${a.tone}" data-kind="${esc(a.kind)}">${chip(a.tone, a.text)}</div>`).join('')}
  </div>`;
}

// ---- the list ------------------------------------------------------------------------------------------
const kpi = (label, value, cls = '') => `<div class="kpi ${cls}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;
const sitesOf = (state) => (state && Array.isArray(state.sites) ? state.sites.filter((s) => s && typeof s === 'object') : null);

/** One line for the module switch / the panel header: «3 станции, 2 на связи, 1 с тревогой». */
export function waterSummary(state) {
  const sites = sitesOf(state);
  if (!sites) return 'нет данных';
  if (!sites.length) return 'станций нет';
  const n = sites.length;
  const online = sites.filter((s) => s.online !== false).length;
  const alarmed = sites.filter((s) => stationTone(s) !== 'ok').length;
  return `${n} ${plural(n, 'станция', 'станции', 'станций')}, ${online} на связи, ${alarmed ? `${alarmed} с тревогой` : 'тревог нет'}`;
}

export function renderWaterList(state, opts = {}) {
  const sites = sitesOf(state);
  if (!sites) return `<p class="muted">${esc(opts.error || 'загрузка состояния насосных станций…')}</p>`;
  if (!sites.length) return '<p class="muted">Насосных станций пока нет. Станция подключается так же, как шкаф освещения: <code>python -m urban.provision enroll-water</code>.</p>';
  const online = sites.filter((s) => s.online !== false).length;
  const alarmed = sites.filter((s) => stationTone(s) !== 'ok').length;
  const flows = sites.filter((s) => s.online !== false).map((s) => val(s.flow)).filter((v) => v != null);
  const kws = sites.filter((s) => s.online !== false).map((s) => val(s.power_kw)).filter((v) => v != null);
  const sum = (xs) => xs.reduce((a, b) => a + b, 0);
  // worst first, then by name: what needs a person is on top
  const rows = [...sites].sort((a, b) => (RANK[stationTone(b)] - RANK[stationTone(a)])
    || String(a.site ?? a.id).localeCompare(String(b.site ?? b.id), 'ru', { numeric: true }));
  return `<div class="grid">${kpi('станций на связи', `${online} / ${sites.length}`, online < sites.length ? 'warn' : '')}${kpi('с тревогой', alarmed, alarmed ? 'warn' : '')}
      ${kpi('расход, м³/ч', flows.length ? sum(flows).toFixed(1) : NONE)}${kpi('мощность, кВт', kws.length ? sum(kws).toFixed(1) : NONE)}</div>
    ${opts.error ? `<p class="err">${esc(opts.error)}</p>` : ''}
    <h4>Станции</h4>${rows.map((s) => renderStation(s, opts)).join('')}`;
}

export const READONLY_TEXT = 'Модуль только читает: управления насосами из центра нет.';

/** The whole water panel: header, the list (replaced on refresh), the read-only note. No controls by design. */
export function renderWaterPanel(state, { focus = null, error = '' } = {}) {
  return `<h3>Водоканал</h3>
    <div data-wt="list">${renderWaterList(state, { focus, error })}</div>
    <div class="item wt-note">${chip('ok', 'только чтение')} ${esc(READONLY_TEXT)}</div>`;
}

// ---- the DOM part ----------------------------------------------------------------------------------------
/**
 * Wires the panel into `root` (the #panel element). `api` is {get}; `onData(state)` hands the state to the map
 * layer; `onFocus(id)` flies the map to a station. Only GET /api/water/state is ever called.
 */
export function createWater({ root, api, onData = () => {}, onFocus = () => {}, onAuthLost = () => {}, every = 5000 }) {
  let state = null, error = '', active = false, timer = null, focus = null;
  const drawList = () => {
    const el = root.querySelector('[data-wt="list"]');
    if (!el) return;
    const html = renderWaterList(state, { focus, error });
    if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
  };
  async function refresh() {
    if (!active || (typeof document !== 'undefined' && document.hidden)) return;
    try { state = await api.get('/api/water/state'); error = ''; onData(state); } catch (e) {
      if (e.message === 'нужно войти заново') onAuthLost();
      error = `Состояние насосных станций не получено: ${e.message}`;
    }
    if (active) drawList();
  }
  root.addEventListener('click', (e) => {
    if (!active) return;
    const row = e.target.closest?.('[data-wt-fly]');
    if (row) { focus = row.dataset.wtFly; onFocus(focus); drawList(); }
  });
  return {
    start() {
      active = true;
      error = '';
      root.innerHTML = renderWaterPanel(state, { focus, error });
      refresh();
      clearInterval(timer);
      timer = setInterval(refresh, every);
    },
    stop() { active = false; clearInterval(timer); timer = null; },
    /** A station was picked on the map: mark it and show its row. */
    pick(id) {
      focus = id;
      drawList();
      root.querySelector(`[data-station="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest' });
    },
    get state() { return state; },
    refresh,
  };
}
