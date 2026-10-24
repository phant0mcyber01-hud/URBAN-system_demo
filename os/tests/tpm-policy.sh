#!/bin/bash
# URBAN measured-boot TPM mechanism (revised after review), on swtpm with tpm2-tools (bookworm).
set -u
export LC_ALL=C
WORK=/run/tpmtest; STATE=$WORK/state
export TPM2TOOLS_TCTI="swtpm:host=127.0.0.1,port=2321"
SWPID=""
PCR_SB=7; PCR_SVN=11
FLOOR=0x01800010
rm -rf "$WORK"; mkdir -p "$WORK" "$STATE"
fails=0
pass(){ echo "ok   $*"; }
bad(){ echo "BAD  $*"; fails=$((fails+1)); }

h2b(){ python3 -c "import sys;sys.stdout.buffer.write(bytes.fromhex(sys.argv[1].strip()))" "$1"; }
flushall(){ tpm2_flushcontext -t >/dev/null 2>&1; tpm2_flushcontext -l >/dev/null 2>&1; tpm2_flushcontext -s >/dev/null 2>&1; }
port_open(){ (exec 3<>/dev/tcp/127.0.0.1/2321) 2>/dev/null && { exec 3>&- 3<&-; return 0; }; return 1; }
swtpm_start(){
  if [ -n "$SWPID" ]; then kill "$SWPID" 2>/dev/null; wait "$SWPID" 2>/dev/null; SWPID=""; fi
  for _ in $(seq 1 100); do port_open || break; sleep 0.1; done
  if port_open; then echo "FAIL: TPM port still open"; exit 1; fi
  swtpm socket --tpm2 --tpmstate dir="$STATE" --ctrl type=tcp,port=2322 --server type=tcp,port=2321 --flags startup-clear >/dev/null 2>&1 &
  SWPID=$!
  for _ in $(seq 1 50); do tpm2_getcap properties-fixed >/dev/null 2>&1 && return 0; sleep 0.1; done
  echo FAIL swtpm; exit 1; }
reboot_tpm(){ swtpm_start; }

# a boot: firmware extends PCR 7 with the Secure Boot state AND the db cert that let the loader run ($1=v1|v2);
# the signed initramfs extends PCR 11 with the release SVN ($2). No PCR 12 - the installation is held by the phase lock.
measure(){ printf 'sb-urban+dbcert:%s' "$1" | tpm2_pcrevent "$PCR_SB" >/dev/null
  printf 'urban-svn:%s' "$2" | tpm2_pcrevent "$PCR_SVN" >/dev/null; }
phase_lock(){ printf 'urban-phase:data-unlocked' | tpm2_pcrevent "$PCR_SVN" >/dev/null; }  # after the unlock stage

rd(){ tpm2_nvread "$FLOOR" 2>/dev/null > "$WORK/fl"; python3 -c "print(int.from_bytes(open('$WORK/fl','rb').read(),'big'))"; }
operand(){ python3 -c "import sys;sys.stdout.buffer.write(int(sys.argv[1]).to_bytes(8,'big'))" "$1"; }
predict11(){ tpm2_pcrreset 23 >/dev/null 2>&1; printf 'urban-svn:%s' "$1" | tpm2_pcrevent 23 >/dev/null
  tpm2_pcrread "sha256:23" | sed -n 's/.*0x//p' | tr -d '\n '; }
# owner signs aHash = H(approvedPolicy || policyRef): message is approved||ref
osign(){ cat "$1" "$2" > "$WORK/msg"; openssl dgst -sha256 -sign "$WORK/owner.pem" -out "$3" "$WORK/msg" 2>/dev/null; }
loadowner(){ tpm2_loadexternal -C o -G rsa -u "$WORK/owner.pub" -c "$WORK/owner.ctx" -n "$WORK/owner.name" >/dev/null 2>&1; }

REF_U="$WORK/ref-unseal"; REF_I="$WORK/ref-increment"
N=5; NP1=6

echo "== setup: installation X, release N=$N, Secure Boot db cert v2 =="
swtpm_start
# approvals are PER UNIT: the policyRef binds them to this TPM's endorsement key name (read by the centre at
# provisioning), so an approval made for another unit (another base) cannot authorise anything here
tpm2_createek -c "$WORK/ek.ctx" -G rsa -u "$WORK/ek.pub" >/dev/null 2>&1
tpm2_readpublic -c "$WORK/ek.ctx" -n "$WORK/ek.name" >/dev/null 2>&1; tpm2_flushcontext -t >/dev/null 2>&1
EKNAME=$(od -An -tx1 "$WORK/ek.name" | tr -d ' \n')
[ ${#EKNAME} -ge 64 ] || { echo "FAIL: could not read the EK name"; exit 1; }
# policyRef is a TPM2B_NONCE (at most one digest, 32 bytes): the ref is SHA-256(label || EK name)
mkref(){ printf '%s:%s' "$1" "$2" | openssl dgst -sha256 -binary > "$3"; }
mkref urban-unseal "$EKNAME" "$REF_U"; mkref urban-increment "$EKNAME" "$REF_I"
echo "   approvals bound to this unit's EK name ${EKNAME:0:16}..."
tpm2_dictionarylockout -s -n 1000 -t 1 -l 1 >/dev/null 2>&1
echo "tpm2-tools $(tpm2_startup --version | sed 's/.*version="//; s/".*//'), swtpm $(swtpm --version 2>/dev/null | sed 's/.*version //; s/,.*//'), cryptsetup $(cryptsetup --version | awk '{print $2}')"

openssl genrsa -out "$WORK/owner.pem" 2048 2>/dev/null
openssl rsa -in "$WORK/owner.pem" -pubout -out "$WORK/owner.pub" 2>/dev/null
loadowner; flushall

# ---- floor = a CAPPED counter, raised only under owner-signed approvals (M1) ----
tpm2_startauthsession -S "$WORK/t" >/dev/null
tpm2_policyauthorize -S "$WORK/t" -q "$REF_I" -n "$WORK/owner.name" -L "$WORK/floor.authpol" >/dev/null
tpm2_flushcontext "$WORK/t" >/dev/null
tpm2_nvdefine -C o -s 8 -L "$WORK/floor.authpol" -a "policywrite|authread|ownerread|nt=counter|no_da" "$FLOOR" >/dev/null
pass "floor is a capped counter: policywrite only (no empty-auth increment), authPolicy=PolicyAuthorize(owner,ref=increment)"

# init increment approval (sets the per-unit base b): CC=NV_Increment AND PolicyNvWritten(clear)
tpm2_startauthsession -S "$WORK/t" >/dev/null
tpm2_policycommandcode -S "$WORK/t" TPM2_CC_NV_Increment >/dev/null
tpm2_policynvwritten -S "$WORK/t" c -L "$WORK/ap-init" >/dev/null
tpm2_flushcontext "$WORK/t" >/dev/null
osign "$WORK/ap-init" "$REF_I" "$WORK/sg-init"

# increment approval for target T: CC=NV_Increment AND PolicyNV(floor < T)  (raises floor toward T, stops at T)
approve_inc(){ local T=$1; operand "$T" > "$WORK/opc-$T"
  tpm2_startauthsession -S "$WORK/t" >/dev/null
  tpm2_policycommandcode -S "$WORK/t" TPM2_CC_NV_Increment >/dev/null
  tpm2_policynv -S "$WORK/t" -i "$WORK/opc-$T" "$FLOOR" ult -L "$WORK/ap-inc-$T" >/dev/null
  tpm2_flushcontext "$WORK/t" >/dev/null
  osign "$WORK/ap-inc-$T" "$REF_I" "$WORK/sg-inc-$T"; }

# do one increment under an approval ($1 approved digest, $2 sig, $3 the pre-step to rebuild the session digest)
inc_once(){ local ap=$1 sg=$2 pre=$3 r
  flushall; loadowner
  cat "$ap" "$REF_I" > "$WORK/vmsg"
  tpm2_verifysignature -c "$WORK/owner.ctx" -g sha256 -m "$WORK/vmsg" -s "$sg" -t "$WORK/tk" -f rsassa >/dev/null 2>&1 || { echo "VERIFY-FAIL"; return 1; }
  tpm2_flushcontext -t >/dev/null 2>&1
  tpm2_startauthsession --policy-session -S "$WORK/p" >/dev/null
  tpm2_policycommandcode -S "$WORK/p" TPM2_CC_NV_Increment >/dev/null
  eval "$pre"
  tpm2_policyauthorize -S "$WORK/p" -i "$ap" -q "$REF_I" -n "$WORK/owner.name" -t "$WORK/tk" >/dev/null 2>&1
  if tpm2_nvincrement -C "$FLOOR" -P session:"$WORK/p" "$FLOOR" >/dev/null 2>"$WORK/e"; then r=ok; else r="REFUSED($(grep -o 'tpm:.*' "$WORK/e" | head -1))"; fi
  tpm2_flushcontext "$WORK/p" >/dev/null 2>&1; echo "$r"; }

# the first, base-setting increment; base b is read afterwards (a used fTPM may start high)
r=$(inc_once "$WORK/ap-init" "$WORK/sg-init" 'tpm2_policynvwritten -S "$WORK/p" c >/dev/null 2>&1')
[ "$r" = ok ] && pass "first (base-setting) increment under the signed init approval" || bad "init increment: $r"
BASE=$(rd)
echo "   per-unit base b = $BASE (recorded at provisioning)"
raise_floor_to(){ local T=$1 g; approve_inc "$T"
  while [ "$(rd)" -lt "$T" ]; do g=$(inc_once "$WORK/ap-inc-$T" "$WORK/sg-inc-$T" "tpm2_policynv -S \"\$WORK/p\" -i \"\$WORK/opc-$T\" $FLOOR ult >/dev/null 2>&1"); [ "$g" = ok ] || { echo "   raise stuck: $g"; break; }; done; }
raise_floor_to $((BASE+N))
echo "   floor raised to $(rd) = b+N (release N marked good)"

# ---- the sealed data key object ----
# approved unseal(S) = PolicyPCR(11 = SVN S) AND PolicyNV(floor < b+S+1) AND PolicyCommandCode(Unseal), ref=unseal
approve_unseal(){ local S=$1; predict11 "$S" | { read -r hx; h2b "$hx"; } > "$WORK/p11-$S"; operand $((BASE+S+1)) > "$WORK/opu-$S"
  tpm2_startauthsession -S "$WORK/t" >/dev/null
  tpm2_policypcr -S "$WORK/t" -l "sha256:$PCR_SVN" -f "$WORK/p11-$S" >/dev/null
  tpm2_policynv -S "$WORK/t" -i "$WORK/opu-$S" "$FLOOR" ult >/dev/null
  tpm2_policycommandcode -S "$WORK/t" TPM2_CC_Unseal -L "$WORK/approved-$S" >/dev/null
  tpm2_flushcontext "$WORK/t" >/dev/null
  osign "$WORK/approved-$S" "$REF_U" "$WORK/sig-$S"; }
approve_unseal "$N"; approve_unseal "$NP1"
pass "owner signed the unseal approvals for N and N+1 (distinct ref from the increment approvals)"

# object authPolicy = PolicyAuthorize(owner, ref=unseal) THEN PolicyPCR(PCR 7)
build_authpol(){ tpm2_startauthsession -S "$WORK/t" >/dev/null
  tpm2_policyauthorize -S "$WORK/t" -q "$REF_U" -n "$WORK/owner.name" >/dev/null
  tpm2_policypcr -S "$WORK/t" -l "sha256:$PCR_SB" -L "$WORK/authpolicy" >/dev/null
  tpm2_flushcontext "$WORK/t" >/dev/null; }
# PCR 7 must hold the enrol-time value when we build authPolicy: this is the first boot (db cert v2, release N)
reboot_tpm; measure v2 "$N"; loadowner; build_authpol
load_primary(){ flushall; tpm2_createprimary -C o -g sha256 -G ecc -c "$WORK/prim.ctx" >/dev/null 2>&1; }
load_primary
SECRET="URBAN-DATA-KEY-$(openssl rand -hex 8)"
printf '%s' "$SECRET" | tpm2_create -C "$WORK/prim.ctx" -u "$WORK/seal.pub" -r "$WORK/seal.priv" \
  -L "$WORK/authpolicy" -i - -a "fixedtpm|fixedparent|adminwithpolicy|noda" >/dev/null
pass "sealed the data key under PolicyAuthorize(owner)+PolicyNV(floor)+PolicyCommandCode(Unseal)+PolicyPCR(7)"

# ---- the data volume: a real LUKS2 file, TPM secret in keyslot 0, recovery passphrase in keyslot 1 (TPM-independent) ----
RECKEY="urban-recovery-$(openssl rand -hex 8)"
truncate -s 32M "$WORK/data.luks"
printf '%s' "$SECRET"  | cryptsetup luksFormat --type luks2 --batch-mode --pbkdf pbkdf2 --pbkdf-force-iterations 1000 --key-file=- "$WORK/data.luks"
printf '%s' "$SECRET"  | cryptsetup luksAddKey --pbkdf pbkdf2 --pbkdf-force-iterations 1000 --key-file=- "$WORK/data.luks" <(printf '%s' "$RECKEY")
pass "LUKS2 data volume: keyslot 0 = TPM secret, keyslot 1 = recovery passphrase (not touched by the TPM)"
opens(){ printf '%s' "$1" | cryptsetup open --test-passphrase --key-file=- "$WORK/data.luks" >/dev/null 2>&1; }  # no dm, unprivileged

# ---- unseal under the measured policy for release S; surfaces WHICH step refused (nothing hidden) ----
unseal(){ local S=$1 out rc
  flushall
  tpm2_createprimary -C o -g sha256 -G ecc -c "$WORK/prim.ctx" >/dev/null 2>&1
  rm -f "$WORK/seal.ctx"
  if ! tpm2_load -C "$WORK/prim.ctx" -u "$WORK/seal.pub" -r "$WORK/seal.priv" -c "$WORK/seal.ctx" >/dev/null 2>"$WORK/e0"; then
    echo "LOAD-FAIL:$(grep -o 'tpm:.*' "$WORK/e0" | head -1)"; return 7; fi
  tpm2_flushcontext -t >/dev/null 2>&1
  loadowner
  cat "$WORK/approved-$S" "$REF_U" > "$WORK/vmsg"
  if ! tpm2_verifysignature -c "$WORK/owner.ctx" -g sha256 -m "$WORK/vmsg" -s "$WORK/sig-$S" -t "$WORK/tkt" -f rsassa >/dev/null 2>&1; then echo "VERIFY-FAIL"; return 4; fi
  tpm2_flushcontext -t >/dev/null 2>&1
  tpm2_startauthsession --policy-session -S "$WORK/ps" >/dev/null
  tpm2_policypcr -S "$WORK/ps" -l "sha256:$PCR_SVN" >/dev/null 2>"$WORK/e1" || { echo "POLICYPCR11-ERR:$(grep -o 'tpm:.*' "$WORK/e1"|head -1)"; tpm2_flushcontext "$WORK/ps" >/dev/null 2>&1; return 5; }
  if ! tpm2_policynv -S "$WORK/ps" -i "$WORK/opu-$S" "$FLOOR" ult >/dev/null 2>"$WORK/e2"; then echo "POLICYNV-FAIL:$(grep -o 'tpm:.*' "$WORK/e2"|head -1)"; tpm2_flushcontext "$WORK/ps" >/dev/null 2>&1; return 3; fi
  tpm2_policycommandcode -S "$WORK/ps" TPM2_CC_Unseal >/dev/null 2>&1
  if ! tpm2_policyauthorize -S "$WORK/ps" -i "$WORK/approved-$S" -q "$REF_U" -n "$WORK/owner.name" -t "$WORK/tkt" >/dev/null 2>"$WORK/e3"; then echo "AUTHORIZE-FAIL:$(grep -o 'tpm:.*' "$WORK/e3"|head -1)"; tpm2_flushcontext "$WORK/ps" >/dev/null 2>&1; return 6; fi
  tpm2_policypcr -S "$WORK/ps" -l "sha256:$PCR_SB" >/dev/null 2>&1
  out=$(tpm2_unseal -c "$WORK/seal.ctx" -p session:"$WORK/ps" 2>"$WORK/e4"); rc=$?
  tpm2_flushcontext "$WORK/ps" >/dev/null 2>&1
  if [ $rc -eq 0 ]; then echo "UNSEALED:$out"; else echo "UNSEAL-FAIL:$(grep -o 'tpm:.*' "$WORK/e4"|head -1)"; fi
  return $rc; }
# a full boot: unseal, then open the LUKS volume with the secret, then PHASE-LOCK PCR 11 (the unlock stage is over)
boot_unlock(){ local S=$1 u s; u=$(unseal "$S"); s=${u#UNSEALED:}
  if [ "${u#UNSEALED:}" != "$u" ] && opens "$s"; then phase_lock; echo "OPENED"; else echo "$u"; fi; }

echo
echo "== 1. CONTROL: release N on installation X (db v2, floor=b+N) opens the data =="
reboot_tpm; measure v2 "$N"; r=$(boot_unlock "$N"); echo "   $r"
[ "$r" = OPENED ] && pass "N opens the LUKS volume (unseal -> keyslot 0)" || bad "N should open, got $r"

echo
echo "== 2. ORDINARY UPDATE to N+1 before the floor is raised: opens without the recovery key =="
reboot_tpm; measure v2 "$NP1"; r=$(boot_unlock "$NP1"); echo "   floor still $(rd): $r"
[ "$r" = OPENED ] && pass "update to N+1 opens with no recovery key (owner-signed policy)" || bad "N+1 should open pre-raise, got $r"

echo
echo "== 3. POWER-CUT ORDERING =="
echo "   3a. cut between authorising N+1 and raising the floor: floor stays b+N, both N and N+1 open"
reboot_tpm; measure v2 "$N";  r=$(boot_unlock "$N");  echo "      fall back to N:  $r"; [ "$r" = OPENED ] && pass "3a N still opens" || bad "3a N should open"
reboot_tpm; measure v2 "$NP1"; r=$(boot_unlock "$NP1"); echo "      forward to N+1: $r"; [ "$r" = OPENED ] && pass "3a N+1 still opens" || bad "3a N+1 should open"
echo "   3b. cut between raising the floor and marking the other slot bad"
raise_floor_to $((BASE+NP1)); echo "      floor raised to $(rd) = b+N+1 (N+1 marked good)"
# the raise is 'while floor < b+SVN': running it again is a no-op, so a power cut during 3b is simply redone
raise_floor_to $((BASE+NP1)); echo "      re-run of the raise after a cut is a no-op, floor still $(rd)"
reboot_tpm; measure v2 "$NP1"; r=$(boot_unlock "$NP1"); echo "      N+1 still opens: $r"; [ "$r" = OPENED ] && pass "3b N+1 opens after the raise" || bad "3b N+1 should open"

echo
echo "== 4. ROLLBACK to N after the floor is b+N+1 is REFUSED (PolicyNV), control N+1 opens =="
reboot_tpm; measure v2 "$N"; r=$(unseal "$N"); echo "   boot old release N: $r"
case "$r" in POLICYNV-FAIL*) pass "rollback to N refused at PolicyNV (floor)";; *) bad "rollback N should fail at PolicyNV, got $r";; esac
reboot_tpm; measure v2 "$NP1"; r=$(boot_unlock "$NP1"); echo "   control N+1: $r"; [ "$r" = OPENED ] && pass "N+1 opens (control)" || bad "N+1 control should open"

echo
echo "== 5. OLD LOADER / zero-PCR bypass (A0, B1): PCR 7 has the old db cert; root extends 11 itself -> REFUSED =="
reboot_tpm; printf 'sb-urban+dbcert:v1' | tpm2_pcrevent "$PCR_SB" >/dev/null   # old loader authorised by the OLD db cert
printf 'urban-svn:%s' "$NP1" | tpm2_pcrevent "$PCR_SVN" >/dev/null             # attacker root extends the right SVN
r=$(unseal "$NP1"); echo "   old-db-cert boot, root-extended 11: $r"
case "$r" in UNSEAL-FAIL*) pass "old loader (PCR 7 = old db cert) refused at the final PolicyPCR(7)";; *) bad "old loader should fail at PolicyPCR7, got $r";; esac
reboot_tpm; measure v2 "$NP1"; r=$(unseal "$NP1"); echo "   control new db cert v2: ${r%%:*}"; case "$r" in UNSEALED:*) pass "new db cert v2 unseals (control)";; *) bad "v2 control should unseal, got $r";; esac
# pure zero-PCR boot (firmware/loader never measured; root extends 11 itself): PCR 7 = 0, refused all the same
reboot_tpm; printf 'urban-svn:%s' "$NP1" | tpm2_pcrevent "$PCR_SVN" >/dev/null
r=$(unseal "$NP1"); echo "   zero-PCR boot (PCR 7 never set), root-extended 11: $r"
case "$r" in UNSEAL-FAIL*) pass "zero-PCR boot refused (PCR 7 = 0 != the sealed Secure Boot state)";; *) bad "zero-PCR boot should fail at PolicyPCR7, got $r";; esac

echo
echo "== 6. PHASE LOCK (B2/B3): after the unlock stage, no root may re-unseal =="
reboot_tpm; measure v2 "$NP1"; u=$(unseal "$NP1"); echo "   unlock stage: ${u%%:*}"
phase_lock    # urban-grow extends PCR 11 on exit (trap EXIT / oneshot After=urban-grow Before=sshd,getty)
r=$(unseal "$NP1"); echo "   a root shell obtained afterwards (its own authorized_keys/maintenance.pub): $r"
# PCR 11 is inside the owner-signed approved policy, so the phase marker makes the session digest no longer match
# the signed approved policy: the TPM refuses at PolicyAuthorize, and only there (the one expected tag) - the secret
# is never re-released to a root obtained after the unlock stage.
case "$r" in AUTHORIZE-FAIL*) pass "phase lock: root after the unlock stage refused at PolicyAuthorize (PCR 11 phase marker) - the secret is never re-released";; *) bad "phase lock should refuse at PolicyAuthorize, got $r";; esac
echo "   (the cmdline allowlist and no-GRUB-password keep root from appearing BEFORE the lock - design-only, checked on hardware in step 11)"

echo
echo "== 7. FLOOR INTEGRITY (M1): unauthorised increment refused; replay refused; clear/undefine cannot lower it =="
if tpm2_nvincrement "$FLOOR" >/dev/null 2>&1; then bad "empty-auth increment should be refused"; else pass "empty-auth increment refused (policywrite)"; fi
r=$(inc_once "$WORK/ap-init" "$WORK/sg-init" 'tpm2_policynvwritten -S "$WORK/p" c >/dev/null 2>&1'); echo "   replay of the init approval (index already written): $r"
case "$r" in REFUSED*) pass "replay of the first-increment approval refused (PolicyNvWritten clear)";; *) bad "init replay should be refused, got $r";; esac
BEFORE=$(rd)
tpm2_nvundefine -C o "$FLOOR" >/dev/null 2>&1 && echo "   owner (empty auth) undefined the counter (floor was $BEFORE)"
tpm2_nvdefine -C o -s 8 -L "$WORK/floor.authpol" -a "policywrite|authread|ownerread|nt=counter|no_da" "$FLOOR" >/dev/null 2>&1
# the redefined index is unwritten: its first (signed) increment reveals the retained TPM maxCounter
inc_once "$WORK/ap-init" "$WORK/sg-init" 'tpm2_policynvwritten -S "$WORK/p" c >/dev/null 2>&1' >/dev/null
V=$(rd 2>/dev/null)
echo "   after undefine+redefine, the first increment reads $V (TPM maxCounter is retained)"
if [ -n "$V" ] && [ "$V" -gt "$BEFORE" ]; then pass "undefine+redefine cannot lower the floor (value $V > prior $BEFORE)"; else bad "redefined counter should exceed the prior floor $BEFORE, got $V"; fi

echo
echo "== 8. RECOVERY (TPM-independent LUKS keyslot) opens in any state and survives a TPM clear =="
reboot_tpm    # worst state: PCRs all zero, nothing measured
opens "$RECKEY" && pass "recovery passphrase opens the LUKS volume with the TPM in any state" || bad "recovery should open"
opens "wrong-recovery-key" && bad "a wrong recovery key should not open" || pass "a wrong recovery key is refused by LUKS"
flushall; tpm2_clear -c l >/dev/null 2>&1 && echo "   TPM cleared (lockout hierarchy)"
# a clear also resets the dictionary-attack settings: re-provisioning sets them again (as the setup did)
tpm2_dictionarylockout -c >/dev/null 2>&1; tpm2_dictionarylockout -s -n 1000 -t 1 -l 1 >/dev/null 2>&1
opens "$RECKEY" && pass "recovery still opens after tpm2_clear (keyslot 1 untouched by the TPM)" || bad "recovery should survive a clear"
# re-seal after recovery lands in the new policy: a fresh TPM, a new base, a new secret in keyslot 0
reboot_tpm; measure v2 "$N"
loadowner
tpm2_startauthsession -S "$WORK/t" >/dev/null; tpm2_policyauthorize -S "$WORK/t" -q "$REF_I" -n "$WORK/owner.name" -L "$WORK/floor.authpol" >/dev/null; tpm2_flushcontext "$WORK/t" >/dev/null
tpm2_nvdefine -C o -s 8 -L "$WORK/floor.authpol" -a "policywrite|authread|ownerread|nt=counter|no_da" "$FLOOR" >/dev/null 2>&1
inc_once "$WORK/ap-init" "$WORK/sg-init" 'tpm2_policynvwritten -S "$WORK/p" c >/dev/null 2>&1' >/dev/null
BASE2=$(rd); echo "   re-provisioned floor, new base b2=$BASE2"
raise_floor_to $((BASE2+N)) >/dev/null 2>&1
approve_unseal2(){ local S=$1; predict11 "$S" | { read -r hx; h2b "$hx"; } > "$WORK/p11b-$S"; operand $((BASE2+S+1)) > "$WORK/opu-$S"
  tpm2_startauthsession -S "$WORK/t" >/dev/null
  tpm2_policypcr -S "$WORK/t" -l "sha256:$PCR_SVN" -f "$WORK/p11b-$S" >/dev/null
  tpm2_policynv -S "$WORK/t" -i "$WORK/opu-$S" "$FLOOR" ult >/dev/null
  tpm2_policycommandcode -S "$WORK/t" TPM2_CC_Unseal -L "$WORK/approved-$S" >/dev/null
  tpm2_flushcontext "$WORK/t" >/dev/null; osign "$WORK/approved-$S" "$REF_U" "$WORK/sig-$S"; }
reboot_tpm; measure v2 "$N"; loadowner; build_authpol
SECRET2="URBAN-DATA-KEY-$(openssl rand -hex 8)"
load_primary; printf '%s' "$SECRET2" | tpm2_create -C "$WORK/prim.ctx" -u "$WORK/seal.pub" -r "$WORK/seal.priv" -L "$WORK/authpolicy" -i - -a "fixedtpm|fixedparent|adminwithpolicy|noda" >/dev/null
approve_unseal2 "$N"
# rotate the LUKS keyslot 0 to the new secret, authorised by the recovery key (the old secret is not known after recovery):
# kill slot 0, then add the new secret back into slot 0
printf '%s' "$RECKEY" | cryptsetup luksKillSlot --key-file=- "$WORK/data.luks" 0 >/dev/null 2>&1
printf '%s' "$RECKEY" | cryptsetup luksAddKey --pbkdf pbkdf2 --pbkdf-force-iterations 1000 --key-slot 0 --key-file=- "$WORK/data.luks" <(printf '%s' "$SECRET2") >/dev/null 2>&1
reboot_tpm; measure v2 "$N"; r=$(boot_unlock "$N"); echo "   after re-seal, new secret opens: $r"
[ "$r" = OPENED ] && pass "re-seal after recovery lands in the new policy (new secret opens keyslot 0)" || bad "re-seal should open, got $r"
opens "$SECRET" && bad "the old secret should no longer open keyslot 0" || pass "the old secret no longer opens the volume"

echo
echo "== 9. MINOR: a release below the floor needs the recovery key at EVERY boot until an update arrives =="
raise_floor_to $((BASE2+NP1)) >/dev/null 2>&1; echo "   floor now $(rd) = b2+N+1; release N is below it"
for b in 1 2; do reboot_tpm; measure v2 "$N"; r=$(unseal "$N"); echo "   boot $b of release N (below floor): $r"
  case "$r" in POLICYNV-FAIL*) : ;; *) bad "below-floor release N should be refused at PolicyNV on every boot, got $r"; esac; done
pass "a release below the floor is refused by the TPM at every boot -> recovery key each boot until an update arrives"

echo
echo "== 10. PER-UNIT APPROVALS (N3): an approval signed for another unit (another EK name) is REFUSED here =="
approve_unseal2 "$NP1"
reboot_tpm; measure v2 "$NP1"; r=$(unseal "$NP1"); echo "   control, this unit's approval for N+1: ${r%%:*}"
case "$r" in UNSEALED:*) pass "this unit's own approval opens (control)";; *) bad "own approval should unseal, got $r";; esac
mkref urban-unseal "000b$(printf 'another-unit-ek' | sha256sum | cut -c1-64)" "$WORK/ref-other"
cp "$WORK/sig-$NP1" "$WORK/sig-$NP1.own"; osign "$WORK/approved-$NP1" "$WORK/ref-other" "$WORK/sig-$NP1"
OWNREF=$REF_U; REF_U="$WORK/ref-other"
reboot_tpm; measure v2 "$NP1"; r=$(unseal "$NP1"); echo "   the same approved policy, owner-signed for another unit: $r"
REF_U=$OWNREF; mv -f "$WORK/sig-$NP1.own" "$WORK/sig-$NP1"
case "$r" in UNSEAL-FAIL*) pass "another unit's approval refused (its policyRef leads to another digest than this object's authPolicy)";; *) bad "another unit's approval should be refused at Unseal, got $r";; esac

echo
if [ "$fails" -eq 0 ]; then echo "TPM POLICY CHECK PASSED"; else echo "TPM POLICY CHECK FAILED ($fails)"; fi
[ -n "$SWPID" ] && kill "$SWPID" 2>/dev/null
exit "$fails"
