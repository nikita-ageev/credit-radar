# -*- coding: utf-8 -*-
"""Шлюз публикации (14.09.2026). Через него проходит КАЖДЫЙ текст, уходящий в канал и на сайт: сводка, вечерний выпуск.

Повод: 14.09 в канал ушли (1) реклама из Telegram-канала (erid, ссылка на промостраницу банка) без ссылки на источник,
(2) ключевая ставка и динамика акций, которые уже публиковались. Никита: «перепиши код так, чтобы подобное было ИСКЛЮЧЕНО».

Три правила, все — кодом, до модели и после неё:
  1. Реклама не проходит: erid / промо-utm / рекламные слова / ссылка из канала на маркетинговую страницу банка.
  2. Строка-новость без ссылки на источник не проходит. Источник — https-адрес с латинским хостом (СМИ, регулятор,
     БКИ) или сам пост канала t.me/<канал>/<id>. «Ссылка», вытащенная из текста поста (Дом.РФ), источником не считается.
  3. Повторы не проходят: ключ каждой опубликованной строки хранится в state/published_lines.json;
     новость — 7 дней, ставка ЦБ — пока значение не изменится, акции — тикер раз в 7 дней.

Использование: items = gate.filter_items(items) — до сборки; text, dropped = gate.vet(text) — после сборки, перед
публикацией; gate.remember(text) — после успешной публикации. Всё детерминированно, модели здесь нет.
"""
import os, re, json, time, html as _html
from urllib.parse import urlparse, parse_qs

import core

STATE_FILE = os.path.join(core.STATE, "published_lines.json")
NEWS_TTL = 7 * 86400
STOCK_TTL = 7 * 86400

# --- 1. реклама -----------------------------------------------------------------------------------------------
_AD_WORDS = re.compile(
    r"(#реклама|\bреклама\.\s*(ооо|ао|пао|ип|банк)|\berid\b|промокод\w*|скидк\w*|розыгрыш\w*|дарим|подарок|акци[яи]\s+(до|только)|успей\w*|только до\s+\d|"
    r"оформи\w*\s+(карту|кредит|счёт|счет)|подключ\w*\s+(сейчас|сегодня|по ссылке)|по ссылке|переходи\w*|"
    r"кэшб[эе]к\s+до|бонус\w*\s+до|партн[её]рск\w*|спецпредложени\w*|выгодн\w*\s+предложени\w*|клуб\w*\s+предпринимател\w*|"
    r"реферальн\w*|приведи\s+друга|за\s+рекомендаци\w*|ООО\s+«|ИНН\s*\d{10})", re.I)
_AD_UTM = re.compile(r"(erid=|utm_campaign=[^&]*(referral|promo|akci|sale|offer|partner|bonus|cashback)|utm_medium=(post|cpc|banner))", re.I)
# домены банков: ссылка из КАНАЛА на такую страницу — промо, а не источник новости
_BANK_HOSTS = ("sber", "vtb", "втб", "tbank", "tinkoff", "alfabank", "gazprombank", "gpb", "mtsbank", "sovcombank",
               "raiffeisen", "rencredit", "otpbank", "uralsib", "psbank", "rshb", "pochtabank", "domrf", "дом.рф",
               "ozon", "wildberries", "wbbank", "yandex", "mkb.ru", "open.ru", "rosbank", "homebank", "hcb")
_TRACKERS = re.compile(r"(telegram\.org|tgme|/s/|max\.ru|vk\.com|youtube|youtu\.be|sendsay|bit\.ly|clck\.ru|dzen\.ru|\.link/|"
                       r"taplink|tgstat|goo\.gl|t\.co/)", re.I)


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _from_channel(item_or_link):
    link = item_or_link if isinstance(item_or_link, str) else str((item_or_link or {}).get("via") or (item_or_link or {}).get("link") or "")
    return link.startswith("https://t.me/")


def is_ad(title="", desc="", link="", from_channel=False):
    """Причина, почему это реклама, или None."""
    text = f"{title} {desc}"
    if _AD_UTM.search(link or ""):
        return "промо-метки в ссылке (erid/utm)"
    if _AD_WORDS.search(text):
        m = _AD_WORDS.search(text)
        return f"рекламные слова: «{m.group(0)}»"
    h = _host(link)
    if from_channel and h and any(b in h for b in _BANK_HOSTS):
        return f"ссылка из канала на страницу банка ({h})"
    return None


# --- 2. источник ------------------------------------------------------------------------------------------------
def valid_source(url):
    """https-адрес с латинским хостом и точкой, не трекер/сокращалка; t.me/<канал>/<id> — тоже источник."""
    if not url or not re.match(r"^https?://", url or ""):
        return False
    h = _host(url)
    if not h or not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", h):      # кириллический/пустой хост — не источник
        return False
    if _TRACKERS.search(url) and not url.startswith("https://t.me/"):
        return False
    if url.startswith("https://t.me/"):
        return bool(re.match(r"^https://t\.me/[A-Za-z0-9_]+/\d+", url))
    return True


# --- 3. повторы -------------------------------------------------------------------------------------------------
def _load():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(d, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def news_key(title):
    ws = re.sub(r"[^\w\s]", " ", (title or "").lower().replace("ё", "е")).split()
    return "news:" + " ".join(sorted(set(w[:5] for w in ws if len(w) > 3))[:12])


_STRIP = re.compile(r"<[^>]+>")
_KR = re.compile(r"Банк России.*?(снизил|повысил|сохранил|оставил).*?ставк\w*.*?(\d+[,.]\d)\s*%", re.I | re.S)
_STOCK = re.compile(r"moex\.com/ru/issue\.aspx\?code=([A-Z]+)")


def line_keys(line):
    """Ключи повторов для строки сводки (уже с HTML)."""
    plain = _html.unescape(_STRIP.sub("", line))
    keys = []
    m = _KR.search(plain)
    if m:
        keys.append(f"kr:{m.group(2).replace('.', ',')}")
    for t in _STOCK.findall(line):
        keys.append(f"stock:{t}")
    if not keys:
        body = re.sub(r"^\s*•\s*", "", plain)
        body = re.sub(r"^[^:]{2,40}:\s", "", body, 1)     # «Frank Media: » — источник не часть ключа
        body = re.split(r"\s+—\s+", body, 1)[0]            # без «что это значит»
        keys.append(news_key(body))
    return keys


def _ttl(key):
    return None if key.startswith("kr:") else STOCK_TTL if key.startswith("stock:") else NEWS_TTL


IGNORE_SINCE = [None]   # 15.09.2026: при переделке сегодняшнего выпуска (--redo) ключи, записанные после этого момента, — свои же, не повтор

def already_published(key, now=None):
    d = _load(); ts = d.get(key)
    if ts is None or isinstance(ts, dict):
        return False
    if IGNORE_SINCE[0] is not None and ts >= IGNORE_SINCE[0]:
        return False
    ttl = _ttl(key)
    return ttl is None or (now or time.time()) - ts < ttl


# --- применение --------------------------------------------------------------------------------------------------
def filter_items(items, log=None):
    """Новости до сборки: убрать рекламу, повторы, строки без источника (ссылка из канала заменяется на сам пост)."""
    out = []
    for x in items or []:
        link = str(x.get("link") or ""); via = str(x.get("via") or "")
        reason = is_ad(x.get("title", ""), x.get("desc", ""), link, from_channel=bool(via))
        if reason:
            log and log(f"шлюз: реклама, снято — {x.get('src')}: «{str(x.get('title'))[:60]}» ({reason})"); continue
        if not valid_source(link):
            if via and valid_source(via):
                x = dict(x, link=via)
            else:
                log and log(f"шлюз: нет источника, снято — {x.get('src')}: «{str(x.get('title'))[:60]}»"); continue
        if already_published(news_key(x.get("title", ""))):
            log and log(f"шлюз: уже публиковалось, снято — «{str(x.get('title'))[:60]}»"); continue
        out.append(x)
    return out


_BULLET_SECTIONS = ("Банк России", "Новости", "Акции", "Главное", "Ещё за день")


_WAS_BECAME = re.compile(r"было «([^»]*)», стало «([^»]*)»")


def same_was_became(plain):
    """True, если в строке «было „X“, стало „Y“» X и Y совпадают (после обрезки многоточий, пробелов и знаков).
    Такая строка — пустая для читателя, публиковать её нельзя."""
    m = _WAS_BECAME.search(plain or "")
    if not m:
        return False
    norm = lambda t: re.sub(r"[\s…«»\"'.,;:—–\-]+", " ", t).strip().lower()
    return norm(m.group(1)) == norm(m.group(2))


def vet(text, log=None):
    """Финальная проверка собранного HTML перед публикацией. Возвращает (текст, список снятых строк с причинами).
    Строки по банкам («Условия у банков») не трогаем: их источники — страницы условий, проверяются в digest.check."""
    dropped = []
    blocks = text.split("\n\n")
    out_blocks = []
    for b in blocks:
        head = _STRIP.sub("", b.split("\n", 1)[0]).strip().rstrip(".")
        is_main = head.startswith("Главное")
        sec = next((s for s in _BULLET_SECTIONS if head.startswith(s)), None)
        if not sec and not is_main:
            out_blocks.append(b); continue
        lines = b.split("\n")
        keep = []
        for i, ln in enumerate(lines):
            # 16.09.2026: в блоке «Главное» проверяем ссылкой только первую строку (заголовок-ссылка);
            # строки «— факт» и «Что значит:» — продолжение той же новости, у них своей ссылки нет и быть не должно
            is_bullet = ln.lstrip().startswith("•") or (is_main and i == 0)
            if i == 0 and not is_main:
                keep.append(ln); continue
            if not is_bullet:
                keep.append(ln); continue
            reason = None
            hrefs = re.findall(r'href="([^"]+)"', ln)
            plain = _html.unescape(_STRIP.sub("", ln))
            if same_was_became(plain):
                reason = "«было» и «стало» совпадают"   # 16.09.2026, Альфа: разница осталась за многоточием
            elif not hrefs or not any(valid_source(_html.unescape(h)) for h in hrefs):
                reason = "нет ссылки на источник"
            else:
                for h in hrefs:
                    r = is_ad("", plain, _html.unescape(h), from_channel=False)
                    if r: reason = r; break
                if not reason and _AD_WORDS.search(plain):
                    reason = f"рекламные слова: «{_AD_WORDS.search(plain).group(0)}»"
            if not reason:
                for k in line_keys(ln):
                    if already_published(k):
                        reason = f"уже публиковалось ({k[:40]})"; break
            if reason:
                dropped.append((plain[:90], reason)); log and log(f"шлюз: снято — {plain[:70]} ({reason})")
            else:
                keep.append(ln)
        if is_main:
            if len(keep) == 0 or not keep[0].strip() or not keep[0].lstrip().startswith("<b>Главное"):
                continue                                     # заголовок «Главного» снят — уходит весь блок, хвосты без него не нужны
            out_blocks.append("\n".join(keep)); continue
        if len(keep) <= 1:                                   # остался только заголовок раздела
            if sec == "Новости":
                out_blocks.append("<b>Новости</b>\nНовых новостей по розничному кредиту за сутки нет.")
            continue
        out_blocks.append("\n".join(keep))
    return "\n\n".join(out_blocks), dropped


def finalize(text, log=None):
    """Единая точка перед каналом и сайтом (17.09.2026): вёрстка под Telegram (publish.tidy) → безопасные автозамены
    терминов (style.autofix) → шлюз (vet). Раньше те же три шага были расписаны отдельно в утренней сводке и в вечернем
    выпуске; теперь порядок и состав один, и его проверяет evals/publish_test.py. Возвращает (текст, снятые строки)."""
    import publish, style
    if not text:
        return "", []
    return vet(publish.tidy(style.autofix(text)), log)


def published_titles(days=7):
    """Заголовки строк, опубликованных за N дней (для сверки «то же событие другими словами» моделью)."""
    d = _load(); now = time.time()
    return [v["t"] for k, v in d.items() if k.startswith("title:") and isinstance(v, dict) and now - v["ts"] < days * 86400]


def remember(text):
    """После успешной публикации: запомнить ключи всех строк и их заголовки."""
    d = _load(); now = time.time()
    for b in text.split("\n\n"):
        head = _STRIP.sub("", b.split("\n", 1)[0]).strip()
        if not head.startswith(_BULLET_SECTIONS):
            continue
        for ln in b.split("\n"):
            if ln.lstrip().startswith("•"):
                m = re.search(r"<a href=\"[^\"]+\">([^<]{15,})</a>", ln)
                if m:
                    t = _html.unescape(m.group(1)).strip()
                    d["title:" + news_key(t)] = {"t": t, "ts": now}
    for b in text.split("\n\n"):
        head = _STRIP.sub("", b.split("\n", 1)[0]).strip()
        if not (head.startswith(_BULLET_SECTIONS)):
            continue
        for ln in b.split("\n"):
            if ln.lstrip().startswith("•") or head.startswith("Главное"):
                for k in line_keys(ln):
                    d[k] = now
    # чистка старых
    d = {k: v for k, v in d.items() if (isinstance(v, dict) and now - v["ts"] < 30 * 86400) or (not isinstance(v, dict) and (_ttl(k) is None or now - v < 30 * 86400))}
    _save(d)


def seed(keys):
    d = _load(); now = time.time()
    for k in keys: d[k] = now
    _save(d)


if __name__ == "__main__":
    import sys
    t = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    txt, dr = vet(t, log=print)
    print("---\n" + txt); print("---\nснято:", dr)
