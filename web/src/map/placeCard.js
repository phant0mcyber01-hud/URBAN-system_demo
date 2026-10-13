// Place card: what was clicked on the map or chosen in search.
import { BUILDING_RU, POI, ROAD_RU } from '../config.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));

/** Turns a picked object into {title, sub, rows, action?}. `kind` is the layer id it came from. */
export function describe(kind, d, S) {
  switch (kind) {
    case 'buildings':
      return {
        title: d.name || BUILDING_RU[d.kind] || 'Здание', sub: d.name ? BUILDING_RU[d.kind] || 'Здание' : 'по данным OpenStreetMap',
        rows: [['Высота', `≈ ${Math.round(d.h0)} м${d.est ? ' (оценка)' : ''}`]],
      };
    case 'roads': {
      const jam = S.jamRoads.includes(d);
      return {
        title: d.name || 'Проезд без названия', sub: ROAD_RU[d.cls] ?? 'улица',
        rows: [['Полос в направлении', d.lanes], ['Ограничение', `${d.speed} км/ч`], ['Сейчас', jam ? 'затор' : 'свободно']],
      };
    }
    case 'stops':
      return { title: 'Остановка', sub: 'условная: маршруты в демо сгенерированы', rows: [['Код', d.id]] };
    case 'pois':
      return { title: d.name || POI[d.kind].label, sub: POI[d.kind].label, rows: [] };
    case 'infra':
      return {
        title: d.id, sub: 'объект инфраструктуры (демо)', action: { tab: 'infra', label: 'Открыть в «Инфраструктуре»' },
        rows: [[d.metric, `${d.value} ${d.unit}`], ['Порог', `${d.limit} ${d.unit}`],
          ['Состояние', { ok: 'норма', warn: 'отклонение', alert: 'порог превышен' }[d.status]]],
      };
    case 'detectors': {
      const [n, speed, queue] = S.cams[d.id] ?? [0, 0, 0];
      return {
        title: d.name, sub: `регулируемый перекрёсток · детектор очереди ${d.id}`,
        action: { tab: 'transport', label: 'Открыть «Пробки и светофоры»' },
        rows: [['Устройство URBAN Edge', (() => { const u = S.dev[d.id]; if (!u) return 'нет сведений';
          return u.lock ? 'заблокировано' : u.s === 'revoked' ? 'отозвано' : u.o ? (u.al.length ? u.al[0][1] : 'на связи, норма') : 'нет связи'; })()],
          ...((S.dev[d.id]?.t != null) ? [['Температура в шкафу', `${S.dev[d.id].t} °C`]] : []),
          ['Очередь, самый длинный подъезд', queue == null ? 'нет данных' : `${queue} м`], ['Машин рядом', n], ['Средняя скорость', `${speed} км/ч`]],
      };
    }
    case 'incidents':
      return {
        title: 'ДТП', sub: d.street || d.edge, action: { tab: 'emergency', label: 'Открыть в «Экстренных»' },
        rows: [['Статус', 'активно']],
      };
    case 'street':
      return { title: d.title, sub: 'улица', rows: [] };
    default:
      return null;
  }
}

export function createPlaceCard({ onClose, onAction }) {
  const el = $('place');
  let action = null;
  el.addEventListener('click', (ev) => {
    if (ev.target.closest('#place-close')) { api.hide(); onClose(); }
    if (ev.target.closest('#place-action') && action) onAction(action);
  });
  const api = {
    show(card) {
      action = card.action ?? null;
      el.hidden = false;
      el.innerHTML = `<button id="place-close" title="Закрыть">✕</button>
        <h3>${esc(card.title)}</h3><p class="muted">${esc(card.sub)}</p>
        ${card.rows.map(([k, v]) => `<div class="row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}
        ${action ? `<button id="place-action">${esc(action.label)}</button>` : ''}`;
    },
    hide() { el.hidden = true; action = null; },
    get open() { return !el.hidden; },
  };
  return api;
}
