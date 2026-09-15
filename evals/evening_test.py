#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регрессия вечернего выпуска (15.09.2026): «Главное» — по весу темы для розничного кредитора, не по типу источника;
строки без факта и без «что значит» не публикуются. Повтор выпуска 15.09: ОКБ про POS-кредиты стало «Главным» без единого факта,
а ЦБ про эскроу вышло голой ссылкой. Запуск: python3 evals/evening_test.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import digest, evening

okb = {"src": "ОКБ", "title": "Итоги кредитования в точках продаж в августе 2026 года", "link": "https://bki-okb.ru/1", "desc": ""}
nbki = {"src": "НБКИ", "title": "НБКИ: в августе 2026 года было выдано 101,0 тыс. автокредитов", "link": "https://nbki.ru/1", "desc": ""}
cbr = {"src": "Банк России", "title": "Информация о проектном финансировании: заключении кредитных договоров с застройщиками, использующими счета эскроу", "link": "https://cbr.ru/1", "desc": ""}
vedo = {"src": "Ведомости", "title": "«Сберинвестиции» ожидают ключевую ставку 13,75% в конце года", "link": "https://vedomosti.ru/1", "desc": ""}
komm = {"src": "Коммерсантъ", "title": "Сбербанк секьюритизирует портфель потребкредитов", "link": "https://kommersant.ru/1", "desc": ""}
kk = {"src": "Frank Media", "title": "Выдача кредитных карт в августе достигла рекорда", "link": "https://frankmedia.ru/1", "desc": ""}

lines = [
    (okb, "", [], ""),
    (nbki, "", ["Выдачи снизились на 6,9% к августу 2025"], ""),
    (cbr, "", [], ""),
    (vedo, "", ["В октябре брокер ждёт сохранения ставки 14%"], "ставки по кредитам не снизятся до декабря"),
    (komm, "", ["Шестой выпуск — 80 млрд руб."], ""),
]
text = evening.build_text(lines, "15.09.2026")
main = text.split("\n")[2] if "Главное" in text else ""
w = digest.npv_weight
checks = [
    ("вес POS ниже веса ставки", w(okb) < w(vedo)),
    ("вес эскроу ниже веса автокредита", w(cbr) < w(nbki)),
    ("вес кредитных карт максимальный", w(kk) == 1.0),
    ("«Главное» — не ОКБ про POS", "точках продаж" not in main),
    ("«Главное» — ставка (Ведомости)", "13,75%" in main),
    ("строка без факта (ОКБ) не в «Главном» без фактов", not main.startswith("<b>Главное.</b> ОКБ")),
    ("без строк с весом < 0.5 в «Главном»", digest.npv_weight(lines[0][0]) < 0.5),
]
# без единой содержательной строки — выпуск без «Главного»
text2 = evening.build_text([(okb, "", [], ""), (cbr, "", [], "")], "15.09.2026")
checks.append(("нет фактов ни у кого — заголовка «Главное» нет", "Главное" not in text2))
fails = [n for n, ok in checks if not ok]
for n, ok in checks: print(("PASS " if ok else "FAIL ") + n)
print("\nИТОГ:", "OK" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
