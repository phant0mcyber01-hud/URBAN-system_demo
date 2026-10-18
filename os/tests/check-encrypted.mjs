// G6: with a TPM the data partition becomes LUKS2 unlocked by that TPM at first boot (what the installer put there
// is kept, a reboot unlocks it again); without a TPM it stays plain and the screen says so. The proof is the raw
// partition: the maintenance key is nowhere on the encrypted one and in clear on the plain one (the control).
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("encrypted") !== 0) fail("encryption scenario");
