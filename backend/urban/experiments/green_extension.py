"""Does extending green on queued approaches actually help? A paired experiment.

For each random seed the same demand is simulated twice:
  baseline  - traffic lights keep their fixed plans, nothing is touched;
  control   - every proposal of the platform is approved by a simulated operator after a
              short reaction delay, with all gateway limits and leases in force.
The scripted incident is switched off so only the signal policy differs.

Measured over all vehicles that entered the network (finished or not): mean time loss,
mean waiting time, and the number of completed trips.

    python -m urban.experiments.green_extension --seeds 6 --horizon 1800 --jobs 6

Writes docs/benchmark.md and prints the same table.
"""
import argparse
import concurrent.futures
import json
import math
import queue
import statistics
import xml.etree.ElementTree as ET

from .. import config as c
from ..api.cameras import load_cameras
from ..platform import Platform
from ..sources import sumo

REACTION = 10  # seconds the simulated operator needs to approve a proposal


class Done(Exception):
    pass


def run_arm(args: tuple) -> dict:
    seed, horizon, control = args
    meta = json.loads((c.OUT / "meta.json").read_text())
    equipment: queue.Queue = queue.Queue()
    platform = Platform(meta["center"], meta["radius"], load_cameras(), send=equipment.put)
    trips = c.DATA / f"tripinfo-{'control' if control else 'baseline'}-{seed}.xml"

    def publish(raw):
        platform.handle(raw)
        if control:
            for p in list(platform.gateway.proposals.values()):
                if p["status"] == "pending" and raw["t"] - p["t"] >= REACTION:
                    platform.gateway.decide(p["id"], True, "simulated-operator", raw["t"])

    sumo.run(publish, equipment, seed=seed, incident_at=math.inf, realtime=False, until=horizon,
             sumo_args=["--tripinfo-output", str(trips), "--tripinfo-output.write-unfinished", "true"])

    loss, wait, finished = [], [], 0
    for trip in ET.parse(trips).getroot().iter("tripinfo"):
        loss.append(float(trip.get("timeLoss")))
        wait.append(float(trip.get("waitingTime")))
        finished += float(trip.get("arrival")) >= 0
    trips.unlink(missing_ok=True)
    proposals = platform.gateway.proposals.values()
    return {"loss": statistics.fmean(loss), "wait": statistics.fmean(wait), "finished": finished,
            "vehicles": len(loss), "proposals": len(proposals), "applied": platform.gateway.applied}


def summarise(rows: list[dict]) -> dict:
    """Paired differences control - baseline per seed."""
    out = {}
    for key in ("loss", "wait", "finished"):
        diffs = [r["control"][key] - r["baseline"][key] for r in rows]
        base = statistics.fmean(r["baseline"][key] for r in rows)
        mean = statistics.fmean(diffs)
        sd = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
        better = sum(d < 0 for d in diffs) if key != "finished" else sum(d > 0 for d in diffs)
        out[key] = {"baseline": base, "diff": mean, "sd": sd, "pct": 100 * mean / base if base else 0.0,
                    "se": sd / math.sqrt(len(diffs)) if diffs else 0.0, "better": better, "n": len(diffs)}
    return out


def verdict(summary: dict) -> str:
    s = summary["loss"]
    if abs(s["diff"]) <= 2 * s["se"] or s["n"] < 3:
        return ("Эффект на потерях времени не отличим от шума: среднее изменение меньше двух стандартных ошибок. "
                "По этим данным нельзя утверждать, что продление зелёного помогает или вредит.")
    if s["diff"] < 0:
        return (f"Потери времени в среднем ниже на {abs(s['pct']):.1f}% и это больше двух стандартных ошибок. "
                "Эффект есть в этой симуляции; на реальном трафике его нужно проверять отдельно.")
    return (f"Потери времени в среднем выше на {abs(s['pct']):.1f}% и это больше двух стандартных ошибок. "
            "В таком виде правило вредит: продление зелёного одному подходу отнимает время у остальных.")


def report(rows: list[dict], horizon: int) -> str:
    summary = summarise(rows)
    lines = [
        "# Замер: помогает ли продление зелёного",
        "",
        "Парный эксперимент в симуляции SUMO: один и тот же спрос и одно и то же зерно случайности",
        "прогоняются дважды, без вмешательства и с применением всех предложений платформы",
        f"(оператор-имитация подтверждает каждое через {REACTION} с; пределы и аренда шлюза действуют).",
        "Сценарное ДТП отключено. Считаются все машины, въехавшие в сеть, включая не доехавшие.",
        "",
        f"Горизонт: {horizon} с симуляции, прогонов: {len(rows)}.",
        "",
        "| Зерно | Потери времени, с (без / с) | Ожидание, с (без / с) | Завершено поездок (без / с) | Предложений / применено |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        b, k = r["baseline"], r["control"]
        lines.append(f"| {r['seed']} | {b['loss']:.1f} / {k['loss']:.1f} | {b['wait']:.1f} / {k['wait']:.1f} | "
                     f"{b['finished']} / {k['finished']} | {k['proposals']} / {k['applied']} |")
    lines += ["", "## Итог", "", "| Показатель | Без вмешательства | Изменение | ± ст. ошибка | В пользу управления |", "|---|---|---|---|---|"]
    names = {"loss": "Потери времени на машину, с", "wait": "Ожидание на машину, с", "finished": "Завершено поездок"}
    for key, name in names.items():
        s = summary[key]
        lines.append(f"| {name} | {s['baseline']:.1f} | {s['diff']:+.1f} ({s['pct']:+.1f}%) | {s['se']:.1f} | "
                     f"{s['better']} из {s['n']} прогонов |")
    lines += ["", verdict(summary), "",
              "## Ограничения",
              "",
              "- Спрос синтетический (случайные поездки), а не реальный трафик Ферганы.",
              "- Планы светофоров сгенерированы автоматически, а не взяты с реальных перекрёстков.",
              "- Правило простое: +10 или +15 с зелёного подходу с очередью. Координации соседних перекрёстков нет.",
              "- Сеть в этом сценарии перегружена (потери около 5 минут на машину). При таком спросе локальное",
              "  продление одной фазы перераспределяет задержку, а не убирает её.",
              "- Число прогонов небольшое; вывод верен для этой сети и этого уровня загрузки.",
              "",
              "Повторить: `python -m urban.experiments.green_extension`.", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--horizon", type=int, default=1800)
    ap.add_argument("--jobs", type=int, default=4, help="simulations to run in parallel")
    args = ap.parse_args()

    tasks = [(seed, args.horizon, control) for seed in range(1, args.seeds + 1) for control in (False, True)]
    with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
        results = dict(zip(tasks, pool.map(run_arm, tasks)))
    rows = []
    for seed in range(1, args.seeds + 1):
        row = {"seed": seed, "baseline": results[(seed, args.horizon, False)],
               "control": results[(seed, args.horizon, True)]}
        rows.append(row)
        print(f"seed {seed}: loss {row['baseline']['loss']:.1f} -> {row['control']['loss']:.1f} s, "
              f"applied {row['control']['applied']}", flush=True)
    text = report(rows, args.horizon)
    (c.ROOT / "docs" / "benchmark.md").write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
