// G2 / step 6: with UEFI Secure Boot enforced (URBAN's own PK/KEK/db enrolled in the VM's firmware) the unit boots with
// the signed chain and a verified root; a boot loader not signed by URBAN's db is refused by the firmware (unsigned, and
// signed by a foreign key); a boot config changed on the ESP is not executed; one changed byte in an installed slot
// makes that slot fail and the unit comes back on the other slot by itself. Each fact has its control (the same image
// with Secure Boot off; the original loader on the same machine; the restored config; the slot verified before the
// change). One machine at a time, emulated: a long run.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
build("2");
if (scenario("secureboot") !== 0) fail("secure boot scenario");
