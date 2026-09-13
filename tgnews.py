# -*- coding: utf-8 -*-
"""Новости из публичных Telegram-каналов через веб-превью t.me/s/<канал> (без API и без ключей).
13.09.2026: пост «Выдача кредитных карт в августе — 1,54 млн, рекорд» вышел в каналах за день до прессы и в RSS-ленты
Радара не попал. Каналы отраслевые, публичные; читаем раз в прогон, по одной странице (≈20 последних постов).
Формат элемента — как в news.fresh: src, title, link, desc, dt."""
import re, html, urllib.request
from datetime import datetime, timezone, timedelta

CHANNELS = [
    ("Frank Media", "frank_media"),
    ("Frank RG", "frank_rg"),
    ("Труба под Неглинной", "trubapodneglinnoy"),
    ("Банк России (канал)", "centralbank_russia"),
    ("Рисковик", "riskovik"),
    ("ФИНСАЙД", "finside"),
    ("Финтехно", "fintexno"),
    ("Банки, деньги, два офшора", "bankrollo"),      # инсайды и утечки, 454 тыс.; фильтр релевантности режет шум
    ("Финансовый караульный", "karaulny_accountant"),  # то же, 92 тыс.
]
_RX = re.compile(r'data-post="(?P<post>[^"]+)".*?tgme_widget_message_text[^>]*>(?P<html>.*?)</div>.*?<time datetime="(?P<dt>[^"]+)"', re.S)
_TAG = re.compile(r"<[^>]+>")

def _text(h):
    t = re.sub(r"<br\s*/?>", "\n", h)
    t = _TAG.sub("", t)
    t = html.unescape(t).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", t).strip()

def fetch(channel, timeout=20):
    req = urllib.request.Request(f"https://t.me/s/{channel}", headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")

def items(hours=36):
    """Посты за последние `hours` часов из всех каналов. Заголовок — первая строка/предложение (≤120 знаков)."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out, errs = [], []
    for name, ch in CHANNELS:
        try:
            page = fetch(ch)
        except Exception as e:
            errs.append(f"{ch}: {type(e).__name__}"); continue
        for m in _RX.finditer(page):
            try:
                dt = datetime.fromisoformat(m["dt"].replace("Z", "+00:00"))
            except Exception:
                continue
            if dt < since:
                continue
            text = _text(m["html"])
            if len(text) < 40:
                continue
            first = text.split("\n", 1)[0].strip(" *_")
            if len(first) > 120:
                cut = re.split(r"(?<=[.!?])\s", first, 1)[0]
                first = (cut if len(cut) <= 120 else first[:117].rsplit(" ", 1)[0] + "…")
            # шапка с эмодзи/«срочно» — убираем
            first = re.sub(r"^[^\w«»\"(]+", "", first).strip()          # эмодзи и «⚡️» в начале
            if first.count("»") > first.count("«"):                     # «Слово» в начале — кавычку открывающую вернуть
                first = "«" + first
            ext = re.findall(r'href="(https?://(?!t\.me/)[^"]+)"', m["html"])
            ext = [u for u in ext if not re.search(r"telegram\.org|tgme|/s/|max\.ru|vk\.com|youtube|youtu\.be|sendsay|bit\.ly|clck\.ru|dzen\.ru|\.link/", u)]
            link = html.unescape(ext[0]) if ext else f"https://t.me/{m['post']}"
            out.append(dict(src=name, title=first, link=link, desc=text[:400], dt=dt, via=f"https://t.me/{m['post']}"))
    return out, errs

if __name__ == "__main__":
    its, errs = items(hours=48)
    for x in sorted(its, key=lambda x: x["dt"], reverse=True):
        print(f"{x['dt']:%d.%m %H:%M} [{x['src']}] {x['title']}  {x['link']}")
    print("ошибки:", errs)
