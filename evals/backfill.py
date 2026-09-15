#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разовое заполнение evals/days из того, что уже лежит в out/ (12.09) и в dry-логах (11.09).
Дальше снимки пишет сам radar.daily. Запуск из /root/radar: python3 evals/backfill.py"""
import os, sys, re, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT); os.chdir(ROOT)
DAYS = os.path.join(ROOT, "evals", "days"); os.makedirs(DAYS, exist_ok=True)

def save(date, **kw):
    p = os.path.join(DAYS, date + ".json")
    d = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"date": date}
    d.update({k: v for k, v in kw.items() if v is not None})
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved", p, sorted(d.keys()))

# 12.09: черновик сводки с изменениями + контекст дня + котировки
dd = json.load(open("out/digest_draft.json", encoding="utf-8"))
date = dd["ts"][:10]
ctx = json.load(open("out/daily_context.txt.json", encoding="utf-8"))
quotes = None
try:
    import signals; quotes = signals.load_prev("quotes")
except Exception as e:
    print("котировки не загрузились:", e)
import sources
save(date, chs=dd["changes"], news=[], quotes=quotes, ctx=ctx["text"] if ctx["ts"][:10] == date else None,
     digest=dd["text"], n_pages=sum(len(b["pages"]) for b in sources.BANKS.values()), n_banks=len(sources.BANKS))

# 11.09: текст сводки из dry-лога (входы не сохранялись) + разбор из daily_draft (если он за 11.09)
try:
    log = open("out/dry_0911.log", encoding="utf-8").read()
    m = re.search(r"СВОДКА \(\d+ зн\.\):\n(.*?)\n\d{4}-\d\d-\d\d \d\d:\d\d", log, re.S)
    text11 = m.group(1).strip() if m else None
    essay = None
    dr = json.load(open("out/daily_draft.json", encoding="utf-8"))
    if dr["ts"][:10] == "2026-09-11":
        essay = {"res": dr["res"], "text": dr["text"], "caption": dr["caption"]}
    save("2026-09-11", digest=text11, essay=essay)
except Exception as e:
    print("11.09 не собрался:", e)
