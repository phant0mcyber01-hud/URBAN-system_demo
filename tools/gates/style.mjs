// Gate helper: the interface builds and its style is one system.
//   - every colour in style.css comes from the :root token block (no raw hex or rgb outside it)
//   - the display font with Cyrillic and the URBAN mark are in the build
//   - nothing of Ubisoft's (names, the ctOS mark) is in what ships: index.html, scripts, the built bundle
// The detectors are tried on known-bad samples first, so a pass cannot come from a check that sees nothing.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const WEB = join(ROOT, "web");
const fail = (m) => { console.error("FAILED: " + m); process.exit(1); };

const RAW = /#[0-9a-fA-F]{3,8}\b|rgba?\(/g;
const FOREIGN = /ct\s?OS|Watch[\s_-]?Dogs|Ubisoft|DedSec|Blume/i;
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");
function rawOutsideRoot(css) {
  const body = stripComments(css);
  const root = /:root\s*\{[^}]*\}/.exec(body);
  return (root ? body.replace(root[0], "") : body).match(RAW) || [];
}
// positive controls: the detectors must catch what they are for
if (rawOutsideRoot(":root{--a:#fff}\n.x{color:#123456}").length !== 1) fail("colour detector misses a raw colour");
if (rawOutsideRoot(":root{--a:rgba(1,2,3,.5)}\n.x{color:var(--a)}").length !== 0) fail("colour detector flags tokens");
if (!FOREIGN.test("powered by ctOS") || FOREIGN.test("URBAN Grid")) fail("name detector is broken");

const build = spawnSync("npm run build", { cwd: WEB, encoding: "utf8", shell: true });
if (build.status !== 0) fail("npm run build: " + (build.stderr || build.stdout).slice(-400));

const css = readFileSync(join(WEB, "src", "style.css"), "utf8");
const raw = rawOutsideRoot(css);
if (raw.length) fail(`colours outside the token block: ${[...new Set(raw)].join(", ")}`);
for (const token of ["--accent", "--display", "--mono", "--warn", "--bad", "--surface"]) {
  if (!new RegExp(`${token}\\s*:`).test(css)) fail(`token ${token} is not defined`);
}

const html = readFileSync(join(WEB, "index.html"), "utf8");
if ((html.match(/class="mark"/g) || []).length < 2) fail("the URBAN mark is not in the top bar and on the sign-in screen");
if (!existsSync(join(WEB, "public", "mark.svg"))) fail("web/public/mark.svg (the tab icon) is missing");

const walk = (d, out = []) => {
  for (const n of readdirSync(d)) {
    const p = join(d, n);
    if (statSync(p).isDirectory()) walk(p, out); else out.push(p);
  }
  return out;
};
const dist = walk(join(WEB, "dist"));
if (!dist.some((f) => /exo-2-cyrillic-(500|600)-normal.*\.woff2$/.test(f))) fail("the display font with Cyrillic is not in the build");
if (!dist.some((f) => f.endsWith("mark.svg"))) fail("the tab icon is not in the build");
const shipped = [join(WEB, "index.html"), ...walk(join(WEB, "src")).filter((f) => f.endsWith(".js")),
  ...dist.filter((f) => /\.(js|css|html|svg)$/.test(f))];
for (const f of shipped) {
  const text = readFileSync(f, "utf8");
  const m = FOREIGN.exec(text);
  if (m) fail(`"${m[0]}" in ${f.replace(ROOT, "")}: the product must carry URBAN's own identity only`);
}
console.log(`checked ${shipped.length} shipped files, ${css.length} bytes of style`);
console.log("STYLE CHECK PASSED");
