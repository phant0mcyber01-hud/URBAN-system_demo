// G8: docs/edge-os.md covers what a person needs to build, flash, start, update and trust the unit's OS,
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
};
const missing = Object.entries(need).filter(([, rx]) => !headings.some((h) => rx.test(h))).map(([k]) => k);
if (missing.length) fail("sections missing: " + missing.join(", "));
for (const cmd of ["node os/build.mjs", "urban-unit setup", "urban-unit update", "urban-unit.txt"]) {
  if (!text.includes(cmd)) fail(`the guide never shows ${cmd}`);
}
if (text.length < 4000) fail("the guide is too short to be followed");
console.log("DOCS CHECK PASSED");
