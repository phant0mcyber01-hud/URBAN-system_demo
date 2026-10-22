// The owner's side of a unit's data recovery (step 7). A unit whose TPM no longer matches its Secure Boot state (Secure Boot
// turned off, its keys changed, a firmware update) keeps its data locked and asks on its console for the recovery key. That
// key lies on the unit's ESP only encrypted to the escrow key (urban-recovery.enc); this opens it with the private half and
// prints the recovery key to type there.
//
//   node os/recovery.mjs decrypt <urban-recovery.enc> [--key <escrow.key>]
//
// The decryption runs in the builder container (os/recovery.sh, openssl RSA-OAEP SHA-256: the same code the QEMU check
// uses). The escrow key is data/os/keys/escrow/escrow.key unless --key names another file, e.g. on the USB stick it is kept
// on offline. Nothing is written anywhere; the recovery key is only printed.
import { existsSync, statSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { OUT, ensureBuilder, fail, inBuilder } from "./tests/lib.mjs";

const usage = "usage: node os/recovery.mjs decrypt <urban-recovery.enc> [--key <escrow.key>]";
const args = process.argv.slice(2);
if (args[0] !== "decrypt" || !args[1]) fail(usage);
let keyArg = null;
const rest = args.slice(2);
for (let i = 0; i < rest.length; i++) {
  if (rest[i] === "--key" && rest[i + 1]) keyArg = rest[++i];
  else fail(usage);
}
const file = resolve(args[1]);
if (!existsSync(file) || !statSync(file).isFile()) fail(`no such file: ${file}`);
const quote = (s) => `'${s.replace(/'/g, `'\\''`)}'`;

const extra = ["-v", `${dirname(file)}:/in:ro`];
let keyIn = "/out/keys/escrow/escrow.key";  // data/os/keys is mounted there by inBuilder
if (keyArg) {
  const key = resolve(keyArg);
  if (!existsSync(key) || !statSync(key).isFile()) fail(`no such escrow key: ${key}`);
  extra.push("-v", `${dirname(key)}:/escrow:ro`);
  keyIn = `/escrow/${basename(key)}`;
} else if (!existsSync(join(OUT, "keys", "escrow", "escrow.key"))) {
  fail(`the escrow private key is not in ${join(OUT, "keys", "escrow")} (kept offline?): give it with --key <path>`);
}
ensureBuilder();
const r = inBuilder(`bash /work/os/recovery.sh decrypt ${quote(`/in/${basename(file)}`)} ${quote(keyIn)}`, { extra });
process.exit(r.status ?? 1);
