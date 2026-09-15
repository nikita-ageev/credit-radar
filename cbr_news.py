# -*- coding: utf-8 -*-
"""Регуляторный радар: новости ЦБ РФ по рискам и экономике кредитных продуктов → Telegram.

Источники (открытые RSS Банка России):
    https://www.cbr.ru/rss/RssPress  — пресс-релизы
    https://www.cbr.ru/rss/RssNews   — «новое на сайте» (указания, доклады, статистика)
Фильтр — по ключевым словам розничного кредитного риска (МПЛ, ПСК, ключевая ставка,
ПДН, надбавки, резервы, МФО, БКИ, рассрочка …). Шум (ломбардный список, обеспечение,
монеты, технические работы) отсекается явно. Новые совпадения шлются в личный чат
через токен Джарвиса (/opt/jarvis/advisor.env). Состояние: state/cbr_news_seen.json.

Запуск:  python cbr_news.py          — проверить и отправить новое (крон 2 раза в день: 09:30 и 19:30 МСК)
         python cbr_news.py --init   — пометить текущую ленту прочитанной, ничего не слать
Заказ: Никита, 09.09.2026 («регуляторно-цбшный радар — пиши мне все новости от ЦБ
касающиеся рисков и экономики кредитных продуктов»).
"""
import os, re, sys, json, html, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

import core   # свой CA-бандл (certs/bundle.pem), паузы, robots.txt

HERE   = os.path.dirname(os.path.abspath(__file__))
STATE  = os.path.join(HERE, "state", "cbr_news_seen.json")
ENV    = "/opt/jarvis/advisor.env"
FEEDS  = [("Пресс-релиз", "https://www.cbr.ru/rss/RssPress"),
          ("На сайте ЦБ", "https://www.cbr.ru/rss/RssNews")]

KEEP = re.compile(r"""
    макропруденц | \bМПЛ\b | надбав | \bПСК\b | полн(?:ой|ая|ую)\s+стоимост |
    ключев(?:ой|ая|ую)\s+ставк | потребительск | кредит(?:ов|ы|а|ах|ам|н)  | \bПДН\b |
    долгов(?:ой|ая|ую)\s+нагруз | резерв | розничн | ипотек | микрофинанс | \bМФО\b |
    рассрочк | \bБКИ\b | кредитн\w*\s+истори | коллектор | \bФССП\b | просроч |
    скоринг | банковск\w*\s+сектор | денежно-кредитн | заемщик | заёмщик |
    самозапрет | период\s+охлаждения | мошеннич | 353-ФЗ | 230-ФЗ | базов\w*\s+стандарт
""", re.I | re.X)
DROP = re.compile(r"""
    ломбардн | принимаемых\s+в\s+обеспечение | технические\s+работы | монет | скульптур |
    инсайдерск | выставк | музе | лекци | конкурс | валютн\w*\s+курс | официальн\w*\s+курс |
    репо\b | депозитн\w*\s+аукцион | купонн | максимальных\s+процентных\s+ставок |
    отозвана\s+лицензия | юридических\s+лиц | постоянного\s+действия | интерактивное\s+представление |
    средствам\s+в\s+иностранной | кредитных\s+организаций\s+средствам | международные\s+резервы |
    ликвидности\s+банковского | статистические\s+показатели | динамические\s+ряды
""", re.I | re.X)

def fetch(url):
    body, err = core.get(url, timeout=30)
    if body is None:
        raise RuntimeError(err)
    return body.encode("utf-8")

def items(kind, url):
    out = []
    try:
        root = ET.fromstring(fetch(url))
    except Exception as e:
        print(f"{kind}: ошибка загрузки: {e}", file=sys.stderr); return out
    for it in root.iter("item"):
        t = html.unescape((it.findtext("title") or "").strip())
        l = (it.findtext("link") or "").strip()
        d = html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or "")).strip()
        p = (it.findtext("pubDate") or "").strip()
        out.append(dict(kind=kind, title=t, link=l, desc=d[:300], date=p, id=l or t))
    return out

# «Новое на сайте» — поток в основном статистический. Оставляем только нормативные акты и
# доклады: указание/положение/инструкция/проект/доклад/консультац/решение/информационное письмо.
NORM = re.compile(r"указани|положени|инструкци|проект|доклад|консультац|решени|информационн\w*\s+письм|"
                  r"методическ|разъяснен|обзор\s+ключев|среднесрочн\w*\s+прогноз|финансов\w*\s+стабильност", re.I)
STAT = re.compile(r"статистик|показател|бюллетен|динамик|ряды|сведения\s+о|информация\s+по|за\s+\w+\s+20\d\d\s+года|"
                  r"на\s+\d\d\.\d\d\.20\d\d|№\s*\d+\s*\(", re.I)

def relevant(x):
    blob = x["title"] + " " + x["desc"]
    if DROP.search(x["title"]) or not KEEP.search(blob):
        return False
    if x["kind"] == "На сайте ЦБ":
        return bool(NORM.search(x["title"])) and not STAT.search(x["title"])
    return True

def load_seen():
    try:
        with open(STATE, encoding="utf-8") as f: return set(json.load(f))
    except Exception: return set()

def save_seen(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f: json.dump(sorted(s)[-2000:], f, ensure_ascii=False)

def tg_send(text):
    env = {}
    for line in open(ENV, encoding="utf-8"):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1); env[k] = v.strip().strip('"')
    tok, chat = env["TG_BOT_TOKEN"], env["OWNER_CHAT_ID"]
    for i in range(0, len(text), 3900):
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[i:i+3900],
                                       "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=20)

def fmt_date(p):
    for f in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try: return datetime.strptime(p, f).strftime("%d.%m %H:%M")
        except Exception: pass
    return p[:16]

def main(init=False):
    seen = load_seen()
    fresh = []
    for kind, url in FEEDS:
        for x in items(kind, url):
            if x["id"] in seen: continue
            seen.add(x["id"])
            if relevant(x): fresh.append(x)
    save_seen(seen)
    if init:
        print(f"init: помечено {len(seen)}, из них релевантных {len(fresh)} (не отправлено)"); return
    if not fresh:
        print("нового нет"); return
    lines = ["ЦБ — регуляторный радар"]
    for x in fresh:
        lines.append(f"\n{x['kind']} · {fmt_date(x['date'])}\n{x['title']}\n{x['link']}")
    tg_send("\n".join(lines))
    print(f"отправлено {len(fresh)}: " + " | ".join(x["title"][:60] for x in fresh))

if __name__ == "__main__":
    main(init="--init" in sys.argv)
