"""The calibration page of an URBAN Edge unit: which camera watches which approach, and where the road is.

    python -m urban.vision setup --cameras /etc/urban/cameras.json --unit edge-J-07
    ssh -L 8090:127.0.0.1:8090 urban@<unit>       then open the address it prints, on the installer's laptop

Runs on the unit and listens on the unit itself only; a one-time token in the address keeps anyone else
on the cabinet network out. Camera frames are shown here, to the person standing at the cabinet, and go
nowhere else: the platform never sees a picture. The installer clicks four points on a frame - the two
ends of the stop line and two points a measured distance up the approach - types the distance, and sees
the lanes drawn back over the road to check them. The approaches come from the junction's passport,
delivered with the unit's kit.
"""
import hmac
import html
import json
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..sensors.geometry import Homography
from . import cameras as cams
from .source import VideoSource, redact

CAMERA_ID = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def grab(source: str):
    """One frame from a camera, or None. Nothing is kept."""
    for _, frame in VideoSource(source, fps=1, realtime=False).frames(threading.Event()):
        return frame
    return None


class Setup:
    def __init__(self, cameras: Path, junction: Path | None, model: str | None = None):
        self.path = cameras
        self.junction = json.loads(junction.read_text(encoding="utf-8")) if junction and junction.exists() else {}
        self.detector = None
        if model:
            from .detector import YoloxDetector
            self.detector = YoloxDetector(Path(model))
        self.lock = threading.Lock()

    def state(self) -> dict:
        return {"cameras": [{**c, "source": redact(c.get("source", "")), "ready": cams.calibrated(c)} for c in cams.load(self.path)],
                "approaches": self.junction.get("approaches", []), "junction": self.junction.get("junction", ""),
                "name": self.junction.get("name", "")}

    def upsert(self, body: dict) -> dict:
        ident = str(body.get("camera", "")).strip().lower()
        if not ident or len(ident) > 32 or set(ident) - set(CAMERA_ID):
            raise ValueError("имя камеры: латиница, цифры, - и _, например cam-1")
        with self.lock:
            config = cams.load(self.path)
            cam = next((c for c in config if c["camera"] == ident), None)
            if cam is None:
                cam = {"camera": ident}
                config.append(cam)
            if body.get("source"):
                cam["source"] = str(body["source"]).strip()
            if not cam.get("source"):
                raise ValueError("укажите адрес потока камеры (rtsp://…) или файл")
            known = {a["id"] for a in self.junction.get("approaches", [])}
            if "approach" in body:
                if known and body["approach"] not in known:
                    raise ValueError("такого подъезда нет в паспорте перекрёстка")
                cam["approach"] = str(body["approach"])
            for field, lo, hi, kind in (("lanes", 1, 6, int), ("lane_width", 2.5, 5.0, float), ("zone", 20, 250, float)):
                if field in body:
                    value = kind(body[field])
                    if not lo <= value <= hi:
                        raise ValueError(f"{field}: от {lo} до {hi}")
                    cam[field] = value
            cams.save(self.path, config)
            return cam

    def calibrate(self, body: dict) -> dict:
        points = body.get("points")
        if not isinstance(points, list) or len(points) != 4:
            raise ValueError("нужны ровно четыре точки")
        clean = [{"px": [float(p["px"][0]), float(p["px"][1])], "m": [float(p["m"][0]), float(p["m"][1])]} for p in points]
        Homography([((p["px"][0], p["px"][1]), (p["m"][0], p["m"][1])) for p in clean])  # raises if degenerate
        with self.lock:
            config = cams.load(self.path)
            cam = next((c for c in config if c["camera"] == body.get("camera")), None)
            if cam is None:
                raise ValueError("нет такой камеры")
            cam["points"] = clean
            cams.save(self.path, config)
            return cam

    def remove(self, ident: str):
        with self.lock:
            cams.save(self.path, [c for c in cams.load(self.path) if c["camera"] != ident])

    def snapshot(self, ident: str, overlay: bool) -> bytes | None:
        import cv2
        cam = next((c for c in cams.load(self.path) if c["camera"] == ident), None)
        if cam is None or not cam.get("source"):
            return None
        frame = grab(cam["source"])
        if frame is None:
            return None
        if overlay:
            from .draw import overlay as draw
            boxes = self.detector.detect(frame) if self.detector is not None else []
            frame = draw(frame, cam, boxes)
        ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return jpeg.tobytes() if ok else None


def handler(setup: Setup, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _authorised(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            given = query.get("token", [""])[0] or (cookie["urban_setup"].value if "urban_setup" in cookie else "")
            return hmac.compare_digest(given.encode(), token.encode())

        def _send(self, code: int, body: bytes, kind: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._authorised():
                return self._send(403, "Нужна ссылка с ключом, которую напечатал блок.".encode(), "text/plain; charset=utf-8")
            url = urlparse(self.path)
            if url.path == "/":
                cookie = {"Set-Cookie": f"urban_setup={token}; HttpOnly; SameSite=Strict; Path=/"}
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8", cookie)
            if url.path == "/api/state":
                return self._json(200, setup.state())
            if url.path == "/snapshot.jpg":
                q = parse_qs(url.query)
                jpeg = setup.snapshot(q.get("camera", [""])[0], q.get("overlay", ["0"])[0] == "1")
                if jpeg is None:
                    return self._send(502, "Камера не отдала кадр: проверьте адрес и сеть.".encode(), "text/plain; charset=utf-8")
                return self._send(200, jpeg, "image/jpeg")
            self._send(404, b"", "text/plain")

        def do_POST(self):
            if not self._authorised():
                return self._send(403, b"", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/camera":
                    return self._json(200, setup.upsert(body))
                if path == "/api/points":
                    return self._json(200, setup.calibrate(body))
                if path == "/api/remove":
                    setup.remove(str(body.get("camera", "")))
                    return self._json(200, {"ok": True})
                self._send(404, b"", "text/plain")
            except (ValueError, KeyError, TypeError, IndexError) as err:
                self._json(400, {"error": str(err)})

    return Handler


def serve(args):
    cameras = Path(args.cameras)
    junction = Path(args.junction) if args.junction else (cameras.parent / f"{args.unit}.junction.json" if args.unit else None)
    setup = Setup(cameras, junction, args.model)
    token = secrets.token_urlsafe(18)
    server = ThreadingHTTPServer((args.host, args.port), handler(setup, token))
    where = setup.junction.get("junction") or "junction unknown (no kit next to the cameras file)"
    print(f"calibration page for {html.escape(where)}: http://{args.host}:{args.port}/?token={token}", flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: listening beyond this unit; camera pictures are visible to whoever has the address", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>URBAN Edge · камеры</title>
<style>
:root { --bg:#0e1013; --s:#15181c; --s2:#1b1f24; --line:#2a3036; --t:#e4e7eb; --t2:#a1a8b1; --t3:#6f7782;
        --accent:#4c9aff; --ok:#3fb950; --warn:#e3a33a; --bad:#e5534b; }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--t); font:13px/1.45 system-ui, "Segoe UI", sans-serif; }
header { padding:12px 16px; border-bottom:1px solid var(--line); background:var(--s); display:flex; gap:12px; align-items:baseline }
header b { letter-spacing:.14em } header span { color:var(--t3) }
main { display:grid; grid-template-columns:320px 1fr; min-height:calc(100vh - 46px) }
aside { border-right:1px solid var(--line); padding:12px 16px; background:var(--s) }
section { padding:12px 16px }
h4 { margin:16px 0 6px; font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--t3) }
label { display:grid; gap:4px; color:var(--t2); font-size:12px; margin:6px 0 }
input, select { background:var(--bg); color:var(--t); border:1px solid var(--line); border-radius:4px; padding:7px 9px; font:inherit }
button { background:var(--s2); color:var(--t); border:1px solid var(--line); border-radius:4px; padding:6px 12px; cursor:pointer; font:inherit }
button.go { background:var(--accent); border-color:var(--accent); color:#06121f; font-weight:600 }
.cam { padding:8px 6px; border-bottom:1px solid var(--line); cursor:pointer; display:flex; justify-content:space-between; gap:8px }
.cam:hover, .cam.sel { background:var(--s2) } .muted { color:var(--t2) } .err { color:var(--bad) }
.chip { font-size:11.5px } .chip.ok::before { content:"● "; color:var(--ok) } .chip.warn::before { content:"▲ "; color:var(--warn) }
.stage { position:relative; display:inline-block; max-width:100%; cursor:crosshair }
.stage img { display:block; max-width:100%; border:1px solid var(--line) }
.stage svg { position:absolute; inset:0; width:100%; height:100% }
.row { display:flex; gap:8px; align-items:end; flex-wrap:wrap } .row label { margin:0 }
code { font-family:Consolas, monospace }
</style></head><body>
<header><b>URBAN EDGE</b><span id="where">камеры блока</span></header>
<main>
<aside>
  <p class="muted">Кадры видны только здесь, на блоке. В платформу уходят метры очереди.</p>
  <h4>Камеры</h4><div id="list"></div>
  <h4>Добавить или изменить</h4>
  <form id="add">
    <label>Имя <input name="camera" placeholder="cam-1" required></label>
    <label>Адрес потока <input name="source" placeholder="rtsp://${CAM1_USER}:${CAM1_PASS}@10.0.0.11/stream1"></label>
    <label>Подъезд <select name="approach"></select></label>
    <div class="row"><label>Полос <input name="lanes" type="number" min="1" max="6" value="2" style="width:70px"></label>
      <label>Ширина полосы, м <input name="lane_width" type="number" step="0.1" min="2.5" max="5" value="3.5" style="width:90px"></label>
      <label>Зона, м <input name="zone" type="number" min="20" max="250" value="100" style="width:80px"></label></div>
    <p><button class="go">Сохранить</button> <span class="err" id="adderr"></span></p>
  </form>
  <p class="muted">Пароли камер — в env-файле блока как <code>CAM1_USER</code>, <code>CAM1_PASS</code>; в адресе пишутся
    <code>${CAM1_USER}</code>, на странице не показываются.</p>
</aside>
<section>
  <div id="empty" class="muted">Выберите камеру слева.</div>
  <div id="work" hidden>
    <div class="row"><b id="title"></b><button id="refresh">Новый кадр</button><button id="restart">Отметить заново</button></div>
    <p class="muted" id="hint"></p>
    <div class="stage"><img id="shot" alt="кадр камеры"><svg id="marks"></svg></div>
    <div class="row" id="measure" hidden>
      <label>Расстояние от стоп-линии до дальней линии, м <input id="dist" type="number" min="5" max="200" step="0.5" value="30" style="width:110px"></label>
      <button class="go" id="savepts">Сохранить калибровку</button>
    </div>
    <p class="err" id="err"></p>
  </div>
</section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const STEPS = ['левый конец стоп-линии (у бордюра)', 'правый конец стоп-линии (у осевой)',
  'точку дальней линии напротив правого конца', 'точку дальней линии напротив левого конца'];
let state = { cameras: [], approaches: [] }, cur = null, clicks = [];
const post = (path, body) => fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  .then(async (r) => { const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j; });

async function load() {
  state = await fetch('/api/state').then((r) => r.json());
  $('where').textContent = state.junction ? `${state.junction} · ${state.name}` : 'перекрёсток неизвестен: нет комплекта рядом с файлом камер';
  $('add').approach.innerHTML = state.approaches.map((a) => `<option value="${esc(a.id)}">${esc(a.from || a.id)} · ${esc(a.name)}</option>`).join('')
    || '<option value="">— нет паспорта —</option>';
  $('list').innerHTML = state.cameras.map((c) => `<div class="cam ${c.camera === cur ? 'sel' : ''}" data-cam="${esc(c.camera)}">
      <span><b>${esc(c.camera)}</b> ${esc(c.approach || '')}<br><small class="muted">${esc(c.source)}</small></span>
      <span class="chip ${c.ready ? 'ok' : 'warn'}">${c.ready ? 'откалибрована' : 'нет калибровки'}</span></div>`).join('')
    || '<p class="muted">Камер пока нет.</p>';
}
function pick(id) {
  cur = id; clicks = [];
  const c = state.cameras.find((x) => x.camera === id);
  $('empty').hidden = true; $('work').hidden = false;
  $('title').textContent = `${c.camera} → ${c.approach || 'подъезд не выбран'}`;
  const f = $('add'); f.camera.value = c.camera; f.source.value = ''; f.approach.value = c.approach || '';
  f.lanes.value = c.lanes ?? 2; f.lane_width.value = c.lane_width ?? 3.5; f.zone.value = c.zone ?? 100;
  shot(c.ready); load();
}
function shot(withOverlay) {
  $('err').textContent = '';
  $('shot').src = `/snapshot.jpg?camera=${encodeURIComponent(cur)}&overlay=${withOverlay ? 1 : 0}&n=${Date.now()}`;
  hint();
}
function hint() {
  $('measure').hidden = clicks.length < 4;
  $('hint').textContent = clicks.length < 4 ? `Отметьте ${STEPS[clicks.length]} (${clicks.length + 1} из 4).`
    : 'Четыре точки отмечены. Введите расстояние между стоп-линией и дальней линией и сохраните; полосы будут нарисованы поверх дороги.';
  const img = $('shot'), sx = img.clientWidth / (img.naturalWidth || 1), sy = img.clientHeight / (img.naturalHeight || 1);
  $('marks').innerHTML = clicks.map(([u, v], i) => `<circle cx="${u * sx}" cy="${v * sy}" r="6" fill="none" stroke="#4c9aff" stroke-width="2"/>
    <text x="${u * sx + 8}" y="${v * sy - 8}" fill="#4c9aff" font-size="13">${i + 1}</text>`).join('')
    + (clicks.length > 1 ? `<polyline fill="none" stroke="#4c9aff" stroke-dasharray="4 3" points="${clicks.map(([u, v]) => `${u * sx},${v * sy}`).join(' ')}"/>` : '');
}
$('shot').onload = hint;
$('shot').onerror = () => { $('err').textContent = 'Камера не отдала кадр: проверьте адрес потока и сеть шкафа.'; };
$('shot').onclick = (e) => {
  if (clicks.length >= 4) return;
  const img = $('shot'), r = img.getBoundingClientRect();
  clicks.push([(e.clientX - r.left) * img.naturalWidth / r.width, (e.clientY - r.top) * img.naturalHeight / r.height]);
  hint();
};
$('restart').onclick = () => { clicks = []; shot(false); };
$('refresh').onclick = () => shot(state.cameras.find((x) => x.camera === cur)?.ready);
$('savepts').onclick = async () => {
  const c = state.cameras.find((x) => x.camera === cur), d = Number($('dist').value);
  const w = (c.lanes ?? 2) * (c.lane_width ?? 3.5);
  const m = [[0, 0], [0, w], [d, w], [d, 0]];
  try {
    await post('/api/points', { camera: cur, points: clicks.map((px, i) => ({ px, m: m[i] })) });
    clicks = []; await load(); shot(true);
  } catch (err) { $('err').textContent = err.message; }
};
$('list').onclick = (e) => { const el = e.target.closest('[data-cam]'); if (el) pick(el.dataset.cam); };
$('add').onsubmit = async (e) => {
  e.preventDefault(); $('adderr').textContent = '';
  const f = Object.fromEntries(new FormData(e.target));
  if (!f.source) delete f.source;
  try { const c = await post('/api/camera', f); await load(); pick(c.camera); } catch (err) { $('adderr').textContent = err.message; }
};
load();
</script></body></html>
"""
