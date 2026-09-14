#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кредитный радар — ежедневная конкурентная разведка по розничным кредитным продуктам.

Запуск:
  radar.py crawl      обойти источники, сохранить снапшоты, посчитать изменения
  radar.py analyze    разобрать найденные изменения мозгом, собрать посты (без публикации)
  radar.py run        полный цикл: обход -> анализ -> публикация в канал
  radar.py digest     собрать и опубликовать сводку за сутки одним постом
  radar.py selftest   проверить доступность источников, ключей и канала
  radar.py bakeoff    сравнить модели на одной и той же находке (какая пишет лучше)
  radar.py market [продукт]   собрать ОБЗОР РЫНКА в черновик (не публикует)
  radar.py draft              показать черновик целиком — прочитать перед публикацией
  radar.py review             ТРОЙНАЯ проверка черновика (факты / методология / риски)
  radar.py revise             доработать черновик по замечаниям проверки
  radar.py publish-draft      опубликовать черновик (только после пройденной проверки)
  radar.py collect    снять замеры отзывов и широких сигналов (медленно, раз в сутки)
  radar.py context    показать контекст, который уходит аналитику (--refresh — пересобрать)
  radar.py prune      экономная чистка архива (сырые копии старше 14 дней)

Всё состояние — локально в state/, готовые посты и картинки — в out/.
Источники только открытые. Внутренних данных банка здесь нет и быть не должно.
"""
import os, re, sys, json, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import core, sources, brain, publish, charts

LOG = os.path.join(HERE, "radar_log.txt")
FINDINGS = os.path.join(core.STATE, "findings.json")

# Модули, которые собираются отдельно; радар работает и без них.
def _opt(name):
    try:
        return __import__(name)
    except Exception as e:
        log(f"модуль {name} недоступен: {type(e).__name__}: {str(e)[:80]}")
        return None

def log(msg):
    line = f"{core.msk():%Y-%m-%d %H:%M:%S} МСК  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------- обход источников ----------------
def _lines(t):
    return sum(1 for l in (t or "").split("\n") if l.strip())

def crawl(only=None):
    """Обходит продуктовые страницы, сравнивает с прошлым снапшотом.
    Возвращает список находок: что и у кого изменилось."""
    found = []
    for key, bank in sources.BANKS.items():
        if only and key not in only:
            continue
        for product, url in bank["pages"].items():
            try:
                html, text, how, err = core.get_rendered(url)
                if err:
                    log(f"[{bank['name']}/{product}] недоступно: {err}")
                    continue
                prev = core.load_snap(key, product)
                # 13.09.2026: Сбер отдал страницу без тела (только меню и «Войти по Сбер ID»), и в канал ушло
                # «ставка убрана, лимит убран». Неполный снимок не сравниваем и не сохраняем; если неполным был
                # прошлый — новый становится базой, но изменений не считаем.
                if prev and prev.get("text"):
                    ratio = _lines(text) / max(1, _lines(prev["text"]))
                    if ratio < 0.7:
                        html2, text2, how2, err2 = core.get_rendered(url)
                        if not err2 and _lines(text2) > _lines(text):
                            html, text, how = html2, text2, how2
                            ratio = _lines(text) / max(1, _lines(prev["text"]))
                    if ratio < 0.7:
                        log(f"[{bank['name']}/{product}] неполная страница ({_lines(text)} строк из {_lines(prev['text'])}), снимок не сохранён")
                        continue
                    if ratio > 1.4:
                        core.save_snap(key, product, url, text, html)
                        log(f"[{bank['name']}/{product}] прошлый снимок был неполным ({_lines(prev['text'])} → {_lines(text)} строк), новая база, изменений не считаем")
                        continue
                core.save_snap(key, product, url, text, html)
                if not prev:
                    log(f"[{bank['name']}/{product}] первый снимок ({how}, {len(text)//1024} КБ)")
                    continue
                if prev["sha"] == core.load_snap(key, product)["sha"]:
                    continue
                changes, total = core.diff(prev["text"], text, sources.SIGNAL_WORDS)
                if changes:
                    hist = core.history_lines(key, product, days=7, before=f"{core.msk():%Y%m%d}")
                    if len(hist) >= 3:
                        stable = core.stable_diff(changes, hist)
                        if len(stable) < len(changes):
                            log(f"[{bank['name']}/{product}] мигание вёрстки: {len(changes) - len(stable)} строк из {len(changes)} отброшено по архиву за {len(hist)} дн")
                        changes = stable
                if not changes:
                    continue
                log(f"[{bank['name']}/{product}] изменений {total}, значимых строк {len(changes)}")
                found.append({"bank_key": key, "bank": bank["name"], "product": product,
                              "url": url, "changes": changes, "total": total,
                              "own": bool(bank.get("own"))})
            except Exception as e:
                log(f"[{bank['name']}/{product}] сбой: {type(e).__name__}: {e}")
    with open(FINDINGS, "w", encoding="utf-8") as f:
        json.dump({"ts": core.msk().isoformat(timespec="seconds"), "items": found},
                  f, ensure_ascii=False, indent=1)
    log(f"обход завершён: находок {len(found)}")
    return found

def load_findings():
    try:
        with open(FINDINGS, encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except Exception:
        return []


# ---------------- сбор внешних данных ----------------
COLLECT_BANKS = ["sber", "tbank", "alfa", "gpb", "vtb", "sovcom", "mts", "yandex"]   # raif снят 09.09.2026: кредитки не выдаёт с 2022, шум

def collect():
    """Снимает замеры отзывов и широких сигналов.

    Отдельная команда, а не часть обхода: эти источники медленные и их незачем
    трогать дважды в день. Без неё context_block у reviews/signals возвращает
    пустышку — ровно это и вскрыла ревизия: модули были на месте, а данных за ними нет.
    """
    rv = _opt("reviews")
    if rv and hasattr(rv, "measure"):
        ok = 0
        for b in COLLECT_BANKS:
            try:
                rv.measure(b); ok += 1
            except Exception as e:
                log(f"отзывы {b}: {type(e).__name__}: {str(e)[:70]}")
        log(f"отзывы: снято по {ok} банкам из {len(COLLECT_BANKS)}")
    sg = _opt("signals")
    if sg and hasattr(sg, "collect_all"):
        try:
            res = sg.collect_all()
            log(f"сигналы: {str(res)[:200]}")
        except Exception as e:
            log(f"сигналы: {type(e).__name__}: {str(e)[:90]}")

# ---------------- дополнительный контекст для мозга ----------------
CTX_CACHE = os.path.join(core.STATE, "context_cache.json")
CTX_TTL = 12 * 3600        # отчётность ЦБ обновляется раз в месяц, чаще считать незачем

FACTS_DIR = os.path.join(core.STATE, "tariffs", "facts")

def verified_facts_block(max_days=45):
    """Факты, проверенные вручную (facts_shots.py → state/tariffs/facts/<дата>/context_blocks.json):
    цитаты из тарифов со ссылками, рынок, отзывы с других площадок, результаты регрессий. В публичный контекст
    идёт public_block (только источники, разрешённые robots.txt), при RADAR_PRIVATE=1 — private_block.
    Берём последний по дате, не старше max_days. 13.09.2026 — первая партия (минимальный платёж по КК)."""
    try:
        days = sorted(d for d in os.listdir(FACTS_DIR) if os.path.isfile(os.path.join(FACTS_DIR, d, "context_blocks.json")))
    except Exception:
        return ""
    import datetime as _dt
    out = []
    for d in days[-3:]:
        try:
            if (core.msk().date() - _dt.date.fromisoformat(d)).days > max_days:
                continue
            with open(os.path.join(FACTS_DIR, d, "context_blocks.json"), encoding="utf-8") as f:
                cb = json.load(f)
            key = "private_block" if os.environ.get("RADAR_PRIVATE") == "1" else "public_block"
            if cb.get(key):
                out.append(cb[key])
        except Exception as e:
            log(f"факты {d}: {type(e).__name__}: {e}")
    return "\n\n".join(out)

def extra_context(refresh=False):
    """Контекст для аналитика: отчётность ЦБ, репутация, широкие сигналы.

    Результат кэшируется: сбор блока ЦБ занимает около минуты (десятки запросов к
    cbr.ru), и повторять это на каждом прогоне — впустую жечь время и чужой сервер.
    """
    cache = CTX_CACHE.replace(".json", "_private.json") if os.environ.get("RADAR_PRIVATE") == "1" else CTX_CACHE
    if not refresh:
        try:
            with open(cache, encoding="utf-8") as f:
                c = json.load(f)
            if time.time() - c.get("ts", 0) < CTX_TTL and c.get("text"):
                return c["text"]
        except Exception:
            pass
    blocks = []
    for mod_name in ("cbr", "reviews", "signals", "tariffs"):
        m = _opt(mod_name)
        if m and hasattr(m, "context_block"):
            try:
                b = str(getattr(m, "context_block")() or "").strip()
                if len(b) > 80:                 # пустышку в контекст не тащим
                    blocks.append(b[:4000])
                else:
                    log(f"{mod_name}: контекст пуст ({len(b)} знаков) — данных нет, "
                        f"нужен сбор (radar.py collect)")
            except Exception as e:
                log(f"{mod_name}.context_block сбой: {type(e).__name__}: {e}")
    fb = verified_facts_block()
    if fb:
        blocks.append(fb[:6000])
    text = "\n\n".join(blocks)
    try:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "text": text}, f, ensure_ascii=False)
    except Exception:
        pass
    return text

# ---------------- анализ ----------------
def _chart_from_spec(spec, path):
    if not spec:
        return None
    try:
        kind = spec.get("kind")
        if kind == "bars":
            return charts.bars(path, spec["labels"], [float(v) for v in spec["values"]],
                               spec.get("title", ""), spec.get("subtitle", ""),
                               spec.get("source", ""), spec.get("unit", ""))
        if kind == "table":
            return charts.table(path, spec["headers"], spec["rows"], spec.get("title", ""),
                                spec.get("subtitle", ""), spec.get("source", ""),
                                spec.get("highlight_row"))
        if kind == "scatter":
            pts = [q for q in (spec.get("points") or []) if isinstance(q, dict)]
            if len(pts) < 3:
                return None
            out, st = charts.scatter(path, pts, spec.get("title", ""), spec.get("subtitle", ""),
                                     spec.get("source", ""), spec.get("x_label", ""), spec.get("y_label", ""),
                                     spec.get("size_label", ""), None, spec.get("x_unit", ""), spec.get("y_unit", ""))
            spec["_stats"] = st
            return out
    except Exception as e:
        log(f"график не построен: {type(e).__name__}: {e}")
    return None

def analyze(min_importance=3, limit=6):
    """Разбирает находки мозгом. Возвращает готовые к публикации посты."""
    items = load_findings()
    if not items:
        log("анализировать нечего"); return []
    ctx = extra_context()
    posts = []
    for it in items[:limit]:
        if it.get("own"):
            continue                      # свой банк — точка отсчёта, не повод для поста
        try:
            res = brain.analyze_change(it["bank"], it["product"], it["url"], it["changes"], ctx)
        except Exception as e:
            log(f"мозг не ответил по {it['bank']}/{it['product']}: {type(e).__name__}: {e}")
            continue
        imp = int(res.get("importance") or 0)
        if imp < min_importance or res.get("skip_reason"):
            log(f"пропуск {it['bank']}/{it['product']}: важность {imp} "
                f"{res.get('skip_reason') or ''}")
            continue
        stamp = f"{it['bank_key']}_{core.msk():%Y%m%d_%H%M%S}"
        shot = core.screenshot(it["url"], os.path.join(core.OUT, f"shot_{stamp}.png"))
        chart = _chart_from_spec(res.get("chart"), os.path.join(core.OUT, f"chart_{stamp}.png"))
        posts.append({"text": publish.render_post(res, it["url"]),
                      "images": [p for p in (shot, chart) if p],
                      "importance": imp, "meta": it})
        log(f"пост готов: {it['bank']}/{it['product']}, важность {imp}")
    posts.sort(key=lambda p: -p["importance"])
    with open(os.path.join(core.OUT, "posts.json"), "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=1)
    return posts

def digest():
    items = [i for i in load_findings() if not i.get("own")]
    if not items:
        log("сводка пустая"); return None
    res = brain.digest(items, extra_context())
    if not res.get("post"):
        log("мозг не собрал сводку"); return None
    stamp = f"digest_{core.msk():%Y%m%d}"
    chart = _chart_from_spec(res.get("chart"), os.path.join(core.OUT, f"chart_{stamp}.png"))
    return {"text": publish.render_post(res), "images": [p for p in (chart,) if p],
            "importance": int(res.get("importance") or 3)}


# ---------------- обзорный пост по рынку ----------------
DRAFT = os.path.join(core.OUT, "draft.json")

# Строки витрины, где есть параметры продукта, а не реклама вообще.
import re as _re
_PARAM = _re.compile(r"(\d+\s*%|\d+\s*дн|беспроцент|грейс|льготн|лимит|кэшбэк|кешбэк|"
                     r"бесплатн|обслуживани|годовых|минимальн\w+ платёж|снят|ПСК|"
                     r"до\s*\d[\d\s]*(?:₽|руб|млн|тыс))", _re.I)

# Строки, которые ОБЯЗАНЫ попасть в выжимку: они снимают двусмысленность.
# Обжигались на этом: у банка на странице написано «льготный период на покупки — 110
# дней, на перевод долга других банков — до 200», но в выжимку попадала только цифра
# 200 с баннера, и разбор сравнивал перевод долга с чужим грейсом на покупки.
_CRUCIAL = _re.compile(r"(льготн\w+ период|беспроцентн\w+ период|на покупк|"
                       r"перевод\w* (?:долг|балансa?|задолженност)|рефинансир|"
                       r"не распространя|ПСК|полная стоимость)", _re.I)

def showcase(product="кредитная карта", per_bank=40):
    """Дословные строки с витрин по продукту — из снимков, без похода в сеть.

    Строки, снимающие двусмысленность (к чему относится льготный период, ПСК,
    исключения), идут ПЕРВЫМИ и не вытесняются рекламной мелочью.
    """
    out = []
    for key, bank in sources.BANKS.items():
        if bank.get("own"):
            continue                      # свой банк в обзор рынка не тащим
        url = bank["pages"].get(product)
        snap = core.load_snap(key, product) if url else None
        if not snap:
            continue
        seen, crucial, rest = set(), [], []
        for line in snap["text"].split("\n"):
            line = line.strip()
            if not (3 < len(line) < 220) or line.lower() in seen:
                continue
            if _CRUCIAL.search(line):
                seen.add(line.lower()); crucial.append(line)
            elif _PARAM.search(line):
                seen.add(line.lower()); rest.append(line)
        lines = crucial[:per_bank] + rest[:max(0, per_bank - len(crucial))]
        if lines:
            out.append(f"### {bank['name']} — {url}\n" + "\n".join("• " + l for l in lines))
    return out


_GRACE_LINE = _re.compile(r"(льготн|беспроцентн|без процент)", _re.I)
_DAYS = _re.compile(r"\d{1,3}\s*дн", _re.I)
_COND = _re.compile(r"(рефинансир|перевод\w* (?:долг|денег|балансa?|задолженност)|"
                    r"карт\w* других банков|погас|не распространя|не вход)", _re.I)

def grace_table(product="кредитная карта"):
    """Размеченный свод по льготному периоду: число + ВСЯ строка + пометка условия.

    Модель систематически брала с баннера «до 200 дней» и подписывала его «на покупки»,
    хотя на той же странице сказано, что 200 дней даются под рефинансирование карт
    других банков. Просить её быть внимательнее бесполезно — надёжнее подать данные
    уже размеченными: где число, где условие, и что в беспроцентный период не входит.
    """
    out = []
    for key, bank in sources.BANKS.items():
        if bank.get("own"):
            continue
        snap = core.load_snap(key, product) if bank["pages"].get(product) else None
        if not snap:
            continue
        rows, seen = [], set()
        for line in snap["text"].split("\n"):
            line = " ".join(line.split())
            if not (5 < len(line) < 260) or line.lower() in seen:
                continue
            if _GRACE_LINE.search(line) and (_DAYS.search(line) or _COND.search(line)):
                seen.add(line.lower())
                mark = " [!! ЕСТЬ УСЛОВИЕ ИЛИ ИСКЛЮЧЕНИЕ]" if _COND.search(line) else ""
                rows.append(f"• {line}{mark}")
        if rows:
            out.append(f"### {bank['name']}\n" + "\n".join(rows[:14]))
    return out

DISCLOSURE_TASK = (
    "ТЕМА ПОСТА: ЧТО БАНКИ РАСКРЫВАЮТ НА ВИТРИНАХ КРЕДИТНЫХ КАРТ, А ЧТО НЕТ.\n\n"
    "Это описательный разбор практики раскрытия, а НЕ поиск причинных связей. "
    "Никаких выводов вида «длинный грейс приводит к чему-то» и никаких показателей "
    "отчётности: их в этом посте нет вообще, работай только со строками витрин.\n\n"
    "Что нужно показать:\n"
    "1. Главное наблюдение: «беспроцентный период» на витринах — это НЕ ОДИН показатель. "
    "У разных банков за одной формулировкой стоят разные вещи: грейс на покупки, "
    "льготный период на перевод долга из других банков, срок рассрочки. Покажи это "
    "на конкретных банках с дословными формулировками и с условиями, при которых "
    "число действует. Вывод: рекламные цифры разных банков между собой несопоставимы.\n"
    "2. Что раскрыто и что не раскрыто по каждому банку: длина и ТИП льготного периода, "
    "ставка или ПСК, стоимость обслуживания, условия по снятию наличных и переводам, "
    "минимальный платёж. Пиши прочерк там, где на витрине этого нет.\n"
    "3. Отдельно: у кого на витрине есть ПСК — единственный сопоставимый измеритель цены.\n"
    "4. Практический вывод для читателя: по какому параметру карты вообще можно "
    "сравнивать, а по какому нельзя, и на что смотреть, чтобы понять реальную цену.\n\n"
    "ЗАПРЕТЫ ДЛЯ ЭТОГО ПОСТА: не приписывай банкам намерение ввести в заблуждение — "
    "они публикуют условия сами, ты лишь показываешь, что форматы разные. "
    "Не используй слова «прячет», «фасад», «на самом деле». Не делай превосходных "
    "степеней без сплошной проверки. Не считай годовых ставок из плат, у которых "
    "не указана периодичность.\n\n"
    "В chart дай таблицу: строки — банки, колонки — «льготный период (и к чему)», "
    "«ставка / ПСК», «обслуживание», «снятие наличных». Прочерк там, где не раскрыто.")

def market(product="кредитная карта", shots=3, topic="экономика"):
    """Обзор рынка. topic: «экономика» — витрины плюс отчётность;
    «раскрытие» — только витрины, разбор практики раскрытия условий.
    Черновик, не публикует."""
    blocks = showcase(product)
    if not blocks:
        log("нет снимков витрин — сначала crawl"); return None
    ctx = extra_context() if topic == "экономика" else ""
    grace = grace_table(product)
    if topic == "раскрытие":
        task = (DISCLOSURE_TASK +
                "\n\nЛЬГОТНЫЙ ПЕРИОД — ВСЕ УПОМИНАНИЯ ЦЕЛИКОМ, ПО БАНКАМ.\n"
                "Пометка [!! ЕСТЬ УСЛОВИЕ ИЛИ ИСКЛЮЧЕНИЕ] означает, что число привязано "
                "к условию (рефинансирование чужих карт, перевод долга) либо часть операций "
                "в период не входит. Такое число нельзя называть грейсом на покупки.\n\n"
                + "\n\n".join(grace) +
                "\n\nОСТАЛЬНЫЕ ПАРАМЕТРЫ С ВИТРИН (дословные строки):\n\n"
                + "\n\n".join(blocks) + "\n\n" + brain.SCHEMA)
    else:
        task = (
        f"ОБЗОР РЫНКА по продукту «{product}». У тебя два независимых источника, и в этом "
        "вся ценность разбора: страницы условий банков и их официальная отчётность. "
        "Соедини их: что банки ОБЕЩАЮТ клиенту и что говорит их экономика о том, "
        "чем они за это платят.\n\n"
        "ЛЬГОТНЫЙ ПЕРИОД — ВСЕ УПОМИНАНИЯ ЦЕЛИКОМ, ПО БАНКАМ.\n"
        "Читай эти строки ДОСЛОВНО. Пометка [!! ЕСТЬ УСЛОВИЕ ИЛИ ИСКЛЮЧЕНИЕ] означает, "
        "что число даётся не просто так: оно привязано к рефинансированию чужих карт, "
        "к переводу долга, либо часть операций в льготный период не входит. Такое число "
        "НЕЛЬЗЯ называть грейсом на покупки и НЕЛЬЗЯ сравнивать с обычным грейсом "
        "других банков, не назвав условие.\n\n"
        + "\n\n".join(grace) +
        "\n\nОСТАЛЬНЫЕ ПАРАМЕТРЫ С ВИТРИН (дословные строки, снято сегодня):\n\n"
        + "\n\n".join(blocks) +
        ("\n\nОТЧЁТНОСТЬ И РЫНОЧНЫЙ КОНТЕКСТ:\n" + ctx if ctx else "") +
        "\n\nСделай обзорный пост: заголовок-вывод; разброс условий с цифрами; "
        "разбор механики там, где щедрость витрины чем-то оплачена (посчитай эффективную "
        "стоимость, покажи формулу); сопоставь агрессивность витрины с маржой и стоимостью "
        "риска банка по отчётности; гипотеза о том, куда движется рынок, и что её опровергнет. "
        "В chart дай таблицу или столбики по банкам.\n\n" + brain.SCHEMA)
    res = brain._json_out(brain.ask(task, max_tokens=12000)) or {}
    if not res.get("post"):
        log("мозг не собрал обзор"); return None

    img = _chart_from_spec(res.get("chart"), os.path.join(core.OUT, "chart_market.png"))
    pics = []
    for key, bank in sources.BANKS.items():
        if len(pics) >= shots or bank.get("own"):
            continue
        url = bank["pages"].get(product)
        if not url:
            continue
        p = core.screenshot(url, os.path.join(core.OUT, f"shot_{key}_{_slug_prod(product)}.png"))
        if p:
            pics.append(p)
    draft = {"res": res, "text": publish.render_post(res),
             "images": ([img] if img else []) + pics}
    with open(DRAFT, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=1)
    log(f"черновик готов: важность {res.get('importance')}, "
        f"{len(draft['text'])} знаков, картинок {len(draft['images'])}")
    return draft

def _slug_prod(p):
    return core._slug(p)

def show_draft():
    with open(DRAFT, encoding="utf-8") as f:
        d = json.load(f)
    print("ЗАГОЛОВОК:", d["res"].get("title"))
    print("важность:", d["res"].get("importance"), "| обрезан:", d["res"].get("_truncated"))
    print("проверка:", publish.looks_broken(d["text"]) or "чисто")
    print("картинки:", d["images"])
    print("-" * 60); print(d["text"])



REVIEW_FILE = os.path.join(core.OUT, "review.json")


def revise_draft():
    """Доработка черновика по замечаниям проверки.

    Переписывать пост с нуля после каждой проверки бессмысленно: генерация случайна,
    и вместе с исправленными ошибками уезжают удачные куски, а взамен приезжают новые
    ошибки. Поэтому правим точечно: отдаём модели её же текст, список замечаний и
    исходные данные, и просим изменить ТОЛЬКО то, на что указали.
    """
    with open(DRAFT, encoding="utf-8") as f:
        d = json.load(f)
    with open(REVIEW_FILE, encoding="utf-8") as f:
        rev = json.load(f)
    issues = []
    for c in rev["checks"]:
        for i in (c.get("issues") or []):
            if i.get("severity") in ("критично", "важно"):
                issues.append(f"[{c['role']}] {i.get('what')}\n   как править: {i.get('fix')}")
    if not issues:
        log("замечаний нет — править нечего"); return
    parts = ["ЭТО ДОРАБОТКА УЖЕ НАПИСАННОГО ПОСТА, А НЕ НОВЫЙ ПОСТ.",
             "Исправь РОВНО то, на что указали проверяющие. Всё остальное оставь как есть: "
             "структуру, порядок абзацев, удачные формулировки и цифры, к которым претензий нет.",
             "", "ТЕКУЩИЙ ТЕКСТ ПОСТА:", d["text"], "",
             "ЗАМЕЧАНИЯ, КОТОРЫЕ НАДО ЗАКРЫТЬ:", "\n\n".join(issues), "",
             "ИСХОДНЫЕ ДАННЫЕ (сверяйся с ними, новых цифр не добавляй):",
             "\n\n".join(grace_table())[:40000], "",
             "Верни пост целиком в том же формате.", brain.SCHEMA]
    res = brain._json_out(brain.ask("\n".join(parts), max_tokens=12000)) or {}
    if not res.get("post"):
        log("доработка не удалась"); return
    img = _chart_from_spec(res.get("chart"), os.path.join(core.OUT, "chart_market.png"))
    pics = [p for p in d["images"] if "shot_" in p and os.path.exists(p)]
    d = {"res": res, "text": publish.render_post(res),
         "images": ([img] if img else []) + pics}
    with open(DRAFT, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    log(f"черновик доработан: {len(d['text'])} знаков, закрыто замечаний {len(issues)}")
    return d

def review_draft():
    """Тройная проверка черновика тремя РАЗНЫМИ моделями в трёх РАЗНЫХ ролях.

    Одна модель, проверяющая свой же текст одним промптом, склонна соглашаться
    с собой. Поэтому роли разведены (факты / методология / риски) и модели разные.
    Публикация без пройденной проверки заблокирована.
    """
    with open(DRAFT, encoding="utf-8") as f:
        d = json.load(f)
    # Проверяющим отдаём РОВНО ТОТ ЖЕ материал, что видел автор. Иначе они честно
    # объявляют выдумкой то, что есть в источнике: так «110 дней на покупки» у ВТБ
    # были помечены как выдуманные только потому, что размеченный свод по льготному
    # периоду до проверки не доезжал.
    parts = ["ЛЬГОТНЫЙ ПЕРИОД — ВСЕ УПОМИНАНИЯ ПО БАНКАМ:"] + grace_table()
    tf = _opt("tariffs")
    if tf and hasattr(tf, "context_block"):
        try:
            tb = str(tf.context_block() or "")
            if len(tb) > 80:
                parts += ["", "ТАРИФНЫЕ ДОКУМЕНТЫ (сводка значений):", tb]
            if hasattr(tf, "quotes_block"):
                qb = str(tf.quotes_block() or "")
                if len(qb) > 80:
                    parts += ["", qb]
        except Exception as e:
            log(f"tariffs.context_block: {type(e).__name__}: {str(e)[:70]}")
    parts += ["", "ОСТАЛЬНЫЕ ПАРАМЕТРЫ С ВИТРИН:"] + showcase()
    ctx = extra_context()
    if ctx:
        parts += ["", "ОТЧЁТНОСТЬ И РЫНОЧНЫЙ КОНТЕКСТ:", ctx]
    ev = "\n\n".join(parts)[:120000]
    results = brain.review(d["text"], ev)
    out = {"ts": time.time(), "text_hash": hash(d["text"]), "checks": []}
    for role, model, r in results:
        verdict = r.get("verdict", "нельзя")
        issues = r.get("issues") or []
        hard = [i for i in issues if i.get("severity") in ("критично", "важно")]
        out["checks"].append({"role": role, "model": model, "verdict": verdict,
                              "issues": issues, "summary": r.get("summary", "")})
        mark = "СБОЙ ПРОВЕРКИ" if verdict == "сбой" else verdict
        log(f"проверка [{role}/{model}]: {mark} — {r.get('summary','')[:110]}")
        for i in hard:
            log(f"    {i.get('severity')}: {str(i.get('what'))[:110]}")
    out["passed"] = all(c["verdict"] == "публиковать" for c in out["checks"])
    out["failed_checks"] = [c["role"] for c in out["checks"] if c["verdict"] == "сбой"]
    if out["failed_checks"]:
        log("ВНИМАНИЕ: проверки не отработали (" + ", ".join(out["failed_checks"]) +
            ") — это не запрет, а сбой; прогони review ещё раз")
    with open(REVIEW_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log("ИТОГ ПРОВЕРКИ: " + ("пройдена, можно публиковать" if out["passed"]
                             else "НЕ пройдена — публикация заблокирована"))
    return out

def _review_ok(text):
    """Публикуем только если проверка пройдена И относится к ЭТОМУ тексту."""
    try:
        with open(REVIEW_FILE, encoding="utf-8") as f:
            r = json.load(f)
    except Exception:
        return False, "проверка не проводилась (radar.py review)"
    if r.get("text_hash") != hash(text):
        return False, "проверка относится к другому тексту — прогони review заново"
    if not r.get("passed"):
        bad = [c["role"] for c in r["checks"] if c["verdict"] != "публиковать"]
        return False, "проверка не пройдена: " + ", ".join(bad)
    return True, ""

def verify_quotes(res, product=None):
    """Проверяет, что каждая цитата из блока «дословно» РЕАЛЬНО есть в снимках.

    Появилось после разбора, где цитата оказалась склейкой двух блоков вёрстки,
    а вывод — противоречащим собственной таблице поста. Цитата, которой нет в
    источнике, — это выдумка, пусть и невольная; публиковать такое нельзя.
    """
    facts = [f for f in (res.get("facts") or []) if isinstance(f, str) and len(f) > 15]
    if not facts:
        return True, []
    haystack = []
    for key, bank in sources.BANKS.items():
        prods = [product] if product else list(bank["pages"])
        for pr in prods:
            snap = core.load_snap(key, pr)
            if snap:
                haystack.append(" ".join(snap["text"].split()).lower())
    bad = []
    for f in facts:
        needle = " ".join(re.sub(r"^[^:]{1,24}:\s*", "", f).strip(" «»\"").split()).lower()
        core_part = needle[:110]
        if not any(core_part in h for h in haystack):
            bad.append(f)
    return (not bad), bad

def publish_draft():
    """Публикует черновик — только после того, как его прочитали глазами."""
    with open(DRAFT, encoding="utf-8") as f:
        d = json.load(f)
    if d["res"].get("_truncated"):
        log("черновик обрезан — не публикую"); return
    bad = publish.looks_broken(d["text"])
    if bad:
        log(f"черновик испорчен: {bad} — не публикую"); return
    ok_rev, why = _review_ok(d["text"])
    if not ok_rev:
        log(f"НЕ ПУБЛИКУЮ: {why}"); return
    ok, wrong = verify_quotes(d["res"])
    if not ok:
        log("НЕ ПУБЛИКУЮ: цитаты не найдены дословно в снимках источников:")
        for w in wrong:
            log("   " + w[:120])
        return
    imgs = [p for p in d["images"] if os.path.exists(p)]
    publish.post(d["text"], imgs)
    log(f"опубликован обзор: {len(d['text'])} знаков, картинок {len(imgs)}")

# ---------------- публикация ----------------

def _gate_light(text, evidence, revised):
    """Лёгкий гейт после ревизии: один факт-чек на Opus. Без ревизии — считаем, что проверки
    уже прошли (замечаний уровня «важно» не было)."""
    if not revised:
        return True, ""
    try:
        r = brain.review_one(brain.REV_FACTS, "claude-opus-5", text, evidence)
    except Exception as e:
        return False, f"факт-чек сбой: {type(e).__name__}"
    bad = [i for i in (r.get("issues") or []) if i.get("severity") in ("критично", "важно")]
    log(f"факт-чек после ревизии: {r.get('verdict')} — {str(r.get('summary',''))[:100]}")
    if bad:
        return False, "факт-чек: " + "; ".join(str(i.get("what"))[:80] for i in bad[:2])
    return True, ""

def _autopublish_ok(text, evidence=""):
    """Гейт автономной публикации. Публикуем, если проверяющий по ФАКТАМ не нашёл
    ошибок (нет «критично»/«важно») и проверяющий по РИСКАМ не дал «критично».
    Замечания по методологии («правки») не блокируют — это осознанный риск владельца
    ради регулярного ритма. Так автопост защищён от вранья в цифрах и репутационных
    ляпов, но не застревает в бесконечной шлифовке."""
    try:
        checks = brain.review(text, evidence)
    except Exception as e:
        log(f"авто-ревью не отработало: {type(e).__name__}: {e}"); return False, "ревью сбой"
    facts = next((c for r, m, c in checks if r == "факты"), {})
    risk  = next((c for r, m, c in checks if r == "риски"), {})
    fbad = [i for i in (facts.get("issues") or []) if i.get("severity") in ("критично", "важно")]
    rbad = [i for i in (risk.get("issues") or []) if i.get("severity") == "критично"]
    for c in checks:
        log(f"авто-ревью [{c[2].get('role','?') if False else ''}] {c[0]}/{c[1]}: {c[2].get('verdict')}")
    if fbad:
        return False, "факт-чек: " + "; ".join(str(i.get("what"))[:80] for i in fbad[:2])
    if rbad:
        return False, "риски(критично): " + "; ".join(str(i.get("what"))[:80] for i in rbad[:2])
    return True, ""

def publish_posts(posts, dry=False, gated=False):
    sent = 0
    for p in posts:
        if dry:
            print("\n" + "=" * 70); print(p["text"])
            print("картинки:", p["images"]); continue
        if gated:
            ok, why = _autopublish_ok(p["text"], "\n\n".join(grace_table() + showcase()))
            if not ok:
                log(f"АВТО-ПРОПУСК (не прошёл гейт): {why}"); continue
        try:
            publish.post(p["text"], p["images"])
            sent += 1; log(f"опубликовано (важность {p['importance']})"); time.sleep(3)
        except publish.NotConfigured as e:
            log(f"канал не настроен: {e}"); break
        except Exception as e:
            log(f"публикация не удалась: {type(e).__name__}: {e}")
    return sent


# ---------------- ежедневный бриф ----------------
# Канал выходит КАЖДЫЙ день. Тишина на витринах — тоже факт; рынок, регулятор,
# котировки, отзывы и тарифы живут всегда. Один пост в день, всегда через гейт.
DAILY_TITLES = os.path.join(core.STATE, "daily_titles.json")
DAILY_STATS  = os.path.join(core.STATE, "daily_stats.json")
DAILY_DRAFT  = os.path.join(core.OUT, "daily_draft.json")

def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default

EVAL_DAYS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals", "days")
def _eval_snapshot(date, **kw):
    """Снимок дня для evals (12.09): входы и выходы выпуска, чтобы гонять проверки повторно и сравнивать версии."""
    try:
        os.makedirs(EVAL_DAYS, exist_ok=True)
        p = os.path.join(EVAL_DAYS, date + ".json")
        d = _load_json(p, {"date": date}); d.update({k: v for k, v in kw.items() if v is not None}); _save_json(p, d)
    except Exception as e:
        log(f"снимок для evals не записан: {type(e).__name__}: {e}")

def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, indent=1, default=str)

HOLD_FLAG = os.path.join(core.STATE, "hold.flag")
HOLD_MIN = int(os.environ.get("RADAR_HOLD_MIN", "15") or 0)

def _owner_hold_window(caption, text):
    """Окно на «стоп» перед публикацией разбора: черновик уходит владельцу в личку (через Джарвиса),
    HOLD_MIN минут ждём. Если за это время Джарвис получил «радар стоп», он кладёт state/hold.flag,
    и разбор не публикуется (сводка выходит всегда). RADAR_HOLD_MIN=0 — публиковать сразу."""
    if HOLD_MIN <= 0:
        return True
    try:
        if os.path.exists(HOLD_FLAG):
            os.remove(HOLD_FLAG)
    except Exception:
        pass
    body = re.sub(r"</?[bi]>", "", caption + "\n\n" + text)[:3300]
    _notify_owner(f"Радар: через {HOLD_MIN} мин выйдет разбор. Ответь «радар стоп», чтобы не публиковать; "
                  f"«радар ок» — публиковать сразу.\n\n{body}")
    deadline = time.time() + HOLD_MIN * 60
    go_flag = HOLD_FLAG + ".go"
    while time.time() < deadline:
        if os.path.exists(HOLD_FLAG):
            try: os.remove(HOLD_FLAG)
            except Exception: pass
            return False
        if os.path.exists(go_flag):
            try: os.remove(go_flag)
            except Exception: pass
            return True
        time.sleep(10)
    return True

def _notify_owner(text):
    """Личное сообщение владельцу через токен Джарвиса (лежит рядом на сервере)."""
    env = {}
    for path in ("/opt/jarvis/advisor.env", os.path.join(HERE, "..", "Инструменты", "advisor.env")):
        try:
            for line in open(path, encoding="utf-8"):
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1); env[k] = v.strip().strip('"')
            break
        except Exception:
            continue
    tok, chat = env.get("TG_BOT_TOKEN"), env.get("OWNER_CHAT_ID")
    if not tok or not chat:
        return False
    import urllib.request, urllib.parse
    for i in range(0, len(text), 3900):
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[i:i+3900],
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=20)
        except Exception as e:
            log(f"уведомление владельцу не ушло: {type(e).__name__}"); return False
    return True

def _quote_in(fact, ctx_norm):
    needle = " ".join(re.sub(r"^[^:]{1,40}:\s*", "", fact).strip(" «»\"").split()).lower()
    return len(needle) >= 12 and needle[:100] in ctx_norm

_SRC_NAMES = {"alfabank.ru": "Альфа-Банк", "sberbank.ru": "Сбербанк", "tbank.ru": "Т-Банк", "vtb.ru": "ВТБ",
    "gazprombank.ru": "Газпромбанк", "sovcombank.ru": "Совкомбанк", "mtsbank.ru": "МТС Банк", "otpbank.ru": "ОТП",
    "rencredit.ru": "Ренессанс", "uralsib.ru": "Уралсиб", "pay.yandex.ru": "Яндекс Банк", "wb-bank.ru": "ВБ Банк",
    "finance.ozon.ru": "свой банк (own)", "kommersant.ru": "Коммерсантъ", "frankmedia.ru": "Frank Media",
    "vedomosti.ru": "Ведомости", "banki.ru": "Banki.ru", "cbr.ru": "Банк России", "sravni.ru": "Сравни", "moex.com": "Мосбиржа"}

def _src_name(url):
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    for k, v in _SRC_NAMES.items():
        if host.endswith(k): return v
    return host

def _public_counts():
    """Банки и страницы, которые попадают в публикации: свой банк (own) не считаем (12.09, «12 банков под наблюдением»)."""
    pub = [b for b in sources.BANKS.values() if not b.get("own")]
    return sum(len(b["pages"]) for b in pub), len(pub)
def daily_context(found):
    """Всё, что мозг видит в этот день. Это же — доказательная база для факт-чека."""
    n_pages, n_banks_pub = _public_counts()
    comp = [i for i in found if not i.get("own")]
    stats = _load_json(DAILY_STATS, {})
    today = core.msk().strftime("%Y-%m-%d")
    stats[today] = len(comp)
    streak = 0
    for d in sorted(stats, reverse=True):
        if stats[d] == 0: streak += 1
        else: break
    _save_json(DAILY_STATS, {k: stats[k] for k in sorted(stats)[-60:]})
    lines = [f"ВИТРИНЫ КОНКУРЕНТОВ, обход {today}: проверено {n_pages} страниц у {n_banks_pub} банков "
             f"(кредитная карта, рассрочка/BNPL, кредит наличными, автокредит). Изменений: на {len(comp)} страницах."]
    if not comp:
        lines.append(f"Изменений условий не зафиксировано. Дней подряд без изменений у банков: {streak}.")
    for it in comp[:8]:
        lines.append(f"— {it['bank']} / {it['product']} ({it['url']}): значимых строк {len(it['changes'])} из {it['total']}")
        lines += ["   " + c for c in it["changes"][:12]]
    blocks = ["\n".join(lines)]
    nw = _opt("news")
    if nw:
        try: blocks.append(nw.context_block())
        except Exception as e: log(f"новости: {type(e).__name__}: {e}")
    blocks.append(extra_context(refresh=True))
    ctx = "\n\n".join(b for b in blocks if b)
    # свой банк ниже радара: строки про него в контекст дня не попадают
    ctx = "\n".join(l for l in ctx.split("\n") if not re.search(r"озон[\s-]*банк|ozon", l, re.I))
    return ctx

def daily_publish_draft(path=None):
    """Опубликовать ИМЕННО сохранённый черновик (прочитанный глазами) через гейт."""
    d = _load_json(path or DAILY_DRAFT, {})
    ctx = _load_json(os.path.join(core.OUT, "daily_context.txt.json"), {}).get("text", "")
    if not d.get("text") or not ctx:
        log("черновик брифа или контекст не найдены"); return None
    text = publish.tidy(d["text"]); caption = publish.tidy(d.get("caption") or "")
    if not caption:
        log("в черновике нет подписи (caption)"); return None
    ok, why = _autopublish_ok("ПОДПИСЬ К ГРАФИКУ:\n" + caption + "\n\nПОЛНЫЙ РАЗБОР:\n" + text, ctx)
    if not ok:
        log(f"черновик брифа не прошёл гейт: {why}")
        _notify_owner(f"Радар: черновик брифа не прошёл гейт ({why}).")
        return None
    chart = d.get("chart")
    publish.post_brief(caption, text, chart if chart and os.path.exists(chart) else None)
    now = core.msk(); titles = _load_json(DAILY_TITLES, [])
    titles.append({"date": now.strftime("%Y-%m-%d"), "title": d.get("res", {}).get("title", ""),
                   "rubric": brain.RUBRICS[now.weekday()][0]})
    _save_json(DAILY_TITLES, titles[-30:])
    log(f"дневной бриф опубликован из черновика: «{d.get('res', {}).get('title','')[:80]}»")
    try:
        import sitepub
        dd = _load_json(os.path.join(core.OUT, "digest_draft.json"), {})
        dtext = dd.get("text", "") if str(dd.get("ts", ""))[:10] == now.strftime("%Y-%m-%d") else ""
        u = sitepub.publish_day(now.strftime("%Y-%m-%d"), dtext,
                                essay={"title": d.get("res", {}).get("title", ""), "caption": caption, "text": text,
                                       "rubric": brain.RUBRICS[now.weekday()][0], "chart": chart})
        log(f"сайт: {u or 'выпуск не залит'}")
    except Exception as e:
        log(f"сайт: выпуск не опубликован: {type(e).__name__}: {e}")
    return True

def fix_preview(message_id):
    """Перепривязать картинку к уже опубликованному брифу: новая заливка (уникальное имя,
    OpenGraph-страница) и editMessageText с превью над текстом. Нужна, если превью не встало."""
    d = _load_json(DAILY_DRAFT, {})
    chart = d.get("chart")
    if not (d.get("text") and chart and os.path.exists(chart)):
        log("fix_preview: нет черновика или графика"); return False
    url = publish.upload_public(chart, title=d.get("res", {}).get("title", "Кредитный радар"),
                                desc=brain.RUBRICS[core.msk().weekday()][0])
    if not url:
        log("fix_preview: хостинг недоступен"); return False
    body = f'<a href="{url}">&#8205;</a>' + publish.tidy(d["text"])
    r = publish._call("editMessageText", {"chat_id": publish.CHANNEL, "message_id": int(message_id),
        "text": body[:publish.MSG_LIMIT], "parse_mode": "HTML",
        "link_preview_options": json.dumps({"url": url, "prefer_large_media": True, "show_above_text": True})})
    log(f"fix_preview {message_id}: {r.get('ok')} → {url}")
    return bool(r.get("ok"))

BUDGET_USD   = float(os.environ.get("RADAR_BUDGET_USD", "0") or 0)        # 0 — без ограничения
BUDGET_START = os.environ.get("RADAR_BUDGET_START", "")                   # YYYY-MM-DD
BUDGET_DAYS  = int(os.environ.get("RADAR_BUDGET_DAYS", "30") or 30)


# ---------------- тихий день: без модели, когда разбирать нечего ----------------
_SIG = re.compile(r"(\d[\d\s]*[,.]?\d*\s*%|ставк|пск|комисси|лимит|грейс|льготн|беспроцентн|\bдн(ей|я)\b|"
                  r"минимальн\w* плат|первоначальн|срок|запуск|нов(ая|ый|ое) (карт|продукт|тариф|услов)|рассрочк|кэшб|"
                  r"первые \d+|акци[яи]\b|снижен|повышен)", re.I)
_NEWS_HARD = re.compile(r"(банк россии|цб\b|ключев|макропруденц|надбав|мпл\b|лимит|закон|фз\b|госдум|минфин|"
                        r"запуст|снизил|повысил|ставк|просроч|реструктур|бки\b|скоринг|рассрочк|bnpl|кредитн\w* карт)", re.I)
ESSAY_MAX_WEEK = int(os.environ.get("RADAR_ESSAY_MAX_WEEK", "3") or 3)

def _significant(found):
    """Дешёвая эвристика без модели: есть ли за сутки то, ради чего стоит писать разбор.
    Считаем ценовые/продуктовые строки в диффах конкурентов и «жёсткие» новости.
    Возвращает (значимо, причины, косметика)."""
    reasons, cosmetic = [], []
    for it in found:
        if it.get("own"):
            continue
        hits = [c for c in (it.get("changes") or []) if _SIG.search(c) and 8 <= len(c) <= 400]
        if len(hits) >= 2:
            reasons.append(f"{it['bank']} / {it['product']}: {len(hits)} ценовых строк")
        else:
            cosmetic.append(f"{it['bank']} / {it['product']}")
    news_hits = []
    nw = _opt("news")
    if nw:
        try:
            items, _ = nw.fresh(hours=30, mark=False)
            for x in items:
                if x.get("repeat"):
                    continue
                blob = f"{x.get('title','')} {x.get('desc','')}"
                if _NEWS_HARD.search(blob):
                    news_hits.append(x)
        except Exception as e:
            log(f"новости для оценки значимости: {type(e).__name__}: {e}")
    if news_hits:
        reasons.append(f"новостей по делу: {len(news_hits)}")
    return bool(reasons), reasons, cosmetic, news_hits

def _essays_this_week(titles):
    import datetime as _dt
    week_ago = (core.msk().date() - _dt.timedelta(days=7)).isoformat()
    return sum(1 for t in titles if t.get("date", "") >= week_ago and t.get("rubric") not in ("тихий день", "сводка"))

def quiet_text(found, cosmetic, news_hits):
    """Короткий пост «значимых изменений нет» — собирается из данных обхода, модель не вызывается."""
    n_pages, _nb = _public_counts()
    stats = _load_json(DAILY_STATS, {})
    streak = 0
    for d in sorted(stats, reverse=True):
        if stats[d] == 0: streak += 1
        else: break
    lines = ["<b>Тихий день: значимых изменений у банков нет</b>", "",
             f"За сутки проверено {n_pages} страниц условий у {_nb} банков: карты, рассрочка, кредит наличными, "
             f"автокредит. Ценовых и продуктовых изменений не найдено."]
    if cosmetic:
        lines[-1] += " Косметические правки страниц: " + ", ".join(cosmetic[:5]) + "."
    if streak >= 2:
        lines.append(f"Дней подряд без изменений условий: {streak}.")
    if news_hits:
        lines += ["", "<b>Из новостей</b>"]
        for x in news_hits[:3]:
            lines.append(f"• {x['src']}: {x['title']}")
    else:
        lines += ["", "Регуляторных и рыночных новостей по розничному кредиту, заслуживающих разбора, не было."]
    return "\n".join(lines)

def budget_guard():
    """Возвращает (можно_ли_работать, модель_для_генерации). Логика простая: остаток бюджета
    делим на оставшиеся дни; если средний расход за 3 дня выше дневной нормы — генерируем
    на Sonnet; если бюджет исчерпан — не работаем и пишем владельцу."""
    if not BUDGET_USD or not BUDGET_START:
        return True, brain.DAILY_MODEL
    import datetime as _dt
    start = _dt.date.fromisoformat(BUDGET_START); today = core.msk().date()
    left_days = max(1, BUDGET_DAYS - (today - start).days)
    used = brain.spent(since=BUDGET_START); left = BUDGET_USD - used
    per_day = left / left_days
    avg3 = brain.spent(days=3) / 3
    log(f"бюджет: потрачено {used:.2f} из {BUDGET_USD:.0f} $, осталось {left:.2f} $ на {left_days} дн "
        f"({per_day:.2f} $/день), средний расход за 3 дня {avg3:.2f} $/день")
    if left <= 0.5:
        _notify_owner(f"Радар: бюджет исчерпан ({used:.2f} из {BUDGET_USD:.0f} $). Публикации остановлены до пополнения.")
        return False, brain.DAILY_MODEL
    if used >= 0.8 * BUDGET_USD and not os.path.exists(os.path.join(core.STATE, "budget80.flag")):
        open(os.path.join(core.STATE, "budget80.flag"), "w").close()
        _notify_owner(f"Радар: израсходовано 80% бюджета ({used:.2f} из {BUDGET_USD:.0f} $).")
    model = brain.DAILY_MODEL
    if avg3 > per_day * 1.1 or per_day < 0.45:
        model = "claude-sonnet-5"
        log(f"бюджет: генерация на {model} (норма {per_day:.2f} $/день)")
    return True, model

def evening(dry=False, redo=False):
    """Вечерний выпуск — конвейер в evening.py (первоисточники, факты из статей, проверки кодом, судья, шлюзы)."""
    import evening as ev
    return ev.run(dry=dry, redo=redo)

def daily(dry=False, skip_crawl=False, full=False, theme=None):
    """Ежедневный выпуск. Порядок:
    1) сводка дня — всегда (изменения условий, новости по одному событию, акции буквально),
       собирается из данных, модель только редактирует строки;
    2) тематический разбор — только по инфоповоду (конкурент поменял ценовой параметр),
       не чаще RADAR_ESSAY_MAX_WEEK в неделю, короткий, с проверкой стиля и фактов.
    --theme / --full принудительно запускают разбор."""
    import digest, style
    if os.environ.get("RADAR_PRIVATE") == "1" and not dry:
        # приватный режим (ручные копии тарифов, часть закрыта robots.txt): в канал не публикуем никогда
        log("RADAR_PRIVATE=1: публикация в канал отключена, работаем как --dry"); dry = True
    ok_budget, gen_model = budget_guard()
    if not ok_budget and not dry:
        log("выпуск пропущен: бюджет"); return None
    brain.DAILY_MODEL = gen_model
    found = load_findings() if skip_crawl else crawl()
    # новости читаем ДО контекста дня: context_block помечает их показанными, и сводка осталась бы пустой
    nw = _opt("news"); news_items = []
    if nw:
        try: news_items, _ = nw.fresh(hours=30, mark=False)
        except Exception as e: log(f"новости: {type(e).__name__}: {e}")
    import gate
    news_items = gate.filter_items(news_items, log)        # 14.09.2026: реклама, повторы, строки без источника — не проходят
    ctx = daily_context(found)
    _save_json(os.path.join(core.OUT, "daily_context.txt.json"), {"ts": core.msk().isoformat(), "text": ctx})
    now = core.msk()
    titles = _load_json(DAILY_TITLES, [])

    # ---- 1. сводка дня ----
    chs = digest.changes(found)
    sg = _opt("signals"); quotes = None
    if sg:
        try: quotes = sg.load_prev("quotes")
        except Exception: quotes = None
    polished = None
    if chs or [x for x in news_items if not x.get("repeat")]:
        try: polished = brain.digest_polish(chs, [x for x in news_items if not x.get("repeat")])
        except Exception as e: log(f"редактура сводки не удалась: {type(e).__name__}: {e}")
    stats = _load_json(DAILY_STATS, {}); streak = 0
    for d_ in sorted(stats, reverse=True):
        if stats[d_] == 0: streak += 1
        else: break
    n_pages, n_banks_pub = _public_counts()
    dtext = digest.build(chs, news_items, quotes, polished, n_pages, n_banks_pub, streak,
                         now.strftime("%d.%m.%Y"))
    dtext = publish.tidy(style.autofix(dtext))
    dtext, _gate_dropped = gate.vet(dtext, log)            # шлюз публикации: последняя проверка перед каналом и сайтом
    if _gate_dropped:
        log("шлюз: снято строк " + str(len(_gate_dropped)))
    if digest.REJECTED:
        log("сводка, проверка чисел: отвергнуто строк модели " + str(len(digest.REJECTED)) + ": " + "; ".join(digest.REJECTED))
    _dig_issues = digest.check(dtext, chs, news_items)
    if _dig_issues:
        log("сводка, самопроверка: " + "; ".join(_dig_issues))
        _notify_owner("Радар, сводка: самопроверка нашла проблемы, проверь пост после выхода: " + "; ".join(_dig_issues))
    if len(dtext) > publish.MSG_LIMIT:
        dtext = dtext[:publish.MSG_LIMIT - 1].rsplit("\n", 1)[0] + "…"
    _save_json(os.path.join(core.OUT, "digest_draft.json"), {"ts": now.isoformat(), "text": dtext, "changes": chs})
    _eval_snapshot(now.strftime("%Y-%m-%d"), chs=chs, news=news_items, quotes=quotes, ctx=ctx, digest=dtext,
                   n_pages=n_pages, n_banks=n_banks_pub, streak=streak)
    site_essay = {}   # заполняется после публикации разбора; уходит на сайт вместе со сводкой
    def _publish_digest(essay_trig=None):
        """Сводка выходит после разбора: если разбор опубликован, строка про этот банк ссылается на него."""
        t = dtext
        if essay_trig:
            t = t.replace(f"• {essay_trig['bank']}", f"• {essay_trig['bank']}", 1)
            t += f"\n\nРазбор изменения у {essay_trig['bank']} — в посте выше."
        if dry:
            print("\n" + "=" * 70 + "\nСВОДКА (%d зн.):\n" % len(t) + t); return
        try:
            publish.post_brief(t, "", None, mode="photo")
            gate.remember(t)                                  # ключи опубликованных строк — чтобы не повторить
            if nw and hasattr(nw, "mark_seen"): nw.mark_seen(news_items)
            titles.append({"date": now.strftime("%Y-%m-%d"), "title": "сводка", "rubric": "сводка"})
            _save_json(DAILY_TITLES, titles[-30:])
            log(f"сводка опубликована: изменений {len(chs)}, новостей {len([x for x in news_items if not x.get('repeat')])}, "
                f"расход {brain.spent(days=1):.2f} $")
        except Exception as e:
            log(f"сводка не ушла: {type(e).__name__}: {e}"); _notify_owner(f"Радар: сводка не ушла ({e}).")
            return
        # ---- сайт: страница выпуска (тот же текст, что в канале), архив, лента, sitemap, IndexNow ----
        try:
            import sitepub
            u = sitepub.publish_day(now.strftime("%Y-%m-%d"), t, essay=site_essay or None)
            log(f"сайт: {u or 'выпуск не залит'}")
        except Exception as e:
            log(f"сайт: выпуск не опубликован: {type(e).__name__}: {e}")

    # ---- 2. тематический разбор — только по инфоповоду ----
    trig = digest.trigger(chs)
    n_week = _essays_this_week(titles)
    if theme:
        rubric = ("Заданная тема", theme)
    elif trig and (n_week < ESSAY_MAX_WEEK or full):
        rubric = ("Инфоповод", f"{trig['bank']} ({trig['product']}) изменил параметр «{trig['param']}»: было "
                  f"«{trig['old'][:200]}», стало «{trig['new'][:200]}». Разбери, как «{trig['param']}» устроен у других "
                  f"банков по данным дня, чем отличается новое значение, и что это значит для соседних продуктов, "
                  f"маржи и риска.")
    elif full:
        rubric = brain.RUBRICS[now.weekday()]
    else:
        log(f"разбор не нужен: инфоповод {'есть' if trig else 'нет'}, разборов за неделю {n_week}/{ESSAY_MAX_WEEK}")
        _publish_digest(); return {"digest": dtext, "quiet": True}
    log(f"разбор: {rubric[0]} — {rubric[1][:120]}")
    recent = [t["title"] for t in titles[-7:] if t.get("rubric") != "сводка"]
    res = brain.daily_post(ctx, rubric, recent, now.strftime("%d.%m.%Y, %A"))
    if not res.get("post"):
        log("разбор: мозг не вернул пост"); _notify_owner("Радар: разбор не собрался (мозг не вернул пост)."); _publish_digest(); return None
    ctx_norm = " ".join(ctx.split()).lower()
    facts = [f for f in (res.get("facts") or []) if isinstance(f, str)]
    res["facts"] = [f for f in facts if _quote_in(f, ctx_norm)]
    srcs = [u for u in (res.get("sources") or []) if isinstance(u, str) and u.startswith("http") and u in ctx][:4]
    blocked_src = [u for u in srcs if not core.allowed(u)]
    if blocked_src and not dry:
        # источник закрыт robots.txt — разбор на нём в канал не идёт (13.09.2026, правило Никиты)
        log("разбор НЕ опубликован: источник закрыт robots.txt: " + "; ".join(blocked_src))
        _notify_owner("Радар: разбор опирается на документ, закрытый robots.txt, в канал не пошёл: " + "; ".join(blocked_src))
        _publish_digest(); return None
    stamp = f"daily_{now:%Y%m%d}"
    chart = _chart_from_spec(res.get("chart"), os.path.join(core.OUT, f"chart_{stamp}.png"))
    def _render(r):
        rr = dict(r); rr["facts"] = [f[:160] for f in (rr.get("facts") or [])][:2]; rr["also_today"] = []
        t = style.autofix(publish.render_post(rr).replace("<i>Дословно с витрин:</i>", "<i>Дословно:</i>"))
        st = (r.get("chart") or {}).get("_stats") if isinstance(r.get("chart"), dict) else None
        if st and st.get("n", 0) >= 3 and st.get("r") == st.get("r"):
            verdict = "связь статистически значима" if st["p"] < 0.05 else "статистически значимой связи нет"
            t += (f"\n\n<i>Статистика по графику: {verdict} (p = {st['p']:.2f} при пороге 0,05); n = {st['n']}, "
                  f"r = {st['r']:+.2f}, R² = {st['r2']:.2f}; ρ Спирмена = {st['rho']:+.2f}, p = {st['p_rho']:.2f}.</i>")
        if srcs:
            t += "\n\n" + "Источники: " + " · ".join(f'<a href="{u}">{_src_name(u)}</a>' for u in srcs)
        return t
    def _cap(r):
        c = publish.tidy(style.autofix(r.get("caption") or ""))
        if not c:
            c = f"<b>{r.get('title','')}</b>\n\n" + re.sub(r"<[^>]+>", "", (r.get("post") or ""))[:500]
        st = (r.get("chart") or {}).get("_stats") if isinstance(r.get("chart"), dict) else None
        if st and st.get("n", 0) >= 3 and st.get("r") == st.get("r") and "значим" not in c.lower():
            c += ("\n\n<i>" + ("Связь статистически значима" if st["p"] < 0.05 else "Статистически значимой связи нет")
                  + f" (p = {st['p']:.2f}, n = {st['n']}).</i>")
        return c
    text = publish.tidy(_render(res)); caption = _cap(res)
    _save_json(DAILY_DRAFT, {"ts": now.isoformat(), "res": res, "text": text, "caption": caption, "chart": chart})
    # ---- проверки: факты (модель) + стиль (код) → одна ревизия ----
    issues = []
    try:
        checks = brain.review("ПОДПИСЬ К ГРАФИКУ (уходит в канал):\n" + caption + "\n\nПОЛНЫЙ РАЗБОР:\n" + text, ctx)
        for r_, m_, c_ in checks:
            log(f"ревью {r_}/{m_}: {c_.get('verdict')} — {str(c_.get('summary',''))[:100]}")
        issues = [i for r_, m_, c_ in checks for i in (c_.get("issues") or []) if i.get("severity") in ("критично", "важно")]
    except Exception as e:
        log(f"ревью не удалось: {type(e).__name__}: {e}")
    st_issues = style.check(caption + "\n" + text)
    if st_issues:
        log("стиль: " + "; ".join(i["what"] for i in st_issues[:8]))
        issues += style.issues_for_model(st_issues)
    st = (res.get("chart") or {}).get("_stats") if isinstance(res.get("chart"), dict) else None
    if st and st.get("n", 0) >= 3 and st.get("r") == st.get("r"):
        issues.append({"severity": "важно", "what": f"Статистика по графику: n={st['n']}, r={st['r']:+.2f}, R²={st['r2']:.2f}, p={st['p']:.2f}.",
                       "fix": "При p>0.05 пиши словами «статистически значимой связи нет»; сами числа добавятся автоматически."})
    if issues:
        try:
            res2 = brain.daily_revise(res, issues[:14], ctx)
            if res2.get("post"):
                res2["facts"] = [f for f in (res2.get("facts") or []) if isinstance(f, str) and _quote_in(f, ctx_norm)]
                if isinstance(res.get("chart"), dict): res2["chart"] = res["chart"]
                res = res2; text = publish.tidy(_render(res)); caption = _cap(res)
                _save_json(DAILY_DRAFT, {"ts": now.isoformat(), "res": res, "text": text, "caption": caption, "chart": chart, "revised": True})
        except Exception as e:
            log(f"ревизия не удалась: {type(e).__name__}: {e}")
    left = style.check(caption + "\n" + text)
    hard = [i for i in left if not i["what"].startswith(("средняя длина", "предложение из"))]
    _eval_snapshot(now.strftime("%Y-%m-%d"), essay={"res": res, "text": text, "caption": caption, "rubric": rubric[0]})
    if hard:
        log("стиль после ревизии: " + "; ".join(i["what"] for i in hard[:6]))
    if dry:
        print("\n" + "=" * 70); print("ПОДПИСЬ (%d зн.):" % len(caption)); print(caption); print("-" * 70); print(text)
        print("график:", chart); print("стиль (осталось):", [i["what"] for i in left])
        _publish_digest(trig if rubric[0] == "Инфоповод" else None)
        return {"digest": dtext, "text": text, "caption": caption}
    if len(hard) >= 3:
        log("разбор НЕ опубликован: стиль не вычищен")
        _notify_owner("Радар: разбор не прошёл проверку стиля, отложен. Черновик:\n\n" + re.sub(r"</?[bi]>", "", text)[:3500])
        _publish_digest(); return None
    ok, why = _gate_light("ПОДПИСЬ К ГРАФИКУ:\n" + caption + "\n\nПОЛНЫЙ РАЗБОР:\n" + text, ctx, revised=bool(issues))
    if not ok:
        log(f"разбор НЕ опубликован: {why}")
        _notify_owner(f"Радар: разбор не прошёл гейт ({why}). Черновик:\n\n" + re.sub(r"</?[bi]>", "", text)[:3500])
        _publish_digest(); return None
    if not _owner_hold_window(caption, text):
        log("разбор НЕ опубликован: владелец остановил («радар стоп»)"); _publish_digest(); return None
    try:
        publish.post_brief(caption, text, chart)
    except Exception as e:
        log(f"публикация разбора не удалась: {type(e).__name__}: {e}"); _publish_digest(); return None
    site_essay.update({"title": res.get("title", ""), "caption": caption, "text": text, "rubric": rubric[0], "chart": chart})
    titles.append({"date": now.strftime("%Y-%m-%d"), "title": res.get("title", ""), "rubric": rubric[0]})
    _save_json(DAILY_TITLES, titles[-30:])
    log(f"разбор опубликован: «{res.get('title','')[:80]}» ({rubric[0]}); расход за сегодня {brain.spent(days=1):.2f} $")
    _publish_digest(trig if rubric[0] == "Инфоповод" else None)
    return {"digest": dtext, "text": text, "caption": caption, "chart": chart}


# ---------------- проверки и бейк-офф ----------------
def selftest():
    print("ИСТОЧНИКИ")
    ok = bad = 0
    for key, bank in sources.BANKS.items():
        for product, url in bank["pages"].items():
            html, text, how, err = core.get_rendered(url)
            status = f"OK ({how}, {len(text)//1024} КБ)" if not err else f"НЕТ: {err}"
            ok, bad = (ok + 1, bad) if not err else (ok, bad + 1)
            print(f"  {bank['name']:<18}{product:<20}{status}")
    print(f"\nитого: доступно {ok}, недоступно {bad}")
    print("\nКЛЮЧИ И КАНАЛ")
    print("  ANTHROPIC_API_KEY:", "есть" if brain.KEY else "НЕТ")
    try:
        print("  канал:", publish.selftest())
    except Exception as e:
        print("  канал:", type(e).__name__, str(e)[:90])
    print("\nСКРИНШОТЫ")
    p = core.screenshot("https://example.com", os.path.join(core.OUT, "selftest.png"))
    print("  ", "работают" if p else "НЕ работают (нет Chrome или он не снимает)")

def bakeoff(models=("claude-sonnet-5", "claude-opus-5", "claude-fable-5")):
    """Один и тот же материал через разные модели — чтобы выбирать по факту, а не по вере."""
    items = load_findings()
    if not items:
        log("для сравнения нужна хотя бы одна находка — сначала crawl"); return
    it = max(items, key=lambda x: len(x["changes"]))
    for m in models:
        brain.MODEL = m
        t = time.time()
        try:
            res = brain.analyze_change(it["bank"], it["product"], it["url"], it["changes"])
            print("\n" + "=" * 70)
            print(f"МОДЕЛЬ: {m}   {time.time()-t:.1f}s   важность {res.get('importance')}")
            print("-" * 70)
            print(res.get("title", "")); print(); print((res.get("post") or "")[:1600])
        except Exception as e:
            print(f"\n{m}: сбой {type(e).__name__}: {str(e)[:120]}")


# ---------------- экономный бэкап ----------------
KEEP_RAW_DAYS = 14        # сырые копии страниц держим две недели, дальше — только текст

def prune():
    """Чистка архива: сырые HTML тяжёлые и нужны только для разбора спорных случаев.
    Текстовые снапшоты (по ним считаются диффы) не трогаем — они лёгкие и нужны всегда."""
    import glob, datetime
    cutoff = (core.msk() - datetime.timedelta(days=KEEP_RAW_DAYS)).strftime("%Y%m%d")
    freed = n = 0
    for f in glob.glob(os.path.join(core.STATE, "*", "raw", "*.html.gz")):
        stamp = os.path.basename(f).rsplit("_", 1)[-1].split(".")[0]
        if stamp.isdigit() and stamp < cutoff:
            freed += os.path.getsize(f); os.remove(f); n += 1
    for f in sorted(glob.glob(os.path.join(core.OUT, "shot_*.png")))[:-60]:
        freed += os.path.getsize(f); os.remove(f); n += 1
    log(f"чистка: удалено {n} файлов, освобождено {freed//1024} КБ")

# ---------------- CLI ----------------
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    dry = "--dry" in sys.argv
    if cmd == "evening":
        evening(dry=dry, redo="--redo" in sys.argv); sys.exit(0)
    try:
        if cmd == "crawl":
            crawl()
        elif cmd == "analyze":
            publish_posts(analyze(), dry=True)
        elif cmd == "run":
            crawl(); publish_posts(analyze(), dry=dry)
        elif cmd == "auto":
            crawl(); publish_posts(analyze(), gated=True)
        elif cmd == "fix-preview":
            fix_preview(sys.argv[2])
        elif cmd == "daily":
            if "--publish-draft" in sys.argv:
                dp = sys.argv[sys.argv.index("--draft") + 1] if "--draft" in sys.argv else None
                daily_publish_draft(dp)
            else:
                th = None
                if "--theme" in sys.argv:
                    th = sys.argv[sys.argv.index("--theme") + 1]
                daily(dry=dry, skip_crawl="--no-crawl" in sys.argv, full="--full" in sys.argv, theme=th)
        elif cmd == "digest":
            d = digest()
            if d:
                publish_posts([d], dry=dry)
        elif cmd == "selftest":
            selftest()
        elif cmd == "bakeoff":
            bakeoff()
        elif cmd == "prune":
            prune()
        elif cmd == "collect":
            collect()
        elif cmd == "market":
            topic = "раскрытие" if "--раскрытие" in sys.argv else "экономика"
            prod = next((a for a in sys.argv[2:] if not a.startswith("-")), "кредитная карта")
            if market(prod, topic=topic):
                show_draft()
        elif cmd == "draft":
            show_draft()
        elif cmd == "review":
            review_draft()
        elif cmd == "revise":
            if revise_draft():
                show_draft()
        elif cmd == "publish-draft":
            publish_draft()
        elif cmd == "context":
            print(extra_context(refresh="--refresh" in sys.argv))
        else:
            print(__doc__)
    except Exception:
        log("СБОЙ:\n" + traceback.format_exc())
        raise

if __name__ == "__main__":
    main()
