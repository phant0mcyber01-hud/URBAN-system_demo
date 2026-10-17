// docs/edge-os.md covers what a person needs to build, flash, start, update and trust the unit's OS,
// and says plainly what has not been tried on real hardware.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { ROOT, fail } from "./lib.mjs";

const path = join(ROOT, "docs", "edge-os.md");
if (!existsSync(path)) fail("docs/edge-os.md is missing");
const text = readFileSync(path, "utf8");
const headings = [...text.matchAll(/^##+ (.+)$/gm)].map((m) => m[1].toLowerCase());
const need = {
  "building the image": /сборк/, "writing it onto the unit": /запис/, "first boot": /первая загрузка/,
  "updates": /обновлени/, "rollback": /откат/, "not tried on hardware": /не проверено/,
  "levels and roles": /уровн.*рол|рол.*уровн/, "power": /мощност/,
};
const missing = Object.entries(need).filter(([, rx]) => !headings.some((h) => rx.test(h))).map(([k]) => k);
if (missing.length) fail("sections missing: " + missing.join(", "));
for (const cmd of ["node os/build.mjs", "urban-unit setup", "urban-unit update", "urban-unit.txt", "ROLE=",
  "district,center", "urban-power", "/run/urban/power.json", "check-roles.mjs", "Подписывает команды только центр"]) {
  if (!text.includes(cmd)) fail(`the guide never shows ${cmd}`);
}
if (text.length < 4000) fail("the guide is too short to be followed");

// docs/mini-pc.md: from a box to an enrolled unit, for a person with a Windows laptop
const guide = join(ROOT, "docs", "mini-pc.md");
if (!existsSync(guide)) fail("docs/mini-pc.md is missing");
const g = readFileSync(guide, "utf8");
const gh = [...g.matchAll(/^##+ (.+)$/gm)].map((m) => m[1].toLowerCase());
const gneed = {
  "requirements": /что нужно/, "files": /файлы/, "writing the image": /запись/, "firmware settings": /bios|uefi/,
  "first boot": /первая загрузка/, "enrolment": /подключение к центру/, "maintenance": /обслуживание/,
  "faults": /неполадки/, "stand checklist": /чек-лист/, "not tried": /не проверено/,
};
const gmissing = Object.entries(gneed).filter(([, rx]) => !gh.some((h) => rx.test(h))).map(([k]) => k);
if (gmissing.length) fail("mini-pc.md sections missing: " + gmissing.join(", "));
for (const must of ["node os/unit-image.mjs", "Rufus", "Secure Boot", "maintenance_ed25519", "Get-FileHash",
  "urban-unit setup", "urban-unit code", "Power On", "ADAPTER=none", "не запускался ни разу"]) {
  if (!g.includes(must)) fail(`mini-pc.md never says ${JSON.stringify(must)}`);
}
const context = readFileSync(join(ROOT, "CONTEXT.md"), "utf8");
if (!context.includes("docs/mini-pc.md") || !context.includes("unit-image.mjs")) fail("CONTEXT.md does not record the mini-PC step");
for (const must of ["## Три уровня", "Решения владельца", "Подписывает только центр", "Кадры не выходят за пределы района",
  "ROLE=district,center", "node/power.py", "Не сделано (следующие шаги)"]) {
  if (!context.includes(must)) fail(`CONTEXT.md does not record ${JSON.stringify(must)}`);
}
console.log("DOCS CHECK PASSED");
