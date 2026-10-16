// G7: the platform and unit tests, with the project's own virtual environment.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { ROOT, fail } from "./lib.mjs";

const python = [join(ROOT, ".venv", "Scripts", "python.exe"), join(ROOT, ".venv", "bin", "python")].find(existsSync);
if (!python) fail("no .venv: run scripts/setup.ps1 first");
const r = spawnSync(python, ["-m", "pytest", "-q", "-p", "no:cacheprovider"], { cwd: join(ROOT, "backend"), encoding: "utf8" });
const out = (r.stdout || "") + (r.stderr || "");
const summary = out.trim().split(/\r?\n/).pop();
console.log(summary);
const passed = /(\d+) passed/.exec(summary);
if (r.status !== 0 || !passed || /failed|error/.test(summary)) fail("pytest: " + summary);
console.log(`PYTEST PASSED (${passed[1]} tests)`);
