# -*- coding: utf-8 -*-
"""Ядро радара: вежливая загрузка, извлечение текста, снапшоты и диффы, скриншоты.

Принципы:
  • только открытые источники, только чтение, никакой авторизации;
  • уважаем robots.txt и держим паузу между запросами к одному домену;
  • системное доверие TLS НЕ трогаем: российский УЦ Минцифры лежит в собственном
    бандле certs/bundle.pem и используется только этим процессом;
  • состояние сайтов храним локально — в этом смысл: диффы считаем сами.
"""
import os, re, ssl, time, json, gzip, hashlib, difflib, subprocess, urllib.request, urllib.error
import urllib.robotparser as robotparser
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

HERE   = os.path.dirname(os.path.abspath(__file__))
STATE  = os.path.join(HERE, "state")
OUT    = os.path.join(HERE, "out")
BUNDLE = os.path.join(HERE, "certs", "bundle.pem")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
# Путь к браузеру: на Mac — Chrome, на сервере — chromium. Переопределяется env.
CHROME = os.environ.get("RADAR_CHROME") or next(
    (p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/usr/bin/chromium-browser", "/usr/bin/chromium",
                 "/snap/bin/chromium", "/usr/bin/google-chrome-stable")
     if os.path.exists(p)), "")
# SPKI корневого УЦ Минцифры — чтобы Chrome доверял ТОЛЬКО ему, а не «всем подряд».
RU_ROOT_PEM = os.path.join(HERE, "certs", "ru_root.pem")

_CTX = ssl.create_default_context(cafile=BUNDLE if os.path.exists(BUNDLE) else None)   # без certs/ — системные корни
_LAST_HIT = {}          # домен -> время последнего запроса
_ROBOTS   = {}          # домен -> RobotFileParser
PAUSE = 3.0             # секунд между запросами к одному домену

def msk():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def _host(url):
    return re.sub(r"^https?://([^/]+).*$", r"\1", url)

# --- robots.txt: собственный матчер -----------------------------------------
# Стандартный urllib.robotparser нормализует путь правила через urlparse, и правило
# banki.ru «Disallow: /?» (запрет корня С QUERY) превращается в «Disallow: /» — то есть
# в запрет всего сайта. Это ошибка разбора, а не воля площадки: ниже в том же блоке идут
# полторы сотни точечных Disallow и несколько Allow, которые при тотальном запрете были бы
# бессмысленны. Поэтому свой матчер: с поддержкой * и $, без нормализации пути и с выбором
# самого длинного правила (так делают Google и Яндекс). Он СТРОЖЕ стандартного, а не мягче:
# правила вида «Disallow: */responses/*» urllib вообще не понимает, а этот понимает.
def _rule_rx(pattern):
    p = pattern.strip()
    end = p.endswith("$")
    if end:
        p = p[:-1]
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in p)
    return re.compile("^" + rx + ("$" if end else ""))

def _load_robots(host):
    """Правила блока User-agent: * — [(allow?, длина шаблона, регулярка)].
    Берём именно блок '*': наш User-Agent браузерный, мы не выдаём себя за
    поисковых роботов и не имеем права на их послабления."""
    if host in _ROBOTS:
        return _ROBOTS[host]
    groups, agents, rules, prev_agent = [], [], [], False
    try:
        raw = _raw_get(f"https://{host}/robots.txt", timeout=10)
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                if not prev_agent:
                    if agents:
                        groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(v.lower()); prev_agent = True
            elif k in ("allow", "disallow"):
                prev_agent = False
                if v:
                    rules.append((k == "allow", len(v), _rule_rx(v)))
        if agents:
            groups.append((agents, rules))
        rules = next((r for a, r in groups if "*" in a), [])
    except Exception:
        rules = None            # robots не забрался — ведём себя как обычный браузер
    _ROBOTS[host] = rules
    return rules

def allowed(url):
    host = _host(url)
    path = re.sub(r"^https?://[^/]+", "", url) or "/"
    rules = _load_robots(host)
    if not rules:               # None (нет файла) или пустой список правил
        return True
    best = None
    for allow, ln, rx in rules:
        if rx.match(path) and (best is None or ln > best[1] or (ln == best[1] and allow)):
            best = (allow, ln, rx)
    return best is None or best[0]

def _raw_get(url, timeout=25):
    h = _host(url)
    wait = PAUSE - (time.time() - _LAST_HIT.get(h, 0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data
    finally:
        _LAST_HIT[h] = time.time()

def get(url, timeout=25, tries=2):
    """Возвращает (html, ошибка). Никогда не бросает — падение одного источника
    не должно ронять весь обход."""
    if not allowed(url):
        return None, "запрещено robots.txt"
    last = ""
    for i in range(tries):
        try:
            return _raw_get(url, timeout).decode("utf-8", "replace"), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (401, 403, 404, 451):
                break            # защита/нет страницы — повтор не поможет
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:60]}"
        time.sleep(1.5 * (i + 1))
    return None, last

# ---------------- HTML -> осмысленный текст ----------------
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    def __init__(self):
        super().__init__(convert_charrefs=True); self.out = []; self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP: self.skip += 1
    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip: self.skip -= 1
    def handle_data(self, d):
        if not self.skip:
            t = " ".join(d.split())
            if t: self.out.append(t)

def to_text(html):
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        pass
    lines, seen = [], set()
    for t in p.out:
        if len(t) < 2 or t in seen:
            continue
        seen.add(t); lines.append(t)
    return "\n".join(lines)

# Динамический мусор, который меняется каждый запрос и создаёт ложные диффы.
_NOISE = [
    (re.compile(r"\b\d{2}:\d{2}(:\d{2})?\b"), "<время>"),
    (re.compile(r"\b\d{1,2}\s+(январ|феврал|март|апрел|ма|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\s+\d{4}"), "<дата>"),
    (re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b"), "<дата>"),
    (re.compile(r"[?&](utm_[a-z]+|_r|rnd|sid|ts)=[^\s\"']+"), ""),
    (re.compile(r"\b[0-9a-f]{16,}\b"), "<хэш>"),
]
def denoise(text):
    for rx, rep in _NOISE:
        text = rx.sub(rep, text)
    return text

# ---------------- снапшоты и диффы ----------------
def _slug(s):
    return re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", s).strip("_")[:60]

def snap_path(bank, product):
    d = os.path.join(STATE, bank)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _slug(product) + ".json")

def load_snap(bank, product):
    try:
        with open(snap_path(bank, product), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_snap(bank, product, url, text, html=None):
    p = snap_path(bank, product)
    rec = {"url": url, "ts": msk().isoformat(timespec="seconds"),
           "sha": hashlib.sha256(text.encode()).hexdigest()[:16], "text": text}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    if html:                       # сырой HTML — в архив, сжатым
        arc = os.path.join(STATE, bank, "raw")
        os.makedirs(arc, exist_ok=True)
        with gzip.open(os.path.join(arc, f"{_slug(product)}_{msk():%Y%m%d}.html.gz"), "wt",
                       encoding="utf-8") as f:
            f.write(html)
    return rec

def history_lines(bank, product, days=7, before=None):
    """Множества строк страницы по архиву raw за последние `days` снимков (без снимка за дату `before`).
    Нужны, чтобы отличать настоящее изменение от «мигания» блоков при рендере: 13.09.2026 калькулятор
    Сбера («До 5 млн ₽») то появлялся, то исчезал — это не новость."""
    d = os.path.join(STATE, bank, "raw")
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith(_slug(product) + "_") and n.endswith(".html.gz"))
    except Exception:
        return []
    if before:
        names = [n for n in names if not n.endswith(f"_{before}.html.gz")]
    out = []
    for n in names[-days:]:
        try:
            with gzip.open(os.path.join(d, n), "rt", encoding="utf-8") as f:
                out.append({l for l in denoise(to_text(f.read())).split("\n") if l.strip()})
        except Exception:
            continue
    return out

def stable_diff(changes, history):
    """Оставляет только устойчивые изменения: «+строка» — если её не было ни в одном из прошлых снимков,
    «-строка» — если она была во всех. Остальное — мигание вёрстки."""
    if not history:
        return changes
    keep = []
    for c in changes:
        line = c[1:]
        if c.startswith("+") and any(line in h for h in history):
            continue
        if c.startswith("-") and not all(line in h for h in history):
            continue
        keep.append(c)
    return keep

# Строки страницы, которые никогда не являются изменением условий: cookie-баннеры, чат-боты,
# опросы «оцените страницу», промо приложений, навигационные заголовки. 12.09.2026 такие строки
# давали «значимых строк 4 из 4» у Сбера (cookies/Принять/ГигаЧат) и засоряли сводку и статистику.
_PAGE_NOISE = re.compile(
    r"(cookie|куки|мы используем|принять\b|согласн\w* с использованием|гигачат|чат-бот|чат бот|поможет разобраться|"
    r"оцените,? как вам|оцените страницу|есть вопросы|задать вопрос|скачать приложение|установите приложение|"
    r"в приложении|app store|google play|rustore|подборки товаров|медиа о самых|поделиться|карта сайта|"
    r"войти\b|личный кабинет|все права защищены|©|политик\w* конфиденциальност|"
    r"^\W*(какие документы нужны|требования к заёмщику|требования к заемщику|как получить|как оформить|"
    r"часто задаваемые вопросы|вопросы и ответы|подробнее|узнать больше|оформить|подать заявку)\W*$)",
    re.I)
def is_page_noise(line):
    return bool(_PAGE_NOISE.search(line.strip().lstrip("+-").strip()))

def diff(old_text, new_text, signal_words, limit=40):
    """Возвращает (значимые строки, всего изменений). Значимость — по словам-маркерам.
    Шумовые строки (cookie, чат-бот, «оцените страницу», промо приложений) не считаются изменениями."""
    o = [l for l in old_text.split("\n") if l.strip()]
    n = [l for l in new_text.split("\n") if l.strip()]
    changes = []
    for line in difflib.unified_diff(o, n, lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith(("+", "-")) and not is_page_noise(line):
            changes.append(line)
    low = lambda s: s.lower()
    strong = [c for c in changes if any(w in low(c) for w in signal_words)]
    picked = (strong or changes)[:limit]
    return picked, len(changes)

# ---------------- скриншоты ----------------
RU_SUB_PEM = os.path.join(HERE, "certs", "ru_sub.pem")

def _spki_one(pem_path):
    """base64(SHA-256(SubjectPublicKeyInfo)) одного сертификата — формат, который
    ждёт Chrome в --ignore-certificate-errors-spki-list."""
    import base64
    pub = subprocess.run(["openssl", "x509", "-in", pem_path, "-pubkey", "-noout"],
                         capture_output=True, timeout=10).stdout
    der = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "der"],
                         input=pub, capture_output=True, timeout=10).stdout
    dgst = subprocess.run(["openssl", "dgst", "-sha256", "-binary"], input=der,
                          capture_output=True, timeout=10).stdout
    if not dgst:
        raise RuntimeError("пустой SPKI")
    return base64.b64encode(dgst).decode()

def _spki_list():
    """SPKI-отпечатки УЦ Минцифры (корень И промежуточный) для Chrome: доверяем
    ТОЧЕЧНО им, а не отключаем проверку сертификатов целиком.

    Была ошибка: прежняя версия декодировала DER как utf-8, падала, и функция молча
    возвращала None — Chrome шёл без флага и на Сбере/Совкомбанке отдавал НЕ страницу
    банка, а свою страницу-предупреждение «Your connection is not private» (~7 КБ
    текста, одинаковых для всех URL). Именно это и выглядело как «сайт отдаёт заглушку».
    """
    out = []
    for pem in (RU_ROOT_PEM, RU_SUB_PEM):
        if not os.path.exists(pem):
            continue
        try:
            out.append(_spki_one(pem))
        except Exception:
            pass
    return out


def _chrome_base():
    import base64, tempfile
    # ОТДЕЛЬНЫЙ профиль на каждый запуск: с общим профилем второй headless-Chrome
    # молча виснет до таймаута (споткнулся на этом — скриншот «делался» 70 секунд).
    prof = tempfile.mkdtemp(prefix="radar_chrome_")
    # ВАЖНО: в Chrome 152 режим "--headless=new" с --dump-dom виснет и не отдаёт DOM;
    # обычный "--headless" DOM печатает (проверено 07.09.2026). Процесс при этом
    # всё равно не завершается сам — его снимает _run_chrome по стабилизации вывода.
    cmd = [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
           "--no-first-run", "--no-default-browser-check", "--disable-extensions",
           "--disable-dev-shm-usage", "--no-sandbox", f"--user-data-dir={prof}",
           # UA и язык — те же, что в обычном GET: иначе часть сайтов отдаёт
           # другую вёрстку, и диффы между «http» и «браузер» становятся ложными.
           f"--user-agent={UA}", "--lang=ru-RU", "--accept-lang=ru-RU,ru",
           "--disable-blink-features=AutomationControlled",
           "--window-size=1440,2400"]
    spki = _spki_list()
    if spki:
        cmd.append("--ignore-certificate-errors-spki-list=" + ",".join(spki))
    return cmd

def _run_chrome(cmd, outfile, timeout, min_bytes=2000, quiet_ticks=3):
    """Запуск Chrome с выводом в файл. Хром 152 после --dump-dom не завершается,
    поэтому ждём не выхода процесса, а стабилизации размера файла и снимаем его сами.
    Без этого весь рендер упирался в таймаут и «не работал» на ровном месте."""
    import tempfile, signal
    with open(outfile, "wb") as f:
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    prev, quiet = -1, 0
    try:
        for _ in range(int(timeout * 2)):
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            try:
                size = os.path.getsize(outfile)
            except OSError:
                size = 0
            if size >= min_bytes and size == prev:
                quiet += 1
                if quiet >= quiet_ticks:
                    break
            else:
                quiet = 0
            prev = size
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    return os.path.getsize(outfile) if os.path.exists(outfile) else 0

def render(url, wait_ms=12000, timeout=60):
    """Отрисовка страницы headless-хромом и выгрузка готового DOM.
    Нужна там, где сайт отдаёт пустую JS-заглушку или бьёт по антиботу
    (Сбер, Альфа, Совкомбанк, Озон): обычный GET там возвращает каркас без цифр."""
    if not os.path.exists(CHROME):
        return None, "нет Chrome"
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="radar_dom_", suffix=".html")
    os.close(fd)
    cmd = _chrome_base() + [f"--virtual-time-budget={wait_ms}", "--dump-dom", url]
    try:
        n = _run_chrome(cmd, tmp, timeout)
        if n < 2000:
            return None, "пустой DOM"
        with open(tmp, encoding="utf-8", errors="replace") as f:
            return f.read(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"
    finally:
        try: os.unlink(tmp)
        except Exception: pass

MIN_TEXT = 1500     # меньше — почти наверняка JS-заглушка, а не страница продукта

def get_rendered(url):
    """Сначала дешёвый GET; если пришла заглушка — рендерим браузером.
    Возвращает (html, текст, способ, ошибка)."""
    html, err = get(url)
    if html:
        txt = denoise(to_text(html))
        if len(txt) >= MIN_TEXT:
            return html, txt, "http", None
    html2, err2 = render(url)
    if not html2:                    # headless-хром иногда срывается на первом заходе
        time.sleep(2.0)
        html2, err2 = render(url, wait_ms=15000, timeout=75)
    if html2:
        txt2 = denoise(to_text(html2))
        if len(txt2) >= 300:
            return html2, txt2, "браузер", None
    return None, None, None, (err or err2 or "пусто")

def screenshot(url, path, width=1280, height=1600, wait_ms=3500):
    """Скриншот страницы headless-хромом. Возвращает путь или None."""
    if not os.path.exists(CHROME):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = _chrome_base() + [f"--window-size={width},{height}",
                            f"--virtual-time-budget={wait_ms}",
                            f"--screenshot={path}", url]
    import tempfile, signal
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        prev, quiet = -1, 0
        for _ in range(120):                      # тот же приём, что и в render
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size > 8000 and size == prev:
                quiet += 1
                if quiet >= 2:
                    break
            else:
                quiet = 0
            prev = size
        if proc.poll() is None:
            try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception: proc.kill()
        return path if os.path.exists(path) and os.path.getsize(path) > 8000 else None
    except Exception:
        return None
