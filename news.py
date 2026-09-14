# -*- coding: utf-8 -*-
"""Новости рынка розничного кредита из открытых RSS: Frank Media, Banki.ru, «Ведомости.
Финансы», «Коммерсантъ. Финансы», плюс релевантные новости Банка России (через cbr_news).

Зачем: витрины конкурентов меняются редко, а рынок живёт каждый день. Дневной пост
без новостного слоя — это пересказ тишины. Здесь только отбор и нормализация; никакой
модели, никаких выводов — их делает мозг.

Каждый элемент: источник, заголовок, дата, ссылка, короткое описание. Отбор по словарю
розничного кредита; окно — последние 36 часов; повторы по заголовку схлопываются.
Состояние: state/news_seen.json (что уже показывали, чтобы не повторять день за днём).
"""
import os, re, json, html, time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

import core

FEEDS = [
    ("Frank Media",   "https://frankmedia.ru/feed"),
    ("Banki.ru",      "https://www.banki.ru/xml/news.rss"),
    ("Ведомости",     "https://www.vedomosti.ru/rss/rubric/finance"),
    ("Коммерсантъ",   "https://www.kommersant.ru/RSS/section-finance.xml"),
]
SEEN = os.path.join(core.STATE, "news_seen.json")

KEEP = re.compile(r"""
    кредит | \bкарт[аыуе]?\b | кредитн\w*\s+карт | рассрочк | \bBNPL\b | займ | заём | микрофинанс | \bМФО\b |
    \bПСК\b | \bМПЛ\b | \bПДН\b | макропруденц | надбавк | ключев\w*\s+ставк | ставк\w*\s+по\s+кредит |
    просроч | коллектор | \bБКИ\b | \bНБКИ\b | \bОКБ\b | скоринг | долгов\w*\s+нагруз |
    розничн\w*\s+кредитован | потребкредит | потребительск\w*\s+кредит | автокредит | ипотек |
    Сбер | Т-Банк | Тинькофф | Т-Технолог | Альфа-Банк | \bВТБ\b | Газпромбанк | Совкомбанк | МТС\s*Банк |
    Яндекс\s*Банк | Озон\s*Банк | Ozon | Wildberries | Вайлдберриз | \bWB\b | ОТП | Ренессанс | Уралсиб |
    Банк\s+России | \bЦБ\b | Набиуллин | самозапрет | период\s+охлаждения | мошеннич | кэшбэк | кешбэк
""", re.I | re.X)
DROP = re.compile(r"страхов|ОСАГО|каско|вклад\w*\s+под|курс\s+(доллар|евро|юан)|облигаци|IPO|дивиденд", re.I)

def _fetch(url):
    body, err = core.get(url, timeout=30)
    if body is None:
        raise RuntimeError(err)
    return body

def _items(src, url):
    out = []
    try:
        raw = _fetch(url)
    except Exception as e:
        return out, f"{src}: {type(e).__name__}: {str(e)[:60]}"
    # Banki.ru отдаёт XML с невалидными символами и голыми «&» — парсер падает на всей ленте.
    # Поэтому разбираем по item-блокам регулярками: терпимо к мусору, теряет только битый item.
    for m in re.finditer(r"<item\b.*?</item>", raw, re.S):
        block = m.group(0)
        def fld(tag):
            mm = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
            v = mm.group(1) if mm else ""
            v = re.sub(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", r"\1", v, flags=re.S)
            return v.strip()
        t = html.unescape(" ".join(fld("title").split()))
        l = html.unescape(fld("link"))
        d = html.unescape(re.sub(r"<[^>]+>", " ", fld("description")))
        d = re.sub(r"Сообщение .*? появилось сначала на Frank Media\s*\.?", "", d)   # хвост Frank Media
        d = " ".join(d.split())[:240]
        p = fld("pubDate")
        try:
            dt = parsedate_to_datetime(p)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
        out.append(dict(src=src, title=t, link=l, desc=d, dt=dt))
    return out, None

RETAIL = re.compile(r"кредитн\w*\s+карт|\bкарт[аыуе]?\b|рассрочк|наличными|потреб\w*\s*кредит|потребкредит|ипотек|автокредит|"
                    r"\bМФО\b|микрозайм|микрофинанс|займ|заём|физлиц|физическ\w*\s+лиц|розничн|населени|заёмщик|заемщик|"
                    r"\bПДН\b|\bМПЛ\b|\bПСК\b|долгов\w*\s+нагруз|просроч|коллектор|\bБКИ\b|\bНБКИ\b|\bОКБ\b|скоринг|"
                    r"ключев\w*\s+ставк|ставк\w*\s+по\s+кредит|\bBNPL\b|льготн\w*\s+период|минимальн\w*\s+плат", re.I)
TITLE_OK = re.compile(r"ставк|кредит|заём|займ|\bбанк(и|ов|ам|ами|ах|е)?\b|\bЦБ\b|Банк\w*\s+России|\bБКИ\b|\bМФО\b|ипотек|рассрочк|\bкарт|просроч|скоринг|заёмщик|заемщик|\bПДН\b|\bМПЛ\b|коллектор|" + "".join([]) + r"Сбер|ВТБ|Т-Банк|Тинькофф|Альфа|Газпромбанк|Совкомбанк|Озон|Ozon|Яндекс|Wildberries|ОТП|Уралсиб", re.I)
B2B = re.compile(r"юрлиц|бизнес[уа]?\b|компани[ийя]|корпоратив|\bМСБ\b|\bМСП\b|малого\s+бизнеса|предпринимател|\bРКО\b|эквайринг|"
                 r"факторинг|лизинг|госзакуп|облигац|\bIPO\b|эмитент", re.I)

def _relevant(x):
    """Розничный кредит, а не «что-то про банк». 14.09.2026: новость «компании из СНГ задолжали бизнесу» прошла
    по имени банка — теперь имя банка само по себе не пропуск; B2B-новость проходит только с розничным термином."""
    blob = x["title"] + " " + x["desc"]
    if DROP.search(x["title"]) and not re.search(r"кредит|карт|рассрочк", x["title"], re.I):
        return False
    if not KEEP.search(blob):
        return False
    if B2B.search(x["title"]) and not RETAIL.search(x["title"]):
        return False                                   # B2B в заголовке — розничный термин нужен в самом заголовке
    if not RETAIL.search(blob) and not re.search(r"кредит", blob, re.I):
        return False
    # заголовок сам должен быть про кредит/ставки/банки: «Король Петербургской биржи и просто Штиглиц» (14.09) прошёл
    # по слову в анонсе. Первоисточники (ЦБ, БКИ) — исключение: у них всё по теме.
    if x.get("src") not in ("Банк России", "НБКИ", "ОКБ", "Скоринг Бюро") and not TITLE_OK.search(x["title"]):
        return False
    return True

def _load_seen():
    try:
        with open(SEEN, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def _save_seen(s):
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    cutoff = time.time() - 7 * 86400
    s = {k: v for k, v in s.items() if v > cutoff}
    with open(SEEN, "w", encoding="utf-8") as f: json.dump(s, f, ensure_ascii=False)

def fresh(hours=36, mark=True):
    """Свежие релевантные новости за окно. mark=True — запомнить показанные."""
    now = datetime.now(timezone.utc); since = now - timedelta(hours=hours)
    seen = _load_seen(); res, errs, keys = [], [], set()
    for src, url in FEEDS:
        items, err = _items(src, url)
        if err: errs.append(err)
        for x in items:
            if x["dt"] and x["dt"] < since: continue
            if not _relevant(x): continue
            k = re.sub(r"\W+", " ", x["title"].lower()).strip()[:80]
            if k in keys: continue
            keys.add(k)
            x["repeat"] = k in seen
            res.append(x)
    # новости ЦБ — через уже работающий регуляторный радар, без его состояния
    try:
        import cbr_news
        for kind, url in cbr_news.FEEDS:
            for x in cbr_news.items(kind, url):
                if not cbr_news.relevant(x): continue
                try:
                    dt = parsedate_to_datetime(x["date"])
                    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    dt = None
                if dt and dt < now - timedelta(hours=72): continue
                k = re.sub(r"\W+", " ", x["title"].lower()).strip()[:80]
                if k in keys: continue
                keys.add(k)
                res.append(dict(src="Банк России", title=x["title"], link=x["link"],
                                desc=x["desc"][:240], dt=dt, repeat=k in seen))
    except Exception as e:
        errs.append(f"ЦБ: {type(e).__name__}")
    # Пресс-релизы БКИ (13.09.2026): первоисточник статистики, надёжнее каналов — идут раньше в списке
    try:
        import bkinews
        bk_items, bk_errs = bkinews.items(hours=max(hours, 48))
        errs += bk_errs
        for x in bk_items:
            if not _relevant(x): continue
            k = re.sub(r"\W+", " ", x["title"].lower()).strip()[:80]
            if k in keys: continue
            keys.add(k)
            x["repeat"] = k in seen
            res.append(x)
    except Exception as e:
        errs.append(f"БКИ: {type(e).__name__}")
    # Telegram-каналы отрасли (13.09.2026): выходят раньше прессы; фильтр релевантности тот же
    try:
        import tgnews
        tg_items, tg_errs = tgnews.items(hours=hours)
        errs += tg_errs
        for x in tg_items:
            if not _relevant(x): continue
            k = re.sub(r"\W+", " ", x["title"].lower()).strip()[:80]
            if k in keys: continue
            keys.add(k)
            x["repeat"] = k in seen
            res.append(x)
    except Exception as e:
        errs.append(f"TG: {type(e).__name__}")
    res.sort(key=lambda x: (x["repeat"], -(x["dt"].timestamp() if x["dt"] else 0)))
    if mark:
        for x in res: seen[re.sub(r"\W+", " ", x["title"].lower()).strip()[:80]] = time.time()
        _save_seen(seen)
    return res, errs

def context_block(hours=36, limit=28):
    # 14.09.2026: контекст дня НЕ помечает новости показанными — иначе сухой прогон «съедает» новости у настоящего
    # выпуска (пометка — только после публикации, news.mark_seen в radar.daily / evening.run)
    res, errs = fresh(hours, mark=False)
    lines = [f"НОВОСТИ РЫНКА за последние {hours} ч (открытые RSS: Frank Media, Banki.ru, Ведомости, "
             "Коммерсантъ, Банк России). Это заголовки и анонсы СМИ, не первоисточник: цитировать "
             "можно дословно как «по сообщению <источник>», выводы делать осторожно."]
    if not res:
        lines.append("Релевантных новостей не найдено." + (f" Сбои: {'; '.join(errs)}" if errs else ""))
        return "\n".join(lines)
    for x in res[:limit]:
        when = x["dt"].astimezone(timezone(timedelta(hours=3))).strftime("%d.%m %H:%M") if x["dt"] else "дата н/д"
        tag = " (уже было вчера)" if x.get("repeat") else ""
        lines.append(f"— [{x['src']}] {when}{tag}: {x['title']}" + (f" — {x['desc']}" if x['desc'] else "") + f"\n   {x['link']}")
    if errs:
        lines.append("Недоступны: " + "; ".join(errs))
    return "\n".join(lines)

if __name__ == "__main__":
    print(context_block())


def mark_seen(items):
    """Запомнить показанные новости (после публикации сводки), не перекачивая ленты."""
    seen = _load_seen()
    for x in items or []:
        seen[re.sub(r"\W+", " ", x["title"].lower()).strip()[:80]] = time.time()
    _save_seen(seen)
