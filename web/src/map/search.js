// Search box: streets, facilities, cameras and utility assets. Arrow keys + Enter, "/" to focus.
import { POI } from '../config.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
// fold apostrophe variants (Uzbek oʻ / gʻ are typed in many ways) and ё
const norm = (s) => s.toLowerCase().replace(/[ʻʼ’‘`']/g, "'").replace(/ё/g, 'е');

export function createSearch(scene, { getAssets, onSelect }) {
  const fixed = [];
  for (const [name, mids] of scene.streets) {
    const mean = [mids.reduce((s, p) => s + p[0], 0) / mids.length, mids.reduce((s, p) => s + p[1], 0) / mids.length];
    const p = mids.reduce((best, m) => (scene.distM(m, mean) < scene.distM(best, mean) ? m : best));
    fixed.push({ type: 'street', title: name, sub: 'улица', p, zoom: 16.5 });
  }
  for (const poi of scene.pois) {
    fixed.push({ type: 'poi', title: poi.name || POI[poi.kind].label, sub: POI[poi.kind].label, p: poi.p, ref: poi });
  }
  for (const cam of scene.cams) fixed.push({ type: 'cam', title: cam.id, sub: `камера · ${cam.name}`, p: cam.p, ref: cam });
  for (const e of fixed) e.key = norm(`${e.title} ${e.sub}`);

  const input = $('q'), list = $('results');
  let found = [], active = -1;

  function entries() {
    return fixed.concat(getAssets().map((a) => ({
      type: 'asset', title: a.id, sub: a.metric, p: [a.lon, a.lat], ref: a, key: norm(`${a.id} ${a.metric}`),
    })));
  }
  function render() {
    list.hidden = !found.length;
    list.innerHTML = found.map((e, i) => `<li class="${i === active ? 'sel' : ''}" data-i="${i}">
      <b>${esc(e.title)}</b><span>${esc(e.sub)}</span></li>`).join('');
  }
  function query() {
    const q = norm(input.value.trim());
    active = -1;
    found = q ? entries().filter((e) => e.key.includes(q))
      .sort((a, b) => (b.key.startsWith(q) - a.key.startsWith(q)) || a.title.localeCompare(b.title))
      .slice(0, 8) : [];
    render();
  }
  function choose(i) {
    const e = found[i];
    if (!e) return;
    input.value = e.title;
    found = [];
    render();
    input.blur();
    onSelect(e);
  }

  input.addEventListener('input', query);
  input.addEventListener('focus', query);
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      if (found.length) { active = (active + (ev.key === 'ArrowDown' ? 1 : -1) + found.length) % found.length; render(); }
    } else if (ev.key === 'Enter') {
      choose(active >= 0 ? active : 0);
    } else if (ev.key === 'Escape') {
      found = []; render(); input.blur();
    }
  });
  list.addEventListener('pointerdown', (ev) => {
    const li = ev.target.closest('li');
    if (li) { ev.preventDefault(); choose(Number(li.dataset.i)); }
  });
  input.addEventListener('blur', () => setTimeout(() => { found = []; render(); }, 120));
  window.addEventListener('keydown', (ev) => {
    if (ev.key === '/' && document.activeElement !== input) { ev.preventDefault(); input.focus(); input.select(); }
  });
  return { clear() { input.value = ''; } };
}
