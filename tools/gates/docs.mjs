// Gate helper: documents that must exist and say specific things.
//   node tools/gates/docs.mjs controllers | context
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const fail = (m) => { console.error("FAILED: " + m); process.exit(1); };
const read = (p) => (existsSync(join(ROOT, p)) ? readFileSync(join(ROOT, p), "utf8") : fail(`${p} is missing`));
const has = (text, what, list) => { const miss = list.filter((s) => !text.includes(s)); if (miss.length) fail(`${what} lacks: ${miss.join(" | ")}`); };

const which = process.argv[2];
if (which === "controllers") {
  const t = read("docs/controllers.md");
  has(t, "docs/controllers.md", ["## Таблица совместимости", "NTCIP 1202", "GB/T 20999", "OCIT-O", "UTMC", "Modbus TCP",
    "`binary`", "`onehot`", "`bcd`", "разрешение ДУ", "Обратная связь", "python -m urban.equipment probe",
    "--adapter contacts", "--adapter profile", "## Безопасность контактов", "## Не проверено", "нормально разомкнутые"]);
  for (const f of ["test_contacts.py", "test_contact_buses.py", "test_profiles.py", "test_probe.py"])
    if (!existsSync(join(ROOT, "backend", "tests", f))) fail(`the document cites ${f}, which does not exist`);
} else if (which === "context") {
  const t = read("CONTEXT.md");
  has(t, "CONTEXT.md", ["## Совместимость с контроллерами", "## Визуальный стиль", "ctOS", "Ubisoft", "docs/controllers.md",
    "контакт", "профил", "python -m urban.equipment probe"]);
} else fail("usage: docs.mjs controllers|context");
console.log("DOCS CHECK PASSED");
