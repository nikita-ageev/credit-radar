# -*- coding: utf-8 -*-
"""Общий журнал Радара (17.09.2026): одна функция log для radar.py, evening.py и остальных модулей.
Раньше evening.log импортировал radar ради radar.log, а radar.evening импортировал evening — цикл, который работал
только потому, что импорты были ленивыми. Теперь оба берут log отсюда; radar.log остаётся псевдонимом."""
import os
import core

LOG = os.path.join(core.HERE, "radar_log.txt")

def log(msg):
    line = f"{core.msk():%Y-%m-%d %H:%M:%S} МСК  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
