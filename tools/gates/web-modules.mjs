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

if (problems.length) fail(problems.map((p) => "- " + p).join("\n"));
console.log("WEB MODULES CHECK PASSED");
