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
// step 8 built the tunnel: its bullet says it was checked only in QEMU and what stays open, not that it is missing
const wgBullet = unverified.split(/\n- /).find((b) => b.includes("WireGuard")) || "";
const wgLimits = [/только в QEMU/, /настоящ\S* камер/, /VLAN/, /PresharedKey/, /IPv6/].every((rx) => rx.test(wgBullet));
for (const [what, ok] of [["Secure Boot", unverified.includes("Secure Boot")], ["ARM", word(unverified, "ARM")],
  ["WireGuard (checked only in QEMU: real camera, VLAN, PresharedKey, IPv6)", wgLimits]])
  if (!ok) fail(`the «Что не проверено» section of docs/edge-os.md does not mention ${what}`);
if (/Канала WireGuard между перекрёстком и районом нет/.test(text)) fail("docs/edge-os.md still says there is no WireGuard tunnel to the district");

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
// step 8 built the tunnel: the paragraph keeps WireGuard only for what stays open (hardware, PresharedKey, IPv6)
if (!/WireGuard[^;]*на железе/.test(todo[0]) || !/PresharedKey/.test(todo[0]) || !/IPv6/.test(todo[0]))
  fail("the «Не сделано» paragraph of CONTEXT.md does not say what stays open of the WireGuard tunnel (hardware, PresharedKey, IPv6)");
if (context.includes("защищённый канал перекрёсток — район (WireGuard, отдельная сеть камер)"))
  fail("CONTEXT.md still lists the WireGuard channel between the junction and the district as not done");
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

// step 5: the device key lives in the TPM; the docs no longer list it as missing
for (const must of ["ECDSA P-256", "tpm2-tools", "--tpm auto", ".tpm/", "python -m urban.devices.signer"])
  if (!text.includes(must)) fail(`docs/edge-os.md never says ${JSON.stringify(must)} (device key in the TPM)`);
if (/Ключ блока не в самом чипе/.test(text)) fail("docs/edge-os.md still says the unit's key is not in the chip");
const todo5 = /\*\*Не сделано[^\n]*(?:\n(?!\n)[^\n]*)*/.exec(context);
if (todo5 && /подпись внутри TPM/i.test(todo5[0])) fail("the «Не сделано» paragraph of CONTEXT.md still lists signing inside the TPM");
for (const must of ["Ключ устройства в TPM", "ECDSA P-256"])
  if (!context.includes(must)) fail(`CONTEXT.md does not record ${JSON.stringify(must)} (step 5)`);

// step 6: Secure Boot and a verified system
if (!headings.some((h) => /защищённая загрузка|secure boot/.test(h))) fail("docs/edge-os.md has no section on Secure Boot");
for (const must of ["dm-verity", "db.crt", "check_signatures=enforce", "check-secureboot.mjs", "slot-A.cfg", "KEK"])
  if (!text.includes(must)) fail(`docs/edge-os.md never says ${JSON.stringify(must)} (Secure Boot)`);
if (/Защищённая загрузка \(Secure Boot\) не включена/.test(text)) fail("docs/edge-os.md still says Secure Boot is not enabled");
if (todo5 && /Secure Boot/.test(todo5[0]) && !/Secure Boot на (железе|мини-ПК)/.test(todo5[0]))
  fail("the «Не сделано» paragraph of CONTEXT.md still lists Secure Boot as not done");
if (!context.includes("Защищённая загрузка (шаг 6)")) fail("CONTEXT.md does not record step 6 (Защищённая загрузка (шаг 6))");

// step 7: PCR 7 seal with recovery, the boot-disk rule, the version floor; what stays open, said plainly
for (const must of ["--tpm2-pcrs=7", "urban-recovery.enc", "os/recovery.mjs", "DATA LOCKED", "/dev/disk/urban/",
  "check-pcr7.mjs", "check-bootdisk.mjs", "urban.release floor"])
  if (!text.includes(must)) fail(`docs/edge-os.md never says ${JSON.stringify(must)} (step 7)`);
if (/без привязки к PCR|не привязан к (состоянию Secure Boot|измерениям загрузки \(PCR\))/i.test(text))
  fail("docs/edge-os.md still says the data key is not bound to PCRs");
if (!/старый подписанный выпуск|старую подписанную версию/i.test(text) || !/измеряем\S* загрузк/i.test(text))
  fail("docs/edge-os.md does not say that booting an old signed release with the disk in hand stays open (measured boot is later)");
if (!/перемонтир/i.test(text)) fail("docs/edge-os.md does not say why the ESP is not made read-only (root can remount it)");
if (!context.includes("(шаг 7)")) fail("CONTEXT.md does not record step 7");

// step 8: the WireGuard tunnel between each junction unit and its district; its intruder row and its honest limits
const sectionOf = (doc, rx) => {  // a "## " section up to the next "## " heading, or ""
  const h = rx.exec(doc);
  if (!h) return "";
  const after = doc.slice(h.index + h[0].length);
  const next = /^## /m.exec(after);
  return next ? after.slice(0, next.index) : after;
};
const tunnel = sectionOf(text, /^## Туннель до района.*$/m);
if (!tunnel) fail("docs/edge-os.md has no section on the tunnel to the district («Туннель до района»)");
for (const must of ["51820", "10.77.0.0/16", "wg-peers", "wg-link", "wg-inbox", "urban-wg.path", "urban-wg.timer",
  "urban-wg.service", "urban-wg boot", "plain_relay", "/run/urban/relay-mode", "wg-cameras.json", "rtsp_transport",
  "check-wireguard.mjs", "wireguard-tools", "urban.net.wireguard keygen", "net.ipv4.conf.wg0.forwarding",
  "/var/lib/urban/wg", "URBAN-WG", "Принять ключ туннеля", "Закрыть открытый порт района"])
  if (!text.includes(must)) fail(`docs/edge-os.md never says ${JSON.stringify(must)} (the tunnel to the district)`);
// the honest limits, one marker each: what the tunnel does not close or what was not tried
for (const [what, rx] of [["checked only in QEMU", /Проверено только в QEMU/], ["the tunnel key is a root file, not in the TPM", /Curve25519/],
  ["no PresharedKey, no IPv6", /PresharedKey[\s\S]*IPv6/], ["the first key is trusted on first use", /на доверии/],
  ["the helper's trust anchor is pinned at setup", /[Яя]корь доверия помощника/], ["the clock rule knows only the build date", /только дату сборки/],
  ["mode only is an admin's switch", /Режим `only`/], ["message versions are the centre's clock (rotate-gateway)", /rotate-gateway/],
  ["the district endpoint is an IPv4 address, no DNS name", /имени DNS/], ["a camera cabled straight to the district is no longer read", /напрямую/],
  ["SSH stays open, cookie replies under load", /cookie/], ["registry races, reused district numbers", /assign_index/],
  ["gateway key rotation", /Смена ключа шлюза/], ["re-sent until applied («не применено»)", /не применено/],
  ["revoking a district does not cut its units' tunnels", /Отзыв или удаление района/]])
  if (!rx.test(tunnel)) fail(`the tunnel section of docs/edge-os.md does not state the limit: ${what}`);
// the intruder table has a row for the wire between the unit and the district
const intruderRows = sectionOf(text, /^## .*взломщик.*$/m).split("\n").filter((l) => l.startsWith("|"));
if (!intruderRows.some((l) => /район/.test(l) && /WireGuard/.test(l) && /не видео/.test(l)))
  fail("the intruder table of docs/edge-os.md has no row for the wire between the unit and the district");
// the mini-PC guide: the unit's two networks, UDP 51820 and the stand checklist
if (!gh.some((h) => /две сети/.test(h) && /туннел/.test(h))) fail("mini-pc.md has no section on the two networks and the tunnel");
for (const must of ["UDP 51820", "wg-cameras.json", "/var/lib/urban/etc/network/"])
  if (!g.includes(must)) fail(`mini-pc.md never says ${JSON.stringify(must)} (the tunnel to the district)`);
const checklist = sectionOf(g, /^## Чек-лист.*$/m);
if (!/51820/.test(checklist) || !/туннел/.test(checklist) || !/две сети/i.test(checklist))
  fail("the stand checklist of mini-pc.md does not check the two networks and the tunnel");
// vision: RTSP over TCP, through the tunnel
const vision = readFileSync(join(ROOT, "docs", "vision.md"), "utf8");
for (const must of ["rtsp_transport", "10.77.", "wg-cameras.json", "через туннель"])
  if (!vision.includes(must)) fail(`docs/vision.md never says ${JSON.stringify(must)} (RTSP through the tunnel)`);
// the design doc says it was built in step 8, points to GATES.md and no longer teaches what was not built
const design = readFileSync(join(ROOT, "docs", "wireguard-design.md"), "utf8");
if (!/построен на шаге 8/.test(design) || !design.includes("GATES.md")) fail("docs/wireguard-design.md does not say it was built in step 8 and point to GATES.md");
if (!design.includes("/var/lib/urban/wg/key") || /pki\/wg\/|wg syncconf wg0|проект шага 7|Ворота шага 7/.test(design))
  fail("docs/wireguard-design.md still describes the first draft (key in pki/wg, wg syncconf, step 7 gates)");
if (!context.includes("(шаг 8)") || !/\| [0-9–]+ октября \| шаг 8/.test(context)) fail("CONTEXT.md does not record step 8 (section and history row)");
for (const must of ["UDP 51820", "10.77.0.0/16", "urban-wg", "wg-inbox", "check-wireguard.mjs", "PresharedKey"])
  if (!context.includes(must)) fail(`CONTEXT.md does not record ${JSON.stringify(must)} (step 8)`);

// step 9: the stand kit. Every unit's image from unit-image.mjs has its own GUIDs and GRUB password (kept next to the
// image, then in the unit's passport); the keys get an encrypted off-line backup; the stand's installation order; the
// production keys stay the owner's decision. The old limits (one GRUB password for the fleet, identical GUIDs) are closed.
const order = sectionOf(g, /^## Порядок установки стенда.*$/m);
if (!order) fail("mini-pc.md has no section «Порядок установки стенда»");
// the steps, in this order: BIOS before the first boot (fTPM, Secure Boot keys with PK last, Secure Boot on, USB and PXE off, BIOS password), Rufus,
// first boot, urban-recovery.enc, the two networks and cameras, enrolment, the per-unit GRUB password
const steps = [["BIOS before the first boot from the internal disk", /BIOS[^\n]*до первой загрузки с внутреннего диска/],
  ["fTPM on", /fTPM/], ["URBAN Secure Boot keys enrolled", /ключи Secure Boot URBAN внесены/], ["PK last", /PK\.cer/],
  ["Secure Boot on", /Secure Boot —\s+Enabled/], ["USB boot off", /USB Boot/], ["PXE off", /PXE/],
  ["BIOS password", /пароль на настройки BIOS/i], ["Rufus", /Rufus/], ["first boot", /Первая загрузка/], ["urban-recovery.enc", /urban-recovery\.enc/],
  ["two networks and cameras", /wg-cameras\.json/], ["enrolment", /Подключение в центре/], ["the GRUB password file", /\.grub-password/]];
let at = 0;
for (const [what, rx] of steps) {
  const m = rx.exec(order.slice(at));
  if (!m) fail(`the «Порядок установки стенда» of mini-pc.md does not give, in order, the step: ${what}`);
  at += m.index + m[0].length;
}
for (const must of ["node os/unit-image.mjs", "data/os/release/", ".grub-password", "паспорт блока", "keys-backup.mjs"])
  if (!order.includes(must)) fail(`the «Порядок установки стенда» of mini-pc.md never says ${JSON.stringify(must)}`);
if (/У копий одного образа\s+одинаковые GUID/.test(g)) fail("mini-pc.md still says every copy of one image has the same GUIDs (images from unit-image.mjs have their own)");
for (const must of ["unit-image.mjs", ".grub-password", "keys-backup.mjs", "check-keys-backup.mjs", "URBAN_KEYS_PASSPHRASE",
  "--cipher-algo AES256", "боевые", "не сделано"])
  if (!text.includes(must)) fail(`docs/edge-os.md never says ${JSON.stringify(must)} (step 9)`);
const keysSection = sectionOf(text, /^## Ключи: копия вне ноутбука.*$/m);
if (!keysSection) fail("docs/edge-os.md has no section on the key backup («Ключи: копия вне ноутбука …»)");
for (const [what, rx] of [["backup", /keys-backup\.mjs backup/], ["list", /keys-backup\.mjs list/], ["restore", /keys-backup\.mjs restore/],
  ["a wrong passphrase writes nothing", /неверная фраза[^.]*не пишут ничего/], ["two copies off the laptop", /две копии/],
  ["production keys: a fresh key folder on an offline machine", /без\s+сети/], ["every unit written again", /записываются заново/],
  ["the owner's decision, not done in step 9", /решение владельца[^.]*в шаге 9 оно не сделано/]])
  if (!rx.test(keysSection)) fail(`the key backup section of docs/edge-os.md does not say: ${what}`);
// «Известные пределы защищённой загрузки»: the fleet's one GRUB password and the identical GUIDs are closed, and say by what
const limits = (/\*\*Известные пределы защищённой загрузки\.\*\*[\s\S]*?(?=\n## )/.exec(text) || [""])[0];
if (!limits) fail("docs/edge-os.md has no «Известные пределы защищённой загрузки»");
if (/Один пароль обслуживания GRUB на весь парк\.\*\*|Узнавший его на одном блоке может править строку загрузки на любом/.test(text))
  fail("docs/edge-os.md still lists one GRUB password for the whole fleet as an open limit");
if (/все блоки, записанные с одного образа, имеют одинаковые GUID|случайные GUID на каждый блок при\s+установке \(следующий шаг\)/.test(text))
  fail("docs/edge-os.md still says every unit has identical partition GUIDs (closed for images from unit-image.mjs)");
if (/один пароль GRUB на весь парк/.test(text)) fail("the «Что не проверено» summary of docs/edge-os.md still says one GRUB password for the fleet");
const gpLimit = limits.split(/\n(?=\d+\. )/).find((b) => /пароль обслуживания GRUB/i.test(b)) || "";
if (!/свой у каждого блока/.test(gpLimit) || !gpLimit.includes("unit-image.mjs") || !gpLimit.includes(".grub-password") || !/node os\/build\.mjs/.test(gpLimit))
  fail("the GRUB password limit of docs/edge-os.md does not say it is closed per unit by unit-image.mjs and that the fleet image keeps the shared one");
const guidLimit = limits.split(/\n(?=\d+\. )/).find((b) => /GUID/.test(b)) || "";
if (!/закрыты для образов из/.test(guidLimit) || !guidLimit.includes("unit-image.mjs"))
  fail("the boot-disk limit of docs/edge-os.md does not say identical GUIDs are closed for images from unit-image.mjs");
// CONTEXT.md: the step-9 section, its history row, and «Не сделано» no longer lists what step 9 closed
if (!context.includes("(шаг 9)") || !/\| 23 октября \| шаг 9/.test(context)) fail("CONTEXT.md does not record step 9 (section and history row)");
for (const must of ["keys-backup.mjs", ".grub-password", "check-keys-backup.mjs", "Порядок установки стенда"])
  if (!context.includes(must)) fail(`CONTEXT.md does not record ${JSON.stringify(must)} (step 9)`);
const todo9 = /\*\*Не сделано[^\n]*(?:\n(?!\n)[^\n]*)*/.exec(context)[0];
if (/случайные GUID разделов на каждый блок|отдельные пароли GRUB/.test(todo9))
  fail("the «Не сделано» paragraph of CONTEXT.md still lists per-unit GUIDs or GRUB passwords as not done");
if (!/боевые ключи/.test(todo9)) fail("the «Не сделано» paragraph of CONTEXT.md does not keep the production keys (the owner's step)");
console.log("DOCS CHECK PASSED");
