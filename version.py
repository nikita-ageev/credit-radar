# -*- coding: utf-8 -*-
"""Версия Радара (17.09.2026): semver из файла VERSION + короткий sha коммита, если код лежит в git.

    radar.py version          → «Радар v1.0.0+0d69284»
Правила: любой PR, меняющий поведение, добавляет строку в CHANGELOG.md [Unreleased]; перед merge такого PR — bump VERSION
(patch — починки, minor — новые рубрики/источники/форматы) и перенос строк в раздел версии. CI проверяет, что верхняя
версия CHANGELOG равна VERSION (evals/version_test.py); тег v<VERSION> ставится в Actions после merge в main.
"""
import os, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

def _read():
    try:
        return open(os.path.join(HERE, "VERSION"), encoding="utf-8").read().strip()
    except Exception:
        return "0.0.0"

def _sha():
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

VERSION = _read()          # «1.0.0» — то, что видит читатель сайта
SHA = _sha()               # «0d69284» или пусто вне git
__version__ = VERSION + (f"+{SHA}" if SHA else "")

def banner():
    return f"Радар v{__version__}"

if __name__ == "__main__":
    print(banner())
