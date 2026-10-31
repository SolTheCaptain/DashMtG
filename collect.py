#!/usr/bin/env python3
"""Собирает данные для дашборда в data.json.

Читает config.json, для каждого блока вызывает обработчик по полю "type"
и пишет результат в data.json. Только стандартная библиотека Python.

Как добавить новый тип блока:
  1. написать функцию вида  def мой_блок(card, tz): ... return [строки]
  2. добавить её в словарь HANDLERS внизу файла
  3. использовать "type": "мой_блок" в config.json
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from html import unescape
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (dashboard-collector)"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


# ---------- общие помощники ----------

def http_json(url, headers=None):
    req = Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8")
    return json.loads(text) if text.strip() else {}


def http_text(url, timeout=30):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------- источник: Scryfall (сеты Magic, ключ не нужен) ----------

def mtg_roadmap(card, tz):
    """Основные бумажные сеты Magic от сегодня: до конца года или `count` ближайших; без даты — TBA."""
    today = datetime.now(tz).date()
    count = card.get("count")
    end = date(today.year + 1, 12, 31) if count else date(today.year, 12, 31)
    types = set(card.get("types", ["core", "expansion"]))
    j = http_json("https://api.scryfall.com/sets", {
        "User-Agent": "DashMtGMain/1.0 (github.com/SolTheCaptain/DashMtGMain)",
        "Accept": "application/json;q=0.9,*/*;q=0.8"})
    dated, tba = [], list(card.get("extra", []))
    for s in j.get("data") or []:
        if s.get("digital") or s.get("set_type") not in types:
            continue
        if not s.get("released_at"):
            tba.append(s["name"])
            continue
        d = date.fromisoformat(s["released_at"])
        if today <= d <= end:
            dated.append((d, s["name"]))
    dated.sort()
    if not dated and not tba:
        return [f"До конца {end.year} года новых сетов в источнике нет."]
    lines = [f"{d.day} {MONTHS[d.month - 1]} — {name}" for d, name in dated]
    lines += [f"TBA — {name}" for name in tba]
    return lines[:count] if count else lines


# ---------- источник: Wizards (дата следующего анонса банов) ----------

MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
BR_URL = "https://magic.wizards.com/en/news/announcements/banned-and-restricted-{m}-{d}-{y}"


def br_find_latest(today):
    """Страницы анонсов называются banned-and-restricted-<месяц>-<день>-<год>: перебираем дни назад."""
    def probe(d):
        try:
            return d, http_text(BR_URL.format(m=MONTHS_EN[d.month - 1], d=d.day, y=d.year))
        except Exception:
            return d, None
    for start in range(0, 150, 14):
        days = [today - timedelta(days=i) for i in range(start, start + 14)]
        with ThreadPoolExecutor(7) as ex:
            hits = [(d, h) for d, h in ex.map(probe, days) if h]
        if hits:
            return max(hits, key=lambda x: x[0])
    return None, None


def br_text_lines(html):
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?i)</?(p|div|li|ul|ol|h[1-6]|br|tr|section|article)[^>]*>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return [re.sub(r"\s+", " ", unescape(l)).strip() for l in html.split("\n") if l.strip()]


def mtg_bans(card, tz):
    """Дата следующего анонса банов: в последнем анонсе Wizards есть фраза «next ... <месяц> <день>, <год>»."""
    man = card.get("manual")
    if man:  # ручной запасной вариант из config.json
        return [man["next"]] if man.get("next") else ["Дата следующего анонса пока неизвестна."]
    d, html = br_find_latest(datetime.now(tz).date())
    if not d:
        raise RuntimeError("страница анонса не найдена")
    m = re.search(r"(?is)next.{0,80}?([A-Z][a-z]+) (\d{1,2}), (\d{4})", " ".join(br_text_lines(html)))
    if m and m.group(1).lower() in MONTHS_EN:
        nd = date(int(m.group(3)), MONTHS_EN.index(m.group(1).lower()) + 1, int(m.group(2)))
        return [f"{nd.day} {MONTHS[nd.month - 1]} {nd.year}"]
    return ["Дата следующего анонса пока неизвестна."]


# ---------- источник: ЦБ РФ и CoinGecko (котировки, ключи не нужны) ----------

def num(v, nd=2):
    return f"{v:,.{nd}f}".replace(",", "\xa0").replace(".", ",")


CBR_IDS = {"USD": "R01235", "EUR": "R01239"}


def cbr_history(code, days, tz):
    """Официальный курс ЦБ за последние дни: [{d: 'ГГГГ-ММ-ДД', v: число}], только рабочие дни."""
    import xml.etree.ElementTree as ET
    end = datetime.now(tz).date() + timedelta(days=1)
    start = end - timedelta(days=days + 1)
    url = ("https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1=" + start.strftime("%d/%m/%Y") +
           "&date_req2=" + end.strftime("%d/%m/%Y") + "&VAL_NM_RQ=" + CBR_IDS[code])
    with urlopen(Request(url, headers={"User-Agent": UA}), timeout=30) as r:
        root = ET.fromstring(r.read())
    out = []
    for rec in root.iter("Record"):
        d = datetime.strptime(rec.get("Date"), "%d.%m.%Y").date()
        v = float(rec.find("Value").text.replace(",", ".")) / int(rec.find("Nominal").text)
        if d <= datetime.now(tz).date():  # завтрашний курс ЦБ публикует заранее, на графике он ни к чему
            out.append({"d": d.isoformat(), "v": round(v, 4)})
    return sorted(out, key=lambda x: x["d"])


def btc_history(days, tz):
    """BTC/USD по дням (последняя цена за сутки) из CoinGecko."""
    j = http_json(f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={days + 1}")
    by_day = {}
    for ts, price in j.get("prices") or []:
        d = datetime.fromtimestamp(ts / 1000, tz).date()
        by_day[d] = price
    return [{"d": d.isoformat(), "v": round(v)} for d, v in sorted(by_day.items())][-(days + 1):]


YAHOO = {"USD": "RUB=X", "EUR": "EURRUB=X", "BTC": "BTC-USD"}


def intraday(code, tz, step=15):
    """Рыночный курс за последние сутки с шагом step минут: [{t: ISO-время, v: число}].
    USD и EUR — Yahoo Finance, BTC — CoinGecko (запасной вариант — Yahoo)."""
    now = datetime.now(timezone.utc)
    raw = []
    if code == "BTC":
        try:
            j = http_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1")
            raw = [(datetime.fromtimestamp(ts / 1000, timezone.utc), p) for ts, p in j.get("prices") or []]
        except Exception as e:
            print(f"::warning::quotes day BTC (CoinGecko): {e}")
    if not raw:
        j = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(YAHOO[code])}?range=2d&interval={step}m")
        r = ((j.get("chart") or {}).get("result") or [{}])[0]
        closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        raw = [(datetime.fromtimestamp(ts, timezone.utc), v) for ts, v in zip(r.get("timestamp") or [], closes) if v]
    buckets = {}  # последняя цена в каждом интервале step минут
    for t, v in raw:
        if now - t <= timedelta(hours=24):
            buckets[int(t.timestamp()) // (step * 60)] = (t, v)
    nd = 0 if code == "BTC" else 4
    return [{"t": t.isoformat(timespec="minutes"), "v": round(v, nd)} for _, (t, v) in sorted(buckets.items())]


def market_daily(code, days, tz):
    """Рыночный курс по дням (закрытие дня) из Yahoo Finance: [{d, v}] за последние days+1 дней."""
    j = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(YAHOO[code])}?range=1mo&interval=1d")
    r = ((j.get("chart") or {}).get("result") or [{}])[0]
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    by_day = {}
    for ts, v in zip(r.get("timestamp") or [], closes):
        if v:
            by_day[datetime.fromtimestamp(ts, tz).date()] = v
    return [{"d": d.isoformat(), "v": round(v, 4)} for d, v in sorted(by_day.items())][-(days + 1):]


def quotes(card, tz):
    """Рыночные курсы USD и EUR к рублю и BTC к доллару — как в Google (Yahoo Finance, BTC — CoinGecko).
    Графики по каждой паре: неделя (закрытие дня) и сутки (каждые 15 минут).
    Если рыночный источник не ответил: курс ЦБ (BTC — CoinGecko)."""
    ch = card.get("chart") or {}
    days, step = int(ch.get("days", 7)), int(ch.get("step", 15))
    fiat = card.get("fiat", ["USD", "EUR"])
    charts = {}  # сбой одного графика остальное не ломает
    for code in ch.get("codes", fiat + ["BTC"]):
        pair, cur = f"{code}/{'USD' if code == 'BTC' else 'RUR'}", "$" if code == "BTC" else "₽"
        got = {}
        for rng, fn, title in (
                ("week", lambda: btc_history(days, tz) if code == "BTC" else market_daily(code, days, tz), f"{days} дн."),
                ("day", lambda: intraday(code, tz, step), "сутки")):
            try:
                pts = fn()
            except Exception as e:
                print(f"::warning::quotes {rng} {code}: {e}")
                pts = []
            if rng == "week" and len(pts) < 2 and code != "BTC":
                try:  # запасной вариант недели — курс ЦБ
                    pts = cbr_history(code, days, tz)
                except Exception as e:
                    print(f"::warning::quotes week {code} (ЦБ): {e}")
            if len(pts) >= 2:
                got[rng] = {"title": f"{pair} · {title}", "points": pts, "cur": cur}
        if got:
            charts[code] = got

    def now_rate(code):  # текущий курс = последняя точка суточного графика
        p = (charts.get(code, {}).get("day") or {}).get("points") or []
        return p[-1]["v"] if p else None

    lines, cbr = [], None
    for code in fiat:
        v = now_rate(code)
        if v is None:
            try:
                cbr = cbr or http_json("https://www.cbr-xml-daily.ru/daily_json.js")
                x = cbr["Valute"][code]
                v = x["Value"] / x["Nominal"]
            except Exception as e:
                print(f"::warning::quotes {code}: {e}")
        lines.append(f"{code}/RUR — {num(v) if v is not None else 'нет данных'}")
    v = now_rate("BTC")
    if v is None:
        try:
            v = http_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")["bitcoin"]["usd"]
        except Exception as e:
            print(f"::warning::quotes BTC: {e}")
    lines.append(f"BTC/USD — {num(v, 0) if v is not None else 'нет данных'}")
    return {"lines": lines, "charts": charts} if charts else lines


# ---------- источник: опубликованный лист Google Таблицы ----------

_SHEETS = {}


def sheet_rows(card):
    """Строки опубликованного листа. Ссылка на CSV — в секрете, имя секрета в поле `secret` блока.
    Лист скачивается один раз за запуск (несколько блоков читают один лист); Google бывает медленным — ждём до 3 минут и пробуем дважды."""
    name = card.get("secret", "")
    url = os.environ.get(name)
    if not url:
        raise RuntimeError(f"нет секрета {name or '(не задан secret в config.json)'}")
    if url not in _SHEETS:
        for attempt in range(2):
            try:
                text = http_text(url, timeout=180).lstrip("\ufeff")
                break
            except Exception:
                if attempt:
                    raise
        if text.lstrip().startswith("<"):
            raise RuntimeError("лист недоступен: проверь публикацию в формате CSV")
        _SHEETS[url] = list(csv.reader(io.StringIO(text)))
    return _SHEETS[url]


def sheet_payments(card, tz):
    """Столбцы A–C: кто, сумма, реквизит."""
    rows = []
    for r in sheet_rows(card):
        name, amount, note = (list(r) + ["", "", ""])[:3]
        name, note = name.strip(), note.strip()
        raw = re.sub(r"[\s\xa0₽]", "", amount).replace("−", "-").replace(",", ".")
        try:
            v = float(raw)
        except ValueError:
            continue  # шапка или пустая строка
        if name:
            rows.append({"name": name, "v": round(v, 2), "note": note})
    lines = [f"{x['name']} — {num(x['v'], 0)} ₽" + (f" ({x['note']})" if x["note"] else "") for x in rows]
    return {"lines": lines or ["Платежей нет."], "rows": rows}


def sheet_cell(card):
    """Число под ячейкой-подписью `anchor` в столбце `col` (0 = A)."""
    rows, col = sheet_rows(card), int(card.get("col", 0))
    for i, r in enumerate(rows[:-1]):
        if col < len(r) and r[col].strip().lower() == card["anchor"].strip().lower():
            nxt = rows[i + 1]
            return (nxt[col] if col < len(nxt) else "").strip()
    raise RuntimeError(f"в листе нет ячейки «{card['anchor']}» в столбце {col}")


def sheet_list(card, tz):
    """Непустые ячейки столбца `col` в строках 1…`count`."""
    i = ord(card.get("col", "A").upper()) - ord("A")
    cells = [(r[i] if i < len(r) else "").strip() for r in sheet_rows(card)[:int(card.get("count", 5))]]
    return [c for c in cells if c] or ["Пусто."]


def sheet_table(card, tz):
    """Таблица-блок листа: ищем ячейку с текстом `anchor` (шапка блока) и берём под ней строки
    по столбцам anchor-столбец + `cols` (смещения вправо), пока не кончатся названия."""
    rows = sheet_rows(card)
    anchor = card["anchor"].strip().lower()
    first = int(card.get("cols", [1, 2])[0])
    cell = lambda i, j: (rows[i][j] if j < len(rows[i]) else "").strip()
    # шапка блока — ячейка с anchor, у которой справа подпись столбца (одиночные метки вроде «Checks» в углу пропускаем)
    hits = [(i, j) for i, r in enumerate(rows) for j, c in enumerate(r)
            if c.strip().lower() == anchor and cell(i, j + first)]
    n = int(card.get("nth", 0))  # если шапок с таким текстом несколько — какая по счёту (слева направо, с 0)
    at = hits[n] if n < len(hits) else None
    if not at:
        raise RuntimeError(f"в листе нет ячейки «{card['anchor']}»")
    i0, j0 = at
    offs = [0] + [int(x) for x in card.get("cols", [1, 2])]
    out = []
    for r in rows[i0 + 1:]:
        cells = [re.sub(r"\s+", " ", r[j0 + k] if j0 + k < len(r) else "").strip() for k in offs]
        if not cells[0]:
            break
        out.append(cells)
    head = card.get("head") or ["", "", ""]
    lines = [" · ".join(x) for x in out] or ["Пусто."]
    table = {"head": head, "rows": out}
    for k in ("zones", "zero_cols", "export", "export_label"):  # подкраска столбца и кнопки выгрузки — их отрабатывает страница
        if card.get(k):
            table[k] = card[k]
    return {"lines": lines, "table": table}


# ---------- источник: TopDeck (вход по cookie из секрета TOPDECK_COOKIE) ----------

TOPDECK = "https://topdeck.ru"


_TD_PAGES = {}


def topdeck_get(path, secret="TOPDECK_COOKIE"):
    """Страница TopDeck под входом: cookie из секрета (у каждого аккаунта свой). За запуск каждая страница качается один раз."""
    if (path, secret) in _TD_PAGES:
        return _TD_PAGES[(path, secret)]
    ck = os.environ.get(secret)
    if not ck:
        seen = ", ".join(f"{k}{'' if os.environ[k] else ' (пустой)'}" for k in sorted(os.environ) if k.startswith("TOPDECK"))
        raise RuntimeError(f"нет секрета {secret} (сборка видит: {seen or 'ничего'})")
    req = Request(TOPDECK + path, headers={"User-Agent": UA, "Cookie": ck})
    with urlopen(req, timeout=60) as r:
        _TD_PAGES[(path, secret)] = r.read().decode("utf-8", "replace")
    return _TD_PAGES[(path, secret)]


def topdeck_trades(card, tz):
    """Видимость торговых тем: на странице участника под входом хозяина есть кнопка «Скрыть» (тема видна) или «Показать» (скрыта)."""
    items, lines, fails = [], [], 0
    for m in card.get("members", []):
        it = {"label": m["label"], "url": TOPDECK + (f"/forums/topic/{m['topic']}/" if m.get("topic") else f"/apps/toptrade/member/{m['id']}")
              if m.get("id") else None}
        if m.get("topic"):  # дата обновления из названия темы («… обновление 2610» → 26.10) и можно ли её поднять
            try:
                page = topdeck_get(f"/forums/topic/{m['topic']}/", m.get("cookie", "TOPDECK_COOKIE"))
                bump = re.search(r"(?is)data-controller='plugins\.tb\.BumpUpTopics'>(.*?)</li>", page)
                if bump:  # «Поднять эту тему»: неактивна (elBumpDisabled) — ещё рано, иначе — можно
                    off = "elBumpDisabled" in bump.group(1) or "ipsButton_disabled" in bump.group(1)
                    it["bump"] = "wait" if off else "can"
                    tip = re.search(r"title='([^']*)'", bump.group(1))
                    if tip:
                        it["bump_tip"] = unescape(tip.group(1)).strip()
                title = unescape((re.search(r"(?is)<title>(.*?)</title>", page) or [""])[1])
                d = re.search(r"(?i)обновлени\w*\s*(\d{1,2})[.\s/-]?(\d{2})\b", title)
                if d:
                    it["date"] = f"{int(d.group(1)):02d}.{d.group(2)}"
            except Exception as e:
                print(f"::warning::topdeck тема {m['label']}: {e}")
        if not m.get("id"):
            it["state"] = "stub"  # заглушка
        else:
            try:
                html = topdeck_get(f"/apps/toptrade/member/{m['id']}", m.get("cookie", "TOPDECK_COOKIE"))
                btns = re.findall(r"<button[^>]*>(?:\s|<[^>]+>)*(Скрыть|Показать)\s*</button>", html)
                if "Скрыть" in btns:
                    it["state"] = "shown"
                elif "Показать" in btns:
                    it["state"] = "hidden"
                else:
                    raise RuntimeError(f"кнопки «Скрыть/Показать» нет — вход не сработал, обнови {m.get('cookie', 'TOPDECK_COOKIE')}")
            except Exception as e:
                fails += 1
                print(f"::warning::topdeck {m['label']}: {e}")
                it["state"] = "error"
        items.append(it)
        lines.append(f"{m['label']} — {dict(shown='видна', hidden='скрыта', stub='—', error='нет данных')[it['state']]}")
    if fails and fails == sum(1 for m in card.get("members", []) if m.get("id")):
        raise RuntimeError("не удалось получить статус ни одной темы")
    return {"lines": lines, "items": items}


def topdeck_inbox(card, tz):
    """Непрочитанные личные сообщения: счётчик у конверта в шапке форума (берём страницу темы — она уже скачана для торговых тем).
    Текст переписки не читаем, только число."""
    items, lines, fails = [], [], 0
    for m in card.get("members", []):
        it = {"label": m["label"], "url": TOPDECK + "/messenger/"}
        try:
            html = topdeck_get(f"/forums/topic/{m['topic']}/" if m.get("topic") else "/", m.get("cookie", "TOPDECK_COOKIE"))
            n = re.search(r"data-notificationType='inbox'[^>]*?(?:data-currentCount='(\d+)'[^>]*)?>\s*(\d+)", html)
            if not n:
                raise RuntimeError(f"счётчика сообщений нет — вход не сработал, обнови {m.get('cookie', 'TOPDECK_COOKIE')}")
            cnt = int(n.group(1) or n.group(2))
            it.update(state="hidden" if cnt else "shown", info="Не прочитано" if cnt else "Прочитано", count=cnt)
        except Exception as e:
            fails += 1
            print(f"::warning::topdeck сообщения {m['label']}: {e}")
            it["state"] = "error"
        items.append(it)
        lines.append(f"{m['label']} — {it.get('info', 'нет данных')}")
    if fails and fails == len(card.get("members", [])):
        raise RuntimeError("не удалось проверить сообщения ни одного участника")
    return {"lines": lines, "items": items}


def left_seconds(t):
    """«1д 23ч», «5ч 10м», «40м» → секунды до окончания."""
    mult = {"д": 86400, "ч": 3600, "м": 60, "с": 1}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)\s*([дчмс])", t))


def topdeck_aucs(card, tz):
    """Аукционы участника: сколько идёт сейчас и когда закончится ближайший — «N до ДД.ММ ЧЧ:ММ».
    TopDeck пишет только остаток («1д 23ч», «21ч 17мин»), поэтому окончание = время сбора + остаток."""
    items, lines, fails = [], [], 0
    for m in card.get("members", []):
        it = {"label": m["label"], "url": TOPDECK + f"/apps/toptrade/member/{m['id']}/aucs" if m.get("id") else None}
        if not m.get("id"):
            it["state"] = "stub"
        else:
            try:
                html = topdeck_get(f"/apps/toptrade/member/{m['id']}/aucs", m.get("cookie", "TOPDECK_COOKIE"))
                left = [(left_seconds(x), x) for x in re.findall(r'js-time-left"[^>]*>([^<]*)', html)]
                left = sorted(x for x in left if x[0] > 0)
                it["state"] = "shown" if left else "stub"
                info = str(len(left))
                if left:  # окончание ближайшего: время сбора + остаток; больше суток TopDeck минут не даёт — тогда «:00»
                    sec = left[0][0]
                    end = datetime.now(tz) + timedelta(seconds=sec)
                    info += f" до {end:%d.%m %H:%M}" if sec < 86400 else f" до {end:%d.%m %H}:00"
                it["info"] = info
            except Exception as e:
                fails += 1
                print(f"::warning::topdeck аукционы {m['label']}: {e}")
                it["state"] = "error"
        items.append(it)
        lines.append(f"{m['label']} — {it.get('info', '—')}")
    if fails and fails == sum(1 for m in card.get("members", []) if m.get("id")):
        raise RuntimeError("не удалось получить аукционы ни одного участника")
    return {"lines": lines, "items": items}


# ---------- реестр типов блоков ----------

HANDLERS = {
    "quotes": quotes,
    "sheet_payments": sheet_payments,
    "sheet_list": sheet_list,
    "sheet_table": sheet_table,
    "topdeck_trades": topdeck_trades,
    "topdeck_aucs": topdeck_aucs,
    "topdeck_inbox": topdeck_inbox,
    "mtg_roadmap": mtg_roadmap,
    "mtg_bans": mtg_bans,
}


# ---------- секреты и шифрование ----------

def resolve(x):
    """Строки вида "$ИМЯ" в config.json заменяются значением секрета ИМЯ (личные данные не лежат в репо)."""
    if isinstance(x, dict):
        return {k: resolve(v) for k, v in x.items()}
    if isinstance(x, list):
        return [resolve(v) for v in x]
    if isinstance(x, str) and re.fullmatch(r"\$[A-Z0-9_]+", x):
        v = os.environ.get(x[1:])
        if not v:  # секрет не заведён или пустой (Actions передаёт пустую строку)
            print(f"::warning::нет секрета {x[1:]}")
        return v or ""
    return x


# data.json шифруется паролем из секрета DASH_PASSWORD; страница расшифровывает его в браузере.
# Ключ: PBKDF2-SHA256 (пароль, постоянная соль, KDF_ITER) → 64 байта: 32 на шифр, 32 на подпись.
# Шифр: поток HMAC-SHA256(ключ, nonce + номер блока) XOR данные; подпись: HMAC-SHA256(nonce + шифротекст).
KDF_SALT, KDF_ITER = b"DashMtGMain", 200000


def keys(password):
    k = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), KDF_SALT, KDF_ITER, 64)
    return k[:32], k[32:]


def stream(key, nonce, n):
    out = bytearray()
    for i in range((n + 31) // 32):
        out += hmac.new(key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
    return bytes(out[:n])


def encrypt(obj, password):
    ek, mk = keys(password)
    pt = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(pt, stream(ek, nonce, len(pt))))
    tag = hmac.new(mk, nonce + ct, hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return {"enc": 1, "iter": KDF_ITER, "nonce": b64(nonce), "ct": b64(ct), "tag": b64(tag)}


def decrypt(box, password):
    ek, mk = keys(password)
    nonce, ct, tag = (base64.b64decode(box[k]) for k in ("nonce", "ct", "tag"))
    if not hmac.compare_digest(hmac.new(mk, nonce + ct, hashlib.sha256).digest(), tag):
        raise ValueError("неверный пароль")
    return json.loads(bytes(a ^ b for a, b in zip(ct, stream(ek, nonce, len(ct)))))


# ---------- запуск ----------

def load_previous():
    """Прошлые данные нужны, чтобы при сбое источника показать последнее удачное."""
    prev = {}
    try:
        url = os.environ.get("PREV_URL")
        if url:
            data = http_json(url + "?t=" + str(int(datetime.now().timestamp())))
        else:
            with open(os.path.join(ROOT, "data.json"), encoding="utf-8") as f:
                data = json.load(f)
        if data.get("enc"):
            data = decrypt(data, os.environ.get("DASH_PASSWORD", ""))
        for c in data.get("cards", []):
            prev[c["id"]] = c
    except Exception:
        pass  # прошлых данных нет или пароль сменился: соберём с нуля
    return prev


def is_fresh(sec, at_iso, tz):
    """Редкие данные не собираем каждый запуск: refresh_days — раз в N дней, refresh_at "ЧЧ:ММ" — раз в сутки после этого времени."""
    try:
        at = datetime.fromisoformat(at_iso)
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    if sec.get("refresh_days"):
        return now - at < timedelta(days=sec["refresh_days"])
    if sec.get("refresh_at"):
        h, m = map(int, sec["refresh_at"].split(":"))
        loc = now.astimezone(tz)
        mark = loc.replace(hour=h, minute=m, second=0, microsecond=0)
        if loc < mark:
            mark -= timedelta(days=1)
        return at >= mark  # уже собрано после последнего «ЧЧ:ММ»
    return False


def run_section(sec, tz, prev):
    """Один блок: возвращает словарь для data.json. Ошибка не роняет остальные."""
    out = {"id": sec["id"], "title": sec["title"]}
    if sec.get("link"):  # иконка заголовка — ссылка
        out["link"] = sec["link"]
    if sec.get("buttons"):  # кнопки-копировалки внизу блока
        out["buttons"] = sec["buttons"]
    if prev and prev.get("ok") and prev.get("at") and prev.get("rev") == sec.get("rev") and is_fresh(sec, prev["at"], tz):
        out.update(ok=True, lines=prev["lines"], at=prev["at"], rev=sec.get("rev"))
        for k in ("charts", "rows", "table", "items"):
            if prev.get(k):
                out[k] = prev[k]
        if out.get("table"):  # подписи, цвета и кнопки берём из текущего config.json, данные — из кеша
            out["table"] = {**out["table"], **{k: sec[k] for k in ("head", "zones", "zero_cols", "export", "export_label") if k in sec}}
        print(f"cached   {sec['id']}")
        return out
    try:
        fn = HANDLERS.get(sec.get("type"))
        if not fn:
            raise RuntimeError(f"неизвестный тип блока: {sec.get('type')}")
        res = fn(sec, tz)
        if isinstance(res, dict):
            out.update(ok=True, lines=res["lines"], charts=res.get("charts"), rows=res.get("rows"), table=res.get("table"), items=res.get("items"))
        else:
            out.update(ok=True, lines=res)
        out["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out["rev"] = sec.get("rev")
        print(f"ok       {sec['id']}")
    except Exception as e:
        print(f"::warning::{sec['id']}: {e}")
        out.update(ok=False, error=str(e), lines=(prev or {}).get("lines") or [])
        for k in ("charts", "rows", "table", "items"):
            if (prev or {}).get(k):
                out[k] = prev[k]
    return out


def run_group(card, tz, prev_card, now_iso):
    """Карточка с подблоками и одной общей пометкой «обновлено»."""
    prev_secs = {s["id"]: s for s in (prev_card or {}).get("sections", [])}
    secs = [run_group(s, tz, prev_secs.get(s["id"]), now_iso) if s.get("type") == "group"
            else run_section(s, tz, prev_secs.get(s["id"]))
            for s in card["sections"] if s.get("enabled", True) is not False]
    any_ok = any(s["ok"] for s in secs)
    out = {"id": card["id"], "title": card["title"], "ok": all(s["ok"] for s in secs),
           "fetched": now_iso if any_ok else (prev_card or {}).get("fetched"), "sections": secs}
    b = card.get("badge")
    if b:  # число рядом с иконкой карточки (из ячейки листа), обновляется по своему расписанию
        pb = (prev_card or {}).get("badge")
        if pb and pb.get("at") and (b.get("refresh_at") or b.get("refresh_days")) and is_fresh(b, pb["at"], tz):
            out["badge"] = {**pb, "label": b.get("label", ""), "red_from": b.get("red_from")}
        else:
            try:
                out["badge"] = {"label": b.get("label", ""), "value": sheet_cell(b), "red_from": b.get("red_from"), "at": now_iso}
            except Exception as e:
                print(f"::warning::{card['id']} badge: {e}")
                if pb:
                    out["badge"] = pb
    if card.get("link"):  # иконка заголовка — ссылка
        out["link"] = card["link"]
    return out


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        config = resolve(json.load(f))
    tz = ZoneInfo(config.get("timezone", "Europe/Moscow"))
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev = load_previous()
    cards = []
    for card in config["cards"]:
        if card.get("enabled", True) is False:
            continue
        if card.get("type") == "group":
            cards.append(run_group(card, tz, prev.get(card["id"]), now_iso))
            continue
        out = run_section(card, tz, prev.get(card["id"]))
        out["fetched"] = now_iso if out["ok"] else (prev.get(card["id"]) or {}).get("fetched")
        cards.append(out)
    data = {"updated": now_iso, "timezone": tz.key, "cards": cards}
    pw = os.environ.get("DASH_PASSWORD")
    if pw:
        data = encrypt(data, pw)
    else:
        print("::warning::нет секрета DASH_PASSWORD: data.json публикуется без шифрования")
    with open(os.path.join(ROOT, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"готово, карточек: {len(cards)}")


if __name__ == "__main__":
    main()

# КОНЕЦ ФАЙЛА collect.py
