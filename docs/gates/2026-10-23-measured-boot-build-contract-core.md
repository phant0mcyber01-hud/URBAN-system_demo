# Step 11 integration contract: core (sections 1-3)

Scope: how URBAN OS units provision, store and use the measured-boot objects proven in step 10
(docs/measured-boot-design.md, os/tests/tpm-policy.sh). Names used throughout:

- **S**: the release's security version, an integer equal to the release version (release.py already requires an int;
  build.sh:24 and unit-image.mjs:49 must be tightened to `^[0-9]+$`).
- **EK name**: the 34-byte TPM name of the RSA endorsement key, `000b || SHA-256(TPMT_PUBLIC)`, read with
  `tpm2_createek -G rsa` + `tpm2_readpublic -n` (tpm-policy.sh:49-51). Written as 68 lower-case hex characters.
- **P** ("approved object"): the step-10 sealed object, `authPolicy = PolicyAuthorize(K_unseal, REF_U) then PolicyPCR(7)`.
- **T** ("first-release object"): the provisioning object defined in section 1.
- **b**: the unit's base, the value of NV_FLOOR after its first increment on this TPM.

Corrections to the step-10 text that this contract makes binding:

- design.md:122 says the release signature rides in the RAUC bundle. Approvals are per unit (N3), so they cannot be in
  the fleet-wide bundle. They come over the device channel and are stored on the ESP (section 3).
- design.md:353 says the centre's release path signs. The centre never signs. It exports a request, stores signed
  approvals and delivers them. Only the offline owner CLI signs (section 3.4).
- design.md:262 says the NV_FLOOR name is the same on all units. In the proof (tpm-policy.sh:66) its authPolicy contains
  REF_I, which contains the EK name. The NV name is therefore per unit, and the signer recomputes it (section 2.4).

---

## 1. Provisioning a new unit

### 1.1 The problem

Every approval is bound to the unit's EK name (policyRef) and to its base b (the PolicyNV operand b+S+1). The EK name
exists only on the board. b exists only after the first NV_FLOOR increment on that TPM. But the data partition must be
encrypted and usable from the first boot: urban-prepare creates the device key, the enrolment code, the WireGuard key
and the kit there (urban-prepare:191-229). So the unit has to pass boot 1 and boots 2..k before any owner approval for
it can exist. No step of that period may be an unattended path that another genuine URBAN image, booted from a USB stick
on the same board, could use to obtain the internal disk's key.

### 1.2 Options weighed

**(A) The stand reads the EK name before the image is written, and the owner signs approvals into the per-unit image.**
os/unit-image.mjs runs in the builder container on the stand laptop and writes an image file, which is then put on the
disk with Rufus (unit-image.mjs:75-122). The board's TPM is not reachable from there. Reading the EK would need a
separate provisioning boot on the board. Even with the EK, b is unknown until the counter's first increment on that TPM.
A board with a used fTPM starts high, and the TPM's retained maxCounter decides the value (scenario 7). So the unit
needs a boot on the board, then an owner signing round, then a second image write: two stand trips per unit, and the
second boot still needs something to open the data before the approvals are written. Rejected.

**(B) The first boot seals nothing until an approval arrives over the kit channel.** The data partition would stay
plain until then: device-key blobs, the WireGuard private key and the kit sit unencrypted on a disk that can be removed.
A disk-in-hand attacker could also edit `/var/lib/urban/ssh/authorized_keys` on the plain partition and get a shell
later. Keeping the secret only in RAM until the approval arrives fails at the first power cut (DATA LOCKED or data
loss). It also needs in-place encryption of a partition already in use, which urban-grow deliberately refuses
(urban-grow:315-319). Rejected.

**(C) A temporary seal, replaced by P at the first approved boot.** With PCR 7 alone it is unsafe. The phase lock
changes only PCR 11, so a PCR-7-only object still opens for a root shell obtained after boot. A genuine image from USB
with a foreign maintenance.pub gives exactly such a shell (attack 2). The idea is sound, though, if the temporary object
is bound to everything an owner approval would bind it to. **Chosen, in that form:** T below.

### 1.3 Decision: a first-release object T, sealed beside P at first boot

At first boot the unit seals the same freshly generated secret into two objects:

- **P**, the final object. Its authPolicy needs only `K_unseal`'s TPM name (the public key is in the image), the unit's
  own EK name (for REF_U) and the current PCR 7. No approval is needed to create it.
- **T**, the first-release object. Its authPolicy is the literal composite policy:

  ```
  T.authPolicy = PolicyPCR(sha256:7,11 = current PCR 7, PCR 11 = extend-from-zero("urban-svn:S"))
                 AND PolicyNV(NV_FLOOR, operand b+S+1 as 8-byte big-endian, offset 0, UNSIGNED_LT)
                 AND PolicyCommandCode(TPM2_CC_Unseal)
  ```

  T applies exactly the conditions an owner approval for (this EK, b, S) would apply. It differs from such an approval
  in one way: it is fixed into the object, not signed. T is only ever sealed by a genuine image, at its own pre-lock
  stage, for a secret that this same boot has just generated.

Reasoning. T opens the data for release S on this board under the same three conditions as P with an approval for S:
the Secure Boot state, the release's PCR 11 before the phase lock, and the floor below b+S+1. It is therefore never more
permissive than P. It needs no owner round trip, so boots 2..k open by themselves. It loses its use without any action
when the floor reaches b+S+1, which happens once a later release S+1 is marked good. A copy of T's blob kept from an old
LUKS header is then refused at PolicyNV, so T leaves no rollback path. The only change to the step-10 policy is that the
first, base-setting NV_FLOOR increment becomes unsigned (section 2.4). Nothing about P changes.

**Why no USB-booted genuine image can use this path.** Consider a genuine URBAN image booted from USB on the same board,
with a foreign maintenance.pub and a foreign authorized_keys on its stick:

1. Its urban-grow formats and opens only the data partition of its own boot disk, the stick: step 7, urban.bootpart
   GUID, urban-grow:42/:87. Any T it makes is for its own fresh secret. It never has the internal disk's secret and
   cannot seal it.
2. It never alters an existing written NV_FLOOR. Its first boot finds the index written with the expected name and only
   reads it (section 1.5, rule F2). So the internal disk's floor, and with it T's and P's NV condition, are unchanged.
3. Its only shell comes from its own maintenance.pub over SSH. ssh.service is ordered after urban-phase-lock.service,
   so by then PCR 11 already carries `urban-phase:data-unlocked`. The internal disk's T is refused at PolicyPCR(11)
   (literal value mismatch), and P is refused at PolicyAuthorize (scenario 6).
4. The internal disk's own unseal stage runs only when the internal disk's GRUB and initramfs boot. They are signed,
   with no menu editing and the command-line allowlist.
5. Copying the internal data partition onto the stick and booting the genuine image from it is booting a clone. It is
   already named open in design.md:284-289: the clone opens, but it gets no shell (the clone's unit.conf exists, so
   the stick's maintenance.pub is ignored, urban-prepare:39-57), and the phase lock closes any re-unseal.

**Rule that keeps T from becoming a re-seal path.** T is created only in one of two situations, both before the phase
lock. (a) On a freshly formatted data partition, for the secret this boot generated: `encrypt()`, the ext4 branch of
urban-grow:569-570. (b) In a recovery re-seal, after a person has typed the recovery key, and only when this same boot
has performed the unsigned first increment of NV_FLOOR, which happens only after a TPM clear. No code path ever re-seals
an already-released secret into T automatically.

### 1.4 Preconditions checked before anything is written (first boot)

urban-grow checks all of the following before `luksFormat`. If any one fails, it formats nothing, shows a fixed reason
word on the console, and halts the first boot. The partition stays plain ext4 and empty, so a later boot retries:

| Check | Reason word on failure |
|---|---|
| A TPM 2.0 is present (`/sys/class/tpm/tpm0`, `/dev/tpmrm0` within 10 s) | `NO-TPM` |
| SecureBoot efivar = 1 (urban-grow:51 `SBVAR`) | `SECURE-BOOT-OFF` |
| `db` contains the URBAN db2 certificate and `dbx` contains the old URBAN db certificate (efivars db/dbx parsed as EFI_SIGNATURE_LISTs and compared with `/usr/share/urban/sb/db2.cer` and `old-db.cer`) | `ENROLMENT-OLD` |
| PCR 11 (sha256) equals extend-from-zero(`urban-svn:<S>`), with S read from `/etc/urban-svn` in the verity root and equal to `VERSION` in `/etc/urban-os-release` | `PCR11-UNEXPECTED` |
| `tpm2_createek -G rsa` succeeds (empty endorsement auth) | `EK-UNREADABLE` |
| The TPM names of the image's approval keys, computed with `tpm2_loadexternal`, equal the names shipped in `/usr/share/urban/approve/*.name` | `KEY-NAME-MISMATCH` |
| NV_FLOOR passes rule F1, F2 or F3 (section 1.5) | `FLOOR-FOREIGN` |

A first boot with Secure Boot off is refused, which changes today's behaviour of sealing with a warning (urban-grow:16).
Under step 11 a seal made with Secure Boot off protects nothing: any loader gives the same PCR 7, and a root shell could
extend PCR 11 itself.

### 1.5 NV_FLOOR at first boot (never destructive)

The unit computes the name it expects for NV_FLOOR (section 2.4) and reads the index's public area with
`tpm2_nvreadpublic`:

- **F1. The index is absent.** Define it with the step-11 attributes and authPolicy. Do the unsigned first increment
  (branch B, section 2.4). b is the value read afterwards.
- **F2. The index is present, its name matches the expected written-state name, and it is written.** Keep it. b is its
  current value. This happens on a re-flash of a used board, and also on any genuine image's first boot on this board,
  including one from a USB stick, which then leaves the index untouched.
- **F3. The index is present and unwritten, with the expected unwritten-state name.** A power cut came between define
  and increment. Do the unsigned first increment.
- **Anything else** (a name that matches neither state: other attributes, another authPolicy or another key set): stop
  with `FLOOR-FOREIGN`. The unit never undefines an index. The owner clears the TPM in the BIOS at the stand, then
  boots again (F1).

### 1.6 Unit steps

**Boot 1** (urban-grow `encrypt()`, before urban-phase-lock.service; nothing is written to /run or any file):

1. Run the preconditions of 1.4. Read the EK name, compute REF_U and REF_I (section 2.3), and set up NV_FLOOR (1.5).
   b is now known.
2. Generate a 32-byte secret in memory with `openssl rand`, base64-encoded as text, and hold it only in a shell variable
   and pipes. Run `luksFormat --type luks2` with `--key-file=-`, so the secret goes into keyslot 0.
3. Add the recovery keyslot (keyslot 1, step-7 mechanism, escrowed to `urban-recovery.enc` on the ESP) before any TPM
   object is written. This keeps the step-10 ordering rule that recovery comes first.
4. Build P's authPolicy with a trial session: PolicyAuthorize(K_unseal name, REF_U), then PolicyPCR(7) at the current
   value. Create P with `tpm2_create` under the owner primary (section 2.5).
5. Build T's authPolicy with a trial session as in 1.3: PolicyPCR 7+11 at the current values, PolicyNV(b+S+1, ult)
   against the live NV_FLOOR, then CommandCode(Unseal). Create T the same way.
6. Write both objects as LUKS2 tokens on keyslot 0 (section 2.6), first T, then P.
7. Open the volume from the in-memory secret. The first boot needs no unseal.
8. Write the root-only state file `/var/lib/urban/etc/tpm/state.json` on the now-open data partition:
   `{ek, base, svn_first: S, seal: "first-release"}`. It is information for keygen and reporting only and is never used
   as authority. Then exit. urban-phase-lock.service extends PCR 11.
9. urban-prepare (after the lock) creates the device key and prints the enrolment code URBAN2, which carries the EK name
   and b (section 3.2). The console shows `DATA: SEALED, WAITING FOR OWNER APPROVAL`.

**Boots 2..k (no approval for S on the ESP):**

1. The initramfs extends PCR 11 with `urban-svn:S`.
2. urban-grow finds no approval file for S, or one that fails. It unseals T: PolicyPCR(7,11), then PolicyNV, then
   CommandCode(Unseal), then `tpm2_unseal`, piped into `cryptsetup open --key-file=-`. The data opens.
3. The phase lock runs. Updates are not installed: install-check refuses any bundle whose SVN has no verified approval
   on the ESP (section 3.1), and the centre does not offer one (`pending_release` is gated).

**First boot with a verified approval for the running S on the ESP:**

1. urban-grow unseals P with the approval: verifysignature, PolicyPCR(11), PolicyNV, CommandCode(Unseal),
   PolicyAuthorize, PolicyPCR(7), unseal. This is the session of tpm-policy.sh:144-164, unchanged.
2. Only after P has opened the volume, urban-grow removes the T token (`cryptsetup token remove --token-id <T>`) and
   sets `seal: "approved"` in the state file. A power cut before the removal is harmless: the next boot opens with P
   again and removes T then.
3. urban-health, after it marks the slot good, raises NV_FLOOR to b+S under the increment approval (while floor < b+S),
   as design.md step 6 describes. T still satisfies `floor < b+S+1` here and is no more permissive than P for S.
4. If the first approved release is a newer S' instead (the unit was provisioned on S, the centre now runs S'), the
   update installs with the approval for S'. P opens S', urban-health raises the floor to b+S', and T is refused from
   then on at PolicyNV as well as at PolicyPCR(11). T's token is removed at that boot.

**Recovery re-seal** (after DATA LOCKED and the recovery key typed by a person):

- **NV_FLOOR still written** (PCR 7 changed: firmware, Secure Boot or db/dbx change). Generate a new secret. Replace
  keyslot 0, authorised by the recovery key (tpm-policy.sh:262-263). Seal a new P under the current PCR 7. No T is
  made. The existing approvals (same EK, same b) open P on the next boot.
- **NV_FLOOR absent** (a TPM clear). Apply F1, which performs the unsigned first increment and gives a new base b2.
  Make a new P and a T for the running S and b2. The unit reports b2 as a base change, which the centre holds as
  pending until an admin confirms it (section 3.2). The owner signs again for b2 (section 3.4, `--accept-base`).

### 1.7 Owner steps

Once per fleet:

1. On the offline machine: `node os/approve.mjs keys --out <offline dir>`. Copy only `unseal.pem` and `increment.pem`
   to the build machine's `data/os/keys/approve/` (section 2.1).
2. Build the step-11 image. The build installs the two public keys and their TPM names into
   `/usr/share/urban/approve/`.
3. Enrol the new Secure Boot material in each board's BIOS before the unit's first boot: PK/KEK unchanged, db = db2
   certificate, dbx = the old URBAN db certificate. This is the step 6/9 stand procedure with the new files.

Per new unit:

1. Write the per-unit image with `node os/unit-image.mjs` (no GRUB password: section 2.1) and power the unit on at the
   stand.
2. Read the console: device-key fingerprint, EK fingerprint (the first 16 hex characters of SHA-256 over the EK name),
   base b, and the state `WAITING FOR OWNER APPROVAL`.
3. Enrol the unit at the centre with the URBAN2 code. The admin compares the device-key fingerprint and the EK
   fingerprint shown by the centre with the console. The centre refuses an EK that is already registered to another
   device.
4. At the centre: `urban-center release request <S> --devices <id>` writes `request-<S>.json` (section 3.3). Carry it
   to the offline machine.
5. Offline: `node os/approve.mjs sign request-<S>.json --keys <offline dir>` writes `approvals-<S>.json` and updates the
   ledger (section 3.4). Carry it back.
6. At the centre: `urban-center release approve approvals-<S>.json`. The centre checks the approval against the owner
   public keys and its registry, stores it, and pushes it to the unit. The unit's root helper verifies it and puts it on
   the ESP. At the unit's next boot the console shows `DATA: APPROVED`.

Per release: steps 4-6 for every unit (one request file can list them all), before the centre offers the bundle. The
centre offers a release only to units it holds a stored approval for.

---

## 2. Keys and TPM objects

### 2.1 Owner key files

On the build machine, under `data/os/keys/` (public material only, except where noted):

| Path | Content | Goes into the image as |
|---|---|---|
| `approve/unseal.pem` | RSA-2048 public key (SubjectPublicKeyInfo PEM), **K_unseal** | `/usr/share/urban/approve/unseal.pem` |
| `approve/increment.pem` | RSA-2048 public key, **K_increment** | `/usr/share/urban/approve/increment.pem` |
| (made by the build) | TPM names of the two keys, 34 bytes each, computed by `backend/urban/tpm_policy.py` | `/usr/share/urban/approve/{unseal,increment}.name` |
| `sb/db2.{key,crt,cer,esl}` | the new Secure Boot db certificate (CN `URBAN Secure Boot db 2`), same lost-half rule as `sb/db.*` (keys.sh:35-61) | `BOOTX64.EFI` is signed with it; `db2.cer` goes to `/usr/share/urban/sb/db2.cer` |
| `sb/dbx.esl` | an X509 EFI_SIGNATURE_LIST holding the old `sb/db.crt`, for BIOS enrolment | `old-db.cer` goes to `/usr/share/urban/sb/old-db.cer` (precondition check, 1.4) |
| `boot-gpg-2/`, `boot-2.pub.gpg` | the second-generation GPG boot key (lost-half rule as keys.sh:65-93) | `grub-mkimage --pubkey`; signs kernel, initrd and slot files |
| `grub-password` | **removed** (GRUB gets `superusers` with no `password_pbkdf2`) | nothing |

The private halves `unseal.key` and `increment.key` (RSA-2048, PKCS#8 PEM) and `ledger.json` exist **only in the owner's
offline directory**, made by `approve.mjs keys`. They are never in `data/os/keys`. build.sh and check-keys-backup.mjs fail
if `data/os/keys/approve/*.key` exists. This is stricter than the escrow key (keys.sh:100-127): approval keys are used
for every release, and a leak of K_unseal reopens rollback (design.md:293-297).

**Test key set.** The QEMU gates (G4-G7) must sign approvals without the production private keys. The harness keeps a
separate pair in `data/os/test-keys/approve/{unseal,increment}.{key,pem}`, made on first use. Test images are built with
`--approve-keys test`, which embeds the test public keys and writes `APPROVE_KEYS=test` into `/etc/urban-os-release`.
urban-console shows `TEST APPROVAL KEYS` on such a unit. bundle-hook install-check refuses a bundle whose `APPROVE_KEYS`
fingerprint differs from the running image's, so a test image and a production unit never cross. check-image (G3) asserts
that the release images in `data/os/release` carry the production fingerprints. The cost is a separate test build. The
alternative, a test private key in `data/os/keys`, would put a signing key for real units on a networked machine.

### 2.2 Which key signs what

| Key | Signs | Checked by |
|---|---|---|
| K_unseal (offline) | unseal approvals: `aHash = SHA-256(approved_unseal(S) ‖ REF_U)`, RSASSA-PKCS1-v1_5 / SHA-256 | the unit's TPM (`tpm2_verifysignature` ticket, then `PolicyAuthorize`); pre-checked by the centre and by `urban-unit apply-approvals` |
| K_increment (offline) | increment approvals: `SHA-256(approved_inc(b+S) ‖ REF_I)` | the unit's TPM when urban-health raises NV_FLOOR |
| (none) | the first NV_FLOOR increment: unsigned branch B, valid only while the index is unwritten (2.4) | the TPM (`PolicyNvWritten(NO)`) |
| escrow key (offline, step 7) | nothing; it decrypts `urban-recovery.enc` | `os/recovery.mjs` |
| db2 (build machine) | `BOOTX64.EFI` | the firmware; recorded in PCR 7 |
| GPG boot key gen 2 (build machine) | kernel, initrd (which carries `/etc/urban-svn`), `slot-<X>.cfg`, `slot-<X>.sha256` | GRUB with `check_signatures=enforce` |
| RAUC `signing.key` (build machine) | bundles | RAUC (keyring `ca.pem`) |
| device key (per unit, in the TPM) | enrolment and health reports, including the `tpm` part (3.2) | the centre |

The centre holds only the two `.pem` public keys. It can verify approvals, but it cannot make them.

### 2.3 policyRef (per unit, 32 bytes)

```
REF_U = SHA-256( ASCII("urban-unseal:"    + hex_lower(EK name)) )      -- 32 bytes, passed as TPM2B_NONCE
REF_I = SHA-256( ASCII("urban-increment:" + hex_lower(EK name)) )
EK name = 0x000b || SHA-256(TPMT_PUBLIC of the RSA-2048 EK, default template of `tpm2_createek -G rsa`)
```

This is byte-exact with tpm-policy.sh:51-55: `od -tx1` hex of the name, a `:` separator, `printf '%s:%s'`. The EK is
always created transient with `tpm2_createek -G rsa`. A persistent EK at 0x81010001, if the board has one, is neither
used nor removed. The EK certificate is not verified in step 11. EK honesty comes from the human-checked enrolment
(3.2), and the owner ledger refuses an EK that is already bound to another device (3.4).

### 2.4 NV_FLOOR

- **Handle:** `0x01800010` (owner range), fixed.
- **Size:** 8 bytes, counter, big-endian value.
- **Attributes:** `policywrite|authread|ownerread|nt=counter|no_da`, with no `ownerwrite` and no `authwrite`.
  As TPMA_NV that is `0x02060018` before the first write and `0x22060018` after it (WRITTEN, bit 29). Both values are
  asserted against `tpm2_nvreadpublic` in G1.
- **authPolicy (changed from step 10: a PolicyOR with an unsigned init branch):**

  ```
  dA = PolicyAuthorize(name(K_increment), REF_I)                               -- signed raises
  dB = PolicyCommandCode(TPM2_CC_NV_Increment) ; PolicyNvWritten(NO)           -- first increment only
  NV_FLOOR.authPolicy = PolicyOR(dA, dB)
  ```

  Signed raise session: CommandCode(NV_Increment), then PolicyNV(floor ult b+S), then PolicyAuthorize(approved, REF_I,
  ticket), which gives dA; then PolicyOR(dA, dB); then `tpm2_nvincrement`.
  Init session: CommandCode(NV_Increment), then PolicyNvWritten(NO), which gives dB; then PolicyOR(dA, dB); then
  `tpm2_nvincrement`.

  Why the unsigned branch adds no attack surface: the signed init approval of step 10 has no operand, and scenario 7
  shows that anyone with the (empty) owner auth can undefine and redefine the index and replay it. Its signature
  therefore proved nothing against an attacker. The first write of a counter index takes the TPM's retained maxCounter,
  so an init can never bring the floor below any earlier value. Once the index is written, branch B fails at
  PolicyNvWritten. A redefinition with any other policy or attributes gives another NV name, and every approval and T
  then fail at PolicyNV (denial of service only). G1 must prove: init via B on an unwritten index; B refused once
  written; undefine and redefine does not lower the floor; a signed raise via dA plus PolicyOR works.

- **NV name** (the signer must rebuild it; the PolicyNV digest contains it):

  ```
  name = 0x000b || SHA-256( UINT32 0x01800010 || UINT16 0x000b || UINT32 attrs(written) ||
                            UINT16 0x0020 || authPolicy(32) || UINT16 0x0008 )
  ```

  It is per unit, because authPolicy contains REF_I. Approvals always use the written-state name.

- **Who writes it:** urban-grow (branch B, first boot or after a TPM clear, before the lock), and urban-health (branch
  A, after the slot is marked good, while floor < b+S). Nothing else writes it. Nothing ever undefines it.

### 2.5 Sealed objects P and T

- **Parent:** the deterministic owner-hierarchy primary `tpm2_createprimary -C o -g sha256 -G ecc`, re-created
  transient at each use (tpm-policy.sh:128). Owner auth stays empty, because signer.py:38 needs the same.
- **Attributes (both):** `fixedtpm|fixedparent|adminwithpolicy|noda`, with no `userwithauth`, so only the policy opens
  them. `noda` keeps policy failures from locking the TPM out.
- **Payload:** the LUKS keyslot-0 passphrase, 32 random bytes as base64 (44 bytes, under the 128-byte sealed-data limit).
- **P.authPolicy:** `PolicyAuthorize(name(K_unseal), REF_U) ; PolicyPCR(sha256:7 = value at seal)`. It is fixed for
  the life of the seal and does not depend on release or base.
- **T.authPolicy:** `PolicyPCR(sha256:7,11 = values at seal, PCR 11 = extend-from-zero("urban-svn:S")) ;
  PolicyNV(NV_FLOOR, b+S+1, ult) ; PolicyCommandCode(TPM2_CC_Unseal)`.

### 2.6 Where the objects are stored: LUKS2 tokens in the data volume's header

Both blobs are stored as custom LUKS2 tokens on keyslot 0, written with `cryptsetup token import --token-id N` and read
with `cryptsetup token export`. They are not stored on the ESP, on the data partition's file system, or in NV. Reasons:

- They must be readable **before** the volume is open, which rules out a file inside the data partition.
- They belong to the volume they open. A re-flash, a new disk or a new LUKS header replaces them together with the
  keyslot, so a blob can never point at another volume's keyslot.
- The ESP is unsigned FAT that every re-flash and every update helper rewrites, and it is the file system a foreign stick
  replaces. Approvals can live there because the TPM checks them. Blobs need no secrecy (`fixedtpm`), but keeping them
  with their volume removes a class of mismatch failures.
- NV space is scarce, NV has no tie to the volume, and an NV blob would outlive a re-flash.

Token JSON (cryptsetup requires `type` and `keyslots`; the other fields are ours):

```json
{ "type": "urban-tpm2", "keyslots": ["0"], "urban-format": 1,
  "role": "approved",                 // "approved" = P, "first-release" = T
  "ek": "000b<64 hex>",
  "pcr7": "<64 hex>",                 // PCR 7 at seal (diagnostics: urban-console says "PCR 7 changed")
  "svn": 7, "base": 4711,             // T only: S and b it was sealed for (diagnostics only, never authority)
  "pub": "<base64 TPM2B_PUBLIC>", "priv": "<base64 TPM2B_PRIVATE>" }
```

The recovery keyslot 1 keeps its step-7 token. LUKS2's default 16 KiB metadata area fits all three tokens. G3/G4 assert
`luksDump` shows exactly one `urban-tpm2` token of role `approved` on an approved unit, and none of type `systemd-tpm2`.
The checks that hard-code the step-7 strings (vm.py:745, vm_pcr7.py:88-100) change with this.

### 2.7 The per-unit base b

- **Made by:** the TPM, at the unsigned first increment (or, on a used board, NV_FLOOR's current value: rule F2).
- **Recorded on the unit:** in T's token and in `/var/lib/urban/etc/tpm/state.json`, for display and reporting only.
  No unit code builds or accepts authority from it. The helper uses it to reject approvals that do not fit (a
  convenience check). The TPM's PolicyNV is the authority.
- **Recorded at the centre:** in the device registry record (`data/pki/devices.json`, registry.py), as new optional
  fields:
  - `ek_name`: 68 hex characters.
  - `tpm_base`: an int, set once from the URBAN2 enrolment code.
  - `tpm_base_pending`: an int, set when a health report shows another base (after a TPM clear). It is applied only by
    an admin's confirmation (`provision.py tpm-confirm <device>` or an audited UI route, like wg-accept), and audited as
    `tpm-base-confirmed`.
  - All three are forgotten when the device key changes (re-enrolment), like `_forget_tunnel_key` (registry.py:372).
- The registry field is named `tpm_base`, and the TPM index is called "the TPM floor" in text. release.py's existing
  `floor` is the centre's minimum commanded version, a different thing.
- The base also appears inside every stored approval (3.1), so the approval files and the registry can be cross-checked.
  The owner's ledger pins it independently (3.4).

---

## 3. File formats and the owner CLI

### 3.1 The approval file (one per unit per release)

- **On the unit:** `/boot/efi/EFI/URBAN/approvals/<S>.json` on the ESP, i.e. `\EFI\URBAN\approvals\<S>.json`. It is
  written by root (`urban-unit apply-approvals` or `apply-update`) as tmp file, `rename`, then read back. It is read by
  urban-grow before the unlock (esp_mount ro, urban-grow:197-204) and by urban-health after it.
- **At the centre:** `<releases dir>/approvals/<S>/<device>.json`, with the same content.
- **Encoding:** UTF-8 JSON, at most 4 KiB, exactly these keys, no others. Hex is lower-case and fixed-length.

```json
{
  "format": "urban-approval/1",
  "device": "edge-J-07",
  "ek": "000b<64 hex>",
  "svn": 7,
  "base": 4711,
  "floor_name": "000b<64 hex>",          // NV_FLOOR written-state name for this EK (2.4)
  "pcr11": "<64 hex>",                   // extend-from-zero("urban-svn:7")
  "unseal": {
    "key": "<64 hex>",                   // SHA-256 of K_unseal's SubjectPublicKeyInfo DER
    "ref": "<64 hex>",                   // REF_U
    "operand": "<16 hex>",               // b+S+1, 8-byte big-endian
    "policy": "<64 hex>",                // approved digest: PolicyPCR(11) ; PolicyNV(ult operand) ; CC(Unseal)
    "sig": "<base64, 256 bytes>"         // RSASSA-PKCS1-v1_5 SHA-256 over policy(32) || ref(32)
  },
  "increment": {
    "key": "<64 hex>", "ref": "<64 hex>",// REF_I
    "operand": "<16 hex>",               // b+S
    "policy": "<64 hex>",                // CC(NV_Increment) ; PolicyNV(ult operand)
    "sig": "<base64, 256 bytes>"
  }
}
```

The unit's helper accepts a file only when all of the following hold. It reports refusals with fixed reason words
(`approval-format`, `approval-not-this-unit`, `approval-base`, `approval-digest`, `approval-signature`, `approval-keys`):

1. The JSON is strict, within the size limit, not a symlink, and has exactly the listed keys.
2. `ek` equals the unit's EK name, and `ref` values equal REF_U and REF_I computed from it.
3. `base` equals the unit's recorded base, `operand`s equal `base+svn+1` and `base+svn`, and `floor_name` equals the
   live `tpm2_nvreadpublic` name.
4. `pcr11` and both `policy` digests recompute from (`svn`, operands, `floor_name`) with the image's own
   `tpm_policy` code.
5. `key` fingerprints equal the image's keys.
6. `tpm2_verifysignature` with the loaded key succeeds for both parts.

Only then is the file written to the ESP. The ESP copy stays untrusted. urban-grow and urban-health rerun steps 2 and
4 and let the TPM decide. Tampering with the ESP copy can only deny service.

**Retention:** keep the approvals for the running SVN, for the other slot's SVN, and for anything newer. The helper
deletes files whose `svn + base + 1 <= floor`, which can never open again.

**Delivery from the centre:**

- **With an update:** the offer body gains `"approval": <the object above>` (hub.py:334). update.py `valid_offer`
  accepts and strictly checks it, and writes it as `urban-<S>.approval.json` beside the bundle. `request.json` gains
  `"approval": "urban-<S>.approval.json"`. `urban-unit apply-update` validates it and writes it to the ESP **before**
  `rauc install`. install-check refuses the bundle when `approvals/<RAUC_MF_VERSION>.json` is missing or fails.
- **Without an update** (a new unit's running S, or a re-approval after a base change): a device command whose body is
  exactly `{"approval": <object>}`, sent through `_deliver` outside the version floor, like `_wg_push`. The unit service
  writes it to `/var/lib/urban/approval-inbox/<S>.json`. `urban-approval.path` then runs `urban-unit apply-approvals`
  as root.

### 3.2 The unit's EK report to the centre

**(a) Enrolment code, URBAN2** (printed by keygen, run as root from urban-prepare:202, read from the root-written
`state.json`):

```
URBAN2:<device>:<serial>:<public>:<ek 68 hex>:<base decimal>:<check>
check = SHA-256("URBAN2:<device>:<serial>:<public>:<ek>:<base>")[:6], upper case    (as enrolment.py:64-66)
```

`parse_code` still accepts URBAN1 (5 parts, `ek=None`). A device enrolled with URBAN1 never gets approvals: it is a
pre-step-11 unit and is re-flashed. The kit carries `ek`, and `install_kit` refuses a kit made for another EK, as it
does for another fingerprint. The centre refuses an `ek_name` already held by another device record. The console and
the centre's Devices page both show `EK <first 16 hex of SHA-256(ek name)>` for the admin's comparison.

**(b) Health report `tpm` part** (signed with the device key like the rest of health). It is read from the root-written
`/run/urban/tpm.json` and whitelisted in `health.clean()`:

```json
"tpm": { "ek": "000b<64 hex>", "base": 4711, "floor": 4718, "svn": 7,
         "seal": "first-release" | "approved" | "recovered",
         "approved": [7, 8] }                  // SVNs with a verified approval on the ESP, at most 8 entries
```

Centre handling (`hub._tpm_report`, after `_wg_report`):

- `ek` different from the registry: audit `tpm-ek-mismatch`, raise an alert, push no approvals.
- `base` different: set `tpm_base_pending` and wait for an admin.
- `floor` and `seal` are displayed, never trusted. There is no TPM quote in step 11, and the docs say so.

### 3.3 The approval request (centre to owner)

`urban-center release request <S> [--devices a,b,...]` writes `request-<S>.json`. Without `--devices`, it lists every
device that has `ek_name` and `tpm_base`, is offered or will be offered release S, and has no stored approval for S. It
also lists each listed unit whose `seal` is `first-release` together with its running SVN.

```json
{ "format": "urban-approval-request/1", "made": "2026-10-23T12:00:00Z",
  "keys": { "unseal": "<64 hex>", "increment": "<64 hex>" },
  "units": [ { "device": "edge-J-07", "ek": "000b<64 hex>", "base": 4711, "svn": 7 } ] }
```

The request carries no digests. The signer recomputes everything from `ek`, `base` and `svn`.

### 3.4 The owner CLI: `os/approve.mjs` (offline machine)

It runs `os/approve.py` inside the builder container, like `os/recovery.mjs` → `recovery.sh`. The builder image has
python3 and openssl and needs no tpm2-tools. Digests come from the pure-Python module `backend/urban/tpm_policy.py`
(mounted at `/work/backend`). The same module is used by the centre's verifier, by build.sh (for the `.name` files) and
by the unit's helper. Its output must equal TPM trial sessions byte for byte, which G1 asserts in the urban-tpm-test
image. It reads only the files it is given, writes only the named outputs and the ledger, and uses no network.

```
node os/approve.mjs keys --out <dir>
    makes unseal.key/.pem and increment.key/.pem (RSA-2048), and an empty ledger.json; refuses to overwrite any of
    them; prints both key fingerprints and TPM names. Copy only the two .pem files to the build machine.

node os/approve.mjs sign <request-S.json> --keys <dir> [--out approvals-S.json] [--accept-base <device>=<base> ...]
    in:  the request, <dir>/{unseal,increment}.key, <dir>/ledger.json
    out: approvals-S.json  { "format": "urban-approvals/1", "approvals": [ <approval object, 3.1>, ... ] },
         and the updated ledger.json (written last, tmp + rename)
    prints one line per unit: device, EK fingerprint, base, svn, "signed" or a refusal word
    exit 0 when every unit was signed; 1 when any was refused (the others are still written)

node os/approve.mjs show <request-or-approvals.json> [--keys <dir>]
    prints the content; with --keys it verifies every signature and recomputes every digest. Writes nothing.
```

Refusals during `sign`, each a fixed word:

- `keys-differ`: the request's key fingerprints are not the keys in `<dir>`.
- `ek-taken`: the EK is in the ledger under another device.
- `base-changed`: the base differs from the ledger's for this EK and no `--accept-base` names it.
- `svn-older`: the svn is below the highest svn already signed for this EK. An older version signed with a raised base
  would be a rollback.
- `bad-request`: a format error.

`ledger.json`, kept offline beside the private keys:

```json
{ "<ek 68 hex>": { "device": "edge-J-07", "base": 4711, "max_svn": 7,
                   "history": [ { "svn": 7, "base": 4711, "at": "2026-10-23T12:05:00Z" } ] } }
```

The ledger is the owner's own check on a compromised or mistaken centre. A centre that supplies an inflated base or an
old svn cannot get an approval that reopens rollback. `--accept-base` is for a TPM clear or a re-flash of a used board.
It must be given per device, and the history records it.

**Back at the centre:** `urban-center release approve approvals-S.json` (staged as root like `release add`,
urban-center:22-49). For each approval it checks the format, that `device`/`ek`/`base` equal the registry, that
`key` fingerprints equal the centre's `.pem` keys, the recomputed digests, and the RSA signatures with the public keys.
It stores the approval under `approvals/<S>/<device>.json`, audits `release-approved`, and pushes it to the unit (3.1).
`pending_release` offers release S only to devices with a stored approval for S. The Devices row shows `approval
missing for S` otherwise.
