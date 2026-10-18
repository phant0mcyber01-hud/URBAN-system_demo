// Shared by the OS checks: the builder container, builds that are redone only when their inputs change,
// and the VM scenarios (os/tests/vm.py) run inside the same container. Node only: works from any shell.
//
// Where things live: the big, rebuildable files (Debian cache, slot images, disk images, bundles, VM copies) stay
// in the Docker volume "urban-os", where empty blocks cost nothing. On this machine, in data/os/, only what must
// not be lost or is worth reading: keys/ (the update CA and signing key) and logs/ (serial consoles of the VMs).
// `node os/build.mjs --export` copies a finished disk image (compressed) and its bundle out for flashing.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
export const OUT = join(ROOT, "data", "os");
const IMAGE = "urban-os-builder:bookworm";
const VOLUME = "urban-os";

function run(cmd, args, { capture = false } = {}) {
  const r = spawnSync(cmd, args, { cwd: ROOT, encoding: "utf8", stdio: capture ? "pipe" : "inherit", maxBuffer: 64 << 20 });
  if (r.error) throw r.error;
  return r;
}

export function fail(message) {
  console.error("FAILED: " + message);
  process.exit(1);
}

export function docker(args, opts) {
  return run("docker", args, opts);
}

let ready = false;
// the builder image is rebuilt only when its Dockerfile changed: the image carries the Dockerfile's digest as a label
export function dockerfileDigest() {
  return createHash("sha256").update(readFileSync(join(ROOT, "os", "build", "Dockerfile"))).digest("hex").slice(0, 16);
}
export function ensureBuilder() {
  if (ready) return;
  const info = docker(["info", "--format", "{{.ServerVersion}}"], { capture: true });
  if (info.status !== 0) fail("the Docker engine is not running (it is only the build and test environment)");
  const want = dockerfileDigest();
  const have = docker(["image", "inspect", "-f", '{{index .Config.Labels "urban.dockerfile"}}', IMAGE], { capture: true });
  if (have.status !== 0 || have.stdout.trim() !== want) {
    console.log(">> builder image: building from os/build/Dockerfile");
    const r = docker(["build", "-q", "--label", `urban.dockerfile=${want}`, "-t", IMAGE, join(ROOT, "os", "build")], { capture: true });
    if (r.status !== 0) fail("builder image: " + r.stderr);
  }
  ready = true;
}

let kvm = null;
const hasKvm = () => spawnSync("docker", ["run", "--rm", "--device", "/dev/kvm", IMAGE, "true"], { encoding: "utf8" }).status === 0;

// one command in the builder; the repository is /work, the volume is /out, keys and logs come from data/os
export function inBuilder(command, { privileged = false, capture = false, extra = [] } = {}) {
  for (const d of ["keys", "logs"]) mkdirSync(join(OUT, d), { recursive: true });
  if (kvm === null) kvm = hasKvm();
  const args = ["run", "--rm", "-v", `${ROOT}:/work`, "-v", `${VOLUME}:/out`,
    "-v", `${join(OUT, "keys")}:/out/keys`, "-v", `${join(OUT, "logs")}:/out/logs`, ...extra];
  if (privileged) args.push("--privileged");
  if (kvm) args.push("--device", "/dev/kvm", "-e", "URBAN_KVM=1");
  args.push(IMAGE, "bash", "-c", command);
  return docker(args, { capture });
}

function walk(dir, out) {
  if (!existsSync(dir)) return out;
  for (const name of readdirSync(dir).sort()) {
    if (name === "__pycache__" || name.endsWith(".egg-info")) continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}

// everything a build is made of: when this digest changes, the build is redone
export function inputsDigest() {
  const files = [
    ...walk(join(ROOT, "os", "build"), []), ...walk(join(ROOT, "os", "rootfs"), []),
    ...walk(join(ROOT, "backend", "urban"), []), join(ROOT, "backend", "pyproject.toml"),
    join(ROOT, "deploy", "urban-edge@.service"), join(ROOT, "deploy", "urban-vision@.service"),
    join(ROOT, "data", "models", "yolox_nano.onnx"), join(OUT, "keys", "ca.pem"),
    ...walk(join(ROOT, "web", "dist"), []),
  ];
  const h = createHash("sha256");
  for (const f of files) {
    if (!existsSync(f)) continue;
    h.update(relative(ROOT, f).replace(/\\/g, "/"));
    h.update(readFileSync(f));
  }
  return h.digest("hex").slice(0, 16);
}

// build.sh for one version, unless the same inputs were built already (the stamp lives in the volume, next to
// the files it vouches for)
export function build(version, { image = false, broken = "" } = {}) {
  ensureBuilder();
  if (!existsSync(join(ROOT, "web", "dist", "index.html"))) fail("web/dist is missing: npm run build in web/ (the control centre's interface goes into the image)");
  const digest = inputsDigest() + (image ? "-image" : "") + (broken ? `-${broken}` : "");
  const want = [`/out/urban-edge-${version}.raucb`, ...(image ? [`/out/urban-edge-${version}.img`] : [])];
  const fresh = inBuilder(`[ "$(cat /out/stamp-${version} 2>/dev/null)" = "${digest}" ] && ${want.map((f) => `[ -f ${f} ]`).join(" && ")}`,
    { capture: true });
  if (fresh.status === 0) {
    console.log(`>> version ${version}: up to date (${digest})`);
    return;
  }
  const flags = `--version ${version}${image ? " --image" : ""}${broken ? ` --break ${broken}` : ""}`;
  console.log(`>> building version ${version} ${flags}`);
  const r = inBuilder(`bash /work/os/build/build.sh ${flags} && echo -n ${digest} > /out/stamp-${version}`, { privileged: true });
  if (r.status !== 0) fail(`build of version ${version} failed`);
}

// copy a finished image (gzip) and bundle out of the volume, for writing onto a unit's disk
export function exportRelease(version, { image = true } = {}) {
  mkdirSync(join(OUT, "release"), { recursive: true });
  const r = inBuilder(
    `set -e; ${image ? `gzip -1 -c /out/urban-edge-${version}.img > /release/urban-edge-${version}.img.gz; ` : ""}` +
    `cp /out/urban-edge-${version}.raucb /out/manifest-${version}.json /release/; ls -la /release`,
    { extra: ["-v", `${join(OUT, "release")}:/release`] });
  if (r.status !== 0) fail("export failed");
}

// a scenario of os/tests/vm.py in the builder (QEMU inside the container)
// (a last argument { extra: [docker args] } adds mounts, e.g. the release folder)
export function scenario(name, ...args) {
  ensureBuilder();
  const { extra = [] } = typeof args.at(-1) === "object" ? args.pop() : {};
  const r = inBuilder(`python3 /work/os/tests/vm.py ${name} ${args.join(" ")}`, { privileged: true, extra });
  return r.status;
}
