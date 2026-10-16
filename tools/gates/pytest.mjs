// Gate helper: the backend tests with the project's own environment, all of them or the files given.
//   node tools/gates/pytest.mjs [tests/test_x.py ...]      prints PYTEST PASSED (N tests) only if all pass
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const python = [join(ROOT, ".venv", "Scripts", "python.exe"), join(ROOT, ".venv", "bin", "python")].find(existsSync);
if (!python) { console.error("FAILED: no .venv (scripts/setup.ps1)"); process.exit(1); }
const files = process.argv.slice(2);
for (const f of files) if (!existsSync(join(ROOT, "backend", f))) { console.error(`FAILED: ${f} does not exist`); process.exit(1); }
const r = spawnSync(python, ["-m", "pytest", "-q", "-p", "no:cacheprovider", ...files], { cwd: join(ROOT, "backend"), encoding: "utf8" });
const summary = ((r.stdout || "") + (r.stderr || "")).trim().split(/\r?\n/).pop();
console.log(summary);
const passed = /(\d+) passed/.exec(summary);
if (r.status !== 0 || !passed || /failed|error/.test(summary)) { console.error("FAILED: " + summary); process.exit(1); }
console.log(`PYTEST PASSED (${passed[1]} tests)`);
