// G17: the unit's control service is up quickly, also with the network cable pulled, and boot costs less CPU
// than before this step. Measured in the same emulated VM as data/os/logs/measure-before.json (taken on the
// previous image, before the speed work), so the two compare like with like.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { OUT, build, fail, scenario } from "./lib.mjs";

const before = join(OUT, "logs", "measure-before.json");
if (!existsSync(before)) fail("no baseline: data/os/logs/measure-before.json (vm.py measure before, on the previous image)");
build("1", { image: true });
if (scenario("measure", "after") !== 0) fail("measuring");
const b = JSON.parse(readFileSync(before, "utf8")), a = JSON.parse(readFileSync(join(OUT, "logs", "measure-after.json"), "utf8"));
if (a.kvm !== b.kvm) fail("the baseline was taken with different acceleration: not comparable");
const sum = (m) => Object.values(m.cpu_s).reduce((x, y) => x + y, 0);
const rows = [];
for (const run of ["cabled", "no-network"]) {
  const after = a[run].edge_active_s - a[run].prepare_done_s, was = b[run].edge_active_s - b[run].prepare_done_s;
  rows.push(`${run}: control up ${after.toFixed(1)} s after urban-prepare (was ${was.toFixed(1)} s); boot CPU ${sum(a[run]).toFixed(1)} s (was ${sum(b[run]).toFixed(1)} s)`);
  if (!(after <= 15)) fail(`${run}: the control service took ${after.toFixed(1)} s after urban-prepare (budget 15 s)`);
}
if (!(sum(a.cabled) < sum(b.cabled))) fail(`boot CPU did not go down: ${sum(a.cabled).toFixed(1)} s, was ${sum(b.cabled).toFixed(1)} s`);
if (!(a.cabled.busy_pct_idle <= b.cabled.busy_pct_idle + 2)) fail(`idle CPU went up: ${a.cabled.busy_pct_idle} %, was ${b.cabled.busy_pct_idle} %`);
for (const r of rows) console.log("   " + r);
console.log(`   running: ${a.cabled.busy_pct_idle} % CPU (was ${b.cabled.busy_pct_idle} %), ${a.cabled.mem_used_mb} MB (was ${b.cabled.mem_used_mb} MB)`);
console.log("SPEED CHECK PASSED");
