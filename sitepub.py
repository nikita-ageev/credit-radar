"""Публикация выпусков Радара на сайт: ageev.dev/credit-radar/<дата>/.

Вызывается из radar.daily после публикации сводки (и разбора, если он был). Ничего не генерирует
моделью: берёт тот же Telegram-HTML, что ушёл в канал, и раскладывает его в страницу выпуска.
Держит архив (archive/), ленту (issues.json, feed.xml) и sitemap выпусков (sitemap-issues.xml),
после заливки пингует IndexNow. Всё состояние — state/site_issues.json, так что любую страницу
можно пересобрать командой `python sitepub.py rebuild`.

Команды:  python sitepub.py backfill [--dry]   собрать выпуски из evals/days/*.json
          python sitepub.py rebuild  [--dry]   пересобрать все страницы из состояния
          python sitepub.py publish ДАТА       опубликовать день из evals/days/ДАТА.json
"""
import os, re, sys, json, html as _html, subprocess, urllib.request, urllib.parse, datetime, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state", "site_issues.json")
OUT = os.path.join(HERE, "out", "site")
DAYS = os.path.join(HERE, "evals", "days")
SITE_DIR = os.environ.get("RADAR_SITE_DIR", "/public_html")
BASE = "https://ageev.dev"
SECTION = "/credit-radar"
CHANNEL = "https://t.me/rcradar"
PUBLISHED = os.path.join(HERE, "state", "published.json")
INDEXNOW_KEY = os.environ.get("RADAR_INDEXNOW_KEY", "2a879c1e5ffb2bcc785eeaea389ea5ba")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

def log(msg):
    print(f"[site] {msg}", flush=True)

# ---------------- состояние ----------------
def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}

def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)

def issues_sorted(st):
    return sorted(st.values(), key=lambda x: x["date"], reverse=True)

# ---------------- текст ----------------
_TAG_OK = re.compile(r"</?(b|i|u|s)>|<a href=\"[^\"]*\">|</a>")

def _sanitize(line):
    """Пропускаем только теги Telegram-HTML, остальное экранируем; ссылки открываются в новой вкладке."""
    out, pos = [], 0
    for m in _TAG_OK.finditer(line):
        out.append(_html.escape(line[pos:m.start()], quote=False))
        t = m.group(0)
        if t.startswith("<a "):
            t = t[:-1] + ' target="_blank" rel="noopener">'
        out.append(t)
        pos = m.end()
    out.append(_html.escape(line[pos:], quote=False))
    s = "".join(out)
    return s.replace("&amp;amp;", "&amp;").replace("&amp;quot;", "&quot;").replace("&amp;lt;", "&lt;").replace("&amp;gt;", "&gt;").replace("&amp;#", "&#")

def strip_tags(s):
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

def caption_body(caption):
    """Подпись к графику без первой жирной строки (это заголовок) и без служебной статистики."""
    lines = [l for l in (caption or "").split("\n")]
    if lines and re.match(r"^\s*<b>.+</b>\s*$", lines[0]):
        lines = lines[1:]
    body = " ".join(l.strip() for l in lines if l.strip() and not l.strip().startswith("<i>"))
    return strip_tags(body)

def clip(s, n=160):
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"

def digest_to_html(text):
    """Telegram-HTML сводки → секции страницы. Возвращает (html, первая содержательная строка)."""
    parts, lst, first, bullets = [], [], "", []
    def close():
        nonlocal lst
        if lst:
            parts.append("<ul class=\"lines\">" + "".join(f"<li>{x}</li>" for x in lst) + "</ul>"); lst = []
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line:
            close(); continue
        m = re.match(r"^<b>(.+)</b>$", line)
        if m:
            close()
            head = m.group(1).replace("у конкурентов", "у банков")
            if re.match(r"^(Сводка|Вечерний выпуск) за \d", head):
                continue
            parts.append(f"<h2>{_sanitize(head)}</h2>"); continue
        if line.startswith("•"):
            item = _sanitize(line.lstrip("•").strip())
            if not first: first = strip_tags(item)
            bullets.append(strip_tags(item)); lst.append(item); continue
        close()
        if not first and not re.match(r"^(Новых новостей|Изменений|Пусто)", line): first = strip_tags(line)
        parts.append(f"<p>{_sanitize(line)}</p>")
    close()
    return "\n".join(parts), first, bullets

def essay_to_html(essay):
    """Полный разбор из канала → статья: абзацы, цитаты, статистика, источники."""
    text = (essay.get("text") or "").strip()
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if blocks and re.match(r"^<b>.+</b>$", blocks[0].split("\n")[0].strip()) and len(blocks[0].split("\n")) == 1:
        blocks = blocks[1:]  # заголовок показываем отдельно
    out = []
    for b in blocks:
        lines = [l.strip() for l in b.split("\n") if l.strip()]
        if lines and re.match(r"^<i>Дословно", lines[0]):
            out.append("<blockquote>" + "".join(f"<p>{_sanitize(l)}</p>" for l in lines[1:]) + "</blockquote>")
        elif lines and lines[0].startswith("<i>Статистика по графику"):
            out.append(f"<p class=\"stat\">{_sanitize(lines[0])}</p>")
        elif lines and lines[0].startswith("Источники:"):
            out.append(f"<p class=\"sources\">{_sanitize(lines[0])}</p>")
        else:
            out.append("".join(f"<p>{_sanitize(l)}</p>" for l in lines))
    return "\n".join(out)

def is_evening(d):
    return str(d).endswith("-evening")

def date_ru(d):
    y, m, dd = (int(x) for x in d[:10].split("-"))
    return f"{dd} {MONTHS[m-1]} {y}" + (", вечер" if is_evening(d) else "")

def date_short(d):
    y, m, dd = d[:10].split("-")
    return f"{dd}.{m}.{y}" + (" (вечер)" if is_evening(d) else "")

# ---------------- шаблоны ----------------
CSS = """
:root{color-scheme:dark;--bg:#020A11;--bg2:#04141F;--ink:#EAF2F8;--ink2:#93AABB;--ink3:#5D7789;--line:#0E2637;--hair:#0A1F2E;--accent:#5E90E8;--rule:#1E445F;--glow:rgba(94,144,232,.10);--card:#061826}
html{background:var(--bg)}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Golos Text","Helvetica Neue",Arial,system-ui,sans-serif;font-size:17px;line-height:1.65;-webkit-font-smoothing:antialiased;overflow-x:hidden}
.bgfx{position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(1100px 640px at 80% -6%,var(--glow),transparent 60%),linear-gradient(180deg,var(--bg2) 0%,var(--bg) 46%)}
.page{position:relative;z-index:2}.wrap{width:100%;max-width:760px;margin:0 auto;padding:0 24px;box-sizing:border-box}
a{color:var(--accent);text-underline-offset:3px;text-decoration-thickness:1px}
.topnav{position:sticky;top:0;z-index:5;background:rgba(2,10,17,.72);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--hair)}
.topnav .wrap{display:flex;justify-content:space-between;align-items:center;height:56px;font-size:14px;max-width:980px}
.topnav a{color:var(--ink2);text-decoration:none;margin-left:20px;white-space:nowrap}.topnav a.brand{margin-left:0;color:var(--ink);font-weight:600}.topnav a.cur{color:var(--ink)}
.eyebrow{font-size:11px;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:var(--ink3)}
h1{font-family:Literata,Georgia,serif;font-weight:600;font-size:clamp(30px,6vw,46px);line-height:1.1;letter-spacing:-.02em;margin:12px 0 0;text-wrap:balance}
.lede{font-size:clamp(15px,3.6vw,18px);color:var(--ink2);margin:18px 0 0;max-width:58ch}
header.issue{padding:64px 0 34px;border-bottom:1px solid var(--line)}
.meta{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--ink3);margin-top:22px}
section.block{padding:38px 0 8px}
section.block>.eyebrow{margin-bottom:6px}
h2{font-family:Literata,Georgia,serif;font-weight:600;font-size:clamp(22px,4.6vw,30px);line-height:1.2;letter-spacing:-.015em;margin:26px 0 12px}
h2:first-of-type{margin-top:8px}
p{margin:0 0 14px}
.essay p{font-size:17.5px}
ul.lines{list-style:none;margin:0 0 18px;padding:0}
ul.lines li{position:relative;padding:9px 0 9px 22px;border-top:1px solid var(--hair);color:var(--ink);font-size:16.5px}
ul.lines li:first-child{border-top:0}
ul.lines li::before{content:"";position:absolute;left:2px;top:19px;width:6px;height:6px;border-radius:50%;background:var(--accent)}
figure{margin:24px 0 26px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px}
figure img{width:100%;height:auto;display:block;border-radius:6px}
figcaption{font-size:13.5px;color:var(--ink2);padding:12px 10px 6px;line-height:1.5}
blockquote{margin:18px 0 22px;padding:4px 0 4px 18px;border-left:2px solid var(--rule);color:var(--ink2);font-size:15.5px}
blockquote p{margin:0 0 8px}
.stat,.sources{font-size:14px;color:var(--ink3)}
.cta{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:34px 0 0;padding-top:26px;border-top:1px solid var(--line)}
.btn{display:inline-block;padding:12px 20px;border-radius:8px;font-weight:600;font-size:15px;text-decoration:none;border:1px solid var(--rule);color:var(--ink)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#04121F}
.cta span{font-size:13px;color:var(--ink3)}
nav.pn{display:flex;justify-content:space-between;gap:20px;margin:40px 0 0;padding-top:20px;border-top:1px solid var(--line);font-size:14px}
nav.pn a{text-decoration:none;color:var(--ink2)}nav.pn a:hover{color:var(--ink)}nav.pn small{display:block;color:var(--ink3);font-size:12px}
footer{font-size:13px;color:var(--ink3);margin:54px 0 40px;line-height:1.6}
.arch .month{font-family:Literata,Georgia,serif;font-size:22px;margin:38px 0 8px;color:var(--ink)}
.arch .row{display:grid;grid-template-columns:96px 1fr;gap:16px;padding:16px 0;border-top:1px solid var(--hair);align-items:baseline}
.arch .row:first-of-type{border-top:0}
.arch .d{font-family:Literata,Georgia,serif;font-size:19px;color:var(--ink2);font-variant-numeric:tabular-nums}
.arch .t{font-weight:600;font-size:16.5px;text-decoration:none;color:var(--ink)}
.arch .t:hover{color:var(--accent)}
.arch .s{font-size:14.5px;color:var(--ink2);margin-top:4px}
.tag{display:inline-block;font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);border:1px solid var(--rule);border-radius:4px;padding:2px 7px;margin-left:8px;vertical-align:middle}
@media (max-width:640px){.topnav .wrap{gap:14px;font-size:13px}.topnav a.brand{white-space:nowrap;flex:none}.topnav .wrap>span{display:flex;gap:14px;overflow-x:auto;white-space:nowrap;scrollbar-width:none;-ms-overflow-style:none;max-width:70vw;-webkit-mask-image:linear-gradient(90deg,#000 88%,transparent);mask-image:linear-gradient(90deg,#000 88%,transparent);padding-right:24px}.topnav .wrap>span::-webkit-scrollbar{display:none}.topnav .wrap>span a{margin-left:0;flex:none}.arch .row{grid-template-columns:1fr;gap:4px}header.issue{padding:44px 0 26px}}
"""

HEAD = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta name="author" content="Никита Агеев">
<meta name="theme-color" content="#020A11">
<meta name="color-scheme" content="dark">
<link rel="canonical" href="{url}">
<meta property="og:type" content="{ogtype}">
<meta property="og:site_name" content="Кредитный радар">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{image}">
<meta property="og:locale" content="ru_RU">
<meta name="twitter:card" content="summary_large_image">
<link rel="alternate" type="application/rss+xml" title="Кредитный радар — выпуски" href="{base}{section}/feed.xml">
<link rel="icon" href="/favicon.ico?v=4" sizes="any">
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v=4">
<link rel="apple-touch-icon" href="/apple-touch-icon.png?v=4">
<link rel="preload" href="/fonts/literata-cyrillic.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/fonts/golos-text-cyrillic.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/fonts/fonts.css?v=240757">
<style>{css}</style>
{jsonld}
</head>
<body>
<script>
// Открывать страницу сверху, если в адресе нет якоря. iOS Safari восстанавливает прокрутку после load
// и scroll-snap дотягивает до «параллельного» экрана, поэтому: snap выключен до первой отрисовки,
// прокрутка в ноль повторяется в первые полсекунды, затем snap возвращается.
(function(){{
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  if (location.hash) return;
  var root = document.documentElement; root.style.scrollSnapType = 'none';
  function top(){{ window.scrollTo(0, 0); }}
  top();
  var n = 0, t = setInterval(function(){{ top(); if (++n >= 8) {{ clearInterval(t); root.style.scrollSnapType = ''; }} }}, 60);
  window.addEventListener('pageshow', function(e){{ if (e.persisted && !location.hash) {{ root.style.scrollSnapType='none'; top(); setTimeout(function(){{ top(); root.style.scrollSnapType=''; }}, 200); }} }});
}})();
</script>
<div class="bgfx" aria-hidden="true"></div>
<div class="page">
<nav class="topnav" aria-label="Разделы сайта"><div class="wrap">
  <a class="brand" href="/">Никита Агеев</a>
  <span><a href="/credit-radar/" class="{cur_radar}">Кредитный радар</a><a href="/jarvis/">Джарвис</a><a href="/#contact">Контакты</a></span>
</div></nav>
"""

FOOT = """
<footer class="wrap">Кредитный радар · автоматический мониторинг условий розничного кредитования у 12 банков · данные с сайтов банков, ссылка на источник в каждой строке · <a href="/credit-radar/">о проекте</a> · <a href="/">Никита Агеев</a></footer>
</div>
</body>
</html>
"""

def _jsonld_issue(iss, url, image):
    d = iss["date"]
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "NewsArticle", "headline": iss["title"][:110], "description": iss["summary"],
             "datePublished": f"{d[:10]}T{'20' if is_evening(d) else '09'}:00:00+03:00", "dateModified": f"{d[:10]}T{'20' if is_evening(d) else '09'}:00:00+03:00",
             "inLanguage": "ru-RU", "mainEntityOfPage": url, "image": [image],
             "author": {"@type": "Person", "name": "Никита Агеев", "url": BASE + "/"},
             "publisher": {"@type": "Organization", "name": "Кредитный радар", "url": BASE + SECTION + "/",
                           "logo": {"@type": "ImageObject", "url": BASE + "/apple-touch-icon.png"}},
             "isPartOf": {"@type": "Blog", "name": "Кредитный радар", "url": BASE + SECTION + "/archive/"}},
            {"@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Кредитный радар", "item": BASE + SECTION + "/"},
                {"@type": "ListItem", "position": 2, "name": "Выпуски", "item": BASE + SECTION + "/archive/"},
                {"@type": "ListItem", "position": 3, "name": date_ru(d), "item": url}]}]}
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>"

def render_issue(iss, prev_iss=None, next_iss=None):
    d = iss["date"]; url = f"{BASE}{SECTION}/{d}/"
    essay = iss.get("essay")
    image = f"{url}chart.png" if essay and essay.get("chart") else f"{BASE}{SECTION}/og.png?v=2"
    digest_html, _, _ = digest_to_html(iss.get("digest", ""))
    # заголовок и описание — под поисковые запросы «условия кредитных карт / кредитов банков», не только под канал
    title_page = f"Кредитные карты и кредиты банков, {date_short(d)} — {clip(iss['title'], 70).rstrip('.')} — Кредитный радар"
    desc_page = clip("Изменения условий кредитных карт, рассрочки, кредитов наличными и автокредитов у 12 банков России за день. "
                     + iss["summary"], 300)
    head = HEAD.format(title=_html.escape(title_page, quote=True), desc=_html.escape(desc_page, quote=True), url=url,
                       ogtype="article", image=image, base=BASE, section=SECTION, css=CSS,
                       jsonld=_jsonld_issue(iss, url, image), cur_radar="cur", cur_arch="")
    body = [head, '<div class="wrap">', '<header class="issue">',
            f'<div class="eyebrow">Кредитный радар · выпуск</div>',
            f'<h1>{date_ru(d)}</h1>',
            f'<p class="lede">{_html.escape(iss["summary"])}</p>',
            '<div class="meta"><span>Выходит каждый день в 09:00 по Москве</span><span>12 банков · 34 страницы условий · открытые источники</span></div>',
            '</header>']
    if essay:
        body.append('<section class="block essay"><div class="eyebrow">Разбор дня</div>')
        body.append(f'<h2>{_sanitize(essay.get("title", ""))}</h2>')
        if essay.get("chart"):
            cap = caption_body(essay.get("caption", ""))
            body.append(f'<figure><a href="chart.png" target="_blank" rel="noopener"><img src="chart.png" alt="{_html.escape(strip_tags(essay.get("title", "")), quote=True)}" width="1800" height="1280" loading="lazy"></a>'
                        + (f'<figcaption>{_html.escape(clip(cap, 320))}</figcaption>' if cap else '') + '</figure>')
        body.append(essay_to_html(essay))
        body.append('</section>')
    body.append('<section class="block"><div class="eyebrow">Сводка дня</div>')
    body.append(digest_html or "<p>Изменений за сутки не зафиксировано.</p>")
    body.append('</section>')
    ids = iss.get("tg_ids") or []
    post_url = f"{CHANNEL}/{ids[0]}" if ids else CHANNEL
    body.append(f'<div class="cta"><a class="btn primary" href="{post_url}" target="_blank" rel="noopener">{"Этот выпуск в Telegram" if ids else "Канал в Telegram"}</a>'
                f'<a class="btn" href="/credit-radar/archive/">Все выпуски</a><span>Сводка и разбор выходят в канале @rcradar в 09:00, на сайте появляются в тот же час.</span></div>')
    pn = ['<nav class="pn" aria-label="Соседние выпуски">']
    pn.append(f'<a href="/credit-radar/{prev_iss["date"]}/"><small>Предыдущий</small>← {date_ru(prev_iss["date"])}</a>' if prev_iss else '<span></span>')
    pn.append(f'<a href="/credit-radar/{next_iss["date"]}/" style="text-align:right"><small>Следующий</small>{date_ru(next_iss["date"])} →</a>' if next_iss else '<span></span>')
    pn.append('</nav>')
    body += pn
    body.append('</div>')
    body.append(FOOT)
    return "\n".join(body)

def render_archive(issues):
    url = f"{BASE}{SECTION}/archive/"
    data = {"@context": "https://schema.org", "@type": "CollectionPage", "name": "Кредитный радар — выпуски", "url": url,
            "hasPart": [{"@type": "NewsArticle", "headline": i["title"][:110], "url": f"{BASE}{SECTION}/{i['date']}/",
                         "datePublished": f"{i['date'][:10]}T{'20' if is_evening(i['date']) else '09'}:00:00+03:00"} for i in issues[:50]]}
    head = HEAD.format(title="Все выпуски — Кредитный радар", desc="Архив ежедневных выпусков Кредитного радара: изменения условий кредитных карт, рассрочки, кредитов наличными и автокредитов у 12 банков, решения Банка России, разборы.",
                       url=url, ogtype="website", image=f"{BASE}{SECTION}/og.png?v=2", base=BASE, section=SECTION, css=CSS,
                       jsonld='<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>",
                       cur_radar="cur", cur_arch="")
    body = [head, '<div class="wrap arch">', '<header class="issue">', '<div class="eyebrow">Кредитный радар</div>', '<h1>Выпуски</h1>',
            '<p class="lede">Каждое утро: что изменилось в условиях розничного кредитования у 12 банков, решения регулятора и, когда есть повод, разбор одного изменения с расчётом. Вечером — новости дня со ссылками на источники.</p>',
            f'<div class="meta"><span>{len(issues)} выпусков</span><span><a href="/credit-radar/feed.xml">RSS</a></span><span><a href="{CHANNEL}" target="_blank" rel="noopener">Канал @rcradar</a></span></div>', '</header>']
    cur = None
    for i in issues:
        y, m, _ = i["date"][:10].split("-")
        key = f"{MONTHS_NOM[int(m)-1]} {y}"
        if key != cur:
            body.append(f'<div class="month">{key}</div>'); cur = key
        tag = ('<span class="tag">разбор</span>' if i.get("essay") else '') + ('<span class="tag">вечер</span>' if is_evening(i["date"]) else '')
        body.append(f'<div class="row"><div class="d">{i["date"][8:10]}.{m}</div><div><a class="t" href="/credit-radar/{i["date"]}/">{_html.escape(i["title"])}</a>{tag}<div class="s">{_html.escape(i["summary"])}</div></div></div>')
    body.append('</div>'); body.append(FOOT)
    return "\n".join(body)

def render_sitemap(issues):
    rows = [f"  <url><loc>{BASE}{SECTION}/archive/</loc><lastmod>{issues[0]['date'][:10] if issues else datetime.date.today().isoformat()}</lastmod><changefreq>daily</changefreq><priority>0.8</priority></url>"]
    for i in issues:
        rows.append(f"  <url><loc>{BASE}{SECTION}/{i['date']}/</loc><lastmod>{i['date'][:10]}</lastmod><changefreq>never</changefreq><priority>0.6</priority></url>")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(rows) + "\n</urlset>\n"

def render_feed(issues):
    items = []
    for i in issues[:30]:
        link = f"{BASE}{SECTION}/{i['date']}/"
        dt = datetime.datetime.strptime(i["date"][:10], "%Y-%m-%d").replace(hour=20 if is_evening(i["date"]) else 9, tzinfo=datetime.timezone(datetime.timedelta(hours=3)))
        items.append(f"<item><title>{_html.escape(i['title'])}</title><link>{link}</link><guid>{link}</guid>"
                     f"<pubDate>{dt.strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate><description>{_html.escape(i['summary'])}</description></item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>Кредитный радар</title>'
            f'<link>{BASE}{SECTION}/archive/</link><description>Ежедневный мониторинг условий розничного кредитования у 12 банков</description><language>ru</language>'
            + "".join(items) + "</channel></rss>\n")

def issues_json(issues):
    return json.dumps([{"date": i["date"], "url": f"{SECTION}/{i['date']}/", "title": i["title"], "summary": i["summary"],
                        "essay": bool(i.get("essay"))} for i in issues[:60]], ensure_ascii=False)

# ---------------- заливка ----------------
SITE_LOCAL = os.environ.get("RADAR_SITE_LOCAL", "")        # каталог nginx на этом же сервере (после переезда)
SITE_FTP = os.environ.get("RADAR_SITE_FTP", "on") != "off"  # заливать ли ещё и на старый хостинг по FTP

def _local_put(local, remote_rel):
    dst = os.path.join(SITE_LOCAL, remote_rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(local, dst); os.chmod(dst, 0o644)
    try:
        import pwd, grp
        os.chown(dst, pwd.getpwnam("www-data").pw_uid, grp.getgrnam("www-data").gr_gid)
        os.chown(os.path.dirname(dst), pwd.getpwnam("www-data").pw_uid, grp.getgrnam("www-data").gr_gid)
    except Exception:
        pass
    return True

def _ftp_put(local, remote_rel):
    """Кладёт файл на сайт: локально в каталог nginx (если задан) и/или по FTP на хостинг."""
    ok = True
    if SITE_LOCAL:
        try: ok &= _local_put(local, remote_rel)
        except Exception as e: log(f"локальная запись не удалась: {remote_rel}: {e}"); ok = False
        if not SITE_FTP: return ok
    host, user, pw = (os.environ.get(k, "") for k in ("RADAR_FTP_HOST", "RADAR_FTP_USER", "RADAR_FTP_PASS"))
    proto = os.environ.get("RADAR_FTP_PROTO", "ftp")
    if not host:
        if SITE_LOCAL: return ok
        log(f"нет RADAR_FTP_HOST, не залито: {remote_rel}"); return False
    r = subprocess.run(["curl", "-sS", "--fail", "--max-time", "120", "--ftp-create-dirs", "-T", local,
                        "-u", f"{user}:{pw}", f"{proto}://{host}{SITE_DIR}/{remote_rel}"], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"заливка не удалась: {remote_rel}: {r.stderr.strip()[:120]}")
    return ok and r.returncode == 0

def _write(rel, content):
    p = os.path.join(OUT, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f: f.write(content)
    return p

def indexnow(urls):
    body = json.dumps({"host": "ageev.dev", "key": INDEXNOW_KEY, "keyLocation": f"{BASE}/{INDEXNOW_KEY}.txt", "urlList": urls}).encode()
    for ep in ("https://api.indexnow.org/indexnow", "https://yandex.com/indexnow"):
        try:
            req = urllib.request.Request(ep, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=20) as r: log(f"IndexNow {ep.split('/')[2]}: {r.status}")
        except Exception as e:
            log(f"IndexNow {ep.split('/')[2]}: {type(e).__name__}")

def tg_ids(date):
    """Номера сообщений канала, опубликованных в этот день (из журнала публикаций Радара)."""
    try:
        rows = json.load(open(PUBLISHED, encoding="utf-8"))
    except Exception:
        return []
    ids = []
    for r in rows:
        ts = str(r.get("ts", ""))
        if ts[:10] != date[:10]:
            continue
        hour = int(ts[11:13]) if len(ts) >= 13 and ts[11:13].isdigit() else 0
        if is_evening(date) != (hour >= 15):
            continue
        ids += [int(i) for i in (r.get("ids") or []) if str(i).isdigit()]
    return sorted(set(ids))

def _chart_path(date):
    p = os.path.join(HERE, "out", f"chart_daily_{date.replace('-', '')}.png")
    return p if os.path.exists(p) else None

def build_all(st, dates=None, dry=False):
    """Собрать и залить страницы за dates (или все), плюс архив, ленту и sitemap."""
    issues = issues_sorted(st)
    by = {i["date"]: k for k, i in enumerate(issues)}
    targets = dates or [i["date"] for i in issues]
    ok = True
    for d in targets:
        if d not in by: continue
        k = by[d]; iss = issues[k]
        html_ = render_issue(iss, prev_iss=issues[k + 1] if k + 1 < len(issues) else None, next_iss=issues[k - 1] if k > 0 else None)
        p = _write(f"{d}/index.html", html_)
        if not dry:
            ok &= _ftp_put(p, f"credit-radar/{d}/index.html")
            ch = _chart_path(d)
            if iss.get("essay") and iss["essay"].get("chart") and ch:
                ok &= _ftp_put(ch, f"credit-radar/{d}/chart.png")
    pa = _write("archive/index.html", render_archive(issues))
    pj = _write("issues.json", issues_json(issues))
    ps = _write("sitemap-issues.xml", render_sitemap(issues))
    pf = _write("feed.xml", render_feed(issues))
    if not dry:
        ok &= _ftp_put(pa, "credit-radar/archive/index.html")
        ok &= _ftp_put(pj, "credit-radar/issues.json")
        ok &= _ftp_put(ps, "credit-radar/sitemap-issues.xml")
        ok &= _ftp_put(pf, "credit-radar/feed.xml")
    return ok

def publish_day(date, digest_text, essay=None, dry=False):
    """Главная точка входа из radar.daily. essay: {title, caption, text, rubric, chart(path|None)} или None."""
    st = load_state()
    _, first, bullets = digest_to_html(digest_text)
    if essay and essay.get("title"):
        title = strip_tags(essay["title"])
        summary = clip(caption_body(essay.get("caption")) or first, 180)
    else:
        title = clip(first, 90) if first else (f"Вечерний выпуск за {date_short(date[:10])}" if is_evening(date) else f"Сводка за {date_short(date)}")
        if is_evening(date) and first:
            title = "Вечером: " + clip(first, 80)
        rest = [b for b in bullets[1:] if not b.startswith("Акции ")]
        summary = clip(" · ".join(rest), 180) if rest else ("Также: котировки банков за неделю и месяц." if len(bullets) > 1 else "Изменений условий за сутки не зафиксировано.")
    rec = {"date": date, "title": title, "summary": summary, "digest": digest_text, "tg_ids": tg_ids(date)}
    if essay:
        rec["essay"] = {"title": strip_tags(essay.get("title", "")), "caption": essay.get("caption", ""), "text": essay.get("text", ""),
                        "rubric": essay.get("rubric", ""), "chart": bool(essay.get("chart") and os.path.exists(essay["chart"]))}
        if rec["essay"]["chart"] and os.path.abspath(essay["chart"]) != os.path.abspath(_chart_path(date) or ""):
            shutil.copy(essay["chart"], os.path.join(HERE, "out", f"chart_daily_{date.replace('-', '')}.png"))
    st[date] = rec
    if not dry: save_state(st)
    issues = issues_sorted(st)
    k = next(i for i, x in enumerate(issues) if x["date"] == date)
    dates = [date] + ([issues[k + 1]["date"]] if k + 1 < len(issues) else []) + ([issues[k - 1]["date"]] if k > 0 else [])
    ok = build_all(st, dates, dry=dry)
    url = f"{BASE}{SECTION}/{date}/"
    if ok and not dry:
        indexnow([url, f"{BASE}{SECTION}/archive/"])
    log(("собрано (dry) " if dry else ("опубликовано " if ok else "залито с ошибками ")) + url)
    return url if ok else None

def backfill(dry=False):
    """Разово: выпуски из снимков evals/days/*.json (digest + essay)."""
    for fn in sorted(os.listdir(DAYS)):
        if not fn.endswith(".json"): continue
        d = json.load(open(os.path.join(DAYS, fn), encoding="utf-8"))
        date = d.get("date") or fn[:-5]
        if not d.get("digest"): continue
        es = d.get("essay")
        essay = None
        if es and es.get("text"):
            essay = {"title": (es.get("res") or {}).get("title") or strip_tags(es["text"].split("\n")[0]), "caption": es.get("caption", ""),
                     "text": es["text"], "rubric": es.get("rubric") or "", "chart": _chart_path(date)}
        publish_day(date, d["digest"], essay, dry=dry)

if __name__ == "__main__":
    args = sys.argv[1:]
    dry = "--dry" in args
    if args and args[0] == "backfill":
        backfill(dry=dry)
    elif args and args[0] == "rebuild":
        st = load_state(); ok = build_all(st, dry=dry); log("пересобрано" if ok else "пересобрано с ошибками")
    elif args and args[0] == "publish" and len(args) > 1:
        d = json.load(open(os.path.join(DAYS, args[1] + ".json"), encoding="utf-8"))
        es = d.get("essay"); essay = None
        if es and es.get("text"):
            essay = {"title": (es.get("res") or {}).get("title", ""), "caption": es.get("caption", ""), "text": es["text"], "rubric": es.get("rubric", ""), "chart": _chart_path(args[1])}
        publish_day(args[1], d["digest"], essay, dry=dry)
    else:
        print(__doc__)
