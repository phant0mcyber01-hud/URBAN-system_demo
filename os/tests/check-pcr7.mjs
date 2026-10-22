// G4 / step 7: the data partition's key is sealed in the TPM to PCR 7 (the firmware's Secure Boot state). One machine with
// a TPM and Secure Boot, four facts: (a) first boot encrypts, the TPM token is bound to PCR 7, a recovery key slot exists
// and urban-recovery.enc (the recovery key encrypted to the escrow key) lies on the ESP; switched off and on exactly as in
// (b) with Secure Boot unchanged, it unlocks by itself (the control: the TPM's state survives that power path, so what
// locks (b) is PCR 7); (b) the same machine started with Secure Boot off: DATA LOCKED on its consoles with the advice to
// turn Secure Boot on before typing the key, its data not mounted, none of its services and no SSH, the raw partition
// the same LUKS2 volume (nothing wiped), a wrong key typed at the password agent's prompt refused and asked again;
// (c) the recovery key decrypted from the ESP's file with the escrow private key (os/recovery.sh, what
// `node os/recovery.mjs decrypt` runs) and typed at the prompt on the serial console opens the data, the unit re-seals to
// the new state and boots with the same device key, no key typed shows on the console or in the journal, and the screen
// says the seal protects nothing while Secure Boot is off; (d) the next boot with Secure Boot still off unlocks by itself
// and still says so. The scenario prints PCR7 CHECK PASSED only when all of it holds. Emulated: a long run.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("pcr7") !== 0) fail("PCR 7 scenario");
