"""Survey of the pilot junctions: printable sheets for the visit, a table for the answers, a summary after.

    python -m urban.survey sheets            data/survey/sheets.html (print it) and data/survey/survey.csv (fill it)
    python -m urban.survey summary FILE.csv  what the answers mean: controllers, adapters, who is ready for which stage

The checklist is the one in docs/hardware.md, section 11. The CSV opens in Excel (UTF-8 with BOM);
yes/no columns take "да"/"нет", empty means "not checked yet".
"""
import argparse
import csv
import html
import json
from collections import Counter
from pathlib import Path

from . import config as c
from .junctions import load_all_junctions

# column, question on the sheet
FIELDS = [
    ("controller_vendor", "Контроллер: производитель"),
    ("controller_model", "Контроллер: модель"),
    ("controller_year", "Контроллер: год выпуска"),
    ("protocol", "Протокол связи (ntcip / contacts / modbus / snmp / gbt20999 / ocit / utmc / другой / нет)"),
    ("plan_inputs", "Входы внешнего выбора плана или «ДУ» есть (да/нет), сколько и как кодируется план"),
    ("plan_count", "Сколько планов можно загрузить"),
    ("ports", "Порты и входы (Ethernet, RS-485, входы выбора плана)"),
    ("conflict_monitor", "Монитор конфликтов есть (да/нет)"),
    ("cabinet_ok", "Шкаф в порядке, закрывается (да/нет)"),
    ("power", "Питание 220 В для блока есть (да/нет)"),
    ("space", "Место в шкафу для блока есть (да/нет)"),
    ("cabinet_sun", "Шкаф на солнце после обеда (да/нет)"),
    ("poles", "Сколько опор, где камера встанет на 8-10 м"),
    ("pole_owner", "Чьи опоры"),
    ("trees", "Деревья или щиты закрывают подъезды (да/нет)"),
    ("night_light", "Подъезды освещены ночью (да/нет)"),
    ("lte", "LTE: лучший оператор и уровень"),
    ("fiber", "Оптика рядом (да/нет)"),
    ("owner", "Кто владеет светофором"),
    ("maintainer", "Кто обслуживает, контакт"),
    ("access", "Как получить доступ в шкаф"),
    ("notes", "Заметки"),
]
YES = {"да", "yes", "y", "1", "+"}
CONTACTS = "входы выбора плана: адаптер есть (`--adapter contacts`), любая кодировка, плата реле или GPIO"
SUPPORTED = {"ntcip": "NTCIP по SNMPv3: адаптер есть (`--adapter ntcip`)",
             "contacts": CONTACTS, "relay": CONTACTS, "дк": CONTACTS, "ду": CONTACTS,
             "modbus": "Modbus: адаптер есть (`--adapter profile`), нужна карта регистров от производителя",
             "snmp": "SNMP своего MIB: адаптер есть (`--adapter profile`), нужен MIB от производителя",
             "gbt20999": "GB/T 20999: своего адаптера нет (нужен текст стандарта); пока — через входы выбора плана",
             "ocit": "OCIT-O: своего адаптера нет (спецификация по лицензии); пока — через входы выбора плана",
             "utmc": "UTMC: своего адаптера нет (нужен MIB UG405 и контроллер); пока — через входы выбора плана"}


def osm_link(lat: float, lon: float) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=19/{lat:.6f}/{lon:.6f}"


def sheets(junctions: list[dict], out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    names = {j["id"]: j["name"] for j in junctions}
    cards = []
    for j in junctions:
        approaches = "".join(f"<li>{html.escape(a.get('name') or a['id'])} — зона датчика {a.get('zone', 100):.0f} м</li>"
                             for a in j["approaches"])
        ahead = sorted({d["junction"] for ph in j["phases"] for d in ph.get("downstream", [])})
        rows = "".join(f"<tr><td>{html.escape(q)}</td><td class='blank'></td></tr>" for _, q in FIELDS)
        cards.append(f"""<section class="card">
  <h2>{j['id']} · {html.escape(j['name'])}</h2>
  <p>{j['lat']:.5f}, {j['lon']:.5f} · <a href="{osm_link(j['lat'], j['lon'])}">карта</a>
     {'· <b>есть окно камеры в демо</b>' if j.get('camera') else ''}</p>
  <p><b>Подъезды:</b></p><ul>{approaches}</ul>
  <p><b>Соседи по ходу движения:</b> {', '.join(f"{a} ({html.escape(names.get(a, ''))})" for a in ahead) or 'нет'}</p>
  <table>{rows}</table>
  <p class="foot">Фото: шкаф снаружи и внутри, табличка контроллера, каждый подъезд с места будущей камеры.</p>
</section>""")
    page = out / "sheets.html"
    page.write_text(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>URBAN · обследование перекрёстков</title>
<style>body{{font:13px/1.4 system-ui,sans-serif;margin:24px;color:#111}} h1{{font-size:18px}}
.card{{border:1px solid #999;border-radius:8px;padding:12px 16px;margin:0 0 16px;page-break-inside:avoid;break-inside:avoid}}
.card h2{{font-size:15px;margin:0 0 4px}} table{{width:100%;border-collapse:collapse}}
td{{border:1px solid #bbb;padding:5px 6px;vertical-align:top}} td:first-child{{width:48%}} .blank{{height:18px}}
.foot{{color:#555;font-size:12px}} @media print{{body{{margin:8mm}} .card{{page-break-after:always}}}}</style></head>
<body><h1>Обследование перекрёстков · {len(junctions)} шт.</h1>
<p>Порядок и смысл вопросов — docs/hardware.md, раздел 11. Ответы переносятся в survey.csv, затем
<code>python -m urban.survey summary survey.csv</code>.</p>
{''.join(cards)}</body></html>""", encoding="utf-8")
    table = out / "survey.csv"
    with table.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["junction", "name", "lat", "lon", *[k for k, _ in FIELDS]])
        for j in junctions:
            w.writerow([j["id"], j["name"], f"{j['lat']:.6f}", f"{j['lon']:.6f}", *[""] * len(FIELDS)])
    return page, table


def yes(v: str) -> bool:
    return (v or "").strip().lower() in YES


def summary(rows: list[dict]) -> str:
    """What the filled table says, in plain words."""
    visited = [r for r in rows if any((r.get(k) or "").strip() for k, _ in FIELDS)]
    protocols = Counter((r.get("protocol") or "").strip().lower() or "не указан" for r in visited)
    models = Counter(f"{r.get('controller_vendor', '').strip()} {r.get('controller_model', '').strip()}".strip() or "не указан"
                     for r in visited)
    stage1 = [r for r in visited if yes(r.get("cabinet_ok")) and yes(r.get("power")) and yes(r.get("space"))
              and (r.get("poles") or "0").strip()[:1].isdigit() and int((r.get("poles") or "0").strip().split()[0]) > 0]
    ready = {"ntcip", "contacts", "relay", "дк", "ду"}
    stage2 = [r for r in stage1 if (r.get("protocol") or "").strip().lower() in ready
              or (r.get("plan_inputs") or "").strip().lower().startswith(tuple(YES))]
    gaps = {r["junction"]: [q for k, q in FIELDS[:12] if not (r.get(k) or "").strip()] for r in visited}
    lines = [f"# Итог обследования: {len(visited)} из {len(rows)} перекрёстков", ""]
    lines += ["## Контроллеры", "", "| Модель | Сколько |", "|---|---|"]
    lines += [f"| {m} | {n} |" for m, n in models.most_common()]
    lines += ["", "## Протоколы и что писать", "", "| Протокол | Перекрёстков | Что это значит |", "|---|---|---|"]
    for proto, n in protocols.most_common():
        lines.append(f"| {proto} | {n} | {SUPPORTED.get(proto, 'нужно выяснить у производителя или обслуживающей организации')} |")
    lines += ["", f"## Готовы к этапу 1 (камеры и блок, контроллер не трогаем): {len(stage1)}", "",
              ", ".join(r["junction"] for r in stage1) or "пока ни один",
              "", f"## Готовы к этапу 2 (управление): {len(stage2)}", "",
              ", ".join(r["junction"] for r in stage2) or "пока ни один (нужен протокол, для которого есть адаптер)",
              "", "## Чего не хватает", ""]
    lines += [f"- {j}: {', '.join(g)}" for j, g in gaps.items() if g] or ["- всё основное заполнено"]
    if len(visited) < len(rows):
        lines += ["", "Ещё не обследованы: " + ", ".join(r["junction"] for r in rows if r not in visited)]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="URBAN junction survey")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sheets", help="printable sheets and an empty table")
    s = sub.add_parser("summary", help="what the filled table says")
    s.add_argument("csv")
    args = ap.parse_args()
    out = c.DATA / "survey"
    if args.cmd == "sheets":
        junctions = load_all_junctions()
        if not junctions:
            raise SystemExit("no junctions: run python -m urban.pipeline first")
        page, table = sheets(junctions, out)
        print(f"sheets: {page}\ntable:  {table}")
        return
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        text = summary(list(csv.DictReader(f)))
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
