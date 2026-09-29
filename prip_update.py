"""Загрузка действующих ПРИП (Белое, Баренцево и Печорское море) с mapm.ru и выгрузка на карту.

Источники: ПРИП Архангельск (/PripAr), ПРИП Мурманск (/Prip) и ПРИП Запад (/PripW — только Печорское
море и проливы в Карское), ФГБУ «АМП Западной Арктики».
Результат в папке out/:
  prip.geojson  — все объекты (точки, районы, линии, окружности)
  prip.gpx      — для OpenCPN / картплоттеров (точки = waypoints, районы = треки)
  prip.kml      — для Google Earth / OpenCPN
  prip_map.html — интерактивная карта (открывается двойным кликом, без сервера)
  prip_raw.json — исходные тексты и список действующих номеров

Запуск:  python prip_update.py            (только стандартная библиотека Python 3.8+)

Автор: Смирнов В.В., smirnov.ecology@yandex.ru
"""
import datetime as dt
import html
import json
import math
import re
import ssl
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape

PECHORA_WORDS = ("ПЕЧОР", "ВАРАНДЕ", "ПРИРАЗЛОМ", "КОЛГУЕВ", "ВАЙГАЧ", "ЮГОРСК", "КАРСКИЕ ВОРОТА", "АМДЕРМ", "НАРЬЯН")


def pechora_only(title, text):
    """ПРИП Запад в основном про Карское море и восточнее — оставляем Печорское море и проливы в Карское."""
    if any(w in title or w in text for w in PECHORA_WORDS):
        return True
    return any(lon < 62 and lat < 72 for lon, lat, *_ in coords_in(text))


SOURCES = {  # название -> (адрес, фильтр ПРИП или None)
    "Архангельск": ("https://www.mapm.ru/PripAr", None),
    "Мурманск": ("https://www.mapm.ru/Prip", None),
    "Запад": ("https://www.mapm.ru/PripW", pechora_only),
}
OUT = Path(__file__).resolve().parent / "out"
AUTHOR = "Смирнов В.В."
AUTHOR_EMAIL = "smirnov.ecology@yandex.ru"
_user, _domain = AUTHOR_EMAIL.split("@")
GPX_META = (f"<metadata><name>ПРИП Белое, Баренцево и Печорское море</name>"
            f'<author><name>{escape(AUTHOR)}</name><email id="{_user}" domain="{_domain}"/></author></metadata>\n')
MSK = dt.timezone(dt.timedelta(hours=3))

MONTHS = {"ЯНВ": 1, "ФЕВ": 2, "МАР": 3, "АПР": 4, "МАЙ": 5, "МАЯ": 5, "ИЮН": 6, "ИЮЛ": 7,
          "АВГ": 8, "СЕН": 9, "ОКТ": 10, "НОЯ": 11, "ДЕК": 12}

COORD_RE = re.compile(
    r"(\d{2})-(\d{2}(?:[.,]\d+)?)(?:-(\d{2}(?:[.,]\d+)?))?\s*([СC])[\s,]*"
    r"(\d{2,3})-(\d{2}(?:[.,]\d+)?)(?:-(\d{2}(?:[.,]\d+)?))?\s*([ВЗB])"
)
SEGMENT_RE = re.compile(r"^\s*(?:\d{1,2}|[А-ЯЁ])\s*\.(?=\s|\d)", re.M)
AREA_WORDS = ("РАЙОН", "ОГРАНИЧЕНН", "ТОЧКАМИ", "ЗАПРЕТН", "ПОЛИГОН", "АКВАТОРИ")
RADIUS_RE = re.compile(r"РАДИУС\w*\s+(\d+(?:[.,]\d+)?)\s*(МЕТР|М\b|КБТ|КАБ|МИЛ|МИЛЬ)")

CATEGORIES = [  # (ключ, подпись, цвет, слова-признаки)
    ("fire", "Стрельбы / запретные районы", "#d62728", ("СТРЕЛЬБ", "ЗАПРЕТН", "РАКЕТ", "УЧЕНИ", "ПУСК", "ВЗРЫВ", "БОМБ", "ТОРПЕД")),
    ("hazard", "Опасности / затонувшие", "#9467bd", ("ЗАТОНУВШ", "ПРЕПЯТСТВ", "САДК", "БОЧК", "ЯКОРЬ", "МИНЫ", "МИННО", "ДРЕЙФ", "ОПАСН", "ПРИТОПЛ", "БОЕПРИПАС")),
    ("works", "Работы / суда", "#ff7f0e", ("РАБОТ", "ДНОУГЛУБ", "ПРОКЛАДК", "ИССЛЕДОВ", "БУКСИР", "БУРЕН", "ОБХОДИТЬ")),
    ("aids", "Навигационное оборудование", "#1f77b4", ("БУЙ", "БУИ", "ОГОН", "ОГНИ", "ЗНАК", "СТВОР", "ВЕХ", "МАЯК", "ОБОРУДОВАН", "СНО", "МДПС", "ККС")),
]
OTHER = ("other", "Прочее", "#7f7f7f")


# ---------- загрузка и разбор HTML ----------

def ssl_context():
    """Стандартные корневые сертификаты + корневой сертификат Минцифры (Russian Trusted Root CA).
    Сертификат mapm.ru выдан УЦ Минцифры, которого нет в стандартных хранилищах Linux и многих Windows."""
    ctx = ssl.create_default_context()
    ca = Path(__file__).resolve().parent / "vendor" / "russian_trusted_root_ca.pem"
    if ca.exists():
        ctx.load_verify_locations(cafile=str(ca))
    return ctx


def fetch(url, attempts=3):
    """mapm.ru иногда отвечает очень медленно — до 3 попыток с паузой."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (prip-update)"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=25, context=ssl_context()) as r:
                return r.read().decode("utf-8", errors="replace")
        except (OSError, TimeoutError):  # URLError и таймауты — наследники OSError
            if i == attempts - 1:
                raise
            time.sleep(5 * (i + 1))


class PripPage(HTMLParser):
    """Собирает последовательность (заголовок года h2, заголовок ПРИП span, текст pre)."""

    def __init__(self):
        super().__init__()
        self.items, self.year, self._tag, self._buf, self._title = [], None, None, [], None
        self.valid_block = None
        self._in_b = False
        self._valid_header = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "pre") or (tag == "span" and self._in_b):
            self._tag, self._buf = tag, []
        if tag == "b":
            self._in_b = True

    def handle_endtag(self, tag):
        if tag == "b":
            self._in_b = False
        if tag != self._tag:
            return
        text = html.unescape("".join(self._buf)).replace("\r", "")
        self._tag = None
        if tag == "h2":
            m = re.search(r"\b(20\d\d)\b", text)
            if m:
                self.year = int(m.group(1))
        elif tag == "span":
            self._title = " ".join(text.split())
        elif tag == "pre":
            if self.valid_block is None and ("ДЕЙСТВУЮЩИЕ" in text[:200] or self._valid_header):
                # Архангельск/Мурманск: заголовок внутри pre; Запад: заголовок в <b> перед pre
                self.valid_block = text if "ДЕЙСТВУЮЩИЕ" in text[:200] else self._valid_header + "\n" + text
            elif self._title:
                self.items.append({"year": self.year, "title": self._title, "text": text.strip()})
                self._title = None

    def handle_data(self, data):
        if self._tag:
            self._buf.append(data)
        elif self._in_b and "ДЕЙСТВУЮЩИЕ" in data and not self._valid_header:
            self._valid_header = " ".join(data.split())


def parse_valid(block):
    """'2026 ГОД 3 4 5 ... 110=' -> {2026: {3,4,5,...}}"""
    valid, year = {}, None
    body = block[block.find("ДЕЙСТВУЮЩИЕ"):].split("\n", 1)[-1]
    for tok in re.findall(r"\d+\s*ГОД|\d+", body.split("=")[0]):
        if "ГОД" in tok:
            year = int(re.match(r"\d+", tok).group())
            year += 2000 if year < 100 else 0
            valid.setdefault(year, set())
        elif year:
            valid[year].add(int(tok))
    return valid


# ---------- разбор текста ПРИП ----------

def to_deg(d, m, s):
    return int(d) + float(m.replace(",", ".")) / 60 + (float(s.replace(",", ".")) / 3600 if s else 0)


def coords_in(text):
    pts = []
    for m in COORD_RE.finditer(text):
        lat = to_deg(m.group(1), m.group(2), m.group(3))
        lon = to_deg(m.group(5), m.group(6), m.group(7)) * (-1 if m.group(8) == "З" else 1)
        pts.append((round(lon, 6), round(lat, 6), m.start(), m.end()))
    return pts


def category(text):
    for key, label, color, words in CATEGORIES:
        if any(w in text for w in words):
            return key
    return OTHER[0]


def parse_cancel(text, year):
    """'ОТМ ЭТОТ НР 302100 СЕНТ' / 'ОТМ ЭТОТ НР 31 ОКТ' -> ISO-время отмены (МСК)."""
    m = re.search(r"ОТМ\w*\s+ЭТОТ\s+НР\s+(\d{2})\s*(\d{2})?(\d{2})?\s*([А-Я]{3})", text)
    if not m or m.group(4) not in MONTHS:
        return None
    day, hh, mm, mon = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), MONTHS[m.group(4)]
    try:
        return dt.datetime(year, mon, day, min(hh, 23), mm, tzinfo=MSK).isoformat()
    except ValueError:
        return None


def circle(lon, lat, radius_m, n=48):
    ring = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        dlat = radius_m * math.cos(a) / 111320
        dlon = radius_m * math.sin(a) / (111320 * math.cos(math.radians(lat)))
        ring.append([round(lon + dlon, 6), round(lat + dlat, 6)])
    return ring


def radius_m(text):
    m = RADIUS_RE.search(text)
    if not m:
        return None
    v, unit = float(m.group(1).replace(",", ".")), m.group(2)
    return v * (185.2 if unit in ("КБТ", "КАБ") else 1852 if unit.startswith("МИЛ") else 1)


def geometries(text, skip=()):
    """Делит текст на пункты (1. 2. / А. Б.) и превращает координаты каждого пункта в геометрию.
    skip — номера пунктов, отменённых другими ПРИП (вместе с их подпунктами А. Б. …)."""
    starts = [m.start() for m in SEGMENT_RE.finditer(text)]
    bounds = [0] + starts + [len(text)]
    segs = [text[a:b] for a, b in zip(bounds, bounds[1:]) if text[a:b].strip()]
    is_area_notice = any(w in text for w in AREA_WORDS)
    out, point_no = [], None
    for seg in segs:
        m = re.match(r"\s*(\d{1,2})\s*\.", seg)
        if m:
            point_no = int(m.group(1))
        if point_no in skip:
            continue
        pts = coords_in(seg)
        if not pts:
            continue
        r = radius_m(seg) or (radius_m(text) if len(pts) == 1 else None)
        along_coast = "БЕРЕГОВОЙ ЛИНИИ" in seg
        if len(pts) == 1 and r:
            out.append({"type": "Polygon", "coordinates": [circle(pts[0][0], pts[0][1], r)],
                        "_kind": f"окружность R={r:.0f} м", "_center": [pts[0][0], pts[0][1]]})
        elif len(pts) >= 2 and "МЕЖДУ БЕРЕГОМ" in seg:
            out.append({"type": "LineString", "coordinates": [[p[0], p[1]] for p in pts],
                        "_kind": "граница района (между берегом и линией)"})
        elif len(pts) >= 3 and (is_area_notice or along_coast):
            ring = [[p[0], p[1]] for p in pts] + [[pts[0][0], pts[0][1]]]
            out.append({"type": "Polygon", "coordinates": [ring],
                        "_kind": "район (граница по берегу упрощена)" if along_coast else "район"})
        elif len(pts) >= 2 and "ЛИНИ" in seg and not along_coast:
            out.append({"type": "LineString", "coordinates": [[p[0], p[1]] for p in pts], "_kind": "линия"})
        else:
            for lon, lat, a, b in pts:
                line_start = seg.rfind("\n", 0, a) + 1
                line_end = seg.find("\n", b)
                label = (seg[line_start:a] + " " + seg[b:line_end if line_end >= 0 else None])
                label = " ".join(re.sub(r"^\s*(?:\d{1,2}|[А-ЯЁ])\s*\.", "", label).split()).strip(" =")
                out.append({"type": "Point", "coordinates": [lon, lat], "_kind": "точка", "_label": label})
    return out


# ---------- сборка ----------

def page_notices(src, page, keep, valid=None, problems=None):
    """ПРИП со страницы mapm.ru -> (notices, found). valid — {год: {номера}} действующих или None (брать все)."""
    notices, found = [], set()
    for it in page.items:
        m = re.search(r"(\d+)\s*/\s*(\d{2})", it["title"])
        if not m:
            if problems is not None:
                problems.append(f"{src}: не распознан номер в «{it['title']}»")
            continue
        num, year = int(m.group(1)), 2000 + int(m.group(2))
        if valid and num not in valid.get(year, set()):
            continue  # не в списке действующих
        if (year, num) in found:
            continue
        found.add((year, num))
        if keep and not keep(it["title"], it["text"]):
            continue  # вне нашего района
        text = re.split(r"\n\s*COASTAL WARNING", it["text"])[0].strip()  # ПРИП Запад дублирует по-английски
        notices.append({"source": src, "id": f"ПРИП {src.upper()} {num}/{str(year)[2:]}",
                        "num": num, "year": year, "title": it["title"], "text": text})
    return notices, found


def collect():
    notices, valid_all, problems = [], {}, []
    for src, (url, keep) in SOURCES.items():
        page = PripPage()
        page.feed(fetch(url))
        valid = parse_valid(page.valid_block or "")
        header = next((l.strip() for l in (page.valid_block or "").splitlines() if "ДЕЙСТВУЮЩИЕ" in l), "")
        valid_all[src] = {"header": header, "numbers": {y: sorted(v) for y, v in valid.items()}}
        found_notices, found = page_notices(src, page, keep, valid, problems)
        notices += found_notices
        missing = [f"{n}/{str(y)[2:]}" for y, ns in valid.items() for n in sorted(ns) if (y, n) not in found]
        if missing:
            problems.append(f"{src}: в списке действующих, но текста на странице нет: {', '.join(missing)}")
    return notices, valid_all, problems


def partial_cancels(notices):
    """'ОТМ 32/26 ПУНКТ 4' / 'ОТМ 144/20 ПУНКТЫ 2 3' в действующих ПРИП -> {(источник, 32, 2026): {4}}."""
    out = {}
    for n in notices:
        for m in re.finditer(r"ОТМ\w*\s+(\d+)\s*/\s*(\d{2})\s+ПУНКТ\w*[ \t]+(\d+(?:[ \t]*(?:,|И)?[ \t]*\d+)*)", n["text"]):
            nums = {int(x) for x in re.findall(r"\d+", m.group(3))}
            out.setdefault((n["source"], int(m.group(1)), 2000 + int(m.group(2))), set()).update(nums)
    return out


def build_features(notices):
    feats = []
    cancelled = partial_cancels(notices)
    for n in notices:
        cat = category(n["title"] + " " + n["text"])
        cancel = parse_cancel(n["text"], n["year"])
        n["cancelled_points"] = sorted(cancelled.get((n["source"], n["num"], n["year"]), ()))
        geoms = geometries(n["text"], n["cancelled_points"])
        n["features"] = len(geoms)
        n["category"], n["cancel"] = cat, cancel
        for g in geoms:
            props = {"id": n["id"], "source": n["source"], "title": n["title"], "category": cat,
                     "kind": g.pop("_kind"), "label": g.pop("_label", ""), "cancel": cancel, "text": n["text"]}
            if "_center" in g:
                props["center"] = g.pop("_center")
            feats.append({"type": "Feature", "geometry": g, "properties": props})
    return feats


def write_gpx(feats, path):
    wpts, trks = [], []
    for f in feats:
        p, g = f["properties"], f["geometry"]
        name = escape(f"{p['id']} {p['label']}".strip()[:60])
        desc = escape(p["text"])
        if g["type"] == "Point":
            lon, lat = g["coordinates"]
            wpts.append(f'<wpt lat="{lat}" lon="{lon}"><name>{name}</name><desc>{desc}</desc><sym>Hazard</sym></wpt>')
        else:
            line = g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"]
            seg = "".join(f'<trkpt lat="{la}" lon="{lo}"/>' for lo, la in line)
            trks.append(f"<trk><name>{name}</name><desc>{desc}</desc><trkseg>{seg}</trkseg></trk>")
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<gpx version="1.1" creator="prip_update" xmlns="http://www.topografix.com/GPX/1/1">\n'
                    + GPX_META
                    + "\n".join(wpts + trks) + "\n</gpx>\n", encoding="utf-8")


def write_kml(feats, path):
    colors = {k: c for k, _, c, _ in CATEGORIES}
    colors[OTHER[0]] = OTHER[2]
    kml_col = lambda hexc, a: a + hexc[5:7] + hexc[3:5] + hexc[1:3]  # #rrggbb -> aabbggrr
    styles = "".join(
        f'<Style id="{k}"><LineStyle><color>{kml_col(c, "ff")}</color><width>2</width></LineStyle>'
        f'<PolyStyle><color>{kml_col(c, "40")}</color></PolyStyle>'
        f'<IconStyle><color>{kml_col(c, "ff")}</color></IconStyle></Style>' for k, c in colors.items())
    pms = []
    for f in feats:
        p, g = f["properties"], f["geometry"]
        c = lambda pts: " ".join(f"{lo},{la}" for lo, la in pts)
        if g["type"] == "Point":
            geo = f"<Point><coordinates>{c([g['coordinates']])}</coordinates></Point>"
        elif g["type"] == "LineString":
            geo = f"<LineString><coordinates>{c(g['coordinates'])}</coordinates></LineString>"
        else:
            geo = (f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{c(g['coordinates'][0])}"
                   f"</coordinates></LinearRing></outerBoundaryIs></Polygon>")
        pms.append(f"<Placemark><name>{escape(p['id'])}</name><styleUrl>#{p['category']}</styleUrl>"
                   f"<description><![CDATA[<pre>{html.escape(p['text'])}</pre>]]></description>{geo}</Placemark>")
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
                    f"<name>ПРИП Белое, Баренцево и Печорское море</name>"
                    f"<description>{escape(f'{AUTHOR}, {AUTHOR_EMAIL}')}</description>{styles}{''.join(pms)}</Document></kml>\n",
                    encoding="utf-8")


def static_list(notices, cats):
    """Список ПРИП прямо в HTML: его видят поисковики и браузеры без JavaScript.
    Скрипт страницы при загрузке заменяет его интерактивным списком."""
    color = {c["key"]: c["color"] for c in cats}
    rows = []
    for n in notices:
        title = re.sub(r"^ПРИП \S+ \d+/\d+\s*", "", n["title"])
        rows.append(f'<div class="item"><span class="sw" style="background:{color.get(n["category"], "#7f7f7f")}">'
                    f'</span> <b>{html.escape(n["id"])}</b><div class="t">{html.escape(title)}</div></div>')
    return "\n".join(rows)


def leaflet_tags():
    """Leaflet встраивается в страницу целиком, чтобы карта не зависела от доступности CDN."""
    vendor = Path(__file__).resolve().parent / "vendor"
    base = "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/"
    try:
        vendor.mkdir(exist_ok=True)
        for name in ("leaflet.js", "leaflet.css"):
            if not (vendor / name).exists():
                (vendor / name).write_text(fetch(base + name), encoding="utf-8")
        js = (vendor / "leaflet.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
        css = (vendor / "leaflet.css").read_text(encoding="utf-8")
        return f"<style>{css}</style>\n<script>{js}</script>"
    except OSError as e:
        print("  Leaflet не встроен, используется CDN:", e)
        return f'<link rel="stylesheet" href="{base}leaflet.css">\n<script src="{base}leaflet.js"></script>'


SHORT = {"Архангельск": "АРХ", "Мурманск": "МУР", "Запад": "ЗАП"}
# Цвета Garmin GPX extensions — их понимают многие навигационные программы, в т.ч. при импорте треков
GARMIN_COLORS = {"fire": "Red", "hazard": "Magenta", "works": "DarkYellow", "aids": "Blue", "other": "DarkGray"}


def tz_name(notice_id):
    """'ПРИП АРХАНГЕЛЬСК 110/26' -> 'ПРИП АРХ 110/26' — так объект называется в TimeZero."""
    parts = notice_id.split()
    return f"ПРИП {SHORT.get(parts[1].capitalize(), parts[1][:3])} {parts[-1]}"


def write_gpx_timezero(feats, path):
    """GPX для импорта в TimeZero: короткие имена (ПРИП АРХ 110/26), районы — замкнутыми треками."""
    wpts, trks = [], []
    counters = {}
    for f in feats:
        p, g = f["properties"], f["geometry"]
        short = tz_name(p["id"])
        counters[p["id"]] = counters.get(p["id"], 0) + 1
        name = escape(short if counters[p["id"]] == 1 else f"{short} ({counters[p['id']]})")
        desc = escape((p["label"] + "\n" if p["label"] else "") + p["text"])
        if g["type"] == "Point":
            lon, lat = g["coordinates"]
            wpts.append(f'<wpt lat="{lat}" lon="{lon}"><name>{name}</name><cmt>{escape(p["label"][:80])}</cmt>'
                        f'<desc>{desc}</desc><sym>Danger</sym><type>ПРИП</type></wpt>')
        else:
            line = g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"]
            seg = "".join(f'<trkpt lat="{la}" lon="{lo}"/>' for lo, la in line)
            color = GARMIN_COLORS.get(p["category"], "DarkGray")
            trks.append(f"<trk><name>{name}</name><desc>{desc}</desc><type>ПРИП</type>"
                        f"<extensions><gpxx:TrackExtension><gpxx:DisplayColor>{color}</gpxx:DisplayColor>"
                        f"</gpxx:TrackExtension></extensions><trkseg>{seg}</trkseg></trk>")
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<gpx version="1.1" creator="PRIP-Sync" xmlns="http://www.topografix.com/GPX/1/1" '
                    'xmlns:gpxx="http://www.garmin.com/xmlschemas/GpxExtensions/v3">\n'
                    + GPX_META
                    + "\n".join(wpts + trks) + "\n</gpx>\n", encoding="utf-8")


CATS = [{"key": k, "label": l, "color": c} for k, l, c, _ in CATEGORIES] + \
       [{"key": OTHER[0], "label": OTHER[1], "color": OTHER[2]}]


def fill_template(name, data, static_html=""):
    """Шаблон страницы (map_template.html / archive_template.html) + данные, Leaflet и коды подтверждения."""
    tpl = (Path(__file__).resolve().parent / name).read_text(encoding="utf-8")
    data = {**data, "author": {"name": AUTHOR, "email": AUTHOR_EMAIL}}
    tpl = tpl.replace("<!--__LEAFLET__-->", leaflet_tags())
    tpl = tpl.replace("<!--__STATIC_LIST__-->", static_html)
    seo = Path(__file__).resolve().parent / "seo_meta.html"   # коды подтверждения Яндекс Вебмастера / Google
    tpl = tpl.replace("<!--__VERIFY__-->", seo.read_text(encoding="utf-8").strip() if seo.exists() else "")
    return tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))


def write_map_page(path, geo, notices, problems):
    """Карта действующих ПРИП (map_template.html)."""
    html_text = fill_template("map_template.html", {"geo": geo, "notices": notices, "cats": CATS,
                                                    "problems": problems}, static_list(notices, CATS))
    Path(path).write_text(html_text, encoding="utf-8")


def run(out_dir=OUT):
    """Загружает действующие ПРИП и пишет все файлы в out_dir. Возвращает сводку."""
    out_dir = Path(out_dir)
    notices, valid, problems = collect()
    feats = build_features(notices)
    now = dt.datetime.now(MSK).isoformat(timespec="minutes")
    out_dir.mkdir(parents=True, exist_ok=True)
    geo = {"type": "FeatureCollection", "features": feats,
           "properties": {"updated": now, "headers": {s: v["header"] for s, v in valid.items()}}}
    (out_dir / "prip.geojson").write_text(json.dumps(geo, ensure_ascii=False), encoding="utf-8")
    (out_dir / "prip_raw.json").write_text(json.dumps(
        {"updated": now, "valid": valid, "problems": problems, "notices": notices},
        ensure_ascii=False, indent=1), encoding="utf-8")
    write_gpx(feats, out_dir / "prip.gpx")
    write_gpx_timezero(feats, out_dir / "prip_timezero.gpx")
    write_kml(feats, out_dir / "prip.kml")
    write_map_page(out_dir / "prip_map.html", geo, notices, problems)
    return {"updated": now, "notices": notices, "features": feats, "valid": valid, "problems": problems,
            "out_dir": out_dir}


def main():
    r = run(sys.argv[1] if len(sys.argv) > 1 else OUT)  # python prip_update.py [папка]
    notices, valid = r["notices"], r["valid"]
    no_geo = [n["id"] for n in notices if not n["features"]]
    print(f"{r['updated']}: действующих ПРИП {len(notices)}, объектов на карте {len(r['features'])}")
    for s, v in valid.items():
        print(f"  {s}: {v['header']}")
    if no_geo:
        print(f"  без координат ({len(no_geo)}): {', '.join(no_geo)}")
    for p in r["problems"]:
        print("  ВНИМАНИЕ:", p)
    print(f"  -> {r['out_dir'] / 'prip_map.html'}")


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    main()
