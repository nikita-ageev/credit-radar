# -*- coding: utf-8 -*-
"""Пресс-релизы бюро кредитных историй — первоисточник рыночной статистики (выдачи, лимиты, отказы, просрочка).
13.09.2026, Никита: «БКИ более надёжный источник, чем тг-каналы». НБКИ отдаёт страницу по HTTP с JSON-LD, ОКБ — за
JS-защитой, читаем headless-браузером Радара (core.get_rendered), в странице лежит JSON WordPress. Скоринг Бюро — общий разбор
ссылок без дат (новое = впервые увиденное). Кредистория пресс-релизов не публикует (только блог), не читаем. Формат элемента — как у news.fresh."""
import re, json, html as H
from datetime import datetime, timezone, timedelta
import core

SOURCES = [
    ("НБКИ", "https://nbki.ru/company/news/", "nbki"),
    ("ОКБ", "https://bki-okb.ru/press/news", "okb"),
    ("Скоринг Бюро", "https://scoring.ru/press/", "generic"),
]

def _dt(s):
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone(timedelta(hours=3)))
    except Exception:
        return None

def _nbki(h):
    out = []
    for m in re.finditer(r'"@type":\s*"BlogPosting".*?"name":\s*"([^"]+)".*?"url":\s*"https://nbki\.ru([^"]*)".*?"datePublished":\s*"([^"]+)"', h, re.S):
        title, path, date = H.unescape(m.group(1)), m.group(2), m.group(3)
        slug = path.strip("/").split("/")[-1]                      # в разметке НБКИ url битый: nbki.ru<slug>/
        out.append(dict(title=title, link=f"https://nbki.ru/company/news/{slug}/", dt=_dt(date), desc=""))
    return out

_MONTHS = {"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,"июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}
def _ru_date(s):
    m = re.match(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", s.strip())
    if not m or m.group(2) not in _MONTHS: return None
    return datetime(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)), 12, tzinfo=timezone(timedelta(hours=3)))

def _okb(h):
    """Карточки press-item: ссылка, текст, дата «09 сентября 2026»."""
    out = []
    for m in re.finditer(r'press-item__link" href="(/press/news/[^"]+)".*?press-item__text">(.*?)</p>.*?press-item__date">([^<]+)<', h, re.S):
        title = H.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        out.append(dict(title=title, link="https://bki-okb.ru" + m.group(1), dt=_ru_date(m.group(3)), desc=""))
    return out

def _generic(h, base):
    out = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", h, re.S | re.I):
        href = re.search(r'href=["\']([^"\']+)', m.group(1)); t = H.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        if not href or len(t) < 30 or not re.search(r"news|press|novost", href.group(1)): continue
        u = href.group(1)
        if u.startswith("/"): u = re.match(r"https?://[^/]+", base).group(0) + u
        out.append(dict(title=t, link=u, dt=None, desc=""))
    return out

def items(hours=72):
    """Свежие релизы БКИ. У элементов без даты (generic) дата — момент первого появления (state/bki_seen.json)."""
    import os
    seen_p = os.path.join(core.STATE, "bki_seen.json")
    try: seen = json.load(open(seen_p, encoding="utf-8"))
    except Exception: seen = {}
    now = datetime.now(timezone.utc); since = now - timedelta(hours=hours)
    out, errs = [], []
    for name, url, kind in SOURCES:
        try:
            h, txt, how, err = core.get_rendered(url)
            if not h:
                errs.append(f"{name}: {err or 'пусто'}"); continue
            its = _nbki(h) if kind == "nbki" else _okb(h) if kind == "okb" else _generic(h, url)
            if not its:
                errs.append(f"{name}: релизы не распознаны"); continue
            seeded = seen.get("__seeded__" + name)
            links_seen = set()
            for x in its:
                if x["link"] in links_seen: continue
                links_seen.add(x["link"])
                if x["dt"] is None:
                    first = seen.get(x["link"])
                    if first is None:
                        first = now.timestamp() if seeded else 0.0     # первый прогон: всё старое, ничего не отдаём
                        seen[x["link"]] = first
                    x["dt"] = datetime.fromtimestamp(first, timezone.utc)
                if x["dt"] < since: continue
                out.append(dict(src=name, **x))
            seen["__seeded__" + name] = now.timestamp()
        except Exception as e:
            errs.append(f"{name}: {type(e).__name__}: {str(e)[:60]}")
    try:
        cutoff = now.timestamp() - 60 * 86400
        json.dump({k: v for k, v in seen.items() if v > cutoff or v == 0.0 or k.startswith("__seeded__")}, open(seen_p, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return out, errs

if __name__ == "__main__":
    its, errs = items(hours=24 * 14)
    for x in sorted(its, key=lambda x: x["dt"], reverse=True):
        print(f"{x['dt']:%d.%m} [{x['src']}] {x['title'][:100]}  {x['link']}")
    print("ошибки:", errs)
