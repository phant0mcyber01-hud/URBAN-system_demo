"""Does moving green towards the overloaded direction actually help? A paired experiment.

For each random seed and demand level the same traffic is simulated twice:
  baseline  - traffic lights keep their fixed plans, nothing is touched;
  control   - every proposal of the platform is approved by a simulated operator after a
              short reaction delay, with all gateway limits, leases and early release in force.
The scripted incident is switched off so only the signal policy differs.

Two questions are measured:
  network   - mean time loss of all vehicles that entered the network (finished or not);
  junctions - mean total queue length at the watched junctions, all approaches together,
              so a gain for one direction paid for by another does not count as a gain.

Coverage: "cameras" uses the 8 demo cameras, "all" pretends every signalised junction has one.

    python -m urban.experiments.signal_rebalance --seeds 5 --scales 0.5,0.75,1.0 --coverage both

Writes docs/benchmark.md and prints the same report.
"""
import argparse
import concurrent.futures
import json
import math
import queue
import statistics
import xml.etree.ElementTree as ET

from .. import config as c
from ..junctions import load_all_junctions, load_cameras
from ..platform import Platform
from ..sources import sumo

REACTION = 10  # seconds the simulated operator needs to approve a proposal
WARMUP = 300   # seconds excluded from the junction metric while the network fills up
METRICS = {"loss": "Потери времени на машину, с", "local": "Суммарная длина очередей у перекрёстков с датчиками, м",
           "finished": "Завершено поездок"}
HIGHER_IS_BETTER = {"finished"}


def run_arm(task: tuple) -> dict:
    seed, horizon, control, scale, coverage = task
    meta = json.loads((c.OUT / "meta.json").read_text())
    cameras = load_all_junctions() if coverage == "all" else load_cameras()
    equipment: queue.Queue = queue.Queue()
    platform = Platform(meta["center"], meta["radius"], cameras, send=equipment.put)
    trips = c.DATA / f"tripinfo-{coverage}-{scale}-{int(control)}-{seed}.xml"
    standing = []
    warmup = min(WARMUP, horizon // 2)

    def publish(raw):
        platform.handle(raw)
        if raw["t"] >= warmup:
            standing.append(sum(sum(q.values()) for q in platform.queues.values()))
        if control:
            for p in list(platform.gateway.proposals.values()):
                if p["status"] == "pending" and raw["t"] - p["t"] >= REACTION:
                    platform.gateway.decide(p["id"], True, "simulated-operator", raw["t"])

    sumo.run(publish, equipment, seed=seed, incident_at=math.inf, realtime=False, until=horizon,
             sumo_args=["--scale", str(scale), "--tripinfo-output", str(trips),
                        "--tripinfo-output.write-unfinished", "true"])

    loss, finished = [], 0
    for trip in ET.parse(trips).getroot().iter("tripinfo"):
        loss.append(float(trip.get("timeLoss")))
        finished += float(trip.get("arrival")) >= 0
    trips.unlink(missing_ok=True)
    done = [p for p in platform.gateway.proposals.values() if p["status"] == "done" and p["result"]]
    return {"loss": statistics.fmean(loss), "local": statistics.fmean(standing), "finished": finished,
            "applied": platform.gateway.applied,
            "early": sum(p["result"]["reason"] in ("balance", "relieved") for p in done)}


def summarise(pairs: list[tuple[dict, dict]]) -> dict:
    """Paired differences control - baseline over seeds, per metric."""
    out = {}
    for key in METRICS:
        diffs = [k[key] - b[key] for b, k in pairs]
        base = statistics.fmean(b[key] for b, _ in pairs)
        mean = statistics.fmean(diffs)
        se = statistics.stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) > 1 else float("inf")
        good = sum((d > 0) if key in HIGHER_IS_BETTER else (d < 0) for d in diffs)
        out[key] = {"baseline": base, "diff": mean, "se": se, "pct": 100 * mean / base if base else 0.0,
                    "good": good, "n": len(diffs), "clear": abs(mean) > 2 * se}
    return out


def word(s: dict, key: str) -> str:
    if not s["clear"]:
        return "в пределах шума"
    better = (s["diff"] > 0) if key in HIGHER_IS_BETTER else (s["diff"] < 0)
    return "лучше" if better else "хуже"


def report(results: dict, args) -> str:
    seeds = range(1, args.seeds + 1)
    lines = [
        "# Замер: помогает ли перераспределение зелёного",
        "",
        "Правило: когда на одном направлении перекрёстка в среднем за цикл стоит длинная очередь (от 40 м), а у",
        "поперечного зелёный простаивает, платформа предлагает включить один из планов, утверждённых для этого",
        "перекрёстка: +5, +10 или +15 с зелёного загруженному направлению за счёт свободного. Длина цикла не",
        "меняется, минимальный зелёный учитывает переход пешеходов. Если все направления стоят, ничего не",
        "предлагается. Команда снимается раньше срока, когда очередь ушла или начало страдать направление-донор.",
        "Очередь измеряется в метрах, как её видит датчик; аренда команды 3 минуты, окно подтверждения 15 минут.",
        "",
        "Парный эксперимент в симуляции SUMO: одинаковый спрос и одинаковое зерно случайности прогоняются",
        f"дважды, без вмешательства и с применением всех предложений (оператор-имитация подтверждает через {REACTION} с).",
        f"Сценарное ДТП отключено. Горизонт {args.horizon} с, по {args.seeds} прогонов на каждую строку.",
        "",
        "Изменение считается явным, если оно больше двух стандартных ошибок; иначе это шум.",
        "",
    ]
    names = {"cameras": "датчики только на 8 перекрёстках с камерами", "all": "датчики на всех регулируемых перекрёстках"}
    for coverage in args.coverages:
        lines += [f"## Покрытие: {names[coverage]}", "",
                  "| Спрос | Показатель | Без вмешательства | Изменение | ± ст. ошибка | Лучше в прогонах | Вывод |",
                  "|---|---|---|---|---|---|---|"]
        for scale in args.scale_list:
            pairs = [(results[(s, args.horizon, False, scale, coverage)], results[(s, args.horizon, True, scale, coverage)])
                     for s in seeds]
            summary = summarise(pairs)
            applied = statistics.fmean(k["applied"] for _, k in pairs)
            early = statistics.fmean(k["early"] for _, k in pairs)
            for key, name in METRICS.items():
                s = summary[key]
                lines.append(f"| ×{scale} | {name} | {s['baseline']:.1f} | {s['diff']:+.1f} ({s['pct']:+.1f}%) | "
                             f"{s['se']:.1f} | {s['good']} из {s['n']} | {word(s, key)} |")
            lines.append(f"| ×{scale} | Команд за прогон | | {applied:.1f}, из них снято досрочно {early:.1f} | | | |")
        lines.append("")
    lines += [
        "## Ограничения",
        "",
        "- Спрос синтетический (случайные поездки), а не реальный трафик Ферганы. ×1.0 — исходный спрос демо,",
        "  при нём сеть перегружена; ×0.5 и ×0.75 — тот же спрос, прореженный случайно.",
        "- Планы светофоров сгенерированы автоматически (две фазы, цикл около 90 с), а не взяты с реальных перекрёстков.",
        "- Координации соседних перекрёстков нет: каждый рассматривается отдельно.",
        "- Оператор-имитация одобряет всё и без задержек на раздумье; живой оператор будет медленнее и избирательнее.",
        "- Исходные планы светофоров наивные (зелёный поровну между направлениями). На перекрёстках, которые",
        "  уже настроил инженер, запас для улучшения меньше, поэтому эти цифры ближе к верхней оценке.",
        "- При полном покрытии выходит около одной команды в минуту на район: одному оператору это много.",
        "  Нужны либо группировка предложений, либо автоматика для этой одной команды после проверки на практике.",
        "- Число прогонов небольшое; выводы верны для этой сети.",
        "",
        "Повторить: `python -m urban.experiments.signal_rebalance`.", "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=1800)
    ap.add_argument("--scales", default="0.5,0.75,1.0", help="demand levels relative to the demo scenario")
    ap.add_argument("--coverage", choices=["cameras", "all", "both"], default="both")
    ap.add_argument("--jobs", type=int, default=6, help="simulations to run in parallel")
    args = ap.parse_args()
    args.scale_list = [float(s) for s in args.scales.split(",")]
    args.coverages = ["cameras", "all"] if args.coverage == "both" else [args.coverage]

    tasks = [(seed, args.horizon, control, scale, coverage)
             for coverage in args.coverages for scale in args.scale_list
             for seed in range(1, args.seeds + 1) for control in (False, True)]
    with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
        results = dict(zip(tasks, pool.map(run_arm, tasks)))
    text = report(results, args)
    (c.ROOT / "docs" / "benchmark.md").write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
