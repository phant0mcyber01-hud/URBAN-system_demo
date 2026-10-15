"""Does the green corridor get the emergency units there sooner, and what does it cost everyone else?

For each random seed and demand level the same traffic and the same scripted incident are simulated twice:
  baseline  - the signals keep their programs; units go as they can (they may pass red lights, but not
              through the cars queued in front of them);
  corridor  - the corridor proposals of the platform are approved by a simulated operator after a short
              reaction delay; each junction is released as soon as the unit has passed it.
Rebalancing proposals are left undecided in both arms, so only the corridor differs.

Measured: for each unit, the time from dispatch until it has crossed the last signalised junction on its
route (what signals can change; the last metres to the incident are decided by the jam the incident itself
causes, the same in both arms), and the mean time loss of all other vehicles (the price of the corridor).

    python -m urban.experiments.green_corridor --seeds 6 --scales 0.75,1.0

Writes docs/corridor.md and prints the same report.
"""
import argparse
import concurrent.futures
import json
import math
import queue
import statistics
import xml.etree.ElementTree as ET

from .. import config as c
from ..corridor import COMMAND
from ..junctions import load_all_junctions
from ..platform import Platform
from ..sources import sumo

SECTION = """## Что из этого следует

Механизм работает как задуман: коридор предлагается одной группой, перекрёсток отпускается, как только машина
его проехала, две машины на одном подъезде делят зелёный, на пересекающихся — идут по очереди, перераспределение
уступает коридору. Но **явной пользы для экстренных машин замер не показал**: в большинстве прогонов разницы нет,
в остальных бывает и быстрее, и медленнее. Причина в самом инструменте: платформа может только выбрать
утверждённый план, а это +15 с зелёного в цикле около 90 с. Если машина подъезжает на красный, она всё равно
ждёт, только чуть меньше.

Поэтому в продукте коридор **по умолчанию выключен** (`GREEN_CORRIDOR=off`) и включается для испытаний.
Настоящий приоритет — функция самого контроллера (preemption в NTCIP 1202 или отдельный вход приоритета), с
безопасными переходными интервалами, которые настраивает инженер. Её подключение зависит от того, какие
контроллеры стоят в Фергане, — это вопрос обследования перекрёстков.
"""

REACTION = 10     # seconds the simulated operator needs to approve the corridor
INCIDENT_AT = 600  # the network is full by then
AFTER = 480        # seconds simulated after the incident


def run_arm(task: tuple) -> dict:
    seed, corridor, scale = task
    meta = json.loads((c.OUT / "meta.json").read_text())
    equipment: queue.Queue = queue.Queue()
    platform = Platform(meta["center"], meta["radius"], load_all_junctions(c.OUT), send=equipment.put)
    trips = c.DATA / f"tripinfo-corridor-{scale}-{int(corridor)}-{seed}.xml"
    units: dict[str, dict] = {}  # dispatch id -> {unit, t, last: route index of its last signalised approach, crossed}

    def publish(raw):
        for ev in raw.get("events", []):
            if ev["type"] == "dispatch" and ev["status"] == "en_route" and ev.get("route"):
                stops = platform.corridors.stops(ev["route"])
                if stops:
                    units[ev["id"]] = {"unit": ev["unit"], "t": ev["t"], "route": ev["route"],
                                       "last": stops[-1][0], "crossed": None}
        platform.handle(raw)
        for v in raw["vehicles"]:
            u = units.get(v.id)
            if u and u["crossed"] is None and not v.edge.startswith(":") and v.edge in u["route"]                     and u["route"].index(v.edge) > u["last"]:
                u["crossed"] = raw["t"] - u["t"]
        if corridor:
            for p in list(platform.gateway.proposals.values()):
                if p["command"] == COMMAND and p["status"] == "pending" and raw["t"] - p["t"] >= REACTION:
                    platform.gateway.decide(p["id"], True, "simulated-operator", raw["t"])

    horizon = INCIDENT_AT + AFTER
    sumo.run(publish, equipment, seed=seed, incident_at=INCIDENT_AT, realtime=False, until=horizon,
             sumo_args=["--scale", str(scale), "--tripinfo-output", str(trips),
                        "--tripinfo-output.write-unfinished", "true"])
    loss = [float(t.get("timeLoss")) for t in ET.parse(trips).getroot().iter("tripinfo")
            if not t.get("id", "").startswith("emg-")]
    trips.unlink(missing_ok=True)
    corridors = [p for p in platform.gateway.proposals.values() if p["command"] == COMMAND]
    return {"units": {u["unit"]: (u["crossed"], horizon - u["t"]) for u in units.values()},
            "loss": statistics.fmean(loss) if loss else 0.0,
            "junctions": len(corridors), "applied": sum(p.get("applied_at") is not None for p in corridors)}


def paired(base: list[float], test: list[float]) -> dict:
    diffs = [b - a for a, b in zip(base, test)]
    mean = statistics.fmean(diffs)
    se = statistics.stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) > 1 else float("inf")
    avg = statistics.fmean(base)
    return {"base": avg, "diff": mean, "pct": 100 * mean / avg if avg else 0.0, "se": se,
            "better": sum(d < -1 for d in diffs), "worse": sum(d > 1 for d in diffs), "n": len(diffs),
            "clear": abs(mean) > 2 * se}


def verdict(s: dict) -> str:
    return "в пределах шума" if not s["clear"] else ("лучше" if s["diff"] < 0 else "хуже")


def report(results: dict, args) -> str:
    seeds = range(1, args.seeds + 1)
    lines = [
        "# Замер: зелёный коридор для экстренных служб",
        "",
        "Когда к ДТП выезжают скорая и ДПС, платформа находит регулируемые перекрёстки на их маршруте и для каждого",
        "предлагает утверждённый план с наибольшим зелёным для подъезда, по которому едет машина. Оператор",
        f"подтверждает весь коридор одной кнопкой (здесь оператор-имитация, через {REACTION} с). Каждый перекрёсток",
        "возвращается к своей программе, как только машина его проехала; все — когда она на месте.",
        "",
        "Парный эксперимент в SUMO: одинаковый трафик и одинаковое ДТП прогоняются дважды, без коридора и с ним.",
        "Экстренные машины в обоих случаях могут проезжать на красный, но не сквозь машины, стоящие перед ними:",
        "коридор помогает тем, что расчищает очередь на их подъезде. Предложения перераспределения зелёного",
        f"в обоих случаях не применяются. ДТП на {INCIDENT_AT}-й секунде, после него {AFTER} с; по {args.seeds} прогонов на строку.",
        "",
        "Изменение считается явным, если оно больше двух стандартных ошибок; иначе это шум.",
        "",
        "Мерится время от выезда до пересечения последнего регулируемого перекрёстка на маршруте: на него светофоры",
        "влияют. Последние метры до ДТП решает пробка, которую создаёт само ДТП, и она одинакова в обоих случаях;",
        "время до ДТП поэтому не показатель коридора (в первой версии замера оно и запутало вывод).",
        "Машина, не прошедшая перекрёстки до конца прогона, учитывается с уже потраченным временем (нижняя оценка).",
        "",
        "| Спрос | Кто | Без коридора, с | Изменение | ± ст. ошибка | Быстрее / так же / медленнее | Вывод |",
        "|---|---|---|---|---|---|---|",
    ]
    costs = []
    for scale in args.scale_list:
        pairs = [(results[(s, False, scale)], results[(s, True, scale)]) for s in seeds]
        for unit in sorted({u for b, _ in pairs for u in b["units"]}):
            base, test = [], []
            for b, k in pairs:
                if unit in b["units"] and unit in k["units"]:
                    # a unit still on its way at the end counts with the time it has spent so far (a lower bound)
                    base.append(b["units"][unit][0] or b["units"][unit][1])
                    test.append(k["units"][unit][0] or k["units"][unit][1])
            if len(base) < 2:
                continue
            s = paired(base, test)
            lines.append(f"| ×{scale} | {unit} | {s['base']:.0f} | {s['diff']:+.0f} с ({s['pct']:+.0f}%) | {s['se']:.0f} | "
                         f"{s['better']} / {s['n'] - s['better'] - s['worse']} / {s['worse']} | {verdict(s)} |")
        loss = paired([b["loss"] for b, _ in pairs], [k["loss"] for _, k in pairs])
        junctions = statistics.fmean(k["junctions"] for _, k in pairs)
        applied = statistics.fmean(k["applied"] for _, k in pairs)
        costs.append(f"| ×{scale} | {loss['base']:.1f} | {loss['diff']:+.1f} с ({loss['pct']:+.1f}%) | {loss['se']:.1f} | "
                     f"{verdict(loss)} | {junctions:.1f} / {applied:.1f} |")
    lines += [
        "",
        "## Цена для остальных",
        "",
        "| Спрос | Потери времени на машину без коридора, с | Изменение | ± ст. ошибка | Вывод | Перекрёстков в коридоре / применено |",
        "|---|---|---|---|---|---|",
        *costs,
        "",
        *SECTION.splitlines(),
        "",
        "## Ограничения",
        "",
        "- Трафик синтетический; маршрут экстренной машины известен заранее. На улице положение машины должно",
        "  приходить от её трекера (AVL), иначе коридор не предлагается.",
        "- Экстренные машины в SUMO не создают «коридор жизни» из машин, как это делают водители на дороге;",
        "  поэтому без светофорного коридора они стоят в очереди дольше, чем в жизни, и выигрыш здесь скорее сверху.",
        "- Оператор-имитация подтверждает за 10 с; живому человеку нужно больше, и чем позже подтверждение,",
        "  тем меньше перекрёстков успевает помочь.",
        "- Один сценарий ДТП на прогон, число прогонов небольшое.",
        "",
        "Повторить: `python -m urban.experiments.green_corridor`.", "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--scales", default="0.75,1.0", help="demand levels relative to the demo scenario")
    ap.add_argument("--jobs", type=int, default=6, help="simulations to run in parallel")
    args = ap.parse_args()
    args.scale_list = [float(s) for s in args.scales.split(",")]
    tasks = [(seed, corridor, scale) for scale in args.scale_list
             for seed in range(1, args.seeds + 1) for corridor in (False, True)]
    with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
        results = dict(zip(tasks, pool.map(run_arm, tasks)))
    text = report(results, args)
    (c.ROOT / "docs" / "corridor.md").write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
