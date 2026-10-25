"""Step 14, G3: the pilot machine's own cameras reach its centre, in QEMU.

    python3 vm.py pilot-cameras        (dispatched by vm.py; this module is not run on its own)

One machine, the pilot (ROLE=district,center: the district server and the control centre on one machine), with 4
virtual CPUs and 3 GB (its camera vision runs the real detector on the emulated CPU). Nobody types an enrolment code:

  * at first boot urban-prepare makes the district side a key of its own (district-center, in the TPM), puts its kit in
    place (to the centre over the loopback) and starts urban-district@district-center; the centre enrols it by itself
    when it starts: Devices shows district-center as a district with that key, the audit says district-enrolled once,
    and its reporter is online at the centre over 127.0.0.1:8443 (TLS, the centre's certificate);
  * one camera: the real video data/videos/chiang-mai-720p.webm (CC BY-SA, copied into the VM for the test only), a file
    source in /etc/urban/cameras.json on the approach J-01-SW, through the real vision service (urban-vision@center,
    the YOLOX model of the image, its sandbox); its queue file is readable by the district's reporter;
  * J-01 and J-02 have units enrolled at the centre (keys made for the test; the boxes are not part of it), straight to
    the centre. Control: while J-01 is not routed through the pilot's district, the centre keeps no queue of J-01-SW,
    although the machine's vision measures it and the district reports it. Then the admin routes J-01's unit through it
    (`urban-center provision assign edge-J-01 --district district-center`) and J-01-SW's queue is in the centre's history;
  * a restart: the same key (fingerprint), the kit not redone, the same district-center, district-enrolled still once,
    online again, J-01-SW's queue arriving again;
  * control again: J-01 routed back straight to the centre, no new J-01-SW queue is kept although the camera still
    measures it.

vm.py's names are taken from the running vm.py (`__main__`), as in vm_rollout.py, whose Api and helpers this reuses.
"""
import json
import re
import sys
import time

import vm_rollout as ro

vm = sys.modules["__main__"]

OWN = "district-center"
VIDEO = "/work/data/videos/chiang-mai-720p.webm"
IN_VM = "/var/lib/urban/video/chiang-mai-720p.webm"  # where vision's sandbox may read it (not the update staging folder)
APPROACH = "J-01-SW"
# the camera over the road in front of it (1280x720): the bottom edge of the picture at the stop line, two lanes of
# 3.5 m, 40 m up the approach at the middle of the picture
CAMERA = {"camera": "cam-1", "source": IN_VM, "approach": APPROACH, "lanes": 2, "lane_width": 3.5, "zone": 100,
          "points": [{"px": [300, 719], "m": [0, 0]}, {"px": [1000, 719], "m": [0, 7]},
                     {"px": [560, 420], "m": [40, 0]}, {"px": [760, 420], "m": [40, 7]}]}
# QEMU's emulated "max" CPU without AVX2: under emulation (no KVM here) the AVX2 gather instruction (vpgatherdd) in
# numpy's dispatched kernels reads a wrong address and the vision process dies with a segfault on its first real frame,
# again at every restart (seen in the first run of this check: 65 segfaults in _multiarray_umath at a vpgatherdd, code
# bytes c4 c2 45 90). Real AVX2 CPUs run that code every day; here numpy and the detector take their SSE/AVX paths.
# Nothing on the machine itself is changed for the check.
EMULATED_CPU = "max,avx2=off"
SAMPLE = 60  # the centre keeps a queue sample a minute (urban/platform.py SAMPLE)


def vm_now(centre):
    """The machine's own clock: the centre dates its history by it. (Not this container's: Docker Desktop's clock can be
    hours off after the laptop slept, and was in the first runs of this check.)"""
    return float(centre.ssh("date +%s.%N").stdout.strip())


def queues_at_centre(api, junction, since):
    """The junction's rows in the centre's history since `since` (the machine's wall time)."""
    rows = api.get(f"/api/history/queues/{junction}?hours=2")
    return [r for r in rows if r["at"] >= since]


def reporter_online(centre, before=0):
    """urban-district@district-center says it is online at the centre for the (before + 1)-th time in this boot."""
    def count():
        log = centre.ssh(f"journalctl -b -u urban-district@{OWN} --no-pager -o cat", check=False).stdout
        return len(re.findall(rf"district: {OWN} online at the centre", log)), log.strip()[-300:]

    def check():
        n, tail = count()
        return n > before, tail
    ro.until(f"urban-district@{OWN} did not come online at its centre", check, 900, 10)
    return count()[0]


def loopback_session(centre):
    """The reporter's TLS connection: from the loopback to the centre's device port on the loopback."""
    out = centre.ssh("ss -Htnp state established '( dport = :8443 )'", check=False).stdout
    lines = [l for l in out.splitlines() if "127.0.0.1:8443" in l and "python" in l]
    if not lines:
        raise vm.Fail(f"no connection of the district's reporter to 127.0.0.1:8443:\n{out}")
    return " ".join(lines[0].split())


def own_row(api):
    row = api.devices().get(OWN)
    if row is None:
        raise vm.Fail(f"Devices does not show {OWN}: {sorted(api.devices())}")
    return row


def enrolled(api):
    return [(r["target"], r["note"], r.get("actor")) for r in reversed(api.audit("district-enrolled"))]


def unit_key(centre, uid, serial):
    """A unit's key and code, made for the test on the pilot itself (the unit's box is not part of this check)."""
    out = centre.ssh(f"/opt/urban/.venv/bin/python -I -m urban.edge --keygen --unit {uid} --serial {serial} "
                     f"--pki /tmp/{uid} --tpm off").stdout
    m = re.search(rf"URBAN1:{uid}:{serial}:([0-9a-f]+):", out)
    if not m:
        raise vm.Fail(f"no enrolment code for {uid}: {out[-300:]}")
    return m.group(1)


def watch_control(api, centre, since, what, window=2 * SAMPLE + 30):
    """For `window` seconds: the district is online and reports J-01-SW (the camera measures it), and the centre keeps no
    row of it after `since`. Returns how many times the queue file had the approach."""
    end, seen = time.time() + window, 0
    while time.time() < end:
        q = json.loads(centre.ssh("runuser -u urban -- cat /run/urban/q/queues.json", check=False).stdout or "{}")
        seen += APPROACH in q
        rows = queues_at_centre(api, "J-01", since)
        if rows:
            raise vm.Fail(f"{what}: the centre kept J-01's queue from the pilot's district: {rows[:3]}")
        if not own_row(api).get("online"):
            raise vm.Fail(f"{what}: {OWN} is not online at the centre, the control would prove nothing")
        time.sleep(15)
    if not seen:
        raise vm.Fail(f"{what}: the camera never measured {APPROACH} during the control, it would prove nothing")
    return seen


def wait_queue(api, since, what, timeout=600):
    def check():
        rows = [r for r in queues_at_centre(api, "J-01", since) if r["approach"] == APPROACH]
        return bool(rows), rows
    return ro.until(what, check, timeout, 15)


def pilot_cameras():
    centre = vm.VM("pilot", unit="center", serial="PILOT-1", role="district,center", host="urban-center", grow_mb=6144,
                   forward=[(2443, 443)], ssh_port=2222, mem=3072, smp=4, cpu=EMULATED_CPU)
    try:
        centre.start()
        centre.wait(r"URBAN-ROLE role=district,center name=center")
        m = centre.wait(rf"URBAN-ENROLMENT unit={OWN} serial=PILOT-1 fingerprint=(\S+) code=(URBAN1:{OWN}:\S+) key=(\w+)")
        fp, code, keyin = m.group(1), m.group(2), m.group(3)
        kit = centre.wait(rf"URBAN-DISTRICT kit of {OWN} in place: to the centre over the loopback 127\.0\.0\.1:8443 "
                          r"\(certificate of urban-center\)")
        centre.wait(rf"URBAN-DISTRICT starting urban-district@{OWN}")
        line = centre.booted("1", "A")
        vm.push_city(centre)  # the city's junctions (data/city: J-01 with its approach J-01-SW), as on a real centre
        ctx = vm.centre_ca(centre)
        vm.health(ctx)
        api = ro.Api(centre, ctx)
        online = reporter_online(centre)
        # ---- its own district, enrolled by itself: Devices, the audit, the loopback
        row = ro.until(f"Devices does not show {OWN} online", lambda: (bool((r := own_row(api)).get("online")), r), 600)
        if (row["kind"], row["address"], row["fingerprint"], row["status"]) != ("district", "urban-center", fp, "active"):
            raise vm.Fail(f"Devices shows {OWN} as {row}")
        if row.get("below_floor") or (row.get("health") or {}).get("os") is not None:
            raise vm.Fail(f"{OWN} reports a system version or is judged by the floor: {row}")
        got = enrolled(api)
        if len(got) != 1 or got[0][0] != OWN or f"urban-center key {fp}" not in got[0][1] or got[0][2] != "platform":
            raise vm.Fail(f"the audit does not say district-enrolled once for {OWN} with its key: {got}")
        code_file = centre.ssh("stat -c '%U:%G %a' /run/urban/own-district.code; cat /run/urban/own-district.code").stdout.split()
        if code_file != ["root:root", "644", code]:
            raise vm.Fail(f"/run/urban/own-district.code is not root's code of {OWN}: {code_file}")
        env = centre.ssh(f"cat /var/lib/urban/etc/edge-{OWN}.env").stdout
        for want in ("PLATFORM=127.0.0.1:8443", "SERVER_NAME=urban-center", "UPDATES=off"):
            if want not in env.splitlines():
                raise vm.Fail(f"the env file of {OWN} lacks {want}:\n{env}")
        session = loopback_session(centre)
        log = centre.ssh("journalctl -u urban-platform --no-pager -o cat", check=False).stdout
        said = re.findall(r"Own district: .*", log)
        print(f"OK {line[line.index('URBAN-OS-CHECK'):][:60]}; no code typed: key {fp} ({keyin}), "
              f"{kit.group(0)[kit.group(0).index('URBAN-DISTRICT'):]}; the centre: {said[-1] if said else '-'}; Devices: "
              f"{OWN} kind {row['kind']}, address {row['address']}, online; audit district-enrolled once: {got[0][1]!r} "
              f"(actor {got[0][2]}); /run/urban/own-district.code root:root 644; env PLATFORM=127.0.0.1:8443, "
              f"UPDATES=off; reporter's connection: {session}", flush=True)

        # ---- one camera of real video through the real vision, on J-01-SW
        sh_put = f"{vm.STAGE}/chiang-mai-720p.webm"
        centre.put(VIDEO, sh_put)
        centre.ssh(f"install -D -m 644 {sh_put} {IN_VM} && rm -f {sh_put} && chmod 755 /var/lib/urban/video")
        with open("/tmp/pilot-cameras.json", "w") as f:
            json.dump([CAMERA], f)
        centre.put("/tmp/pilot-cameras.json", "/var/lib/urban/etc/cameras.json")
        centre.ssh("chmod 644 /var/lib/urban/etc/cameras.json && systemctl restart urban-vision@center")
        units = {"edge-J-01": ("J-01", "URB-1"), "edge-J-02": ("J-02", "URB-2")}
        for uid, (junction, serial) in units.items():
            public = unit_key(centre, uid, serial)
            centre.ssh(f"urban-center provision enroll {uid} --junction {junction} --public {public} --serial {serial}")

        def measured():
            q = centre.ssh("runuser -u urban -- cat /run/urban/q/queues.json", check=False)
            stats = centre.ssh("cat /run/urban/q/vision.json", check=False).stdout.strip()
            try:
                return APPROACH in json.loads(q.stdout or "{}"), {"queues": q.stdout.strip() or q.stderr.strip(), "vision": stats}
            except ValueError:
                return False, {"queues": q.stdout.strip(), "vision": stats}
        seen = ro.until(f"the pilot's vision does not measure {APPROACH} (its queue file, read as the district's user)",
                        measured, 900, 10)
        vlog = centre.ssh("journalctl -u urban-vision@center --no-pager -o cat", check=False).stdout
        started = re.findall(r"vision: 1 camera\(s\).*", vlog)
        if not started or f"cam-1->{APPROACH} ({IN_VM})" not in started[-1]:
            raise vm.Fail(f"urban-vision@center did not start on the camera's video: {vlog[-400:]}")
        print(f"OK the machine's own vision measures {APPROACH} from the real video: {started[-1]}; queue file (read as "
              f"urban): {seen['queues']}; vision.json {seen['vision']}", flush=True)

        # ---- control: J-01 not routed through the pilot's district, its queue is not taken
        t0 = vm_now(centre)
        n = watch_control(api, centre, t0, "before routing")
        print(f"OK control: J-01 not routed through {OWN}: for {2 * SAMPLE + 30} s the camera measured {APPROACH} "
              f"({n} looks) and the district reported it, the centre kept none of it", flush=True)

        # ---- the admin routes J-01's unit through it: the queue reaches the centre
        r = centre.ssh(f"urban-center provision assign edge-J-01 --district {OWN}").stdout.strip()
        if r != f"edge-J-01: via {OWN} (its next kit says so)":
            raise vm.Fail(f"assign: {r}")
        t1 = vm_now(centre)
        rows = wait_queue(api, t1, f"the centre's history has no {APPROACH} queue after J-01 was routed through {OWN}")
        others = queues_at_centre(api, "J-02", t0)
        if others:
            raise vm.Fail(f"the centre kept a queue of J-02, which is not routed through {OWN}: {others[:3]}")
        dev = api.devices()
        if dev["edge-J-01"].get("district") != OWN or dev["edge-J-02"].get("district"):
            raise vm.Fail(f"Devices does not show the routing: {dev['edge-J-01']} / {dev['edge-J-02']}")
        print(f"OK `urban-center provision assign edge-J-01 --district {OWN}` ({r}): the centre's history of J-01 has "
              f"{APPROACH} from the pilot's own vision: {[(round(x['at']), x['approach'], x['metres']) for x in rows[:3]]}; "
              f"J-02 (not routed) has none", flush=True)

        # ---- a restart: the same district, not a second one
        registry = centre.ssh("sha256sum /var/lib/urban/platform/pki/devices.json").stdout.split()[0]
        centre.mark = len(centre.lines)
        centre.reboot()
        m = centre.wait(rf"URBAN-ENROLMENT unit={OWN} serial=PILOT-1 fingerprint=(\S+) code=(\S+)")
        if m.group(1) != fp or m.group(2) != code:
            raise vm.Fail(f"after the restart the district's key is {m.group(1)}, it was {fp}")
        since_boot = centre.mark
        centre.wait(rf"URBAN-DISTRICT starting urban-district@{OWN}")
        if any("URBAN-DISTRICT kit of" in l for l in centre.lines[since_boot:centre.mark]):
            raise vm.Fail("the kit was made again after a restart, with nothing changed")
        centre.booted("1", "A")
        vm.health(ctx)
        reporter_online(centre)  # its journal is new with the boot: online once more
        row = ro.until(f"Devices does not show {OWN} online after the restart",
                       lambda: (bool((r := own_row(api)).get("online")), r), 600)
        got2 = enrolled(api)
        districts = [d for d, x in api.devices().items() if x["kind"] == "district"]
        after = centre.ssh("sha256sum /var/lib/urban/platform/pki/devices.json").stdout.split()[0]
        if got2 != got or districts != [OWN] or row["fingerprint"] != fp or after != registry:
            raise vm.Fail(f"after the restart: district-enrolled {got2} (was {got}), districts {districts}, key "
                          f"{row['fingerprint']} (was {fp}), registry {'changed' if after != registry else 'same'}")
        log = centre.ssh("journalctl -u urban-platform -b --no-pager -o cat", check=False).stdout
        said = re.findall(r"Own district: .*", log)
        t2 = vm_now(centre)
        try:
            rows = wait_queue(api, t2, f"no {APPROACH} queue at the centre after the restart")
        except vm.Fail as err:  # say where the numbers stopped: the camera, the queue file, the district, the centre
            def say(cmd):
                return centre.ssh(cmd, check=False).stdout.strip()[-1500:]
            queue_file = say("ls -l /run/urban/q; cat /run/urban/q/queues.json")
            vision = say("journalctl -b -u urban-vision@center --no-pager -o cat | tail -n 15")
            district = say(f"journalctl -b -u urban-district@{OWN} --no-pager -o cat | tail -n 15")
            platform = say("journalctl -b -u urban-platform --no-pager -o cat | grep -i -E 'district|queue|J-01' | tail -n 15")
            threads = say("p=$(systemctl show -p MainPID --value urban-vision@center); ps -o pid,etime,time,stat -p $p; "
                          "for t in /proc/$p/task/*; do echo \"$(cat $t/comm) $(cat $t/wchan) $(cut -d' ' -f3 $t/stat)\"; "
                          "done; date; uptime")
            centre.ssh("systemctl restart urban-vision@center", check=False)
            time.sleep(90)  # a restarted vision: does it measure now (a boot-time start problem) or not (vision itself)?
            restarted = say("cat /run/urban/q/queues.json /run/urban/q/vision.json")
            raise vm.Fail(f"{err}\n--- queue file: {queue_file}\n--- vision: {vision}\n--- district: {district}"
                          f"\n--- platform: {platform}\n--- vision's threads: {threads}"
                          f"\n--- 90 s after restarting vision: {restarted}"
                          f"\n--- clock: vm {vm_now(centre)}, t2 {t2}, history {queues_at_centre(api, 'J-01', 0)[-3:]}")
        print(f"OK after a restart: the same key {fp}, the kit not redone, one district ({OWN}), district-enrolled still "
              f"once, the registry file unchanged, online again; the centre: {said[-1] if said else '-'}; {APPROACH} "
              f"arrives again ({rows[0]['metres']} m)", flush=True)

        # ---- control again: routed back straight to the centre, its queue is no longer taken
        centre.ssh("urban-center provision assign edge-J-01 --direct")
        n = watch_control(api, centre, vm_now(centre) + SAMPLE + 5, "after routing J-01 back to the centre")
        print(f"OK control: J-01 routed back straight to the centre, no new {APPROACH} queue kept although the camera "
              f"measured it ({n} looks)", flush=True)
        print("PILOT CAMERAS CHECK PASSED", flush=True)
    finally:
        vm.drop(centre)
