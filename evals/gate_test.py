#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регрессия шлюза публикации (14.09.2026): пост 56 — реклама с erid из канала, «ссылка» Дом.РФ, повтор ставки и акций.
Запуск: python3 evals/gate_test.py  → PASS/FAIL по пунктам. Гоняется перед каждым изменением digest/gate/news."""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gate, news

gate.STATE_FILE = os.path.join(tempfile.mkdtemp(), "pl.json")
gate.seed(["kr:14,0", "stock:VTBR"])
gate.remember('<b>Новости</b>\n• Коммерсантъ: <a href="https://www.kommersant.ru/doc/1">Август в долг // Под конец лета количество выданных кредиток достигло рекорда с начала 2025 года</a>.')

POST = """<b>Сводка за 14.09.2026</b>

<b>Главное.</b> <a href="https://www.cbr.ru/hd_base/KeyRate/">Банк России</a> сохранил ключевую ставку 14,0 % годовых.

<b>Банк России</b>
• <a href="https://www.cbr.ru/hd_base/KeyRate/">Банк России</a> сохранил ключевую ставку 14,0 % годовых.

<b>Новости</b>
• Frank Media: <a href="https://frankmedia.ru/304565">Компании из СНГ задолжали российскому бизнесу почти триллион рублей</a>.
• Канал: <a href="https://vtb.ru/promo/?utm_source=tg&amp;utm_medium=post&amp;erid=2VtzqwWWjgG">В Клубе предпринимателей ВТБ действуют всего два правила</a>.
• Труба под Неглинной: <a href="http://%D0%94%D0%BE%D0%BC.%D0%A0%D0%A4/">Крупные банки не планируют менять ставки после решения ЦБ</a> — что-то.
• Коммерсантъ: <a href="https://www.kommersant.ru/doc/1">Август в долг // Под конец лета количество выданных кредиток достигло рекорда с начала 2025 года</a>.
• Ведомости: <a href="https://www.vedomosti.ru/finance/articles/1">Банки вслед за ЦБ взяли паузу в корректировке ставок</a> — смысл.

<b>Акции</b>
• Акции <a href="https://www.moex.com/ru/issue.aspx?code=VTBR">ВТБ</a> за месяц подешевели с 56,9 до 51,9 ₽ (-8,9%), индекс Мосбиржи за то же время снизился на 0,9%.
• Акции <a href="https://www.moex.com/ru/issue.aspx?code=SVCB">Совкомбанк</a> за неделю подешевели с 10,6 до 10,0 ₽ (-5,7%), индекс Мосбиржи за то же время вырос на 1,2%."""

out, dropped = gate.vet(POST)
reasons = " | ".join(r for _, r in dropped)
checks = [
    ("ставка-повтор снята (Главное и раздел)", out.count("14,0 %") == 0),
    ("реклама с erid снята", "Клубе предпринимателей" not in out),
    ("строка со «ссылкой» Дом.РФ снята", "Дом" not in out and "%D0%94" not in out),
    ("повтор новости (Коммерсантъ) снят", "Август в долг" not in out),
    ("акции ВТБ (тикер уже был) сняты", "code=VTBR" not in out),
    ("акции Совкомбанка (не было) оставлены", "code=SVCB" in out),
    ("Ведомости с источником оставлены", "vedomosti.ru" in out),
    ("раздел «Банк России» исчез целиком", "<b>Банк России</b>" not in out),
    ("релевантность: B2B без розничного термина в заголовке — нет", not news._relevant({"src": "Frank Media", "title": "Компании из СНГ задолжали российскому бизнесу почти триллион рублей", "desc": "просрочка 43 млрд"})),
    ("релевантность: история без кредита в заголовке — нет", not news._relevant({"src": "Frank Media", "title": "Король Петербургской биржи и просто Штиглиц", "desc": "кредит"})),
    ("релевантность: «банки взяли паузу в ставках» — да", news._relevant({"src": "Ведомости", "title": "Банки вслед за ЦБ взяли паузу в корректировке ставок", "desc": "кредит"})),
    ("реклама: «ЦБ ограничит рекламу кредитов» — не реклама", gate.is_ad("ЦБ хочет ограничить рекламу кредитов", "", "https://www.rbc.ru/x") is None),
    ("источник: t.me/канал/id — да, Дом.РФ — нет", gate.valid_source("https://t.me/trubapodneglinnoy/14753") and not gate.valid_source("http://Дом.РФ/")),
]
fails = [n for n, ok in checks if not ok]
for n, ok in checks: print(("PASS " if ok else "FAIL ") + n)
print("\nснято:", reasons)
print("\nИТОГ:", "OK" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
