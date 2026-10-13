// Control-centre side panel: one renderer per module tab. The DOM is only touched when the
// generated markup actually changes, so hover and scroll state survive the 1 Hz updates.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
export const clock = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t) % 60).padStart(2, '0')}`;
const chip = (cls, text) => `<span class="chip ${cls}">${text}</span>`;
const kpi = (label, value, cls = '') => `<div class="kpi ${cls}"><b>${value}</b><span>${label}</span></div>`;
const empty = (text) => `<p class="muted">${text}</p>`;
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
};

export function createPanel(scene, S, { onCamera, onAction }) {
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

  // one problem found by a camera, with what the platform suggests and what happened next
  const cmdCard = (c) => {
    const [cls, label] = CMD_STATUS[c.status] ?? ['info', c.status];
    const live = ['approved', 'active', 'revoking'].includes(c.status);
    const buttons = c.status === 'pending'
      ? (S.operator
        ? `<div class="btns"><button class="go" data-act="approve" data-id="${c.id}" ${S.link ? '' : 'disabled'}>Применить</button>
           <button data-act="reject" data-id="${c.id}">Отклонить</button></div>`
        : `<small class="muted">Решение принимает оператор: войдите по токену (вверху справа).</small>`)
      : '';
    const both = (now, other, word) => `<div class="stat">
        <div class="row"><span>Очередь загруженного: было ${c.queue0} м</span><b>${word} ${now ?? '—'} м</b></div>
        <div class="row"><span>Очередь отдающего зелёный: было ${c.other0} м</span><b>${word} ${other ?? '—'} м</b></div></div>`;
    return `<div class="item cmd ${c.status}">
      <div class="row"><b>${esc(c.street || c.cam)}</b>${chip(cls, label)}</div>
      <div>${esc(c.reason)}</div>
      <small>Предложение платформы: включить <b>план ${c.plan}</b> из утверждённых для ${esc(c.cam)}:
        +${c.shift} с зелёного для «${esc(c.street)}» за счёт «${esc(c.from_street)}». Длина цикла не меняется.</small>
      ${live ? `${both(c.queue, c.other, 'сейчас')}
        <small>Действует до ${clock(c.until)} (осталось ${Math.max(0, c.until - S.t)} с). Снимется раньше, если очередь уйдёт
        или начнёт страдать направление, отдающее зелёный.</small>` : ''}
      ${c.status === 'done' && c.result ? `${both(c.result.queue, c.result.other, 'к концу')}
        <small>${esc(END_REASON[c.result.reason] ?? c.result.reason)}</small>` : ''}
      ${buttons}</div>`;
  };

  const TABS = {
    overview: ['Обзор', () => {
      const inc = S.activeIncidents.length, bad = S.infraBad.length, jam = S.jamRoads.length, wait = S.pending().length;
      const feed = [
        ...S.events.map((e) => ({ t: e.t, html: e.type === 'incident'
          ? `${chip(e.status === 'active' ? 'bad' : 'ok', e.status === 'active' ? 'ДТП' : 'снято')} ${esc(e.street || e.edge)}`
          : `${chip('info', esc(e.unit))} ${UNIT_TEXT[e.status]} · от «${esc(e.from)}»` })),
        ...S.alerts.map((a) => ({ t: a.t, html: `${chip(a.level === 'alert' ? 'bad' : 'warn', 'прогноз')} ${esc(a.text)}` })),
        ...S.cmd.map((c) => ({ t: c.t, html: `${chip('warn', 'пробка')} ${esc(c.street || c.cam)}: очередь в среднем ${c.queue0} м` })),
      ].sort((a, b) => b.t - a.t).slice(0, 8);
      return `<div class="grid">
          ${kpi('проблем ждут решения', wait, wait ? 'warn' : '')}${kpi('активных происшествий', inc, inc ? 'bad' : '')}
          ${kpi('авто в районе', S.stats?.cars ?? '—')}${kpi('ср. скорость, км/ч', S.stats?.mean_speed_kmh ?? '—')}
          ${kpi('сегментов с затором', jam, jam ? 'warn' : '')}${kpi('объектов с отклонением', bad, bad ? 'warn' : '')}
        </div>
        ${wait ? `<h4>Ждут решения</h4>${S.pending().map(cmdCard).join('')}` : ''}
        <h4>Сводка анализа</h4>
        ${feed.length ? feed.map((f) => `<div class="item"><time>${clock(f.t)}</time> ${f.html}</div>`).join('') : empty('событий пока нет')}`;
    }],
    transport: ['Пробки и светофоры', () => {
      const streets = jamStreets(), jam = S.jamRoads.length;
      const skipped = S.cmd.filter((c) => c.status === 'expired' || c.status === 'rejected').length;
      const cards = [...S.cmd].filter((c) => c.status !== 'expired' && c.status !== 'rejected').reverse();
      return `<div class="grid">
          ${kpi('ср. скорость, км/ч', S.stats?.mean_speed_kmh ?? '—')}${kpi('потери автобуса, с', S.stats?.bus_avg_loss ?? '—')}
          ${kpi('сегментов с затором', jam, jam ? 'warn' : '')}${kpi('команд передано, эмуляция', S.audit.applied ?? 0)}
        </div>
        <h4>Проблемы, найденные камерами и детекторами</h4>
        ${cards.length ? cards.map(cmdCard).join('') : empty('перекосов нет: нигде одно направление не стоит при свободном поперечном')}
        ${skipped ? `<p class="muted">Без решения или отклонено: ${skipped}</p>` : ''}
        <h4>Заторы сейчас</h4>
        ${streets.length ? streets.slice(0, 6).map(([n, c]) => `<div class="item row"><span>${esc(n)}</span><span>${c} сегм.</span></div>`).join('') : empty('заторов нет')}`;
    }],
    cameras: ['Камеры', () => `<p class="muted">Очереди считаются на всех ${scene.cams.length + scene.detectors.length} регулируемых перекрёстках района.
        У ${scene.cams.length} из них есть окно камеры; остальные показаны на карте кружками. Только поток и очереди: без лиц и номеров.</p>
        ${scene.cams.map((c) => {
          const [n, speed, queue] = S.cams[c.id] ?? [0, 0, 0];
          const st = S.camStatus[c.id];
          return `<button class="item cam ${c.id === S.cam ? 'sel' : ''}" data-cam="${c.id}">
            <span><b>${c.id}</b> ${esc(c.name)} ${st === 'problem' ? chip('warn', 'проблема') : st === 'control' ? chip('ok', 'коррекция') : ''}</span>
            <span>очередь ${queue} м · ${n} ТС · ${speed} км/ч</span></button>`;
        }).join('')}`],
    emergency: ['Экстренные', () => {
      const inc = S.incidents(), dis = S.dispatches();
      return `<h4>Происшествия</h4>
        ${inc.length ? inc.map((i) => `<div class="item">${chip(i.status === 'active' ? 'bad' : 'ok', i.status === 'active' ? 'активно' : 'снято')}
          ДТП · ${esc(i.street || i.edge)}<br><small>зафиксировано в ${clock(i.t - (i.duration ?? 0))}${i.duration ? ` · длилось ${clock(i.duration)}` : ''}</small>
          ${dis.filter((d) => d.ref === i.id).map((d) => `<div class="unit">${chip(d.status === 'arrived' ? 'ok' : 'info', UNIT_TEXT[d.status])}
            <b>${esc(d.unit)}</b> от «${esc(d.from)}»<br><small>расчётное время ${clock(d.eta)}${d.took != null ? ` · фактическое ${clock(d.took)}` : ''}</small></div>`).join('')}
          </div>`).join('') : empty('происшествий нет')}
        <h4>Зелёный коридор (пока только запись)</h4>
        ${shadowList((p) => p.command === 'tls.preempt')}`;
    }],
    infra: ['Инфраструктура', () => `<p class="muted">Телеметрия синтетическая. Анализ: отклонение от собственного базового уровня объекта и прогноз по тренду.</p>
        ${S.infra.length ? S.infra.map((a) => `<div class="item asset">
          <div class="row"><span><b>${esc(a.id)}</b> ${esc(a.metric)}</span><span>${a.value} ${esc(a.unit)} ${chip(...STATUS_CHIP[a.status])}</span></div>
          <div class="bar"><i class="${a.status}" style="width:${Math.min(100, (100 * a.value) / a.limit).toFixed(0)}%"></i></div>
          ${a.eta ? `<small>прогноз достижения порога: ~${Math.max(1, Math.round(a.eta / 60))} мин</small>` : ''}</div>`).join('') : empty('ожидание телеметрии')}
        <h4>Заявки на обслуживание (пока только запись)</h4>
        ${shadowList((p) => p.command === 'workorder.create')}`],
    security: ['Безопасность', () => `<div class="grid">
          ${kpi('команд передано оборудованию (эмуляция)', S.audit.applied ?? 0, 'ok')}${kpi('записей в журнале аудита', S.audit.count)}
        </div>
        <div class="item">${chip(S.link ? 'ok' : 'bad', S.link ? 'связь есть' : 'связь отключена')}
          ${S.link ? 'Платформа продлевает действующие команды.' : 'Продлений нет: светофоры возвращаются к своему плану сами.'}</div>
        <div class="item">${chip('ok', 'решает человек')} Платформа только предлагает. Без решения оператора команда не уходит.</div>
        <div class="item">${chip('ok', 'аренда')} Команда живёт 3 минуты (два цикла светофора) и продлевается; пропала связь или платформа, оборудование само возвращается к базовому плану.</div>
        <div class="item">${chip('ok', 'пределы')} Можно только выбрать один из планов, утверждённых инженером для этого перекрёстка. Произвольных таймингов нет; цикл не удлиняется, минимальный зелёный учитывает время перехода пешеходов.</div>
        <div class="item">${chip('ok', 'приватность')} Камеры отдают счётчики. Лица, номера и треки жителей не обрабатываются.</div>
        <div class="item">${chip('shadow', 'эмуляция')} Реальные светофоры не затронуты: оборудование в демо смоделировано.</div>
        <h4>Журнал аудита (цепочка хэшей)</h4>
        ${S.log.length ? S.log.slice(-8).reverse().map((r) => `<div class="item"><time>#${r.seq}</time> <b>${esc(r.event)}</b> <code>${esc(r.command)}</code>
          <br><small>${esc(r.actor)} · ${esc(r.target)}</small><br><small class="hash">${r.hash.slice(0, 32)}…</small></div>`).join('') : empty('записей нет')}`],
  };

  const panel = $('panel'), tabs = $('tabs');
  let last = '';
  tabs.innerHTML = Object.entries(TABS).map(([k, [label]]) => `<button data-tab="${k}">${label}</button>`).join('');

  const api = {
    render() {
      const html = TABS[S.tab][1]();
      if (html !== last) { panel.innerHTML = html; last = html; }
      $('clock').textContent = clock(S.t);
      const wait = S.pending().length;
      const badge = tabs.querySelector('[data-tab=transport]');
      badge.dataset.badge = wait || '';
    },
    setTab(tab) {
      S.tab = tab;
      for (const b of tabs.children) b.classList.toggle('sel', b.dataset.tab === tab);
      last = '';
      api.render();
      $('side').scrollTop = 0;
    },
  };
  tabs.onclick = (e) => e.target.dataset.tab && api.setTab(e.target.dataset.tab);
  panel.onclick = (e) => {
    const cam = e.target.closest('[data-cam]');
    if (cam) return onCamera(cam.dataset.cam);
    const act = e.target.closest('[data-act]');
    if (act && !act.disabled) onAction(act.dataset.act, act.dataset.id);
  };
  return api;
}
