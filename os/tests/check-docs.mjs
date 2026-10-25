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
// step 11 built the arm64 image: ARM stays in the section only for what is open (encryption on an ARM board with a TPM)
const armOpen = word(unverified, "ARM") && /[Шш]ифрование на ARM-плате с TPM не\s+проверено/.test(unverified);
for (const [what, ok] of [["Secure Boot", unverified.includes("Secure Boot")],
  ["ARM (encryption on an ARM board with a TPM not checked)", armOpen],
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
// step 11 built the arm64 image: the paragraph keeps ARM only for what stays open (encryption on an ARM board with a TPM)
if (!word(todo[0], "ARM") || !/шифрование на ARM-плате с TPM/.test(todo[0]))
  fail("the «Не сделано» paragraph of CONTEXT.md does not say that encryption on an ARM board with a TPM is not checked");
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
// step 10: measured boot - designed and proven on a software TPM (built into the OS is step 11)
const mb = join(ROOT, "docs", "measured-boot-design.md");
if (!existsSync(mb)) fail("docs/measured-boot-design.md is missing");
const design10 = readFileSync(mb, "utf8");
const h10 = [...design10.matchAll(/^##+ (.+)$/gm)].map((m) => m[1].toLowerCase());
const need10 = {
  "the two attacks": /две атаки/, "the pitfalls": /подводные камни/,
  "the mechanism (every PCR, NV index, key, file and who writes each)": /механизм.*pcr.*nv|pcr.*nv.*ключ/,
  "the boot-time sequence": /последовательность загрузки/, "the update-time sequence": /последовательность обновления/,
  "recovery and failure cases": /восстановление и отказ/, "what bookworm lacks": /чего нет в bookworm/,
  "the answers to the review (B1-B3, M1-M2)": /ответы на ревью/,
  "what stays open": /остаётся открытым/, "the step-11 gates": /ворота шага 11/,
};
const miss10 = Object.entries(need10).filter(([, rx]) => !h10.some((h) => rx.test(h))).map(([k]) => k);
if (miss10.length) fail("docs/measured-boot-design.md sections missing: " + miss10.join(", "));
for (const must of ["PolicyAuthorize", "PolicyNV", "PolicyPCR", "PolicyCommandCode", "PolicyNvWritten",
  "PCR 7", "PCR 11", "NV_FLOOR", "capped", "policywrite", "systemd-cryptenroll", "tpm2-tools", "swtpm",
  "check-tpm-policy.mjs", "urban-recovery.enc", "urban-health", "старый подписанный выпуск", "флешк", "GUID",
  "TPM2_Clear", "обрыв питания", "dbx", "ключ восстановления", "стадийн", "phase", "superusers", "init=",
  "db-сертификат", "security version", "systemd 252", "base+", "LUKS2"]) {
  if (!design10.includes(must)) fail(`docs/measured-boot-design.md never says ${JSON.stringify(must)}`);
}
// the attack section names both attacks plainly
if (!/старый.*подписанн/i.test(design10) || !/подлинн\S* образ.*флешк|образ.*с флешки/i.test(design10))
  fail("docs/measured-boot-design.md does not state both attacks (old signed release, genuine image from USB)");
// the review answers name every finding
for (const f of ["B1", "B2", "B3", "M1", "M2"])
  if (!new RegExp(`\\*\\*${f}\\b`).test(design10)) fail(`the «Ответы на ревью» of docs/measured-boot-design.md does not answer ${f}`);
// the second review: its own subsection answers N1-N4 and the dbx choice, and the decisions are in the mechanism and gates
const second = (/^### Ответы на второе ревью[\s\S]*?(?=\n## )/m.exec(design10) || [""])[0];
if (!second) fail("docs/measured-boot-design.md has no subsection «Ответы на второе ревью»");
for (const f of ["N1", "N2", "N3", "N4"])
  if (!new RegExp(`\\*\\*${f}\\b`).test(second)) fail(`«Ответы на второе ревью» does not answer ${f}`);
for (const must of ["urban-phase-lock.service", "FailureAction=reboot", "serial-getty@", "emergency.target",
  "rescue.target", "SYSTEMD_SULOGIN_FORCE", "debug-shell.service", "имя EK", "dbx", "OVMF", "HSM"])
  if (!second.includes(must)) fail(`«Ответы на второе ревью» never says ${JSON.stringify(must)}`);
const gates11 = (/^## Ворота шага 11[\s\S]*?(?=\n## )/m.exec(design10) || [""])[0];
for (const g of ["S1", "S6", "S7", "S8", "S9"])
  if (!gates11.includes(`**${g}.`)) fail(`the step-11 gates of docs/measured-boot-design.md have no ${g}`);
if (!/Компрометация ключа владельца/.test(design10)) fail("docs/measured-boot-design.md does not name owner-key compromise as an open limit");
if (design10.length < 8000) fail("docs/measured-boot-design.md is too short to be the design");
// CONTEXT.md records step 10: section, history row, and the design doc + check are named
if (!context.includes("Измеряемая загрузка (шаг 10)")) fail("CONTEXT.md has no «Измеряемая загрузка (шаг 10)» section");
if (!context.includes("(шаг 10)") || !/\| 23 октября \| шаг 10/.test(context)) fail("CONTEXT.md does not record step 10 (section and history row)");
for (const must of ["docs/measured-boot-design.md", "check-tpm-policy.mjs", "PCR 11", "NV_FLOOR",
  "PolicyAuthorize", "стадийн", "ревью"])
  if (!context.includes(must)) fail(`CONTEXT.md does not record ${JSON.stringify(must)} (step 10)`);

// step 11: URBAN OS for 64-bit ARM boards with UEFI (arm64). The guide says how to build and write the image, which boards
// it is for, how Secure Boot keys get onto such a board, the TPM limit (no TPM in QEMU's virt for Debian's arm64 kernel:
// plain data, said on the screen), what was checked only in QEMU; ARM is no longer listed as missing
const arm = sectionOf(text, /^## Сборка под ARM \(шаг 11\).*$/m);
if (!arm) fail("docs/edge-os.md has no section «Сборка под ARM (шаг 11)»");
for (const must of ["node os/unit-image.mjs --arch arm64", "arch: 'arm64'", "urban-edge-arm64-", ".img.xz", "BOOTAA64.EFI",
  "compatible=urban-edge-aarch64", "debootstrap --foreign", "grub-mkimage", "linux-image-arm64", "zstd", "squash4",
  "verify_detached", "console=ttyAMA0,115200", "aavmf-vars-urban.fd", "arm64-handover-to-kernel-if-sb-enabled",
  "tpm-tis-device", "CONFIG_TCG_TIS", "CONFIG_TCG_CRB", "tpm_tis_spi", "NOT encrypted - this machine has no TPM",
  "data=rw,plain", "lockdown", "U-Boot", "EDK2", "PK.cer", ".esl", "data/os/keys/sb/", "check-image.mjs arm64",
  "check-arm.mjs", "check-arm.mjs update", "169 МБ", "Access Denied", "Rufus", "Get-FileHash", "--devices"])
  if (!arm.includes(must)) fail(`the «Сборка под ARM (шаг 11)» section of docs/edge-os.md never says ${JSON.stringify(must)}`);
for (const [what, rx] of [["boards: 64-bit ARM with UEFI firmware", /64-битн\S* ARM[^.]*UEFI/],
  ["a board without UEFI is not supported", /без UEFI[\s\S]{0,80}не поддерживается/],
  ["enrolling the keys depends on the board's firmware", /зависит от прошивки платы/],
  ["no TPM means no encryption, said on the screen", /[Пп]лата без TPM[^.]*обычным разделом данных[^.]*говорит это на экране/],
  ["encryption on ARM is checked on hardware only", /[Шш]ифрование[^.]*на ARM[^.]*только на плате/],
  ["the TPM drivers of real boards were read from the kernel config, not tried", /прочитано в конфигурации ядра[^.]*не пробовали/],
  ["checked only in QEMU, no real board", /[Тт]олько в QEMU[\s\S]{0,40}ни\s+одной настоящей ARM-платы/],
  ["os/build.mjs has no --arch", /os\/build\.mjs[^.]*--arch[^.]*не знает/]])
  if (!rx.test(arm)) fail(`the «Сборка под ARM (шаг 11)» section of docs/edge-os.md does not say: ${what}`);
// the old "no ARM build" lines are gone, everywhere they stood
for (const [rx, what] of [[/Сборки под ARM нет/, "«Сборки под ARM нет»"], [/\*\*Только x86_64\.\*\*/, "«Только x86_64»"],
  [/сборка под ARM — отдельный шаг, её пока нет/, "«сборка под ARM — отдельный шаг, её пока нет»"]])
  if (rx.test(text)) fail(`docs/edge-os.md still lists the ARM build as missing: ${what}`);
if (/ARM-платы не подойдут/.test(g)) fail("mini-pc.md still says ARM boards will not do (step 11 built the arm64 image)");
for (const must of ["--arch arm64", "UEFI", "Сборка под ARM (шаг 11)"])
  if (!g.includes(must)) fail(`mini-pc.md never says ${JSON.stringify(must)} (ARM boards)`);
// CONTEXT.md: the step-11 section, its history row; «Не сделано» no longer lists the ARM build, keeps measured boot pending
const ctx11 = sectionOf(context, /^## Сборка под ARM \(шаг 11\).*$/m);
if (!ctx11 || !/\| 24 октября \| шаг 11/.test(context)) fail("CONTEXT.md does not record step 11 (section and history row)");
for (const must of ["BOOTAA64.EFI", "urban-edge-aarch64", "tpm-tis-device", "CONFIG_TCG_TIS", "169 МБ", "check-arm.mjs",
  "отложена", "2026-10-23-measured-boot-build-draft.md", "без UEFI"])
  if (!ctx11.includes(must)) fail(`the «Сборка под ARM (шаг 11)» section of CONTEXT.md never says ${JSON.stringify(must)}`);
const todo11 = /\*\*Не сделано[^\n]*(?:\n(?!\n)[^\n]*)*/.exec(context)[0];
if (/сборка под ARM[;,]/i.test(todo11)) fail("the «Не сделано» paragraph of CONTEXT.md still lists the ARM build as not done");
if (!/измеряем\S* загрузк[^;]*(отложен|следующ)/.test(todo11))
  fail("the «Не сделано» paragraph of CONTEXT.md does not keep the measured boot build as pending");
if (/встройка в URBAN OS — шаг 11/.test(context)) fail("CONTEXT.md still says measured boot is built in step 11 (it is parked)");

// step 12: releases by architecture and staged rollout. The «Обновления из центра» section gives the commands, the rules
// (the compatible read from the signed package, numbering per platform, no reported compatible = x86, waves by a stable
// rank), what halts a rollout and what a halt does, how to resume or withdraw, and the honest limits
const ota = sectionOf(text, /^## Обновления из центра.*$/m);
if (!ota) fail("docs/edge-os.md has no section «Обновления из центра»");
for (const must of ["--devices", "--wave", "release promote", "--all", "release resume", "release withdraw", "--arch",
  "urban-edge-x86_64", "urban-edge-aarch64", "RAUC_MF_COMPATIBLE", "/etc/rauc/system.conf", "rollout-halted",
  "update-offered", "GET /api/releases", "Выпуски ОС", "раздача остановлена", "restart pending", "check-rollout.mjs",
  "### Выпуски по платформам (шаг 12)", "### Волны и сама остановка раздачи (шаг 12)", "### Пределы выпусков и волн"])
  if (!ota.includes(must)) fail(`the «Обновления из центра» section of docs/edge-os.md never says ${JSON.stringify(must)} (step 12)`);
for (const [what, rx] of [
  ["the compatible is read from the signed package, any other system refused", /[Пп]латформа пакета читается из самого подписанного пакета[\s\S]{0,300}другой системы центр\s+не\s+принимает/],
  ["each platform has its own numbering", /[Уу] каждой платформы свои номера/],
  ["a device that reports no compatible is treated as x86", /платформу не сообщает\*\*[\s\S]{0,120}считается x86/],
  ["the wave rank is stable: the same devices go first", /первыми всегда идут одни\s+и те же устройства/],
  ["what halts: a failure or a rollback of a device of the wave", /Что останавливает раздачу[\s\S]{0,300}из его волны[\s\S]*\*\*Неудачу\.\*\*[\s\S]*\*\*Откат\.\*\*/],
  ["what does not halt (retried with the next offer)", /\*\*Не останавливают\*\*/],
  ["a halted release is offered to no device", /больше \*\*ни одному\*\* устройству он не предлагается/],
  ["devices already on it are left as they are", /остаются как есть/],
  ["a halt stops every package of that version number (x86 and arm64)", /номер выпуска целиком[\s\S]{0,200}x86 и arm64/],
  ["a package of a halted number added later starts halted", /добавленный\s+позже, сразу остановлен/],
  ["resume and withdraw are the admin's", /Решение за администратором[\s\S]{0,200}release resume N[\s\S]{0,400}release withdraw N/],
  ["the centre trusts what the devices report", /[Цц]ентр верит тому, что сообщают устройства/],
  ["checked only in QEMU", /Проверено только в QEMU/],
  ["the rollout does not update the centre itself", /[Рр]аздача центр не обновляет/]])
  if (!rx.test(ota)) fail(`the «Обновления из центра» section of docs/edge-os.md does not say: ${what} (step 12)`);
// what stood before step 12 and is no longer true is gone
for (const [rx, what] of [[/Политики поэтапной раздачи, кроме/, "«Политики поэтапной раздачи, кроме --devices … нет»"],
  [/снимают и выпускают заново под тем же номером/, "re-adding a release to widen it (promote does that now)"],
  [/Раздача пакета arm64 из центра не\s+проверялась/, "«Раздача пакета arm64 из центра не проверялась»"],
  [/центр не различает архитектуру блока/, "«центр не различает архитектуру блока»"],
  [/команда прямо\s+пишет, что эту сверку не делала/, "release add without rauc (it refuses now: the compatible must be read)"]])
  if (rx.test(text)) fail(`docs/edge-os.md still says what step 12 changed: ${what}`);
if (!text.includes("node os/tests/check-rollout.mjs")) fail("the «Проверка» list of docs/edge-os.md does not show check-rollout.mjs");
// CONTEXT.md: the step-12 section and its history row; «Не сделано» no longer lists staged rollout; «Что дальше» no
// longer says step 12 has no code (the centre's self-update, open in step 12, is step 13's, below)
const ctx12 = sectionOf(context, /^## Выпуски по платформам и волны \(шаг 12\).*$/m);
if (!ctx12 || !/\| 24 октября \| шаг 12:/.test(context)) fail("CONTEXT.md does not record step 12 (section and history row)");
for (const must of ["urban-edge-x86_64", "urban-edge-aarch64", "RAUC_MF_COMPATIBLE", "/etc/rauc/system.conf", "--wave",
  "release promote", "release resume", "release withdraw", "rollout-halted", "check-rollout.mjs", "test_rollout.py",
  "x86 и arm64", "верит тому, что сообщают устройства", "только в QEMU", "вручную"])
  if (!ctx12.includes(must)) fail(`the «Выпуски по платформам и волны (шаг 12)» section of CONTEXT.md never says ${JSON.stringify(must)}`);
const todo12 = /\*\*Не сделано[^\n]*(?:\n(?!\n)[^\n]*)*/.exec(context)[0];
if (/поэтапн\S* раздач/.test(todo12)) fail("the «Не сделано» paragraph of CONTEXT.md still lists staged rollout as not done");
if (/шаг 12 \(начат, кода нет\)|кода шага 12 в репозитории нет/.test(context)) fail("CONTEXT.md still says step 12 has no code");
if (!/волнами/.test(readme) || !readme.includes("release resume")) fail("README.md does not mention the waves and resuming a halted rollout");

// step 13: the centre updates itself. The «Обновления из центра» section has its own subsection: the command, what it
// refuses, what happens (the platform hands the package to the same root helper, the reboot, the audit), a bad version,
// the data kept, what the operators see while the centre reboots, the honest limits, and what check-center-update.mjs
// checks; nothing in the guide says any more that the centre is updated by hand only
const self13 = (() => {
  const h = /^### Самообновление центра \(шаг 13\).*$/m.exec(ota);
  if (!h) return "";
  const rest = ota.slice(h.index + h[0].length);
  const next = /^###? /m.exec(rest);
  return next ? rest.slice(0, next.index) : rest;
})();
if (!self13) fail("the «Обновления из центра» section of docs/edge-os.md has no subsection «Самообновление центра (шаг 13)»");
for (const must of ["urban-center release self-update", "refused:", "urban-unit apply-update", "centre-update-started",
  "centre-updated", "centre-rolled-back", "`centre`", "release N for <compatible>, from M", "now N", "started N, back on M",
  "/etc/rauc/system.conf", "RAUC_MF_VERSION", "release resume N", "check-center-update.mjs", "ROLE=district,center",
  "docs/software.md"])
  if (!self13.includes(must)) fail(`the «Самообновление центра (шаг 13)» subsection of docs/edge-os.md never says ${JSON.stringify(must)}`);
for (const [what, rx] of [
  ["no automatic self-update: the admin names the version", /[Аа]втоматического самообновления нет/],
  ["refused: a halted release", /\*\*остановлен\*\*/],
  ["refused: a withdrawn release", /\*\*снят\*\*/],
  ["refused: not newer than the running version", /\*\*не новее\*\* работающей/],
  ["refused: no release N for this machine's platform", /выпуска N для платформы этой машины нет/],
  ["the platform does not install the system itself", /платформа работает без привилегий и систему не ставит/],
  ["the outcome is written once, after the next boot", /\*\*один раз\*\* пишет итог/],
  ["a bad version: the boot loader brings the old slot back by itself", /[Пп]лохая версия[\s\S]{0,200}загрузчик сам возвращает прежний слот/],
  ["the data partition is the same: database, enrolments, keys, audit", /[Рр]аздел данных обновление не трогает: база[^.]*подключения[^.]*ключи[^.]*журнал/],
  ["the devices come back by themselves", /подключаются заново сами/],
  ["while the centre reboots the operators have no interface", /[Пп]ока центр перезагружается[^.]*интерфейса нет/],
  ["the units keep running on their own (lease, base plan)", /[Бб]локи в это время работают сами[^.]*базовый план/],
  ["commands time out by design", /закрываются по сроку[\s—-]*так задумано/],
  ["Devices shows the centre's own system and its last self-update", /«Устройства»[^.]*систем\S* самого центра[^.]*последнее\s+самообновление/],
  ["a version that breaks after the self-check is not rolled back", /самопроверку прошла, а сломалась позже[\s\S]{0,120}сама не откатывается/],
  ["the pilot machine updates both roles at once", /обновляет обе роли сразу/],
  ["checked only in QEMU, without KVM", /[Пп]роверено только в QEMU\*\*, без KVM/],
  ["the check: release 2, data the same, the unit back online, centre-updated", /выпуск 2[\s\S]{0,300}те же, что до обновления, блок сам снова на связи/],
  ["the check: a broken 3 rolls back to 2 by itself", /сломанный выпуск 3[\s\S]{0,200}сам возвращается на версию 2/],
  ["the check: a halted release is refused", /[Оо]становленный выпуск команда отвергает/]])
  if (!rx.test(self13)) fail(`the «Самообновление центра (шаг 13)» subsection of docs/edge-os.md does not say: ${what}`);
// what stood before step 13 and is no longer true is gone, everywhere it stood
for (const [rx, what] of [[/[Сс]ам центр обновляется по-прежнему вручную/, "«Сам центр обновляется по-прежнему вручную»"],
  [/[Сс]ам центр\s+обновляется по-прежнему вручную/, "«Сам центр обновляется по-прежнему вручную» (wrapped)"],
  [/центр пока\s+обновляется вручную/, "«центр пока обновляется вручную»"]]) {
  if (rx.test(text)) fail(`docs/edge-os.md still says the centre is updated by hand: ${what}`);
  if (rx.test(context)) fail(`CONTEXT.md still says the centre is updated by hand: ${what}`);
  if (rx.test(readme)) fail(`README.md still says the centre is updated by hand: ${what}`);
}
if (!text.includes("node os/tests/check-center-update.mjs")) fail("the «Проверка» list of docs/edge-os.md does not show check-center-update.mjs");
if (!/Самообновление\s+центра \(шаг 13\) проверено только в QEMU/.test(unverified))
  fail("the «Что не проверено» section of docs/edge-os.md does not say the centre's self-update is checked only in QEMU");
if (!/release self-update N/.test(readme)) fail("README.md does not mention the centre's self-update (release self-update N)");
// CONTEXT.md: the step-13 section, the state paragraph, its history row; «Не сделано» and «Что дальше» no longer list
// the centre's self-update as missing, «Не сделано» keeps what stays open of it
const ctx13 = sectionOf(context, /^## Самообновление центра \(шаг 13\).*$/m);
if (!ctx13 || !/\| 24 октября \| шаг 13:/.test(context)) fail("CONTEXT.md does not record step 13 (section and history row)");
for (const must of ["urban-center release self-update N", "refused:", "urban-unit apply-update", "centre-update-started",
  "centre-updated", "centre-rolled-back", "check-center-update.mjs", "test_self_update.py", "/etc/rauc/system.conf",
  "Автоматического", "только в QEMU", "обновляет обе роли сразу", "сама не откатывается", "нет интерфейса"])
  if (!ctx13.includes(must)) fail(`the «Самообновление центра (шаг 13)» section of CONTEXT.md never says ${JSON.stringify(must)}`);
if (!/\*\*Шаг 13 — центр обновляет себя сам\.\*\*/.test(context)) fail("the state paragraph of CONTEXT.md does not record step 13");
const todo13 = /\*\*Не сделано[^\n]*(?:\n(?!\n)[^\n]*)*/.exec(context)[0];
if (/обновление самого центра|самообновление центра по-прежнему ручное/.test(todo13))
  fail("the «Не сделано» paragraph of CONTEXT.md still lists the centre's self-update as not done");
if (!/самообновление центра \(шаг 13\) проверено только в QEMU/.test(todo13))
  fail("the «Не сделано» paragraph of CONTEXT.md does not say the centre's self-update is checked only in QEMU");
const next13 = sectionOf(context, /^## Что дальше.*$/m);
if (/обновление самого центра из центра/.test(next13)) fail("the «Что дальше» list of CONTEXT.md still lists the centre's self-update");

// step 14: the pilot machine's cameras reach the centre. The guide has its own section: what was missing, what the machine
// does by itself at first boot (its own district name and key, enrolled at its own centre as an ordinary district with no
// code typed, in Devices and the audit, reports over loopback, the same district after a restart), what the admin still
// does (routing junctions through it, naming the cameras' approaches), what does not change, the honest limits and what
// check-pilot-cameras.mjs checks; nothing in the guide says any more that the pilot's cameras give the centre nothing
const pilot14 = sectionOf(text, /^## Камеры пилотной машины \(шаг 14\).*$/m);
if (!pilot14) fail("docs/edge-os.md has no section «Камеры пилотной машины (шаг 14)»");
for (const must of ["ROLE=district,center", "district-center", "urban-district@district-center", "127.0.0.1:8443",
  "district-enrolled", "urban-center provision assign edge-J-07 --district district-center", "/etc/urban/cameras.json",
  "`approach`", "«Устройства", "check-pilot-cameras.mjs", "data/videos", "urban-prepare", "/run/urban/own-district.code",
  "urban-center provision own-kit", "UPDATES=off", "DEVICE_NAME", "own-district.json"])
  if (!pilot14.includes(must)) fail(`the «Камеры пилотной машины (шаг 14)» section of docs/edge-os.md never says ${JSON.stringify(must)}`);
for (const [what, rx] of [
  ["before: the pilot had no district name, key or enrolment", /не было ни имени района, ни ключа, ни кода подключения/],
  ["its own key, made once, never replaced, not the centre's", /[Кк]люч делается один раз и не\s+заменяется никогда/],
  ["enrolled at its own centre as an ordinary district, no code typed", /[Цц]ентр при запуске сам подключает этот ключ как обычный район[\s\S]{0,200}[Кк]од никто не вводит/],
  ["shown in Devices and audited like any district", /в «Устройствах» он виден отдельной строкой, в журнале[\s\S]{0,80}как у любого района/],
  ["reports over the machine's loopback, the same channel as a separate district", /через петлю самой машины,\s+тем же каналом, что отдельный район/],
  ["the same district after a restart, never a second", /второго\s+района не появляется/],
  ["a district the admin revoked or removed stays so", /отозвал или удалил, так и остаётся/],
  ["the centre takes only the queues of junctions routed through it", /только очереди подходов тех перекрёстков,\s+которые проведены через него/],
  ["a junction not routed through it is not taken", /не проведённого через этот\s+район, центр не принимает/],
  ["the admin routes junctions and names the cameras' approaches", /\*\*Что делает администратор\.\*\* Две вещи[\s\S]*Провести перекрёстки через этот район[\s\S]*Назвать подходы камер/],
  ["a separate district, a unit and a plain centre do not change", /\*\*Что не меняется\.\*\*[\s\S]{0,300}\(`ROLE=center`\) своего района не получают/],
  ["still no relay on the pilot", /Ретранслятора у пилотной\s+машины по-прежнему нет/],
  ["checked only in QEMU, on recorded video", /\*\*Проверено только в QEMU\*\*, без KVM, на записанном видео/],
  ["the check: real video, real vision, the queue in the centre, the same district after a restart, the control",
    /настоящее видео[\s\S]{0,40}настоящее распознавание[\s\S]{0,400}центр показывает очередь этого подхода[\s\S]{0,200}тот же район, а не второй[\s\S]{0,200}не\s+берёт \(контроль\)/]])
  if (!rx.test(pilot14)) fail(`the «Камеры пилотной машины (шаг 14)» section of docs/edge-os.md does not say: ${what}`);
const roles14 = text.split("\n").find((l) => l.startsWith("| `district,center` |")) || "";
if (!/district-center/.test(roles14)) fail("the roles table of docs/edge-os.md does not say the pilot's district side is a district of its own (district-center)");
// what stood before step 14 and is no longer true is gone, everywhere it stood
for (const [rx, what] of [[/Эту машину нельзя подключить как район/, "«Эту машину нельзя подключить как район»"],
  [/камеры пилотной машины пока ничего не\s+отдают центру/i, "«камеры пилотной машины пока ничего не отдают центру»"],
  [/\*\*Камеры пилотной машины\*\* \(район и центр на одной\) пока ничего не передают/, "«Камеры пилотной машины … пока ничего не передают центру»"],
  [/машину нельзя\s+подключить как район/, "«машину нельзя подключить как район»"],
  [/камеры пилотной машины \(район и центр на одной\) ничего не передают центру/, "«камеры пилотной машины … ничего не передают центру»"]]) {
  if (rx.test(text)) fail(`docs/edge-os.md still says the pilot's cameras give the centre nothing: ${what}`);
  if (rx.test(context)) fail(`CONTEXT.md still says the pilot's cameras give the centre nothing: ${what}`);
  if (rx.test(readme)) fail(`README.md still says the pilot's cameras give the centre nothing: ${what}`);
}
if (!text.includes("node os/tests/check-pilot-cameras.mjs")) fail("the «Проверка» list of docs/edge-os.md does not show check-pilot-cameras.mjs");
if (!/Камеры пилотной машины \(шаг 14\) проверены только в QEMU/.test(unverified))
  fail("the «Что не проверено» section of docs/edge-os.md does not say the pilot's cameras are checked only in QEMU");
if (!/district-center/.test(readme) || !readme.includes("Камеры пилотной машины (шаг 14)"))
  fail("README.md does not mention the pilot machine's own district (district-center, «Камеры пилотной машины (шаг 14)»)");
// CONTEXT.md: the step-14 section, the state paragraph, its history row; the roles table; «Не сделано» keeps what stays
// open, «Что дальше» no longer lists the pilot's cameras
const ctx14 = sectionOf(context, /^## Камеры пилотной машины \(шаг 14\).*$/m);
if (!ctx14 || !/\| 24 октября \| шаг 14:/.test(context)) fail("CONTEXT.md does not record step 14 (section and history row)");
for (const must of ["district-center", "urban-district@district-center", "127.0.0.1:8443", "district-enrolled",
  "Код никто не вводит", "--district district-center", "approach", "check-pilot-cameras.mjs", "test_pilot_district.py",
  "data/videos", "только в QEMU", "Ничего не меняется", "второй раз"])
  if (!ctx14.includes(must)) fail(`the «Камеры пилотной машины (шаг 14)» section of CONTEXT.md never says ${JSON.stringify(must)}`);
if (!/\*\*Шаг 14 — камеры пилотной машины доходят до центра\.\*\*/.test(context)) fail("the state paragraph of CONTEXT.md does not record step 14");
const ctxRoles14 = context.split("\n").find((l) => l.startsWith("| `district,center` |")) || "";
if (!/district-center/.test(ctxRoles14)) fail("the roles table of CONTEXT.md does not say the pilot's district side is a district of its own");
const todo14 = /\*\*Не сделано[^\n]*(?:\n(?!\n)[^\n]*)*/.exec(context)[0];
if (!/камеры пилотной машины \(шаг 14\) проверены только в QEMU/.test(todo14))
  fail("the «Не сделано» paragraph of CONTEXT.md does not say the pilot's cameras are checked only in QEMU");
const next14 = sectionOf(context, /^## Что дальше.*$/m);
if (/камеры самой пилотной машины/.test(next14)) fail("the «Что дальше» list of CONTEXT.md still lists the pilot machine's cameras");

console.log("DOCS CHECK PASSED");
