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
  "the district relay": /районный сервер/, "the light unit": /лёгкий блок/, "encryption": /шифрован/, "the demo": /демонстрац/,
  "what an intruder gets": /взломщик/, "speed": /скорост/,
};
const missing = Object.entries(need).filter(([, rx]) => !headings.some((h) => rx.test(h))).map(([k]) => k);
if (missing.length) fail("sections missing: " + missing.join(", "));
for (const cmd of ["node os/build.mjs", "urban-unit setup", "urban-unit update", "urban-unit.txt", "ROLE=",
  "district,center", "urban-power", "/run/urban/power.json", "check-roles.mjs", "Подписывает команды только центр",
  "CENTER=", "VISION=off", "urban-relay", "LUKS2", "TPM", "--tpm2-device=auto", "node os/demo.mjs", "WireGuard", "ARM",
  "check-chain.mjs", "check-encrypted.mjs", "check-light.mjs", "provision assign", "видит только шифртекст",
  "check-hardened.mjs", "check-speed.mjs", "lockdown=confidentiality", "ptrace_scope = 3", "systemd-analyze security",
  "vm.py measure", "Честные пределы", "sntrup761"]) {
  if (!text.includes(cmd)) fail(`the guide never shows ${cmd}`);
}
if (text.length < 4000) fail("the guide is too short to be followed");

// «Что не проверено»: the section itself (up to the next "## " heading) must say plainly the limits that matter
const heading = /^##+ .*не проверено.*$/im.exec(text);
if (!heading) fail("docs/edge-os.md has no «Что не проверено» section");
const rest = text.slice(heading.index + heading[0].length);
const nextH2 = /^## /m.exec(rest);
const unverified = nextH2 ? rest.slice(0, nextH2.index) : rest;
const word = (t, w) => new RegExp(`(?<![A-Za-z0-9])${w}(?![A-Za-z0-9])`).test(t);  // whole word (JS \b does not know Cyrillic neighbours)
for (const [what, ok] of [["Secure Boot", unverified.includes("Secure Boot")], ["ARM", word(unverified, "ARM")], ["WireGuard", unverified.includes("WireGuard")]])
  if (!ok) fail(`the «Что не проверено» section of docs/edge-os.md does not mention ${what}`);

// step 3: updates delivered by the centre are described, and no longer listed as missing
if (!headings.some((h) => /обновлени.*(из центра|центр)/.test(h))) fail("docs/edge-os.md has no section on updates from the centre");
for (const must of ["urban-center release add", "update-inbox", "urban-update.path", "/run/urban/update.json", "check-ota.mjs",
  "RAUC_MF_VERSION", "новее"]) {
  if (!text.includes(must)) fail(`the guide never shows ${must} (updates from the centre)`);
}
if (/не раздаёт обновлени|Доставка обновлений с платформы не сделана/i.test(text))
  fail("docs/edge-os.md still says the centre does not deliver updates");

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
  "urban-unit setup", "urban-unit code", "Power On", "ADAPTER=none", "не запускался ни разу", "fTPM", "--vision off", "--center"]) {
  if (!g.includes(must)) fail(`mini-pc.md never says ${JSON.stringify(must)}`);
}
const context = readFileSync(join(ROOT, "CONTEXT.md"), "utf8");
if (!context.includes("docs/mini-pc.md") || !context.includes("unit-image.mjs")) fail("CONTEXT.md does not record the mini-PC step");
for (const must of ["## Три уровня", "Решения владельца", "Подписывает только центр", "Кадры не выходят за пределы района",
  "ROLE=district,center", "node/power.py", "Не сделано (следующие шаги)", "Районный сервер пересылает", "LUKS2",
  "VISION=off", "node os/demo.mjs", "WireGuard", "без привязки к PCR"]) {
  if (!context.includes(must)) fail(`CONTEXT.md does not record ${JSON.stringify(must)}`);
}
// the "Не сделано" paragraph (up to the next blank line) names the two big gaps
const todo = /\*\*Не сделано[^\n]*(?:\n(?!\n)[^\n]*)*/.exec(context);
if (!todo) fail("CONTEXT.md has no «Не сделано» paragraph");
if (!todo[0].includes("WireGuard")) fail("the «Не сделано» paragraph of CONTEXT.md does not mention WireGuard");
if (!word(todo[0], "ARM")) fail("the «Не сделано» paragraph of CONTEXT.md does not mention ARM");
if (/обновлени/i.test(todo[0]) && /платформ|центр/i.test(todo[0]) && /не раздаёт|по SSH/i.test(todo[0]))
  fail("the «Не сделано» paragraph of CONTEXT.md still lists updates from the centre as missing");
for (const must of ["urban-center release", "Обновления из центра"])
  if (!context.includes(must)) fail(`CONTEXT.md does not record ${JSON.stringify(must)} (step 3)`);

// step 4: the City OS core and its modules, street lighting the second
const modPath = join(ROOT, "docs", "city-os-modules.md");
if (!existsSync(modPath)) fail("docs/city-os-modules.md is missing");
const mod = readFileSync(modPath, "utf8");
for (const must of ["Core.send", "device_kind", "MODULES", "/api/modules", "enroll-lighting", "MODULE=lighting",
  "urban-lighting@", "python -m urban.modules.lighting", "аренда", "расписани", "## Как написать модуль"]) {
  if (!mod.includes(must)) fail(`docs/city-os-modules.md never says ${JSON.stringify(must)}`);
}
const readme = readFileSync(join(ROOT, "README.md"), "utf8");
if (!/уличное освещение/i.test(readme) || !readme.includes("docs/city-os-modules.md"))
  fail("README.md does not mention the street lighting module and docs/city-os-modules.md");
for (const must of ["Ядро и модули", "уличн", "docs/city-os-modules.md"])
  if (!context.includes(must)) fail(`CONTEXT.md does not record ${JSON.stringify(must)} (step 4)`);
console.log("DOCS CHECK PASSED");
