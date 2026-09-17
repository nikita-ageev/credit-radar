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


# 17.09.2026: МТС-Банк — «Ставка до 19,9% → до 20,0%» вышло как ставка по кредитной карте, наличным и автокредиту.
# На деле это строка из общего меню сайта (вклад «МТС Специальный до 20,0%»), она есть на каждой странице банка.
# Три защиты: (1) одно и то же изменение на ≥2 продуктах банка — общий элемент сайта, не условия продукта;
# (2) рядом с изменённой строкой на странице — слова про вклады/накопительные счета — это не кредитная ставка;
# (3) «ставка до X %» по кредиту ниже ключевой + 3 п.п. — неправдоподобно для кредита, в сводку не идёт.
_DEPOSIT_CTX = re.compile(r"вклад|накопительн|сберегательн|депозит|кешбокс|кэшбокс|доходност|омс\b|металлическ", re.I)
_CREDIT_CTX = re.compile(r"кредит|карт|заём|займ|рассрочк|псk|годовых по кредиту", re.I)
_RATE_UPTO = re.compile(r"(?:ставк\w*\s+)?до\s+(\d{1,2}[,.]?\d?)\s*%", re.I)


def _page_context(bank_key, product, line, span=220):
    """Кусок текущего снимка страницы вокруг строки (для проверки, о чём она)."""
    try:
        import core
        snap = core.load_snap(bank_key, product) or {}
        text = snap.get("text") or ""
        i = text.find(line.strip())
        if i < 0:
            return ""
        return text[max(0, i - span): i + len(line) + span]
    except Exception:
        return ""


def _key_rate_value():
    try:
        prev, cur = key_rate(fetch=False)
        return float((cur or prev or {}).get("rate") or 0)
    except Exception:
        return 0.0


def _not_credit_rate(c, ctx):
    """Причина, по которой «ставка» в строке — не ставка по кредиту, либо None."""
    line = (c.get("new") or c.get("old") or "")
    if _DEPOSIT_CTX.search(line):
        return "строка про вклад/накопительный счёт"
    if ctx and _DEPOSIT_CTX.search(ctx) and not _CREDIT_CTX.search(ctx):
        return "контекст на странице — вклады, не кредит"
    kr = _key_rate_value()
    m = _RATE_UPTO.search(line)
    if kr and m and "от" not in line.lower():
        try:
            v = float(m.group(1).replace(",", "."))
            if v < kr + 3:
                return f"«до {m.group(1)}%» ниже ключевой {kr:g}% + 3 п.п. — не кредитная ставка"
        except ValueError:
            pass
    return None


CHANGE_REJECTS = []   # что отбросил changes() и почему; radar.daily пишет в лог


def changes(found):
    """Значимые изменения условий у банков: параметр, старое → новое. Косметику и шум отбрасываем."""
    CHANGE_REJECTS.clear()
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
            if params[0] in ("ставка", "ПСК"):
                why = _not_credit_rate({"old": old, "new": new}, _page_context(it.get("bank_key", ""), it["product"], line))
                if why:
                    CHANGE_REJECTS.append(f"{it['bank']}/{it['product']}: «{_clip(line, 60)}» — {why}")
                    continue
            out.append({"bank": it["bank"], "product": it["product"], "url": it["url"],
                        "param": params[0], "old": old, "new": new,
                        "kind": "изменение" if (old and new) else ("появилось" if new else "исчезло")})
    # общий элемент сайта: одно и то же изменение на разных продуктах одного банка (меню, шапка, подвал)
    same = {}
    for c in out:
        same.setdefault((c["bank"], c["param"], _norm(c["old"]), _norm(c["new"])), set()).add(c["product"])
    shared = {k for k, v in same.items() if len(v) >= 2}
    if shared:
        for k in shared:
            CHANGE_REJECTS.append(f"{k[0]}: «{_clip(k[3] or k[2], 50)}» одинаково на {len(same[k])} продуктах — общий элемент сайта, не условия")
        out = [c for c in out if (c["bank"], c["param"], _norm(c["old"]), _norm(c["new"])) not in shared]
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


def _norm_txt(s):
    return re.sub(r"[\s«»\"'.,;:—–\-]+", " ", (s or "")).strip().lower()


def diff_fragment(old, new, n=110):
    """16.09.2026: Альфа, льготный период — «было „Если в ДК указаны БП 60 дней…“, стало „Если в ДК указаны БП 60 дней…“»:
    обе строки обрезаны до общего начала, разница осталась за многоточием. Показываем ТОЛЬКО то, что изменилось:
    срезаем общее начало и общий конец по границам слов, оставляем 2 слова контекста. Возвращает (kind, old_frag, new_frag):
    kind — «убрано» (новое = начало старого), «добавлено» (старое = начало нового), «изменение» или None (текст тот же)."""
    o, w = (old or "").strip(), (new or "").strip()
    if _norm_txt(o) == _norm_txt(w):
        return None, "", ""
    ow, nw = o.split(), w.split()
    _k = lambda t: t.strip(".,;:!?»«\"'()").lower()  # сравниваем слова без знаков: «₽.» и «₽» — одно слово
    ok, nk = [_k(t) for t in ow], [_k(t) for t in nw]
    if nk == ok[:len(nk)]:                     # новое — начало старого: хвост убран
        return "убрано", _clip(" ".join(ow[len(nw):]), n), ""
    if ok == nk[:len(ok)]:                     # старое — начало нового: хвост добавлен
        return "добавлено", "", _clip(" ".join(nw[len(ow):]), n)
    i = 0
    while i < min(len(ow), len(nw)) and ok[i] == nk[i]:
        i += 1
    j = 0
    while j < min(len(ow), len(nw)) - i and ok[-1 - j] == nk[-1 - j]:
        j += 1
    ctx = 2
    a, b = max(0, i - ctx), ctx if j else 0
    of = " ".join(ow[a:len(ow) - j + b]) if j else " ".join(ow[a:])
    nf = " ".join(nw[a:len(nw) - j + b]) if j else " ".join(nw[a:])
    return "изменение", (("…" if a > 0 else "") + _clip(of, n)), (("…" if a > 0 else "") + _clip(nf, n))


def _was_became(param, old, new, n=110, dash=" "):
    """Текст «param было „…“, стало „…“» по изменённому фрагменту; None — если по сути ничего не поменялось."""
    kind, of, nf = diff_fragment(old, new, n)
    if kind is None:
        return None
    if kind == "убрано":
        return f"{param}{dash}— убрано «{of}»"
    if kind == "добавлено":
        return f"{param}{dash}— добавлено «{nf}»"
    return f"{param}{dash}было «{of}», стало «{nf}»"


def _restructure_line(group):
    """Одна строка по переписанной странице. Числа не выдёргиваем из фраз: 13.09.2026 «Скидка 2% от ставки»
    превратилась в «ставка: значение 2 % убрано» — неправда. Цитируем фразы как есть."""
    c0 = group[0]; facts = []
    for c in group:
        on, nn = _numunits(c["old"]), _numunits(c["new"])
        if c["kind"] == "изменение" and on != nn and (on or nn):
            wb = _was_became(c["param"] + ":", c["old"], c["new"], 70)
            if wb: facts.append(wb)
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
        wb = _was_became(c["param"], c["old"], c["new"], 110)
        if not wb:
            return None                                # по сути не изменилось — строки нет (16.09.2026, Альфа)
        return f"{_a(c['bank'], c.get('url'))}, {c['product']}: {wb}."
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
    # 14.09.2026: триггер — только релиз самого Банка России о ставке; чужая новость «после решения ЦБ» строку не даёт.
    # Повтор той же ставки в другой день режет gate.py (ключ kr:<ставка>).
    trig = any(x.get("src") == "Банк России" and _KR_TRIG.search(x.get("title", "") + " " + x.get("desc", "")) for x in fresh)
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


# 15.09.2026: «Главное» выбираем не по типу источника, а по весу для P&L розничного кредитора.
# Вес ≈ размер портфеля × маржинальность продукта / уровень решения. POS, ипотека, проектное финансирование,
# переводы, инвестиции — мелкие или чужие для розничного риска темы, им нельзя быть «Главным».
_NPV_WEIGHTS = (
    (r"ключев\w* ставк|решени\w* по ставке|совет директоров банка россии", 1.0),
    (r"макропруденц|мпл\b|надбавк|показател\w* долгов\w* нагрузк|пдн\b|590-п|риск-вес", 1.0),
    (r"кредитн\w* карт|кредитк", 1.0),
    (r"кредит\w* наличн|потребительск\w* кредит|потребкредит|необеспеченн", 0.9),
    (r"рассрочк|bnpl|сплит", 0.9),
    (r"\bбки\b|кредитн\w* истори|скоринг", 0.8),   # 16.09: без \b слева «НБКИ» в источнике давало 0,8 автокредитам
    (r"просрочк|стоимост\w* риска|резерв", 0.8),
    (r"секьюритиз", 0.6),
    (r"микрозайм|мфо\b|микрофинанс", 0.5),
    (r"автокредит", 0.4),
    (r"точках продаж|pos-кредит|pos\b", 0.35),
    (r"ипотек|эскроу|проектн\w* финансиров|застройщик|долев\w* строительств", 0.15),
    (r"перевод|сбп\b|платеж|кошел|вклад|депозит|брокер|инвестиц|облигац|акци\w* банк", 0.2),
)
_NPV_RX = [(re.compile(rx, re.I), w) for rx, w in _NPV_WEIGHTS]

def npv_weight(x):
    """Вес новости для розничного кредитора, 0.1–1.0: максимум по совпавшим темам (заголовок + описание)."""
    t = " ".join(str(x.get(k, "")) for k in ("title", "desc"))
    ws = [w for rx, w in _NPV_RX if rx.search(t)]
    return max(ws) if ws else 0.5


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
            if j >= 0.5 or (cont >= 0.5 and min(len(tx), len(ty)) >= 5):   # 14.09: «банки не планируют менять ставки» ×2 — одно событие
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


def dedupe_by_model(items):
    """Второй проход дедупликации (14.09.2026): модель группирует заголовки одного события, код оставляет
    первоисточник (БКИ/ЦБ > пресса > канал) и дописывает «(также …)». Ошибка модели — список без изменений."""
    if not items:
        return items
    try:
        import gate
        anchors = gate.published_titles(7)
    except Exception:
        anchors = []
    if len(items) + len(anchors) < 2:
        return items
    try:
        import brain
        groups = brain.same_events([x.get("title", "") for x in items] + anchors)
    except Exception:
        groups = []
    if not groups:
        return items
    n = len(items); drop = set(); out = list(items)
    for g in groups:
        if any(i >= n for i in g):                     # событие уже выходило в канале — все свежие из группы снимаем
            for i in g:
                if i < n:
                    drop.add(i); REJECTED.append(f"уже публиковалось (другими словами): «{items[i].get('title', '')[:50]}»")
            continue
        g = [i for i in g if i < n and i not in drop]
        if len(g) < 2:
            continue
        best = max(g, key=lambda i: (source_rank(items[i]), -i))
        for i in g:
            if i == best:
                continue
            if items[i]["src"] not in out[best]["src"]:
                out[best] = dict(out[best], src=out[best]["src"] + f" (также {items[i]['src']})")
            drop.add(i)
    return [x for i, x in enumerate(out) if i not in drop]


def essays_this_week(titles, today, max_week=None):
    """Сколько ДНЕЙ с разбором было за последние 7 дней (17.09.2026). Раньше считались записи журнала: 10.09 три поста
    и черновики дали шесть записей за день, и с 14 по 17.09 разбор блокировался «7/3» при живом инфоповоде.
    Разбор — не чаще одного в день, поэтому единица счёта — день, а не запись."""
    import datetime as _dt
    week_ago = (today - _dt.timedelta(days=7)).isoformat()
    days = {t.get("date", "") for t in titles
            if t.get("date", "") >= week_ago and t.get("rubric") not in ("тихий день", "сводка")}
    return len(days)

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
    fresh = dedupe_by_model(fresh)
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
        wb = _was_became(c["param"], c["old"], c["new"], 60) if c["kind"] == "изменение" else None
        if c["kind"] == "изменение" and not wb:
            main = ""; main_skip = None                 # текст по сути тот же — «Главного» из него не делаем
        else:
            main = (f"{_a(c['bank'], c.get('url'))}, {c['product']}: {wb}." if c["kind"] == "изменение"
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
                cl = _change_line(c)
                if cl:
                    lines.append("• " + cl)
                else:
                    REJECTED.append(f"{c['bank']}/{c['product']}: «было» и «стало» совпадают после нормализации")
        if lines:
            parts.append("<b>Условия у банков</b>\n" + "\n".join(lines))
    else:
        parts.append(f"<b>Условия у банков</b>\nБез изменений: проверено {n_pages} {_pl(n_pages, 'страница', 'страницы', 'страниц')} "
                     f"у {n_banks} {_pl(n_banks, 'банка', 'банков', 'банков')}"
                     + (f", {streak}-й день подряд без изменений." if streak >= 2 else "."))

    other_news = [x for x in other_news if not (main_skip and main_skip[0] == "news" and x.get("title") == main_skip[1])]
    # 14.09.2026: пост канала без «что значит» от модели или без факта (числа / названия банка, ЦБ, БКИ) в заголовке —
    # это мнение или тизер, не новость; в сводку не идёт (в вечернем выпуске то же правило — «нет добавочной информации»)
    _FACT = re.compile(r"\d|Сбер|ВТБ|Т-Банк|Тинькофф|Альфа|Газпромбанк|Совкомбанк|МТС|Озон|Ozon|Яндекс|Wildberries|ОТП|Ренессанс|"
                       r"Уралсиб|Райффайзен|ПСБ|Почта|Хоум|Банк России|ЦБ|НБКИ|ОКБ|Скоринг|Минфин|правительств|Госдум|ФАС", re.I)
    def _keep_news(x):
        if source_rank(x) >= 2:
            return True
        return bool(pol_news.get(x.get("title", ""), "").strip()) and bool(_FACT.search(x.get("title", "")))
    for x in other_news:
        if not _keep_news(x):
            REJECTED.append(f"канал без факта/смысла: «{x.get('title', '')[:50]}»")
    other_news = [x for x in other_news if _keep_news(x)][:3]
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
    if "свой банк (own)" in text or "Ozon" in text:
        issues.append("упомянут свой банк (own)")
    return issues


def is_empty(chs, news_items, quotes):
    return not chs and not [x for x in (news_items or []) if not x.get("repeat")] and not stock_lines(quotes)
