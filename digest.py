# -*- coding: utf-8 -*-
"""Ежедневная сводка: выходит каждый день, коротко, по делу. Собирается из данных
детерминированно; модель (Sonnet, один дешёвый вызов) только переписывает строки
человеческим языком и не имеет права добавлять числа.

Разделы: Банк России (только когда ставка изменилась или есть свежая регуляторная новость; 13.09.2026 Никита:
«не в каждом посте») → изменения условий у банков (факт + одна фраза «что это значит») → новости →
акции (буквально: с A до B ₽, минус N%, индекс за то же время …; в выходные биржа закрыта — раздела нет).
"""
import re, html as _html

def _a(text, url):
    """Ссылка в HTML Telegram: банк → страница условий, новость → статья, ставка → таблица ЦБ."""
    t = _html.escape(str(text), quote=False)
    return f'<a href="{url}">{t}</a>' if url else t

_NUM = re.compile(r"\d[\d\s]*[,.]?\d*")
_KEY = {
    "льготный период":   re.compile(r"льготн|грейс|беспроцентн|без переплат|\bдн(ей|я)\b", re.I),
    "ставка":            re.compile(r"ставк|% годовых", re.I),
    "ПСК":               re.compile(r"\bПСК\b", re.I),
    "минимальный платёж": re.compile(r"минимальн\w* плат|обязательн\w* плат", re.I),
    "лимит":             re.compile(r"лимит|до \d[\d\s]*\s*(₽|руб|млн|тыс)", re.I),
    "комиссия":          re.compile(r"комисси", re.I),
    "срок":              re.compile(r"\bсрок|месяц|\bлет\b", re.I),
    "скидка/акция":      re.compile(r"скидк|акци[яи]|кэшб|кешб|бонус", re.I),
}
_NOISE = re.compile(r"(cookie|куки|чат|поддержк|виджет|войти|скачать|©|лицензи|карта сайта|поделиться)", re.I)


def _norm(s):
    s = _NUM.sub("#", s.lower())
    return re.sub(r"\W+", " ", s).strip()


def pair_changes(changes):
    """'-старая строка' / '+новая строка' → пары (old, new). Одиночные строки — с пустой половиной."""
    minus = [c[1:].strip() for c in changes if c.startswith("-")]
    plus = [c[1:].strip() for c in changes if c.startswith("+")]
    pairs, used = [], set()
    for o in minus:
        key = _norm(o)
        best, score = None, 0.0
        for i, n in enumerate(plus):
            if i in used:
                continue
            kn = _norm(n)
            if not key or not kn:
                continue
            a, b = set(key.split()), set(kn.split())
            j = len(a & b) / max(1, len(a | b))
            if j > score:
                best, score = i, j
        if best is not None and score >= 0.5:
            used.add(best); pairs.append((o, plus[best]))
        else:
            pairs.append((o, ""))
    for i, n in enumerate(plus):
        if i not in used:
            pairs.append(("", n))
    return pairs


def _nums(s):
    return [m.group(0).replace(" ", "") for m in _NUM.finditer(s)]


_LEAD = re.compile(r"^[\s\-—–•·]+")
def _clip(s, n=140):
    """Обрезка по границе слова с многоточием. Никогда не рвём слово пополам («Обыч»)."""
    s = _LEAD.sub("", (s or "").strip())
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(" ,;:—–-(«")
    return cut + "…"

_NUMUNIT = re.compile(r"((?:до|от|не более|не менее)\s+)?(\d[\d\s]*[,.]?\d*)\s*(%|₽|руб\.?|млн|тыс\.?|дн(?:ей|я)?|мес\w*|лет|год\w*)(\s*₽)?", re.I)
def _numunits(s):
    out = []
    for m in _NUMUNIT.finditer(s or ""):
        t = " ".join(x.strip() for x in m.groups() if x)
        out.append(re.sub(r"\s+", " ", t).replace(" %", " %"))
    return out


def changes(found):
    """Значимые изменения условий у банков: параметр, старое → новое. Косметику и шум отбрасываем."""
    out = []; raw = {}
    for it in found:
        if it.get("own"):
            continue
        pairs = pair_changes(it.get("changes") or [])
        raw[(it["bank"], it["product"])] = len(pairs)
        for old, new in pairs:
            line = new or old
            if _NOISE.search(line) or len(line) < 8 or len(line) > 420:
                continue
            if old and new and _nums(old) == _nums(new) and _norm(old) == _norm(new):
                continue                                  # перестановка строк, не изменение
            params = [k for k, rx in _KEY.items() if rx.search(line)]
            if not params:
                continue
            if not _nums(line) and "скидка/акция" not in params:
                continue                                  # без чисел — почти всегда вёрстка
            out.append({"bank": it["bank"], "product": it["product"], "url": it["url"],
                        "param": params[0], "old": old, "new": new,
                        "kind": "изменение" if (old and new) else ("появилось" if new else "исчезло")})
    # один банк/продукт — не больше трёх строк, самые «ценовые» вперёд
    order = ["ставка", "ПСК", "льготный период", "минимальный платёж", "комиссия", "лимит", "срок", "скидка/акция"]
    out.sort(key=lambda c: order.index(c["param"]) if c["param"] in order else 9)
    seen, sig, res = {}, set(), []
    for c in out:
        k = (c["bank"], c["product"])
        # одно и то же изменение часто встречается на странице дважды (шапка и таблица) — оставляем одно
        fp = (c["bank"], c["product"], c["param"], tuple(_nums(c["old"])), tuple(_nums(c["new"])))
        if fp in sig or seen.get(k, 0) >= 3:
            continue
        sig.add(fp); seen[k] = seen.get(k, 0) + 1; res.append(c)
    # страница переписана целиком (много правок) — в сводке это ОДНА строка с сутью, а не обрывки абзацев
    for c in res:
        k = (c["bank"], c["product"])
        c["raw"] = raw.get(k, 0)
        c["restructure"] = raw.get(k, 0) >= 8 or seen.get(k, 0) >= 3
    return res


def _restructure_line(group):
    """Одна строка по переписанной странице. Числа не выдёргиваем из фраз: 13.09.2026 «Скидка 2% от ставки»
    превратилась в «ставка: значение 2 % убрано» — неправда. Цитируем фразы как есть."""
    c0 = group[0]; facts = []
    for c in group:
        on, nn = _numunits(c["old"]), _numunits(c["new"])
        if c["kind"] == "изменение" and on != nn and (on or nn):
            facts.append(f"{c['param']}: было «{_clip(c['old'], 70)}», стало «{_clip(c['new'], 70)}»")
        elif c["kind"] == "появилось" and nn:
            facts.append(f"появилось «{_clip(c['new'], 70)}»")
        elif c["kind"] == "исчезло" and on:
            facts.append(f"убрано «{_clip(c['old'], 70)}»")
    head = f"{_a(c0['bank'], c0.get('url'))}, {c0['product']}: страница условий переписана ({c0.get('raw', 0)} правок)."
    if facts:
        body = "; ".join(dict.fromkeys(facts))
        return head + " " + body[0].upper() + body[1:] + "."
    return head + " Числа те же, изменились формулировки."


def _change_line(c):
    if c["kind"] == "изменение":
        return f"{_a(c['bank'], c.get('url'))}, {c['product']}: {c['param']} было «{_clip(c['old'], 110)}», стало «{_clip(c['new'], 110)}»."
    if c["kind"] == "появилось":
        return f"{_a(c['bank'], c.get('url'))}, {c['product']}: на странице условий появилось «{_clip(c['new'], 130)}»."
    tail = " Новое значение на странице не указано." if c["param"] in ("ставка", "ПСК", "лимит", "льготный период", "минимальный платёж") else ""
    what = _clip(c['old'], 130)
    what = f"строку {what}" if "«" in what else f"«{what}»"
    return f"{_a(c['bank'], c.get('url'))}, {c['product']}: со страницы условий убрали {what}.{tail}"


_KR_URL = "https://www.cbr.ru/hd_base/KeyRate/"
_KR_TRIG = re.compile(r"ключев\w*\s+ставк|сохранени\w*\s+ставки|ставк\w*\s+на\s+уровне|(снизил|повысил|сохранил|оставил)\w*\s+ставку", re.I)
def key_rate(fetch=True):
    """Последняя ключевая ставка ЦБ с публичной страницы cbr.ru (таблица дата/ставка). Кэш в state/key_rate.json."""
    import json, os, urllib.request
    try:
        import core; path = os.path.join(core.STATE, "key_rate.json")
    except Exception:
        path = "key_rate.json"
    prev = {}
    try:
        prev = json.load(open(path, encoding="utf-8"))
    except Exception:
        pass
    if not fetch:
        return prev, prev
    try:
        h = urllib.request.urlopen(urllib.request.Request("https://www.cbr.ru/hd_base/KeyRate/",
                                   headers={"User-Agent": "Mozilla/5.0"}), timeout=25).read().decode("utf-8", "ignore")
        rows = re.findall(r"<tr>\s*<td>(\d{2}\.\d{2}\.\d{4})</td>\s*<td>([\d,\.]+)</td>", h)
        if rows:
            cur = {"date": rows[0][0], "rate": float(rows[0][1].replace(",", "."))}
            json.dump(cur, open(path, "w", encoding="utf-8"), ensure_ascii=False)
            return prev, cur
    except Exception:
        pass
    return prev, prev


def key_rate_line(news_items, fetch=True):
    """Строка про ключевую ставку: только когда ставка изменилась или в ДЕНЬ решения (свежая, не повторная новость).
    13.09.2026 повторная новость о решении 11.09 дала строку второй день подряд — Никита: «так не надо»."""
    fresh = [x for x in (news_items or []) if not x.get("repeat")]
    trig = any(_KR_TRIG.search(x.get("title", "") + " " + x.get("desc", "")) for x in fresh)
    prev, cur = key_rate(fetch)
    if not cur or not cur.get("rate"):
        return ""
    if prev.get("rate") is not None and prev["rate"] != cur["rate"]:
        verb = "снизил" if cur["rate"] < prev["rate"] else "повысил"
        return (f"{_a('Банк России', _KR_URL)} {verb} ключевую ставку с {_fmt_ru(prev['rate'])} до {_fmt_ru(cur['rate'])} % годовых "
                f"(в силе с {cur['date']}).")
    if trig:
        return f"{_a('Банк России', _KR_URL)} сохранил ключевую ставку {_fmt_ru(cur['rate'])} % годовых."
    return ""


_PRIMARY = ("НБКИ", "ОКБ", "Скоринг Бюро", "Банк России")
_PRESS = ("Коммерсантъ", "Ведомости", "Banki.ru", "Frank Media", "РБК", "Интерфакс", "ТАСС")

def source_rank(x):
    """3 — первоисточник (БКИ, ЦБ), 2 — пресса, 1 — канал/репост."""
    src = str(x.get("src", "")); link = str(x.get("link", ""))
    if any(src.startswith(p) for p in _PRIMARY): return 3
    if link.startswith("https://t.me/"): return 1
    if any(src.startswith(p) for p in _PRESS): return 2
    return 2 if not link.startswith("https://t.me/") else 1


def dedupe_news(items):
    """Одно событие из двух источников — одна строка с двумя источниками, не две строки."""
    def toks(t):
        # грубые основы: первые 5 букв слова, чтобы «рублем» и «рублях» были одним словом; числа оставляем целиком
        ws = re.sub(r"[^\w\s]", " ", (t or "").lower().replace("ё", "е")).split()
        return set((w if w.isdigit() else w[:5]) for w in ws if len(w) > 3 or w.isdigit())
    out = []
    for x in items:
        tx = toks(x.get("title")); merged = False
        for y in out:
            ty = y["_toks"]
            if not tx or not ty:
                continue
            j = len(tx & ty) / max(1, len(tx | ty))
            cont = len(tx & ty) / max(1, min(len(tx), len(ty)))
            if j >= 0.5 or (cont >= 0.6 and min(len(tx), len(ty)) >= 4):
                # 13.09.2026: одно событие — ссылка на ПЕРВОИСТОЧНИК (БКИ/ЦБ > пресса > канал), канал остаётся как «через»
                if source_rank(x) > source_rank(y):
                    keep_src, keep_toks = y["src"], y["_toks"]
                    y.update({k: v for k, v in x.items() if k != "_toks"}); y["_toks"] = keep_toks
                    y["src"] = x["src"] + (f" (через {keep_src})" if keep_src not in y["src"] else "")
                elif x["src"] not in y["src"]:
                    y["src"] = y["src"] + f" (также {x['src']})"
                merged = True; break
        if not merged:
            y = dict(x); y["_toks"] = tx; out.append(y)
    for y in out:
        y.pop("_toks", None)
    return out


def trigger(chs):
    """Инфоповод для тематического разбора: банк поменял ключевой ценовой параметр
    (старое и новое значение есть, числа различаются)."""
    for c in chs:
        if c["kind"] != "изменение" or _nums(c["old"]) == _nums(c["new"]):
            continue
        if c["param"] in ("ставка", "ПСК", "льготный период", "минимальный платёж", "комиссия"):
            return c
        if c["param"] == "лимит" and not c.get("restructure"):
            return c                                       # лимит при переписанной странице — не инфоповод
    return None


def stock_lines(quotes, min_week=4.0, min_month=6.0):
    """Буквальные фразы про акции: цена с A до B ₽ (−N%), индекс за то же время …. Без «альфы» и «п.п.»."""
    data = (quotes or {}).get("data") or quotes or {}
    tick = data.get("tickers") or {}
    lines = []
    for sec, r in tick.items():
        if not r.get("ok"):
            continue
        for period, key, akey in (("неделю", "chg_1w", "alpha_1w"), ("месяц", "chg_1m", "alpha_1m")):
            ch, al = r.get(key), r.get(akey)
            if ch is None or al is None or abs(ch) < (min_week if key == "chg_1w" else min_month):
                continue
            last = r.get("last_close")
            if not last:
                continue
            before = last / (1 + ch / 100)
            idx = ch - al
            verb = "подешевели" if ch < 0 else "подорожали"
            iverb = "снизился" if idx < 0 else "вырос"
            pct = f"{ch:+.1f}".replace(".", ",")
            lines.append(f"Акции {_a(r.get('name', sec), f'https://www.moex.com/ru/issue.aspx?code={sec}')} за {period} {verb} с {_fmt_ru(before)} до {_fmt_ru(last)} ₽ "
                         f"({pct}%), индекс Мосбиржи за то же время {iverb} на {_fmt_ru(abs(idx))}%.")
            break                                          # одна фраза на бумагу — самый заметный период
    return lines


def _numset(s):
    return {n.replace(",", ".").rstrip(".") for n in _nums(s or "")}


def unsupported_numbers(text, *sources):
    """Числа из текста модели, которых нет ни в одном источнике (галлюцинация или «округление»).
    13.09.2026: проверка обязательна — строка с таким числом в канал не идёт."""
    have = set()
    for src in sources:
        have |= _numset(src if isinstance(src, str) else " ".join(map(str, src or [])))
    return sorted(n for n in _numset(re.sub(r"<[^>]+>", " ", text)) if n not in have)


def _pl(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 19: return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def _fmt_ru(x):
    return f"{x:,.1f}".replace(",", " ").replace(".", ",")


def _is_weekend(date_str):
    """Суббота/воскресенье по дате сводки: биржа закрыта, раздел «Акции» не выходит (13.09.2026)."""
    import datetime as _dt
    try:
        return _dt.datetime.strptime(date_str, "%d.%m.%Y").weekday() >= 5
    except Exception:
        return False


REJECTED = []   # строки модели, отвергнутые проверкой чисел в последнем build(); radar.daily пишет их в лог


def build(chs, news_items, quotes, polished=None, n_pages=0, n_banks=0, streak=0, date_str="", fetch_rate=True):
    """Текст сводки (HTML для Telegram). polished — результат brain.digest_polish или None."""
    REJECTED.clear()
    pol_ch = (polished or {}).get("changes") or []
    pol_news = {n.get("title", ""): n.get("why", "") for n in ((polished or {}).get("news") or []) if isinstance(n, dict)}
    parts = [f"<b>Сводка за {date_str}</b>" if date_str else "<b>Сводка дня</b>"]

    fresh = dedupe_news([x for x in (news_items or []) if not x.get("repeat")])
    cbr_news = [x for x in fresh if x.get("src") == "Банк России"][:3]
    other_news = [x for x in fresh if x.get("src") != "Банк России"]

    def _news_line(x):
        why = pol_news.get(x.get("title", ""), "")
        if why and unsupported_numbers(why, x.get("title", ""), x.get("desc", "")):
            REJECTED.append(f"новость «{x.get('title', '')[:50]}»: число не из данных")
            why = ""
        return f"• {x['src']}: {_a(x['title'].strip().rstrip('.'), x.get('link'))}" + (f" — {why.strip().rstrip('.')}." if why else ".")

    kr = ""
    try:
        kr = key_rate_line(news_items, fetch=fetch_rate)
    except Exception:
        kr = ""
    reg = (["• " + kr] if kr else []) + [_news_line(x) for x in cbr_news
                                         if not (kr and _KR_TRIG.search(x.get("title", "")))]
    # «Главное» (13.09.2026, ёмкость внимания): одна строка, выбирается кодом по приоритету —
    # изменение ставки ЦБ > ценовой параметр у банка (ставка/ПСК/льготный период) > релиз БКИ > регуляторная новость
    main = ""; main_skip = None
    price = [c for c in (chs or [])[:6] if c.get("param") in ("ставка", "ПСК", "льготный период")]
    bki = [x for x in fresh if x.get("src") in ("НБКИ", "ОКБ", "Скоринг Бюро")]
    if kr and ("снизил" in kr or "повысил" in kr):
        main = kr
    elif price:
        c = price[0]; main_skip = ("ch", (c["bank"], c["product"]))
        main = (f"{_a(c['bank'], c.get('url'))}, {c['product']}: {c['param']} — было «{_clip(c['old'], 60)}», стало «{_clip(c['new'], 60)}»." if c["kind"] == "изменение"
                else f"{_a(c['bank'], c.get('url'))}, {c['product']}: {c['param']} — {'появилось' if c['kind'] == 'появилось' else 'убрано'} «{_clip(c['new'] or c['old'], 70)}».")
    elif bki:
        main = _news_line(bki[0]).lstrip("• "); main_skip = ("news", bki[0].get("title"))
    elif reg:
        main = reg[0].lstrip("• ")
    if main:
        parts.append("<b>Главное.</b> " + main)
    if reg:
        parts.append("<b>Банк России</b>\n" + "\n".join(reg))

    if chs:
        lines, done = [], set()
        for i, c in enumerate(chs[:6]):
            k = (c["bank"], c["product"])
            if c.get("restructure") and k in done:
                continue
            if main_skip == ("ch", k):
                done.add(k); continue                     # уже в «Главном»
            if len(lines) >= 4:                            # больше четырёх строк по банкам не читают
                break
            done.add(k)
            text = pol_ch[i] if i < len(pol_ch) and isinstance(pol_ch[i], str) and pol_ch[i].strip() else None
            if text and (text.count("…") + text.count("...")) == 0 and len(text) < 40:
                text = None                                # слишком коротко для правки — берём детерминированную строку
            if text and c.get("url") and text.startswith(c["bank"]):
                text = _a(c["bank"], c["url"]) + text[len(c["bank"]):]
            if text and "<a href" not in text:
                text = None                                # модель потеряла банк в начале — ссылки не будет, берём свою строку
            if text:
                grp = [x for x in chs[:6] if (x["bank"], x["product"]) == k]
                bad = unsupported_numbers(text, *[x["old"] + " " + x["new"] for x in grp], c["bank"], c["product"])
                if bad:
                    REJECTED.append(f"{c['bank']}/{c['product']}: числа {', '.join(bad)} не из данных")
                    text = None                            # галлюцинация числа — детерминированная строка
            if text:
                lines.append("• " + text)
            elif c.get("restructure"):
                lines.append("• " + _restructure_line([x for x in chs[:6] if (x["bank"], x["product"]) == k]))
            else:
                lines.append("• " + _change_line(c))
        if lines:
            parts.append("<b>Условия у банков</b>\n" + "\n".join(lines))
    else:
        parts.append(f"<b>Условия у банков</b>\nБез изменений: проверено {n_pages} {_pl(n_pages, 'страница', 'страницы', 'страниц')} "
                     f"у {n_banks} {_pl(n_banks, 'банка', 'банков', 'банков')}"
                     + (f", {streak}-й день подряд без изменений." if streak >= 2 else "."))

    other_news = [x for x in other_news if not (main_skip and main_skip[0] == "news" and x.get("title") == main_skip[1])][:3]
    if other_news:
        parts.append("<b>Новости</b>\n" + "\n".join(_news_line(x) for x in other_news))
    else:
        parts.append("<b>Новости</b>\nНовых новостей по розничному кредиту за сутки нет.")

    st = [] if _is_weekend(date_str) else stock_lines(quotes)
    if st:
        parts.append("<b>Акции</b>\n" + "\n".join("• " + s for s in st[:2]))
    return "\n\n".join(parts)


def build_evening(news_items, polished=None, date_str=""):
    """Вечерний выпуск (13.09.2026): только новости дня со ссылками на источники — пресса, релизы БКИ, ЦБ, каналы.
    Одна строка на новость: источник, заголовок-ссылка, фраза модели «что это значит». Без банков, ставки и акций."""
    REJECTED.clear()
    pol_news = {n.get("title", ""): n.get("why", "") for n in ((polished or {}).get("news") or []) if isinstance(n, dict)}
    fresh = dedupe_news([x for x in (news_items or []) if not x.get("repeat")])
    if not fresh:
        return ""
    def _line(x):
        why = pol_news.get(x.get("title", ""), "")
        if why and unsupported_numbers(why, x.get("title", ""), x.get("desc", "")):
            REJECTED.append(f"новость «{x.get('title', '')[:50]}»: число не из данных"); why = ""
        return f"• {x['src']}: {_a(x['title'].strip().rstrip('.'), x.get('link'))}" + (f" — {why.strip().rstrip('.')}." if why else ".")
    # каналы и заметки без фразы «что это значит» от модели — не про розничный кредит (исторические очерки, обзоры недели)
    def _keep(x):
        why = pol_news.get(x.get("title", ""), "")
        return bool(why.strip()) or not (str(x.get("link", "")).startswith("https://t.me/") or x.get("src") == "Frank Media")
    if polished is not None:
        fresh = [x for x in fresh if _keep(x)]
    if not fresh:
        return ""
    cbr = [x for x in fresh if x.get("src", "").startswith("Банк России")][:3]
    bki = [x for x in fresh if x.get("src") in ("НБКИ", "ОКБ", "Скоринг Бюро")][:4]
    rest = [x for x in fresh if x not in cbr and x not in bki][:8]
    parts = [f"<b>Вечерний выпуск за {date_str}</b>" if date_str else "<b>Вечерний выпуск</b>"]
    if cbr: parts.append("<b>Банк России</b>\n" + "\n".join(_line(x) for x in cbr))
    if bki: parts.append("<b>Бюро кредитных историй</b>\n" + "\n".join(_line(x) for x in bki))
    if rest: parts.append("<b>Новости дня</b>\n" + "\n".join(_line(x) for x in rest))
    return "\n\n".join(parts)


_CUT_RX = re.compile(r"[а-яА-ЯёЁ]{3,}»\.(?!\.)")      # «…Обыч». — след обрезки посреди слова
def check(text, chs, news_items):
    """Самопроверка сводки перед публикацией (12.09: «чтобы не повторялось»). Возвращает список проблем."""
    issues = []
    fresh = [x for x in (news_items or []) if not x.get("repeat")]
    if (chs or fresh) and "<a href" not in text:
        issues.append("нет ни одной ссылки на источник")
    if chs and sum(1 for c in chs[:6] if c.get("url") and c["url"] in text) == 0:
        issues.append("строки по банкам без ссылок на страницы условий")
    if fresh and sum(1 for x in fresh[:6] if x.get("link") and x["link"] in text) == 0:
        issues.append("новости без ссылок на статьи")
    if re.search(r"конкурент", text, re.I):
        issues.append("слово «конкурент»")
    for m in re.finditer(r"«([^»]{8,})»", text):
        inner = m.group(1)
        if inner.endswith(("Обыч", "категор", "Он")) or (not inner.endswith(("…", ".", "!", "?", "%", "₽")) and len(inner) >= 100):
            issues.append(f"похоже на обрыв цитаты: «{inner[-25:]}»")
    if "Озон Банк" in text or "Ozon" in text:
        issues.append("упомянут Озон Банк")
    return issues


def is_empty(chs, news_items, quotes):
    return not chs and not [x for x in (news_items or []) if not x.get("repeat")] and not stock_lines(quotes)
