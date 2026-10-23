// The off-line copy of the keys (step 9). data/os/keys holds what every build signs with and what the units in the field
// trust: the update CA and signing key, the Secure Boot PK/KEK/db keys, the GRUB boot key (boot-gpg/), the recovery escrow
// key, the fleet's GRUB password and the maintenance key. Lost with this laptop, no new image or update would ever boot on
// the units already installed. This packs the whole folder into one file encrypted with the owner's passphrase.
//
//   node os/keys-backup.mjs backup  <out file>                 e.g. E:\urban-keys-2026-10-23.tar.gpg (a USB stick)
//   node os/keys-backup.mjs list    <backup file>              the file names inside (asks the passphrase)
//   node os/keys-backup.mjs restore <backup file> <empty folder under data/os>
//
// The passphrase is asked in the terminal (twice for a backup, at least 12 characters) or, for the tests, taken from the
// environment variable URBAN_KEYS_PASSPHRASE. It reaches gpg only on the container's stdin: never a command line, never a
// file. The work runs in the builder container, so gpg and tar are the same on every machine: tar | gpg --symmetric
// (AES-256, a slow S2K) in one pipe, no plain tar anywhere; the keys are mounted read-only, the container has no network.
// A restore first decrypts the whole file to nowhere (the passphrase and the file's integrity checked), and only then
// unpacks: a wrong passphrase or a damaged file writes nothing.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";
import { OUT, ROOT, ensureBuilder, fail } from "./tests/lib.mjs";

const IMAGE = "urban-os-builder:bookworm";  // the builder (os/tests/lib.mjs)
const KEYS = join(OUT, "keys");
const usage = "usage: node os/keys-backup.mjs backup <out file> | list <backup file> | restore <backup file> <empty folder under data/os>";
const [cmd, fileArg, targetArg] = process.argv.slice(2);
if (!["backup", "list", "restore"].includes(cmd) || !fileArg || (cmd === "restore") !== Boolean(targetArg)) fail(usage);
const file = resolve(fileArg);
const quote = (s) => `'${s.replace(/'/g, `'\\''`)}'`;
const inside = (dir, p) => { const r = relative(dir, p); return r === "" || (!r.startsWith("..") && !isAbsolute(r)); };

// a hidden line from the terminal (raw mode: nothing is echoed)
function ask(prompt) {
  process.stdout.write(prompt);
  process.stdin.setRawMode(true);
  process.stdin.setEncoding("utf8");
  process.stdin.resume();
  return new Promise((done) => {
    let s = "";
    const on = (chunk) => {
      for (const c of chunk) {
        if (c === "\u0003") { process.stdin.setRawMode(false); process.stdout.write("\n"); process.exit(130); }
        if (c === "\r" || c === "\n") {
          process.stdin.off("data", on);
          process.stdin.setRawMode(false);
          process.stdin.pause();
          process.stdout.write("\n");
          return done(s);
        }
        if (c === "\u007f" || c === "\b") s = s.slice(0, -1);
        else s += c;
      }
    };
    process.stdin.on("data", on);
  });
}

async function passphrase({ twice }) {
  let p = process.env.URBAN_KEYS_PASSPHRASE;
  delete process.env.URBAN_KEYS_PASSPHRASE;  // not handed on to docker or anything else this starts
  if (p !== undefined) {
    console.log(">> passphrase from URBAN_KEYS_PASSPHRASE (for tests; the owner types it)");
  } else {
    if (!process.stdin.isTTY) {
      fail("no terminal to ask the passphrase in: run this in PowerShell or cmd (in Git Bash: winpty node os/keys-backup.mjs ...)");
    }
    p = await ask("Passphrase of the keys backup: ");
    if (twice && (await ask("The same passphrase again: ")) !== p) fail("the two passphrases differ: nothing written");
  }
  if (/[\r\n]/.test(p)) fail("the passphrase must be one line");
  if (twice && [...p].length < 12) fail("the passphrase is too short: at least 12 characters (a sentence is easier to keep than a code)");
  return p;
}

// one bash script in the builder; the passphrase is the first line of its stdin (read into $P, handed to gpg on fd 3
// by printf, a shell builtin: it never becomes an argument of a process)
function inGpg(script, pass, mounts, { capture = false } = {}) {
  ensureBuilder();
  const prelude = `set -euo pipefail
IFS= read -r P
export GNUPGHOME=$(mktemp -d)
gpgp() { gpg --batch --quiet --no-symkey-cache --pinentry-mode loopback --passphrase-fd 3 "$@" 3< <(printf '%s\\n' "$P"); }
`;
  const args = ["run", "--rm", "-i", "--network", "none"];
  for (const [host, at] of mounts) args.push("-v", `${host}:${at}`);
  args.push(IMAGE, "bash", "-c", prelude + script);
  const r = spawnSync("docker", args, { cwd: ROOT, input: pass + "\n", encoding: "utf8", maxBuffer: 64 << 20,
    stdio: ["pipe", capture ? "pipe" : "inherit", "inherit"] });
  if (r.error) throw r.error;
  return r;
}

const sha256 = (f) => createHash("sha256").update(readFileSync(f)).digest("hex");

if (cmd === "backup") {
  if (!existsSync(join(KEYS, "ca.pem"))) fail(`no keys in ${KEYS}: nothing to back up (a build makes them: node os/build.mjs)`);
  if (inside(KEYS, file)) fail("the backup cannot be written inside data/os/keys (it would pack itself)");
  if (existsSync(file)) fail(`${file} exists: not overwriting it, give a new name`);
  mkdirSync(dirname(file), { recursive: true });
  const pass = await passphrase({ twice: true });
  const name = basename(file);
  const r = inGpg(`out=/backup/${quote(name)}
trap 'rm -f "$out.part"' EXIT
rm -f "$out.part"
tar -C /keys --sort=name -cf - . | gpgp --symmetric --cipher-algo AES256 --s2k-mode 3 --s2k-count 65011712 \\
  --s2k-digest-algo SHA512 --output "$out.part"
gpgp --decrypt "$out.part" | tar -tf - > /dev/null  # it opens with this passphrase, to the last byte
mv "$out.part" "$out"
trap - EXIT`, pass, [[KEYS, "/keys:ro"], [dirname(file), "/backup"]]);
  if (r.status !== 0 || !existsSync(file)) fail("the backup was not written");
  console.log(`>> keys backup: ${file}`);
  console.log(`>> sha256 ${sha256(file)}  (${statSync(file).size} bytes)`);
  console.log(">> keep two copies off this laptop (two USB sticks in two places), and the passphrase apart from them:");
  console.log(">> the file without the passphrase is useless, and so is the passphrase without the file.");
  console.log("KEYS BACKUP WRITTEN");
} else {
  if (!existsSync(file) || !statSync(file).isFile()) fail(`no such file: ${file}`);
  const backup = [dirname(file), "/backup:ro"];
  const f = `/backup/${quote(basename(file))}`;
  if (cmd === "list") {
    const pass = await passphrase({ twice: false });
    const r = inGpg(`names=$(gpgp --decrypt ${f} | tar -tf - 2>/dev/null) || { echo "keys-backup: wrong passphrase or a damaged file" >&2; exit 2; }
printf '%s\\n' "$names" | sed -e 's|^\\./||' -e '/\\/$/d' -e '/^$/d'`, pass, [backup]);
    process.exit(r.status ?? 1);
  }
  // restore: only into an empty folder under data/os (and not into the keys themselves)
  const target = resolve(targetArg);
  if (!inside(OUT, target) || target === OUT) fail(`the restore goes into a folder under ${OUT}, not ${target}`);
  if (inside(KEYS, target)) fail("not into data/os/keys: restore next to it, compare, then put it in place by hand");
  const made = !existsSync(target);
  if (!made && (!statSync(target).isDirectory() || readdirSync(target).length)) fail(`${target} is not an empty folder: nothing restored`);
  const pass = await passphrase({ twice: false });
  mkdirSync(target, { recursive: true });
  const r = inGpg(`gpgp --decrypt ${f} | tar -tf - > /dev/null 2>&1 || { echo "keys-backup: wrong passphrase or a damaged file: nothing restored" >&2; exit 2; }
gpgp --decrypt ${f} | tar -C /restore --no-same-owner -xf - || { find /restore -mindepth 1 -delete; echo "keys-backup: unpacking failed: nothing restored" >&2; exit 1; }`,
  pass, [backup, [target, "/restore"]]);
  if (r.status !== 0) {
    if (made) rmSync(target, { recursive: true, force: true });
    fail("restore failed");
  }
  console.log(`>> restored into ${target}`);
  console.log(">> to build with them: compare with data/os/keys (or put this folder in its place when the laptop is new),");
  console.log(">> then delete this copy: unencrypted keys belong in data/os/keys only");
  console.log("KEYS RESTORED");
}
