#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регрессия утреннего выпуска (17.09.2026): недельный лимит разборов считается по дням, а не по записям журнала.
Кейс: state/daily_titles.json на 17.09 — шесть записей за 10.09 (три поста + черновики), одна за 11.09, дальше только сводки;
старый счётчик давал 7/3 и блокировал разбор при инфоповоде 16–17.09. Запуск: python3 evals/daily_test.py"""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import digest

R = "Репутация как ранний индикатор"
titles = ([{"date": "2026-09-10", "title": f"т{i}", "rubric": R} for i in range(6)]
          + [{"date": "2026-09-11", "title": "грейс", "rubric": "Куда идут банки"}]
          + [{"date": d, "title": "сводка", "rubric": "сводка"} for d in ("2026-09-12", "2026-09-13", "2026-09-14", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17")])
d = datetime.date
checks = [
    ("17.09: два дня с разбором (10.09 и 11.09), не 7", digest.essays_this_week(titles, d(2026, 9, 17)) == 2),
    ("17.09: лимит 3 не исчерпан", digest.essays_this_week(titles, d(2026, 9, 17)) < 3),
    ("18.09: 10.09 выпал из окна — один день", digest.essays_this_week(titles, d(2026, 9, 18)) == 1),
    ("19.09: окно пустое", digest.essays_this_week(titles, d(2026, 9, 19)) == 0),
    ("сводки и тихие дни не считаются", digest.essays_this_week([{"date": "2026-09-17", "rubric": "тихий день"}], d(2026, 9, 17)) == 0),
    ("три разбора в три дня — лимит исчерпан", digest.essays_this_week([{"date": f"2026-09-1{i}", "rubric": "Инфоповод"} for i in (5, 6, 7)], d(2026, 9, 17)) == 3),
    ("пустой журнал — ноль", digest.essays_this_week([], d(2026, 9, 17)) == 0),
]
fails = [n for n, ok in checks if not ok]
for n, ok in checks: print(("PASS " if ok else "FAIL ") + n)
print("\nИТОГ:", "OK" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
