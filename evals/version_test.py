#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Версия и changelog (17.09.2026): VERSION — semver; верхний раздел CHANGELOG.md равен VERSION; [Unreleased] есть и стоит выше.
Запуск: python3 evals/version_test.py"""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import version

ver = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
log = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
heads = re.findall(r"^## \[([^\]]+)\](?: — (\d{4}-\d{2}-\d{2}))?", log, re.M)
released = [(v, d) for v, d in heads if v.lower() != "unreleased"]
checks = [
    ("VERSION — semver", re.fullmatch(r"\d+\.\d+\.\d+", ver) is not None),
    ("version.VERSION читает файл", version.VERSION == ver),
    ("version.__version__ начинается с VERSION", version.__version__.startswith(ver)),
    ("CHANGELOG: раздел [Unreleased] есть", bool(heads) and heads[0][0].lower() == "unreleased"),
    ("CHANGELOG: верхняя выпущенная версия = VERSION", bool(released) and released[0][0] == ver),
    ("CHANGELOG: у выпущенной версии есть дата", bool(released) and bool(released[0][1])),
    ("CHANGELOG: версии не повторяются", len({v for v, _ in released}) == len(released)),
]
fails = [n for n, ok in checks if not ok]
for n, ok in checks: print(("PASS " if ok else "FAIL ") + n)
print("\nИТОГ:", "OK" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
