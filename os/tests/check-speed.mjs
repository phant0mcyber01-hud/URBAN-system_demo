// G17: the unit's control service is up quickly, also with the network cable pulled, and boot costs less CPU
// than before this step. Measured in the same emulated VM as data/os/logs/measure-before.json (taken on the
// previous image, before the speed work), so the two compare like with like.
//
// What keeps this from passing by accident: both runs must show the control service really became active after
// urban-prepare (edge_active_s > prepare_done_s > 0; a service that never started reads 0), the same services'
// CPU figures must exist before and after, and before and after must be different images.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { OUT, build, fail, inputsDigest, scenario } from "./lib.mjs";

const IDLE_NOISE_ALLOWANCE = 2; // percentage points of idle CPU: the measurement's own noise, not an improvement budget
const BUDGET_S = 15;

const before = join(OUT, "logs", "measure-before.json");
if (!existsSync(before)) fail("no baseline: data/os/logs/measure-before.json (vm.py measure before, on the previous image)");
build("1", { image: true });
if (scenario("measure", "after") !== 0) fail("measuring");
const b = JSON.parse(readFileSync(before, "utf8")), a = JSON.parse(readFileSync(join(OUT, "logs", "measure-after.json"), "utf8"));
if (a.kvm !== b.kvm) fail("the baseline was taken with different acceleration: not comparable");

// the measurement must be of the image just built, and not of the one the baseline measured
const want = inputsDigest() + "-image";
if (!a.image || !a.image.rootfs_sha256) fail("measure-after.json carries no image identity (vm.py measure is out of date)");
if (a.image.stamp !== want) fail(`measure-after.json is of build ${a.image.stamp || "(unstamped)"}, the image just built is ${want}`);
if (b.image && b.image.rootfs_sha256) {
  if (b.image.rootfs_sha256 === a.image.rootfs_sha256 || (b.image.stamp && b.image.stamp === a.image.stamp))
    fail("before and after are the same image: this compares a system with itself");
} else {
  // a baseline taken before images were identified: the identity cannot be checked, only that it is not a copy of this run
  console.log("   note: measure-before.json has no image identity (taken before this check recorded it); retake it with vm.py measure before to make the comparison provable");
  for (const run of ["cabled", "no-network"])
    if (JSON.stringify(a[run].cpu_s) === JSON.stringify(b[run].cpu_s) && a[run].edge_active_s === b[run].edge_active_s)
      fail(`${run}: before and after are identical to the digit: the same file measured twice`);
}

const sum = (m) => Object.values(m.cpu_s).reduce((x, y) => x + y, 0);
const rows = [];
for (const run of ["cabled", "no-network"]) {
  for (const [what, m] of [["after", a[run]], ["before", b[run]]]) {
    if (!(m.edge_active_s > m.prepare_done_s && m.prepare_done_s > 0))
      fail(`${run} (${what}): the control service did not become active after urban-prepare (edge_active_s ${m.edge_active_s}, prepare_done_s ${m.prepare_done_s})`);
  }
  const ka = Object.keys(a[run].cpu_s).sort(), kb = Object.keys(b[run].cpu_s).sort();
  if (JSON.stringify(ka) !== JSON.stringify(kb))
    fail(`${run}: CPU figures cover different services (after: ${ka.join(", ")}; before: ${kb.join(", ")}); the sums would not compare`);
  if (ka.length === 0) fail(`${run}: no CPU figures`);
  const after = a[run].edge_active_s - a[run].prepare_done_s, was = b[run].edge_active_s - b[run].prepare_done_s;
  rows.push(`${run}: control up ${after.toFixed(1)} s after urban-prepare (was ${was.toFixed(1)} s); boot CPU ${sum(a[run]).toFixed(1)} s (was ${sum(b[run]).toFixed(1)} s) over ${ka.length} services`);
  if (!(after <= BUDGET_S)) fail(`${run}: the control service took ${after.toFixed(1)} s after urban-prepare (budget ${BUDGET_S} s)`);
}
if (!(sum(a.cabled) < sum(b.cabled))) fail(`boot CPU did not go down: ${sum(a.cabled).toFixed(1)} s, was ${sum(b.cabled).toFixed(1)} s`);
for (const [what, m] of [["after", a.cabled], ["before", b.cabled]])
  if (typeof m.busy_pct_idle !== "number" || !Number.isFinite(m.busy_pct_idle)) fail(`idle CPU (${what}) was not measured`);
if (!(a.cabled.busy_pct_idle <= b.cabled.busy_pct_idle + IDLE_NOISE_ALLOWANCE))
  fail(`idle CPU went up: ${a.cabled.busy_pct_idle} %, was ${b.cabled.busy_pct_idle} % (noise allowance +${IDLE_NOISE_ALLOWANCE} points)`);
for (const r of rows) console.log("   " + r);
console.log(`   running: ${a.cabled.busy_pct_idle} % CPU (was ${b.cabled.busy_pct_idle} %; noise allowance +${IDLE_NOISE_ALLOWANCE} points of idle CPU), ${a.cabled.mem_used_mb} MB (was ${b.cabled.mem_used_mb} MB)`);
console.log("SPEED CHECK PASSED");
