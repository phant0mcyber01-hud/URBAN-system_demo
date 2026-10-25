// G10: the public deck speaks plainly and promises self-updating; the team deck names what changed.
// Reads the decks' sources (docs/deck-public/project, docs/deck/project): slide text and speaker notes.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const problems = [];

function deck(dir) {
  const index = JSON.parse(readFileSync(join(ROOT, dir, "deck.json"), "utf8"));
  const files = readdirSync(join(ROOT, dir, "slides")).filter((f) => f.endsWith(".html")).map((f) => f.slice(0, -5));
  const order = index.order || [];
  for (const id of order) if (!files.includes(id)) problems.push(`${dir}: ${id} is in the order but has no slide file`);
  for (const id of files) if (!order.includes(id)) problems.push(`${dir}: slides/${id}.html is not in the order`);
  const slides = {};
  for (const id of order.filter((x) => files.includes(x))) {
    const html = readFileSync(join(ROOT, dir, "slides", `${id}.html`), "utf8");
    slides[id] = html.replace(/<[^>]+>/g, " ").replace(/&#160;|&nbsp;/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&").replace(/\s+/g, " ");
  }
  return slides;
}

// words a city official or a resident should not have to know
const JARGON = [/\bTPM\b/i, /\bQEMU\b/i, /\bRAUC\b/i, /\bSSH\b/i, /\broot\b/i, /\bLUKS2?\b/i, /\bDNAT\b/i, /\bnftables\b/i,
  /\bWireGuard\b/i, /\bsystemd\b/i, /\bTLS\b/, /\bsquashfs\b/i, /Secure Boot/i, /\bARM\b/, /\bx86/i, /\bKVM\b/i,
  /\bDebian\b/i, /\bLinux\b/i, /\bGRUB\b/i, /ядр[оае]/i, /сертификат/i, /шифртекст/i, /виртуальн/i, /\bOTA\b/i];

function jargon(slides) {
  const found = [];
  for (const [id, text] of Object.entries(slides))
    for (const re of JARGON) {
      const m = text.match(re);
      if (m) found.push(`${id}: «${m[0]}»`);
    }
  return found;
}

const pub = deck("docs/deck-public/project");
const team = deck("docs/deck/project");

const pubJargon = jargon(pub);
if (pubJargon.length) problems.push(`public deck uses technical words: ${pubJargon.join(", ")}`);
// the control: the same scan must find such words in the team deck, or it proves nothing
const teamJargon = jargon(team);
if (teamJargon.length < 5) problems.push(`control failed: the scan finds only ${teamJargon.length} technical words in the team deck`);

const pubAll = Object.values(pub).join(" ");
if (!/обновля\S*\s+(сам|сама|само|сами)(?![а-яё])/i.test(pubAll) || !/без выезда/i.test(pubAll))
  problems.push("public deck does not say the system updates itself without a trip to the junction");

const changes = Object.entries(team).filter(([id]) => /^changes/.test(id));
if (!changes.length) problems.push("team deck has no changes slide (id changes...)");
const changesText = changes.map(([, t]) => t).join(" ");
for (const [what, re] of [["step 2", /шаг\s*2/i], ["step 3", /шаг\s*3/i], ["updates from the centre", /обновлени\S*.{0,40}центр|центр.{0,40}обновлени/i],
  ["the district relay", /район/i], ["encryption", /шифр/i], ["hardening", /взлом/i], ["speed", /скорост|быстр/i]])
  if (!re.test(changesText)) problems.push(`team deck's changes slides do not name ${what}`);

// step 4: street lighting runs on the same system (public, plainly); the team deck shows the module core
if (!/освещени/i.test(pubAll) || !Object.values(pub).some((t) => /освещени[^.]{0,80}(работает|подключен)/i.test(t)))
  problems.push("public deck does not say that street lighting now works on the same system");
const teamAll = Object.values(team).join(" ");
if (!/lighting/.test(teamAll) || !/Core\.send|ядр/i.test(teamAll) || !/модул/i.test(teamAll))
  problems.push("team deck does not show the module core and the lighting module");

// step 5: root on a unit no longer takes the unit's key away; the team deck says the key is in the chip
const intruder = team.intruder || "";
if (/ключ блока, до отзыва/.test(intruder)) problems.push("team intruder slide still says root takes the unit's key");
if (!/TPM|чип/.test(intruder)) problems.push("team intruder slide does not say the key is in the chip");

// step 6: the system is signed and verified at boot; Secure Boot is no longer a gap
if (/Secure Boot/.test(team.gaps || "")) problems.push("team gaps slide still lists Secure Boot");
if (!Object.values(team).some((t) => /Secure Boot/.test(t) && /(провер|подпис)/i.test(t)))
  problems.push("team deck does not say the system is signed and verified at boot");

// step 7: the team deck says what was closed (PCR 7 seal, boot disk only, version floor) and what stays open
if (!/шаг 7/i.test(teamAll) || !/PCR 7/.test(teamAll) || !/загрузочн\S* диск/i.test(teamAll) || !/старый подписанный выпуск|старую подписанную версию/i.test(teamAll))
  problems.push("team deck does not show what step 7 closed (PCR 7, boot disk) and what stays open (an old signed release)");

// step 8: the team deck shows what the tunnel changed and what stays open; the gaps slide no longer lists it as missing;
// the public deck says plainly, in one sentence, that camera video between the junction and the district is now closed
if (!/шаг\s*8/i.test(changesText) || !/WireGuard/.test(changesText) || !/51820/.test(changesText) || !/\bonly\b/.test(changesText))
  problems.push("team deck's changes slides do not show what step 8 changed (WireGuard on UDP 51820, mode only)");
if (!/Открыто:[^.]*(QEMU|PresharedKey)/.test(changesText))
  problems.push("team deck's changes slides do not say what stays open after step 8 (only QEMU, PresharedKey)");
if (/WireGuard до района/.test(team.gaps || "")) problems.push("team gaps slide still lists «WireGuard до района»");
if (!/WireGuard|туннел/.test(intruder)) problems.push("team intruder slide has nothing on the wire between the junction and the district");
const videoClosed = Object.values(pub).flatMap((t) => t.split(/[.!?]/))
  .some((s) => /видео/i.test(s) && /перекрёст/i.test(s) && /район/i.test(s) && /закрыт/i.test(s) && /теперь/i.test(s));
if (!videoClosed) problems.push("public deck does not say that camera video between the junction and the district is now closed");

// step 9: the team deck shows the stand kit (per-unit GUIDs and GRUB password, the key backup, the install order) and
// what stays open (production keys are the owner's step, the hardware is untested)
for (const [what, re] of [["step 9", /шаг\s*9/i], ["per-unit GUIDs", /GUID/], ["per-unit GRUB password", /пароль GRUB/],
  ["the key backup", /keys-backup\.mjs/], ["the install order", /BIOS[^.]{0,40}Rufus/]])
  if (!re.test(changesText)) problems.push(`team deck's changes slides do not show what step 9 changed: ${what}`);
const open9 = changes.find(([id]) => id === "changes9")?.[1] || "";
if (!/Открыто:[^.]*боевые ключи[^.]*владел/.test(open9) || !/Открыто:[^.]*(железо|QEMU)/.test(open9))
  problems.push("the step-9 changes slide does not say what stays open (production keys are the owner's step, hardware untested)");

// step 11: the team deck shows the arm64 build (same chain, its own bundle tag, the light unit's memory) and what stays
// open (the TPM on ARM only on hardware, the measured boot build parked); the gaps slide no longer lists the ARM build;
// the public deck says plainly, in one sentence, that the system now also runs on small, cheap boards
const ch11 = team.changes11 || "";
if (!ch11) problems.push("team deck has no step-11 changes slide (changes11)");
for (const [what, re] of [["step 11", /шаг\s*11/i], ["arm64 / ARM", /arm64|\bARM\b/], ["the same chain (Secure Boot, BOOTAA64.EFI)", /Secure Boot[^.]*BOOTAA64\.EFI/],
  ["its own bundle tag", /urban-edge-aarch64/], ["the light unit's memory", /169 МБ/]])
  if (!re.test(ch11)) problems.push(`the step-11 changes slide does not show ${what}`);
if (!/Открыто:[^.]*TPM[^.]*(желез|плат)/.test(ch11)) problems.push("the step-11 changes slide does not say the TPM on ARM is checked only on hardware");
if (!/Открыто:[^.]*измеряем\S* загрузк\S* отложен/.test(ch11)) problems.push("the step-11 changes slide does not say the measured boot build is parked");
const at11 = (team.changes11 !== undefined) && Object.keys(team).indexOf("changes11") === Object.keys(team).indexOf("changes9") + 1;
if (!at11) problems.push("the step-11 changes slide does not follow changes9 in the team deck's order");
if (/Сборка под ARM/.test(team.gaps || "")) problems.push("team gaps slide still lists «Сборка под ARM»");
const boards = Object.values(pub).flatMap((t) => t.split(/[.!?]/))
  .some((s) => /(маленьк|небольш)/i.test(s) && /(дешёв|недорог)/i.test(s) && /плат/i.test(s) && /теперь/i.test(s));
if (!boards) problems.push("public deck does not say plainly that the system now also runs on small, cheap boards");

// step 12: the team deck shows releases by platform and the waves (the compatible from the signed package, --devices /
// --wave, promote, the halt that stops a rollout by itself, resume / withdraw) and what stays open (only QEMU, the centre
// trusts what devices report, its own update is manual); the public deck says plainly, in one sentence, that a bad update
// stops itself before it reaches the whole city
const ch12 = team.changes12 || "";
if (!ch12) problems.push("team deck has no step-12 changes slide (changes12)");
for (const [what, re] of [["step 12", /шаг\s*12/i], ["releases by platform", /платформ/i], ["the compatible from the signed package", /compatible[^.]*пакет/],
  ["both platforms", /x86_64[^.]*aarch64/], ["the waves", /волн/i], ["--devices / --wave", /--devices[^.]*--wave/], ["promote", /promote/],
  ["the halt (rollout-halted)", /rollout-halted/], ["it stops by itself on a failure or a rollback", /(отказ|неудач)[^.]*откат[^.]*останов|сам\S* стоп/i],
  ["resume / withdraw", /resume[^.]*withdraw/]])
  if (!re.test(ch12)) problems.push(`the step-12 changes slide does not show ${what}`);
if (!/Открыто:[^.]*QEMU/.test(ch12) || !/Открыто:[^.]*(верит|отчёт)/.test(ch12) || !/Открыто:[^.]*вручную/.test(ch12))
  problems.push("the step-12 changes slide does not say what stays open (only QEMU, the centre trusts device reports, its own update manual)");
const at12 = (team.changes12 !== undefined) && Object.keys(team).indexOf("changes12") === Object.keys(team).indexOf("changes11") + 1;
if (!at12) problems.push("the step-12 changes slide does not follow changes11 in the team deck's order");
// step 13: the team deck shows the centre's self-update (the one command the admin runs with the version, the refusals,
// the same root helper and its checks, the rollback by itself with centre-rolled-back, the data kept) and what stays open
// (only QEMU, minutes without an interface, a version that breaks after its self-check is not rolled back); the public
// deck says plainly, in one visible sentence, that the control centre updates the same way: on a failure it returns to the
// previous version by itself and loses nothing
const ch13 = team.changes13 || "";
if (!ch13) problems.push("team deck has no step-13 changes slide (changes13)");
for (const [what, re] of [["step 13", /шаг\s*13/i], ["the command", /urban-center release self-update 2/], ["the admin names the version", /номер называет админ/],
  ["the refusals", /refused:[^.]*halted[^.]*снят[^.]*не новее/], ["the same root helper", /помощник root/], ["its checks", /подпись URBAN[^.]*только новее[^.]*compatible/],
  ["the rollback by itself", /самопроверка не прошла[^.]*centre-rolled-back/], ["the data kept", /база, подключения, ключи, журнал/],
  ["the devices come back by themselves", /блоки вернутся сами/], ["before and after", /Раньше:[^.]*вручную[\s\S]{0,80}Теперь:/]])
  if (!re.test(ch13)) problems.push(`the step-13 changes slide does not show ${what}`);
if (!/Открыто:[^.]*QEMU/.test(ch13) || !/Открыто:[^.]*без интерфейса/.test(ch13) || !/Открыто:[^.]*после самопроверки[^.]*не откатится/.test(ch13))
  problems.push("the step-13 changes slide does not say what stays open (only QEMU, minutes without an interface, a later break not rolled back)");
if (!/<aside>[^<]{200,}<\/aside>/.test(readFileSync(join(ROOT, "docs/deck/project/slides/changes13.html"), "utf8")))
  problems.push("the step-13 changes slide has no speaker note");
const at13 = (team.changes13 !== undefined) && Object.keys(team).indexOf("changes13") === Object.keys(team).indexOf("changes12") + 1;
if (!at13) problems.push("the step-13 changes slide does not follow changes12 in the team deck's order");
{
  // the visible text of the public deck (speaker notes left out): one sentence says the centre returns to the previous
  // version by itself on a failure and loses nothing, next to the sentence on the junctions' careful updates
  const visible = readdirSync(join(ROOT, "docs/deck-public/project/slides")).filter((f) => f.endsWith(".html"))
    .map((f) => readFileSync(join(ROOT, "docs/deck-public/project/slides", f), "utf8").replace(/<aside>[\s\S]*?<\/aside>/g, " ")
      .replace(/<[^>]+>/g, " ").replace(/\s+/g, " ")).join(" ");
  const centreSame = visible.split(/[.!?]/)
    .some((s) => /центр/i.test(s) && /так же/i.test(s) && /сам\S*\s+вернёт прежн/i.test(s) && /ничего не потеря/i.test(s));
  if (!centreSame) problems.push("public deck does not say plainly, on a slide, that the control centre updates the same way: returns to the previous version by itself, nothing lost");
  if (!/обновля[^.]*центр управления[^.]*прежн/i.test(pub.new || "")) problems.push("the public «new» slide's note does not say the control centre updates itself the same careful way");
}
// every footer of the team deck is its place in the order, out of the total
{
  const ids = Object.keys(team);
  for (const [i, id] of ids.entries()) {
    const html = readFileSync(join(ROOT, "docs/deck/project/slides", `${id}.html`), "utf8");
    const want = `${String(i + 1).padStart(2, "0")} / ${ids.length}`;
    if (!html.includes(`>${want}</p>`)) problems.push(`team deck: ${id} does not carry the footer ${want}`);
  }
}
const badStops = Object.values(pub).flatMap((t) => t.split(/[.!?]/))
  .some((s) => /(обновлени|верси)/i.test(s) && /останов\S*\s+сам/i.test(s) && /(весь|всего|всём|весь) город/i.test(s));
if (!badStops) problems.push("public deck does not say plainly that a bad update stops itself before it reaches the whole city");

if (problems.length) {
  for (const p of problems) console.log(`FAIL ${p}`);
  process.exit(1);
}
console.log(`public deck: ${Object.keys(pub).length} slides, no technical words; team deck: ${Object.keys(team).length} slides, `
  + `${teamJargon.length} technical words (the control), ${changes.length} changes slide(s)`);
console.log("DECKS CHECK PASSED");
