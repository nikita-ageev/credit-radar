# -*- coding: utf-8 -*-
"""Публикация в Telegram-канал: текст, скриншоты-доказательства, графики.

Формат поста: заголовок-вывод, разбор, доказательства со ссылкой. Картинки идут
альбомом с подписью — так пост в ленте выглядит как материал, а не как бот-уведомление.
"""
import os, json, uuid, mimetypes, urllib.request, urllib.error

TOKEN   = os.environ.get("RADAR_TG_TOKEN", "").strip()
CHANNEL = os.environ.get("RADAR_CHANNEL", "").strip()   # @имя_канала или -100...
API     = "https://api.telegram.org/bot{}/{}"

class NotConfigured(Exception):
    pass

def _check():
    if not TOKEN or not CHANNEL:
        raise NotConfigured("нет RADAR_TG_TOKEN / RADAR_CHANNEL")

def _call(method, params, timeout=60):
    _check()
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(API.format(TOKEN, method), data=data, timeout=timeout) as r:
        return json.load(r)

def _multipart(method, fields, files, timeout=180):
    _check()
    b = uuid.uuid4().hex; body = b""
    for k, v in fields.items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    for name, path in files:
        fn = os.path.basename(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{name}"; filename="{fn}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n").encode()
        with open(path, "rb") as f:
            body += f.read()
        body += b"\r\n"
    body += f"--{b}--\r\n".encode()
    req = urllib.request.Request(API.format(TOKEN, method), data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "published.json")

def _remember(resp, kind):
    """Журнал опубликованного: id сообщений, время, вид.

    Без него канал невозможно прибрать: Bot API не умеет читать историю, и после
    сбоя не найти, что именно ты отправил. Один раз это уже стоило дублей в канале.
    """
    try:
        res = resp.get("result")
        ids = [m["message_id"] for m in res] if isinstance(res, list) else [res["message_id"]]
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        try:
            log = json.load(open(LEDGER, encoding="utf-8"))
        except Exception:
            log = []
        log.append({"ts": __import__("datetime").datetime.utcnow().isoformat(timespec="seconds"),
                    "kind": kind, "ids": ids})
        json.dump(log[-200:], open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return resp

class Broken(Exception):
    pass

def looks_broken(text):
    """Страховка от публикации испорченного текста.

    Один раз в канал уехал пост, где кириллица превратилась в «ÐÐ¢Ð» — байты UTF-8,
    прочитанные как latin-1. Внешне код отработал успешно: и API вернул ok, и картинки
    приложились. Поэтому теперь текст проверяется ПЕРЕД отправкой: если латиницы из
    верхней половины таблицы больше, чем кириллицы, — это моджибейк, а не текст.
    Возвращает причину отказа или None, если всё в порядке."""
    if not text or not text.strip():
        return "пустой текст"
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
    lat1 = sum(1 for c in text if "\u00C0" <= c <= "\u00FF")
    if lat1 > 20 and lat1 > cyr:
        return f"похоже на моджибейк: латиницы {lat1}, кириллицы {cyr}"
    if cyr < 20 and len(text) > 200:
        return "в длинном тексте почти нет кириллицы"
    return None

CAP_LIMIT = 1024        # лимит подписи к медиа
MSG_LIMIT = 4096        # лимит обычного сообщения

def post(text, images=None, disable_preview=True, force=False):
    """Пост в канал. Если текст длиннее подписи — сначала картинки, следом текст,
    чтобы ничего не обрезалось на полуслове.
    Перед отправкой текст проверяется на порчу: публиковать мусор хуже, чем не публиковать."""
    bad = None if force else looks_broken(text)
    if bad:
        raise Broken(bad)
    images = [p for p in (images or []) if p and os.path.exists(p)]
    if not images:
        return _remember(_call("sendMessage",
                               {"chat_id": CHANNEL, "text": text[:MSG_LIMIT],
                                "parse_mode": "HTML",
                                "disable_web_page_preview": "true" if disable_preview else "false"}),
                         "text")
    if len(text) <= CAP_LIMIT and len(images) == 1:
        return _remember(_multipart("sendPhoto", {"chat_id": CHANNEL, "caption": text,
                                                  "parse_mode": "HTML"},
                                    [("photo", images[0])]), "photo")
    # ПОРЯДОК: сначала ТЕКСТ, потом картинки. Telegram в альбоме всегда ставит
    # изображения перед подписью, из-за чего длинный пост читался как «стена
    # картинок, а потом текст». Аналитику надо читать первой, доказательства —
    # следом. Поэтому длинный текст идёт отдельным сообщением ПЕРВЫМ, а альбом
    # картинок — вторым, без подписи.
    r = _remember(_call("sendMessage",
                        {"chat_id": CHANNEL, "text": text[:MSG_LIMIT],
                         "parse_mode": "HTML", "disable_web_page_preview": "true"}), "text")
    media, files = [], []
    for i, p in enumerate(images[:10]):
        key = f"f{i}"
        media.append({"type": "photo", "media": f"attach://{key}"})
        files.append((key, p))
    _remember(_multipart("sendMediaGroup",
                         {"chat_id": CHANNEL,
                          "media": json.dumps(media, ensure_ascii=False)}, files), "media")
    return r

def render_post(item, url=None):
    """Собирает финальный текст: заголовок-вывод + разбор + доказательства.

    Осторожно с повторами: после доработки модель иногда возвращает в поле post уже
    готовый пост вместе с заголовком и цитатами. Если слепо добавить их ещё раз,
    в канал уедет текст с двумя заголовками и двумя блоками цитат — так и было.
    Поэтому и заголовок, и цитаты добавляются только если их там ещё нет.
    """
    def _norm(t):
        return " ".join((t or "").replace("<b>", "").replace("</b>", "")
                        .replace("<i>", "").replace("</i>", "").split()).lower()

    body = (item.get("post") or "").strip()
    title = (item.get("title") or "").strip()

    # Если тело начинается со своей жирной строки-заголовка — срезаем её.
    # Сравнение текстов тут не работает: модель пишет второй заголовок другими
    # словами, и в канал уходили две почти одинаковые жирные строки подряд.
    first, sep, rest = body.partition("\n")
    fs = first.strip()
    if sep and fs.startswith("<b>") and fs.endswith("</b>") and len(fs) < 220:
        body = rest.lstrip()

    parts = []
    if title:
        parts.append(f"<b>{title}</b>")
    if body:
        parts.append(body)

    facts = [f.strip() for f in (item.get("facts") or []) if f and f.strip()]
    if facts and "дословно из источника" not in _norm(body):
        seen, quotes = set(), []
        for f in facts[:3]:
            key = _norm(f)[:70]
            if key in seen or key in _norm(body):
                continue                      # цитата уже стоит в тексте — не дублируем
            seen.add(key)
            # Цитату отдаём как есть: модель уже присылает её в виде «Банк: «текст»».
            # Попытка «добавить кавычки, если их нет» давала вложенные ёлочки.
            quotes.append(f.strip())
        if quotes:
            parts.append("<i>Дословно с витрин:</i>\n" + "\n".join(quotes))
    if url:
        parts.append(f"Источник: {url}")
    return "\n\n".join(parts)

def selftest():
    """Проверка настройки без публикации в канал."""
    _check()
    me = _call("getMe", {})
    chat = _call("getChat", {"chat_id": CHANNEL})
    admins = _call("getChatAdministrators", {"chat_id": CHANNEL})
    ok_admin = any(a.get("user", {}).get("id") == me["result"]["id"]
                   for a in admins.get("result", []))
    return {"бот": me["result"]["username"], "канал": chat["result"].get("title"),
            "бот_админ": ok_admin}


# ---------------------------------------------------------------------------
# ОДНО СООБЩЕНИЕ С КАРТИНКОЙ НАД ТЕКСТОМ.
# Подпись к фото у ботов ограничена 1024 знаками, а бриф длиннее. Поэтому картинка
# кладётся на публичный хостинг, а в сообщение уходит превью ссылки, поднятое над
# текстом (link_preview_options.show_above_text). В ленте это выглядит как один пост:
# картинка сверху, полный текст под ней. Ссылка в тексте не показывается.
# ---------------------------------------------------------------------------
import re as _re, subprocess as _sp, time as _time

def _ftp_put(path, name):
    host, user, pw = (os.environ.get(k, "") for k in ("RADAR_FTP_HOST", "RADAR_FTP_USER", "RADAR_FTP_PASS"))
    d = os.environ.get("RADAR_FTP_DIR", "/public_html/radar"); proto = os.environ.get("RADAR_FTP_PROTO", "ftp")
    r = _sp.run(["curl", "-sS", "--fail", "--max-time", "90", "--ftp-create-dirs", "-T", path,
                 "-u", f"{user}:{pw}", f"{proto}://{host}{d}/{name}"], capture_output=True, text=True)
    return r.returncode == 0

def _wait_public(url, tries=8):
    for _ in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TelegramBot (like TwitterBot)"}), timeout=15) as resp:
                if resp.status == 200: return True
        except Exception:
            pass
        _time.sleep(2)
    return False

_OG_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>{title}</title>
<meta property="og:type" content="article">
<meta property="og:site_name" content="Retail Credit Radar">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{img}">
<meta property="og:image:width" content="{w}"><meta property="og:image:height" content="{h}">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="{img}">
<meta name="robots" content="noindex"></head>
<body style="margin:0;background:#0E1621"><img src="{img}" style="max-width:100%"></body></html>"""

def upload_public(path, title="Кредитный радар", desc=""):
    """Кладёт картинку и HTML-страницу с OpenGraph на хостинг. Возвращает URL СТРАНИЦЫ
    (по ней Telegram строит превью надёжнее всего) или None.

    Имя файла уникальное на каждую публикацию: превью у Telegram кэшируется по URL,
    и переиспользование имени показывает старую картинку. Проверка доступности — тем же
    User-Agent, что у превью-бота Telegram."""
    base = os.environ.get("RADAR_PUBLIC_BASE", "").rstrip("/")
    if not (base and path and os.path.exists(path) and os.environ.get("RADAR_FTP_HOST")):
        return None
    stem = os.path.splitext(os.path.basename(path))[0] + "_" + _time.strftime("%H%M%S")
    img_name = stem + ".png"
    if not _ftp_put(path, img_name):
        return None
    img_url = f"{base}/{img_name}"
    try:
        from PIL import Image
        w, h = Image.open(path).size
    except Exception:
        w, h = 1800, 1280
    esc = lambda t: (t or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    html = _OG_PAGE.format(title=esc(title), desc=esc(desc[:200]), img=img_url, w=w, h=h)
    page_path = os.path.join(os.path.dirname(path), stem + ".html")
    with open(page_path, "w", encoding="utf-8") as f: f.write(html)
    page_name = stem + ".html"
    if not _ftp_put(page_path, page_name):
        return img_url if _wait_public(img_url) else None
    page_url = f"{base}/{page_name}"
    ok_img = _wait_public(img_url); ok_page = _wait_public(page_url)
    if ok_img and ok_page: return page_url
    if ok_img: return img_url
    return None

def fit(text, limit=None):
    """Уложить текст в лимит сообщения по границе строки с многоточием (17.09.2026: одна реализация вместо двух).
    Обрезка на полуслове = оборванный пост в канале (11.09), поэтому режем только по переводу строки."""
    limit = limit or MSG_LIMIT
    if not text or len(text) <= limit:
        return text
    return text[:limit - 1].rsplit("\n", 1)[0].rstrip() + "…"

def tidy(text):
    """Косметика вёрстки под Telegram: «/0,1%» Telegram красит как команду «/0» —
    раздвигаем слэш; три и более пустых строк — в одну; пробелы у краёв строк.
    Адреса ссылок (href и голые URL) не трогаем — иначе «/2026/09/09/» превращается в кашу."""
    parts = _re.split(r'(<a\s[^>]*>|https?://\S+)', text)
    for i in range(0, len(parts), 2):                       # чётные — обычный текст, нечётные — ссылки
        parts[i] = _re.sub(r"(\d%?)\s*/\s*(\d)", r"\1 / \2", parts[i])
    t = "".join(parts)
    t = _re.sub(r"[ \t]+\n", "\n", t)
    t = _re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def post_single(text, image_path=None, force=False, title="", desc=""):
    """Один пост: картинка (превью) над текстом. Без хостинга — обычный post()."""
    text = tidy(text)
    bad = None if force else looks_broken(text)
    if bad:
        raise Broken(bad)
    url = upload_public(image_path, title=title or "Кредитный радар", desc=desc) if image_path else None
    if not url:
        return post(text, [image_path] if image_path else [], force=force)
    # Невидимая ссылка в начале текста — классический приём каналов: превью гарантированно
    # привязывается к сообщению; link_preview_options поднимает картинку над текстом.
    body = f'<a href="{url}">&#8205;</a>' + text
    params = {"chat_id": CHANNEL, "text": body[:MSG_LIMIT], "parse_mode": "HTML",
              "link_preview_options": json.dumps({"url": url, "prefer_large_media": True,
                                                  "show_above_text": True})}
    return _remember(_call("sendMessage", params), "text+preview")

def delete(message_ids):
    """Удалить свои сообщения из канала (для замены неудачного поста)."""
    out = []
    for mid in message_ids:
        try:
            out.append(_call("deleteMessage", {"chat_id": CHANNEL, "message_id": mid}).get("ok"))
        except Exception as e:
            out.append(f"{type(e).__name__}")
    return out


def post_brief(caption, full_text, image_path=None, mode=None):
    """Публикация брифа. mode: "photo" — ОДНО сообщение: график + подпись (≤1024, рендерится всегда);
    "split" — то же плюс полный разбор ответом; "preview" — старая схема через превью ссылки."""
    mode = mode or os.environ.get("RADAR_POST_MODE", "photo")
    caption = tidy(caption)
    has_image = bool(image_path and os.path.exists(image_path))
    if not has_image and not full_text:
        # обычный текстовый пост: лимит 4096, а не 1024 подписи. 11.09 сводка обрезалась именно здесь.
        if len(caption) > MSG_LIMIT:
            raise Broken(f"текст {len(caption)} зн. длиннее лимита {MSG_LIMIT} — сократи")
        bad = looks_broken(caption)
        if bad:
            raise Broken(bad)
        return _remember(_call("sendMessage", {"chat_id": CHANNEL, "text": caption, "parse_mode": "HTML",
                                               "disable_web_page_preview": "true"}), "text")
    if len(caption) > CAP_LIMIT:
        cut = caption[:CAP_LIMIT - 1]
        cut = cut[:max(cut.rfind(". "), cut.rfind("\n"))] if max(cut.rfind(". "), cut.rfind("\n")) > 400 else cut
        caption = cut.rstrip() + "…"
    bad = looks_broken(caption)
    if bad:
        raise Broken(bad)
    if mode == "preview":
        return post_single(full_text, image_path)
    if image_path and os.path.exists(image_path):
        r = _remember(_multipart("sendPhoto", {"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML"},
                                 [("photo", image_path)]), "photo")
    else:
        r = _remember(_call("sendMessage", {"chat_id": CHANNEL, "text": caption, "parse_mode": "HTML",
                                            "disable_web_page_preview": "true"}), "text")
    if mode == "split" and full_text:
        mid = (r.get("result") or {}).get("message_id")
        ft = tidy(full_text)
        if len(ft) > MSG_LIMIT:                      # обрезка на полуслове = оборванный пост в канале
            cut = ft[:MSG_LIMIT - 1]; cut = cut[:cut.rfind("\n\n")] if cut.rfind("\n\n") > 2000 else cut
            ft = cut.rstrip() + "…"
        params = {"chat_id": CHANNEL, "text": ft, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"}
        if mid: params["reply_parameters"] = json.dumps({"message_id": mid})
        _remember(_call("sendMessage", params), "text-reply")
    return r


def edit_photo(message_id, image_path, caption=None):
    """Заменить картинку (и при необходимости подпись) в уже опубликованном посте канала.
    Нужно, когда график перерисован после публикации: удалять и слать заново — значит
    ломать ответ-тред и терять просмотры. Bot API: editMessageMedia с attach://."""
    media = {"type": "photo", "media": "attach://photo"}
    if caption is not None:
        media["caption"] = tidy(caption); media["parse_mode"] = "HTML"
    return _multipart("editMessageMedia", {"chat_id": CHANNEL, "message_id": str(message_id),
                                           "media": json.dumps(media, ensure_ascii=False)},
                      [("photo", image_path)])


def edit_text(message_id, text):
    """Заменить текст обычного (не медиа) сообщения канала — для правок разбора после публикации."""
    text = tidy(text)
    if len(text) > MSG_LIMIT:
        raise Broken(f"текст {len(text)} зн. длиннее лимита {MSG_LIMIT} — сократи, обрезать нельзя")
    return _call("editMessageText", {"chat_id": CHANNEL, "message_id": str(message_id),
                                     "text": text, "parse_mode": "HTML",
                                     "disable_web_page_preview": "true"})


def edit_caption(message_id, caption):
    """Заменить подпись у фото-поста, не трогая картинку."""
    caption = tidy(caption)
    if len(caption) > CAP_LIMIT:
        raise Broken(f"подпись {len(caption)} зн. длиннее лимита {CAP_LIMIT}")
    return _call("editMessageCaption", {"chat_id": CHANNEL, "message_id": str(message_id),
                                        "caption": caption, "parse_mode": "HTML"})
