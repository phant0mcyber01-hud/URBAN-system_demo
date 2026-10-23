// Control-centre side panel: one renderer per module tab. The DOM is only touched when the
// generated markup actually changes, so hover and scroll state survive the 1 Hz updates.
const $ = (id) => document.getElementById(id);
export const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
// scenario time as m:ss in simulation; wall-clock time of day in field mode
let wallClock = false;
export const setClockMode = (field) => { wallClock = field; };
export const clock = (t) => (wallClock
  ? new Date(t * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  : `${Math.floor(t / 60)}:${String(Math.floor(t) % 60).padStart(2, '0')}`);
const chip = (cls, text) => `<span class="chip ${cls}">${text}</span>`;
const tag = (text) => `<span class="tag">${text}</span>`;  // what kind of record: a label, not a status
const kpi = (label, value, cls = '') => `<div class="kpi ${cls}"><b>${value}</b><span>${label}</span></div>`;
const empty = (text) => `<p class="muted">${text}</p>`;
const KIND_RU = { heat: 'температура', door: 'дверь', controller: 'контроллер', offline: 'нет связи', silent: 'молчит',
  version: 'версия ПО', revoked: 'отозвано' };
const UNIT_TEXT = { en_route: 'в пути', arrived: 'на месте' };
const STATUS_CHIP = { ok: ['ok', 'норма'], warn: ['warn', 'отклонение'], alert: ['bad', 'порог'] };
const CMD_STATUS = {
  pending: ['warn', 'ждёт решения'], approved: ['info', 'отправляется'], active: ['ok', 'применено'],
  revoking: ['warn', 'отзывается'], done: ['info', 'завершено'], rejected: ['info', 'отклонено'],
  expired: ['info', 'не рассмотрено'], failed: ['bad', 'отказ оборудования'],
};
const END_REASON = {
  expired: 'связь пропала, светофор вернулся к своему плану сам',
  window: 'окно подтверждения закончилось, возврат к базовому плану',
  revoked: 'отозвано оператором, возврат к базовому плану',
  released: 'возврат к базовому плану', restart: 'перезапуск сценария',
  relieved: 'очередь ушла, платформа сама вернула базовый план',
  balance: 'начало загружаться направление, отдававшее зелёный: базовый план возвращён',
  passed: 'экстренная машина проехала перекрёсток, возврат к базовому плану',
  arrived: 'экстренная машина на месте, коридор снят',
  corridor: 'уступил место зелёному коридору для экстренной машины',
};

const PASSPORT = { approved: ['ok', 'утверждён'], draft: ['warn', 'черновик'] };
const DEMO_ONLY = new Set(['cameras', 'emergency', 'infra']);  // the twin's scenarios: no real source behind them yet
const when = (at) => (at ? new Date(at * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—');
const RUN_MODE = { simulation: 'симуляция', field: 'оборудование' };
// the copy of the audit log outside this server: without one, a rebuilt chain cannot be told from the real one
const exportItem = (e) => {
  if (!e || !e.sinks.length) {
    return `<div class="item">${chip('warn', 'нет внешней копии')} Журнал хранится только на этом сервере.
      <br><small>Задайте <code>AUDIT_EXPORT</code> (файл на другом диске) или <code>AUDIT_SYSLOG</code> (сборщик журналов) в <code>.env</code>.</small></div>`;
  }
  const bad = e.sinks.filter((s) => s.error);
  return `<div class="item">${chip(bad.length ? 'bad' : 'ok', bad.length ? 'копия отстаёт' : 'внешняя копия')}
    ${e.sinks.map((s) => `<code>${esc(s.name)}</code>: ${s.error ? esc(s.error) : `до записи #${s.seq ?? '…'}`}`).join('<br>')}
    <br><small>${e.checkpoint ? `Последняя подписанная контрольная точка: запись #${e.checkpoint.seq}, ${when(e.checkpoint.at)}.` : 'Подписанных контрольных точек ещё нет.'}
    Проверка копии: <code>python -m urban.export verify</code>.</small></div>`;
};

export function createPanel(scene, S, { onCamera, onAction, onFly, onTab, beforeTab }) {
  const shadowList = (filter, n = 6) => {
    const list = S.shadow.filter(filter).slice(-n).reverse();
    return list.length ? list.map((p) => `<div class="item">${chip('shadow', 'ТОЛЬКО ЗАПИСЬ')} <code>${esc(p.command)}</code>
      <div>${esc(p.target)}</div><small>${esc(p.note)} · не отправляется</small></div>`).join('') : empty('нет предложений');
  };
  const jamStreets = () => {
    const count = new Map();
    for (const r of S.jamRoads) count.set(r.name || 'без названия', (count.get(r.name || 'без названия') ?? 0) + 1);
    return [...count.entries()].sort((a, b) => b[1] - a[1]);
  };

  // URBAN Edge units: the registry with live health, one row per junction; district servers have rows of their own
  const waiting = (d) => !d.o && !d.f && d.s !== 'revoked';  // enrolled, never connected with this key
  const isDistrict = (d) => d.k === 'district';
  // junction units and district servers are counted apart: a district is not a «блок» of any junction
  const split = (list) => ({ units: list.filter((d) => !isDistrict(d)), districts: list.filter(isDistrict) });
  const onlineOf = (list) => list.filter((d) => d.o).length;
  const districtRow = (d) => `<div class="item dev">
        <div class="row"><span>${tag('районный сервер')} <b>${esc(d.id)}</b> <small>${esc(d.addr ?? '')}${d.serial ? ` · ${esc(d.serial)}` : ''}</small></span>${devState(d)}</div>
        <div class="row meta">
          <span title="блоков на связи через него">${d.r && d.r.units != null ? `${esc(d.r.units)} ${plural(Number(d.r.units) || 0, 'блок', 'блока', 'блоков')} через него` : 'ретранслятор: нет данных'}</span>
          <span title="отчётов, накопленных без связи с центром">${d.b ? `${esc(d.b)} отчётов ждут` : 'буфер пуст'}</span>
          <span title="температура в шкафу">${d.t == null ? '—' : `${esc(d.t)} °C`}</span>
          ${osSpan(d)}
          <span title="версия ПО">${esc(d.v ?? '—')}</span>
          <span title="последний отчёт">${age(d.a)} назад</span>
          ${devButton(d)}
        </div>
        ${districtTunnel(d)}
        ${floorTag(d)}
        ${d.al.length > 1 ? `<small>${d.al.slice(1).map((a) => esc(a[1])).join(' · ')}</small>` : ''}
      </div>`;
  // the WireGuard tunnel between a junction unit and its district (d.wg: the centre's view, DeviceHub._wg_row): the
  // first tunnel key a device reports is taken by itself, a changed one waits for the admin; a district's relay port
  // stays open to units outside the tunnel (mode allow) until the admin closes it (mode only)
  const WG_STATE = { up: 'туннель поднят', down: 'туннель не поднят', clock: 'туннель ждёт верного времени', none: 'туннеля ещё нет' };
  const WG_PROBLEM = {
    'no district': 'напрямую к центру: туннеля нет', 'district not active': 'район отозван: туннеля нет',
    'district has no tunnel key yet': 'район ещё не сообщил ключ туннеля', 'district address is not IPv4': 'адрес района не IPv4: туннеля нет',
    'no tunnel key yet': 'блок ещё не сообщил ключ туннеля',
  };
  const wgPending = (d) => (d.wg?.pending
    ? `${chip('warn', esc(`новый ключ туннеля ${d.wg.pending} ждёт подтверждения`))}${S.can('admin')
      ? `<button data-act="wg-accept" data-id="${esc(d.id)}" title="Сверьте с экраном устройства: без повторного подключения ключ не меняется">Принять ключ туннеля</button>` : ''}` : '');
  const wgCommon = (w) => `${w.behind ? chip('warn', 'не применено') : ''}${w.reason ? `<span title="последний отказ помощника туннеля">отказ: ${esc(w.reason)}</span>` : ''}${w.ignored_keys
    ? `<span title="другие ключи, присланные после ожидающего, не сохранены">ещё ключей отброшено: ${esc(w.ignored_keys)}</span>` : ''}`;
  const unitTunnel = (d) => {
    const w = d.wg;
    // a unit straight to the centre that never reported a tunnel key (an older system, the demo) has nothing to show
    if (!w || (!w.address && !w.fingerprint && !w.pending && ['no district', 'revoked'].includes(w.problem))) return '';
    const key = w.fingerprint ? `<span title="отпечаток ключа туннеля">ключ ${esc(w.fingerprint)}</span>` : '';
    const link = w.address
      ? `<span title="адрес блока в туннеле">туннель ${esc(w.address)}</span>${key}
        ${w.handshake_age != null ? `<span title="последнее рукопожатие с районом">рукопожатие ${age(w.handshake_age)} назад</span>` : ''}
        ${w.proven ? '<span title="блок ходит в центр через туннель">доказан</span>'
          : '<span title="блок ходит в центр мимо туннеля, через открытый порт района">старый путь</span>'}
        ${w.state ? `<span>${esc(WG_STATE[w.state] ?? w.state)}</span>` : ''}`
      : key;
    const problem = WG_PROBLEM[w.problem] ? `<span>${esc(WG_PROBLEM[w.problem])}</span>` : '';
    return `<div class="row meta">${link}${problem}${wgCommon(w)}${wgPending(d)}</div>`;
  };
  const districtTunnel = (d) => {
    const w = d.wg;
    if (!w) return '';
    const units = Object.values(S.dev).filter((u) => !isDistrict(u) && u.via === d.id && u.s === 'active');
    const inTunnel = Math.max(0, units.length - (w.unproven?.length ?? 0));
    const button = S.can('admin') && (w.index != null || w.mode === 'only')
      ? (w.mode === 'only'
        ? `<button data-act="wg-allow" data-id="${esc(d.id)}" title="Ретранслятор снова примет блоки и мимо туннеля">Открыть порт района</button>`
        : `<button data-act="wg-only" data-id="${esc(d.id)}" title="Ретранслятор будет принимать блоки только через туннель">Закрыть открытый порт района</button>`)
      : '';
    return `<div class="row meta">
        <span title="номер района в туннельной сети">${w.index != null ? `туннель №${esc(w.index)} · ${esc(w.address)}` : 'туннелю ещё не назначен номер'}</span>
        <span title="кому отвечает ретранслятор района">${w.mode === 'only' ? 'порт района закрыт: только туннель' : 'порт района открыт'}</span>
        <span title="блоки района, которые в этой сессии ходят в центр через туннель">${inTunnel} из ${units.length} блоков в туннеле</span>
        ${w.fingerprint ? `<span title="отпечаток ключа туннеля">ключ ${esc(w.fingerprint)}</span>` : ''}
        ${w.state ? `<span>${esc(WG_STATE[w.state] ?? w.state)}</span>` : ''}
        ${wgCommon(w)}${wgPending(d)}${button}
      </div>`;
  };
  const plural = (n, one, few, many) => (n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many);
  const devState = (d) => (d.s === 'revoked' ? chip('bad', 'отозвано') : d.lock ? chip('bad', 'заблокирован')
    : d.o ? (d.al.length ? chip(d.al[0][0] === 'alert' ? 'bad' : 'warn', d.al[0][1]) : chip('ok', 'норма'))
      : waiting(d) ? chip('info', 'ждёт первого подключения') : chip('warn', 'нет связи'));
  const devButton = (d) => (S.can('admin')
    ? `${S.mode === 'field' && d.s !== 'revoked' ? `<button data-act="kit" data-id="${esc(d.id)}" title="Адрес платформы, её сертификат и корневой ключ — для установки на блок">Комплект</button>` : ''}
       <button data-act="${d.s === 'revoked' ? 'restore' : 'revoke'}" data-id="${esc(d.id)}">${d.s === 'revoked' ? 'Доверять' : 'Отозвать'}</button>
       ${d.s === 'revoked' && S.mode === 'field' ? `<button data-act="remove" data-id="${esc(d.id)}" title="Блок выведен из работы: забыть его, перекрёсток освободится">Убрать</button>` : ''}` : '');
  const age = (s) => (s == null ? '—' : s < 60 ? `${s} с` : `${Math.floor(s / 60)} мин`);
  // the system version and where its over-the-air update stands (the device reports it, the centre offers it)
  const UPDATE_RU = { downloading: (u) => `обновляется до ${u.version}`, ready: (u) => `обновляется до ${u.version}`,
    installing: (u) => `обновляется до ${u.version}`, installed: (u) => `обновлено до ${u.version}`,
    failed: (u) => `обновление не загружено${u.reason ? `: ${u.reason}` : ''}`,
    refused: (u) => `обновление отклонено${u.reason ? `: ${u.reason}` : ''}` };
  const osSpan = (d) => `<span title="версия системы и состояние обновления по воздуху">ОС ${esc(d.os ?? '—')}${d.up && UPDATE_RU[d.up.state]
    ? ` · ${esc(UPDATE_RU[d.up.state](d.up))}` : ''}</span>`;
  // the centre's version floor: a device that has not shown at least that system version in its current session
  // (po: the version it showed, fl: the floor) gets no commands, only update offers, until it does
  const floorText = (d) => (d.po != null
    ? `ниже допустимой версии (ОС ${d.po} < ${d.fl}): команды не отправляются`
    : `версия ОС не подтверждена (допустимая от ${d.fl}): команды не отправляются`);
  const floorTag = (d) => (d.bf ? `<div class="floor">${chip('bad', esc(floorText(d)))}</div>` : '');
  // a changed tunnel key waiting for the admin is flagged too: a genuine box does not change it without re-enrolment
  const flagged = (d) => (!d.o && !waiting(d)) || d.lock || d.al.length || d.s === 'revoked' || d.bf || d.wg?.pending;
  const floorBox = () => {
    const f = S.osFloor;
    const value = f == null ? 'загрузка…' : f.floor == null ? 'не задана — команды получают устройства с любой версией ОС'
      : `ОС ${esc(f.floor)}: устройства на более старой системе не получают команд, пока не обновятся
        (${f.below?.length ? `сейчас таких ${f.below.length}` : 'сейчас таких нет'})${f.set_by ? ` · задал ${esc(f.set_by)}, ${esc(f.at ?? '')}` : ''}`;
    const buttons = S.can('admin') && f != null
      ? `<div class="btns"><button data-act="floor-set">${f.floor == null ? 'Задать минимум' : 'Изменить'}</button>
         ${f.floor != null ? '<button data-act="floor-clear">Снять минимум</button>' : ''}</div>` : '';
    return `<div class="item"><b>Минимальная версия ОС</b> — ${value}${buttons}</div>`;
  };
  const deviceSummary = () => {
    const all = Object.values(S.dev);
    if (!all.length) return empty('ожидание устройств');
    const { units, districts } = split(all);
    const bad = all.filter(flagged);
    const part = (list, what) => (list.length ? chip(onlineOf(list) === list.length && !bad.length ? 'ok' : 'warn',
      `${onlineOf(list)} из ${list.length} ${what} на связи`) : '');
    return `<div class="item">${part(units, 'блоков')} ${part(districts, plural(districts.length, 'районный сервер', 'районных сервера', 'районных серверов'))}
        ${bad.length ? `${bad.length} с замечаниями, подробности во вкладке «Устройства».` : 'Замечаний нет.'}</div>`;
  };
  const enrolButton = (junction = '') => (S.mode === 'field' && S.can('admin')
    ? `<div class="btns"><button class="go" data-act="enrol" data-id="${esc(junction)}">Подключить блок или районный сервер</button></div>` : '');
  const devicesTab = () => {
    const all = Object.entries(S.dev);
    if (!all.length) {
      return S.mode === 'field'
        ? `<p class="muted">Блоков URBAN Edge пока нет. Блок ставится в шкаф светофора: он видит очереди через камеры и
            выбирает планы в контроллере по командам, которые утвердил оператор.</p>
          <h4>Как подключить</h4>
          <div class="item"><b>1.</b> На блоке создать ключ — он не покидает блок:
            <code>python -m urban.edge --keygen --unit edge-J-07 --serial URB-0042</code>. Блок напечатает код подключения и отпечаток ключа.</div>
          <div class="item"><b>2.</b> Здесь: «Подключить», вставить код, выбрать перекрёсток (и районный сервер, если блок подключается через него), сверить отпечаток с тем, что напечатал блок.</div>
          <div class="item"><b>3.</b> Скачать комплект и поставить его на блок:
            <code>python -m urban.edge --setup urban-edge-J-07.json --unit edge-J-07</code>, затем запустить службу блока.</div>
          ${enrolButton() || '<p class="muted">Подключать блоки может администратор.</p>'}
          ${floorBox()}`
        : empty('ожидание устройств');
    }
    const { units, districts } = split(all.map(([, d]) => d));
    const online = onlineOf(units);
    const pendingUnits = all.filter(([, d]) => waiting(d)).length;
    const alerts = all.filter(([, d]) => d.al.some((a) => a[0] === 'alert') || d.lock || d.s === 'revoked' || d.bf).length;
    const warns = all.filter(([, d]) => d.al.length && !d.al.some((a) => a[0] === 'alert') && !d.lock && d.s !== 'revoked' && !d.bf).length;
    const hot = all.map(([, d]) => d.t).filter((t) => typeof t === 'number');
    const rows = all
      .sort(([, a], [, b]) => (b.lock - a.lock) || ((b.bf ?? 0) - (a.bf ?? 0)) || (b.al.length - a.al.length) || (a.o - b.o))
      .map(([junction, d]) => (isDistrict(d) ? districtRow(d) : `<div class="item dev" data-fly="${esc(junction)}">
        <div class="row"><span><b>${esc(d.id)}</b> <small>${esc(junction)}${d.via ? ` · через ${esc(d.via)}` : ''}${d.serial ? ` · ${esc(d.serial)}` : ''}</small></span>${devState(d)}</div>
        <div class="row meta">
          <span title="температура в шкафу">${d.t == null ? '—' : `${esc(d.t)} °C`}</span>
          <span title="дверь шкафа">${d.d === 'open' ? 'дверь открыта' : d.d === 'closed' ? 'дверь закрыта' : '—'}</span>
          <span title="связь с контроллером">${d.c == null ? '—' : d.c === 'ok' ? 'контроллер ок' : esc(d.c)}</span>
          <span title="действующих аренд">${esc(d.l ?? 0)} аренд</span>
          ${osSpan(d)}
          <span title="версия ПО">${esc(d.v ?? '—')}</span>
          <span title="последний отчёт">${age(d.a)} назад</span>
          ${devButton(d)}
        </div>
        ${unitTunnel(d)}
        ${floorTag(d)}
        ${d.al.length > 1 ? `<small>${d.al.slice(1).map((a) => esc(a[1])).join(' · ')}</small>` : ''}
      </div>`)).join('');
    const recent = [...S.devAlerts].reverse().slice(0, 6);
    return `<div class="grid">
        ${kpi('блоков на связи', `${online} / ${units.length}`, online === units.length ? 'ok' : 'warn')}
        ${districts.length ? kpi('районных серверов на связи', `${onlineOf(districts)} / ${districts.length}`, onlineOf(districts) === districts.length ? 'ok' : 'warn') : ''}
        ${kpi('тревог', alerts, alerts ? 'bad' : 'ok')}${kpi('предупреждений', warns, warns ? 'warn' : '')}
        ${kpi('макс. температура, °C', hot.length ? Math.max(...hot) : '—')}
      </div>
      <p class="muted">Реестр устройств URBAN Edge: у каждого свой ключ, команды подписаны шлюзом. Вскрытый шкаф или
        потеря контроллера блокируют перекрёсток: команды снимаются, новые не принимаются. Районный сервер несёт
        соединения своих блоков в центр, не читая их, и отчитывается очередями своих камер.
        ${pendingUnits ? `Ждут первого подключения: ${pendingUnits}.` : ''}</p>
      ${floorBox()}
      ${enrolButton()}
      ${recent.length ? `<h4>Последние события</h4>${recent.map((a) => `<div class="item"><time>${clock(a.t)}</time>
        ${chip(a.level === 'alert' ? 'bad' : 'warn', KIND_RU[a.kind] ?? esc(a.kind))} ${esc(a.device)}: ${esc(a.text)}</div>`).join('')}` : ''}
      <h4>Все устройства</h4>
      ${rows}`;
  };

  // one problem found by a camera, with what the platform suggests and what happened next
  const cmdCard = (c) => {
    const [cls, label] = CMD_STATUS[c.status] ?? ['info', c.status];
    const live = ['approved', 'active', 'revoking'].includes(c.status);
    const unit = S.dev[c.cam];
    const belowFloor = c.status === 'pending' && unit?.bf;  // the centre will not command this junction's unit now
    const buttons = c.status === 'pending'
      ? (S.can('operator')
        ? `${belowFloor ? `<small>Нельзя применить: блок ${esc(unit.id)} — ${esc(floorText(unit))}.</small>` : ''}
           <div class="btns"><button class="go" data-act="approve" data-id="${c.id}" ${S.link && !belowFloor ? '' : 'disabled'}>Применить</button>
           <button data-act="reject" data-id="${c.id}">Отклонить</button></div>`
        : `<small class="muted">Решение принимает оператор; у вашей учётной записи роль «наблюдатель».</small>`)
      : '';
    const both = (now, other, word) => `<div class="stat">
        <div class="row"><span>Очередь загруженного: было ${c.queue0} м</span><b>${word} ${now ?? '—'} м</b></div>
        <div class="row"><span>Очередь отдающего зелёный: было ${c.other0} м</span><b>${word} ${other ?? '—'} м</b></div></div>`;
    return `<div class="item cmd ${c.status}">
      <div class="row"><b>${esc(c.street || c.cam)}</b>${chip(cls, label)}</div>
      <div>${esc(c.reason)}</div>
      ${c.command === 'tls.corridor'
        ? `<small>Зелёный коридор: <b>план ${c.plan}</b> для ${esc(c.cam)} — +${c.shift} с зелёного подъезду «${esc(c.street)}»,
            пока ${esc(c.unit || 'экстренная машина')} не проедет перекрёсток. Длина цикла не меняется.</small>`
        : `<small>Предложение платформы: включить <b>план ${c.plan}</b> из утверждённых для ${esc(c.cam)}:
            +${c.shift} с зелёного для «${esc(c.street)}» за счёт «${esc(c.from_street)}». Длина цикла не меняется.</small>`}
      ${live ? `${both(c.queue, c.other, 'сейчас')}
        <small>Действует до ${clock(c.until)} (осталось ${Math.max(0, c.until - S.t)} с). Снимется раньше, если очередь уйдёт
        или начнёт страдать направление, отдающее зелёный.</small>` : ''}
      ${c.status === 'done' && c.result ? `${both(c.result.queue, c.result.other, 'к концу')}
        <small>${esc(END_REASON[c.result.reason] ?? c.result.reason)}</small>` : ''}
      ${buttons}</div>`;
  };

  // pending proposals on one street come as a group: one decision for the whole street, the cards below it
  const withGroups = (list) => {
    const seen = new Set();
    return list.map((c) => {
      if (c.status !== 'pending' || !c.group) return cmdCard(c);
      if (seen.has(c.group)) return '';
      seen.add(c.group);
      const members = list.filter((m) => m.status === 'pending' && m.group === c.group);
      const ids = members.map((m) => m.id).join(',');
      const buttons = S.can('operator')
        ? `<div class="btns"><button class="go" data-act="approve-group" data-id="${ids}" ${S.link ? '' : 'disabled'}>Применить все (${members.length})</button>
           <button data-act="reject-group" data-id="${ids}">Отклонить все</button></div>`
        : '';
      return `<div class="item group">
        <div class="row"><b>${esc(c.group_street)}</b>${chip('warn', `${members.length} ${members.length < 5 ? 'перекрёстка' : 'перекрёстков'}`)}</div>
        <small>${c.command === 'tls.corridor'
          ? 'Экстренная машина едет к происшествию. Светофоры на её маршруте дадут больше зелёного её подъезду, каждый — пока она не проедет. Подтвердите коридор целиком или по перекрёсткам.'
          : 'Перекосы на одной улице сразу в нескольких местах. Можно решить для всей улицы одной кнопкой — каждый перекрёсток всё равно проверяется и записывается в журнал отдельно — или по одному ниже.'}</small>
        ${buttons}
        <div class="members">${members.map(cmdCard).join('')}</div></div>`;
    }).join('');
  };

  // junction passports: what the engineer checked against the controller; a draft is watched, never commanded
  const unitChip = (id) => {
    const d = S.dev[id];
    if (!d) return chip('info', 'блока нет');
    return d.s === 'revoked' ? chip('bad', 'блок отозван') : d.lock ? chip('bad', 'заблокирован') : d.o ? ''
      : waiting(d) ? chip('info', 'блок ждёт подключения') : chip('warn', 'блок не на связи');
  };
  const passportList = () => {
    const list = S.junctions;
    const ready = list.filter((j) => j.passport.status === 'approved').length;
    const units = list.filter((j) => S.dev[j.id]).length;
    return `<div class="grid">
        ${kpi('перекрёстков', list.length)}${kpi('паспорт утверждён', ready, ready < list.length ? 'warn' : '')}
        ${kpi('с блоком URBAN Edge', units)}${kpi('блоков на связи', list.filter((j) => S.dev[j.id]?.o).length)}
      </div>
      <p class="muted">Паспорт перекрёстка — подъезды, фазы и таблица планов. Пока он черновик, платформа только
        наблюдает за перекрёстком: ничего не предлагает, а блок не выполнит ни одной команды.
        ${S.mode === 'field' ? 'Черновики собраны из OpenStreetMap; инженер сверяет их с контроллером и утверждает.' : 'В демо паспорта синтетические.'}</p>
      ${list.map((j) => {
        const [cls, label] = PASSPORT[j.passport.status] ?? ['info', j.passport.status];
        const q = S.cams[j.id]?.[2];
        return `<button class="item jn" data-junction="${esc(j.id)}">
          <span><b>${esc(j.id)}</b> ${esc(j.name)}</span>
          <span class="row meta">${chip(cls, label)}${unitChip(j.id)}<span>${q == null ? 'нет данных' : `очередь ${q} м`}</span></span></button>`;
      }).join('')}`;
  };
  const passportView = (j) => {
    const p = j.passport, edit = S.mode === 'field' && S.can('admin');
    const [cls, label] = PASSPORT[p.status] ?? ['info', p.status];
    const num = (name, value, lo, hi) => (edit
      ? `<input class="num" name="${name}" type="number" min="${lo}" max="${hi}" value="${value}" />` : `<b>${value}</b>`);
    const shifts = j.plans.length - 1;
    const unit = S.dev[j.id];
    return `<button class="back" data-junction="">‹ Все перекрёстки</button>
      <div class="row"><h3>${esc(j.id)} · ${esc(j.name)}</h3>${chip(cls, label)}</div>
      <div class="kv"><span>Версия</span><b>${p.version ?? 1}</b>
        ${p.status === 'approved' ? `<span>Утвердил</span><b>${esc(p.approved_by)} · ${esc(p.approved_at)}</b><span>Хэш</span><code>${esc(p.hash)}</code>`
          : `<span>Источник</span><b>${p.source === 'osm' ? 'OpenStreetMap' : esc(p.source ?? '—')}</b>`}
        ${p.edited_by ? `<span>Правил</span><b>${esc(p.edited_by)} · ${esc(p.edited_at)}</b>` : ''}
        <span>Блок</span><span>${unit ? `${esc(unit.id)} ${unitChip(j.id) || 'на связи'}` : 'не установлен'}</span>
      </div>
      ${!unit ? enrolButton(j.id) : ''}
      ${p.note ? `<p class="muted">${esc(p.note)}</p>` : ''}
      ${j.problems.length ? `<div class="item">${chip('warn', 'нельзя утвердить')} ${j.problems.map(esc).join('; ')}</div>` : ''}
      <form data-passport="${esc(j.id)}" onsubmit="return false">
      <h4>Подъезды</h4>
      <table class="tbl"><tr><th>Откуда</th><th>Улица</th><th>Полос</th><th>Зона датчика, м</th></tr>
        ${j.approaches.map((a) => `<tr><td>${esc(a.from ?? a.id)}</td><td>${esc(a.name || '—')}</td>
          <td>${num(`lanes:${a.id}`, a.lanes ?? 1, 1, 8)}</td><td>${num(`zone:${a.id}`, Math.round(a.zone), 10, 300)}</td></tr>`).join('')}</table>
      <h4>Фазы · цикл ${j.cycle ?? '—'} с</h4>
      <table class="tbl"><tr><th>Фаза</th><th>Зелёный получают</th><th>Зелёный, с</th><th>Минимум, с</th></tr>
        ${j.phases.map((ph) => `<tr><td>${ph.index}</td>
          <td>${ph.edges.map((e) => esc(j.approaches.find((a) => a.id === e)?.from ?? e)).join(', ')}</td>
          <td>${num(`duration:${ph.index}`, ph.duration, 5, 120)}</td><td>${num(`min_green:${ph.index}`, ph.min_green, 5, 60)}</td></tr>`).join('')}</table>
      <p class="muted">Таблица планов строится из фаз: базовый план и ${shifts} ${shifts % 10 === 1 && shifts % 100 !== 11 ? 'сдвиг' : [2, 3, 4].includes(shifts % 10) && ![12, 13, 14].includes(shifts % 100) ? 'сдвига' : 'сдвигов'} зелёного на 5, 10 или 15 с
        между фазами — цикл не меняется, минимум каждой фазы соблюдён. Платформа может выбрать только из неё.</p>
      ${edit ? `<div class="btns">
          <button data-act="passport-save" data-id="${esc(j.id)}">Сохранить черновик</button>
          ${p.status === 'approved'
            ? `<input name="why" placeholder="причина" maxlength="200" /><button data-act="passport-withdraw" data-id="${esc(j.id)}">Вернуть в черновик</button>`
            : `<button class="go" data-act="passport-approve" data-id="${esc(j.id)}" ${j.problems.length ? 'disabled' : ''}>Утвердить</button>`}
        </div>
        <p class="muted">Любая правка делает паспорт черновиком новой версии: действующие команды на перекрёстке снимаются.
          Утверждение записывается в журнал вместе с хэшем паспорта; блок получает новую таблицу планов сразу.</p>`
        : `<p class="muted">${S.mode === 'field' ? 'Править и утверждать паспорта может администратор.' : 'В демо паспорта синтетические и не правятся.'}</p>`}
      </form>
      <div class="btns"><button data-fly="${esc(j.id)}">Показать на карте</button></div>`;
  };

  const TABS = {
    overview: ['Обзор', () => {
      const inc = S.activeIncidents.length, bad = S.infraBad.length, jam = S.jamRoads.length, wait = S.pending().length;
      const feed = [
        ...S.events.map((e) => ({ t: e.t, html: e.type === 'incident'
          ? `${e.status === 'active' && S.activeIncidents.some((x) => x.id === e.id) ? chip('bad', 'ДТП') : tag(e.status === 'active' ? 'ДТП' : 'снято')} ${esc(e.street || e.edge)}`
          : `${tag(esc(e.unit))} ${UNIT_TEXT[e.status]} · от «${esc(e.from)}»` })),
        ...S.alerts.map((a) => ({ t: a.t, html: `${tag('прогноз')} ${esc(a.text)}` })),
        ...S.cmd.map((c) => ({ t: c.t, html: `${tag('пробка')} ${esc(c.street || c.cam)}: очередь в среднем ${c.queue0} м` })),
        ...S.devAlerts.map((a) => ({ t: a.t, html: `${tag('устройство')} ${esc(a.device)}: ${esc(a.text)}` })),
      ].sort((a, b) => b.t - a.t).slice(0, 8);
      const field = S.mode === 'field';
      const { units, districts } = split(Object.values(S.dev)), online = onlineOf(units);
      const ready = (S.junctions ?? []).filter((j) => j.passport.status === 'approved').length;
      const running = S.cmd.filter((c) => ['approved', 'active', 'revoking'].includes(c.status)).length;
      return `<div class="grid">
          ${kpi('проблем ждут решения', wait, wait ? 'warn' : '')}${field
            ? `${kpi('команд действует', running)}
               ${kpi('паспортов утверждено', S.junctions ? `${ready} / ${S.junctions.length}` : '—', S.junctions && ready < S.junctions.length ? 'warn' : '')}
               ${kpi('блоков на связи', units.length ? `${online} / ${units.length}` : '0', units.length && online < units.length ? 'warn' : '')}
               ${districts.length ? kpi('районных серверов на связи', `${onlineOf(districts)} / ${districts.length}`, onlineOf(districts) < districts.length ? 'warn' : '') : ''}`
            : `${kpi('активных происшествий', inc, inc ? 'bad' : '')}
               ${kpi('авто в районе', S.stats?.cars ?? '—')}${kpi('ср. скорость, км/ч', S.stats?.mean_speed_kmh ?? '—')}
               ${kpi('сегментов с затором', jam, jam ? 'warn' : '')}${kpi('объектов с отклонением', bad, bad ? 'warn' : '')}`}
        </div>
        ${field && S.junctions && !S.junctions.length ? `<div class="item">${chip('warn', 'город не настроен')}
          На сервере: <code>python -m urban.city create --name … --bbox …</code> — карта и перекрёстки из OpenStreetMap.</div>` : ''}
        ${field && S.junctions && S.junctions.length && !ready ? `<div class="item">${chip('warn', 'нет утверждённых паспортов')}
          Перекрёстки только наблюдаются. Паспорт сверяет и утверждает инженер — раздел «Перекрёстки».</div>` : ''}
        ${field && !units.length ? `<div class="item">${chip('info', 'блоков нет')}
          Ни один блок URBAN Edge ещё не подключён. Как поставить и зарегистрировать блок — deploy/README.md.</div>` : ''}
        ${wait ? `<h4>Ждут решения</h4>${withGroups(S.pending())}` : ''}
        <h4>Сводка анализа</h4>
        ${feed.length ? feed.map((f) => `<div class="item"><time>${clock(f.t)}</time> ${f.html}</div>`).join('') : empty('событий пока нет')}`;
    }],
    junctions: ['Перекрёстки', () => {
      if (!S.junctions) return empty('загрузка паспортов…');
      if (!S.junctions.length) {
        return `<p class="muted">Город ещё не настроен. На сервере: <code>python -m urban.city create --name … --bbox …</code> —
          карта и перекрёстки из OpenStreetMap, без симулятора.</p>`;
      }
      const j = S.junction && S.junctions.find((x) => x.id === S.junction);
      return j ? passportView(j) : passportList();
    }],
    transport: ['Пробки и светофоры', () => {
      const streets = jamStreets(), jam = S.jamRoads.length;
      const skipped = S.cmd.filter((c) => c.status === 'expired' || c.status === 'rejected').length;
      const cards = [...S.cmd].filter((c) => c.status !== 'expired' && c.status !== 'rejected').reverse()
        .sort((a, b) => (b.status === 'pending') - (a.status === 'pending'));  // what waits for a decision goes first
      const field = S.mode === 'field';
      return `<div class="grid">
          ${field
            ? `${kpi('ждут решения', S.pending().length, S.pending().length ? 'warn' : '')}
               ${kpi('команд действует', S.cmd.filter((c) => ['approved', 'active', 'revoking'].includes(c.status)).length)}`
            : `${kpi('ср. скорость, км/ч', S.stats?.mean_speed_kmh ?? '—')}${kpi('потери автобуса, с', S.stats?.bus_avg_loss ?? '—')}
               ${kpi('сегментов с затором', jam, jam ? 'warn' : '')}`}
          ${kpi(field ? 'команд передано оборудованию' : 'команд передано, эмуляция', S.audit.applied ?? 0)}
        </div>
        <h4>Проблемы, найденные камерами и детекторами</h4>
        ${cards.length ? withGroups(cards) : empty('перекосов нет: нигде одно направление не стоит при свободном поперечном')}
        ${skipped ? `<p class="muted">Без решения или отклонено: ${skipped}</p>` : ''}
        ${field ? '' : `<h4>Заторы сейчас</h4>
        ${streets.length ? streets.slice(0, 6).map(([n, c]) => `<div class="item row"><span>${esc(n)}</span><span>${c} сегм.</span></div>`).join('') : empty('заторов нет')}`}`;
    }],
    cameras: ['Камеры', () => `<p class="muted">Очереди считаются на всех ${scene.cams.length + scene.detectors.length} регулируемых перекрёстках района.
        У ${scene.cams.length} из них есть окно камеры; остальные показаны на карте кружками. Только поток и очереди: без лиц и номеров.</p>
        ${scene.cams.map((c) => {
          const [n, speed, queue] = S.cams[c.id] ?? [0, 0, 0];
          const st = S.camStatus[c.id];
          return `<button class="item cam ${c.id === S.cam ? 'sel' : ''}" data-cam="${c.id}">
            <span><b>${c.id}</b> ${esc(c.name)} ${st === 'problem' ? chip('warn', 'проблема') : st === 'control' ? chip('ok', 'коррекция') : ''}</span>
            <span>${queue == null ? 'нет данных' : `очередь ${queue} м`}${n == null ? '' : ` · ${n} ТС · ${speed} км/ч`}</span></button>`;
        }).join('')}`],
    emergency: ['Экстренные', () => {
      const inc = S.incidents(), dis = S.dispatches();
      return `<h4>Происшествия</h4>
        ${inc.length ? inc.map((i) => `<div class="item">${chip(i.status === 'active' ? 'bad' : 'ok', i.status === 'active' ? 'активно' : 'снято')}
          ДТП · ${esc(i.street || i.edge)}<br><small>зафиксировано в ${clock(i.t - (i.duration ?? 0))}${i.duration ? ` · длилось ${clock(i.duration)}` : ''}</small>
          ${dis.filter((d) => d.ref === i.id).map((d) => `<div class="unit">${chip(d.status === 'arrived' ? 'ok' : 'info', UNIT_TEXT[d.status])}
            <b>${esc(d.unit)}</b> от «${esc(d.from)}»<br><small>расчётное время ${clock(d.eta)}${d.took != null ? ` · фактическое ${clock(d.took)}` : ''}</small></div>`).join('')}
          </div>`).join('') : empty('происшествий нет')}
        <h4>Зелёный коридор</h4>
        ${S.corridor
          ? (() => {
              const cor = S.cmd.filter((c) => c.command === 'tls.corridor').reverse();
              return cor.length ? withGroups(cor) : empty('коридоров нет: экстренные машины сейчас не едут');
            })()
          : `<p class="muted">Выключен (<code>GREEN_CORRIDOR=off</code>). Механизм готов, но замер не показал явной пользы:
              выбор утверждённого плана даёт экстренной машине лишь +15 с зелёного в цикле. Настоящий приоритет —
              функция самого контроллера; подробно в docs/corridor.md.</p>`}`;
    }],
    infra: ['Инфраструктура', () => `<p class="muted">Телеметрия синтетическая. Анализ: отклонение от собственного базового уровня объекта и прогноз по тренду.</p>
        ${S.infra.length ? S.infra.map((a) => `<div class="item asset">
          <div class="row"><span><b>${esc(a.id)}</b> ${esc(a.metric)}</span><span>${a.value} ${esc(a.unit)} ${chip(...STATUS_CHIP[a.status])}</span></div>
          <div class="bar"><i class="${a.status}" style="width:${Math.min(100, (100 * a.value) / a.limit).toFixed(0)}%"></i></div>
          ${a.eta ? `<small>прогноз достижения порога: ~${Math.max(1, Math.round(a.eta / 60))} мин</small>` : ''}</div>`).join('') : empty('ожидание телеметрии')}
        <h4>Заявки на обслуживание (пока только запись)</h4>
        ${shadowList((p) => p.command === 'workorder.create')}`],
    devices: ['Устройства', devicesTab],
    history: ['История', () => {
      const h = S.history;
      if (!h) return empty('загрузка истории…');
      const ok = h.chain.intact;
      return `<div class="grid">
          ${kpi('записей аудита', h.counts.audit, ok ? 'ok' : ok === false ? 'bad' : '')}${kpi('команд за всё время', h.counts.commands)}
          ${kpi('запусков платформы', h.counts.runs)}${kpi('замеров очередей', h.counts.queues)}
        </div>
        <div class="item">${chip(ok ? 'ok' : ok === false ? 'bad' : 'warn', ok ? 'цепочка целая' : ok === false ? 'цепочка нарушена' : 'проверяется')}
          ${ok ? `${h.chain.records ? `Журнал проверен от записи #1 до #${h.chain.records} (последняя проверка ${when(h.chain.checked)})` : 'Журнал начат при этом запуске'}; полная проверка повторяется каждые 15 минут, цепочка хэшей продолжается между перезапусками.`
            : ok === false ? `Нарушение на записи #${h.chain.broken_at}: файл истории изменяли в обход платформы.` : 'Полная проверка цепочки идёт в фоне после запуска.'}
          <br><small>Хранится в <code>data/urban.db</code> (SQLite): журнал, команды, здоровье устройств и очереди раз в минуту.</small></div>
        ${exportItem(h.export)}
        <h4>Команды</h4>
        ${h.commands.length ? h.commands.map((c) => {
          const [cls, label] = CMD_STATUS[c.status] ?? ['info', c.status];
          const r = c.result;
          return `<div class="item cmd">
            <div class="row"><span><time>${when(c.at)}</time><b>${esc(c.street || c.cam)}</b></span>${chip(cls, label)}</div>
            <small>план ${c.plan} для ${esc(c.cam)}${c.actor ? ` · решение: ${esc(c.actor)}` : ''} · запуск #${c.run}</small>
            ${r && r.queue != null ? `<br><small>очередь ${c.queue0} → ${r.queue} м, донор ${c.other0} → ${r.other} м · ${esc(END_REASON[r.reason] ?? r.reason)}</small>` : ''}
          </div>`;
        }).join('') : empty('команд ещё не было')}
        <h4>Запуски</h4>
        ${h.runs.map((r) => `<div class="item row"><span><time>${when(r.started)}</time>#${r.id} · ${RUN_MODE[r.mode] ?? esc(r.mode)}${r.note === 'scenario restarted' ? ' · сценарий перезапущен' : ''}</span>
          <small>${r.commands} команд · ${r.audit} записей</small></div>`).join('')}`;
    }],
    security: ['Безопасность', () => `<div class="grid">
          ${kpi(S.mode === 'field' ? 'команд передано оборудованию' : 'команд передано оборудованию (эмуляция)', S.audit.applied ?? 0, 'ok')}${kpi('записей в журнале аудита', S.audit.count)}
        </div>
        <div class="item">${chip(S.link ? 'ok' : 'bad', S.link ? 'связь есть' : 'связь отключена')}
          ${S.link ? 'Платформа продлевает действующие команды.' : 'Продлений нет: светофоры возвращаются к своему плану сами.'}</div>
        <div class="item">${chip('ok', 'решает человек')} Платформа только предлагает. Без решения оператора команда не уходит.</div>
        <div class="item">${chip('ok', 'аренда')} Команда живёт 3 минуты (два цикла светофора) и продлевается; пропала связь или платформа, оборудование само возвращается к базовому плану.</div>
        <div class="item">${chip('ok', 'пределы')} Можно только выбрать один из планов, утверждённых инженером для этого перекрёстка. Произвольных таймингов нет; цикл не удлиняется, минимальный зелёный учитывает время перехода пешеходов.</div>
        <div class="item">${chip('ok', 'приватность')} Камеры отдают счётчики. Лица, номера и треки жителей не обрабатываются.</div>
        ${S.mode === 'field'
          ? `<div class="item">${chip('ok', 'оборудование')} Команды уходят на реальные устройства URBAN Edge; очереди только из их отчётов.</div>`
          : `<div class="item">${chip('shadow', 'эмуляция')} Реальные светофоры не затронуты: оборудование в демо смоделировано.</div>`}
        <h4>Устройства URBAN Edge</h4>
        ${deviceSummary()}
        <h4>Журнал аудита (цепочка хэшей)</h4>
        ${S.log.length ? S.log.slice(-8).reverse().map((r) => `<div class="item"><time>#${r.seq}</time> <b>${esc(r.event)}</b> <code>${esc(r.command)}</code>
          <br><small>${esc(r.actor)} · ${esc(r.target)}</small><br><small class="hash">${r.hash.slice(0, 32)}…</small></div>`).join('') : empty('записей нет')}`],
  };

  const panel = $('panel'), tabs = $('tabs');
  let last = '';
  const visible = () => Object.entries(TABS).filter(([k]) => S.mode !== 'field' || !DEMO_ONLY.has(k));
  const drawTabs = () => {
    tabs.innerHTML = visible().map(([k, [label]]) => `<button data-tab="${k}" class="${k === S.tab ? 'sel' : ''}">${label}</button>`).join('');
  };
  drawTabs();

  const api = {
    render() {
      if (S.module !== 'traffic') { $('clock').textContent = clock(S.t); return; }  // another module owns the panel
      const html = TABS[S.tab][1]();
      if (html !== last) { panel.innerHTML = html; last = html; }
      $('clock').textContent = clock(S.t);
      const wait = S.pending().length;
      tabs.querySelector('[data-tab=transport]').dataset.badge = wait || '';
      const devBad = Object.values(S.dev).filter(flagged).length;
      tabs.querySelector('[data-tab=devices]').dataset.badge = devBad || '';
      const drafts = (S.junctions ?? []).filter((j) => j.passport.status !== 'approved').length;
      tabs.querySelector('[data-tab=junctions]').dataset.badge = S.mode === 'field' && drafts ? drafts : '';
    },
    setMode(mode) {
      S.mode = mode;
      drawTabs();
      if (!visible().some(([k]) => k === S.tab)) api.setTab('overview');
      else api.render();
    },
    refresh() { last = ''; api.render(); },
    setTab(tab) {
      if (beforeTab) beforeTab();  // a traffic tab was asked for: leave the other module
      S.tab = tab;
      for (const b of tabs.children) b.classList.toggle('sel', b.dataset.tab === tab);
      last = '';
      api.render();
      $('side').scrollTop = 0;
      if (onTab) onTab(tab);
    },
  };
  tabs.onclick = (e) => e.target.dataset.tab && api.setTab(e.target.dataset.tab);
  panel.onclick = (e) => {
    const jn = e.target.closest('[data-junction]');
    if (jn) { S.junction = jn.dataset.junction || null; last = ''; api.render(); $('side').scrollTop = 0; return; }
    const cam = e.target.closest('[data-cam]');
    if (cam) return onCamera(cam.dataset.cam);
    const act = e.target.closest('[data-act]');
    if (act && !act.disabled) return onAction(act.dataset.act, act.dataset.id);
    const fly = e.target.closest('[data-fly]');
    if (fly && onFly) onFly(fly.dataset.fly);
  };
  return api;
}
