// Entry point: loads the scene, wires map, camera window, search, place card and the panel,
// then runs one animation loop that only updates what moves.
import './style.css';
import { loadScene } from './scene/scene.js';
import { staticLayers } from './scene/staticLayers.js';
import { overlayLayers } from './scene/overlays.js';
import { Live } from './scene/live.js';
import { createMap } from './map/view.js';
import { createCameraView } from './map/cameraView.js';
import { createSearch } from './map/search.js';
import { createPlaceCard, describe } from './map/placeCard.js';
import { createPanel, clock, esc, setClockMode } from './ui/panel.js';
import { createState } from './state.js';
import qrcode from 'qrcode-generator';
import * as api from './api.js';

const $ = (id) => document.getElementById(id);

const scene = await loadScene();
const S = createState(scene);
const live = new Live(scene);

// ---- layer menu ----
const LAYERS = [
  ['buildings', 'Здания в 3D'], ['traffic', 'Транспортный поток'], ['transit', 'Остановки'],
  ['cameras', 'Камеры'], ['infra', 'Инфраструктура'], ['pois', 'Службы и больницы'], ['labels', 'Названия улиц'],
];
const show = Object.fromEntries(LAYERS.map(([k]) => [k, true]));
let base = staticLayers(scene, show);
let camBase = null;
$('layers-menu').innerHTML = LAYERS.map(([k, label]) => `<label><input type="checkbox" data-layer="${k}" checked> ${label}</label>`).join('');
$('layers-menu').onchange = (e) => {
  show[e.target.dataset.layer] = e.target.checked;
  base = staticLayers(scene, show);
  camBase = null;
};
$('layers-btn').onclick = () => { $('layers-menu').hidden = !$('layers-menu').hidden; };

// ---- map, camera window, place card, search, panel ----
const card = createPlaceCard({
  onClose: () => { S.pin = null; },
  onAction: (action) => panel.setTab(action.tab),
});

function select(kind, object, lonlat) {
  const info = describe(kind, object, S);
  if (!info) return;
  S.pin = lonlat;
  card.show(info);
}

const { map, overlay, flyTo } = createMap(scene, {
  onClick: ({ object, layer, coordinate }) => {
    if (!object || !layer) { card.hide(); S.pin = null; return; }
    if (layer.id === 'cams') { panel.setTab('cameras'); cameras.open(object.id); return; }
    select(layer.id, object, coordinate ? [coordinate[0], coordinate[1]] : null);
  },
});

// facility names only when zoomed in, so the overview stays clean
map.on('zoom', () => {
  const near = map.getZoom() >= 15.8;
  if (near !== (show.poiLabels === true)) { show.poiLabels = near; base = staticLayers(scene, show); }
});

const cameras = createCameraView(scene, { onChange: (id) => { S.cam = id; panel.render(); } });
const panel = createPanel(scene, S, {
  onFly: (junction) => {
    const target = scene.cams.find((c) => c.id === junction) ?? scene.detectors.find((d) => d.id === junction);
    if (!target) return;
    flyTo(target.p, 17);
    select('detectors', target, target.p);
  },
  onCamera: (id) => (id === S.cam ? cameras.close() : cameras.open(id)),
  onTab: (tab) => { clearInterval(historyTimer); if (tab === 'history') { loadHistory(); historyTimer = setInterval(loadHistory, 20000); } },
  onAction: (act, id) => run(async () => {
    if (act === 'revoke' || act === 'restore') return api.post(`/api/devices/${id}/status`, { status: act === 'revoke' ? 'revoked' : 'active' });
    if (act === 'approve-group' || act === 'reject-group') {
      const results = await api.post('/api/commands/group', { ids: id.split(','), approve: act === 'approve-group' });
      const failed = Object.values(results).filter((s) => !s).length;
      if (failed) toast(`${failed} из ${Object.keys(results).length} не удалось: устройство не на связи или решение уже принято`);
      return results;
    }
    return api.post(`/api/commands/${id}/${act}`);
  }),
});

// history comes from the store, not from live frames: fetched when the tab is open
let historyTimer = null;
async function loadHistory() {
  if (!S.user) return;
  try {
    const [h, commands] = await Promise.all([api.get('/api/history'), api.get('/api/history/commands?limit=40')]);
    S.history = { ...h, commands };
    panel.render();
  } catch (e) { if (e.message === 'нужно войти заново') signedOut(); }
}

// ---- accounts: everything needs a sign-in; what the buttons do depends on the role ----
const ROLE_TEXT = { viewer: 'наблюдатель', operator: 'оператор', admin: 'администратор' };
function toast(text) {
  let el = $('toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'glass'; document.body.append(el); }
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
}
async function run(action) {
  try { await action(); } catch (e) { toast(e.message); if (e.message === 'нужно войти заново') signedOut(); }
}
function renderOperator() {
  $('op-tools').hidden = !S.user;
  if (S.user) {
    $('op-user').textContent = `${S.user.name} · ${ROLE_TEXT[S.user.role] ?? S.user.role}${S.user.mfa_setup ? ' · нужен второй фактор' : ''}`;
    $('op-user').className = `chip ${S.user.mfa_setup ? 'warn' : S.user.role === 'viewer' ? 'info' : 'ok'}`;
  }
  $('op-link').hidden = $('op-revoke').hidden = !S.can('operator');
  $('op-users').textContent = S.can('admin') ? 'Учётные записи' : 'Мой пароль';
  const link = $('link');
  link.textContent = S.link ? 'связь с оборудованием' : 'связь отключена';
  link.className = `chip ${S.link ? 'ok' : 'bad'}`;
  $('op-link').textContent = S.link ? 'Отключить связь' : 'Восстановить связь';
  $('op-link').classList.toggle('on', !S.link);
  panel.render();
}
function renderMode() {
  const field = S.mode === 'field';
  const chip = $('modechip');
  chip.textContent = field ? 'ОБОРУДОВАНИЕ' : 'ЭМУЛЯЦИЯ';
  chip.className = `chip ${field ? 'ok' : 'shadow'}`;
  chip.title = field ? 'Платформа работает с реальными устройствами URBAN Edge' : 'Оборудование в демо смоделировано';
  $('foot-note').textContent = field
    ? 'Режим оборудования: очереди и состояние шкафов приходят от устройств URBAN Edge; трафик на карте не показывается (видеопотока нет); сеть и здания из OSM.'
    : 'Демо на синтетических данных: трафик, автобусные линии, телеметрия, камеры и сами светофоры смоделированы; сеть и здания из OSM.';
}
function signedOut() {
  S.user = null;
  if (ws) { const w = ws; ws = null; w.close(); }
  $('users').hidden = true;
  $('login').hidden = false;
  $('login-pass').value = '';
  $('login-code').value = '';
  $('login-code-row').hidden = true;
  $('login-name').focus();
  renderOperator();
}
function afterSignIn() {
  if (!S.user?.mfa_setup) return;
  toast('Для вашей роли нужен второй фактор: пока он не включён, права только на просмотр');
  $('users').hidden = false;
  renderUsers();
}
$('login-form').onsubmit = async (e) => {
  e.preventDefault();
  $('login-err').textContent = '';
  try {
    S.user = await api.login($('login-name').value.trim(), $('login-pass').value, $('login-code').value.trim());
  } catch (err) {
    $('login-err').textContent = err.message;
    if (err.needCode) { $('login-code-row').hidden = false; $('login-code').focus(); }
    $('login-code').value = '';
    return;
  }
  $('login').hidden = true;
  $('login-pass').value = '';
  $('login-code').value = '';
  $('login-code-row').hidden = true;
  renderOperator();
  connect();
  afterSignIn();
};
$('op-logout').onclick = async () => { await api.logout(); signedOut(); };
$('op-revoke').onclick = () => run(() => api.post('/api/revoke-all'));
$('op-link').onclick = () => run(() => api.post('/api/link', { up: !S.link }));

// accounts dialog: admins manage everyone, others change their own password
const MFA_TEXT = 'Одноразовые коды из приложения-аутентификатора (Google Authenticator, Aegis, FreeOTP и подобные).';
function mfaSection() {
  if (S.user.mfa) return `<h4>Второй фактор</h4><p class="muted">Включён. ${MFA_TEXT} Потеряли телефон — администратор сбросит.</p>`;
  return `<form data-form="mfa-start"><h4>Второй фактор</h4>
      <p class="muted">${S.user.mfa_setup ? '<b>Для вашей роли обязателен.</b> ' : ''}${MFA_TEXT}</p>
      <label>Текущий пароль <input name="current" type="password" autocomplete="current-password" required /></label>
      <button type="submit">Включить</button></form>`;
}
function mfaEnrol(started) {
  const qr = qrcode(0, 'M');
  qr.addData(started.uri);
  qr.make();
  const secret = started.secret.replace(/(.{4})/g, '$1 ').trim();
  return `<form data-form="mfa-confirm"><h4>Второй фактор</h4>
      <p class="muted">Отсканируйте код приложением или введите ключ вручную, затем введите 6 цифр из приложения.</p>
      <div class="qr">${qr.createSvgTag(4, 8)}</div>
      <p><code class="secret">${esc(secret)}</code></p>
      <label>Код из приложения <input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" required /></label>
      <button type="submit">Подтвердить</button></form>`;
}
async function renderUsers() {
  const body = $('users-body');
  const own = `${mfaSection()}<form data-form="password"><h4>Мой пароль</h4>
      <label>Текущий пароль <input name="current" type="password" autocomplete="current-password" required /></label>
      <label>Новый пароль (от 10 символов) <input name="password" type="password" autocomplete="new-password" required minlength="10" /></label>
      <button type="submit">Сменить пароль</button></form>`;
  if (!S.can('admin')) { body.innerHTML = own; return; }
  let users = [];
  try { users = await api.get('/api/users'); } catch (e) { body.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  body.innerHTML = `<p class="muted">Наблюдатель смотрит, оператор решает, администратор ещё управляет устройствами и учётными записями.
      Пароль хранится как scrypt-хэш в <code>data/users.json</code>; то же можно делать из консоли: <code>python -m urban.users</code>.</p>
    ${users.map((u) => `<div class="item row ${u.enabled ? '' : 'off'}">
      <span><b>${esc(u.name)}</b>${chipHtml(u.role)}${u.enabled ? '' : chipHtml('off')}${u.mfa ? '<span class="chip ok">2FA</span>' : u.mfa_required ? '<span class="chip warn">нет 2FA</span>' : ''}<small>с ${esc(u.created)}</small></span>
      <span class="acts">
        ${u.mfa ? `<button data-mfa-reset="${esc(u.name)}" title="Потерян телефон: при следующем входе второй фактор настраивается заново">Сбросить 2FA</button>` : ''}
        <button data-reset="${esc(u.name)}">Пароль</button>
        ${u.name === S.user.name ? '' : `<button data-enable="${esc(u.name)}" data-to="${u.enabled ? 0 : 1}">${u.enabled ? 'Отключить' : 'Включить'}</button>`}
      </span></div>`).join('')}
    <form data-form="create"><h4>Новая учётная запись</h4>
      <label>Имя <input name="name" required maxlength="40" pattern="[A-Za-z0-9._\\-]+" autocomplete="off" /></label>
      <label>Пароль (от 10 символов) <input name="password" type="password" autocomplete="new-password" required minlength="10" /></label>
      <label>Роль <select name="role"><option value="viewer">наблюдатель</option><option value="operator" selected>оператор</option><option value="admin">администратор</option></select></label>
      <button type="submit">Добавить</button></form>
    ${own}`;
}
const chipHtml = (role) => (role === 'off' ? '<span class="chip bad">отключена</span>'
  : `<span class="chip ${role === 'viewer' ? 'info' : 'ok'}">${ROLE_TEXT[role] ?? esc(role)}</span>`);
$('op-users').onclick = () => { $('users').hidden = !$('users').hidden; if (!$('users').hidden) renderUsers(); };
$('users-close').onclick = () => { $('users').hidden = true; };
$('users-body').onsubmit = (e) => {
  e.preventDefault();
  const form = e.target, data = Object.fromEntries(new FormData(form));
  run(async () => {
    if (form.dataset.form === 'mfa-start') {
      const started = await api.post('/api/me/mfa/start', { current: data.current });
      form.outerHTML = mfaEnrol(started);
      $('users-body').querySelector('[data-form=mfa-confirm] input').focus();
      return;
    }
    if (form.dataset.form === 'mfa-confirm') {
      await api.post('/api/me/mfa/confirm', { code: data.code });
      toast('второй фактор включён, войдите заново с кодом');
      signedOut();
      return;
    }
    if (form.dataset.form === 'create') {
      await api.post('/api/users', data);
      toast(`учётная запись ${data.name} создана`);
    } else {
      await api.post(`/api/users/${encodeURIComponent(S.user.name)}/password`, { password: data.password, current: data.current });
      toast('пароль изменён, войдите заново');
      signedOut();
      return;
    }
    renderUsers();
  });
};
$('users-body').onclick = (e) => {
  const reset = e.target.closest('[data-reset]'), enable = e.target.closest('[data-enable]');
  const mfaReset = e.target.closest('[data-mfa-reset]');
  if (mfaReset) {
    run(async () => {
      await api.post(`/api/users/${encodeURIComponent(mfaReset.dataset.mfaReset)}/mfa-reset`);
      toast(`второй фактор ${mfaReset.dataset.mfaReset} сброшен`);
      renderUsers();
    });
    return;
  }
  if (reset) {
    const row = reset.closest('.item');
    if (row.querySelector('form')) return;
    row.insertAdjacentHTML('beforeend', `<form data-form="reset" data-name="${reset.dataset.reset}">
      <label>Новый пароль для ${esc(reset.dataset.reset)} <input name="password" type="password" autocomplete="new-password" required minlength="10" /></label>
      <button type="submit">Сбросить</button></form>`);
    row.querySelector('input').focus();
    row.querySelector('form').onsubmit = (ev) => {
      ev.preventDefault();
      run(async () => {
        await api.post(`/api/users/${encodeURIComponent(reset.dataset.reset)}/password`, { password: ev.target.password.value });
        toast(`пароль ${reset.dataset.reset} сброшен`);
        renderUsers();
      });
    };
  } else if (enable) {
    run(async () => {
      await api.post(`/api/users/${encodeURIComponent(enable.dataset.enable)}/enabled`, { enabled: enable.dataset.to === '1' });
      renderUsers();
    });
  }
};

createSearch(scene, {
  getAssets: () => S.infra,
  onSelect: (entry) => {
    flyTo(entry.p, entry.zoom);
    if (entry.type === 'cam') { panel.setTab('cameras'); cameras.open(entry.ref.id); S.pin = null; card.hide(); return; }
    const kind = { poi: 'pois', asset: 'infra', street: 'street' }[entry.type];
    select(kind, entry.ref ?? entry, entry.p);
  },
});

window.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape') return;
  if (card.open) { card.hide(); S.pin = null; } else if (S.cam) cameras.close();
});

// ---- render loop ----
let frames = 0, fpsAt = performance.now(), busy = 0;
function frame(now) {
  const started = performance.now();
  live.update(now);
  const pulse = 0.5 + 0.5 * Math.sin(now / 300);
  overlay.setProps({
    layers: [...base, ...overlayLayers(scene, S, show, { main: true, pulse }), ...live.layers(show.traffic)],
  });
  const camOverlay = cameras.overlay;
  if (camOverlay) {
    camBase ??= staticLayers(scene, { ...show, labels: false }, { pickable: false }).filter((l) => l.id !== 'rim');
    camOverlay.setProps({
      layers: [...camBase, ...overlayLayers(scene, S, show, { main: false, pulse }), ...live.layers(show.traffic)],
    });
  }
  frames++;
  busy += performance.now() - started;
  if (now - fpsAt >= 1000) {
    $('perf').textContent = `${Math.round((frames * 1000) / (now - fpsAt))} к/с · ${(busy / frames).toFixed(2)} мс на кадр · ${live.n} ТС`;
    frames = 0; busy = 0; fpsAt = now;
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---- live data ----
function onFrame(f) {
  live.ingest(f.v);
  S.ingest(f);
  renderOperator();
  if (S.cam) {
    const [n, speed, queue] = S.cams[S.cam] ?? [0, 0, 0];
    cameras.setHud(`${clock(S.t)}${n == null ? '' : ` · ТС в кадре: ${n}`} · очередь: ${queue == null ? 'нет данных' : `${queue} м`}${speed == null ? '' : ` · ср. скорость: ${speed} км/ч`} · обезличено`);
  }
}

// the live socket: the first message carries the session token, a stale one closes it with 4401
let ws = null;
function connect() {
  if (ws || !S.user) return;
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = async () => {
    ws.send(JSON.stringify({ token: api.wsToken() }));
    try {
      const snap = await api.get('/api/state');
      S.mode = snap.mode ?? 'simulation';
      setClockMode(S.mode === 'field');
      renderMode();
      S.applySnapshot(snap);
    } catch { /* the socket will tell us if the session is gone */ }
    renderOperator();
  };
  ws.onmessage = (m) => onFrame(JSON.parse(m.data));
  ws.onclose = (e) => {
    ws = null;
    if (e.code === 4401) signedOut();
    else if (S.user) setTimeout(connect, 1500);
  };
}
panel.setTab('overview');
S.user = await api.whoami();
if (S.user) connect(); else signedOut();
renderOperator();
afterSignIn();
