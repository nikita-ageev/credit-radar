#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Eval Радара: снимки дней → проверки кодом → судья Sonnet 5 → таблица + стоимость.

  python3 evals/run.py                     # все дни из evals/days, сохранённые тексты, судья Haiku
  python3 evals/run.py --regen             # пересобрать сводку из сохранённых входов (Sonnet, ≈0,02 $/день)
  python3 evals/run.py --days 2026-09-12   # один день
  python3 evals/run.py --no-judge          # только код, бесплатно
  python3 evals/run.py show results/X.jsonl [--fails]
  python3 evals/run.py compare results/A.jsonl results/B.jsonl

Запускать из /root/radar (импортирует digest, style, brain, publish, radar как есть).
Снимок дня пишет radar.daily → evals/days/ГГГГ-ММ-ДД.json: chs, news, quotes, ctx, digest, essay.
"""
import os, sys, re, json, argparse, urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
for _line in open(os.path.join(ROOT, "radar.env"), encoding="utf-8"):      # ключ и настройки как у cron (run.sh)
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1); os.environ.setdefault(_k.strip().replace("export ", ""), _v.strip().strip('"').strip("'"))
import digest, style, brain, publish  # noqa: E402

DAYS = os.path.join(HERE, "days"); RESULTS = os.path.join(HERE, "results")
os.makedirs(DAYS, exist_ok=True); os.makedirs(RESULTS, exist_ok=True)
COST = {"regen": 0.0, "judge": 0.0}
JUDGE_MODEL = os.environ.get("EVAL_JUDGE") or "claude-sonnet-5"   # сильный судья; выпусков мало, ≈0,02 $/день
_JPRICE = (3.0, 15.0, 3.75, 0.30)

def _rubric(section):
    t = open(os.path.join(HERE, "rubric.md"), encoding="utf-8").read()
    return t.split(f"## Текст для судьи ({section})", 1)[1].split("\n", 1)[1].split("\n## ", 1)[0].strip()
JUDGE_DIGEST = _rubric("сводка"); JUDGE_ESSAY = _rubric("разбор")

def _judge(system, user, max_tokens=600):
    payload = {"model": JUDGE_MODEL, "max_tokens": max_tokens, "thinking": {"type": "disabled"},
               "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
               "messages": [{"role": "user", "content": user}]}
    d = brain._post(payload, timeout=60)
    u = d.get("usage") or {}
    COST["judge"] += (u.get("input_tokens", 0) * _JPRICE[0] + u.get("output_tokens", 0) * _JPRICE[1]
                      + u.get("cache_creation_input_tokens", 0) * _JPRICE[2] + u.get("cache_read_input_tokens", 0) * _JPRICE[3]) / 1e6
    txt = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else {}

# ---------- разбор текста сводки ----------
_PLAIN = lambda s: re.sub(r"<[^>]+>", "", s or "")
def sections(text):
    """{'Условия у банков': [строки], 'Новости': [...], ...} — строки без маркера."""
    out, cur = {}, None
    for line in (text or "").split("\n"):
        m = re.match(r"<b>(.+?)</b>\s*$", line.strip())
        if m: cur = m.group(1); out[cur] = []; continue
        if cur and line.strip():
            out[cur].append(line.strip().lstrip("• ").strip())
    return out
_NUM = re.compile(r"\d+(?:[,.]\d+)?")
def _nums(s): return set(_NUM.findall(_PLAIN(s or "")))

def bank_lines(text): return sections(text).get("Условия у банков", [])

def d_checks(text, chs, news):
    bl = bank_lines(text)
    body = [l for k, v in sections(text).items() if k in ("Условия у банков", "Новости") for l in v]
    allowed = set()
    for c in chs or []:
        allowed |= _nums(c.get("old")) | _nums(c.get("new")) | {str(c.get("raw", ""))}
    st = style.check(text)
    hard = [i for i in st if not i["what"].startswith(("средняя длина", "предложение из"))]
    grounded = [(_nums(l) - {"1", "2", "3"}) <= allowed for l in bl if not l.startswith("Без изменений")] if chs else []
    return {"d1": int(not digest.check(text, chs, news)),
            "d2": int(not hard),
            "d3": int(all(re.search(r"[.!?…»]$", _PLAIN(l).strip()) and not re.search(r"[а-яё]{3,}…", _PLAIN(l)) for l in body)) if body else None,
            "d4": int(all("<a href" in l for l in bl if not l.startswith("Без изменений"))) if bl and chs else None,
            "d5": int(all(grounded)) if grounded else None,
            "d6": int(not re.search(r"конкурент|наш банк|у нас|для нас|озон|ozon", _PLAIN(text), re.I))}

def d_judge(text, chs):
    bl = [l for l in bank_lines(text) if not l.startswith("Без изменений")]
    if not bl or not chs: return {}
    src = "\n".join(f"- {c['bank']} / {c['product']} / {c['param']} / {c['kind']}: БЫЛО «{(c.get('old') or '')[:200]}» СТАЛО «{(c.get('new') or '')[:200]}»"
                    for c in chs[:8])
    lines = "\n".join(f"{i+1}. {_PLAIN(l)}" for i, l in enumerate(bl))
    j = _judge(JUDGE_DIGEST, f"ИСХОДНЫЕ ДАННЫЕ:\n{src}\n\nСТРОКИ СВОДКИ:\n{lines}")
    rows = j.get("lines") or []
    out = {}
    for p in ("d7", "d8", "d9"):
        v = [r.get(p) for r in rows if r.get(p) in (0, 1)]
        out[p] = int(all(v)) if v else None
    out["why"] = "; ".join(f"{r.get('i')}: {r['why']}" for r in rows if r.get("why"))
    out["lines"] = rows
    return out

def e_checks(essay, ctx):
    if not essay or not essay.get("text"): return {}, {}
    text = essay.get("caption", "") + "\n" + essay["text"]
    st = style.check(text)
    hard = [i for i in st if not i["what"].startswith(("средняя длина", "предложение из"))]
    code = {"e5": int(not hard)}
    if ctx:
        cn = _nums(ctx); tn = _nums(text) - {"1", "2", "3", "10", "100"}
        code["e4_code"] = int(len(tn - cn) <= max(2, len(tn) // 4))   # расчётные числа допускаем, но не больше четверти
    user = (f"ДАННЫЕ ДНЯ:\n{ctx[:12000]}\n\n" if ctx else "ДАННЫЕ ДНЯ: не сохранены.\n\n") + f"РАЗБОР:\n{_PLAIN(text)[:6000]}"
    j = _judge(JUDGE_ESSAY, user, 400)
    return code, {k: j.get(k) for k in ("e1", "e2", "e3", "e4", "why")}

def regen_digest(day):
    chs, news, quotes = day.get("chs") or [], day.get("news") or [], day.get("quotes")
    fresh = [x for x in news if not x.get("repeat")]
    polished = None
    if chs or fresh:
        before = brain.spent(days=0) if False else None
        polished = brain.digest_polish(chs, fresh)
    text = digest.build(chs, news, quotes, polished, day.get("n_pages", 0), day.get("n_banks", 0),
                        day.get("streak", 0), day["date"][8:10] + "." + day["date"][5:7] + "." + day["date"][:4], fetch_rate=False)
    return publish.tidy(style.autofix(text))

def run_day(day, regen=False, use_judge=True):
    text = day.get("digest") or ""
    if regen:
        c0 = brain.spent(days=1); text = regen_digest(day); COST["regen"] += brain.spent(days=1) - c0
    chs, news = day.get("chs") or [], day.get("news") or []
    pts = d_checks(text, chs, news)
    dj = d_judge(text, chs) if use_judge and text else {}
    for p in ("d7", "d8", "d9"): pts[p] = dj.get(p)
    ecode, ej = ({}, {})
    if day.get("essay") and not regen:
        ecode, ej = e_checks(day["essay"], day.get("ctx")) if use_judge else (e_checks_code_only(day["essay"], day.get("ctx")), {})
        pts.update({"e1": ej.get("e1"), "e2": ej.get("e2"), "e3": ej.get("e3"),
                    "e4": (None if ej.get("e4") is None else int(ej.get("e4") == 1 and ecode.get("e4_code", 1) == 1)),
                    "e5": ecode.get("e5")})
    app = [v for v in pts.values() if v in (0, 1)]
    return {"date": day["date"], "regen": regen, "text": text, "points": pts, "judge": {"digest": dj, "essay": ej},
            "score": round(sum(app) / len(app), 3) if app else None,
            "n_lines": len(bank_lines(text)), "n_chs": len(chs)}

def e_checks_code_only(essay, ctx):
    text = essay.get("caption", "") + "\n" + essay["text"]
    hard = [i for i in style.check(text) if not i["what"].startswith(("средняя длина", "предложение из"))]
    return {"e5": int(not hard)}

NAMES = {"d1": "самопроверка", "d2": "стиль", "d3": "строки закончены", "d4": "ссылки в строках", "d5": "числа из данных",
         "d6": "не палит автора", "d7": "одно событие", "d8": "ясно, что изменилось", "d9": "факт, не реклама",
         "e1": "заголовок = вывод", "e2": "гипотезы помечены", "e3": "нет причинности из точки", "e4": "числа из данных", "e5": "стиль разбора"}

def summarize(rows):
    pts = {}
    for p in NAMES:
        v = [r["points"].get(p) for r in rows if r["points"].get(p) in (0, 1)]
        pts[p] = (round(100 * sum(v) / len(v)), len(v)) if v else (None, 0)
    return {"n": len(rows), "points": pts, "score": round(100 * sum(r["score"] or 0 for r in rows) / max(1, len(rows))),
            "cost_regen": round(COST["regen"], 4), "cost_judge": round(COST["judge"], 4),
            "fails": {r["date"]: [p for p, v in r["points"].items() if v == 0] for r in rows if any(v == 0 for v in r["points"].values())}}

def render(s, meta):
    L = [f"## Eval Радара — {meta['stamp']} · {meta['tag']} · {'пересборка' if meta['regen'] else 'сохранённые тексты'}", "",
         "| Пункт | Доля 1 | n |", "|---|---|---|"]
    for p, (v, n) in s["points"].items():
        if n: L.append(f"| {p} {NAMES[p]} | {v} % | {n} |")
    L += ["", f"**Итог: {s['score']} / 100** на {s['n']} днях. Стоимость: пересборка {s['cost_regen']} $, судья {s['cost_judge']} $.",
          "Нули: " + ("; ".join(f"{d}: {', '.join(v)}" for d, v in s["fails"].items()) or "нет")]
    return "\n".join(L)

def load_days(only=None):
    out = []
    for f in sorted(os.listdir(DAYS)):
        if f.endswith(".json") and (not only or f[:-5] in only):
            d = json.load(open(os.path.join(DAYS, f), encoding="utf-8")); d.setdefault("date", f[:-5]); out.append(d)
    return out

def cmd_run(a):
    days = load_days(a.days.split(",") if a.days else None)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M"); name = f"{stamp}_{a.tag}{'_regen' if a.regen else ''}"
    path = os.path.join(RESULTS, name + ".jsonl"); rows = []
    print(f"{len(days)} дней → {path}")
    with open(path, "w", encoding="utf-8") as f:
        for d in days:
            r = run_day(d, regen=a.regen, use_judge=not a.no_judge); rows.append(r)
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            zeros = [p for p, v in r["points"].items() if v == 0]
            print(f"{r['date']} score={r['score']} строк {r['n_lines']}/{r['n_chs']} {('нули: ' + ','.join(zeros)) if zeros else 'ок'}")
        s = summarize(rows); meta = {"stamp": stamp, "tag": a.tag, "regen": a.regen}
        f.write(json.dumps({"summary": s, "meta": meta}, ensure_ascii=False) + "\n")
    md = render(s, meta); open(os.path.join(RESULTS, name + ".md"), "w", encoding="utf-8").write(md + "\n"); print("\n" + md)

def _load(p):
    rows = [json.loads(l) for l in open(p, encoding="utf-8")]
    return [r for r in rows if "summary" not in r], next(r for r in rows if "summary" in r)

def cmd_show(a):
    rows, _ = _load(a.file)
    for r in rows:
        zeros = [p for p, v in r["points"].items() if v == 0]
        if a.fails and not zeros: continue
        print(f"=== {r['date']} score={r['score']} {('нули: ' + ','.join(zeros)) if zeros else ''}")
        print(_PLAIN(r["text"]))
        dj = r["judge"].get("digest") or {}
        if dj.get("why"): print("судья (сводка):", dj["why"])
        ej = r["judge"].get("essay") or {}
        if ej.get("why"): print("судья (разбор):", ej["why"])
        print()

def cmd_compare(a):
    ra, sa = _load(a.a); rb, sb = _load(a.b)
    print(f"A {os.path.basename(a.a)} итог {sa['summary']['score']}\nB {os.path.basename(a.b)} итог {sb['summary']['score']}\n| Пункт | A | B |\n|---|---|---|")
    for p in NAMES:
        va, vb = sa["summary"]["points"][p][0], sb["summary"]["points"][p][0]
        if va is not None or vb is not None: print(f"| {p} {NAMES[p]} | {va} | {vb} |")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run"); r.add_argument("--regen", action="store_true"); r.add_argument("--days"); r.add_argument("--tag", default="run")
    r.add_argument("--no-judge", action="store_true")
    s = sub.add_parser("show"); s.add_argument("file"); s.add_argument("--fails", action="store_true")
    c = sub.add_parser("compare"); c.add_argument("a"); c.add_argument("b")
    argv = sys.argv[1:] if sys.argv[1:] and sys.argv[1] in ("run", "show", "compare") else ["run"] + sys.argv[1:]
    a = ap.parse_args(argv)
    {"run": cmd_run, "show": cmd_show, "compare": cmd_compare}[a.cmd](a)
