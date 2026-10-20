// Module «Уличное освещение»: the module switch and the lighting panel.
// Everything that decides what is on the screen is a pure function (data -> HTML string), so the gate
// (tools/gates/web-modules.mjs) can run it in node. createLighting() is the thin DOM part around them: it polls
// GET /api/lighting/state, keeps the operator's form while the list refreshes, and sends override / release.
// This file imports nothing, so node can load it as is.

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
const chip = (cls, text) => `<span class="chip ${cls}">${esc(text)}</span>`;
const plural = (n, one, few, many) => (n % 10 === 1 && n % 100 !== 11 ? one
  : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many);

// ---- module switch ----------------------------------------------------------------------------------
// The modules this interface has a screen for, with the title used when the server did not send one.
export const UI_MODULES = { traffic: 'Светофоры и пробки', lighting: 'Уличное освещение' };

/** /api/modules answer -> [{id, title}] for the modules that have a screen here, in the server's order. */
export function normalizeModules(raw) {
  const list = Array.isArray(raw) ? raw : Array.isArray(raw?.modules) ? raw.modules : [];
  const seen = new Set();
  const out = [];
  for (const m of list) {
    const id = typeof m === 'string' ? m : m?.id;
    if (typeof id !== 'string' || !(id in UI_MODULES) || seen.has(id)) continue;
    seen.add(id);
    const title = typeof m?.title === 'string' && m.title.trim() ? m.title.trim() : UI_MODULES[id];
    out.push({ id, title });
  }
  return out;
}

/** The switch. Empty when there is nothing to switch between (a lone traffic module: the interface stays as it was). */
export function renderModuleSwitch(modules, active = 'traffic') {
  const list = normalizeModules(modules);
  if (list.length < 2) return '';
  return `<div class="modsw-box" role="tablist" aria-label="Службы города">${list.map((m) =>
    `<button role="tab" data-module="${esc(m.id)}" aria-selected="${m.id === active}" class="${m.id === active ? 'sel' : ''}">${esc(m.title)}</button>`).join('')}</div>`;
}

// ---- one cabinet -------------------------------------------------------------------------------------
const hhmm = (sec) => new Date(sec * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
const DOOR = { closed: 'дверь закрыта', open: 'дверь открыта' };
const SCHEDULE = { astro: 'расписание: по солнцу', photocell: 'расписание: фотореле' };

/** Why a cabinet needs attention: [{level: 'bad'|'warn', text}], from its own report fields and the server's alerts. */
export function cabinetProblems(c) {
  const out = [];
  if (c.online === false) out.push({ level: 'bad', text: 'шкаф не на связи' });
  if (Number(c.failed) > 0) {
    const n = Number(c.failed);
    out.push({ level: 'warn', text: `${n} ${plural(n, 'лампа перегорела', 'лампы перегорели', 'ламп перегорело')}` });
  }
  if (c.door === 'open') out.push({ level: 'warn', text: 'дверь открыта' });
  if (c.refused) out.push({ level: 'warn', text: `команда отклонена: ${c.refused}` });
  return out;
}
export const isAlert = (c) => cabinetProblems(c).length > 0 || (Array.isArray(c.alerts) && c.alerts.length > 0);
/** Colour class of a cabinet on the map and in the list: bad (offline), warn (needs a visit), on, off. */
export function cabinetTone(c) {
  const problems = cabinetProblems(c);
  if (problems.some((p) => p.level === 'bad')) return 'bad';
  if (problems.length || (Array.isArray(c.alerts) && c.alerts.length)) return 'warn';
  return c.on ? 'on' : 'off';
}

const overrideUntil = (c) => c.override?.until ?? c.lease_until ?? null;
export function modeText(c) {
  const overridden = c.mode === 'override' || !!c.override;
  const until = overrideUntil(c);
  if (!overridden) return 'по расписанию';
  return until ? `команда центра до ${hhmm(until)}` : 'команда центра';
}
const levelText = (c) => (c.on && c.level > 0 ? `включён, ${Math.round(c.level)} %` : 'выключен');
const lampsText = (c) => {
  const n = Number(c.lamps);
  if (!Number.isFinite(n)) return 'лампы: нет данных';
  const f = Number(c.failed) || 0;
  return `${n} ${plural(n, 'лампа', 'лампы', 'ламп')}${f ? `, ${f} ${plural(f, 'перегорела', 'перегорели', 'перегорело')}` : ''}`;
};
const powerText = (c) => (Number.isFinite(Number(c.power_w)) && c.power_w != null ? `${(c.power_w / 1000).toFixed(2)} кВт` : '— кВт');
const ageText = (s) => (s == null ? '' : s < 60 ? `${Math.round(s)} с` : `${Math.floor(s / 60)} мин`);

export function renderCabinet(c, { canOperate = false, selected = new Set(), focus = null } = {}) {
  const id = c.cabinet ?? c.id;
  const problems = cabinetProblems(c);
  const alert = isAlert(c);
  const serverText = (Array.isArray(c.alerts) ? c.alerts : []).map((a) => (Array.isArray(a) ? a[1] : typeof a === 'string' ? a : a?.text))
    .filter((t) => t && !problems.some((p) => p.text === t));
  const o = c.override;
  return `<div class="item lt-cab${alert ? ' alert' : ''}${focus === id ? ' focus' : ''}" data-cabinet="${esc(id)}"${alert ? ' data-alert="1"' : ''}>
    <div class="row"><span>${canOperate
      ? `<input type="checkbox" data-lt-sel="${esc(id)}" ${selected.has(id) ? 'checked' : ''} aria-label="Выбрать шкаф ${esc(id)}"> ` : ''}<b>${esc(id)}</b></span>
      <span>${c.online === false ? chip('bad', `нет связи${c.age != null ? `, ${ageText(c.age)}` : ''}`) : chip('ok', 'на связи')}</span></div>
    <div class="row meta"><span class="lt-mode${c.mode === 'override' || o ? ' ov' : ''}">${esc(modeText(c))}</span><span>${esc(levelText(c))}</span></div>
    <div class="row meta"><span>${esc(lampsText(c))}</span><span>${esc(powerText(c))}</span><span>${esc(DOOR[c.door] ?? 'дверь: нет данных')}</span></div>
    ${o && (o.reason || o.actor) ? `<small>${o.actor ? esc(o.actor) : ''}${o.reason ? `${o.actor ? ': ' : ''}«${esc(o.reason)}»` : ''}</small>` : ''}
    ${c.schedule && !o ? `<small>${esc(SCHEDULE[c.schedule] ?? '')}${c.dark === true ? ', сейчас темно' : c.dark === false ? ', сейчас светло' : ''}</small>` : ''}
    ${problems.map((p) => `<div class="lt-alert ${p.level}">${chip(p.level, p.text)}</div>`).join('')}
    ${serverText.map((t) => `<div class="lt-alert warn">${chip('warn', t)}</div>`).join('')}
    <button class="lt-fly" data-lt-fly="${esc(id)}" ${Number.isFinite(c.lon) && Number.isFinite(c.lat) ? '' : 'hidden'}>На карте</button>
  </div>`;
}

// ---- the list ------------------------------------------------------------------------------------------
const kpi = (label, value, cls = '') => `<div class="kpi ${cls}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;

export function renderLightingList(state, opts = {}) {
  if (!state || !Array.isArray(state.cabinets)) return `<p class="muted">${esc(opts.error || 'загрузка состояния освещения…')}</p>`;
  const cabs = state.cabinets;
  if (!cabs.length) return '<p class="muted">Шкафов освещения пока нет. Шкаф подключается так же, как блок перекрёстка: <code>python -m urban.provision enroll-lighting</code>.</p>';
  const online = cabs.filter((c) => c.online !== false).length;
  const alerts = cabs.filter(isAlert).length;
  const kw = cabs.filter((c) => c.online !== false).reduce((s, c) => s + (Number(c.power_w) || 0), 0) / 1000;
  const overridden = cabs.filter((c) => c.mode === 'override' || c.override).length;
  // alerts first, then offline, then by id: what needs a person is on top
  const rows = [...cabs].sort((a, b) => (isAlert(b) - isAlert(a)) || String(a.cabinet ?? a.id).localeCompare(String(b.cabinet ?? b.id), 'ru', { numeric: true }));
  return `<div class="grid">${kpi('шкафов на связи', `${online} / ${cabs.length}`, online < cabs.length ? 'warn' : '')}${kpi('тревог', alerts, alerts ? 'warn' : '')}
      ${kpi('потребление, кВт', kw.toFixed(2))}${kpi('под командой центра', overridden)}</div>
    ${opts.error ? `<p class="err">${esc(opts.error)}</p>` : ''}
    <h4>Шкафы</h4>${rows.map((c) => renderCabinet(c, opts)).join('')}`;
}

// ---- operator controls --------------------------------------------------------------------------------
export const SAFETY_TEXT = 'Команда действует ограниченное время; без связи шкаф сам вернётся к расписанию.';
export const DEFAULT_FORM = { level: 50, minutes: 60, reason: '' };

export function renderControls({ canOperate = false, form = DEFAULT_FORM } = {}) {
  if (!canOperate) return '<p class="muted">Управлять освещением могут оператор и администратор; наблюдатель только смотрит.</p>';
  return `<h4>Управление</h4>
    <form class="lt-form" data-lt-form autocomplete="off">
      <p class="muted" data-lt-count>Отметьте шкафы в списке.</p>
      <div class="btns"><button type="button" data-lt-all>Выбрать все</button><button type="button" data-lt-none>Снять выбор</button></div>
      <label>Уровень света: <output data-lt-out>${esc(form.level)}</output> %
        <input type="range" name="level" min="0" max="100" step="5" value="${esc(form.level)}"></label>
      <div class="btns"><button type="button" data-lt-preset="0">Выключить</button><button type="button" data-lt-preset="50">50 %</button><button type="button" data-lt-preset="100">100 %</button></div>
      <label>На сколько минут (1–720) <input name="minutes" type="number" min="1" max="720" step="1" value="${esc(form.minutes)}" required></label>
      <label>Причина (обязательно) <input name="reason" maxlength="200" value="${esc(form.reason)}" required placeholder="например: ремонт линии на Навои"></label>
      <div class="btns"><button type="submit" class="go" data-lt-apply>Применить</button><button type="button" data-lt-release>Вернуть расписание</button></div>
      <small class="err" data-lt-err></small>
    </form>`;
}

/** The whole lighting panel. The list sits in its own element, so refreshes replace only that and keep the form. */
export function renderLightingPanel(state, { canOperate = false, selected = new Set(), form = DEFAULT_FORM, focus = null, error = '' } = {}) {
  return `<h3>Уличное освещение</h3>
    <div data-lt="list">${renderLightingList(state, { canOperate, selected, focus, error })}</div>
    <div data-lt="controls">${renderControls({ canOperate, form })}</div>
    <div class="item lt-safety">${chip('ok', 'аренда')} ${esc(SAFETY_TEXT)}</div>`;
}

/** Check the operator's form. Returns {ok, error, body} - body is what POST /api/lighting/override takes. */
export function overrideRequest(form, cabinets) {
  if (!cabinets.length) return { ok: false, error: 'Выберите хотя бы один шкаф.' };
  const level = Number(form.level), minutes = Number(form.minutes), reason = String(form.reason ?? '').trim();
  if (!Number.isInteger(level) || level < 0 || level > 100) return { ok: false, error: 'Уровень от 0 до 100 %.' };
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 720) return { ok: false, error: 'Срок от 1 до 720 минут.' };
  if (!reason) return { ok: false, error: 'Укажите причину: она попадёт в журнал.' };
  return { ok: true, body: { cabinets, level, minutes, reason } };
}

// ---- the DOM part ----------------------------------------------------------------------------------------
/**
 * Wires the panel into `root` (the #panel element). `api` is {get, post}; `canOperate()` says whether the signed-in
 * role may command; `onData(state)` hands the state to the map layer; `onFocus(cabinet)` flies the map to a cabinet.
 */
export function createLighting({ root, api, canOperate, onData = () => {}, onFocus = () => {}, onAuthLost = () => {}, toast = () => {}, every = 5000 }) {
  let state = null, error = '', active = false, timer = null, focus = null, builtFor = null;
  const selected = new Set();
  const form = { ...DEFAULT_FORM };
  const part = (name) => root.querySelector(`[data-lt="${name}"]`);

  const drawList = () => {
    const el = part('list');
    if (!el) return;
    const html = renderLightingList(state, { canOperate: canOperate(), selected, focus, error });
    if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
    const count = root.querySelector('[data-lt-count]');
    if (count) count.textContent = selected.size ? `Выбрано шкафов: ${selected.size}` : 'Отметьте шкафы в списке.';
  };
  const build = () => {
    builtFor = canOperate();
    root.innerHTML = renderLightingPanel(state, { canOperate: builtFor, selected, form, focus, error });
    part('list').dataset.html = renderLightingList(state, { canOperate: builtFor, selected, focus, error });
  };
  const setState = (s) => {
    state = s;
    const ids = new Set((s.cabinets ?? []).map((c) => c.cabinet ?? c.id));
    for (const id of [...selected]) if (!ids.has(id)) selected.delete(id);
    onData(s);
  };
  const failed = (e) => {
    if (e.message === 'нужно войти заново') onAuthLost();
    return e.message;
  };
  async function refresh() {
    if (!active || (typeof document !== 'undefined' && document.hidden)) return;
    try { setState(await api.get('/api/lighting/state')); error = ''; } catch (e) { error = `Состояние освещения не получено: ${failed(e)}`; }
    if (active) drawList();
  }
  const formError = (text) => { const el = root.querySelector('[data-lt-err]'); if (el) el.textContent = text; };
  const readForm = () => {
    const f = root.querySelector('[data-lt-form]');
    if (f) Object.assign(form, { level: f.level.value, minutes: f.minutes.value, reason: f.reason.value });
    return form;
  };
  async function send(path, body, done) {
    formError('');
    try {
      const s = await api.post(path, body);
      if (s?.cabinets) { setState(s); error = ''; } else await refresh();
      toast(done);
      drawList();
    } catch (e) { formError(failed(e)); }
  }

  root.addEventListener('click', (e) => {
    if (!active) return;
    const t = e.target;
    const preset = t.closest?.('[data-lt-preset]');
    if (preset) {
      const f = root.querySelector('[data-lt-form]');
      f.level.value = preset.dataset.ltPreset;
      f.querySelector('[data-lt-out]').textContent = f.level.value;
      return;
    }
    if (t.closest?.('[data-lt-all]')) { for (const c of state?.cabinets ?? []) selected.add(c.cabinet ?? c.id); drawList(); return; }
    if (t.closest?.('[data-lt-none]')) { selected.clear(); drawList(); return; }
    if (t.closest?.('[data-lt-release]')) {
      const cabinets = [...selected];
      if (!cabinets.length) return formError('Выберите хотя бы один шкаф.');
      send('/api/lighting/release', { cabinets }, `Расписание возвращено: ${cabinets.join(', ')}`);
      return;
    }
    const fly = t.closest?.('[data-lt-fly]');
    if (fly) { focus = fly.dataset.ltFly; onFocus(focus); drawList(); }
  });
  root.addEventListener('change', (e) => {
    if (!active) return;
    const box = e.target.closest?.('[data-lt-sel]');
    if (!box) return;
    if (box.checked) selected.add(box.dataset.ltSel); else selected.delete(box.dataset.ltSel);
    drawList();
  });
  root.addEventListener('input', (e) => {
    if (!active) return;
    const f = e.target.form;
    if (e.target.name === 'level' && f) f.querySelector('[data-lt-out]').textContent = e.target.value;
    readForm();
  });
  root.addEventListener('submit', (e) => {
    if (!active || !e.target.matches?.('[data-lt-form]')) return;
    e.preventDefault();
    const r = overrideRequest(readForm(), [...selected]);
    if (!r.ok) return formError(r.error);
    const text = r.body.level === 0 ? 'выключено' : `${r.body.level} %`;
    send('/api/lighting/override', r.body, `Команда отправлена: ${r.body.cabinets.join(', ')}, ${text}, ${r.body.minutes} мин`);
  });

  return {
    start() {
      active = true;
      error = '';
      build();
      refresh();
      clearInterval(timer);
      timer = setInterval(refresh, every);
    },
    stop() { active = false; clearInterval(timer); timer = null; },
    /** The role changed (sign-in, 2FA): rebuild the controls. Cheap to call on every frame. */
    sync() { if (active && builtFor !== canOperate()) build(); },
    /** A cabinet was picked on the map: mark it, tick it for the operator, show its row. */
    pick(cabinet) {
      focus = cabinet;
      if (canOperate()) selected.add(cabinet);
      drawList();
      root.querySelector(`[data-cabinet="${CSS.escape(cabinet)}"]`)?.scrollIntoView({ block: 'nearest' });
    },
    get state() { return state; },
    refresh,
  };
}
