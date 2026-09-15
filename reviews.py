# -*- coding: utf-8 -*-
"""Репутация и тональность конкурентов по открытым отзывам на агрегаторах.

Что здесь есть:
  • ratings(bank_key)        — «народный рейтинг», средняя оценка, число отзывов,
                               доля решённых обращений — ровно то, что банк
                               публикует на своей странице агрегатора;
  • recent(bank_key, limit)  — последние отзывы: дата, оценка, заголовок, текст;
  • themes(reviews)          — на какие темы жалуются и какая доля негатива в теме;
  • sentiment(reviews)       — доля 1–2 звёзд против 4–5 плюс динамика к прошлому замеру;
  • measure(bank_key)        — полный замер с записью в state/reviews/<банк>.json;
  • context_block()          — короткая сводка (~1,5 тыс. знаков) для контекста
                               аналитика, по образцу cbr.context_block(): читает
                               последний сохранённый замер, в сеть не ходит.

Правила игры (те же, что в core.py):
  • только открытые страницы, никакой авторизации и никаких приватных API;
  • robots.txt уважаем (см. robots_ok ниже — там важная оговорка про core.allowed);
  • пауза между запросами к домену берётся из core (3 секунды), её не обходим;
  • если источник закрыт — честно ставим пометку «недоступен» и причину,
    а не подставляем цифры «по памяти».

Источники и что с них реально снимается:
  banki.ru — HTML отдаётся обычным GET'ом целиком: в нём есть и агрегированные
             показатели («Народный рейтинг», % решённых), и JSON-LD со списком
             отзывов, и служебный JSON data-module-options с оценками, датами и
             ответами банка. Это основной источник по отдельным отзывам.
  sravni.ru — SSR-разметка содержит только агрегаты: общий рейтинг, число отзывов,
             долю «рекомендуют», оценки по продуктам и облако частых тем.
             Сами тексты отзывов подгружаются XHR-запросом к /proxy-reviews/…,
             а /proxy-* закрыт в robots.txt — туда мы НЕ ходим. Поэтому по sravni
             у нас агрегаты и ноль отдельных отзывов, и это осознанное ограничение.
"""
import os, re, json, time, html as _html
from html.parser import HTMLParser

import core
import sources

STATE_DIR = os.path.join(core.STATE, "reviews")
MAX_HISTORY = 90          # сколько замеров храним на банк
TEXT_LIMIT  = 700         # до скольких символов режем текст отзыва


# ============================================================================
#  1. robots.txt: свой матчер
# ============================================================================
# Почему не core.allowed(): стандартный urllib.robotparser нормализует путь
# правила через urlparse/urlunparse, и правило banki.ru «Disallow: /?»
# (запрет URL вида /?что-то, то есть корня с query) превращается в «Disallow: /»
# — запрет всего сайта. Это баг разбора, а не воля banki.ru: в том же блоке
# User-agent:* ниже идут полторы сотни точечных Disallow и несколько Allow,
# которые при тотальном запрете не имели бы смысла. Поэтому здесь свой матчер:
# с поддержкой * и $, без нормализации пути и с выбором правила по длине
# (как это делают Google и Яндекс). Он строже стандартного, а не мягче:
# правила вида «Disallow: */telecom/responses/*» он тоже понимает, а urllib — нет.
#
# TODO для core.py: этот матчер стоит поднять в core и заменить им allowed(),
# сейчас core.get() не может забрать ни одной страницы banki.ru из-за той же ошибки.

_ROBOTS_CACHE = {}


def _rule_rx(pattern):
    """Правило robots -> регулярка. * = любой кусок, $ в конце = конец URL."""
    p = pattern.strip()
    end = p.endswith("$")
    if end:
        p = p[:-1]
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in p)
    return re.compile("^" + rx + ("$" if end else ""))


def _load_robots(host):
    """Возвращает список правил (allow?, длина шаблона, регулярка) для User-agent: *.

    Группу выбираем по '*': наш User-Agent — обычный браузерный, мы не выдаём
    себя за Yandex/Googlebot и не имеем права пользоваться их послаблениями.
    """
    if host in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[host]
    groups, agents, rules, prev_agent_line = [], [], [], False
    try:
        raw = core._raw_get(f"https://{host}/robots.txt", timeout=10)
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                if not prev_agent_line:          # начался новый блок
                    if agents:
                        groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(v.lower())
                prev_agent_line = True
            elif k in ("allow", "disallow"):
                prev_agent_line = False
                if v:
                    rules.append((k == "allow", len(v), _rule_rx(v)))
        if agents:
            groups.append((agents, rules))
        rules = next((r for a, r in groups if "*" in a), [])
    except Exception:
        rules = None          # robots не забрался — ведём себя как браузер
    _ROBOTS_CACHE[host] = rules
    return rules


def robots_ok(url):
    """(можно?, причина). Правило выбирается самое длинное; при равенстве — Allow."""
    host = core._host(url)
    path = re.sub(r"^https?://[^/]+", "", url) or "/"
    rules = _load_robots(host)
    if rules is None:
        return True, "robots.txt недоступен — считаем, что можно"
    if not rules:
        return True, ""
    best = None
    for allow, ln, rx in rules:
        if rx.match(path):
            if best is None or ln > best[1] or (ln == best[1] and allow):
                best = (allow, ln, rx)
    if best is None or best[0]:
        return True, ""
    return False, "запрещено robots.txt"


def _get(url, tries=2):
    """GET с проверкой robots и паузой из core. Возвращает (html, ошибка)."""
    ok, why = robots_ok(url)
    if not ok:
        return None, why
    last = ""
    for i in range(tries):
        try:
            return core._raw_get(url).decode("utf-8", "replace"), None
        except Exception as e:
            code = getattr(e, "code", None)
            last = f"HTTP {code}" if code else f"{type(e).__name__}: {str(e)[:60]}"
            if code in (401, 403, 404, 451):
                break                      # повтор не поможет
            time.sleep(1.5 * (i + 1))
    return None, last


# ============================================================================
#  2. Разбор HTML
# ============================================================================
class _Nodes(HTMLParser):
    """Текстовые узлы по порядку и БЕЗ дедупликации.

    core.to_text() схлопывает повторы строк — для витрины это правильно, а для
    sravni смертельно: там рейтинги по продуктам идут парами «название / число»,
    и выброшенная как дубль «3.9» ломает всю привязку.
    """
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, d):
        if not self.skip:
            t = " ".join(d.split())
            if t:
                self.out.append(t)


def _nodes(html):
    p = _Nodes()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.out


def _num(s):
    """'2,52' / '8 019' / '35.21%' -> float. Нечисло -> None."""
    if s is None:
        return None
    s = str(s).replace("\xa0", " ").replace(" ", "").replace(",", ".").rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _strip_tags(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"</p\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", _html.unescape(s)).strip()


# ============================================================================
#  3. banki.ru
# ============================================================================
BANKI_URL      = "https://www.banki.ru/services/responses/bank/{slug}/"
BANKI_URL_PROD = "https://www.banki.ru/services/responses/bank/{slug}/product/{code}/"
BANKI_RESPONSE = "https://www.banki.ru/services/responses/bank/response/{id}/"

# Продукты banki.ru, которые нас интересуют (их фильтр — часть пути, не query).
BANKI_PRODUCTS = {
    "кредитная карта":  "creditcards",
    "кредит наличными": "credits",
    "автокредит":       "autocredits",
    "дебетовая карта":  "debitcards",
}
BANKI_PRODUCT_NAME = {v: k for k, v in BANKI_PRODUCTS.items()}


def _banki_agg(html):
    """Агрегаты со страницы: JSON-LD + текстовый блок «Народный рейтинг»."""
    agg = {}
    # 3.1 JSON-LD — самое точное: ratingValue с полной точностью и reviewCount
    for blk in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                          html, re.S):
        try:
            d = json.loads(blk)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") == "Organization":
            agg["bank_name"] = d.get("name")
            a = d.get("aggregateRating") or {}
            agg["rating"] = _num(a.get("ratingValue"))
            agg["reviews_total"] = int(_num(a.get("reviewCount")) or 0) or None
            break
    # 3.2 Текстовый блок — там место в рейтинге, % решённых и число ответов.
    #     Классы у banki.ru хэшированные и меняются, а подписи стабильны,
    #     поэтому идём по подписям, а не по CSS.
    n = _nodes(html)
    for i, t in enumerate(n):
        if t.endswith(" место") and i + 3 < len(n) and n[i + 1] == "из":
            agg["place"] = int(_num(t.split()[0]) or 0) or None
            m = re.search(r"(\d[\d\s]*)", n[i + 2])
            if m:
                agg["place_of"] = int(_num(m.group(1)) or 0) or None
        if t == "средняя оценка" and i:
            agg.setdefault("rating_shown", _num(n[i - 1]))
        if t == "решено проблем" and i:
            agg["resolved_pct"] = _num(n[i - 1])
        if t.startswith("отзыв") and i and _num(n[i - 1]) is not None:
            agg.setdefault("reviews_shown", int(_num(n[i - 1])))
        if t.startswith("ответ") and i and _num(n[i - 1]) is not None:
            agg.setdefault("answers", int(_num(n[i - 1])))
    return agg


def _banki_reviews(html, product_code=None):
    """Отзывы из служебного JSON data-module-options (он же кормит React-виджет).

    В нём на каждый отзыв: id, заголовок, полный текст, оценка (может быть null —
    «Без оценки»), дата, ответ банка, признак «зачтён в рейтинг» и статус решения.
    """
    out, more = [], False
    for blob in re.findall(r"data-module-options='([^']*)'", html):
        s = _html.unescape(blob)
        if '"perPage"' not in s:
            continue
        try:
            resp = json.loads(s)["responses"]
        except Exception:
            continue
        more = bool(resp.get("hasMorePages"))
        code = product_code or resp.get("product")
        for r in resp.get("data", []):
            txt = _strip_tags(r.get("text"))
            out.append({
                "src":       "banki.ru",
                "id":        r.get("id"),
                "url":       BANKI_RESPONSE.format(id=r.get("id")),
                "date":      (r.get("dateCreate") or "")[:10],
                "rating":    r.get("grade"),          # 1..5 или None
                "title":     (r.get("title") or "").strip(),
                "text":      txt[:TEXT_LIMIT],
                "text_full_len": len(txt),
                "product":     BANKI_PRODUCT_NAME.get(code) if code else None,
                "product_src": "фильтр агрегатора" if code else None,
                "counted":   bool(r.get("isCountable")),      # зачтён в рейтинг
                "resolved":  r.get("resolutionIsApproved"),   # True/False/None
                "answered":  bool(r.get("agentAnswerText")),
            })
        break
    return out, more


def banki_ratings(slug):
    """Агрегаты banki.ru по банку. Возвращает (данные, ошибка)."""
    url = BANKI_URL.format(slug=slug)
    html, err = _get(url)
    if not html:
        return None, err
    agg = _banki_agg(html)
    agg["url"] = url
    if agg.get("rating") is None and agg.get("place") is None:
        # Блока «Народный рейтинг» на странице нет. Два разных случая:
        #   • свой банк (own) — карточка заведена, отзывов вообще нет;
        #   • Почта Банк — отзывы есть, но банк выведен из народного рейтинга
        #     (последние отзывы датированы апрелем 2026).
        # Отличить их можно по тому, вернул ли banki_recent() хоть один отзыв.
        agg["note"] = ("блок «Народный рейтинг» на странице не опубликован — "
                       "банк вне рейтинга либо отзывов нет")
    return agg, None


def banki_recent(slug, limit=30, product=None):
    """Последние отзывы с banki.ru. product — ключ из BANKI_PRODUCTS или None.

    Пагинация: ?page=N, по 25 отзывов на страницу. robots.txt banki.ru запрещает
    только фильтры вида ?f[...] — постраничная навигация разрешена, но всё равно
    проходит через robots_ok().
    """
    code = BANKI_PRODUCTS.get(product) if product else None
    base = (BANKI_URL_PROD.format(slug=slug, code=code) if code
            else BANKI_URL.format(slug=slug))
    out, page, err = [], 1, None
    while len(out) < limit and page <= 8:
        url = base if page == 1 else f"{base}?page={page}"
        html, e = _get(url)
        if not html:
            err = e if page == 1 else None      # обрыв на 2-й странице не фатален
            break
        chunk, more = _banki_reviews(html, code)
        if not chunk:
            break
        out.extend(chunk)
        if not more:
            break
        page += 1
    return out[:limit], err


# ============================================================================
#  4. sravni.ru
# ============================================================================
SRAVNI_URL = "https://www.sravni.ru/bank/{slug}/otzyvy/"
# Признак «мягкого 404»: несуществующий slug отдаёт скелет без карточки банка.
SRAVNI_404 = "не найден (нет такой страницы)"


def sravni_ratings(slug):
    """Агрегаты sravni.ru: рейтинг, число отзывов, доля «рекомендуют»,
    оценки по продуктам и облако «часто упоминают».

    Отдельных отзывов здесь нет: список тянется XHR'ом к /proxy-reviews/…,
    а раздел /proxy-* закрыт в robots.txt sravni.ru — мы туда не ходим.
    """
    url = SRAVNI_URL.format(slug=slug)
    html, err = _get(url)
    if not html:
        return None, err
    n = _nodes(html)
    if "Рейтинг" not in n or "из 5" not in n:
        return None, SRAVNI_404
    d = {"url": url}
    i = n.index("Рейтинг")
    if n[i + 2] == "из 5":
        d["rating"] = _num(n[i + 1])
    # Заголовок страницы — на sravni он в предложном падеже («Отзывы об МТС Банке»),
    # приводить его к именительному не пытаемся: имя банка и так есть в sources.BANKS.
    for t in n[max(0, i - 4):i]:
        if t.startswith("Отзывы о"):
            d["page_title"] = t
    for j, t in enumerate(n):
        if t.startswith("отзыв") and j and _num(n[j - 1]) is not None:
            d.setdefault("reviews_total", int(_num(n[j - 1])))
        if t == "рекомендуют" and j and n[j - 1].endswith("%"):
            d["recommend_pct"] = _num(n[j - 1])
    # оценки по продуктам: пары «название / число», у части продуктов числа нет
    if "продуктам" in n:
        k = n.index("продуктам") + 1
        prods = {}
        while k < len(n) and n[k] not in ("Часто упоминают", "Самые полезные", "Ещё"):
            if _num(n[k]) is None:
                val = _num(n[k + 1]) if k + 1 < len(n) else None
                prods[n[k]] = val
                k += 2 if val is not None else 1
            else:
                k += 1
        d["by_product"] = prods
    # «часто упоминают»: тема -> сколько отзывов
    if "Часто упоминают" in n:
        k = n.index("Часто упоминают") + 1
        tags = {}
        while k + 1 < len(n) and n[k] not in ("Самые полезные", "Проверенные", "Банки"):
            if _num(n[k]) is None and _num(n[k + 1]) is not None:
                tags[n[k]] = int(_num(n[k + 1]))
                k += 2
            else:
                k += 1
        d["mentions"] = tags
    d["note"] = ("тексты отзывов не собираем: их отдаёт /proxy-reviews, "
                 "закрытый в robots.txt sravni.ru")
    return d, None


# ============================================================================
#  5. Публичный API по ключу банка
# ============================================================================
# Проверенные slug'и агрегаторов. Дубль того, что лежит в sources.AGG_SLUGS, но
# держим свою копию здесь: sources.py правят и другие модули радара, а завязка
# сбора отзывов на чужой файл — лишний способ тихо сломаться. Приоритет у этой
# таблицы, всё остальное добирается из sources как запасной вариант.
#
# Проверено 07–08.09.2026 запросом к живым страницам. Ловушка sravni.ru: на
# несуществующий slug он отдаёт 200 OK и скелет страницы, а не 404, поэтому
# валидность проверяется наличием карточки банка, а не HTTP-кодом.
AGG_SLUGS = {
    "sber":   dict(banki="sberbank",    sravni="sberbank-rossii"),
    "tbank":  dict(banki="tcs",         sravni="t-bank"),
    "alfa":   dict(banki="alfabank",    sravni="alfa-bank"),
    "gpb":    dict(banki="gazprombank", sravni="gazprombank"),
    "vtb":    dict(banki="vtb",         sravni="vtb"),
    "sovcom": dict(banki="sovcombank",  sravni="sovkombank"),
    "ozon":   dict(banki="ozonbank",    sravni="ozon-bank"),   # на banki.ru отзывов нет
    "yandex": dict(banki="yandexbank",  sravni="yandex-bank"),
    "mts":    dict(banki="mts-bank",    sravni="mts-bank"),
    "pochta": dict(banki="pochtabank",  sravni=None),          # вне народного рейтинга
    "raif":   dict(banki="raiffeisen",  sravni="rajffajzenbank"),
    "wb":     dict(banki="wbbank",      sravni=None),          # на sravni карточки нет
}

# Короткие имена для контекстного блока — там важна ширина строки.
SHORT_NAMES = {
    "sber": "Сбер", "tbank": "Т-Банк", "alfa": "Альфа", "gpb": "Газпромбанк",
    "vtb": "ВТБ", "sovcom": "Совкомбанк", "ozon": "свой банк (own)",
    "yandex": "Яндекс Банк", "mts": "МТС Банк", "pochta": "Почта Банк",
    "raif": "Райффайзен", "wb": "ВБ Банк",
}


def _slugs(bank_key):
    """Свои проверенные slug'и, с откатом на sources.AGG_SLUGS для незнакомых ключей."""
    if bank_key in AGG_SLUGS:
        return AGG_SLUGS[bank_key]
    try:
        return sources.AGG_SLUGS.get(bank_key) or {}
    except Exception:
        return {}


def bank_name(bank_key):
    try:
        return (sources.BANKS.get(bank_key) or {}).get("name") or SHORT_NAMES.get(bank_key, bank_key)
    except Exception:
        return SHORT_NAMES.get(bank_key, bank_key)


def ratings(bank_key):
    """Репутационные показатели банка со всех агрегаторов.

    Возвращает {"bank":..., "ts":..., "banki.ru": {...|None}, "sravni.ru": {...|None},
                "errors": {...}} — то, что реально опубликовано, без домыслов.
    """
    sl = _slugs(bank_key)
    res = {"bank": bank_key,
           "bank_name": bank_name(bank_key),
           "ts": core.msk().isoformat(timespec="seconds"),
           "banki.ru": None, "sravni.ru": None, "errors": {}}
    if sl.get("banki"):
        d, err = banki_ratings(sl["banki"])
        res["banki.ru"] = d
        if err:
            res["errors"]["banki.ru"] = err
    else:
        res["errors"]["banki.ru"] = "нет slug'а"
    if sl.get("sravni"):
        d, err = sravni_ratings(sl["sravni"])
        res["sravni.ru"] = d
        if err:
            res["errors"]["sravni.ru"] = err
    else:
        res["errors"]["sravni.ru"] = "нет slug'а"
    return res


def recent(bank_key, limit=30, product=None):
    """Последние отзывы о банке: дата, оценка, заголовок, обрезанный текст, продукт.

    Сейчас источник один — banki.ru (см. оговорку про sravni в шапке модуля).
    Продукт проставляется точно, если запрошен продуктовый срез (product=...),
    иначе угадывается по ключевым словам — и это помечено в поле product_src.
    """
    sl = _slugs(bank_key)
    out, errs = [], {}
    if sl.get("banki"):
        rev, err = banki_recent(sl["banki"], limit=limit, product=product)
        out.extend(rev)
        if err:
            errs["banki.ru"] = err
    else:
        errs["banki.ru"] = "нет slug'а"
    # Это не ошибка загрузки, а ограничение источника — помечаем отдельно,
    # чтобы не путать с падением площадки.
    errs["sravni.ru"] = ("тексты отзывов не собираются: их отдаёт /proxy-reviews, "
                         "закрытый в robots.txt sravni.ru")
    for r in out:                       # догадка о продукте там, где фильтра не было
        if not r.get("product"):
            g = _guess_product(r)
            if g:
                r["product"], r["product_src"] = g, "по ключевым словам"
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out[:limit], errs


# ============================================================================
#  6. Темы жалоб
# ============================================================================
# Темы — те, что важны для розничного кредитного риска. Каждая тема это
# (что ищем) и опционально (что обязано встретиться рядом): «страховка» сама по
# себе — не жалоба, жалоба — «навязали страховку», поэтому у неё есть второе условие.
THEMES = [
    ("кредитная карта",       r"кредитн\w*\s+карт|кредитк|кред\.\s*карт", None),
    ("рассрочка",             r"рассрочк|в\s*дол[ья]м|халв\b|халв\w|подел[ия]\b|сплит\b|плат\w+\s+частями|bnpl", None),
    ("кредит наличными",      r"кредит\w*\s+наличн|наличным[ии]?\b|потребительск\w*\s+кредит|потребкредит", None),
    ("автокредит",            r"автокредит|авто\s*кредит", None),
    ("лимит",                 r"лимит", None),
    ("ставка",                r"ставк\w|процент\w*\s+годов|годовых|переплат|пск\b", None),
    ("списание",              r"списал\w*|списан\w*|списыва\w*|снял\w*\s+деньг|безакцепт|списание", None),
    ("навязанная страховка",  r"страховк\w|страхован\w|полис",
                              r"навяз|без\s+моего|не\s+прос\w+|втюх|подключил\w*\s+без|скрыт\w|обманом|незаметно|автоматом|не\s+согла"),
    ("отказ",                 r"отказ\w*|не\s+одобр\w+|заблокир\w+\s+заявк", None),
    ("служба поддержки",      r"поддержк\w|оператор\w*|колл-?центр|call-?центр|горяч\w*\s+лини|чат\s+банк|техподдержк|саппорт", None),
    ("мошенничество",         r"мошенник\w*|мошеннич\w*|обман\w*|развод\w*\s+на\s+деньг|фишинг|украл\w*\s+деньг|фрод", None),
    ("коллекторы",            r"коллектор\w*|взыскан\w*|выбива\w*\s+долг|судебн\w*\s+приказ|приставы", None),
]
_THEMES_RX = [(name, re.compile(inc, re.I), re.compile(req, re.I) if req else None)
              for name, inc, req in THEMES]

# Отдельная карта «текст -> продукт» для угадывания продукта в отзыве.
_PRODUCT_RX = [(name, rx) for name, rx, _ in _THEMES_RX
               if name in ("кредитная карта", "рассрочка", "кредит наличными", "автокредит")]


def _guess_product(review):
    blob = f"{review.get('title','')} {review.get('text','')}"
    for name, rx in _PRODUCT_RX:
        if rx.search(blob):
            return name
    return None


def is_negative(review):
    """Негатив = 1–2 звезды. Если оценки нет — по словарю маркеров (см. sentiment)."""
    r = review.get("rating")
    if r is not None:
        return r <= 2
    return _marker_score(f"{review.get('title','')} {review.get('text','')}") < 0


def themes(reviews):
    """Разбор отзывов по темам: сколько попало, какая доля негатива внутри темы.

    Один отзыв может попасть в несколько тем — это нормально: «навязали страховку
    при выдаче кредитки» и правда про две темы сразу. Поэтому сумма по темам
    больше числа отзывов, и доли считаются от общего числа отзывов, а не друг от друга.
    """
    total = len(reviews) or 1
    res = {}
    for name, inc, req in _THEMES_RX:
        hits = []
        for r in reviews:
            blob = f"{r.get('title','')} {r.get('text','')}"
            if inc.search(blob) and (req is None or req.search(blob)):
                hits.append(r)
        if not hits:
            continue
        neg = [r for r in hits if is_negative(r)]
        rated = [r["rating"] for r in hits if r.get("rating") is not None]
        res[name] = {
            "n":          len(hits),
            "share":      round(len(hits) / total, 3),      # доля от всех отзывов
            "neg":        len(neg),
            "neg_share":  round(len(neg) / len(hits), 3),   # доля негатива внутри темы
            "avg_rating": round(sum(rated) / len(rated), 2) if rated else None,
            "examples":   [{"date": r["date"], "rating": r["rating"],
                            "title": r["title"][:90], "url": r.get("url")}
                           for r in neg[:3]],
        }
    return dict(sorted(res.items(), key=lambda kv: (-kv[1]["neg"], -kv[1]["n"])))


# ============================================================================
#  7. Тональность
# ============================================================================
# ЧЕСТНО О МЕТОДЕ. Никакого ML здесь нет и не планируется.
# Тональность считается двумя простыми способами:
#   (а) по звёздам: 1–2 = негатив, 3 = нейтрально, 4–5 = позитив. Это не наша
#       оценка, а оценка самого автора отзыва — самый надёжный сигнал;
#   (б) по словарю маркеров — только для отзывов «Без оценки» (на banki.ru таких
#       заметная часть). Считаем совпадения негативных и позитивных слов,
#       знак разницы и есть тональность.
# Слабые места метода перечислены в reviews_report.md; коротко: словарь не знает
# отрицаний («не обманули»), сарказма и цитат из ответа банка. Но главная проблема
# не в словаре, а в ВЫБОРКЕ: лента «все отзывы» на banki.ru сильно накачана
# позитивом — у Сбербанка последние 30 отзывов дают среднюю 5,0 при народном
# рейтинге 2,52. Поэтому абсолютный уровень тональности по свежей ленте читать
# нельзя; смысл имеют (1) динамика день ко дню на одинаковой выборке,
# (2) темы жалоб внутри негатива и (3) сам разрыв со средней площадки —
# он и лежит в поле sample_gap.
NEG_WORDS = [
    "ужас", "хамств", "обман", "мошенн", "жалоб", "отвратит", "кошмар", "позор",
    "навяз", "не реш", "не помог", "игнор", "бесит", "возмут", "грабёж", "грабеж",
    "списал", "списан", "незаконн", "отказ", "потерял", "висит", "не дозвон",
    "врут", "врал", "некомпетент", "футбол", "разочаров", "беспредел", "блокир",
    "хамит", "нагло", "принудит", "штраф", "просроч", "коллектор", "угроз",
]
POS_WORDS = [
    "спасибо", "благодар", "отличн", "быстро реш", "вежлив", "профессионал",
    "удобн", "доволен", "довольна", "рекоменду", "оператив", "помог", "чётко",
    "четко", "приятн", "молодц", "выручил", "внимательн", "лучший", "супер",
]


def _marker_score(text):
    t = (text or "").lower()
    neg = sum(t.count(w) for w in NEG_WORDS)
    pos = sum(t.count(w) for w in POS_WORDS)
    return pos - neg


def sentiment(reviews, previous=None):
    """Доля 1–2 звёзд против 4–5 плюс динамика к предыдущему замеру.

    previous — блок sentiment из прошлого сохранённого замера (см. measure);
    если его нет, поле dynamics будет None.
    """
    n = len(reviews)
    rated = [r for r in reviews if r.get("rating") is not None]
    unrated = [r for r in reviews if r.get("rating") is None]
    neg = [r for r in rated if r["rating"] <= 2]
    neu = [r for r in rated if r["rating"] == 3]
    pos = [r for r in rated if r["rating"] >= 4]
    # отзывы без оценки — только по словарю; отдельной строкой, не в общий котёл
    mk = [_marker_score(f"{r.get('title','')} {r.get('text','')}") for r in unrated]
    res = {
        "n": n,
        "n_rated": len(rated),
        "n_unrated": len(unrated),
        "neg": len(neg), "neu": len(neu), "pos": len(pos),
        "neg_share": round(len(neg) / len(rated), 3) if rated else None,
        "pos_share": round(len(pos) / len(rated), 3) if rated else None,
        "avg_rating": round(sum(r["rating"] for r in rated) / len(rated), 2) if rated else None,
        "balance": (round(len(pos) / len(rated) - len(neg) / len(rated), 3)
                    if rated else None),          # позитив минус негатив, в долях
        "unrated_neg_by_markers": sum(1 for s in mk if s < 0),
        "unrated_pos_by_markers": sum(1 for s in mk if s > 0),
        "period": {"from": min((r.get("date") or "" for r in reviews), default=None),
                   "to":   max((r.get("date") or "" for r in reviews), default=None)},
        "method": ("простой метод: (1) звёзды автора отзыва, (2) словарь маркеров "
                   "только для отзывов без оценки; ML не используется. Выборка — "
                   "лента свежих отзывов агрегатора, она смещена в позитив, см. sample_gap"),
        "dynamics": None,
    }
    if previous:
        d = {}
        for k in ("neg_share", "pos_share", "avg_rating", "balance"):
            a, b = res.get(k), previous.get(k)
            d[k] = round(a - b, 3) if (a is not None and b is not None) else None
        d["since"] = previous.get("_ts")
        res["dynamics"] = d
    return res


# ============================================================================
#  8. Замеры и динамика между днями
# ============================================================================
def _state_path(bank_key):
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, f"{bank_key}.json")


def load_state(bank_key):
    try:
        with open(_state_path(bank_key), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"bank": bank_key, "history": [], "last_reviews": []}


def _delta(new, old, keys):
    """Разница по числовым полям двух словарей (или None, если сравнивать не с чем)."""
    if not old:
        return None
    out = {}
    for k in keys:
        a, b = (new or {}).get(k), (old or {}).get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out[k] = round(a - b, 4)
        else:
            out[k] = None
    return out


def measure(bank_key, limit=30, save=True):
    """Полный замер по банку: рейтинги + свежие отзывы + темы + тональность,
    с динамикой к предыдущему сохранённому замеру и записью в state/reviews/."""
    st = load_state(bank_key)
    hist = st.get("history", [])
    prev = hist[-1] if hist else None

    rat = ratings(bank_key)
    rev, rev_errs = recent(bank_key, limit=limit)
    th = themes(rev)
    prev_sent = dict(prev.get("sentiment") or {}, _ts=prev.get("ts")) if prev else None
    sent = sentiment(rev, previous=prev_sent)

    entry = {
        "ts":        rat["ts"],
        "date":      rat["ts"][:10],
        "banki.ru":  rat.get("banki.ru"),
        "sravni.ru": rat.get("sravni.ru"),
        "errors":    dict(rat.get("errors") or {}),      # что не удалось загрузить
        "notes":     dict(rev_errs),                     # ограничения источников
        "sentiment": {k: v for k, v in sent.items() if k != "dynamics"},
        "themes":    {k: {"n": v["n"], "neg": v["neg"], "neg_share": v["neg_share"]}
                      for k, v in th.items()},
    }
    # разрыв между средней по свежей выборке и народным рейтингом площадки
    br = (entry.get("banki.ru") or {}).get("rating")
    entry["sample_gap"] = (round(sent["avg_rating"] - br, 2)
                           if (sent.get("avg_rating") is not None and br is not None) else None)

    # динамика агрегатов агрегаторов — считаем к последнему замеру любой давности
    entry["delta"] = {
        "banki.ru":  _delta(entry.get("banki.ru"), (prev or {}).get("banki.ru"),
                            ("rating", "reviews_total", "resolved_pct", "place")),
        "sravni.ru": _delta(entry.get("sravni.ru"), (prev or {}).get("sravni.ru"),
                            ("rating", "reviews_total", "recommend_pct")),
        "sentiment": sent.get("dynamics"),
    }

    if save:
        st["bank"] = bank_key
        st["history"] = (hist + [entry])[-MAX_HISTORY:]
        st["last_reviews"] = rev            # чтобы можно было пересчитать темы офлайн
        st["updated"] = entry["ts"]
        with open(_state_path(bank_key), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)

    return {"entry": entry, "reviews": rev, "themes": th, "sentiment": sent}


# ============================================================================
#  9. Печать в консоль
# ============================================================================
def _sign(v, nd=2):
    """Дельта со знаком; None -> прочерк (сравнивать было не с чем)."""
    return "—" if v is None else f"{v:+.{nd}f}"


def _fmt(v, suffix="", nd=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def report(bank_key, limit=30):
    r = measure(bank_key, limit=limit)
    e, th, se = r["entry"], r["themes"], r["sentiment"]
    name = bank_name(bank_key)
    print(f"\n{'='*78}\n{name}  [{bank_key}]  {e['ts']}\n{'='*78}")

    b = e.get("banki.ru")
    if b:
        print(f"  banki.ru : рейтинг {_fmt(b.get('rating'))} | место "
              f"{_fmt(b.get('place'))} из {_fmt(b.get('place_of'))} | отзывов "
              f"{_fmt(b.get('reviews_total'))} | решено {_fmt(b.get('resolved_pct'),'%')} "
              f"| ответов {_fmt(b.get('answers'))}")
        if b.get("note"):
            print(f"             ({b['note']})")
    else:
        print(f"  banki.ru : НЕДОСТУПНО — {e['errors'].get('banki.ru')}")

    s = e.get("sravni.ru")
    if s:
        print(f"  sravni.ru: рейтинг {_fmt(s.get('rating'))} | отзывов "
              f"{_fmt(s.get('reviews_total'))} | рекомендуют "
              f"{_fmt(s.get('recommend_pct'),'%',0)}")
        bp = s.get("by_product") or {}
        keep = [k for k in bp if any(w in k.lower() for w in
                ("кредит", "карт", "рассроч", "обслужив"))]
        if keep:
            print("             по продуктам: " +
                  ", ".join(f"{k} {_fmt(bp[k],'',1)}" for k in keep[:7]))
        if s.get("mentions"):
            print("             часто упоминают: " +
                  ", ".join(f"{k} ({v})" for k, v in list(s["mentions"].items())[:6]))
    else:
        print(f"  sravni.ru: НЕДОСТУПНО — {e['errors'].get('sravni.ru')}")

    print(f"  тональность ({se['n']} свежих отзывов, {se['period']['from']}…{se['period']['to']}): "
          f"1–2★ {_fmt((se['neg_share'] or 0)*100,'%',0)} | 3★ {se['neu']} | "
          f"4–5★ {_fmt((se['pos_share'] or 0)*100,'%',0)} | средняя {_fmt(se['avg_rating'])} "
          f"| без оценки {se['n_unrated']}")
    d = se.get("dynamics")
    if d:
        print(f"  динамика к замеру {str(d.get('since'))[:16]}: доля 1–2★ "
              f"{_sign(d.get('neg_share'), 3)} | средняя {_sign(d.get('avg_rating'))} "
              f"| баланс {_sign(d.get('balance'), 3)}")
        db = e.get("delta", {}).get("banki.ru") or {}
        ds = e.get("delta", {}).get("sravni.ru") or {}
        print(f"             banki.ru: рейтинг {_sign(db.get('rating'))}, отзывов "
              f"{_sign(db.get('reviews_total'), 0)}, решено {_sign(db.get('resolved_pct'))} п.п. | "
              f"sravni.ru: рейтинг {_sign(ds.get('rating'))}, отзывов "
              f"{_sign(ds.get('reviews_total'), 0)}")
    else:
        print("  динамика: первый замер, сравнивать не с чем")

    # Расхождение «свежая выборка vs народный рейтинг» — само по себе сигнал:
    # если последние 30 отзывов дают 5,0 при народном рейтинге 2,5, значит поток
    # свежих отзывов накачан позитивом, и тональность по нему читать нельзя.
    gap = e.get("sample_gap")
    if gap is not None:
        flag = ("  ← выборка смещена, абсолютный уровень тональности недостоверен"
                if abs(gap) >= 1.0 else "")
        print(f"  сдвиг выборки: свежие {_fmt(se.get('avg_rating'))} против народного "
              f"рейтинга {_fmt((e.get('banki.ru') or {}).get('rating'))} "
              f"(разрыв {_sign(gap)}){flag}")

    if th:
        print("  темы (по свежим отзывам):")
        for k, v in list(th.items())[:8]:
            print(f"    {k:22s} n={v['n']:3d}  негатив {v['neg']:3d} "
                  f"({v['neg_share']*100:.0f}%)  средняя {_fmt(v['avg_rating'])}")
            for ex in v["examples"][:1]:
                print(f"        ↳ {ex['date']} {ex['rating']}★ «{ex['title']}»")
    else:
        print("  темы: в выборке нет отзывов по нашим темам")
    return r


# ============================================================================
# 10. Адаптер для аналитика: короткий контекстный блок
# ============================================================================
# По образцу cbr.context_block(): радар подмешивает этот текст в контекст, поэтому
# в нём только проверенные цифры с указанием источника и явная оговорка про метод.
# Ходить в сеть здесь нельзя — читаем последний сохранённый замер из state/reviews/.
# Обновить данные: reviews.py (обход) или context_block(refresh=True).
CONTEXT_LIMIT = 2000        # ориентир по длине блока, знаков


def _ru(v, nd=2, dash="—"):
    """Число в русском виде: запятая вместо точки."""
    if v is None:
        return dash
    return (f"{v:.{nd}f}" if isinstance(v, float) else str(v)).replace(".", ",")


def _prev_entry(history):
    """Предыдущий замер ДРУГОГО дня — иначе динамика меряла бы шум внутри суток."""
    if len(history) < 2:
        return None
    today = history[-1].get("date")
    for e in reversed(history[:-1]):
        if e.get("date") != today:
            return e
    return history[-2]


def context_block(banks=None, refresh=False, limit=30):
    """Короткая (~2000 знаков) сводка по репутации конкурентов для контекста аналитика.

    Только то, что реально опубликовано на площадках, плюс динамика к прошлому
    замеру и прямая оговорка, что тональность считается простым методом.
    """
    banks = banks or KEY_BANKS
    if refresh:
        for k in banks:
            try:
                measure(k, limit=limit)
            except Exception:
                pass

    rows, ts, theme_neg, stale = [], None, {}, []
    for k in banks:
        hist = load_state(k).get("history") or []
        if not hist:
            continue
        e, prev = hist[-1], _prev_entry(hist)
        ts = max(ts, e["ts"]) if ts else e["ts"]
        b, sr, se = e.get("banki.ru") or {}, e.get("sravni.ru") or {}, e.get("sentiment") or {}
        # Дельту считаем здесь заново, а не берём e["delta"]: та посчитана к
        # непосредственно предыдущей записи, которая может быть замером того же дня.
        # В контекст аналитика нужна динамика СУТКИ к суткам.
        d = {}
        if prev is None:
            stale.append(SHORT_NAMES.get(k, k))
        else:
            d = _delta(b, prev.get("banki.ru") or {}, ("rating",)) or {}
        rows.append((
            SHORT_NAMES.get(k, k),
            _ru(b.get("rating")),
            f"№{b['place']}" if b.get("place") else "—",
            _ru(b.get("resolved_pct"), 0) + "%" if b.get("resolved_pct") is not None else "—",
            _ru(sr.get("rating")),
            _ru(sr.get("recommend_pct"), 0) + "%" if sr.get("recommend_pct") is not None else "—",
            _ru((se.get("neg_share") or 0) * 100, 0) + "%" if se.get("neg_share") is not None else "—",
            ("%+.2f" % d["rating"]).replace(".", ",") if isinstance(d.get("rating"), float) else "—",
        ))
        for name, t in (e.get("themes") or {}).items():
            theme_neg[name] = theme_neg.get(name, 0) + t.get("neg", 0)

    if not rows:
        return "[репутационные замеры не найдены — запусти: python reviews.py]"

    out = [f"РЕПУТАЦИЯ КОНКУРЕНТОВ по открытым отзывам, замер {str(ts)[:10]} "
           f"(banki.ru + sravni.ru).",
           "Цифры площадок проверены и взяты со страниц как есть; тональность — "
           "по последним 30 отзывам banki.ru.",
           "банк         banki  место решено | sravni реком | 1–2★ | Δрейт"]
    for r in rows:
        out.append(f"{r[0]:<12} {r[1]:>5} {r[2]:>5} {r[3]:>6} | {r[4]:>6} {r[5]:>5} "
                   f"| {r[6]:>4} | {r[7]}")

    # Рейтинг sravni.ru по продуктам — для разборов по конкретному продукту (13.09.2026: кредитные карты)
    prod = []
    for k in banks:
        hist = load_state(k).get("history") or []
        if not hist:
            continue
        bp = ((hist[-1].get("sravni.ru") or {}).get("by_product") or {})
        cc, cash = bp.get("Кредитные карты"), bp.get("Кредиты наличными")
        if cc is not None or cash is not None:
            prod.append(f"{SHORT_NAMES.get(k, k)} КК {_ru(cc)} / наличные {_ru(cash)}")
    if prod:
        out.append("Рейтинг sravni.ru по продуктам (КК — кредитные карты, наличные — кредиты наличными): " + "; ".join(prod) + ".")

    top = sorted(theme_neg.items(), key=lambda kv: -kv[1])[:5]
    if any(v for _, v in top):
        out.append("Темы негатива в свежих отзывах (сумма по банкам): " +
                   ", ".join(f"{n} {v}" for n, v in top if v))

    out += [
        "МЕТОД ТОНАЛЬНОСТИ ПРОСТОЙ, ML нет: доля 1–2★ против 4–5★ по оценкам самих "
        "авторов плюс словарь маркеров для отзывов без оценки.",
        "Абсолютный уровень тональности НЕДОСТОВЕРЕН: лента свежих отзывов banki.ru "
        "накачана позитивом (у Сбера последние 30 отзывов дают 5,00 при народном "
        "рейтинге 2,52). Опираться можно на рейтинги площадок, на динамику и на "
        "распределение жалоб по темам, но не на «долю негатива» как на уровень.",
        "Не собрано: тексты отзывов sravni.ru (закрыты robots.txt, /proxy-*); "
        "свой банк (own) на banki.ru — карточка есть, отзывов ноль, точка отсчёта по "
        "своему банку только по sravni.ru.",
    ]
    if stale:
        out.append("Динамика не считается (первый замер): " + ", ".join(stale) + ".")

    txt = "\n".join(out)
    if len(txt) > CONTEXT_LIMIT + 400:      # страховка от разрастания при 12 банках
        txt = txt[:CONTEXT_LIMIT + 400].rsplit("\n", 1)[0] + "\n[блок обрезан]"
    return txt


KEY_BANKS = ["sber", "tbank", "alfa", "gpb", "vtb", "sovcom", "ozon", "yandex", "mts"]

if __name__ == "__main__":
    import sys
    keys = sys.argv[1:] or KEY_BANKS
    for k in keys:
        try:
            report(k)
        except Exception as ex:                 # один банк не должен ронять обход
            print(f"\n{k}: СБОЙ — {type(ex).__name__}: {ex}")
