// G5 (step 9): the off-line backup of data/os/keys holds. With a test passphrase in URBAN_KEYS_PASSPHRASE it backs the
// keys up to a scratch folder under data/os and proves: the file is an OpenPGP message, not a tar (no "PRIVATE KEY", no
// bytes of any private key, no passphrase inside); `list` names every file of data/os/keys; a wrong passphrase is refused
// and writes nothing; the right one restores every file byte for byte into an empty folder; nothing else is left in the
// scratch folder at any point. The scratch folder (with the restored keys) is removed at the end, pass or fail.
import { spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { OUT, ROOT, fail } from "./lib.mjs";

const KEYS = join(OUT, "keys");
const scratch = join(OUT, `keys-backup-check-${process.pid}`);
const backup = join(scratch, "keys.tar.gpg");
const PASS = "urban test " + randomBytes(9).toString("base64");

function files(dir, base = dir, out = []) {
  for (const name of readdirSync(dir).sort()) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) files(p, base, out);
    else out.push(relative(base, p).replace(/\\/g, "/"));
  }
  return out;
}
const sha = (p) => createHash("sha256").update(readFileSync(p)).digest("hex");
function tool(args, pass) {
  const env = { ...process.env };
  if (pass === undefined) delete env.URBAN_KEYS_PASSPHRASE;
  else env.URBAN_KEYS_PASSPHRASE = pass;
  const r = spawnSync(process.execPath, [join(ROOT, "os", "keys-backup.mjs"), ...args], { cwd: ROOT, env, encoding: "utf8" });
  if (r.error) throw r.error;
  return { status: r.status, out: r.stdout, err: r.stderr };
}
function only(dir, names, when) {
  const have = readdirSync(dir).sort();
  if (have.join("\n") !== [...names].sort().join("\n")) fail(`${when}: ${dir} holds ${JSON.stringify(have)}, expected ${JSON.stringify(names)}`);
}

if (spawnSync("docker", ["info", "--format", "{{.ServerVersion}}"], { encoding: "utf8" }).status !== 0)
  fail("the Docker engine is not running (the backup runs gpg in the builder container)");
// what the build needs (os/build/keys.sh, build.sh, unit-image.mjs): the backup takes the whole folder, these must be in it
const needed = ["ca.key", "ca.pem", "signing.key", "signing.pem", "sb/owner.guid", "boot.pub.gpg", "grub-password",
  "maintenance_ed25519", "maintenance_ed25519.pub", "escrow/escrow.pem",
  ...["PK", "KEK", "db"].flatMap((n) => [`sb/${n}.key`, `sb/${n}.crt`])];
if (!existsSync(KEYS)) fail(`no ${KEYS}: build first (node os/build.mjs --image)`);
const want = files(KEYS);
for (const n of needed) if (!want.includes(n)) fail(`data/os/keys has no ${n}: build first (node os/build.mjs --image, node os/unit-image.mjs)`);
if (!want.some((n) => /^boot-gpg\/private-keys-v1\.d\/.+\.key$/.test(n))) fail("data/os/keys/boot-gpg holds no private key");
if (!want.includes("escrow/escrow.key")) console.log(">> escrow/escrow.key is not in data/os/keys (kept offline): backed up without it");
const before = Object.fromEntries(want.map((n) => [n, sha(join(KEYS, n))]));

mkdirSync(scratch, { recursive: true });
try {
  // 1. backup
  const b = tool(["backup", backup], PASS);
  if (b.status !== 0) fail(`backup failed:\n${b.out}${b.err}`);
  if (!b.out.includes(createHash("sha256").update(readFileSync(backup)).digest("hex"))) fail("backup does not print the file's sha256");
  if (!/two copies off this laptop/.test(b.out) || !/passphrase apart/.test(b.out)) fail("backup does not tell where to keep the copies and the passphrase");
  only(scratch, ["keys.tar.gpg"], "after the backup");  // no plain tar, no .part left behind
  console.log(`ok   backup written (${statSync(backup).size} bytes), nothing else in ${relative(ROOT, scratch)}`);

  // 2. it is an OpenPGP message, and no key or the passphrase can be read in it
  const blob = readFileSync(backup);
  const tag = blob[0] & 0x40 ? blob[0] & 0x3f : (blob[0] >> 2) & 0x0f;
  if (!(blob[0] & 0x80) || tag !== 3) fail(`the backup does not start with a symmetric-key OpenPGP packet (byte 0x${blob[0].toString(16)})`);
  if (blob.subarray(257, 262).toString("latin1") === "ustar") fail("the backup is a plain tar");
  if (blob.includes("PRIVATE KEY")) fail("the backup contains the text PRIVATE KEY");
  if (blob.includes(PASS)) fail("the backup contains the passphrase");
  const secrets = want.filter((n) => /\.key$|^grub-password$|^maintenance_ed25519$/.test(n));
  for (const n of secrets) {
    const k = readFileSync(join(KEYS, n));
    const mid = k.subarray(Math.max(0, (k.length >> 1) - 24), (k.length >> 1) + 24);  // a slice from the middle of the key
    if (blob.includes(k) || (mid.length >= 16 && blob.includes(mid))) fail(`the backup contains bytes of ${n}`);
  }
  console.log(`ok   OpenPGP symmetric message (tag 3), not a tar; no PRIVATE KEY, no bytes of ${secrets.length} secret files, no passphrase`);

  // 3. list names every file
  const l = tool(["list", backup], PASS);
  if (l.status !== 0) fail(`list failed:\n${l.out}${l.err}`);
  const listed = l.out.split(/\r?\n/).filter((s) => s && !s.startsWith(">>"));
  const missing = want.filter((n) => !listed.includes(n));
  const extra = listed.filter((n) => !want.includes(n));
  if (missing.length || extra.length) fail(`list: missing ${JSON.stringify(missing)}, extra ${JSON.stringify(extra)}`);
  console.log(`ok   list names all ${want.length} files of data/os/keys`);

  // 4. a wrong passphrase: list and restore refused, nothing written
  const wrong = join(scratch, "restore-wrong");
  mkdirSync(wrong);
  if (tool(["list", backup], PASS + "x").status === 0) fail("list opened the backup with a wrong passphrase");
  const w = tool(["restore", backup, wrong], PASS + "x");
  if (w.status === 0) fail("restore accepted a wrong passphrase");
  if (readdirSync(wrong).length) fail(`restore with a wrong passphrase wrote ${JSON.stringify(readdirSync(wrong))}`);
  only(scratch, ["keys.tar.gpg", "restore-wrong"], "after the wrong passphrase");
  rmSync(wrong, { recursive: true });
  console.log("ok   a wrong passphrase is refused (list and restore), the target stays empty");

  // 5. targets the tool must refuse: outside data/os, inside data/os/keys, not empty
  const full = join(scratch, "not-empty");
  mkdirSync(full);
  writeFileSync(join(full, "x"), "x");
  for (const [what, target] of [["outside data/os", join(ROOT, "data", `keys-restore-${process.pid}`)],
    ["inside data/os/keys", join(KEYS, `restore-${process.pid}`)], ["a non-empty folder", full]]) {
    const t = tool(["restore", backup, target], PASS);
    if (t.status === 0) fail(`restore into ${what} (${target}) was not refused`);
    if (target !== full && existsSync(target)) { rmSync(target, { recursive: true, force: true }); fail(`restore into ${what} made ${target}`); }
  }
  if (readdirSync(full).join() !== "x") fail("a refused restore changed the non-empty folder");
  rmSync(full, { recursive: true });
  console.log("ok   restore refuses a target outside data/os, inside data/os/keys and a non-empty folder");

  // 6. the right passphrase restores every file byte for byte
  const right = join(scratch, "restore");
  mkdirSync(right);
  const r = tool(["restore", backup, right], PASS);
  if (r.status !== 0) fail(`restore failed:\n${r.out}${r.err}`);
  const got = files(right);
  if (got.join("\n") !== want.join("\n")) fail(`restored files differ: ${JSON.stringify(got.filter((n) => !want.includes(n)))} / ${JSON.stringify(want.filter((n) => !got.includes(n)))}`);
  for (const n of want) if (sha(join(right, n)) !== before[n]) fail(`restored ${n} differs from data/os/keys/${n}`);
  for (const n of want) if (sha(join(KEYS, n)) !== before[n]) fail(`data/os/keys/${n} changed during the check`);
  console.log(`ok   restored ${got.length} files, each sha256 equal to data/os/keys`);
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
if (existsSync(scratch)) fail(`${scratch} could not be removed: delete it by hand (it holds a restored copy of the keys)`);
console.log("ok   the restored copy and the backup file removed");
console.log("KEYS BACKUP CHECK PASSED");
