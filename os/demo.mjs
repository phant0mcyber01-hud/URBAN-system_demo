// The demo of URBAN OS: three machines in QEMU on this computer - a junction unit, a district server and the
// control centre - each with its screen in a browser, the centre's interface too.
//
//   node os/demo.mjs            start the three machines; prints the addresses when they are up
//   node os/demo.mjs pilot      one machine only: the pilot's district server and centre
//   node os/demo.mjs enrol      enrol the district and the unit at the centre by its CLI (what the UI does)
//   node os/demo.mjs kit FILE   put a kit downloaded from the centre's UI onto the machine it is for
//   node os/demo.mjs stop       switch everything off (nothing is kept: every start is a fresh install)
//   node os/demo.mjs smoke      start, check the screens and the centre answer, stop (the gate)
//
// The first admin password is in data/os/logs/demo-admin.txt once the centre has started.
import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { basename, join } from "node:path";
import { OUT, ROOT, build, docker, ensureBuilder, fail } from "./tests/lib.mjs";

const NAME = "urban-demo";
const SCREENS = [6080, 6081, 6082];  // unit, district, centre (os/demo/screen.py in front of VNC :2, :1, :0)
const VNC = { 6080: 5902, 6081: 5901, 6082: 5900 };
const TITLE = { 6080: "junction unit", 6081: "district server", 6082: "control centre" };
const NOVNC = join(ROOT, "os", "demo", "novnc");  // noVNC 1.5.0 (MPL-2.0), its core and vendor folders
const CENTRE = 9443;
const mode = process.argv[2] || "start";

const sleep = (ms) => spawnSync(process.execPath, ["-e", `setTimeout(()=>{},${ms})`]);
const logs = () => { const r = docker(["logs", NAME], { capture: true }); return (r.stdout || "") + (r.stderr || ""); };
const running = () => docker(["inspect", "-f", "{{.State.Running}}", NAME], { capture: true }).stdout.trim() === "true";

function stop() {
  docker(["rm", "-f", NAME], { capture: true });
}

function start(pilot) {
  stop();
  if (!existsSync(join(NOVNC, "core", "rfb.js"))) fail("os/demo/novnc is missing (noVNC core and vendor folders)");
  build("1", { image: true });
  const kvm = spawnSync("docker", ["run", "--rm", "--device", "/dev/kvm", "urban-os-builder:bookworm", "true"]).status === 0;
  if (!kvm) console.log(">> no KVM in Docker's Linux VM: the processors are emulated, everything inside is slow\n"
    + ">>   (Windows on AMD: Memory integrity in Windows Security keeps nested virtualization off; see docs/edge-os.md)");
  const screens = pilot ? [6082] : SCREENS;
  const inside = [
    "set -e",
    ...screens.map((p) => `python3 /work/os/demo/screen.py --listen ${p} --vnc 127.0.0.1:${VNC[p]} --novnc /work/os/demo/novnc --title "${TITLE[p]}" &`),
    `exec python3 /work/os/tests/vm.py demo${pilot ? " pilot" : ""}`,
  ].join("\n");
  const r = docker(["run", "-d", "--name", NAME, "--privileged", ...(kvm ? ["--device", "/dev/kvm", "-e", "URBAN_KVM=1"] : []),
    ...screens.flatMap((p) => ["-p", `127.0.0.1:${p}:${p}`]), "-p", `127.0.0.1:${CENTRE}:2443`,
    "-v", `${ROOT}:/work`, "-v", "urban-os:/out", "-v", `${join(OUT, "keys")}:/out/keys`, "-v", `${join(OUT, "logs")}:/out/logs`,
    "urban-os-builder:bookworm", "bash", "-c", inside], { capture: true });
  if (r.status !== 0) fail("could not start: " + r.stderr);
  console.log(`>> URBAN OS is booting ${pilot ? "the pilot's machine" : "three machines"} (several minutes without KVM)`);
  const end = Date.now() + 30 * 60 * 1000;
  for (;;) {
    const text = logs();
    if (text.includes("DEMO READY")) break;
    if (text.includes("FAILED") || !running()) fail("the machines did not come up:\n" + text.slice(-3000));
    if (Date.now() > end) fail("timed out:\n" + text.slice(-3000));
    sleep(5000);
  }
  const codes = [...logs().matchAll(/^ {3}(demo-\S+): (URBAN1:\S+)$/gm)].map((m) => `  ${m[1].padEnd(16)} ${m[2]}`).join("\n");
  console.log(`
URBAN OS is running${kvm ? "" : " (emulated: slow)"}.
  screens             ${pilot ? "" : `unit http://127.0.0.1:6080/\n                      district http://127.0.0.1:6081/\n                      `}centre http://127.0.0.1:6082/
  control centre      https://127.0.0.1:${CENTRE}/   (the centre's own certificate: the browser warns once)
  admin password      data/os/logs/demo-admin.txt
${codes ? `  enrolment codes     (also on the machines' screens)\n${codes}\n  enrol them          in the UI (Devices -> Enrol), then node os/demo.mjs kit <downloaded file>; or: node os/demo.mjs enrol\n` : ""}  switch off          node os/demo.mjs stop
DEMO STARTED`);
}

function exec(args) {
  if (!running()) fail("the demo is not running: node os/demo.mjs");
  const r = docker(["exec", NAME, "python3", "/work/os/tests/vm.py", ...args], { capture: true });
  process.stdout.write(r.stdout || "");
  if (r.status !== 0) fail((r.stderr || r.stdout || "").slice(-2000));
}

function smoke() {
  start(false);
  const page = (port) => spawnSync("curl", ["-s", "-o", "/dev/null", "-w", "%{http_code}", `http://127.0.0.1:${port}/`],
    { encoding: "utf8" }).stdout.trim();
  for (const p of SCREENS) if (page(p) !== "200") fail(`screen on ${p} does not answer (${page(p)})`);
  // the screen itself: through the WebSocket the browser must get QEMU's VNC greeting
  const banner = spawnSync(process.execPath, ["-e", `
    const ws = new WebSocket("ws://127.0.0.1:${SCREENS[2]}/websockify", ["binary"]); ws.binaryType = "arraybuffer";
    const t = setTimeout(() => { console.log("timeout"); process.exit(1); }, 15000);
    ws.onmessage = (m) => { console.log(new TextDecoder().decode(new Uint8Array(m.data).slice(0, 11))); clearTimeout(t); ws.close(); process.exit(0); };
    ws.onerror = (e) => { console.log("error " + (e.message || "")); process.exit(1); };`], { encoding: "utf8" });
  if (!(banner.stdout || "").startsWith("RFB 003")) fail("the centre's screen does not carry VNC: " + (banner.stdout || banner.stderr));
  const h = spawnSync("curl", ["-sk", `https://127.0.0.1:${CENTRE}/api/health`], { encoding: "utf8" }).stdout;
  if (!h.includes('"ok":true')) fail("the centre does not answer over HTTPS: " + h);
  console.log(`   three screens answer (VNC ${banner.stdout.trim()} through the centre's); centre: ${h.trim()}`);
  stop();
  if (running()) fail("stop left the demo running");
  console.log("DEMO SMOKE PASSED");
}

switch (mode) {
  case "start": start(false); break;
  case "pilot": start(true); break;
  case "stop": stop(); console.log("stopped"); break;
  case "enrol": exec(["demo-enrol"]); break;
  case "kit": {
    const file = process.argv[3];
    if (!file) fail("node os/demo.mjs kit <urban-<device>.json downloaded from the centre>");
    const kit = JSON.parse(readFileSync(file, "utf8"));
    const inside = `/out/logs/kit-${basename(file)}`;
    const cp = docker(["cp", file, `${NAME}:${inside}`], { capture: true });
    if (cp.status !== 0) fail(cp.stderr);
    exec(["demo-kit", inside, kit.unit]);
    break;
  }
  case "smoke": ensureBuilder(); smoke(); break;
  default: fail("node os/demo.mjs [start|pilot|enrol|kit FILE|stop|smoke]");
}
