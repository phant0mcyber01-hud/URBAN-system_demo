// Gate helper: the backend tests with the project's own environment, all of them or the files given.
//   node tools/gates/pytest.mjs [tests/test_x.py ...]      prints PYTEST PASSED (N tests) only if all pass and none is skipped
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { judge } from "./pytest-result.mjs";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const python = [join(ROOT, ".venv", "Scripts", "python.exe"), join(ROOT, ".venv", "bin", "python")].find(existsSync);
if (!python) { console.error("FAILED: no .venv (scripts/setup.ps1)"); process.exit(1); }
const files = process.argv.slice(2);
for (const f of files) if (!existsSync(join(ROOT, "backend", f))) { console.error(`FAILED: ${f} does not exist`); process.exit(1); }
const r = spawnSync(python, ["-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", ...files], { cwd: join(ROOT, "backend"), encoding: "utf8" });
const out = (r.stdout || "") + (r.stderr || "");
console.log(out.trim().split(/\r?\n/).pop());
const verdict = judge(out, r.status);
if (!verdict.ok) { console.error("FAILED: " + verdict.message); process.exit(1); }
if (verdict.message.includes("allowed skips")) console.log(verdict.message);
console.log(`PYTEST PASSED (${verdict.passed} tests)`);
