// G5: the interface builds and shows the modules.
//   node tools/gates/web-modules.mjs     prints WEB MODULES CHECK PASSED only if the build works and the pure render
//   functions of web/src/ui/lighting.js (module switch, lighting panel, operator controls) say what they must.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const WEB = join(ROOT, "web");
const problems = [];
const must = (ok, what) => { if (!ok) problems.push(what); };
const fail = (m) => { console.error("FAILED: " + m); process.exit(1); };

// 1. the interface builds
const build = spawnSync("npm run build", { cwd: WEB, shell: true, encoding: "utf8" });
if (build.status !== 0) fail("npm run build in web/ failed:\n" + ((build.stdout || "") + (build.stderr || "")).split(/\r?\n/).slice(-25).join("\n"));
const dist = join(WEB, "dist", "assets");
const bundle = existsSync(dist) ? readdirSync(dist).filter((f) => f.endsWith(".js")).map((f) => readFileSync(join(dist, f), "utf8")).join("\n") : "";
for (const s of ["/api/modules", "/api/lighting/state", "/api/lighting/override", "/api/lighting/release"])
  must(bundle.includes(s), `the built interface never calls ${s}`);
// the water module is read only: the bundle reads /api/water/state and knows no other /api/water/ route
must(bundle.includes("/api/water/state"), "the built interface never calls /api/water/state");
const waterRoutes = [...bundle.matchAll(/\/api\/water\/[A-Za-z0-9_\-/]*/g)].map((m) => m[0]).filter((r) => r !== "/api/water/state");
must(!waterRoutes.length && !/\/api\/water\/(?!state\b)/.test(bundle), `the built interface has a water route other than /api/water/state: ${waterRoutes.join(", ")}`);

// 2. the pure render functions
const L = await import(pathToFileURL(join(WEB, "src", "ui", "lighting.js")).href);
const now = Math.floor(Date.now() / 1000);
const cab = (id, over = {}) => ({ id: `light-${id}`, cabinet: id, lon: 71.78, lat: 40.38, online: true, mode: "schedule", on: true, level: 100,
  lamps: 24, failed: 0, power_w: 2150, door: "closed", lease_until: null, schedule: "astro", dark: true, age: 3, refused: null,
  alerts: [], override: null, ...over });
const state = { alerts: 3, cabinets: [
  cab("C-01"),
  cab("C-02", { failed: 1, mode: "override", level: 60, lease_until: now + 3600,
    override: { level: 60, until: now + 3600, actor: "oper1", reason: "ремонт линии" }, alerts: ["перегорела лампа"] }),
  cab("C-03", { door: "open" }),
  cab("C-04", { online: false, age: 190, on: false, level: 0, power_w: 0, door: "unknown" }),
  cab("C-05", { on: false, level: 0, power_w: 0, dark: false }),
] };
const modules = [{ id: "traffic", title: "Светофоры и пробки" }, { id: "lighting", title: "Уличное освещение" }];

// the module switch
const sw = L.renderModuleSwitch(modules, "traffic");
must(sw.includes("Светофоры и пробки") && sw.includes("Уличное освещение"), "the module switch lacks a module title");
must(/data-module="traffic"[^>]*aria-selected="true"/.test(sw) && /data-module="lighting"[^>]*aria-selected="false"/.test(sw), "the active module is not marked in the switch");
must(/data-module="lighting"[^>]*aria-selected="true"/.test(L.renderModuleSwitch(modules, "lighting")), "switching to lighting does not mark it");
must(L.renderModuleSwitch([modules[0]]) === "", "a lone traffic module must show no switch");
must(L.renderModuleSwitch(null) === "" && L.renderModuleSwitch(undefined) === "" && L.renderModuleSwitch("oops") === "", "a failed /api/modules must show no switch");
must(L.renderModuleSwitch({ modules }).includes("Уличное освещение"), "the switch does not accept {modules: [...]}");
must(L.renderModuleSwitch([{ id: "traffic" }, { id: "lighting" }]).includes("Уличное освещение"), "a module without a title gets no default title");
must(!L.renderModuleSwitch([...modules, { id: "<script>", title: "x" }]).includes("<script>"), "an unknown module id is rendered");

// the lighting panel, for an operator and for a viewer
const operator = L.renderLightingPanel(state, { canOperate: true, selected: new Set(["C-02"]) });
const viewer = L.renderLightingPanel(state, { canOperate: false });
const rowOf = (html, id) => {
  const i = html.indexOf(`data-cabinet="${id}"`);
  if (i < 0) return "";
  const start = html.lastIndexOf('<div class="item lt-cab', i);
  const next = html.indexOf('<div class="item lt-cab', i);
  return html.slice(start, next < 0 ? html.length : next);
};
for (const c of state.cabinets) must(rowOf(operator, c.cabinet) !== "" && rowOf(viewer, c.cabinet) !== "", `the panel does not list cabinet ${c.cabinet}`);
const alertRow = (id) => /data-alert="1"/.test(rowOf(viewer, id));
must(!alertRow("C-01") && !alertRow("C-05"), "a healthy cabinet is marked as an alert");
must(alertRow("C-02"), "a cabinet with a failed lamp is not marked as an alert");
must(alertRow("C-03"), "a cabinet with an open door is not marked as an alert");
must(alertRow("C-04"), "an offline cabinet is not marked as an alert");
must(rowOf(viewer, "C-02").includes("1 лампа перегорела"), "the failed lamp is not named in the alert");
must(rowOf(viewer, "C-03").includes("дверь открыта") && rowOf(viewer, "C-01").includes("дверь закрыта"), "the door state is not shown");
must(rowOf(viewer, "C-04").includes("нет связи"), "the offline cabinet does not say it has no link");
must(rowOf(viewer, "C-01").includes("на связи"), "the online cabinet does not say it is online");
must(rowOf(viewer, "C-01").includes("по расписанию"), "a scheduled cabinet does not say «по расписанию»");
must(/включён, 100 %/.test(rowOf(viewer, "C-01")) && rowOf(viewer, "C-05").includes("выключен"), "on/off and level are not shown");
must(rowOf(viewer, "C-01").includes("24 лампы") && !/перегор/.test(rowOf(viewer, "C-01")), "the lamp count of a healthy cabinet is wrong");
must(rowOf(viewer, "C-02").includes("24 лампы, 1 перегорела"), "the lamp count with a failed lamp is wrong");
must(rowOf(viewer, "C-01").includes("2.15 кВт"), "the power is not shown in kW");
must(/команда центра до \d\d:\d\d/.test(rowOf(viewer, "C-02")), "an overridden cabinet does not say «команда центра до HH:MM»");
must(!rowOf(viewer, "C-01").includes("команда центра"), "a scheduled cabinet claims a centre command");
must(rowOf(viewer, "C-02").includes("ремонт линии"), "the reason of an override is not shown");
must(viewer.includes(L.SAFETY_TEXT) && L.SAFETY_TEXT.includes("без связи шкаф сам вернётся к расписанию"), "the safety rule is not on the panel");
must(viewer.includes("4 / 5"), "the summary does not count cabinets online");
// alerts come first in the list
must(viewer.indexOf('data-cabinet="C-02"') < viewer.indexOf('data-cabinet="C-01"'), "alerts are not listed first");

// override controls: operator yes, viewer no (and the positive control: the operator render does have them)
const controls = ["data-lt-apply", "data-lt-release", 'name="level"', 'name="minutes"', 'name="reason"', 'data-lt-preset="0"', 'data-lt-preset="50"', 'data-lt-preset="100"'];
for (const k of controls) { must(operator.includes(k), `the operator panel lacks ${k}`); must(!viewer.includes(k), `the viewer panel has ${k}`); }
must(operator.includes(">Применить<") && operator.includes(">Вернуть расписание<") && operator.includes(">Выключить<"), "the operator buttons are not labelled");
must(!viewer.includes(">Применить<") && !viewer.includes("<form") && !viewer.includes('type="checkbox"') && !viewer.includes('type="range"'), "the viewer render has controls");
must(operator.includes('max="720"') && operator.includes('min="1"') && operator.includes('value="60"') && operator.includes("required"), "minutes: range 1-720, default 60, reason required");
must(/data-lt-sel="C-02" checked/.test(operator) && !/data-lt-sel="C-01" checked/.test(operator), "the selection is not reflected in the checkboxes");
must(operator !== viewer, "operator and viewer renders are identical");

// escaping: server text is data
const evil = L.renderLightingPanel({ cabinets: [cab("C-<9>", { refused: "<img src=x onerror=1>" })] }, { canOperate: true });
must(!evil.includes("<img") && !evil.includes("C-<9>"), "cabinet text is not escaped");
// empty and failed states
must(L.renderLightingPanel({ cabinets: [] }, {}).includes("Шкафов освещения пока нет"), "an empty cabinet list is not explained");
must(L.renderLightingPanel(null, { error: "нет связи с центром" }).includes("нет связи с центром"), "a failed fetch is not shown");

// the form -> request
const okReq = L.overrideRequest({ level: "60", minutes: "90", reason: " ремонт " }, ["C-01"]);
must(okReq.ok && okReq.body.level === 60 && okReq.body.minutes === 90 && okReq.body.reason === "ремонт" && okReq.body.cabinets[0] === "C-01", "a good form does not become a good request");
must(!L.overrideRequest({ level: 60, minutes: 60, reason: "" }, ["C-01"]).ok, "an empty reason is accepted");
must(!L.overrideRequest({ level: 60, minutes: 60, reason: "x" }, []).ok, "no cabinet selected is accepted");
must(!L.overrideRequest({ level: 60, minutes: 0, reason: "x" }, ["C-01"]).ok && !L.overrideRequest({ level: 60, minutes: 721, reason: "x" }, ["C-01"]).ok, "minutes outside 1-720 are accepted");
must(!L.overrideRequest({ level: 101, minutes: 60, reason: "x" }, ["C-01"]).ok && !L.overrideRequest({ level: -1, minutes: 60, reason: "x" }, ["C-01"]).ok, "a level outside 0-100 is accepted");
must(typeof L.createLighting === "function", "createLighting is missing");

// the wiring in main.js: modules asked after sign-in, the switch drawn, the map layer on
const main = readFileSync(join(WEB, "src", "main.js"), "utf8");
must(/api\.get\('\/api\/modules'\)/.test(main) && main.includes("renderModuleSwitch") && main.includes("lightingLayers"), "main.js does not fetch the modules, draw the switch and the map layer");
must(existsSync(join(WEB, "src", "scene", "lightingLayer.js")), "the lighting map layer is missing");

// 3. the water module (read only): web/src/ui/water.js
const W = await import(pathToFileURL(join(WEB, "src", "ui", "water.js")).href);
const st = (id, over = {}) => ({ id: `pump-${id}`, site: id, lon: 71.78, lat: 40.38, online: true, age: 2, p_min: 2.0, p_max: 6.0,
  pressure_in: 0.8, pressure_out: 4.2, flow: 118.5, power_kw: 37.2, energy_kwh: 1203344, level: 3.1,
  pumps: [{ run: true, fault: false }, { run: false, fault: false }], door: "closed", kwh_per_m3: 0.31,
  night_min_flow: 41.0, night_baseline: 38.5, source_error: null, refused: null, alerts: [], ...over });
const LEAK = "возможна утечка: ночной минимум 55.0 м³/ч против обычных 38.5";
const water = { alerts: 4, sites: [
  st("S-01"),
  st("S-02", { pumps: [{ run: true, fault: false }, { run: false, fault: true }],
    alerts: [{ kind: "pump-fault", level: "alert", text: "авария насоса 2" }] }),
  st("S-03", { night_min_flow: 55.0, alerts: [{ kind: "leak", level: "warn", text: LEAK }] }),
  st("S-04", { online: false, age: 95, pressure_in: null, pressure_out: null, flow: null, power_kw: null, energy_kwh: null, level: null,
    pumps: [], door: null, kwh_per_m3: null, night_min_flow: null, night_baseline: null, p_min: null, p_max: null,
    alerts: [{ kind: "offline", level: "warn", text: "нет связи" }] }),
  st("S-05", { pressure_out: 1.4, door: "open", alerts: [{ kind: "door", level: "alert", text: "шкаф открыт" },
    { kind: "pressure-low", level: "alert", text: "давление ниже нормы: 1.40 бар (норма от 2.00)" }] }),
] };
const wp = W.renderWaterPanel(water, {});
const wRow = (html, id) => {
  const i = html.indexOf(`data-station="${id}"`);
  if (i < 0) return "";
  const start = html.lastIndexOf('<div class="item wt-st', i);
  const next = html.indexOf('<div class="item wt-st', i);
  return html.slice(start, next < 0 ? html.length : next);
};
for (const s of water.sites) must(wRow(wp, s.id) !== "", `the water panel does not list station ${s.site}`);
must(wp.includes("Водоканал"), "the water panel has no title");
const r1 = wRow(wp, "pump-S-01");
must(r1.includes("4.20 бар") && r1.includes("0.80 бар"), "the station pressures are not shown");
must(r1.includes("норма 2.00–6.00"), "the pressure thresholds are not shown");
must(r1.includes("118.5 м³/ч") && r1.includes("37.2 кВт") && r1.includes("0.31 кВт·ч/м³"), "flow, power or kWh/m³ are not shown");
must(r1.includes("1203344 кВт·ч") && r1.includes("3.10 м"), "the energy counter or level is not shown");
must(r1.includes("Н1 работает") && r1.includes("Н2 стоит") && r1.includes("работает 1"), "the pumps are not shown");
must(r1.includes("шкаф закрыт") && wRow(wp, "pump-S-05").includes("шкаф открыт"), "the door is not shown");
must(r1.includes("ночной минимум 41.0 м³/ч") && r1.includes("обычно 38.5 м³/ч"), "the night minimum vs baseline is not shown");
must(r1.includes("на связи") && !/data-alert="1"/.test(r1), "a healthy station is not shown as healthy");
must(wRow(wp, "pump-S-02").includes("Н2 авария") && wRow(wp, "pump-S-02").includes("авария насоса 2"), "a pump fault is not shown");
must(wRow(wp, "pump-S-03").includes(LEAK), "the leak alert text is not shown");
must(wRow(wp, "pump-S-05").includes("давление ниже нормы"), "the low-pressure alert is not shown");
const r4 = wRow(wp, "pump-S-04");
must(r4.includes("нет связи") && r4.includes("1 мин"), "an offline station does not say it has no link and since when");
must((r4.match(/—/g) || []).length >= 10 && !/\b0\.0+ (бар|м³\/ч|кВт)/.test(r4) && !r4.includes("NaN") && !r4.includes("null") && !r4.includes("undefined"),
  "null readings are not shown as «—» (or a number was invented)");
must(r4.includes("порог не задан"), "a station without thresholds does not say so");
// the worst alert colours the station; the worst stations come first
must(W.stationTone(water.sites[0]) === "ok" && W.stationTone(water.sites[1]) === "bad" && W.stationTone(water.sites[2]) === "warn"
  && W.stationTone(water.sites[3]) === "warn" && W.stationTone(water.sites[4]) === "bad", "a station's worst alert level is wrong");
must(W.stationTone(st("X", { online: false })) === "warn", "an offline station without a server alert is not a warning");
must(wp.indexOf('data-station="pump-S-02"') < wp.indexOf('data-station="pump-S-03"') && wp.indexOf('data-station="pump-S-03"') < wp.indexOf('data-station="pump-S-01"'),
  "stations are not listed worst first");
const s5alerts = W.stationAlerts(st("Y", { alerts: [{ kind: "leak", level: "warn", text: "w" }, { kind: "door", level: "alert", text: "a" }] }));
must(s5alerts[0].tone === "bad" && s5alerts[1].tone === "warn", "a station's alerts are not sorted worst first");
must(W.waterSummary(water) === "5 станций, 4 на связи, 4 с тревогой", `the water summary is wrong: ${W.waterSummary(water)}`);
must(W.waterSummary(null) === "нет данных" && W.waterSummary({ sites: [] }) === "станций нет", "the water summary of no data is wrong");
// read only: no controls anywhere in the water panel
for (const t of ["<button", "<input", "<form", "<select", "<textarea"]) must(!wp.toLowerCase().includes(t), `the water panel has a control (${t})`);
// escaping: device text is data
const HOSTILE = "<img src=x onerror=alert(1)>";
const evilW = W.renderWaterPanel({ sites: [st(HOSTILE, { id: HOSTILE, door: HOSTILE, alerts: [{ kind: HOSTILE, level: HOSTILE, text: HOSTILE }] })] }, { focus: HOSTILE });
must(!evilW.includes("<img") && evilW.includes("&lt;img src=x onerror=alert(1)&gt;"), "water station text is not escaped");
// empty and failed states
must(W.renderWaterPanel({ sites: [] }).includes("Насосных станций пока нет"), "an empty station list is not explained");
must(W.renderWaterPanel(null, { error: "нет связи с центром" }).includes("нет связи с центром"), "a failed water fetch is not shown");
must(typeof W.createWater === "function", "createWater is missing");
// the switch shows «Водоканал» when /api/modules lists water, and not otherwise
const mods3 = [...modules, { id: "water", title: "Водоканал", device_kind: "water", summary: "3 станции" }];
must(L.renderModuleSwitch(mods3, "traffic").includes(">Водоканал<"), "the module switch has no «Водоканал» tab when water is listed");
must(/data-module="water"[^>]*aria-selected="true"/.test(L.renderModuleSwitch(mods3, "water")), "switching to water does not mark it");
must(L.renderModuleSwitch([{ id: "traffic" }, { id: "water" }]).includes(">Водоканал<"), "the water module without a title gets no default title");
must(!L.renderModuleSwitch(modules).includes("Водоканал"), "the «Водоканал» tab shows although water is not listed");
must(main.includes("waterLayers") && main.includes("createWater"), "main.js does not wire the water panel and map layer");
must(existsSync(join(WEB, "src", "scene", "waterLayer.js")), "the water map layer is missing");
const wsrc = readFileSync(join(WEB, "src", "ui", "water.js"), "utf8");
must(!/api\.post|method:\s*['"]POST/i.test(wsrc), "water.js sends something");

if (problems.length) fail(problems.map((p) => "- " + p).join("\n"));
console.log("WEB MODULES CHECK PASSED");
