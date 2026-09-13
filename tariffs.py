# -*- coding: utf-8 -*-
"""Тарифные документы банков → реальные цифры цены продукта.

ЗАЧЕМ. Витрина (лендинг) почти никогда не содержит цену: из девяти проверенных
банков полную стоимость кредита по кредитной карте на лендинге раскрыл один.
Но публиковать условия банк обязан — отдельным документом: PDF «Тарифы»,
«Основные параметры», страница «Тарифы и документы», карточка конкретного продукта.
Этот модуль их находит, скачивает, разбирает и следит за изменениями.

ГЛАВНОЕ ОГРАНИЧЕНИЕ, КОТОРОЕ ВЫЯСНИЛОСЬ НА ПРАКТИКЕ (проверено 08.09.2026):
    большинство банков ЗАПРЕЩАЮТ обход PDF в robots.txt.
        tbank.ru, cdn.tbank.ru   Disallow: /*.pdf
        vtb.ru                   Disallow: *.pdf$
        alfabank.ru              Disallow: /*.pdf   (но НЕ alfabank.servicecdn.ru)
        gazprombank.ru           Disallow: /*.pdf
        otpbank.ru, rencredit.ru, uralsib.ru — тоже запрет
        sberbank.ru, raiffeisen.ru, finance.ozon.ru, mtsbank.ru — PDF РАЗРЕШЁН
    Запрет мы уважаем: такие документы помечаются «запрещено robots.txt» и не
    качаются. Зато у ВТБ, Т-Банка и Газпромбанка ровно те же цифры лежат в HTML —
    на странице продукта или в разделе тарифов, и оттуда их брать можно.
    Поэтому модуль работает с ДВУМЯ видами источников: PDF и HTML-страница условий.

ЧЕСТНОСТЬ. У каждого извлечённого числа хранится дословная строка-источник
(поле «цитата») и ссылка на документ. Число без цитаты не возвращается и не
публикуется — это жёсткое правило, а не пожелание: цена продукта конкурента,
названная неверно, стоит дороже, чем её отсутствие.

Загрузка идёт через core (свой CA-бандл под российские сертификаты, пауза между
запросами, разбор robots.txt, при JS-заглушке — рендер headless-хромом).
"""
import os, re, io, json, gzip, time, hashlib, urllib.request, urllib.error
from urllib.parse import urljoin, urlsplit

import core
import sources

try:
    import fitz                      # PyMuPDF: разбор PDF
except Exception:                    # модуль не обязателен — без него живём на HTML
    fitz = None

CACHE   = os.path.join(core.STATE, "tariffs")
DOCS    = os.path.join(CACHE, "docs")       # тела документов (текст, gzip)
HIST    = os.path.join(CACHE, "hist")       # история изменений по документу
INDEX   = os.path.join(CACHE, "index.json") # url -> {sha, ts, first_seen, bank, ...}
MAX_BYTES = 30 * 1024 * 1024                # 30 МБ: альбом тарифов Сбера ~2 МБ, запас есть
# Версия разбора. Записывается в историю и сравнивается в changes(): правка
# парсера меняет извлечённые числа, но это НЕ изменение тарифа банка, и выдавать
# её за новость нельзя. Поднимать при любом изменении PARAMS/_value/parse_terms.
PARSER_VERSION = 1


# ═══════════════════════════════ 1. ПОИСК ДОКУМЕНТОВ ═══════════════════════════
#
# Стратегия. От продуктовой страницы банка идём тремя путями:
#   A. сама страница продукта — у Т-Банка и ВТБ тарифная таблица прямо на ней;
#   B. ссылки со словами «тариф/условия/документы/правила» — раздел документов;
#   C. страницы-потомки того же раздела (у Газпромбанка цифры лежат на карточке
#      конкретной карты /personal/credit-cards/7980419/, а не на витрине).
# Найденное ранжируем: официальный тариф важнее памятки об акции.

# Слова, по которым узнаём тарифный документ (в URL или в тексте ссылки).
_GOOD = [
    (re.compile(r"основны\w*\s+параметр|osn_par", re.I),               60),
    (re.compile(r"тариф|tarif|tariff", re.I),                          45),
    (re.compile(r"услови\w*\s+(выпуска|обслуживания|кредитован)", re.I),40),
    (re.compile(r"индивидуальн\w*\s+услови|общи\w*\s+услови", re.I),   35),
    (re.compile(r"услови|uslov", re.I),                                20),
    (re.compile(r"документ|document|/docs", re.I),                     15),
    (re.compile(r"правил\w*\s+комплексн|pravila", re.I),               10),
]
# Слова, по которым документ отбраковываем: это не цена продукта.
_BAD = re.compile(
    r"акци|promo|loyalty|бонус|памятк|pamyatk|политик|polit|licen|лиценз|"
    r"персональн\w*\s+данн|personal[-_]data|ваканс|карьер|устав|годов\w*\s+отч|"
    r"раскрыти|disclosure|инвест|invest|брокер|broker|депозитар|страхов|insur|"
    r"/business/|/sme/|/corporate/|/private/|/malyj-biznes/|/finance/|"
    r"юридическ|ип\b|эквайринг|acquiring|зарплатн", re.I)

# Разделы, куда вообще не ходим (не розница).
_OFFTOPIC = re.compile(r"/(business|sme|corporate|malyj-biznes|private|ir|about)/", re.I)

PDF_RX = re.compile(r"\.pdf(\?|$)", re.I)
DOCX_RX = re.compile(r"\.(docx?|xlsx?|rtf)(\?|$)", re.I)

# Ручные подсказки: страницы, которые генеричный обход находит плохо, но на
# которых цифры точно есть. Все проверены руками 08.09.2026 — рядом что именно там.
DOC_HINTS = {
    ("vtb", "кредитная карта"): [
        # ПСК 29–59,9%, ставка 29,9–69,9%, мин. платёж 3%, грейс до 200 дней
        "https://www.vtb.ru/personal/karty/kreditnye/vozmozhnosti/",
        # комиссии за снятие наличных из кредитного лимита, обслуживание
        "https://www.vtb.ru/tarify/chastnim-licam/",
    ],
    ("alfa", "кредитная карта"): [
        # раздел тарифов физлиц: оттуда ведут ссылки на PDF на servicecdn (PDF там разрешён)
        "https://alfabank.ru/retail/tariffs/",
    ],
    ("gpb", "кредитная карта"): [
        # карточка карты «180 дней»: ПСК 55,335–57,303%, ставка 59,99%, снятие 6,9%+690 ₽
        "https://www.gazprombank.ru/personal/credit-cards/7980419/",
    ],
    ("tbank", "кредитная карта"): [
        # тарифная таблица в HTML прямо на карточке продукта (PDF-тарифы закрыты robots)
        "https://www.tbank.ru/cards/credit-cards/platinum/",
    ],
}


def _anchors(html, base):
    """[(абсолютный url, текст ссылки)] — с учётом того, что ссылки бывают
    относительными («/upload/x.pdf», «../tarify/»)."""
    out = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html, re.S | re.I):
        attrs, inner = m.group(1), m.group(2)
        h = re.search(r'href\s*=\s*["\']([^"\']+)["\']', attrs)
        if not h:
            continue
        href = h.group(1).strip().replace("&amp;", "&")
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        txt = re.sub(r"<[^>]+>", " ", inner)
        txt = " ".join(txt.split())[:200]
        try:
            url = urljoin(base, href)
        except Exception:
            continue
        if not url.startswith("http"):
            continue
        out.append((url.split("#")[0], txt))
    return out


def _score(url, text, host, base):
    """Насколько ссылка похожа на тарифный документ. Отрицательное — не берём."""
    hay = url + " " + text
    if _BAD.search(hay) and not re.search(r"тариф|основны\w*\s+параметр|osn_par", hay, re.I):
        return -1
    if _OFFTOPIC.search(url):
        return -1
    s = 0
    for rx, w in _GOOD:
        if rx.search(hay):
            s = max(s, w)
    if PDF_RX.search(url):
        s += 25 if s else 0          # «просто PDF» без тарифных слов не берём
    elif DOCX_RX.search(url):
        s += 15 if s else 0
    # потомок того же раздела витрины — у Газпромбанка там и лежат цифры
    if not s and url.startswith(base.rsplit("/", 2)[0]) and urlsplit(url).netloc.endswith(host):
        depth_base = base.rstrip("/").count("/")
        if url.rstrip("/").count("/") in (depth_base, depth_base + 1) and url.rstrip("/") != base.rstrip("/"):
            s = 8
    if s and urlsplit(url).netloc.endswith(host):
        s += 5                       # свой домен банка надёжнее случайного внешнего
    return s


def _links_path(bank_key, product):
    os.makedirs(CACHE, exist_ok=True)
    d = os.path.join(CACHE, "links")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{bank_key}_{_uid(product or 'all')}.json")


def find_docs(bank_key, product=None, max_docs=14, expand=True, max_age=20 * 3600):
    """Ссылки на тарифные документы банка.

    Возвращает список словарей:
        url, текст (текст ссылки), продукт, вид ('PDF'|'HTML'), балл, разрешено,
        откуда (страница, на которой ссылка найдена).
    Ничего не качает кроме самих продуктовых страниц и (при expand) одного уровня
    разделов «Тарифы/Документы».
    """
    b = sources.BANKS.get(bank_key)
    if not b:
        return []
    if b.get("blocked"):
        return []                    # банк за антиботом — см. sources.py
    # Список ссылок кэшируем на сутки: обход витрины Сбера или Альфы — это рендер
    # headless-хромом на минуту и больше, а набор тарифных документов за день
    # практически не меняется (меняется их содержимое, а его тянет fetch_doc).
    lp = _links_path(bank_key, product)
    if max_age and os.path.exists(lp) and time.time() - os.path.getmtime(lp) < max_age:
        try:
            with open(lp, encoding="utf-8") as f:
                cached = json.load(f)
            if cached:
                return cached[:max_docs]
        except Exception:
            pass
    host = b["host"]
    pages = b["pages"]
    if product:
        pages = {k: v for k, v in pages.items() if k == product}
    found, seen = [], set()

    def add(url, text, prod, score, src):
        url = url.split("#")[0].rstrip()
        if url in seen or score <= 0:
            return
        seen.add(url)
        found.append(dict(url=url, текст=text, продукт=prod,
                          вид="PDF" if PDF_RX.search(url) else "HTML",
                          балл=score, разрешено=core.allowed(url), откуда=src))

    for prod, page in pages.items():
        html, txt, how, err = core.get_rendered(page)
        if not html:
            continue
        # A. сама витрина продукта — у Т-Банка/ВТБ тарифная таблица прямо здесь
        add(page, f"витрина «{prod}»", prod, 30, page)
        anchors = _anchors(html, page)
        for url, text in anchors:
            add(url, text, prod, _score(url, text, host, page), page)
        # B. один уровень вглубь: раздел «Тарифы/Документы» → PDF внутри него
        if expand:
            sections = [d for d in found
                        if d["откуда"] == page and d["вид"] == "HTML" and d["балл"] >= 15
                        and d["url"] != page and d["разрешено"]][:3]
            for sec in sections:
                h2, _t2, _how2, _e2 = core.get_rendered(sec["url"])
                if not h2:
                    continue
                for url, text in _anchors(h2, sec["url"]):
                    add(url, text, prod, _score(url, text, host, sec["url"]), sec["url"])
    # ручные подсказки — с максимальным приоритетом
    for (bk, prod), urls in DOC_HINTS.items():
        if bk != bank_key or (product and prod != product):
            continue
        for u in urls:
            if u in seen:
                for d in found:
                    if d["url"] == u:
                        d["балл"] = max(d["балл"], 100)
            else:
                seen.add(u)
                found.append(dict(url=u, текст="проверенная вручную ссылка", продукт=prod,
                                  вид="PDF" if PDF_RX.search(u) else "HTML",
                                  балл=100, разрешено=core.allowed(u), откуда="DOC_HINTS"))
    # Запрещённые robots.txt уводим вниз: пользы от них нет, а место в
    # выдаче они занимают — у ВТБ восемь закрытых PDF вытеснили рабочие
    # HTML-страницы условий, и банк выглядел «без данных».
    found.sort(key=lambda d: (not d["разрешено"], -d["балл"]))
    try:
        with open(lp, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False)
    except Exception:
        pass
    return found[:max_docs]


# ═══════════════════════════════ 2. ЗАГРУЗКА И КЭШ ═════════════════════════════

def _uid(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _doc_path(url):
    os.makedirs(DOCS, exist_ok=True)
    return os.path.join(DOCS, _uid(url) + ".json.gz")


def _hist_path(url):
    os.makedirs(HIST, exist_ok=True)
    return os.path.join(HIST, _uid(url) + ".jsonl")


def _load_index():
    try:
        with open(INDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(ix):
    os.makedirs(CACHE, exist_ok=True)
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ix, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX)


def _get_bytes(url, timeout=45):
    """Сырые байты + Content-Type. Нужны для PDF: core.get декодирует ответ в
    строку, а PDF — двоичный. Пауза между запросами, UA и CA-бандл берём из core,
    свою загрузку не пишем."""
    if not core.allowed(url):
        return None, None, "запрещено robots.txt"
    h = core._host(url)
    wait = core.PAUSE - (time.time() - core._LAST_HIT.get(h, 0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={
        "User-Agent": core.UA, "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=core._CTX) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return None, ctype, f"документ больше {MAX_BYTES // 1024 // 1024} МБ"
            return data, ctype, None
    except urllib.error.HTTPError as e:
        return None, None, f"HTTP {e.code}"
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:60]}"
    finally:
        core._LAST_HIT[h] = time.time()


def pdf_text(data):
    """Текст PDF со ВОССТАНОВЛЕНИЕМ СТРОК по координатам слов.

    Обычный fitz .get_text() читает многоколоночные тарифные таблицы блоками и
    разрывает пару «название параметра → значение»: у Альфы подпись оказывалась
    за десяток строк от своей цифры. Склейка слов по общей вертикальной координате
    возвращает таблицу в вид «Процентная ставка на покупки   58,49% – 58,99%»,
    то есть подпись и число снова стоят рядом — а именно на этом держится разбор.
    """
    if not fitz:
        return None, "нет pymupdf"
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return None, f"PDF не открылся: {type(e).__name__}"
    lines = []
    try:
        for page in doc:
            rows = {}
            for w in page.get_text("words"):     # x0,y0,x1,y1,слово,...
                rows.setdefault(round(w[1] / 3.0), []).append(w)
            for k in sorted(rows):
                ws = sorted(rows[k], key=lambda w: w[0])
                s, prev = "", None
                for w in ws:
                    if prev is not None and w[0] - prev > 12:
                        s += "   "               # разрыв колонок
                    elif s:
                        s += " "
                    s += w[4]
                    prev = w[2]
                if s.strip():
                    lines.append(s.strip())
    except Exception as e:
        return None, f"PDF не разобрался: {type(e).__name__}"
    return "\n".join(lines), None


def html_text(html):
    """HTML → построчный текст БЕЗ схлопывания повторов.

    core.to_text выбрасывает повторяющиеся строки — для диффов витрин это
    правильно, а для тарифной таблицы губительно: «Бесплатно» встречается в ней
    пять раз подряд напротив разных строк, и после дедупликации значения съезжают
    к чужим подписям. Поэтому здесь свой проход тем же парсером, но без дедупликации.
    """
    p = core._Text()
    try:
        p.feed(html)
    except Exception:
        pass
    return "\n".join(t for t in p.out if t.strip())


def fetch_doc(url, refresh=False, max_age=20 * 3600, bank=None):
    """Скачать документ и вернуть его текст.

    Возвращает словарь: url, вид ('PDF'|'HTML'), текст, sha (хэш текста),
    ts, изменился (bool), из_кэша (bool), ошибка.
    Кэш — state/tariffs/docs/<хэш url>.json.gz; повторно в течение max_age не тянем.
    Хэш текста пишем в индекс и в историю: по нему ловим изменение документа.
    """
    ix = _load_index()
    rec_ix = ix.get(url) or {}
    p = _doc_path(url)
    if not refresh and os.path.exists(p):
        age = time.time() - os.path.getmtime(p)
        if age < max_age:
            try:
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    d = json.load(f)
                d["из_кэша"] = True
                d["изменился"] = False
                if bank and not rec_ix.get("банк"):   # привязку к банку не теряем
                    ix[url] = dict(rec_ix, банк=bank)  # даже если документ взят из кэша
                    _save_index(ix)
                return d
            except Exception:
                pass
    if not core.allowed(url):
        return dict(url=url, вид=None, текст=None, sha=None, ошибка="запрещено robots.txt",
                    из_кэша=False, изменился=False, ts=core.msk().isoformat(timespec="seconds"))

    kind, text, err = None, None, None
    if PDF_RX.search(url):
        data, ctype, err = _get_bytes(url)
        if data:
            kind = "PDF"
            text, err = pdf_text(data)
    else:
        html, txt, how, err = core.get_rendered(url)
        if html and html.lstrip()[:4] == "%PDF":     # ссылка без .pdf, а внутри PDF
            data, ctype, err = _get_bytes(url)
            if data:
                kind, (text, err) = "PDF", pdf_text(data)
        elif html:
            kind, text = "HTML", html_text(html)
    if not text:
        return dict(url=url, вид=kind, текст=None, sha=None, ошибка=err or "пусто",
                    из_кэша=False, изменился=False, ts=core.msk().isoformat(timespec="seconds"))

    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    ts = core.msk().isoformat(timespec="seconds")
    changed = bool(rec_ix.get("sha")) and rec_ix["sha"] != sha
    rec = dict(url=url, вид=kind, текст=text, sha=sha, ts=ts, ошибка=None,
               из_кэша=False, изменился=changed)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump({k: v for k, v in rec.items() if k != "из_кэша"}, f, ensure_ascii=False)
    # История. Пишем не при каждом изменении байтов, а только когда изменились
    # ПАРАМЕТРЫ: на страницах банков крутятся таймеры акций и счётчики, из-за них
    # sha меняется по десять раз в день, а цена продукта — нет. Хэш документа при
    # этом всё равно хранится в индексе и виден в поле «изменился».
    terms_now = _compact(parse_terms(text))
    hist = doc_history(url, limit=1)
    if not hist or hist[-1].get("параметры") != terms_now:
        with open(_hist_path(url), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "sha": sha, "версия_разбора": PARSER_VERSION,
                                "параметры": terms_now}, ensure_ascii=False) + "\n")
    ix[url] = {"sha": sha, "ts": ts, "вид": kind,
               "банк": bank or rec_ix.get("банк"),
               "впервые": rec_ix.get("впервые", ts), "знаков": len(text)}
    _save_index(ix)
    return rec


# ═══════════════════════════════ 3. РАЗБОР ЧИСЕЛ ═══════════════════════════════
#
# Принцип: находим ПОДПИСЬ параметра, затем ближайшее к ней число подходящего вида.
# Подписи ищем по всему тексту документа целиком, поэтому \s+ в шаблонах ловит и
# перенос строки — в PDF подпись часто разорвана: «Процентная ставка для» /
# «прочих операций (годовых)» / «49,8% - 59,8%».
#
# ЦИТАТА. Обязательное требование: у каждого числа должна быть дословная
# строка-источник. Поэтому цитата — это НЕПРЕРЫВНЫЙ кусок исходного текста,
# вырезанный по границам строк (text[начало_строки : конец_строки]).  Никакой
# склейки далёких друг от друга строк в одну: если подпись и значение разнесены,
# в цитату попадёт и всё, что между ними, как в оригинале.  Перед возвратом
# каждая цитата проверяется на вхождение в исходный текст символ в символ —
# не прошедшее проверку значение выбрасывается.

# (?<![\d,.]) — чтобы «2022 - 23,9%» не читалось как диапазон «022–23,9%».
_D = r"(?<![\d,.])\d{1,3}(?:[.,]\d{1,3})?"
RX_PCT_RANGE = re.compile(rf"({_D})\s*%?\s*(?:—|–|-|‒|―|до)\s*({_D})\s*%")
RX_PCT       = re.compile(rf"({_D})\s*%")
RX_DAYS      = re.compile(r"(\d{1,3})\s*(?:календарн\w*\s+)?дн", re.I)
RX_MONEY     = re.compile(r"(\d[\d\s ]{0,9})\s*(?:₽|Р\b|руб)", re.I)
# «5,9% + 590 ₽», «5,9% от суммы + 590 ₽», «4,9% плюс 490 руб.»
RX_PCT_MONEY = re.compile(rf"({_D})\s*%(?:\s*от\s+суммы)?\s*(?:\+|плюс)\s*"
                          rf"(\d[\d\s ]{{0,9}})\s*(?:₽|Р\b|руб)", re.I)
RX_FREE      = re.compile(r"бесплатн\w*|\b0\s*₽", re.I)
# Минимальный платёж — это доля ОТ ЗАДОЛЖЕННОСТИ. Без этого хвоста «5,9%» из
# соседней строки про комиссию за перевод спокойно уезжает в минимальный платёж.
RX_SHARE_TAIL = r"от\s+(?:суммы\s+)?(?:задолженност|долг|основного|остатка)"

# База начисления процента. Без неё число нельзя приводить к годовой ставке:
# «20% годовых» и «0,1% в день» — это 20% и 36,5% годовых.
BASES = [("годовых", r"годовых|год\.|в\s+год\b|годов\w*\s+ставк"),
         ("в день",  r"в\s+день|ежедневн|за\s+каждый\s+день"),
         ("в месяц", r"в\s+месяц|ежемесячн")]

# Периодичность платы. «590 ₽» в год и «590 ₽» в месяц — разные деньги.
PERIODS = [("в месяц",    r"в\s+месяц|ежемесячн"),
           ("в год",      r"в\s+год\b|ежегодн|годово\w*\s+обслуживани|годов\w*\s+плат"),
           ("за операцию", r"от\s+суммы\s+операц|за\s+операц|за\s+каждую\s+операц|разово|"
                           r"за\s+снятие|от\s+суммы\b"),
           ("в сутки",    r"в\s+сутки|в\s+день")]

# К чему относится льготный период. Обожглись на ВТБ: «до 200 дней» — это на
# погашение карт ДРУГИХ банков, а на собственные покупки — до 110.
GRACE_SCOPES = [
    # Требуем явного упоминания ЧУЖОГО банка: без этого «непогашения
    # Задолженности Льготного периода» в условиях Сбера читалось как перевод долга.
    ("перевод долга из других банков",
     r"друг\w*\s+банк|ин\w*\s+банк|чуж\w*\s+банк|рефинанс|перевод\w*\s+баланс|"
     r"balance\s*transfer|карт\w*\s+других\s+банк|друг\w*\s+кредит"),
    ("снятие наличных", r"снят\w*\s+наличн|выдач\w*\s+наличн"),
    ("покупки", r"покупк|оплат\w*\s+товар"),
]
# Оговорки, без которых срок читать нельзя.
GRACE_NOTES = [r"с\s+услугой\s+автопродлени", r"без\s+услуги\s+автопродлени",
               r"при\s+своевременном\s+внесении[^.\n]{0,40}", r"если\s+предусмотрено\s+тарифом",
               r"на\s+стандартных\s+условиях", r"первы\w*\s+\d+\s+дн"]

# (ключ, человекочитаемое имя, [(вес, шаблон подписи)], вид значения, (мин, макс))
#   rate  — процентная ставка: обязана иметь базу «годовых/в день/в месяц»;
#   share — доля от задолженности (минимальный платёж);
#   fee   — комиссия (процент, «процент + рубли» или «бесплатно»);
#   cost  — плата в рублях;
#   pct   — просто процент без требования базы.
PARAMS = [
    ("ставка_покупки", "Ставка по покупкам, % годовых", [
        (95, r"процентн\w*\s+ставк\w*\s+(?:на|за|по)\s+покупк"),
        (92, r"ставк\w*\s+за\s+пользование\s+кредитом"),
        (85, r"ставк\w*[^\n]{0,30}\s+на\s+покупки"),
        (70, r"\bна\s+покупки\b"),
        (55, r"процентн\w*\s+ставк\w*\s*\(годовых\)"),
    ], "rate", (5, 200)),
    ("ставка_наличные", "Ставка по снятию наличных, % годовых", [
        (95, r"процентн\w*\s+ставк\w*\s+на\s+любые\s+операции\s+кроме\s+покупок"),
        # У Альфы подпись разорвана значением: «Процентная ставка на любые
        # 58,49% – 58,99% годовых / операции кроме покупок».
        (86, r"процентн\w*\s+ставк\w*\s+на\s+любые"),
        (93, r"на\s+платы,?\s+снятие\s+наличных"),
        (90, r"ставк\w*[^\n]{0,40}снятие\s+наличных"),
        (88, r"процентн\w*\s+ставк\w*\s+(?:для|на|по)\s+проч\w*\s+операц"),
        (85, r"на\s+покупки,?\s+снятие\s+наличных"),
        (75, r"снятие\s+наличных\s+и\s+прочие\s+операции"),
        (89, r"процентн\w*\s+ставк\w*\s+для\b"),
        (50, r"проч\w*\s+операц\w*\s*\(годовых\)"),
    ], "rate", (5, 200)),
    ("пск", "ПСК, % годовых", [
        (95, r"полн\w*\s+стоимост\w*\s+кредита"),
        (93, r"диапазон\w*\s+значений\s+полной\s+стоимости\s+кредита"),
        (75, r"\bПСК\b"),
    ], "pct", (5, 300)),
    ("мин_платеж", "Минимальный платёж, % от долга", [
        (95, r"минимальн\w*\s+плат[её]ж"),
        (85, r"обязательн\w*\s+плат[её]ж"),
    ], "share", (0.5, 20)),
    ("комиссия_снятие", "Комиссия за снятие наличных", [
        (98, r"комисси\w*\s+за\s+(?:снятие|выдачу)\s+наличных\s+из\s+кредитн"),
        (95, r"комисси\w*\s+за\s+(?:снятие|выдачу)\s+наличн"),
        (90, r"выдача\s+наличных"),
        (85, r"за\s+снятие\s+наличных"),
        (80, r"снятие\s+наличных"),      # строка тарифной таблицы Т-Банка
        (75, r"для\s+снятия\s+в\s+рублях"),
    ], "fee", (0, 20)),
    ("обслуживание", "Обслуживание карты", [
        (95, r"(?:плата|комисси\w*)\s+за\s+обслуживание\s+(?:основной\s+)?(?:кредитной\s+)?карт"),
        (90, r"обслуживани\w*\s+(?:основной\s+)?(?:кредитной\s+)?карт"),
        (80, r"годово\w*\s+обслуживани"),
    ], "cost", (0, 100000)),
    ("неустойка", "Неустойка за просрочку, % годовых", [
        (95, r"неустойк\w*"),
        (88, r"штраф\w*[^\n]{0,30}просроч"),
        (80, r"\bпени\b"),
    ], "rate", (0, 200)),
]

# Льготный период разбираем отдельно: у него важно не только число, но и к чему
# оно относится.  Ключи ниже подставляются в результат parse_terms наравне с PARAMS.
GRACE_KEYS = [
    ("грейс_покупки",       "Льготный период на покупки, дней"),
    ("грейс_перевод_долга", "Льготный период на перевод долга из др. банков, дней"),
    ("грейс_наличные",      "Льготный период на снятие наличных, дней"),
    ("грейс_прочее",        "Льготный период (назначение не указано), дней"),
]
GRACE_LABEL = re.compile(r"беспроцентн\w*\s+период|льготн\w*\s+период|"
                         r"дн\w*\s+без\s+процентов|без\s+процентов|без\s+%", re.I)

# Порядок вывода: ставки → ПСК → грейсы по назначению → платежи и комиссии.
DISPLAY = ([(p[0], p[1]) for p in PARAMS[:3]] + GRACE_KEYS +
           [(p[0], p[1]) for p in PARAMS[3:]])
HUMAN = dict(DISPLAY)

# Сильные подписи всех параметров: на них обрезаем окно поиска значения, чтобы
# число одного параметра не приписалось соседнему (иначе ПСК уезжает в «ставку»).
_STRONG = re.compile("|".join("(?:%s)" % pat for _k, _h, labels, _kd, _r in PARAMS
                              for w, pat in labels if w >= 88)
                     + r"|(?:беспроцентн\w*\s+период)|(?:льготн\w*\s+период)", re.I)

# Запреты: если в цитате встречается это, кандидат отбрасывается. Ровно та же
# ловушка, что и с льготным периодом ВТБ: у ВТБ в таблице комиссий рядом стоят
# «снятие наличных из кредитного лимита» (5,9% + 590 ₽) и «из собственных
# средств» (0%). Второе к цене кредита отношения не имеет.
VETO_ALL = re.compile(r"выпущенн\w*\s+до\s+\d|действовавш|утратил\w*\s+силу|"
                      r"прежн\w*\s+редакц", re.I)

VETO = {
    "комиссия_снятие": re.compile(r"собственн\w*\s+средств|со\s+своего\s+счет", re.I),
    "ставка_покупки":  re.compile(r"вклад|накопительн|сберегательн|депозит", re.I),
    "ставка_наличные": re.compile(r"вклад|накопительн|сберегательн|депозит", re.I),
}

FWD = 240        # сколько знаков после подписи смотрим в поисках числа
BACK = 140       # и сколько до неё (у Т-Банка значение стоит ВЫШЕ подписи)


def _quote(text, a, b, limit=400):
    """Дословная цитата: непрерывный кусок исходного текста от начала строки,
    в которой начинается участок, до конца строки, в которой он кончается.
    Результат гарантированно является подстрокой text — это проверяется вызовом."""
    s = text.rfind("\n", 0, a) + 1
    e = text.find("\n", b)
    if e == -1:
        e = len(text)
    return text[s:e][:limit]


def _tag(chunk, table, default="не указана"):
    for name, pat in table:
        if re.search(pat, chunk, re.I):
            return name
    return default


def _value(chunk, kind, rng, label="", hint="", last=False):
    """Значение нужного вида из куска текста. Возвращает (dict, смещение) или (None, None).

    label — текст найденной подписи, hint — она же вместе с текстом вокруг.
    Из них берётся база начисления: в тарифах Сбера «(годовых)» стоит в подписи и
    на другой строке, чем само число, а витрина ВТБ пишет ставку под заголовком
    «Процентная ставка», не повторяя слово «годовых».  Для kind='rate' значение
    без базы не возвращается вовсе: «49,8%» без указания, годовых это или в день,
    не число, а заготовка для ошибки.

    ВАЖНО: сначала собираем ВСЕХ кандидатов, и только потом выбираем ближайшего к
    подписи. Раньше проверки шли по очереди и более общая форма перехватывала
    ход: у Газпромбанка «В течение 1-го календарного месяца – 0 ₽» читалось как
    диапазон «1–0 ₽» и вытесняло стоявшее рядом с подписью «Бесплатно навсегда».
    """
    lo, hi = rng
    ok = lambda *vs: all(lo <= v <= hi for v in vs)
    found = []                    # [(начало, конец, приоритет формы, dict)]

    def base_of(d, end_pos):
        d["база"] = _tag(chunk[end_pos:end_pos + 45], BASES,
                         _tag(label, BASES, _tag(hint, BASES)))
        if d["база"] == "не указана" and re.search(
                r"процентн\w*\s+ставк|полн\w*\s+стоимост", hint, re.I):
            # Ставку, стоящую под заголовком «Процентная ставка», считаем годовой,
            # но оговорку тащим прямо в текст значения — пусть видно.
            d["база"] = "годовых (в документе явно не указано)"
        return d["база"] != "не указана" or kind != "rate"

    def period_of(d, end_pos, default):
        d["периодичность"] = _tag(chunk[max(0, end_pos - 90):end_pos + 60], PERIODS, default)

    if kind in ("fee", "cost"):
        for m in RX_PCT_MONEY.finditer(chunk):
            pct = float(m.group(1).replace(",", "."))
            rub = float(re.sub(r"\D", "", m.group(2)) or 0)
            if 0 <= pct <= 20:
                d = dict(вид="процент+рубли", мин=pct, макс=pct, рубли=rub,
                         текст=m.group(0).strip())
                period_of(d, m.end(), "за операцию")
                found.append((m.start(), m.end(), 3, d))

    if kind in ("rate", "pct", "share", "fee"):
        for m in RX_PCT_RANGE.finditer(chunk):
            a, b = (float(x.replace(",", ".")) for x in m.groups())
            if not (a <= b and ok(a, b)):
                continue
            d = dict(вид="диапазон, %", мин=a, макс=b, текст=m.group(0).strip())
            if kind in ("rate", "pct") and not base_of(d, m.end()):
                continue
            if kind == "fee":
                period_of(d, m.end(), "за операцию")
            if kind == "share" and not re.search(RX_SHARE_TAIL, chunk[m.end():m.end() + 45], re.I):
                continue
            found.append((m.start(), m.end(), 2, d))
        for m in RX_PCT.finditer(chunk):
            v = float(m.group(1).replace(",", "."))
            if not ok(v):
                continue
            d = dict(вид="%", мин=v, макс=v, текст=m.group(0).strip())
            if kind in ("rate", "pct") and not base_of(d, m.end()):
                continue
            if kind == "fee":
                period_of(d, m.end(), "за операцию")
            if kind == "share" and not re.search(RX_SHARE_TAIL, chunk[m.end():m.end() + 45], re.I):
                continue
            if kind == "share":
                # «не более 14% от задолженности» — это потолок, а не значение. 13.09.2026 разбор
                # написал «Т-Банк 14%», хотя на той же странице «не более 8%». Оговорку сохраняем.
                q = re.search(r"(не\s+более|не\s+менее|до|от|обычно[^\d\n]{0,25}до)\s*$", chunk[max(0, m.start() - 30):m.start()], re.I)
                if q:
                    d["оговорка"] = re.sub(r"\s+", " ", q.group(1).lower())
                    d["оговорка"] = "до" if d["оговорка"].startswith("обычно") else d["оговорка"]
                mn = re.search(r"(?:но\s+не\s+менее|минимум|min\.?)\s*(\d[\d\s ]{0,6})\s*(?:₽|руб)", chunk[m.end():m.end() + 80], re.I)
                if mn:
                    d["минимум_руб"] = float(re.sub(r"\D", "", mn.group(1)) or 0)
            found.append((m.start(), m.end(), 1, d))

    if kind == "cost":
        for m in re.finditer(r"(?:от\s*)?(\d[\d\s ]{0,9})\s*(?:до|—|–|-)\s*"
                             r"(\d[\d\s ]{0,9})\s*(?:₽|руб)", chunk):
            a = float(re.sub(r"\D", "", m.group(1)) or 0)
            b = float(re.sub(r"\D", "", m.group(2)) or 0)
            if a <= b and ok(a, b):
                d = dict(вид="₽", мин=a, макс=b, текст=m.group(0).strip())
                period_of(d, m.end(), "в год")
                found.append((m.start(), m.end(), 3, d))
        for m in RX_FREE.finditer(chunk):
            found.append((m.start(), m.end(), 2, dict(вид="₽", мин=0.0, макс=0.0,
                                        текст=m.group(0).strip(), периодичность="—")))
        for m in RX_MONEY.finditer(chunk):
            v = float(re.sub(r"\D", "", m.group(1)) or 0)
            if ok(v):
                d = dict(вид="₽", мин=v, макс=v, текст=m.group(0).strip())
                period_of(d, m.end(), "в год")
                found.append((m.start(), m.end(), 1, d))

    if kind == "fee":
        for m in RX_FREE.finditer(chunk):
            found.append((m.start(), m.end(), 0, dict(вид="%", мин=0.0, макс=0.0,
                                        текст=m.group(0).strip(), периодичность="—")))
    if not found:
        return None, None
    # 1) выбрасываем куски, целиком лежащие внутри более развёрнутой формы:
    #    «69,9%» внутри «29,9%-69,9%», «590 ₽» внутри «5,9% + 590 ₽». Без этого
    #    диапазон ставки ВТБ схлопывался до одной своей границы.
    found = [f for f in found
             if not any(g is not f and g[2] > f[2] and g[0] <= f[0] and f[1] <= g[1]
                        for g in found)]
    # 2) «бесплатно» рядом с полноценной комиссией — это условная оговорка
    #    («0 ₽ до 50 000 ₽ в первые 30 дней, далее 5,9% + 590 ₽»). Заголовком
    #    комиссии должно быть то, что платит обычный клиент.
    if kind == "fee" and any(f[2] == 3 for f in found):
        found = [f for f in found if f[2] != 0]
    # 3) ближайший к подписи: вперёд — самый левый, назад — самый правый;
    #    при равной позиции побеждает более развёрнутая форма.
    pick = max(found, key=lambda f: (f[0], f[2])) if last else \
           min(found, key=lambda f: (f[0], -f[2]))
    return pick[3], pick[0]


def _cut_forward(chunk):
    """Обрезать окно поиска на первой ЧУЖОЙ сильной подписи: иначе значение
    соседнего параметра приписывается текущему (так ПСК уезжала в «ставку»)."""
    m = _STRONG.search(chunk)
    return chunk[:m.start()] if m else chunk


def _cut_back(chunk):
    """То же назад: берём только хвост после последней чужой подписи."""
    last = None
    for m in _STRONG.finditer(chunk):
        last = m
    return chunk[last.end():] if last else chunk


def grace_periods(text):
    """Льготные периоды с указанием, К ЧЕМУ они относятся и при каком условии.

    Возвращает [{дней, применение, оговорки, цитата, текст}].  Именно здесь ловится
    ловушка ВТБ: «до 200 дней» — это на погашение карт ДРУГИХ банков, а на
    собственные покупки — до 110.  Сваливать их в одно число нельзя, поэтому
    назначение определяется по узкому окну ВОКРУГ САМОГО ЧИСЛА (сначала то, что
    идёт сразу за ним, потом то, что перед), а не по всему абзацу: в абзаце
    рядом стоят все три срока сразу, и широкое окно приписывает им общий смысл.
    Наличие слов «беспроцентный/льготный период» проверяется по широкому окну —
    подпись раздела может быть в стороне от строки с числом.
    """
    out = []
    for m in RX_DAYS.finditer(text):
        v = float(m.group(1))
        if not (10 <= v <= 400):
            continue
        wide = text[max(0, m.start() - 250):m.end() + 250]
        if not GRACE_LABEL.search(wide):
            continue                       # это не про льготный период, а про срок кредита
        # Назначение читаем СНАЧАЛА со своей строки, потом с соседних. Шире брать
        # нельзя: в тарифной таблице все три срока стоят подряд, и широкое окно
        # приписывает сроку на покупки формулировку про снятие наличных.
        ls = text.rfind("\n", 0, m.start()) + 1
        le = text.find("\n", m.end())
        le = len(text) if le < 0 else le
        ps = (text.rfind("\n", 0, ls - 1) + 1) if ls > 0 else 0
        ne = text.find("\n", le + 1)
        ne = len(text) if ne < 0 else ne
        same, neigh = text[ls:le], text[ps:ne]
        scope = _tag(same, GRACE_SCOPES, _tag(neigh, GRACE_SCOPES, "не указано"))
        if scope == "не указано" and not GRACE_LABEL.search(neigh):
            continue                       # число дней без подписи рядом — это не грейс
        q = neigh[:400]
        notes = []
        for pat in GRACE_NOTES:
            mm = re.search(pat, q, re.I)
            if mm:
                notes.append(mm.group(0).strip())
        out.append(dict(дней=v, применение=scope, оговорки=notes,
                        цитата=q, текст=m.group(0).strip()))
    return out


def parse_terms(text, source=None):
    """Числа из текста документа — каждое с дословной строкой-источником.

    Возвращает {ключ: {мин, макс, вид, текст, база/периодичность, подпись,
    цитата, варианты:[…]}}.  Цитата — непрерывный кусок исходного текста,
    проверенный на дословное вхождение; значение без такой цитаты не возвращается.
    В «вариантах» — прочие найденные значения того же параметра: если банк
    называет разные цифры в разных местах, это видно, а не замазано.
    """
    if not text:
        return {}
    res = {}
    for key, human, labels, kind, rng in PARAMS:
        cands = []
        for weight, pat in labels:
            for m in re.finditer(pat, text, re.I):
                a, b = m.start(), m.end()
                label = m.group(0)
                hint = text[max(0, a - 130):b + 160]
                # Смотрим и вперёд, и назад: в PDF-таблицах значение регулярно
                # оказывается ВЫШЕ подписи. Побеждает ближайшее, но значение на
                # ОДНОЙ строке с подписью всегда бьёт значение соседней строки —
                # иначе у ВТБ «29%-59,9% полная стоимость кредита» теряет свою
                # цифру и забирает чужую со следующей строки.
                variants = []
                fwd = _cut_forward(text[b:b + FWD])
                vf, off = _value(fwd, kind, rng, label, hint=hint)
                if vf is not None:
                    pen = 0 if "\n" not in fwd[:off] else 500
                    variants.append((off + pen, vf, (a, b + off + len(vf["текст"]))))
                back = _cut_back(text[max(0, a - BACK):a])
                base0 = a - len(back)
                vb, off2 = _value(back, kind, rng, label, hint=hint, last=True)
                if vb is not None:
                    d2 = len(back) - off2
                    pen = 0 if "\n" not in back[off2:] else 500
                    variants.append((d2 + pen, vb, (base0 + off2, b)))
                if not variants:
                    continue
                dist, val, span = min(variants, key=lambda x: x[0])
                dist %= 500
                if kind == "rate" and val.get("база") == "не указана":
                    continue              # ставка без базы начисления бессмысленна
                q = _quote(text, span[0], span[1])
                if not q or q not in text:
                    continue              # страховка: недословную цитату не отдаём
                if VETO_ALL.search(q):
                    continue              # это старая редакция условий, а не текущая цена
                if kind in ("rate", "pct") and len(set(RX_PCT.findall(q))) >= 3:
                    # Строка вроде «18,9% / 29,8% / 39,8% годовых 25,9% / …» из
                    # альбома тарифов Сбера: это несколько тарифных планов сразу.
                    # Выбирать из них наугад нельзя — параметр не извлекаем.
                    continue
                veto = VETO.get(key)
                if veto and veto.search(q):
                    continue              # цитата про другой продукт — не наш параметр
                cands.append(dict(вес=weight, расстояние=dist, позиция=a,
                                  подпись=" ".join(label.split())[:90], цитата=q, **val))
        if not cands:
            continue
        cands.sort(key=lambda c: (-c["вес"], c["расстояние"], c["позиция"]))
        best = cands[0]
        seen_v, alts = {(best["мин"], best["макс"])}, []
        for c in cands[1:]:
            kv = (c["мин"], c["макс"])
            if kv in seen_v:
                continue
            seen_v.add(kv)
            alts.append(dict(мин=c["мин"], макс=c["макс"], текст=c["текст"],
                             подпись=c["подпись"], цитата=c["цитата"]))
        best["варианты"] = alts[:3]
        best["параметр"] = human
        if source:
            best["источник"] = source
        res[key] = best

    # ПСК не должна подменять ставку: если совпало значение в значение, а рядом
    # был другой кандидат — берём его, а совпавший уводим в «варианты».
    psk = res.get("пск")
    if psk:
        for k in ("ставка_покупки", "ставка_наличные"):
            v = res.get(k)
            if not v or (v["мин"], v["макс"]) != (psk["мин"], psk["макс"]):
                continue
            if not v.get("варианты"):
                del res[k]                 # лучше «нет данных», чем ПСК под видом ставки
                continue
            alt = v["варианты"][0]
            res[k] = dict(v, мин=alt["мин"], макс=alt["макс"], текст=alt["текст"],
                          подпись=alt["подпись"], цитата=alt["цитата"],
                          варианты=[dict(мин=v["мин"], макс=v["макс"], текст=v["текст"],
                                         подпись=v["подпись"], цитата=v["цитата"])])

    # льготные периоды — по назначению
    per = grace_periods(text)
    buckets = {"покупки": "грейс_покупки",
               "перевод долга из других банков": "грейс_перевод_долга",
               "снятие наличных": "грейс_наличные",
               "не указано": "грейс_прочее"}
    known = set()
    for scope, key in buckets.items():
        same = [p for p in per if p["применение"] == scope]
        if not same:
            continue
        top = max(same, key=lambda p: p["дней"])
        if top["цитата"] not in text:
            continue
        # «Назначение не указано» показываем, только если это ДРУГОЙ срок:
        # тот же грейс, лишний раз названный на витрине, строку в таблице не стоит.
        if scope == "не указано" and top["дней"] in known:
            continue
        known.add(top["дней"])
        res[key] = dict(вид="дней", мин=top["дней"], макс=top["дней"], текст=top["текст"],
                        применение=scope, оговорки=top["оговорки"], цитата=top["цитата"],
                        подпись="льготный период", вес=90, расстояние=0, позиция=0,
                        параметр=HUMAN[key],
                        варианты=[dict(мин=p["дней"], макс=p["дней"], текст=p["текст"],
                                       подпись=scope, цитата=p["цитата"])
                                  for p in same if p["дней"] != top["дней"]][:3],
                        **({"источник": source} if source else {}))
    return res


def _compact(terms):
    """Сжатая форма для истории: только числа, без цитат (иначе файл распухнет)."""
    return {k: [v.get("мин"), v.get("макс"), v.get("текст")] for k, v in (terms or {}).items()}


def _n(x):
    if x is None:
        return "?"
    return (f"{x:.3f}".rstrip("0").rstrip(".") if x % 1 else f"{int(x)}").replace(".", ",")


def fmt_value(v):
    """Значение одной строкой, как его писать в отчёт. Периодичность и базу
    дописываем всегда: «590 ₽» без «в год» — число, которое нельзя использовать."""
    if not v:
        return "н/д"
    a, b, t = v.get("мин"), v.get("макс"), v.get("вид")
    if t == "дней":
        s = f"до {_n(b)} дн."
        return s + (f" ({', '.join(v['оговорки'])})" if v.get("оговорки") else "")
    if t == "процент+рубли":
        s = f"{_n(a)}% + {_n(v.get('рубли'))} ₽"
    elif t == "₽":
        s = "бесплатно" if (a == 0 and b == 0) else (f"{_n(a)} ₽" if a == b else f"{_n(a)}–{_n(b)} ₽")
    elif a == b:
        s = f"{_n(a)}%"
    else:
        s = f"{_n(a)}–{_n(b)}%"
    if v.get("оговорка"):
        s = f"{v['оговорка']} {s}"
    if v.get("минимум_руб"):
        s += f" (не менее {_n(v['минимум_руб'])} ₽)"
    p = v.get("периодичность")
    if p and p not in ("—", "не указана"):
        s += f" {p}"
    bs = v.get("база")
    if bs and bs != "не указана" and s.endswith("%"):
        s += f" {bs}"
    return s
# ═══════════════════════════ 4. ПО БАНКУ И СРАВНЕНИЕ ═══════════════════════════

def _trust(doc, meta):
    """Насколько доверяем источнику: официальный PDF-тариф > страница тарифов > витрина."""
    t = meta.get("балл", 0)
    if doc.get("вид") == "PDF":
        t += 20
    return t


MANUAL = os.path.join(CACHE, "manual")     # ручные копии документов (tariff_fetch.py с ноута)

def manual_docs(bank_key=None, product=None):
    """Документы, загруженные вручную (tariff_fetch.py): {url: запись}. Используются ТОЛЬКО в приватном
    режиме (RADAR_PRIVATE=1 — разбор для владельца в Джарвисе): часть из них закрыта robots.txt, и в канал
    построенное на них не идёт (13.09.2026, решение Никиты)."""
    out = {}
    try:
        names = os.listdir(MANUAL)
    except Exception:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(MANUAL, n), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if not d.get("текст"):
            continue
        if bank_key and d.get("банк") != bank_key:
            continue
        if product and d.get("продукт") and d.get("продукт") != product:
            continue
        out[d["url"]] = d
    return out


def private_mode():
    return os.environ.get("RADAR_PRIVATE") == "1"


def bank_terms(bank_key, product="кредитная карта", max_docs=8, refresh=False):
    """Параметры продукта у одного банка.

    Возвращает {'банк', 'название', 'продукт', 'параметры': {…}, 'документы': […],
    'проблемы': […]}.  В каждом параметре — дословная цитата и ссылка на документ.
    """
    b = sources.BANKS.get(bank_key) or {}
    out = dict(банк=bank_key, название=b.get("name", bank_key), продукт=product,
               параметры={}, документы=[], проблемы=[])
    if b.get("blocked"):
        out["проблемы"].append("банк закрыт защитой от роботов — обходить не стали")
        return out
    docs = find_docs(bank_key, product)
    manual = manual_docs(bank_key, product) if private_mode() else {}
    for u, md in manual.items():                       # ручные копии, которых обход не нашёл, — тоже в работу
        if not any(d["url"] == u for d in docs):
            docs = list(docs) + [dict(url=u, текст=md.get("заметка") or "ручная копия", продукт=product,
                                      вид=md.get("вид") or "HTML", балл=100, разрешено=core.allowed(u), откуда="manual")]
    if not docs:
        out["проблемы"].append("тарифные документы не найдены")
        return out
    for meta in docs[:max_docs + len(manual)]:
        md = manual.get(meta["url"])
        if md:
            d = dict(url=meta["url"], вид=md.get("вид") or meta["вид"], текст=md["текст"], sha=md.get("sha"),
                     ts=md.get("ts"), ошибка=None, из_кэша=True, изменился=False, ручная=True)
        elif not meta["разрешено"]:
            out["документы"].append(dict(url=meta["url"], статус="запрещено robots.txt",
                                         вид=meta["вид"], текст_ссылки=meta["текст"]))
            out["проблемы"].append(f"robots.txt запрещает обход: {meta['url']}")
            continue
        else:
            d = fetch_doc(meta["url"], refresh=refresh, bank=bank_key)
        if not d.get("текст"):
            out["документы"].append(dict(url=meta["url"], статус=d.get("ошибка") or "пусто",
                                         вид=meta["вид"], текст_ссылки=meta["текст"]))
            continue
        terms = parse_terms(d["текст"], source=meta["url"])
        out["документы"].append(dict(url=meta["url"], статус="ручная копия от " + str(d.get("ts", ""))[:10] if d.get("ручная") else "ок", вид=d["вид"],
                                     знаков=len(d["текст"]), sha=d["sha"],
                                     изменился=d.get("изменился", False),
                                     нашлось=sorted(terms), текст_ссылки=meta["текст"]))
        tr = _trust(d, meta) + (10 if d.get("ручная") else 0)
        for k, v in terms.items():
            v["доверие"] = tr
            v["документ"] = meta["url"]
            v["ручная_копия"] = bool(d.get("ручная"))
            v["вид_документа"] = d["вид"]
            cur = out["параметры"].get(k)
            if cur is None or v["доверие"] > cur["доверие"] or (
                    v["доверие"] == cur["доверие"] and v["вес"] > cur["вес"]):
                out["параметры"][k] = v
    # Грейс «назначение не указано» держим, только если это НЕ повтор уже
    # разобранного срока из другого документа: у Газпромбанка «карта с льготным
    # периодом до 120 дней» из меню сайта — те же 120 дней, что и по покупкам.
    other = {out["параметры"][k]["макс"] for k in
             ("грейс_покупки", "грейс_перевод_долга", "грейс_наличные")
             if k in out["параметры"]}
    g = out["параметры"].get("грейс_прочее")
    if g and g["макс"] in other:
        del out["параметры"]["грейс_прочее"]
    return out


def compare(product="кредитная карта", banks=None, refresh=False):
    """Сравнение банков по параметрам продукта. {ключ банка: результат bank_terms}."""
    banks = banks or [k for k, v in sources.BANKS.items() if product in v.get("pages", {})]
    return {k: bank_terms(k, product, refresh=refresh) for k in banks}


def compare_table(cmp_res, keys=None):
    """Markdown-таблица сравнения."""
    keys = keys or [k for k, _h in DISPLAY]
    cols = [k for k in cmp_res if cmp_res[k]["параметры"]]
    if not cols:
        return "(данных нет)"
    head = ["Параметр"] + [cmp_res[k]["название"] for k in cols]
    rows = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"]
    for p in keys:
        if not any(cmp_res[k]["параметры"].get(p) for k in cols):
            continue
        rows.append("| " + " | ".join([HUMAN.get(p, p)] +
                    [fmt_value(cmp_res[k]["параметры"].get(p)) for k in cols]) + " |")
    return "\n".join(rows)


# ═══════════════════════════ 5. ОТСЛЕЖИВАНИЕ ИЗМЕНЕНИЙ ═════════════════════════

def doc_history(url, limit=10):
    """История версий документа: [{ts, sha, параметры}] от старых к новым."""
    p = _hist_path(url)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out[-limit:]


def changes(bank_key=None, product="кредитная карта", since_days=2, rescan=False):
    """Что изменилось в тарифных документах со вчера.

    Сравнивает две последние версии каждого известного документа и возвращает
    список изменившихся параметров: [{банк, url, параметр, было, стало, ts}].
    Документ без второй версии молчит: «изменений нет» и «не с чем сравнивать» —
    разные вещи, путать их нельзя.
    Если rescan=True, документы сначала перекачиваются (иначе смотрим накопленное).
    """
    banks = [bank_key] if bank_key else list(sources.BANKS)
    urls = []
    if rescan:
        for bk in banks:
            for d in find_docs(bk, product, max_docs=8):
                if d["разрешено"]:
                    fetch_doc(d["url"], refresh=True, bank=bk)
                    urls.append((bk, d["url"]))
    else:
        # Идём по индексу: банк документа записан туда при скачивании,
        # угадывать его по домену не нужно (у Альфы тарифы вообще на servicecdn.ru).
        for u, rec in _load_index().items():
            if rec.get("банк") in banks:
                urls.append((rec["банк"], u))
    out = []
    cutoff = time.time() - since_days * 86400
    for bk, url in urls:
        h = doc_history(url, limit=2)
        if len(h) < 2:
            continue
        old, new = h[-2], h[-1]
        try:
            t_new = time.mktime(time.strptime(new["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            t_new = time.time()
        if t_new < cutoff:
            continue
        if old.get("версия_разбора") != new.get("версия_разбора"):
            continue                 # менялся парсер, а не документ — это не новость
        po, pn = old.get("параметры") or {}, new.get("параметры") or {}
        norm = lambda v: (v[0], v[1], " ".join(str(v[2]).lower().split())) if v else None
        for k in sorted(set(po) | set(pn)):
            if norm(po.get(k)) != norm(pn.get(k)):   # регистр и пробелы — не изменение
                out.append(dict(банк=bk, url=url, параметр=HUMAN.get(k, k),
                                было=(po.get(k) or [None, None, "н/д"])[2],
                                стало=(pn.get(k) or [None, None, "н/д"])[2], ts=new["ts"]))
    return out


# ═══════════════════════════ 6. БЛОК ДЛЯ АНАЛИТИКА ═════════════════════════════

CTX_CACHE = os.path.join(CACHE, "context.json")
CTX_TTL = 20 * 3600            # тарифы меняются не чаще раза в сутки
# Кого показываем аналитику по умолчанию — как RADAR_PEERS в cbr.py.
# Полный обход реестра занимает десятки минут (Сбер и Альфа требуют рендера).
RADAR_PEERS = ["sber", "tbank", "alfa", "vtb", "gpb", "ozon"]

def context_block(product="кредитная карта", refresh=False, limit=2500, banks=None):
    """Краткий блок для контекста аналитика — по образцу cbr.context_block().
    Только числа с подтверждённой дословной цитатой; ограничения названы прямо."""
    cache = CTX_CACHE.replace(".json", "_private.json") if private_mode() else CTX_CACHE
    if not refresh:
        try:
            with open(cache, encoding="utf-8") as f:
                c = json.load(f)
            if time.time() - c.get("ts", 0) < CTX_TTL and c.get("text"):
                return c["text"]
        except Exception:
            pass
    try:
        cmp_res = compare(product, banks=banks or RADAR_PEERS, refresh=refresh)
    except Exception as e:
        return f"[тарифные документы недоступны: {type(e).__name__}: {e}]"
    lines = [
        f"ТАРИФЫ КОНКУРЕНТОВ — «{product}», на {core.msk():%d.%m.%Y}. Источник — "
        "опубликованные банком тарифы и условия (PDF или страница условий), у каждого "
        "числа в state/tariffs лежит дословная цитата.",
        "Как читать: (1) диапазон ставки/ПСК — границы по линейке, конкретному клиенту "
        "банк ставит своё значение; (2) льготный период указан ОТДЕЛЬНО для покупок и "
        "для перевода долга из других банков — это разные сроки, у ВТБ 110 и 200; "
        "(3) часть PDF-тарифов закрыта robots.txt, тогда взято из HTML-страницы условий; "
        "(4) тарифные планы банков не эквивалентны, сравнение «в лоб» некорректно.", ""]
    for k, r in sorted(cmp_res.items(), key=lambda kv: -len(kv[1]["параметры"])):
        pr = r["параметры"]
        if not pr:
            continue
        bits = []
        for key, human in DISPLAY:
            if key in pr:
                bits.append(f"{human.split(',')[0].lower()} {fmt_value(pr[key])}"
                            + (" [ручная копия документа, только для приватного разбора]" if pr[key].get("ручная_копия") else ""))
        lines.append(f"• {r['название']}: " + "; ".join(bits) + ".")
    miss = [r["название"] for r in cmp_res.values() if not r["параметры"]]
    if miss:
        lines.append("Данных нет: " + ", ".join(miss) + ".")
    text = "\n".join(lines)[:limit]
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "text": text}, f, ensure_ascii=False)
    except Exception:
        pass
    return text


# ═══════════════════════════════ CLI ═══════════════════════════════════════════

def _print_terms(r):
    print(f"\n=== {r['название']} · {r['продукт']} ===")
    for d in r["документы"]:
        mark = "OK" if d["статус"] == "ок" else "--"
        print(f"  {mark} [{d.get('вид') or '?'}] {d['url']}")
        print(f"     {d['статус']}" + (f"; нашлось: {', '.join(d['нашлось'])}"
                                       if d.get("нашлось") else ""))
    if not r["параметры"]:
        print("  параметры не извлечены")
    for key, human in DISPLAY:
        v = r["параметры"].get(key)
        if not v:
            continue
        print(f"\n  {human}: {fmt_value(v)}")
        print(f"     подпись в документе: «{v['подпись']}»")
        print("     цитата:")
        for line in v["цитата"].split("\n"):
            print("       | " + line)
        print(f"     источник: {v['документ']}")
        for a in v.get("варианты", []):
            print(f"     (другое значение в том же документе: {a['текст']} — «{a['подпись']}»)")
    for p in r["проблемы"]:
        print("  ! " + p)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "context"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    prod = sys.argv[3] if len(sys.argv) > 3 else "кредитная карта"
    ref = "--refresh" in sys.argv
    if cmd == "docs":
        for d in find_docs(arg, prod):
            print(f"{d['балл']:>3} {'да ' if d['разрешено'] else 'НЕТ'} [{d['вид']}] "
                  f"{d['url']}\n      «{d['текст'][:90]}»")
    elif cmd == "fetch":
        d = fetch_doc(arg, refresh=ref)
        print(d["вид"], d["sha"], d.get("ошибка"), len(d.get("текст") or ""))
        print((d.get("текст") or "")[:3000])
    elif cmd == "bank":
        _print_terms(bank_terms(arg, prod, refresh=ref))
    elif cmd == "compare":
        res = compare(arg or "кредитная карта", refresh=ref)
        for r in res.values():
            _print_terms(r)
        print("\n" + compare_table(res))
    elif cmd == "history":
        for h in doc_history(arg, limit=20):
            print(h["ts"], h["sha"], json.dumps(h["параметры"], ensure_ascii=False)[:300])
    elif cmd == "changes":
        ch = changes(arg, prod, rescan=ref)
        print("изменений нет" if not ch else "")
        for c in ch:
            print(f"{c['банк']}: {c['параметр']}: {c['было']} → {c['стало']}  ({c['ts']})")
    else:
        print(context_block(refresh=ref))


# ---------------------------------------------------------------------------
# Блок дословных цитат для проверяющих и для автора поста.
# context_block отдаёт только числа; при проверке текста это приводило к тому,
# что подтверждённое значение объявлялось неподтверждённым — цитаты до проверки
# просто не доезжали. Здесь те же числа, но вместе с их источником.
# ---------------------------------------------------------------------------
def quotes_block(product="кредитная карта", keys=("ПСК",), banks=None, limit=4000):
    try:
        cmp_res = compare(product, banks=banks or RADAR_PEERS)
    except Exception as e:
        return f"[тарифные цитаты недоступны: {type(e).__name__}: {e}]"
    out = [f"ДОСЛОВНЫЕ ЦИТАТЫ ИЗ ТАРИФНЫХ ДОКУМЕНТОВ — «{product}».",
           "Каждая цитата проверена на вхождение в исходный текст документа "
           "символ в символ. Значение без цитаты сюда не попадает.", ""]
    for r in sorted(cmp_res.values(), key=lambda x: x["название"]):
        for key, val in (r.get("параметры") or {}).items():
            if keys and not any(k.lower() in key.lower() for k in keys):
                continue
            q = (val or {}).get("цитата") if isinstance(val, dict) else None
            if not q:
                continue
            out.append(f"• {r['название']} — {HUMAN.get(key, key)}: {fmt_value(val)}")
            out.append(f"    цитата: {' '.join(str(q).split())[:300]}")
    return "\n".join(out)[:limit]
