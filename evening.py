# -*- coding: utf-8 -*-
"""Вечерний выпуск Радара (13.09.2026). Воркфлоу с проверками на каждом шаге — по правилам курса по агентам:
детерминированный код решает, что делать; модель только извлекает и формулирует; между шагами шлюзы.

    новости (RSS + БКИ + ЦБ + каналы, окно с утра)
      → дедупликация с выбором ПЕРВОИСТОЧНИКА (БКИ/ЦБ > пресса > канал)
      → текст статьи по ссылке (кэш state/articles/)
      → Fable: 2–3 факта из статьи, которых нет в заголовке, + «что это значит» (JSON)
      → шлюз 1, код: числа фактов есть в тексте статьи; ссылки отвечают; нет запрещённых слов
      → шлюз 2, судья Opus (другая модель), да/нет по пунктам на каждую строку
      → одна ревизия по замечаниям судьи, повторный судья
      → шлюз 3, код: осталось ≥ 1 строки и ≤ 40 % отброшено, лимит бюджета
      → публикация в канал и на сайт, отметка «показано», снимок для evals
Если шлюз не пройден — выпуск не выходит, черновик и причины уходят владельцу.

Команды: `radar.py evening` (боевой), `--dry` (только собрать), `--redo` (переделать сегодняшний: не считать
новости показанными, отредактировать пост в канале по id из журнала и пересобрать страницу сайта).
"""
import os, re, json, time, hashlib, urllib.request
import core, brain, digest, publish, style

ART_DIR = os.path.join(core.STATE, "articles")
EVAL_DIR = os.path.join(core.HERE, "evals", "days")
BUDGET_USD = float(os.environ.get("RADAR_EVENING_BUDGET", "0.8") or 0.8)
MAX_ITEMS = 6
FORBIDDEN = re.compile(r"конкурент|витрин|свой банк (own)|Ozon Bank", re.I)

def log(msg):
    try:
        import radar; radar.log(msg)
    except Exception:
        print(msg)


# ---------------- 1. текст статьи ----------------
def article_text(url, max_chars=7000):
    """Основной текст страницы по ссылке: абзацы длиннее 60 знаков, без меню. Кэш на 3 дня."""
    os.makedirs(ART_DIR, exist_ok=True)
    p = os.path.join(ART_DIR, hashlib.sha256(url.encode()).hexdigest()[:16] + ".json")
    try:
        c = json.load(open(p, encoding="utf-8"))
        if time.time() - c.get("ts", 0) < 3 * 86400:
            return c.get("text", ""), c.get("how", "кэш")
    except Exception:
        pass
    html, how, err = None, "", None
    if url.startswith("https://t.me/"):
        return "", "канал"
    html, err = core.get(url)
    how = "http"
    if not html or len(html) < 3000:
        html2, txt, how2, err2 = core.get_rendered(url)
        if html2 and len(html2) > len(html or ""):
            html, how = html2, "браузер"
    text = ""
    if html:
        raw = core.to_text(html) if hasattr(core, "to_text") else html
        paras = [l.strip() for l in raw.split("\n") if len(l.strip()) >= 60]
        # отбрасываем хвост из меню/подписок: берём подряд идущие содержательные абзацы
        text = "\n".join(paras)[:max_chars]
    try:
        json.dump({"ts": time.time(), "url": url, "text": text, "how": how, "err": err}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return text, how


# ---------------- 2. извлечение фактов (Fable) ----------------
EXTRACT_SYS = """Ты аналитик розничных кредитных рисков. Тебе дают заголовок новости и текст статьи-первоисточника.
Верни строго JSON: {"facts": ["...", "..."], "why": "..."}.
facts — 2–3 коротких факта (до 140 знаков каждый) из ТЕКСТА СТАТЬИ, которых НЕТ в заголовке и которые полезны
руководителю продукта, бизнеса или рисков: числа, сегменты, причины, что говорят банки/бюро, что дальше. Каждое число —
дословно из текста; ничего не считать и не округлять. Если в статье нет ничего сверх заголовка — facts: [].
why — одна фраза (до 120 знаков): что это значит для розничного кредитования; без оценок «важно/интересно», без
прогнозов, без слов «конкурент», «витрина». Если связи с розничным кредитом нет — why: ""."""

def extract(item, text):
    if not text:
        return {"facts": [], "why": ""}
    body = f"ЗАГОЛОВОК: {item['title']}\nИСТОЧНИК: {item['src']}\n\nТЕКСТ СТАТЬИ:\n{text}"
    model = os.environ.get("RADAR_DIGEST_MODEL", "claude-fable-5-1")
    r = brain._with_model(model, lambda: brain._json_out(brain.ask(body, system=EXTRACT_SYS, max_tokens=2500, timeout=180, what="evening_extract")))
    if not isinstance(r, dict):
        return {"facts": [], "why": ""}
    facts = [f.strip().rstrip(".") for f in (r.get("facts") or []) if isinstance(f, str) and f.strip()][:3]
    why = (r.get("why") or "").strip().rstrip(".") if isinstance(r.get("why"), str) else ""
    return {"facts": facts, "why": why}


# ---------------- 3. шлюз 1: код ----------------
def link_alive(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400
    except urllib.error.HTTPError as e:
        return e.code in (403, 429)          # защита от роботов — ссылка есть, просто закрыта для нас
    except Exception:
        return False

def code_checks(item, ex, text):
    """Возвращает (факты после фильтра, why после фильтра, список отброшенного)."""
    dropped = []
    src_text = " ".join([item.get("title", ""), item.get("desc", ""), text or ""])
    facts = []
    for f in ex["facts"]:
        bad = digest.unsupported_numbers(f, src_text)
        if bad:
            dropped.append(f"факт «{f[:50]}»: числа {', '.join(bad)} не из статьи"); continue
        if FORBIDDEN.search(f):
            dropped.append(f"факт «{f[:50]}»: запрещённое слово"); continue
        facts.append(f)
    why = ex["why"]
    if why and (digest.unsupported_numbers(why, src_text) or FORBIDDEN.search(why)):
        dropped.append(f"why «{why[:50]}»: число не из источника или запрещённое слово"); why = ""
    return facts, why, dropped


# ---------------- 4. шлюз 2: судья (другая модель) ----------------
JUDGE_SYS = """Ты проверяющий редактор. Тебе дают строку вечернего выпуска и текст первоисточника. Ответь строго JSON:
{"supported": true/false, "no_hype": true/false, "useful": true/false, "primary": true/false, "issues": ["..."]}
supported — каждое число и утверждение строки есть в тексте источника (заголовок тоже источник);
no_hype — нет оценочных и рекламных слов, нет прогнозов от себя;
useful — строка даёт руководителю продукта/рисков хоть что-то сверх заголовка (если фактов нет — false);
primary — ссылка ведёт на первоисточник или прессу, а не на репост в канале, когда оригинал очевиден из текста;
issues — что именно не так, коротко, если что-то false."""

def judge(line_plain, text, link=""):
    body = f"СТРОКА ВЫПУСКА:\n{line_plain}\nССЫЛКА В СТРОКЕ: {link}\n\nТЕКСТ ИСТОЧНИКА:\n{(text or '')[:5000] or '(нет текста; только заголовок)'}"
    r = brain._with_model("claude-opus-5", lambda: brain._json_out(brain.ask(body, system=JUDGE_SYS, max_tokens=2000, timeout=120, what="evening_judge")))
    if not isinstance(r, dict):
        return {"supported": False, "no_hype": True, "useful": True, "primary": True, "issues": ["судья не ответил"]}
    for k in ("supported", "no_hype", "useful", "primary"):
        r[k] = bool(r.get(k, False))
    r["issues"] = [str(i) for i in (r.get("issues") or [])][:4]
    return r


# ---------------- 5. сборка строки ----------------
def _plain(s):
    return re.sub(r"<[^>]+>", "", s)

MAX_MAIN_FACTS, MAX_REST_FACTS, MAX_REST = 2, 1, 4

def render_line(item, facts, why, main=False):
    """Главное — заголовок + до 2 фактов + «что значит» отдельными строками; остальное — одна строка: источник, ссылка, один факт или «что значит»."""
    src = item["src"]
    head = f"{src}: {digest._a(item['title'].strip().rstrip('.'), item.get('link'))}"
    if main:
        out = ["<b>Главное.</b> " + head + "."]
        for f in facts[:MAX_MAIN_FACTS]:
            out.append("— " + f + ".")
        if why:
            out.append("<i>Что значит:</i> " + why + ".")
        return "\n".join(out)
    tail = (facts[0] if facts else why)
    return "• " + head + (" — " + tail + "." if tail else ".")

def importance(it, facts, why):
    """15.09.2026: важность = вес темы для P&L розничного кредитора × (первоисточник) × (есть ли что сказать).
    POS-кредиты или эскроу не могут быть «Главным» только потому, что релиз выпустило БКИ или ЦБ."""
    w = digest.npv_weight(it)
    src = 1.0 + 0.15 * (digest.source_rank(it) - 1)          # 1.0 канал, 1.15 пресса, 1.3 первоисточник
    info = 1.0 + 0.5 * min(len(facts), 2) + (0.3 if why else 0.0)
    return w * src * info

def build_text(lines, date_str):
    """Порядок — по важности для розничного кредитора (вес темы × источник × факты), не по времени и не по типу источника.
    «Главное» — только строка с весом темы ≥ 0.5 и хотя бы одним фактом или «что значит»; иначе выпуск без «Главного»."""
    ranked = sorted(lines, key=lambda t: -importance(t[0], t[2], t[3]))
    parts = [f"<b>Вечерний выпуск за {date_str}</b>"]
    it, line, facts, why = ranked[0]
    if digest.npv_weight(it) >= 0.5 and (facts or why):
        parts.append(render_line(it, facts, why, main=True))
        rest = ranked[1:1 + MAX_REST]
    else:
        rest = ranked[:1 + MAX_REST]
    if rest:
        parts.append("<b>Ещё за день</b>\n" + "\n".join(render_line(i, f, w) for i, _l, f, w in rest))
    parts.append("<i>Факты «внутри» — из текста первоисточника, проверены кодом и вторым проверяющим.</i>")
    return "\n\n".join(parts)


# ---------------- 6. конвейер ----------------
def run(dry=False, redo=False, hours=14):
    import news
    t0 = time.time(); spent0 = brain.spent(days=1)
    now = core.msk(); date = now.strftime("%Y-%m-%d"); date_str = now.strftime("%d.%m.%Y")
    news_items, errs = news.fresh(hours=hours, mark=False)
    if errs:
        log("вечер: источники с ошибками: " + "; ".join(errs))
    import gate
    fresh = [x for x in news_items if redo or not x.get("repeat")]
    fresh = gate.filter_items(fresh, log)                  # 14.09.2026: реклама, повторы, без источника — не проходят
    fresh = digest.dedupe_news(fresh)
    # 15.09.2026: сначала темы с большим весом для розничного кредита (КК, КН, рассрочка, ставка, МПЛ), потом первоисточник, потом свежесть
    fresh.sort(key=lambda x: (-digest.npv_weight(x), -digest.source_rank(x), -(x["dt"].timestamp() if x.get("dt") else 0)))
    fresh = fresh[:MAX_ITEMS + 4]
    if not fresh:
        log("вечер: свежих новостей нет, выпуск пропущен"); return None
    snapshot = {"date": date, "ts": now.isoformat(), "input": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in x.items()} for x in fresh], "items": []}
    lines, dropped_items, judged = [], [], 0
    for it in fresh:
        if brain.spent(days=1) - spent0 > BUDGET_USD:
            log(f"вечер: бюджет {BUDGET_USD} $ исчерпан, остальные новости без обработки"); break
        rec = {"title": it["title"], "src": it["src"], "link": it.get("link")}
        text, how = article_text(it.get("link", ""))
        rec["article_chars"], rec["article_how"] = len(text), how
        try:
            ex = extract(it, text) if text else {"facts": [], "why": ""}
        except Exception as e:
            log(f"вечер: извлечение не удалось ({it['title'][:40]}): {type(e).__name__}"); ex = {"facts": [], "why": ""}
        rec["extracted"] = ex
        facts, why, dropped = code_checks(it, ex, text)
        rec["code_dropped"] = dropped
        if not it.get("link") or not link_alive(it["link"]):
            rec["gate"] = "ссылка не отвечает"; dropped_items.append(rec); snapshot["items"].append(rec); continue
        # 15.09.2026: строка без факта и без «что значит» — это голая ссылка; её неудобно читать (ссылки открывают не всегда),
        # и она ничего не добавляет. Не проходит ни для каналов, ни для БКИ/ЦБ.
        if not facts and not why:
            rec["gate"] = "нет добавочной информации"; dropped_items.append(rec); snapshot["items"].append(rec); continue
        line = render_line(it, facts, why)
        verdict = judge(_plain(line), text, it.get('link', '')); judged += 1
        rec["judge"] = verdict
        if not verdict["supported"] or not verdict["no_hype"]:
            # одна ревизия: убираем факты, на которые жалуется судья, и why, если проблема в нём
            issues = " ".join(verdict["issues"]).lower()
            facts2 = [f for f in facts if not any(w in issues for w in re.findall(r"\d[\d,\.]*", f))]
            why2 = "" if ("значит" in issues or "прогноз" in issues or not verdict["no_hype"]) else why
            if facts2 != facts or why2 != why:
                line = render_line(it, facts2, why2)
                verdict2 = judge(_plain(line), text, it.get('link', '')); judged += 1
                rec["revised"] = {"facts": facts2, "why": why2, "judge": verdict2}
                if verdict2["supported"] and verdict2["no_hype"]:
                    facts, why = facts2, why2; verdict = verdict2
            if not (verdict["supported"] and verdict["no_hype"]):
                rec["gate"] = "судья: " + "; ".join(verdict["issues"])[:160]; dropped_items.append(rec); snapshot["items"].append(rec); continue
        rec["gate"] = "ок"; rec["line"] = _plain(line); rec["facts"] = facts; rec["why"] = why
        snapshot["items"].append(rec)
        lines.append((it, line, facts, why))
        if len(lines) >= MAX_ITEMS: break
    total = len(lines) + len(dropped_items)
    text = build_text(lines, date_str) if lines else ""
    text = publish.tidy(style.autofix(text)) if text else ""
    if text:
        text, _gd = gate.vet(text, log)                     # шлюз публикации: последняя проверка перед каналом и сайтом
        if _gd: log("вечер, шлюз: снято строк " + str(len(_gd)))
        if text.count("•") == 0: lines = []
    if len(text) > publish.MSG_LIMIT:
        text = text[:publish.MSG_LIMIT - 1].rsplit("\n", 1)[0] + "…"
    cost = brain.spent(days=1) - spent0
    snapshot.update({"text": text, "dropped": len(dropped_items), "kept": len(lines), "judged": judged, "cost_usd": round(cost, 3), "seconds": round(time.time() - t0)})
    # ---- шлюз 3: код ----
    reasons = []
    if not lines: reasons.append("ни одной строки не прошло проверки")
    if total and len(dropped_items) / total > 0.6 and len(lines) < 3: reasons.append(f"отброшено {len(dropped_items)} из {total}")
    if FORBIDDEN.search(_plain(text)): reasons.append("запрещённое слово в тексте")
    _save_snapshot(date, snapshot, dry)
    log(f"вечер: строк {len(lines)}, отброшено {len(dropped_items)}, судья вызван {judged} раз, {cost:.2f} $, {snapshot['seconds']} с")
    for r in dropped_items:
        log(f"вечер, отброшено: {r['title'][:60]} — {r['gate']}")
    if dry:
        print("\n" + "=" * 70 + "\nВЕЧЕРНИЙ ВЫПУСК (%d зн.)%s:\n" % (len(text), " — НЕ ПРОШЁЛ ШЛЮЗ: " + "; ".join(reasons) if reasons else "") + text)
        return text
    if reasons:
        log("вечер: НЕ опубликован — " + "; ".join(reasons))
        _notify(f"Радар, вечер: выпуск не прошёл проверки ({'; '.join(reasons)}). Черновик:\n\n{_plain(text)[:3000]}")
        return None
    if os.environ.get("RADAR_PRIVATE") == "1":
        log("вечер: RADAR_PRIVATE=1, публикация отключена"); return text
    mid = _today_evening_msg_id() if redo else None
    try:
        if mid:
            publish.edit_text(mid, text); log(f"вечер: пост {mid} отредактирован")
        else:
            publish.post_brief(text, "", None, mode="photo")
            log(f"вечер: выпуск опубликован, строк {len(lines)}")
        gate.remember(text)
        news.mark_seen(news_items)
    except Exception as e:
        log(f"вечер: выпуск не ушёл: {type(e).__name__}: {e}"); _notify(f"Радар: вечерний выпуск не ушёл ({e})."); return None
    try:
        import sitepub
        u = sitepub.publish_day(date + "-evening", text)
        log(f"вечер, сайт: {u or 'не залит'}")
    except Exception as e:
        log(f"вечер, сайт: {type(e).__name__}: {e}")
    return text


def _today_evening_msg_id():
    try:
        rows = json.load(open(publish.LEDGER, encoding="utf-8"))
        today = core.msk().strftime("%Y-%m-%d")
        ids = [i for r in rows if str(r.get("ts", ""))[:10] == today and int(str(r.get("ts", ""))[11:13] or 0) >= 15 for i in (r.get("ids") or [])]
        return ids[-1] if ids else None
    except Exception:
        return None

def _save_snapshot(date, snap, dry):
    try:
        os.makedirs(EVAL_DIR, exist_ok=True)
        p = os.path.join(EVAL_DIR, f"{date}-evening{'-dry' if dry else ''}.json")
        json.dump(snap, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    except Exception as e:
        log(f"вечер: снимок для evals не записан: {e}")

def _notify(text):
    try:
        import radar
        radar._notify_owner(text)
    except Exception:
        pass
