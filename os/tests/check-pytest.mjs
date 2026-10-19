// G7: the platform and unit tests, with the project's own virtual environment.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { ROOT, fail } from "./lib.mjs";
import { judge } from "../../tools/gates/pytest-result.mjs";

const python = [join(ROOT, ".venv", "Scripts", "python.exe"), join(ROOT, ".venv", "bin", "python")].find(existsSync);
if (!python) fail("no .venv: run scripts/setup.ps1 first");
const r = spawnSync(python, ["-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider"], { cwd: join(ROOT, "backend"), encoding: "utf8" });
const out = (r.stdout || "") + (r.stderr || "");
console.log(out.trim().split(/\r?\n/).pop());
const verdict = judge(out, r.status);  // all passed and none skipped (tools/gates/pytest-result.mjs)
if (!verdict.ok) fail(verdict.message);
if (verdict.message.includes("allowed skips")) console.log(verdict.message);
console.log(`PYTEST PASSED (${verdict.passed} tests)`);
