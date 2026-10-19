"""Bench acceptance tests from docs/hardware.md, section 11, as commands a person can follow.

    python -m urban.bench log-queues --sensors /run/urban/q/queues.json --out queues.csv
        on the unit: records what it measures, every few seconds, for test A
    python -m urban.bench accuracy --log queues.csv --observed observed.csv
        test A: the unit's queue against an observer's (time, approach, metres or cars); threshold 15 % or two cars
    python -m urban.bench rollback --test link|unit --controller 192.168.10.2:161
        tests V and G: watches the controller and times its return to base after you pull the link or the power

The controller is read over SNMP: SNMPv3 with SNMP_USER/SNMP_AUTH_PASS/SNMP_PRIV_PASS from the environment, or
--snmp-version 1 with --community. What object says "a commanded plan is running" depends on the controller:
by default systemPatternControl (non-zero while a pattern is commanded); give --oid for the controller's own
status object once its MIB is known.
"""
import argparse
import csv
import datetime
import json
import os
import statistics
import time
from pathlib import Path

from .equipment.ntcip import SYSTEM_PATTERN_CONTROL
from .gateway.gateway import POLICY

CAR = 7.5        # metres a standing car takes, for observers who count cars
TOLERANCE = 0.15  # share of the observed queue the unit may be off by ...
SLACK = 2 * CAR   # ... or two cars, whichever is more
MARGIN = 10       # seconds allowed on top of a timer before a rollback test fails


# ---- test A: queue accuracy
def parse_time(text: str) -> float:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return datetime.datetime.fromisoformat(text).timestamp()


def log_queues(sensors: Path, out: Path, every: float = 5.0, clock=time.time, sleep=time.sleep, rounds: int | None = None):
    """Append (time, approach, metres) rows from the unit's queues.json until stopped."""
    new = not out.exists()
    with out.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "approach", "metres"])
        n = 0
        while rounds is None or n < rounds:
            try:
                data = json.loads(sensors.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            now = clock()
            for approach, metres in (data.items() if isinstance(data, dict) else []):
                w.writerow([f"{now:.1f}", approach, metres])
            f.flush()
            n += 1
            sleep(every)


def accuracy(logged: list[dict], observed: list[dict], window: float = 10.0) -> dict:
    """Pairs every observation with the unit's reading closest in time (within `window` seconds)."""
    by_approach: dict[str, list[tuple[float, float]]] = {}
    for r in logged:
        by_approach.setdefault(r["approach"], []).append((parse_time(r["time"]), float(r["metres"])))
    pairs, unmatched = [], 0
    for o in observed:
        t = parse_time(o["time"])
        truth = float(o["metres"]) if (o.get("metres") or "").strip() else float(o["cars"]) * CAR
        near = min(by_approach.get(o["approach"], []), key=lambda x: abs(x[0] - t), default=None)
        if near is None or abs(near[0] - t) > window:
            unmatched += 1
            continue
        err = near[1] - truth
        pairs.append({"approach": o["approach"], "time": t, "observed": truth, "unit": near[1], "error": err,
                      "ok": abs(err) <= max(TOLERANCE * truth, SLACK)})
    ok = sum(p["ok"] for p in pairs)
    return {"pairs": pairs, "unmatched": unmatched, "ok": ok, "n": len(pairs),
            "mean_abs": statistics.fmean(abs(p["error"]) for p in pairs) if pairs else None,
            "bias": statistics.fmean(p["error"] for p in pairs) if pairs else None}


# ---- tests V and G: rollback timing
def wait_for(read, predicate, timeout: float, clock=time.monotonic, sleep=time.sleep, every: float = 1.0):
    """Poll read() until predicate(value) holds. Returns (value, seconds waited) or (last value, None) on timeout."""
    start, value = clock(), None
    while clock() - start <= timeout:
        try:
            value = read()
        except OSError:
            value = None  # an unreachable controller is not "back to base"; keep looking
        if value is not None and predicate(value):
            return value, clock() - start
        sleep(every)
    return value, None


def limit_for(test: str, backup_time: int) -> int:
    """How long the base plan may take to come back: the lease on the unit (V) or the controller's own timer (G)."""
    return POLICY["tls.select_plan"].lease_ttl if test == "link" else backup_time


def controller_client(args):
    from .edge.__main__ import parse_endpoint
    from .equipment.snmp import SnmpClient, SnmpV3Client
    host, port = parse_endpoint(args.controller, 161)
    if args.snmp_version == 3:
        user, a, p = (os.getenv(k, "") for k in ("SNMP_USER", "SNMP_AUTH_PASS", "SNMP_PRIV_PASS"))
        if not (user and a and p):
            raise SystemExit("SNMPv3 needs SNMP_USER, SNMP_AUTH_PASS, SNMP_PRIV_PASS in the environment")
        return SnmpV3Client(host, port, user=user, auth_password=a, priv_password=p, auth=os.getenv("SNMP_AUTH", "sha"))
    return SnmpClient(host, port, community=args.community, version=args.snmp_version)


def rollback(args) -> int:
    client = controller_client(args)
    read = lambda: client.get(args.oid)  # noqa: E731
    limit = limit_for(args.test, args.backup_time)
    what = "выдерните кабель связи блока с платформой" if args.test == "link" else "обесточьте блок URBAN Edge"
    print(f"Испытание {'В' if args.test == 'link' else 'Г'}: допустимо {limit} с (+{MARGIN} с на опрос).")
    print("1. В интерфейсе платформы примените предложение для этого перекрёстка. Жду, пока контроллер его включит…")
    value, _ = wait_for(read, lambda v: v != 0, timeout=600)
    if not value:
        print("Контроллер так и не перешёл на заданный план. Проверьте связь блока с контроллером.")
        return 2
    print(f"   Работает план {value}.")
    input(f"2. Сейчас {what} и сразу нажмите Enter. ")
    _, took = wait_for(read, lambda v: v == 0, timeout=limit + 120)
    if took is None:
        print(f"НЕ ПРОЙДЕНО: через {limit + 120} с контроллер всё ещё не на базовом плане.")
        return 1
    passed = took <= limit + MARGIN
    print(f"{'ПРОЙДЕНО' if passed else 'НЕ ПРОЙДЕНО'}: базовый план вернулся через {took:.0f} с (допустимо {limit} с).")
    return 0 if passed else 1


def main():
    ap = argparse.ArgumentParser(description="URBAN bench acceptance tests")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("log-queues")
    lg.add_argument("--sensors", required=True)
    lg.add_argument("--out", required=True)
    lg.add_argument("--every", type=float, default=5.0)
    ac = sub.add_parser("accuracy")
    ac.add_argument("--log", required=True)
    ac.add_argument("--observed", required=True, help="CSV: time, approach, metres or cars")
    rb = sub.add_parser("rollback")
    rb.add_argument("--test", choices=("link", "unit"), required=True)
    rb.add_argument("--controller", required=True)
    rb.add_argument("--oid", default=SYSTEM_PATTERN_CONTROL)
    rb.add_argument("--backup-time", type=int, default=180, help="unitBackupTime set on the controller")
    rb.add_argument("--snmp-version", type=int, choices=(0, 1, 3), default=3)
    rb.add_argument("--community", default="public")
    args = ap.parse_args()

    if args.cmd == "log-queues":
        log_queues(Path(args.sensors), Path(args.out), args.every)
    elif args.cmd == "accuracy":
        def load(p):
            with open(p, encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))
        r = accuracy(load(args.log), load(args.observed))
        if not r["n"]:
            raise SystemExit("no observation matched a logged reading within 10 s: check the clocks and approach ids")
        share = r["ok"] / r["n"]
        print(f"Испытание А: {r['ok']} из {r['n']} наблюдений в пределах {TOLERANCE:.0%} или двух машин "
              f"({share:.0%}); средняя ошибка {r['mean_abs']:.1f} м, смещение {r['bias']:+.1f} м; без пары {r['unmatched']}.")
        print("ПРОЙДЕНО" if share >= 0.9 else "НЕ ПРОЙДЕНО: меньше 90 % наблюдений в допуске")
    else:
        raise SystemExit(rollback(args))


if __name__ == "__main__":
    main()
