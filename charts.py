# -*- coding: utf-8 -*-
"""Графики для постов канала. Задача — выглядеть дорого и читаться с телефона:
тёмный фон, спокойная палитра, крупная типографика, никакого «экселя».
Каждый график — самостоятельная картинка с заголовком, подписью и источником."""
import os, re as _re, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib import font_manager
from matplotlib.textpath import TextPath
from matplotlib.font_manager import FontProperties

BG    = "#0E1621"     # фон Telegram-тёмной темы, картинка садится в ленту без шва
PANEL = "#15202B"
INK   = "#E8EEF4"
MUTED = "#7A8B9C"
GRID  = "#22303C"
ACC   = "#3FA9F5"     # акцент — «мы» / главный ряд
WARM  = "#F5A623"     # внимание
GOOD  = "#4CD07D"
BAD   = "#FF6B6B"
SERIES = ["#3FA9F5", "#F5A623", "#4CD07D", "#B57BFF", "#FF6B6B", "#5AD2D2", "#E8EEF4"]

def _font():
    for name in ("PT Sans", "Helvetica Neue", "Arial", "DejaVu Sans", "Liberation Sans"):
        try:
            font_manager.findfont(name, fallback_to_default=False); return name
        except Exception:
            continue
    return "DejaVu Sans"

FONT = _font()

def _style(ax, fig):
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID); ax.spines[s].set_linewidth(1.0)
    ax.tick_params(colors=MUTED, labelsize=11, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.9, alpha=0.9)
    ax.set_axisbelow(True)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_fontname(FONT)

def _frame(fig, title, subtitle, source):
    import textwrap
    w_in = fig.get_figwidth()
    title = textwrap.fill(title, max(30, int(w_in * 5.2)))          # длинный заголовок — в две строки
    two = "\n" in title
    fig.text(0.055, 0.955, title, color=INK, fontsize=19 if not two else 17.5, fontweight="bold",
             fontname=FONT, va="top", linespacing=1.15)
    if subtitle:
        subtitle = textwrap.fill(subtitle, max(40, int(w_in * 7.5)))
        fig.text(0.055, 0.895 if not two else 0.845, subtitle, color=MUTED, fontsize=12, fontname=FONT, va="top")
    if source:
        fig.text(0.055, 0.035, source, color=MUTED, fontsize=9.5, fontname=FONT)
    fig.text(0.945, 0.035, "Кредитный радар", color=GRID, fontsize=9.5,
             fontname=FONT, ha="right")

def bars(path, labels, values, title, subtitle="", source="", unit="%",
         highlight=None, decimals=1):
    """Горизонтальные бары — сравнение конкурентов по одному показателю.
    highlight — подпись, которую выделяем акцентом (обычно «свой банк (own)»)."""
    n = len(labels)
    fig, ax = plt.subplots(figsize=(9, 1.05 * n + 2.6), dpi=200)
    order = sorted(range(n), key=lambda i: values[i])
    labels = [labels[i] for i in order]; values = [values[i] for i in order]
    cols = [ACC if (highlight and l == highlight) else "#2C3E50" for l in labels]
    b = ax.barh(labels, values, color=cols, height=0.62, zorder=3)
    for rect, v, l in zip(b, values, labels):
        ax.text(rect.get_width() + max(values) * 0.015, rect.get_y() + rect.get_height() / 2,
                f"{v:.{decimals}f}{unit}", va="center", color=INK if (highlight and l == highlight) else MUTED,
                fontsize=12, fontweight="bold" if (highlight and l == highlight) else "normal",
                fontname=FONT)
    _style(ax, fig)
    ax.grid(axis="y", visible=False); ax.grid(axis="x", color=GRID, linewidth=0.9)
    ax.set_xlim(0, max(values) * 1.18)
    for lbl in ax.get_yticklabels():
        lbl.set_color(INK if (highlight and lbl.get_text() == highlight) else MUTED)
        lbl.set_fontsize(12.5)
    fig.subplots_adjust(left=0.30, right=0.96, top=0.80, bottom=0.13)
    _frame(fig, title, subtitle, source)
    fig.savefig(path, facecolor=BG); plt.close(fig)
    return path

def lines(path, x, series, title, subtitle="", source="", unit="", marks=None):
    """Динамика во времени. marks — [(x, 'подпись')] для отметок событий:
    запусков, раскаток, публикаций ЦБ."""
    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=200)
    for i, (name, ys) in enumerate(series.items()):
        c = SERIES[i % len(SERIES)]
        ax.plot(x, ys, color=c, linewidth=2.6, zorder=3, solid_capstyle="round")
        ax.scatter([x[-1]], [ys[-1]], color=c, s=46, zorder=4)
        ax.annotate(f" {name}", (x[-1], ys[-1]), color=c, fontsize=11.5,
                    fontname=FONT, va="center", fontweight="bold")
    if marks:
        for mx, label in marks:
            ax.axvline(mx, color=WARM, linewidth=1.2, linestyle="--", alpha=0.75, zorder=2)
            ax.text(mx, ax.get_ylim()[1], f" {label}", color=WARM, fontsize=10,
                    fontname=FONT, va="top", rotation=90)
    _style(ax, fig)
    if unit:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}{unit}"))
    fig.subplots_adjust(left=0.09, right=0.86, top=0.78, bottom=0.14)
    _frame(fig, title, subtitle, source)
    fig.savefig(path, facecolor=BG); plt.close(fig)
    return path

def _head(h):
    """Заголовок колонки в верхнем регистре, но аббревиатуры не ломаем:
    «CoR» не должен становиться «COR»."""
    return h if any(c.isupper() for c in h) and any(c.islower() for c in h) else h.upper()

def table(path, headers, rows, title, subtitle="", source="", highlight_row=None):
    """Таблица-карточка для сравнения условий.

    Правила вёрстки (после косяка 11.09.2026, когда заголовок в две строки наехал
    на подзаголовок, а «Газпромбанк» превратился в «Газпромба…»):
    1. Ничего не режем многоточием — длинная ячейка переносится на вторую строку,
       и строка таблицы становится выше.
    2. Ширины колонок считаются по РЕАЛЬНОЙ ширине текста (TextPath), а не по числу знаков.
    3. Шапка (заголовок + подзаголовок) сначала измеряется, и таблица начинается ПОД ней —
       высота картинки подстраивается, а не наоборот.
    """
    import textwrap
    def _cell(c):
        if c is None:
            return ""
        return _re.sub(r"(?<=\d)\.(?=\d)", ",", str(c))
    headers = [str(h) for h in headers[:6]]
    ncol = len(headers)
    rows = [[_cell(c) for c in r[:ncol]] for r in rows[:12]]
    for r in rows:
        r += [""] * (ncol - len(r))

    W_IN = 10.0                                   # ширина фигуры, дюймы
    PAD = 0.055                                   # поля слева/справа, доля ширины
    AX_PT = W_IN * 72 * (1 - 2 * PAD)             # ширина области таблицы, пункты
    GAP = 14.0                                    # воздух между колонками, пункты
    FS = 11.0 if ncol <= 4 else (10.0 if ncol == 5 else 9.0)
    HFS = FS - 1.2
    _fp = FontProperties(family=FONT)

    def _w(text, fs):
        if not text:
            return 0.0
        try:
            return TextPath((0, 0), text, size=fs, prop=_fp).get_extents().width
        except Exception:
            return len(text) * fs * 0.55

    # 1. Естественная ширина каждой колонки — по самому широкому содержимому.
    nat = [max([_w(_head(headers[j]), HFS)] + [_w(r[j], FS) for r in rows]) for j in range(ncol)]
    avail = AX_PT - GAP * (ncol - 1)
    if sum(nat) <= avail:
        colpt = list(nat)
        # лишнее место раздаём пропорционально, чтобы таблица заполняла ширину
        extra = avail - sum(nat)
        colpt = [c + extra * c / sum(nat) for c in colpt]
    else:
        # 2. Не влезает: сжимаем широкие колонки, узкие не трогаем, содержимое переносим.
        min_pt = [min(n, max(_w(_head(headers[j]), HFS), 0.16 * avail)) for j, n in enumerate(nat)]
        colpt = list(nat)
        while sum(colpt) > avail:
            j = max(range(ncol), key=lambda k: colpt[k] - min_pt[k])
            if colpt[j] - min_pt[j] <= 1:
                break
            colpt[j] = max(min_pt[j], colpt[j] - (sum(colpt) - avail))
        scale = min(1.0, avail / sum(colpt))
        colpt = [c * scale for c in colpt]

    def wrap(text, j, fs):
        """Перенос по словам под ширину колонки; максимум 3 строки, без многоточий."""
        if _w(text, fs) <= colpt[j]:
            return [text]
        words = text.split(); lines = []; cur = ""
        for wd in words:
            cand = (cur + " " + wd).strip()
            if _w(cand, fs) <= colpt[j] or not cur:
                cur = cand
            else:
                lines.append(cur); cur = wd
        if cur:
            lines.append(cur)
        return lines[:3]

    head_lines = [wrap(_head(h), j, HFS) for j, h in enumerate(headers)]
    body_lines = [[wrap(c, j, FS) for j, c in enumerate(r)] for r in rows]

    # 3. Геометрия по вертикали — в дюймах, потом в доли фигуры.
    LINE = FS * 1.35 / 72                          # высота строки текста, дюймы
    ROW_PAD = 0.22                                 # воздух внутри строки, дюймы
    row_h = [max(len(c) for c in r) * LINE + ROW_PAD for r in body_lines]
    head_h = max(len(c) for c in head_lines) * (HFS * 1.35 / 72) + ROW_PAD
    title_wr = textwrap.fill(title, 54)
    n_t = title_wr.count("\n") + 1
    tfs = 19 if n_t == 1 else 17
    sub_wr = textwrap.fill(subtitle, 92) if subtitle else ""
    n_s = sub_wr.count("\n") + 1 if sub_wr else 0
    top_pad = 0.30
    title_h = n_t * tfs * 1.2 / 72
    sub_h = (0.10 + n_s * 12 * 1.3 / 72) if n_s else 0
    header_block = top_pad + title_h + sub_h + 0.28
    bottom_block = 0.55
    table_h = head_h + sum(row_h) + 0.12
    H = header_block + table_h + bottom_block

    fig = plt.figure(figsize=(W_IN, H), dpi=200)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([PAD, bottom_block / H, 1 - 2 * PAD, table_h / H])
    ax.axis("off"); ax.set_xlim(0, AX_PT); ax.set_ylim(0, table_h)

    # шапка
    fig.text(PAD, 1 - top_pad / H, title_wr, color=INK, fontsize=tfs, fontweight="bold",
             fontname=FONT, va="top", linespacing=1.15)
    if sub_wr:
        fig.text(PAD, 1 - (top_pad + title_h + 0.10) / H, sub_wr, color=MUTED, fontsize=12,
                 fontname=FONT, va="top", linespacing=1.25)
    if source:
        fig.text(PAD, 0.22 / H, source, color=MUTED, fontsize=9.5, fontname=FONT)
    fig.text(1 - PAD, 0.22 / H, "Кредитный радар", color=GRID, fontsize=9.5, fontname=FONT, ha="right")

    # заголовки колонок
    x0 = [sum(colpt[:j]) + GAP * j for j in range(ncol)]
    y = table_h - 0.06
    for j, lines in enumerate(head_lines):
        ax.text(x0[j], y, "\n".join(lines), color=MUTED, fontsize=HFS, fontname=FONT,
                fontweight="bold", va="top", linespacing=1.3)
    y -= head_h
    ax.plot([0, AX_PT], [y, y], color=GRID, linewidth=1.2)
    # строки
    for i, lines_row in enumerate(body_lines):
        rh = row_h[i]
        hl = (highlight_row is not None and i == highlight_row)
        if hl:
            ax.add_patch(plt.Rectangle((0, y - rh), AX_PT, rh, facecolor=PANEL, edgecolor="none", zorder=0))
        for j, lines in enumerate(lines_row):
            ax.text(x0[j], y - ROW_PAD / 2, "\n".join(lines),
                    color=ACC if hl and j == 0 else (INK if hl else MUTED),
                    fontsize=FS, fontname=FONT, va="top", linespacing=1.3,
                    fontweight="bold" if hl else "normal")
        y -= rh
    fig.savefig(path, facecolor=BG); plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# Диаграмма рассеяния со статистикой: связь двух показателей по банкам.
# Размер точки — третья величина (портфель, число отзывов), подписи — банки,
# линия — МНК, в углу — n, r Пирсона, R², p-value, ρ Спирмена. Статистика
# считается здесь, а не моделью: модели цифры статистики не доверяем.
# ---------------------------------------------------------------------------
import math
import numpy as _np

def _betacf(a, b, x, itmax=200, eps=3e-12):
    # Продолженная дробь для неполной бета-функции (Numerical Recipes).
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300); h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d; d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / (c if abs(c) > 1e-300 else 1e-300); h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d; d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / (c if abs(c) > 1e-300 else 1e-300); dl = d * c; h *= dl
        if abs(dl - 1.0) < eps: break
    return h

def _betainc(a, b, x):
    # Регуляризованная неполная бета I_x(a, b).
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1 - x) / b

def _p_from_r(r, n):
    # Двусторонний p для r Пирсона при H0: r=0 (t-распределение с n-2 ст. св.).
    if n < 3 or abs(r) >= 1: return float("nan") if n < 3 else 0.0
    df = n - 2; t = r * math.sqrt(df / (1 - r * r))
    return _betainc(df / 2.0, 0.5, df / (df + t * t))

def _ranks(v):
    order = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]: j += 1
        for k in range(i, j + 1): r[order[k]] = (i + j) / 2.0 + 1
        i = j + 1
    return r

def scatter_stats(xs, ys):
    n = len(xs)
    if n < 3: return {"n": n}
    x, y = _np.array(xs, float), _np.array(ys, float)
    r = float(_np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else float("nan")
    slope, intercept = _np.polyfit(x, y, 1)
    rho = float(_np.corrcoef(_ranks(xs), _ranks(ys))[0, 1])
    return {"n": n, "r": r, "r2": r * r, "p": _p_from_r(r, n), "rho": rho,
            "p_rho": _p_from_r(rho, n), "slope": float(slope), "intercept": float(intercept)}

def scatter(path, points, title, subtitle="", source="", xlabel="", ylabel="",
            size_label="", highlight=None, x_unit="", y_unit=""):
    """points — [{label, x, y, size}], size — величина для площади точки (может быть None)."""
    pts = [p for p in points if p.get("x") is not None and p.get("y") is not None]
    xs = [float(p["x"]) for p in pts]; ys = [float(p["y"]) for p in pts]
    sizes = [p.get("size") for p in pts]
    have_size = all(s is not None for s in sizes) and len(sizes) > 0
    st = scatter_stats(xs, ys)
    fig, ax = plt.subplots(figsize=(10, 6.6), dpi=200)
    _style(ax, fig); ax.grid(axis="x", color=GRID, linewidth=0.9, alpha=0.9)
    xr = (max(xs) - min(xs)) or 1.0; yr = (max(ys) - min(ys)) or 1.0
    if have_size:
        smax = max(float(s) for s in sizes) or 1.0
        area = [160 + 1100 * float(s) / smax for s in sizes]
    else:
        area = [380] * len(pts)
    if st.get("n", 0) >= 3 and not math.isnan(st.get("r", float("nan"))):
        gx = _np.linspace(min(xs) - xr * 0.05, max(xs) + xr * 0.05, 50)
        ax.plot(gx, st["slope"] * gx + st["intercept"], color=WARM, linewidth=1.4,
                linestyle="--", alpha=0.7, zorder=2)
    cols = [WARM if (highlight and p["label"] == highlight) else ACC for p in pts]
    ax.scatter(xs, ys, s=area, c=cols, alpha=0.5, edgecolors=INK, linewidths=0.8, zorder=3)
    # Подписи: справа от точки; у правого края — слева; при близких точках — разводим по вертикали.
    order = sorted(range(len(pts)), key=lambda k: (round((xs[k] - min(xs)) / xr, 1), ys[k]))
    placed = []
    for k in order:
        fx, fy = (xs[k] - min(xs)) / xr, (ys[k] - min(ys)) / yr
        right = fx > 0.80
        dy = 9
        for (px, py, pdy) in placed:
            if abs(px - fx) < 0.09 and abs(py - fy) < 0.10:
                dy = 20 if fy >= py else -20          # верхняя точка — подпись выше, нижняя — ниже
        radius = math.sqrt(area[k] / math.pi) * 0.9
        ax.annotate(pts[k]["label"], (xs[k], ys[k]),
                    xytext=((-radius - 4) if right else (radius + 4), dy), textcoords="offset points",
                    ha="right" if right else "left", va="center",
                    color=INK, fontsize=11.5, fontname=FONT, zorder=5)
        placed.append((fx, fy, dy))
    def _lab(l, u):
        return l if (not u or u in l) else f"{l}, {u}"
    ax.set_xlabel(_lab(xlabel, x_unit), color=MUTED, fontsize=11.5, fontname=FONT)
    ax.set_ylabel(_lab(ylabel, y_unit), color=MUTED, fontsize=11.5, fontname=FONT)
    ax.set_xlim(min(xs) - xr * 0.16, max(xs) + xr * 0.16); ax.set_ylim(min(ys) - yr * 0.20, max(ys) + yr * 0.22)
    foot = []
    if st.get("n", 0) >= 3 and not math.isnan(st.get("r", float("nan"))):
        foot.append(f"n = {st['n']}   r = {st['r']:+.2f}   R² = {st['r2']:.2f}   p = {st['p']:.2f}   "
                    f"ρ Спирмена = {st['rho']:+.2f} (p = {st['p_rho']:.2f})")
    if have_size and size_label:
        foot.append(f"площадь точки — {size_label}")
    if foot:
        fig.text(0.945, 0.075, "   ·   ".join(foot), color=MUTED, fontsize=10, fontname=FONT, ha="right")
    fig.subplots_adjust(left=0.09, right=0.96, top=0.80, bottom=0.17)
    _frame(fig, title, subtitle, source)
    fig.savefig(path, facecolor=BG); plt.close(fig)
    return path, st
