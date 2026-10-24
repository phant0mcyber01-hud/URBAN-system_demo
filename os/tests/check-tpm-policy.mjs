// G1 (step 10): the measured-boot TPM mechanism, proven on a software TPM before any unit runs it.
//
// The design (docs/measured-boot-design.md) seals the data key under a policy that releases it to the current release
// on THIS installation, refuses an older release once the anti-rollback floor is raised, refuses a genuine image of
// the same release booted as another installation (a different maintenance trust root), releases again after an
// ordinary update without the recovery key, and keeps a recovery path that opens in any boot state. This check plays
// exactly that with the real TPM commands (tpm2-tools 5.4 as bookworm ships them) against swtpm in a container: a
// secret is sealed, then unsealed under the design's policy in each state, each refusal a real TPM policy failure with
// a control that the same command succeeds in the right state. See os/tests/tpm-policy.sh for the commands.
//
// tpm2-tools is not in the builder image, so this builds a tiny derived image (urban-tpm-test:bookworm) once from it,
// adding tpm2-tools from bookworm; later runs reuse it. Needs the Docker engine (the build/test environment); no QEMU,
// no OS image. If `docker info` is not ready yet (Docker Desktop still starting) it waits up to five minutes.
import { spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ROOT, docker, ensureBuilder, dockerfileDigest, fail } from "./lib.mjs";

const BUILDER = "urban-os-builder:bookworm";
const IMAGE = "urban-tpm-test:bookworm";

// wait for the Docker engine (Docker Desktop may still be starting): up to five minutes
function waitForDocker() {
  const deadline = Date.now() + 5 * 60 * 1000;
  for (;;) {
    if (docker(["info", "--format", "{{.ServerVersion}}"], { capture: true }).status === 0) return;
    if (Date.now() > deadline) fail("the Docker engine did not become ready within five minutes (it is the build and test environment)");
    const w = spawnSync(process.execPath, ["-e", "setTimeout(()=>{},5000)"]);  // sleep 5s without a shell
    if (w.status === null) fail("interrupted while waiting for Docker");
  }
}

// the derived image carries the builder's Dockerfile digest as a label: rebuilt when the builder changes
function ensureTpmImage() {
  const want = dockerfileDigest();
  const have = docker(["image", "inspect", "-f", '{{index .Config.Labels "urban.tpmtest"}}', IMAGE], { capture: true });
  if (have.status === 0 && have.stdout.trim() === want) return;
  console.log(">> tpm-test image: building from " + BUILDER + " + tpm2-tools (bookworm)");
  const ctx = mkdtempSync(join(tmpdir(), "urban-tpm-"));
  writeFileSync(join(ctx, "Dockerfile"),
    `FROM ${BUILDER}\n` +
    `RUN apt-get update && apt-get install -y --no-install-recommends tpm2-tools && rm -rf /var/lib/apt/lists/*\n`);
  const r = docker(["build", "-q", "--label", `urban.tpmtest=${want}`, "-t", IMAGE, ctx], { capture: true });
  if (r.status !== 0) fail("could not build the tpm-test image (tpm2-tools from bookworm):\n" + (r.stderr || r.stdout));
}

waitForDocker();
ensureBuilder();     // the builder must exist and be current (the derived image is FROM it)
ensureTpmImage();

// run the mechanism. --privileged is not needed: swtpm is a pure userspace TCP TPM. The repo is read-only at /work.
const r = docker(["run", "--rm", "-v", `${ROOT}:/work:ro`, IMAGE, "bash", "/work/os/tests/tpm-policy.sh"],
  { capture: true });
const out = (r.stdout || "") + (r.stderr || "");
process.stdout.write(out.endsWith("\n") ? out : out + "\n");
if (r.status !== 0) fail("the TPM policy check exited non-zero (a scenario did not hold)");
if (!out.includes("TPM POLICY CHECK PASSED")) fail("the TPM policy mechanism did not pass on swtpm");
// the script printed the verdict line above; the gate greps the command's output for it
