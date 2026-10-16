// Build URBAN Edge OS:  node os/build.mjs [--version 1] [--bundle-only] [--export]
// (--export with --bundle-only copies just the update bundle)
// Needs a running Docker engine: it is only the build environment (Debian bookworm), the OS has no Docker.
// The build stays in the Docker volume "urban-os"; --export copies the disk image (gzip) and the signed update
// bundle to data/os/release/ for writing onto a unit. The update keys are in data/os/keys/ (see docs/edge-os.md).
import { build, exportRelease } from "./tests/lib.mjs";

const args = process.argv.slice(2);
const at = args.indexOf("--version");
const version = at >= 0 ? args[at + 1] : "1";
if (!/^[0-9][0-9A-Za-z.+-]*$/.test(version || "")) {
  console.error("version: digits first, then letters, digits, . + -");
  process.exit(2);
}
const image = !args.includes("--bundle-only");
build(version, { image });
if (args.includes("--export")) exportRelease(version, { image });
