# -*- coding: utf-8 -*-
"""Широкие рыночные сигналы — то, чего нет в лентах «банк поменял ставку».

Шесть направлений, все — открытые источники, только чтение:
  1. quotes()        — котировки и реакция рынка (ISS Мосбиржи);
  2. press()         — публичные заявления: новостные ленты + пресс-центры банков;
  3. jobs()          — найм как утечка стратегии (api.hh.ru, нужен токен приложения);
  4. apps()          — релиз-ноуты мобильных приложений (RuStore, App Store);
  5. search_demand() — поисковый спрос (проверка доступности Вордстата и аналогов);
  6. ads_tariffs()   — тарифные PDF и маркетинговые страницы.

ГЛАВНЫЙ ПРИНЦИП МОДУЛЯ: ценность не в срезе, а в дельте. Каждый сбор кладёт замер
в state/signals/<тема>.json (последний) и дописывает строку в <тема>.jsonl (история).
Функция возвращает и текущие значения, и посчитанную динамику к прошлому замеру.
«У Сбера 11 вакансий про кредитные карты» — не новость. «+9 за неделю» — новость.

Ничего не падает целиком: каждый источник обёрнут, недоступный помечается
{"ok": False, "error": "..."} и не мешает остальным.

ЧТО ЗАКРЫТО (подробности и что нужно от владельца — в signals_report.md):
  • hh.ru: api.hh.ru требует токен приложения (dev.hh.ru), а публичный HTML-поиск
    запрещён robots.txt строкой «Disallow: *?*». Код готов, ждёт HH_TOKEN.
  • Яндекс Вордстат: частотности только после авторизации, открытого API нет.
  • Реестр интернет-рекламы (ЕРИР/ОРД): не публичен, креативы конкурентов недоступны.
  • Пресс-центры Сбера, МТС Банка и Совкомбанка: антибот, не отдаются даже браузером.
"""
import os, re, sys, json, time, statistics
import urllib.parse, urllib.request, urllib.error
from datetime import timedelta
import core                      # вежливый GET, пауза, CA-бандл, разбор HTML
try:
    import sources               # только читаем реестр, не трогаем файл
except Exception:
    sources = None

SIG_DIR = os.path.join(core.STATE, "signals")

# ---------------------------------------------------------------------------
# Осознанное исключение из robots.txt
# ---------------------------------------------------------------------------
# iss.moex.com отдаёт «User-Agent: * / Disallow: /» — этот robots.txt написан против
# поисковых краулеров, которые индексируют миллионы URL. ISS при этом — официально
# опубликованный открытый API Мосбиржи для программного доступа, без ключа и без
# авторизации. Мы делаем ~10 запросов в сутки с паузой 3 с. Решение спорное, поэтому
# оно вынесено в явный флаг: поставьте False — модуль перестанет ходить на ISS.
IGNORE_ROBOTS = {"iss.moex.com": True}


def _open_get(url, timeout=25, tries=2, headers=None):
    """GET в обход core.allowed() — только для доменов из IGNORE_ROBOTS.
    Пауза между запросами к домену сохраняется (используем core._raw_get)."""
    host = core._host(url)
    if not IGNORE_ROBOTS.get(host) and not core.allowed(url):
        return None, "запрещено robots.txt"
    last = ""
    for i in range(tries):
        try:
            return core._raw_get(url, timeout).decode("utf-8", "replace"), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (401, 403, 404, 451):
                break
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:60]}"
        time.sleep(1.5 * (i + 1))
    return None, last


def _head(url, timeout=20):
    """HEAD: размер и дата документа без его скачивания. Для тарифных PDF это
    в разы дешевле, чем тянуть файл целиком ради проверки «а он не менялся?»."""
    host = core._host(url)
    wait = core.PAUSE - (time.time() - core._LAST_HIT.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": core.UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=core._CTX) as r:
            return {"status": r.status,
                    "size": int(r.headers.get("Content-Length") or 0),
                    "modified": r.headers.get("Last-Modified") or "",
                    "etag": (r.headers.get("ETag") or "").strip('"')[:32]}, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:50]}"
    finally:
        core._LAST_HIT[host] = time.time()


def render_dom(url, wait_ms=12000, timeout=55):
    """Тонкая обёртка над core.render(): весь ад с headless-хромом (он не выходит
    сам, ему нужны SPKI-отпечатки УЦ Минцифры) уже решён в ядре — дублировать
    его здесь значит завести вторую реализацию, которая разойдётся с первой."""
    try:
        return core.render(url, wait_ms=wait_ms, timeout=timeout)
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"


# ---------------------------------------------------------------------------
# Состояние: последний замер + история
# ---------------------------------------------------------------------------
def _paths(topic):
    os.makedirs(SIG_DIR, exist_ok=True)
    return (os.path.join(SIG_DIR, f"{topic}.json"),
            os.path.join(SIG_DIR, f"{topic}.jsonl"))


def load_prev(topic):
    """Предыдущий замер или None. Он и есть база для расчёта динамики."""
    last, _ = _paths(topic)
    try:
        with open(last, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# Поля, которые нужны только для сравнения «сегодня против прошлого раза» и не
# должны копиться в истории. Без этого press.jsonl растёт на 180 КБ за замер —
# из-за списка seen_links, который в истории бесполезен.
_BULKY = {"seen_links", "docs", "hits", "market", "mentions", "summary"}


def save_snap(topic, data):
    """Пишем последний замер и дописываем строку в историю (для длинных трендов —
    неделя/месяц, а не только «вчера→сегодня»).

    В истории лежит облегчённая копия: тяжёлые списки нужны только для диффа
    с прошлым замером, а он всегда идёт против <тема>.json, а не против истории."""
    last, hist = _paths(topic)
    ts, day = core.msk().isoformat(timespec="seconds"), core.msk().strftime("%Y-%m-%d")
    rec = {"ts": ts, "date": day, "data": data}
    tmp = last + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, last)
    slim = {k: v for k, v in data.items() if k not in _BULKY} if isinstance(data, dict) else data
    with open(hist, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "date": day, "data": slim}, ensure_ascii=False) + "\n")
    return rec


def load_history(topic, days=60):
    """История замеров за N дней — для трендов длиннее одного шага."""
    _, hist = _paths(topic)
    edge = (core.msk() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    try:
        with open(hist, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("date", "") >= edge:
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


def _pct(new, old):
    if old in (None, 0) or new is None:
        return None
    return round((new / old - 1) * 100, 2)


# ===========================================================================
# 1. КОТИРОВКИ И РЕАКЦИЯ РЫНКА
# ===========================================================================
# Смысл для радара: продуктовая новость конкурента сама по себе ничего не стоит.
# Стоит РЕАКЦИЯ. Причём не абсолютная — весь рынок ходит вместе, — а альфа:
# движение бумаги МИНУС движение индекса Мосбиржи. Растёт рынок на 2%, а Т-Техно
# на 2% — это ноль информации. Падает рынок на 1%, а МТС Банк на 6% — это событие.
TICKERS = {
    "SBER": "Сбербанк",
    "VTBR": "ВТБ",
    "T":    "Т-Технологии",
    "SVCB": "Совкомбанк",
    "MBNK": "МТС Банк",
    "OZON": "Озон",
    "YDEX": "Яндекс",
    "RENI": "Ренессанс Страхование",   # прокси на риск-аппетит в рознице
    "BSPB": "Банк Санкт-Петербург",
    "CBOM": "МКБ",
}
# Не торгуются на Мосбирже и потому вне радара котировок:
NOT_LISTED = {
    "Газпромбанк": "акции не торгуются, публичны только облигации ГПБ",
    "Wildberries (RWB)": "непубличная компания, акций в обращении нет",
    "Альфа-Банк": "непубличная, акции не торгуются",
    "Райффайзенбанк": "дочка RBI, на Мосбирже не торгуется",
    "Почта Банк": "непубличная, вошёл в периметр ВТБ",
}
ISS = "https://iss.moex.com/iss"


def _iss_history(secid, days=70, index=False):
    """Дневная история торгов. Индексы лежат в отдельном рынке — market=index."""
    frm = (core.msk() - timedelta(days=days)).strftime("%Y-%m-%d")
    if index:
        u = (f"{ISS}/history/engines/stock/markets/index/securities/{secid}.json"
             f"?iss.meta=off&iss.only=history&history.columns=TRADEDATE,CLOSE&from={frm}")
    else:
        u = (f"{ISS}/history/engines/stock/markets/shares/boards/TQBR/securities/{secid}.json"
             f"?iss.meta=off&iss.only=history&history.columns=TRADEDATE,CLOSE,VOLUME,VALUE&from={frm}")
    raw, err = _open_get(u, timeout=30)
    if not raw:
        return None, err
    try:
        rows = json.loads(raw)["history"]["data"]
    except Exception as e:
        return None, f"разбор ISS: {e}"
    # ISS иногда отдаёт дни без сделок с CLOSE=None — они портят любой расчёт.
    rows = [r for r in rows if r[1] is not None]
    return rows, None


def quotes():
    """Котировки, дневная/недельная/месячная динамика, объём и альфа к индексу."""
    topic = "quotes"
    prev = load_prev(topic)
    res = {"source": "iss.moex.com (открытый ISS API, без ключа)",
           "not_listed": NOT_LISTED, "tickers": {}, "errors": []}

    # Индекс — база для альфы. Если он не приехал, альфу честно не считаем.
    idx, ierr = _iss_history("IMOEX", index=True)
    idx_close = [r[1] for r in idx] if idx else []
    if ierr:
        res["errors"].append(f"IMOEX: {ierr}")

    def chg(series, back):
        if len(series) <= back:
            return None
        return _pct(series[-1], series[-1 - back])

    idx_d1, idx_w1, idx_m1 = (chg(idx_close, 1), chg(idx_close, 5), chg(idx_close, 22))

    # Снимок текущих цен одним запросом на всю доску — дешевле, чем по бумаге.
    snap = {}
    u = (f"{ISS}/engines/stock/markets/shares/boards/TQBR/securities.json"
         f"?iss.meta=off&iss.only=marketdata&marketdata.columns="
         f"SECID,LAST,VALTODAY,ISSUECAPITALIZATION,UPDATETIME")
    raw, err = _open_get(u, timeout=30)
    if raw:
        try:
            for r in json.loads(raw)["marketdata"]["data"]:
                snap[r[0]] = r
        except Exception as e:
            res["errors"].append(f"снимок доски: {e}")
    else:
        res["errors"].append(f"снимок доски: {err}")

    for sec, name in TICKERS.items():
        rows, err = _iss_history(sec)
        if not rows:
            res["tickers"][sec] = {"ok": False, "name": name, "error": err or "нет истории"}
            res["errors"].append(f"{sec}: {err}")
            continue
        closes = [r[1] for r in rows]
        vals = [r[3] for r in rows if r[3]]          # оборот в рублях
        d1, w1, m1 = chg(closes, 1), chg(closes, 5), chg(closes, 22)
        # Аномальный оборот: сегодня к медиане последних 20 дней. Всплеск оборота
        # без движения цены — тоже сигнал (перекладка крупного держателя).
        vol_ratio = None
        if len(vals) >= 6:
            med = statistics.median(vals[-21:-1]) if len(vals) > 21 else statistics.median(vals[:-1])
            vol_ratio = round(vals[-1] / med, 2) if med else None
        md = snap.get(sec) or []
        rec = {
            "ok": True, "name": name,
            "last_close": closes[-1], "last_date": rows[-1][0],
            "now": md[1] if len(md) > 1 else None,
            "cap_rub": md[3] if len(md) > 3 else None,
            "chg_1d": d1, "chg_1w": w1, "chg_1m": m1,
            # альфа = бумага минус индекс: очищаем движение от общего рынка
            "alpha_1d": round(d1 - idx_d1, 2) if (d1 is not None and idx_d1 is not None) else None,
            "alpha_1w": round(w1 - idx_w1, 2) if (w1 is not None and idx_w1 is not None) else None,
            "alpha_1m": round(m1 - idx_m1, 2) if (m1 is not None and idx_m1 is not None) else None,
            "turnover_rub": vals[-1] if vals else None,
            "turnover_vs_median20": vol_ratio,
        }
        # Пороги сигнала — намеренно консервативные, чтобы не звенеть каждый день.
        flags = []
        if rec["alpha_1d"] is not None and abs(rec["alpha_1d"]) >= 3:
            flags.append(f"альфа за день {rec['alpha_1d']:+.1f} п.п.")
        if rec["alpha_1w"] is not None and abs(rec["alpha_1w"]) >= 7:
            flags.append(f"альфа за неделю {rec['alpha_1w']:+.1f} п.п.")
        if vol_ratio and vol_ratio >= 2.5:
            flags.append(f"оборот x{vol_ratio} к медиане 20 дней")
        rec["flags"] = flags
        res["tickers"][sec] = rec

    res["index"] = {"IMOEX": {"chg_1d": idx_d1, "chg_1w": idx_w1, "chg_1m": idx_m1,
                              "last": idx_close[-1] if idx_close else None}}
    # Дельта к прошлому замеру — что изменилось со вчера в самих флагах
    res["delta"] = {}
    if prev:
        for sec, cur in res["tickers"].items():
            old = (prev.get("data", {}).get("tickers") or {}).get(sec) or {}
            if cur.get("ok") and old.get("ok"):
                res["delta"][sec] = {"prev_close": old.get("last_close"),
                                     "move_since_prev": _pct(cur["last_close"], old["last_close"])}
    save_snap(topic, res)
    return res


# ===========================================================================
# 2. ПУБЛИЧНЫЕ ЗАЯВЛЕНИЯ И ВЫСТУПЛЕНИЯ
# ===========================================================================
# Пресс-центры Сбера, Альфы, ВТБ и ГПБ закрыты антиботом и/или требуют доверия
# к российскому УЦ, которого нет у headless-хрома в этой среде. Рабочий обходной
# путь — открытые RSS новостных лент: там те же заявления топ-менеджеров, только
# отобранные редакцией и с датой. Frank Media — профильное издание по рознице,
# остальные ленты нужны, чтобы не пропустить выступления на конференциях.
WIRES = {
    "Frank Media":   "https://frankmedia.ru/feed",
    "Интерфакс":     "https://www.interfax.ru/rss.asp",
    "ТАСС":          "https://tass.ru/rss/v2.xml",
    "РБК":           "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "Финмаркет":     "https://www.finmarket.ru/rss/mainnews.asp",
    "Ведомости":     "https://www.vedomosti.ru/rss/news",
    "Коммерсантъ":   "https://www.kommersant.ru/RSS/news.xml",
}
# Пресс-центры первоисточника — пробуем, но не рассчитываем.
PRESS_PAGES = {
    "sber":   "https://www.sberbank.com/ru/press_center",
    "vtb":    "https://www.vtb.ru/o-banke/press-centr/novosti-i-press-relizy/",
    "tbank":  "https://www.tbank.ru/about/news/",
    "alfa":   "https://alfabank.ru/press/",
    "gpb":    "https://www.gazprombank.ru/press/",
    "mts":    "https://www.mtsbank.ru/about/press-centr/",
    "sovcom": "https://sovcombank.ru/about/news",
}
# Кого ищем в тексте новости. Ключи короче пяти символов («втб», «пск», «мпл»,
# «рвб») матчатся как ЦЕЛОЕ слово: без замыкающей границы «пск» находился в «Псков»,
# а «мпл» — в «комплексы». Длинные ключи, наоборот, ищутся по началу слова, чтобы
# «райффайзен» ловил и «Райффайзенбанк».
def _player_rx(key):
    return re.compile(r"\b" + re.escape(key) + (r"\b" if len(key) <= 4 else ""), re.I | re.U)


PLAYERS = {
    "Сбер": ["сбер", "сбербанк", "греф"],
    "ВТБ": ["втб", "костин", "пьянов"],
    "Т-Банк": ["т-банк", "т-технологи", "тинькофф", "хьюз"],
    "Альфа-Банк": ["альфа-банк", "альфа банк"],
    "Газпромбанк": ["газпромбанк"],
    "Совкомбанк": ["совкомбанк", "хотимский"],
    "МТС Банк": ["мтс банк", "мтс-банк"],
    "Яндекс": ["яндекс банк", "яндекс сплит", "яндекс пэй"],
    "Wildberries": ["wildberries", "вайлдберриз", "рвб", "wb банк"],
    "Озон": ["озон банк", "ozon банк", "озон финтех"],
    "Почта Банк": ["почта банк"],
    "Райффайзен": ["райффайзен"],
    "ЦБ / регулятор": ["цб рф", "банк россии", "набиуллина", "данилов", "мпл",
                       "макропруденциальн", "полная стоимость кредита", "пск"],
}
PLAYER_RX = {k: _player_rx(k) for keys in PLAYERS.values() for k in keys}

# Про что именно. Первая версия фильтра ловила «ракетные комплексы» на подстроку
# «мпл» и «ЛДПР про лимит маткапитала» — поэтому здесь границы слов, а не «in».
CREDIT_RX = re.compile(
    r"\b("
    r"кредитован\w*|кредитк\w*|кредитн\w*|потребкредит\w*|необеспеченн\w*|"
    r"рассрочк\w*|BNPL|POS-кредит\w*|автокредит\w*|микрозайм\w*|займ\w*|"
    r"просроч\w*|цесси\w*|коллектор\w*|коллекшн|взыскан\w*|"
    r"скоринг\w*|андеррайтинг\w*|макропруденциальн\w*|МПЛ|ПСК|"
    r"полной\s+стоимости\s+кредита|риск-аппетит\w*|"
    r"розничн\w*\s+портфел\w*|кредитн\w*\s+портфел\w*|"
    r"ипотек\w*|закредитованн\w*|долгов\w*\s+нагрузк\w*|ПДН"
    r")\b", re.I | re.U)
# Дешёвый стоп-лист: «кредитный рейтинг» и «рейтинговое агентство» — это про
# облигации эмитента, а не про розничный кредит; на выборке 535 новостей они и были
# основным остаточным шумом.
NOISE_RX = re.compile(r"кредитн\w*\s+рейтинг|рейтингов\w*\s+агентств|"
                      r"кредитн\w*\s+нот|синдицированн\w*\s+кредит", re.I | re.U)
# Старый плоский список оставлен: им пользуется дифф пресс-центров, где нужен
# именно грубый отбор строк-кандидатов, а не точная классификация новости.
TOPIC_WORDS = [
    "кредит", "кредитован", "кредитк", "кредитн", "рассрочк", "bnpl", "pos-кредит",
    "автокредит", "ипотек", "потребительск", "необеспеченн", "розничн портфел",
    "риск-аппетит", "просрочк", "резерв", "цессия", "коллект", "взыскан",
    "лимит", "грейс", "макропруденц", "скоринг", "одобрен", "ставк",
]


def _rss_items(xml):
    """Разбор RSS/Atom без внешних библиотек: нам нужны только title/link/date."""
    out = []
    blocks = re.findall(r"<item\b.*?</item>|<entry\b.*?</entry>", xml, re.S | re.I)
    for b in blocks:
        def pick(tag):
            m = re.search(rf"<{tag}[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>", b, re.S | re.I)
            return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        link = pick("link")
        if not link:
            m = re.search(r'<link[^>]+href="([^"]+)"', b, re.I)
            link = m.group(1) if m else ""
        link = re.sub(r"^<!\[CDATA\[|\]\]>$", "", link)
        out.append({
            "title": re.sub(r"<[^>]+>", "", pick("title")),
            "link": link,
            "date": pick("pubDate") or pick("updated") or pick("published"),
            "summary": re.sub(r"<[^>]+>", " ", pick("description") or pick("summary"))[:400],
        })
    return out


PRESS_TXT = "press_center"      # префикс файлов-снимков пресс-центров


def _press_center(bank, url):
    """Снимок текста пресс-центра + новые строки относительно прошлого захода.

    Разбирать вёрстку каждого пресс-центра отдельно — работа на выброс: банки
    перекраивают их несколько раз в год. Поэтому берём текст целиком, храним
    снимок и показываем ДИФФ: новые строки и есть новые заголовки."""
    html, err = core.get(url)
    text = core.to_text(html) if html else ""
    how = "http"
    if len(text) < 4000:
        html2, err2 = render_dom(url)
        if html2:
            t2 = core.to_text(html2)
            if any(m in t2 for m in ("Forbidden", "Privacy error", "NET::ERR",
                                     "Ваше подключение не защищено")):
                err = "антибот или ошибка сертификата в headless-браузере"
            elif len(t2) > len(text):
                text, how, err = t2, "браузер", None
        else:
            err = err or err2
    ok = len(text) >= 4000
    rec = {"ok": ok, "chars": len(text), "how": how if ok else None, "url": url,
           "error": None if ok else (err or "страница отдаёт только навигацию (JS)")}
    if not ok:
        return rec, []
    text = core.denoise(text)
    old = core.load_snap(PRESS_TXT, bank)
    new_lines = []
    if old and old.get("text"):
        picked, total = core.diff(old["text"], text, TOPIC_WORDS, limit=25)
        new_lines = [l[1:].strip() for l in picked
                     if l.startswith("+") and len(l) > 25]
        rec["changed_lines"] = total
    else:
        rec["changed_lines"] = None
        rec["note"] = "первый снимок — новые заголовки появятся со второго запуска"
    core.save_snap(PRESS_TXT, bank, url, text)
    return rec, new_lines


def press(max_per_wire=150):
    """Заявления и выступления: открытые ленты + диффы пресс-центров первоисточника.

    Три корзины, потому что у них разная ценность:
      hits    — игрок + розничный кредит: прямое попадание, читать обязательно;
      market  — тема без конкретного игрока: регуляторика, рынок, статистика ЦБ;
      mention — игрок без темы: фон, нужен чтобы не пропустить перестановки и M&A.
    """
    topic = "press"
    prev = load_prev(topic)
    seen_before = set((prev or {}).get("data", {}).get("seen_links", []))

    hits, market, mentions, wires_stat, seen_links = [], [], [], {}, []
    for wire, url in WIRES.items():
        xml, err = _open_get(url, timeout=25, tries=3)
        if not xml:
            wires_stat[wire] = {"ok": False, "error": err}
            continue
        items = _rss_items(xml)[:max_per_wire]
        wires_stat[wire] = {"ok": True, "items": len(items)}
        for it in items:
            if not it["link"]:
                continue
            seen_links.append(it["link"])
            blob = (it["title"] + " " + it["summary"]).lower()
            has_topic = bool(CREDIT_RX.search(blob)) and not NOISE_RX.search(blob)
            who = [p for p, keys in PLAYERS.items()
                   if any(PLAYER_RX[k].search(blob) for k in keys)]
            if not has_topic and not who:
                continue
            rec = {"wire": wire, "who": who, "title": it["title"], "date": it["date"],
                   "link": it["link"], "summary": it["summary"][:280],
                   "new": it["link"] not in seen_before}
            if has_topic and who:
                hits.append(rec)
            elif has_topic:
                market.append(rec)
            else:
                mentions.append(rec)

    press_stat, press_new = {}, {}
    for bank, url in PRESS_PAGES.items():
        try:
            rec, new_lines = _press_center(bank, url)
        except Exception as e:
            rec, new_lines = {"ok": False, "url": url,
                              "error": f"{type(e).__name__}: {str(e)[:60]}"}, []
        press_stat[bank] = rec
        if new_lines:
            press_new[bank] = new_lines

    # Компактный счётчик «сколько раз игрок засветился по кредитной теме».
    # Сами списки материалов из истории вычищаются как тяжёлые, а этот счётчик
    # остаётся — по нему и считается порог «три и более упоминания за неделю».
    hit_counts = {}
    for h in hits:
        for w in h["who"]:
            hit_counts[w] = hit_counts.get(w, 0) + 1

    data = {"wires": wires_stat, "press_centers": press_stat,
            "press_center_new_lines": press_new, "hit_counts": hit_counts,
            "hits": hits, "market": market, "mentions": mentions,
            "new_hits": [h for h in hits if h["new"]],
            "seen_links": seen_links[-4000:],
            "baseline": "первый замер: отметка «новое» появится со второго запуска"
                        if not prev else f"«новое» относительно замера {prev['date']}"}
    save_snap(topic, data)
    return data


# ===========================================================================
# 3. ВАКАНСИИ КАК УТЕЧКА СТРАТЕГИИ
# ===========================================================================
# Оба пути на hh закрыты — подробности в комментарии к HH_TOKEN ниже. Код готов
# и включается сам, как только у радара появится токен приложения.
# Идентификаторы работодателей проверены фактическим запросом hh.ru/employer/<id>:
# заголовок og:title отдаёт название компании, по нему и сверялись.
HH_EMPLOYERS = {
    "sber":   (3529,  "СБЕР"),
    "tbank":  (78638, "Т-Банк"),
    "alfa":   (80,    "Альфа-Банк"),
    "vtb":    (4181,  "Банк ВТБ (ПАО)"),
    "gpb":    (3388,  "Газпромбанк"),
    "sovcom": (7944,  "Совкомбанк"),
    "mts":    (4496,  "МТС Банк"),
    "yandex": (1740,  "Яндекс"),
    "wb":     (87021, "RWB (Wildberries & Russ)"),
    # Отдельного работодателя «свой банк (own)» на hh нет — вакансии банка
    # публикуются под общим работодателем Ozon, поэтому по нему счётчик
    # темы будет завышен вкладом маркетплейса. Учитывать при сравнении.
    "ozon":   (2180,  "Ozon"),
    "reni":   (5923,  "Ренессанс Банк"),
    "otp":    (4394,  "ОТП Банк"),
    "rshb":   (58320, "Россельхозбанк"),
    "rsb":    (586,   "Банк Русский Стандарт"),
}
# Не нашлись на hh поиском по названию компании — держим явным списком,
# чтобы «нет данных» не выглядело как «ноль вакансий».
HH_MISSING = {
    "pochta": "Почта Банк — работодателя с таким названием на hh.ru не найдено "
              "(розница интегрирована в периметр ВТБ)",
}
# Темы. Морфология у hh включена по умолчанию, кавычки задают фразу.
HH_TOPICS = {
    "BNPL/рассрочка":     'BNPL OR рассрочка OR "POS-кредитование" OR "плати частями"',
    "кредитные карты":    '"кредитная карта" OR "кредитные карты"',
    "кредит наличными":   '"кредит наличными" OR "потребительский кредит"',
    "автокредит":         'автокредит OR автокредитование',
    "скоринг/риск-модели": 'скоринг OR андеррайтинг OR "кредитный риск" OR "риск-модель"',
    "коллекшн":           'коллекшн OR взыскание OR "просроченная задолженность"',
}
_NUM = re.compile(r"Найден[оаы]\s+([\d\u00a0\u2009\u202f ]+)\s+ваканс", re.I)

# ВАЖНО про доступ. Оба пути на hh закрыты, и это надо знать до того, как строить
# на них процесс:
#  • api.hh.ru — отдаёт 403 (ddos-guard). Публичный поиск по вакансиям с 2024 года
#    требует зарегистрированного приложения: dev.hh.ru → client_id/secret → токен.
#    Токен бесплатный, но выдаётся на юрлицо/аккаунт — это действие владельца.
#  • hh.ru/search/vacancy?... — страница физически открывается и счётчик «Найдено N»
#    в ней есть (проверено: employer_id=78638 & BNPL → 17, employer_id=3529 &
#    «кредитная карта» → 11). НО в robots.txt hh для User-agent: * стоит
#    «Disallow: *?*» — то есть любой URL с параметрами запрещён к обходу.
#    Поисковый URL без параметров не бывает, значит весь HTML-поиск закрыт.
#    Ходить туда вопреки robots — риск блокировки IP и претензии от hh, ради
#    источника, который легально открывается токеном. Поэтому по умолчанию НЕ ходим.
HH_TOKEN = os.environ.get("HH_TOKEN", "").strip()
HH_APP_UA = os.environ.get("HH_APP_UA", "KreditRadar/1.0 (radar@example.com)")
# Аварийный тумблер на случай, если владелец сознательно решит игнорировать robots
# hh. По умолчанию выключен и включаться должен осознанно, а не «чтобы заработало».
HH_HTML_DESPITE_ROBOTS = os.environ.get("HH_HTML_DESPITE_ROBOTS") == "1"


def _hh_api_count(params):
    """Число вакансий через официальный API. Требует токена приложения."""
    p = dict(params); p.setdefault("area", 113); p["per_page"] = 0
    url = "https://api.hh.ru/vacancies?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {HH_TOKEN}", "HH-User-Agent": HH_APP_UA,
        "User-Agent": HH_APP_UA, "Accept": "application/json"})
    host = core._host(url)
    wait = core.PAUSE - (time.time() - core._LAST_HIT.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    try:
        with urllib.request.urlopen(req, timeout=25, context=core._CTX) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("found"), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} (токен недействителен или приложение не одобрено)"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"
    finally:
        core._LAST_HIT[host] = time.time()


def _hh_html_count(params):
    """Число вакансий из публичного HTML-поиска. Работает, но запрещено robots.txt —
    вызывается только при HH_HTML_DESPITE_ROBOTS=1."""
    p = dict(params); p.setdefault("area", 113)      # 113 = вся Россия
    url = "https://hh.ru/search/vacancy?" + urllib.parse.urlencode(p)
    html, err = (_open_get(url, timeout=30) if HH_HTML_DESPITE_ROBOTS
                 else core.get(url, timeout=30))
    if not html:
        return None, err
    text = core.to_text(html)
    m = _NUM.search(text)
    if m:
        return int(re.sub(r"[^\d]", "", m.group(1))), None
    if "не найдено" in text.lower():
        return 0, None
    return None, "счётчик не найден в разметке (hh поменял вёрстку?)"


def _hh_count(params):
    """Один счётчик. Сначала API (если есть токен), иначе HTML (если разрешён)."""
    if HH_TOKEN:
        return _hh_api_count(params)
    if HH_HTML_DESPITE_ROBOTS:
        return _hh_html_count(params)
    return None, "нет доступа: api.hh.ru требует токен, HTML-поиск закрыт robots.txt"


def hh_access_status():
    """Честный отчёт о доступности hh — чтобы «нет данных» не путали с «нет найма»."""
    if HH_TOKEN:
        n, e = _hh_api_count({"employer_id": HH_EMPLOYERS["tbank"][0]})
        return {"mode": "api.hh.ru по токену", "ok": n is not None, "probe": n, "error": e}
    if HH_HTML_DESPITE_ROBOTS:
        return {"mode": "HTML-поиск в обход robots.txt (включён вручную)", "ok": True,
                "warning": "hh.ru запрещает обход URL с параметрами; риск блокировки"}
    return {"mode": "нет доступа", "ok": False,
            "error": "api.hh.ru → 403 без токена приложения; "
                     "hh.ru/search/vacancy?... → запрещён robots.txt (Disallow: *?*)",
            "what_owner_must_do": [
                "Зарегистрировать приложение на dev.hh.ru (бесплатно), получить "
                "client_id/client_secret и application-токен.",
                "Положить токен в переменную окружения HH_TOKEN, а контакт "
                "приложения — в HH_APP_UA (hh требует его в заголовке HH-User-Agent).",
                "После этого модуль сам переключится на api.hh.ru: тот же набор "
                "счётчиков, но легально и без риска блокировки.",
            ]}


def jobs(banks=None, topics=None):
    """Счётчики вакансий по банку и теме + динамика к прошлому и недельному замеру."""
    topic_key = "jobs"
    prev = load_prev(topic_key)
    prev_data = (prev or {}).get("data", {}).get("banks", {})
    hist = load_history(topic_key, days=45)
    # Замер недельной давности — для «за неделю», а не только «со вчера».
    week_ago, edge = None, (core.msk() - timedelta(days=7)).strftime("%Y-%m-%d")
    for r in hist:
        if r["date"] <= edge:
            week_ago = r
    # Пока недели истории нет, сравниваем с прошлым замером — иначе первую неделю
    # радар будет молчать и это прочитают как «ничего не происходит».
    base_kind = "неделя"
    if week_ago:
        wk = week_ago["data"].get("banks", {})
    else:
        wk, base_kind = prev_data, "прошлый замер"

    access = hh_access_status()
    out = {"source": "hh.ru", "access": access, "missing": HH_MISSING,
           "banks": {}, "signals": []}
    if not access.get("ok"):
        # Не имитируем данные и не тратим сеть впустую.
        out["baseline"] = "сбор не выполнялся: источник закрыт (см. access)"
        save_snap(topic_key, out)
        return out

    banks = banks or list(HH_EMPLOYERS)
    topics = topics or list(HH_TOPICS)
    for bk in banks:
        if bk not in HH_EMPLOYERS:
            continue
        eid, ename = HH_EMPLOYERS[bk]
        rec = {"employer_id": eid, "employer": ename, "topics": {}}
        total, err = _hh_count({"employer_id": eid})
        rec["total"] = total
        if err:
            rec["total_error"] = err
        for t in topics:
            n, e = _hh_count({"employer_id": eid, "text": HH_TOPICS[t]})
            old = ((prev_data.get(bk) or {}).get("topics") or {}).get(t, {}).get("count")
            oldw = ((wk.get(bk) or {}).get("topics") or {}).get(t, {}).get("count")
            item = {"count": n, "error": e, "base": base_kind,
                    "d_prev": (n - old) if (n is not None and old is not None) else None,
                    "d_week": (n - oldw) if (n is not None and oldw is not None) else None}
            # Доля темы в общем найме банка — нормировка. Банк может просто расти целиком.
            item["share_pct"] = round(100 * n / total, 2) if (n and total) else None
            rec["topics"][t] = item
            # Порог сигнала: рост темы к недельному замеру. Абсолютный +5 или +50%
            # (для маленьких баз процент врёт, для больших — врёт абсолют, берём оба).
            d = item["d_week"]
            if d is not None and oldw is not None:
                if d >= 5 or (oldw >= 4 and d / max(oldw, 1) >= 0.5):
                    out["signals"].append(
                        {"bank": bk, "employer": ename, "topic": t,
                         "was": oldw, "now": n, "delta": d,
                         "base": base_kind,
                         "note": "резкий набор — ранний признак запуска/масштабирования"})
                if d <= -5:
                    out["signals"].append(
                        {"bank": bk, "employer": ename, "topic": t,
                         "was": oldw, "now": n, "delta": d,
                         "base": base_kind,
                         "note": "сворачивание набора — направление притормозили"})
        out["banks"][bk] = rec
    out["baseline"] = ("первый замер: динамики нет, она появится со второго запуска"
                       if not prev else f"динамика к замеру {prev['date']}")
    save_snap(topic_key, out)
    return out


# ===========================================================================
# 4. РЕЛИЗ-НОУТЫ МОБИЛЬНЫХ ПРИЛОЖЕНИЙ
# ===========================================================================
# Стор — самый ранний открытый источник о продукте: фича попадает в «Что нового»
# за недели до пресс-релиза, а иногда и вместо него. Google Play из РФ отдаёт 404
# по приложениям российских банков (удалены), App Store — тоже по большинству;
# рабочий канал — RuStore.
RUSTORE_APPS = {
    "sber":   ("ru.sberbankmobile",                     "СберБанк Онлайн"),
    "tbank":  ("com.idamob.tinkoff.android",            "Т-Банк"),
    "alfa":   ("ru.alfabank.mobile.android",            "Альфа-Банк"),
    "vtb":    ("ru.vtb24.mobilebanking.android",        "ВТБ Онлайн"),
    "gpb":    ("ru.gazprombank.android.mobilebank.app", "Газпромбанк"),
    "sovcom": ("ru.sovcomcard.halva.v1",                "Халва — Совкомбанк"),
    "mts":    ("ru.lewis.dbo",                          "МТС Деньги"),
    "yandex": ("com.yandex.bank",                       "Яндекс Пэй / Банк"),
    "ozon":   ("ru.ozon.app.android",                   "Ozon"),
    "wb":     ("com.wildberries.ru",                    "Wildberries"),
}
APPSTORE_IDS = {          # что реально осталось в российском App Store (raif снят 09.09.2026)
}
_RS_VER = re.compile(r"Что нового\s*\n\s*Версия:\s*\n\s*(.+?)\s*\n\s*Дата:\s*\n\s*(.+?)\s*\n(.*?)\n\s*(?:История версий|Поддержка приложения)",
                     re.S)


def _rustore(pkg):
    url = f"https://www.rustore.ru/catalog/app/{pkg}"
    html, err = core.get(url, timeout=30)
    if not html:
        return {"ok": False, "error": err, "url": url}
    t = core.to_text(html)
    m = _RS_VER.search(t)
    rec = {"ok": bool(m), "url": url}
    if m:
        rec.update(version=m.group(1).strip(), date=m.group(2).strip(),
                   notes=re.sub(r"\s+", " ", m.group(3)).strip()[:900])
    else:
        rec["error"] = "блок «Что нового» не разобрался"
    r = re.search(r"Рейтинг:\s*\n(.+?)\n(.+?)\s*оцен", t)
    if r:
        rec["rating"], rec["ratings_count"] = r.group(1).strip(), r.group(2).strip()
    d = re.search(r"([\d  ]+\+?)\s*\n\s*Скачали за месяц", t)
    if d:
        rec["downloads_month"] = d.group(1).strip()
    return rec


def _appstore(track_id):
    u = f"https://itunes.apple.com/lookup?id={track_id}&country=ru"
    raw, err = _open_get(u, timeout=25)
    if not raw:
        return {"ok": False, "error": err}
    try:
        r = json.loads(raw)["results"][0]
    except Exception as e:
        return {"ok": False, "error": f"нет в российском App Store ({e})"}
    return {"ok": True, "name": r.get("trackName"), "version": r.get("version"),
            "date": (r.get("currentVersionReleaseDate") or "")[:10],
            "notes": re.sub(r"\s+", " ", r.get("releaseNotes") or "")[:900],
            "url": r.get("trackViewUrl")}


# Слова, ради которых мы вообще читаем релиз-ноуты.
APP_SIGNAL_WORDS = ["рассрочк", "части", "bnpl", "кредит", "лимит", "займ", "долям",
                    "сплит", "кредитн", "карта рассрочки", "оформ", "одобрен",
                    "подписк", "кешбэк", "кэшбэк", "маркетплейс", "оплата частями"]


def apps():
    """Версии и «Что нового» из сторов + отметка о новой версии со прошлого замера."""
    topic = "apps"
    prev = load_prev(topic)
    old = (prev or {}).get("data", {}).get("rustore", {})
    out = {"rustore": {}, "appstore": {}, "google_play": {}, "new_releases": []}

    for bk, (pkg, title) in RUSTORE_APPS.items():
        rec = _rustore(pkg)
        rec["package"], rec["title"] = pkg, title
        was = (old.get(bk) or {}).get("version")
        rec["prev_version"] = was
        rec["is_new"] = bool(rec.get("version") and was and rec["version"] != was)
        if rec.get("ok"):
            low = (rec.get("notes") or "").lower()
            rec["credit_related"] = [w for w in APP_SIGNAL_WORDS if w in low]
        out["rustore"][bk] = rec
        if rec["is_new"]:
            out["new_releases"].append({"bank": bk, "store": "RuStore",
                                        "from": was, "to": rec["version"],
                                        "notes": rec.get("notes", "")[:300]})

    for bk, tid in APPSTORE_IDS.items():
        out["appstore"][bk] = _appstore(tid)
    # Проверяем и фиксируем факт, а не предполагаем: Google Play из РФ по банкам — 404.
    probe = "com.idamob.tinkoff.android"
    _, gerr = core.get(f"https://play.google.com/store/apps/details?id={probe}&hl=ru&gl=RU")
    out["google_play"] = {"ok": False, "probe": probe,
                          "error": gerr or "нет данных",
                          "note": "приложения российских банков удалены из Google Play; "
                                  "канал нерабочий, RuStore его замещает"}
    out["baseline"] = "первый замер: новые релизы будут видны со второго запуска" \
        if not prev else f"сравнение с замером {prev['date']}"
    save_snap(topic, out)
    return out


# ===========================================================================
# 5. ПОИСКОВЫЙ СПРОС
# ===========================================================================
# Здесь честный отрицательный результат важнее имитации: подставлять «примерные»
# частотности вместо реальных — прямой путь к неверному выводу у CRO.
DEMAND_PROBES = {
    "Яндекс Вордстат": "https://wordstat.yandex.ru/?region=all&view=graph&words=%D1%80%D0%B0%D1%81%D1%81%D1%80%D0%BE%D1%87%D0%BA%D0%B0",
    "Google Trends (explore API)": "https://trends.google.com/trends/api/explore?hl=ru&tz=-180&req=%7B%22comparisonItem%22%3A%5B%7B%22keyword%22%3A%22bnpl%22%2C%22geo%22%3A%22RU%22%2C%22time%22%3A%22today%2012-m%22%7D%5D%2C%22category%22%3A0%2C%22property%22%3A%22%22%7D",
    "Google Trends (RSS трендов)": "https://trends.google.com/trending/rss?geo=RU",
    "Яндекс Радар": "https://radar.yandex.ru/",
}


def search_demand():
    """Проверка доступности источников спроса. Данные не выдумываем — только статус."""
    topic = "search_demand"
    out = {"probes": {}, "verdict": "", "what_owner_must_provide": []}
    for name, url in DEMAND_PROBES.items():
        html, err = core.get(url, timeout=25)
        rec = {"ok": bool(html), "error": err, "bytes": len(html or "")}
        if html and name.startswith("Яндекс Вордстат"):
            t = core.to_text(html)
            # Страница открывается, но цифр на ней нет — только описание сервиса.
            has_numbers = bool(re.search(r"\b\d[\d  ]{2,}\b", t))
            rec.update(ok=False, has_numbers=has_numbers,
                       error="страница отдаёт только описание сервиса; частотности "
                             "рендерятся после авторизации в Яндекс ID")
        out["probes"][name] = rec
    out["verdict"] = ("Открытой замены Вордстату нет. Google Trends по РФ нерелевантен "
                      "(доля Google в поиске мала и explore-API отдаёт 429 без cookie), "
                      "RSS отдаёт только общие тренды дня без привязки к запросу.")
    out["what_owner_must_provide"] = [
        "Доступ к Яндекс Вордстат: логин Яндекс ID с подтверждённым доступом "
        "(данные забираются только через интерфейс — открытого API у Вордстата нет).",
        "Либо аккаунт Яндекс.Директ с API-токеном: метод Keywords/ReportService даёт "
        "частотности программно и легально, это платный рекламный кабинет.",
        "Либо выгрузка из внутренней системы веб-аналитики банка по брендовым и "
        "продуктовым запросам — если у своего банка (own) уже закуплен Wordstat/Директ.",
        "Конкретно нужны недельные частотности по ~40 запросам: «рассрочка», "
        "«карта рассрочки», «кредитная карта без процентов», «долями», «сплит», "
        "«плати частями», «халва», «bnpl», плюс брендовые «озон банк кредит» и т.п.",
    ]
    save_snap(topic, out)
    return out


# ===========================================================================
# 6. РЕКЛАМА, МАРКЕТИНГ И ТАРИФНЫЕ ДОКУМЕНТЫ
# ===========================================================================
# Витрина — это обещание, тариф — обязательство. Условия меняют в PDF, и часто
# раньше, чем на витрине. У ВТБ дата вшита прямо в имя файла (kk_tarif_30.08.2026.pdf) —
# появление нового файла и есть событие, читать содержимое для сигнала не обязательно.
_PDF_RX = re.compile(r'href="([^"]+?\.pdf(?:\?[^"]*)?)"', re.I)
_DATE_IN_NAME = re.compile(r"(\d{2})[._-](\d{2})[._-](\d{4})")


def _abs_url(base, href):
    return urllib.parse.urljoin(base, href.replace("&amp;", "&"))


# Что вообще считаем тарифным документом. Ограничение не косметическое:
# на отрендеренной странице банка в подвале и меню висят сотни PDF (лицензии,
# памятки, политики, формы заявлений), и без фильтра «новых документов» будет
# столько же, сколько страница в этот раз успела дорисовать. Замерено: три
# прогона подряд дали 58, 44 и 1019 документов — последний целиком из-за того,
# что тяжёлые страницы Сбера в тот раз отрендерились полностью.
TARIFF_RX = re.compile(
    r"tarif|тариф|usloviy|uslov|условия|pravil|правил|condition|"
    r"kredit|credit|karta|card|rassroch|dolyam|akci|promo|proc(ent)?|stavk",
    re.I)
# Если со страницы пришло больше этого числа PDF — почти наверняка мы поймали
# весь подвал сайта, а не документы продукта. Такая страница в дельту не идёт.
PAGE_PDF_CAP = 40


def ads_tariffs(pages=None, head_limit=60):
    """Инвентаризация тарифных PDF на продуктовых страницах + дельта к прошлому замеру.

    Дельта считается ТОЛЬКО по документам, похожим на тариф/условия, и только по
    страницам, которые в этот раз пришли тем же способом (http или браузер), что
    и в прошлый. Смена способа меняет DOM целиком, и любая дельта после неё —
    артефакт нашего сбора, а не действие банка."""
    topic = "tariffs"
    prev = load_prev(topic)
    old_data = (prev or {}).get("data", {})
    old_docs = old_data.get("docs", {})
    old_pages = old_data.get("pages", {})

    if pages is None:
        pages = []
        if sources:
            for bk, cfg in sources.BANKS.items():
                for prod, url in (cfg.get("pages") or {}).items():
                    pages.append((bk, prod, url))
        else:
            return {"ok": False, "error": "sources.py не импортировался"}

    docs, page_stat, checked = {}, {}, 0
    for bk, prod, url in pages:
        html, err = core.get(url, timeout=30)
        how = "http"
        if not html or len(html) < 20000:
            # Рендер здесь укорочен намеренно: страниц 30, и полный бюджет ядра
            # (12 с ожидания + 55 с таймаут) растягивает обход за 40 минут.
            # Ссылки на PDF появляются в DOM рано, ждать полной отрисовки незачем.
            h2, e2 = render_dom(url, wait_ms=7000, timeout=25)
            if h2 and len(h2) > len(html or ""):
                html, how, err = h2, "браузер", None
            else:
                err = err or e2
        key = f"{bk}/{prod}"
        if not html:
            page_stat[key] = {"ok": False, "error": err, "url": url}
            continue
        raw = sorted({_abs_url(url, h) for h in _PDF_RX.findall(html)})
        found = [u for u in raw if TARIFF_RX.search(u)]
        noisy = len(raw) > PAGE_PDF_CAP
        page_stat[key] = {"ok": True, "how": how, "pdf_all": len(raw),
                          "pdf_tariff": len(found), "noisy": noisy, "url": url}
        if noisy:
            # Страницу инвентаризуем, но в дельту не пускаем: см. комментарий выше.
            continue
        for u in found:
            d = docs.setdefault(u, {"banks": set(), "products": set(), "pages": set()})
            d["banks"].add(bk); d["products"].add(prod); d["pages"].add(key)

    # HEAD по документам: размер и дата — дёшево и достаточно для детекции правки.
    # Лимит есть, поэтому порядок важен: сначала то, что похоже на тариф или условия.
    prio = re.compile(r"tarif|тариф|usloviy|условия|kredit|credit|card|karta", re.I)
    order = sorted(docs, key=lambda u: (0 if prio.search(u) else 1, u))
    for u in order[:head_limit]:
        meta, herr = _head(u)
        docs[u].update(meta or {})
        if herr:
            docs[u]["error"] = herr
        checked += 1
    for u, d in docs.items():
        d["banks"] = sorted(d["banks"]); d["products"] = sorted(d["products"])
        d["pages"] = sorted(d["pages"])
        m = _DATE_IN_NAME.search(u.rsplit("/", 1)[-1])
        d["date_in_name"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

    def comparable(page_keys):
        """Страница сопоставима, если сегодня открылась, не «шумная» и пришла тем же
        способом, что и в прошлый раз."""
        for k in page_keys:
            now, was = page_stat.get(k) or {}, old_pages.get(k) or {}
            if now.get("ok") and not now.get("noisy") and was.get("ok") \
               and now.get("how") == was.get("how"):
                return True
        return False

    new_docs, changed, gone, unverified = [], [], [], []
    for u, d in docs.items():
        o = old_docs.get(u)
        if o is None and old_docs:
            (new_docs if comparable(d["pages"]) else unverified).append(
                {"url": u, "banks": d["banks"], "products": d["products"],
                 "date_in_name": d.get("date_in_name")})
        elif o and d.get("size") and o.get("size") and d["size"] != o["size"]:
            # Размер меряется HEAD-ом самого файла и от способа съёма страницы
            # не зависит — здесь фильтр по сопоставимости не нужен.
            changed.append({"url": u, "banks": d["banks"],
                            "size_was": o["size"], "size_now": d["size"],
                            "pct": round(100 * (d["size"] / o["size"] - 1), 2),
                            "modified": d.get("modified")})
    for u, o in (old_docs or {}).items():
        if u in docs:
            continue
        (gone if comparable(o.get("pages") or []) else unverified).append(
            {"url": u, "banks": o.get("banks"), "was_size": o.get("size")})

    data = {"pages": page_stat, "docs": docs, "checked_head": checked,
            "new_docs": new_docs, "changed_docs": changed, "gone_docs": gone,
            "unverified_docs": unverified,
            "note": "в дельту попадают только документы, похожие на тариф/условия, "
                    "и только со страниц, снятых тем же способом, что и в прошлый раз",
            "baseline": "первый замер: дельта появится со второго запуска" if not old_docs
                        else f"дельта к замеру {prev['date']}"}
    save_snap(topic, data)
    return data


# ===========================================================================
# Адаптер для мозга радара
# ===========================================================================
def _fmt_num(x, suffix=""):
    return "—" if x is None else f"{x:+.1f}{suffix}" if isinstance(x, float) else f"{x}{suffix}"


def context_block(limit=2200):
    """Короткая текстовая сводка свежих сигналов — подмешивается в контекст аналитика.

    Читает ПОСЛЕДНИЕ сохранённые замеры, ничего не собирая заново: блок должен быть
    дешёвым и не зависеть от того, доступен ли сейчас источник. Если замера нет —
    так и пишем, а не молчим: аналитик должен знать, что данных нет, иначе он
    решит, что «сигналов не было».
    """
    L = ["РЫНОЧНЫЕ СИГНАЛЫ (открытые источники, замер радара).",
         "Читать так: альфа = движение бумаги МИНУС индекс Мосбиржи; само по себе "
         "движение котировки о конкуренте не говорит ничего, говорит только альфа.",
         "Динамика вакансий и версий приложений — опережающие индикаторы: они "
         "появляются раньше пресс-релиза. Это гипотеза, а не факт запуска.",
         ""]

    q = load_prev("quotes")
    if not q:
        L.append("КОТИРОВКИ: замера нет.")
    else:
        # Дата замера и дата закрытия торгов — разные вещи: замер обычно делается
        # ночью, а последняя торговая сессия была накануне. Берём торговую.
        rows_ok = [v for v in q["data"]["tickers"].values() if v.get("ok")]
        trade_day = max((v.get("last_date") or "") for v in rows_ok) if rows_ok else q["date"]
        L.append(f"КОТИРОВКИ (закрытие торгов {trade_day}, замер {q['date']}, ISS Мосбиржи):")
        idx = (q["data"].get("index") or {}).get("IMOEX") or {}
        L.append(f"  IMOEX: день {_fmt_num(idx.get('chg_1d'), '%')}, "
                 f"неделя {_fmt_num(idx.get('chg_1w'), '%')}, "
                 f"месяц {_fmt_num(idx.get('chg_1m'), '%')}.")
        rows = [(k, v) for k, v in q["data"]["tickers"].items() if v.get("ok")]
        rows.sort(key=lambda kv: -abs(kv[1].get("alpha_1w") or 0))
        for k, v in rows[:5]:
            L.append(f"  {v['name']} ({k}): {v['last_close']}, альфа нед "
                     f"{_fmt_num(v.get('alpha_1w'), ' п.п.')}, мес "
                     f"{_fmt_num(v.get('alpha_1m'), ' п.п.')}"
                     + (f" — {'; '.join(v['flags'])}" if v.get("flags") else ""))
        L.append("")

    j = load_prev("jobs")
    if not j:
        L.append("НАЁМ: замера нет.")
    else:
        acc = j["data"].get("access") or {}
        sig = j["data"].get("signals") or []
        if not acc.get("ok"):
            # Закрытый источник и «нет сдвигов» — разные вещи. Путать нельзя:
            # аналитик обязан знать, что тут просто нет данных.
            L.append("НАЁМ: данных нет, источник закрыт (hh.ru требует токен "
                     "приложения). НЕ трактовать как отсутствие найма.")
        elif sig:
            L.append("НАЁМ (hh.ru, дельта к замеру недельной давности):")
            for s_ in sig[:6]:
                L.append(f"  {s_['employer']} / {s_['topic']}: {s_['was']} → {s_['now']} "
                         f"({s_['delta']:+d}) — {s_['note']}")
        else:
            L.append("НАЁМ (hh.ru): значимых сдвигов по темам нет"
                     + (" (первый замер, база для сравнения только формируется)."
                        if "первый замер" in str(j["data"].get("baseline")) else "."))
        L.append("")

    a = load_prev("apps")
    if a:
        nr = a["data"].get("new_releases") or []
        if nr:
            L.append("РЕЛИЗЫ ПРИЛОЖЕНИЙ (RuStore, новые версии с прошлого замера):")
            for r in nr[:5]:
                L.append(f"  {r['bank']}: {r['from']} → {r['to']}. {r['notes'][:160]}")
        else:
            L.append("РЕЛИЗЫ ПРИЛОЖЕНИЙ: новых версий с прошлого замера нет.")
        L.append("")

    t = load_prev("tariffs")
    if t:
        nd, ch = t["data"].get("new_docs") or [], t["data"].get("changed_docs") or []
        if nd or ch:
            L.append("ТАРИФНЫЕ ДОКУМЕНТЫ:")
            for d in nd[:4]:
                L.append(f"  новый PDF: {','.join(d['banks'])} / {','.join(d['products'])} "
                         f"{'(дата в имени файла ' + d['date_in_name'] + ')' if d.get('date_in_name') else ''} {d['url']}")
            for d in ch[:4]:
                L.append(f"  изменён PDF: {','.join(d['banks'])} {d['size_was']}→{d['size_now']} байт {d['url']}")
        else:
            L.append("ТАРИФНЫЕ ДОКУМЕНТЫ: изменений не зафиксировано.")
        L.append("")

    pr = load_prev("press")
    if pr:
        nh = pr["data"].get("new_hits") or pr["data"].get("hits") or []
        if nh:
            L.append("ЗАЯВЛЕНИЯ И НОВОСТИ ПО РОЗНИЧНОМУ КРЕДИТУ:")
            for h in nh[:6]:
                L.append(f"  [{h['wire']}] {', '.join(h['who']) or 'рынок'}: {h['title'][:120]}")
        pn = pr["data"].get("press_center_new_lines") or {}
        for bank, lines in list(pn.items())[:3]:
            L.append(f"  пресс-центр {bank}: " + " | ".join(lines[:3])[:200])
        L.append("")

    # Футер обрезать нельзя ни при каких обстоятельствах: это единственная защита
    # от того, что модель начнёт рассуждать о недоступных данных по памяти.
    footer = ("ЧЕГО В ЭТОМ БЛОКЕ НЕТ (не выдумывать): поисковый спрос — Вордстат "
              "требует авторизации, открытой замены нет; рекламные креативы "
              "конкурентов закрыты (ЕРИР не публичен).")
    body = "\n".join(L).rstrip()
    room = max(200, limit - len(footer) - 2)
    if len(body) > room:
        body = body[:room].rsplit("\n", 1)[0] + "\n…"
    return body + "\n\n" + footer


# ===========================================================================
# Оркестрация
# ===========================================================================
COLLECTORS = {
    "quotes":  ("Котировки и реакция рынка", quotes),
    "press":   ("Публичные заявления", press),
    "jobs":    ("Вакансии как утечка стратегии", jobs),
    "apps":    ("Релиз-ноуты приложений", apps),
    "demand":  ("Поисковый спрос", search_demand),
    "tariffs": ("Реклама и тарифные документы", ads_tariffs),
}


def collect_all(only=None):
    """Прогон всех направлений. Падение одного не останавливает остальные."""
    out = {}
    for key, (title, fn) in COLLECTORS.items():
        if only and key not in only:
            continue
        t0 = time.time()
        try:
            out[key] = {"ok": True, "title": title, "data": fn()}
        except Exception as e:
            out[key] = {"ok": False, "title": title, "error": f"{type(e).__name__}: {e}"}
        out[key]["seconds"] = round(time.time() - t0, 1)
        print(f"[{key}] {title}: {'ok' if out[key]['ok'] else out[key].get('error')} "
              f"({out[key]['seconds']} c)", file=sys.stderr)
    return out


if __name__ == "__main__":
    only = [a for a in sys.argv[1:] if not a.startswith("-")] or None
    res = collect_all(only)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
