"""Архив ПРИП: какие ПРИП действовали в заданный период.

Запуск (так его вызывает GitHub Actions каждый час):
    python prip_archive.py <папка сайта> <файл архива .json>
Собирает действующие ПРИП (как prip_update.py), дописывает их в архив и строит страницу archive.html.

Откуда берутся данные:
  - каждый запуск: действующие ПРИП; момент, когда ПРИП пропал из списка действующих, фиксируется;
  - mapm.ru отдаёт и отменённые ПРИП за год (раз в сутки) — ими архив дополняется задним числом.
Сроки действия:
  - начало — дата выпуска из подписи ПРИП («191000 МСК» — день и время, месяц восстанавливается
    по порядку номеров) или из заголовка «МУРМАНСК 01 14/08 1500=»;
  - конец — «ОТМ ЭТОТ НР …», дата ПРИП, который его отменил («ОТМ 105/26»),
    или момент исчезновения из списка действующих.

Автор: Смирнов В.В., smirnov.ecology@yandex.ru
"""
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

import prip_update as pu

YEARS_BACK = 1   # отменённые ПРИП запрашиваются за текущий и прошлый год
FIELDS = ("source", "num", "year", "title", "text")


def iso(t):
    return t.isoformat(timespec="minutes") if t else None


def parse(s):
    return dt.datetime.fromisoformat(s) if s else None


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"started": None, "notices": {}}


def save(path, arch):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # sort_keys и отсутствие «времени последнего запуска» — чтобы файл менялся только при реальных изменениях
    Path(path).write_text(json.dumps(arch, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def fetch_cancelled(now, problems):
    """Отменённые ПРИП, которые ещё показывает mapm.ru, за текущий и прошлый год."""
    out = []
    for src, (url, keep) in pu.SOURCES.items():
        for year in range(now.year - YEARS_BACK, now.year + 1):
            try:
                page = pu.PripPage()
                page.feed(pu.fetch(f"{url}/GetPripValidNo?yearPrip={year}&findPrip="))
            except OSError as e:
                problems.append(f"архив: отменённые ПРИП {src} за {year} не загружены ({e})")
                continue
            out += pu.page_notices(src, page, keep)[0]
    return out


def update(arch, active, cancelled, now):
    """Дописывает в архив действующие и отменённые ПРИП; отмечает пропавшие из списка действующих."""
    recs = arch["notices"]
    arch["started"] = arch.get("started") or iso(now)
    active_ids = {n["id"] for n in active}
    sources_ok = {n["source"] for n in active}   # источник не загрузился — его ПРИП не трогаем
    for n in active:
        r = recs.setdefault(n["id"], {"first_seen": iso(now)})
        r.update({k: n[k] for k in FIELDS}, active=True)
        r.pop("gone_seen", None)
    for nid, r in recs.items():
        if r.get("active") and nid not in active_ids and r["source"] in sources_ok:
            r["active"] = False
            r["gone_seen"] = iso(now)
    for n in cancelled:
        if n["id"] not in recs:
            recs[n["id"]] = {**{k: n[k] for k in FIELDS}, "first_seen": iso(now), "active": False, "backfilled": True}


def month_shift(t, back):
    total = t.year * 12 + t.month - 1 - back
    return total // 12, total % 12 + 1


def issued_guess(text, year, upper):
    """Дата выпуска ПРИП: (время, точно ли). upper — не позже этого момента."""
    m = re.search(r"\b(\d{2})/(\d{2})\s+(\d{2})(\d{2})\s*=", text)       # МУРМАНСК 01 14/08 1500=
    if m:
        try:
            return dt.datetime(year, int(m.group(2)), int(m.group(1)), int(m.group(3)), int(m.group(4)),
                               tzinfo=pu.MSK), True
        except ValueError:
            pass
    sig = re.findall(r"\b(\d{2})(\d{2})(\d{2})\s*МСК", text)               # 191000 МСК ГС-
    if sig:
        d, h, mi = map(int, sig[-1])
        for back in range(13):
            y, mo = month_shift(upper, back)
            if y < year:
                break
            try:
                cand = dt.datetime(y, mo, d, h, mi, tzinfo=pu.MSK)
            except ValueError:
                continue
            if cand <= upper and cand.year == year:
                return cand, False
    return None, False


def full_cancels(text):
    """Номера ПРИП, отменённые целиком: 'ОТМ 105/26', 'ОТМ 12/24 261/25 И ЭТОТ ПУНКТ'; частичные ('ПУНКТ 4') — нет."""
    out = []
    for m in re.finditer(r"ОТМ\w*\s+((?:\d+\s*/\s*\d{2}[\s,И]*)+)(ПУНКТ)?", text):
        if m.group(2):
            continue
        out += [(int(a), 2000 + int(b)) for a, b in re.findall(r"(\d+)\s*/\s*(\d{2})", m.group(1))]
    return out


def compute_periods(arch, now):
    recs = arch["notices"]
    # 1. дата выпуска: номера идут по порядку, поэтому месяц восстанавливается от новых к старым
    groups = {}
    for nid, r in recs.items():
        groups.setdefault((r["source"], r["year"]), []).append(r)
    for rs in groups.values():
        upper, from_neighbor = now, False
        for r in sorted(rs, key=lambda r: -r["num"]):
            year_end = dt.datetime(r["year"], 12, 31, 23, 59, tzinfo=pu.MSK)   # ПРИП прошлых лет
            upper = min(upper, parse(r["first_seen"]), year_end)
            t, exact = issued_guess(r["text"], r["year"], upper)
            if t is None and from_neighbor:
                t = upper   # подпись без даты («1830 МСК»): выпущен не позже следующего по номеру ПРИП
            r["start_year_only"] = t is None       # ни подписи, ни соседей — известен только год из номера
            if t is None:
                t = dt.datetime(r["year"], 1, 1, tzinfo=pu.MSK)
            r["start"], r["start_exact"] = iso(t), exact
            if not r["start_year_only"]:
                upper, from_neighbor = t, True
    # 2. кем отменён
    cancelled_by = {}
    for nid, r in recs.items():
        if r.get("start_year_only"):
            continue   # дата выпуска неизвестна — как момент отмены других ПРИП не годится
        for num, year in full_cancels(r["text"]):
            target = f"ПРИП {r['source'].upper()} {num}/{str(year)[2:]}"
            t = parse(r.get("start")) or parse(r["first_seen"])
            if target in recs and target != nid and (target not in cancelled_by or t < cancelled_by[target][1]):
                cancelled_by[target] = (nid, t)
    # 3. окончание
    for nid, r in recs.items():
        start = parse(r.get("start"))
        if start and re.search(r"ОТМ\w*\s+ЭТОТ\s+НР\s*=", r["text"]):
            # служебный ПРИП («1. ОТМ 76/26  2. ОТМ ЭТОТ НР=»): только отменяет другие, действует в момент выпуска
            r["end"], r["end_kind"] = iso(start), "служебный: отменяет другие ПРИП"
            continue
        explicit = parse(pu.parse_cancel(r["text"], r["year"]))
        if explicit and start and explicit < start:
            explicit = explicit.replace(year=explicit.year + 1)
        if r.get("active"):
            r["end"], r["end_kind"] = (iso(explicit), "срок в тексте ПРИП") if explicit and explicit < now else (None, None)
            continue
        cands = []
        if explicit:
            cands.append((explicit, "срок в тексте ПРИП"))
        if nid in cancelled_by:
            cands.append((cancelled_by[nid][1], f"отменён {cancelled_by[nid][0]}"))
        if start:
            cands = [c for c in cands if c[0] >= start]   # отмена не может быть раньше выпуска
        gone = parse(r.get("gone_seen"))
        if gone:
            cands = [c for c in cands if c[0] <= gone] or [(gone, "снят со списка действующих")]
        if not cands:
            cands = [(parse(r["first_seen"]), "отменён не позже этой даты")]
        r["end"], r["end_kind"] = iso(min(cands)[0]), min(cands)[1]


def build_page(arch, out_dir, now, problems):
    notices = []
    for nid, r in arch["notices"].items():
        n = {"id": nid, **{k: r[k] for k in FIELDS}, "start": r.get("start"), "start_exact": r.get("start_exact"),
             "start_year_only": r.get("start_year_only", False),
             "end": r.get("end"), "end_kind": r.get("end_kind"), "active": r.get("active", False)}
        notices.append(n)
    notices.sort(key=lambda n: (n["start"] or n["end"] or "", n["num"]), reverse=True)
    for n in notices:   # по одному: частичные отмены из других ПРИП в архиве не применяются
        feats = pu.build_features([n])
        n["geo"] = [{"type": "Feature", "geometry": f["geometry"],
                     "properties": {"kind": f["properties"]["kind"], "label": f["properties"]["label"]}} for f in feats]
    # список ПРИП прямо в HTML — для поисковиков; скрипт страницы заменяет его таблицей по фильтру
    static = "\n".join(f'<tr><td class="id">{html.escape(n["id"])}</td>'
                       f'<td class="hide">{html.escape(re.sub(r"^ПРИП \S+ \d+/\d+\s*", "", n["title"]))}</td>'
                       f'<td></td><td class="hide"></td></tr>' for n in notices)
    data = {"notices": notices, "cats": pu.CATS, "problems": problems, "started": arch["started"], "updated": iso(now)}
    (Path(out_dir) / "archive.html").write_text(pu.fill_template("archive_template.html", data, static),
                                                encoding="utf-8")
    return notices


def main():
    out_dir, arch_path = Path(sys.argv[1]), Path(sys.argv[2])
    r = pu.run(out_dir)                         # действующие ПРИП, карта и файлы
    now = parse(r["updated"])
    problems = list(r["problems"])
    arch = load(arch_path)
    # отменённые ПРИП — раз в сутки (и при первом запуске): их списки большие, а меняются редко
    cancelled = fetch_cancelled(now, problems) if not arch["notices"] or now.hour == 4 else []
    update(arch, r["notices"], cancelled, now)
    compute_periods(arch, now)
    save(arch_path, arch)
    notices = build_page(arch, out_dir, now, problems)
    print(f"архив: {len(notices)} ПРИП ({sum(n['active'] for n in notices)} действующих), ведётся с {arch['started']}")
    for p in problems:
        print("  ВНИМАНИЕ:", p)


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    main()
