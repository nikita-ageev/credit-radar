#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Публикующий путь без сети (17.09.2026): post_brief / edit_text / fit / tidy / gate.finalize с подменённым Bot API.
Повод: обрезка сводки 11.09 на лимите подписи, «текст на полуслове», моджибейк в канале (09.09) — всё это было найдено
читателями, а не тестом. Запуск: python3 evals/publish_test.py"""
import os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RADAR_TG_TOKEN", "test"); os.environ.setdefault("RADAR_CHANNEL", "@test")
import publish, gate, style

publish.TOKEN, publish.CHANNEL = "test", "@test"
publish.LEDGER = os.path.join(tempfile.mkdtemp(), "published.json")
gate.STATE_FILE = os.path.join(tempfile.mkdtemp(), "pl.json")
CALLS = []
def _fake_call(method, params, timeout=60):
    CALLS.append((method, dict(params))); return {"ok": True, "result": {"message_id": len(CALLS)}}
def _fake_multipart(method, fields, files, timeout=180):
    CALLS.append((method, dict(fields))); return {"ok": True, "result": {"message_id": len(CALLS)}}
publish._call, publish._multipart = _fake_call, _fake_multipart
png = os.path.join(tempfile.mkdtemp(), "c.png"); open(png, "wb").write(bytes([137, 80, 78, 71, 13, 10, 26, 10]))

def raises(fn, *a, **k):
    try: fn(*a, **k); return False
    except publish.Broken: return True

para = "Банк снизил ставку по кредитной карте, срок льготного периода без изменений. "
long_text = "<b>Сводка</b>\n\n" + "\n".join("• " + para * 3 for _ in range(40))      # ≈ 9 тыс. знаков
mid_text = "<b>Сводка</b>\n\n" + "\n".join("• " + para for _ in range(20))            # ≈ 1,7 тыс. — больше подписи, меньше сообщения
mojibake = ("".join(chr(c) for c in (208, 159, 208, 190, 209, 129, 209, 130)) + " ") * 40   # «Пост» как latin-1
checks = []

CALLS.clear(); publish.post_brief(mid_text, "", None, mode="photo")
checks.append(("текст без фото > 1024 уходит sendMessage целиком (лимит 4096, не 1024)",
               CALLS[-1][0] == "sendMessage" and CALLS[-1][1]["text"] == publish.tidy(mid_text)))
checks.append(("текст без фото > 4096 — отказ Broken, а не обрезка", raises(publish.post_brief, long_text, "", None, mode="photo")))
CALLS.clear(); publish.post_brief(mid_text, "", png, mode="photo")
cap = CALLS[-1][1]["caption"]
checks.append(("подпись к фото режется до лимита по границе предложения/строки с многоточием",
               CALLS[-1][0] == "sendPhoto" and len(cap) <= publish.CAP_LIMIT and cap.endswith("…") and cap[-2] != " "))
checks.append(("моджибейк не публикуется", raises(publish.post_brief, mojibake, "", None)))
checks.append(("edit_text длиннее лимита — отказ", raises(publish.edit_text, 1, long_text)))
CALLS.clear(); publish.edit_text(1, mid_text)
checks.append(("edit_text в лимите — editMessageText", CALLS[-1][0] == "editMessageText"))
checks.append(("журнал опубликованного пишется", os.path.exists(publish.LEDGER) and len(json.load(open(publish.LEDGER))) >= 1))
f = publish.fit(long_text)
checks.append(("fit: длинный текст укладывается в 4096 по границе строки с многоточием",
               len(f) <= publish.MSG_LIMIT and f.endswith("…") and f[:-1].endswith(".")))
checks.append(("fit: короткий текст не трогается", publish.fit(mid_text) == mid_text and publish.fit("") == ""))
t = publish.tidy('Ставка 0,1%/0,2 % <a href="https://x.ru/2026/09/17/">ссылка</a> https://y.ru/a/b/c\n\n\n\nконец  \n')
checks.append(("tidy: слэш между числами раздвинут, адреса не тронуты, пустые строки схлопнуты",
               "0,1% / 0,2" in t and 'href="https://x.ru/2026/09/17/"' in t and "https://y.ru/a/b/c" in t and "\n\n\n" not in t and t.endswith("конец")))
src = "<b>Новости</b>\n• Frank Media: <a href=\"https://frankmedia.ru/1\">Ставка по картам у конкурентов выросла</a>."
fin, dropped = gate.finalize(src)
checks.append(("finalize = tidy → autofix → vet (слово «конкурент» заменено, строка с источником осталась)",
               fin == gate.vet(publish.tidy(style.autofix(src)))[0] and "конкурент" not in fin and "frankmedia.ru" in fin))
checks.append(("finalize пустого текста — пусто", gate.finalize("") == ("", [])))
fails = [n for n, ok in checks if not ok]
for n, ok in checks: print(("PASS " if ok else "FAIL ") + n)
print("\nИТОГ:", "OK" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
