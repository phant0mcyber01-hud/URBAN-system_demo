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

if (problems.length) {
  for (const p of problems) console.log(`FAIL ${p}`);
  process.exit(1);
}
console.log(`public deck: ${Object.keys(pub).length} slides, no technical words; team deck: ${Object.keys(team).length} slides, `
  + `${teamJargon.length} technical words (the control), ${changes.length} changes slide(s)`);
console.log("DECKS CHECK PASSED");
