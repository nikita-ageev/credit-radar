# -*- coding: utf-8 -*-
"""Реестр наблюдения: за кем и за чем следим.

Продукты выбраны зеркально линейке Озон Банка: кредитная карта, карта рассрочки
(банковская и BNPL), кредит наличными, автокредит.
Правило: сюда попадает ТОЛЬКО то, что открыто опубликовано на сайте компании.

КАК ЧИТАТЬ КОММЕНТАРИИ. У каждой ссылки помечены дата проверки и способ:
    http     — страница отдаёт содержимое обычным GET (core.get);
    браузер  — обычный GET отдаёт JS-каркас или упирается в антибот,
               нужен рендер headless-хромом (core.get_rendered сам переключится).
Все ссылки ниже прогнаны через core.get_rendered 07–08.09.2026: в извлечённом
тексте есть реальные параметры продукта (ставка / ПСК / лимит / срок / грейс /
кэшбэк), а не только меню и футер. Что НЕ удалось — перечислено в sources_report.md
и продублировано комментариями BROKEN ниже.

ВАЖНО ПРО core.py (починено 08.09.2026, иначе весь блок «браузер» врал):
  1) Chrome 152 с "--headless=new" не отдаёт --dump-dom и висит до таймаута;
     нужен обычный "--headless", а процесс приходится снимать по стабилизации вывода.
  2) Функция расчёта SPKI-отпечатка УЦ Минцифры молча падала и возвращала None —
     Chrome шёл БЕЗ --ignore-certificate-errors-spki-list и на всех российских
     сертификатах отдавал свою страницу «Your connection is not private».
     Именно поэтому Сбер раньше отдавал ~7 КБ одинакового текста на все четыре URL.
"""

# ---- ключевые конкуренты и их продуктовые страницы -------------------------
# regnum — регистрационный номер в ЦБ (нужен для форм 101/102/135 в cbr.py).
# Все regnum сверены 08.09.2026 по cbr.ru: coinfo/f101 → «Наименование кредитной
# организации» совпадает с name.
BANKS = {
    "sber": dict(name="Сбербанк", regnum=1481, host="www.sberbank.ru", pages={
        # 08.09.2026 · браузер · грейс до 120 дней, кэшбэк до 10%, лимит до 1 млн ₽
        "кредитная карта":  "https://www.sberbank.ru/ru/person/bank_cards/credit",
        # 08.09.2026 · браузер · сумма/срок/ставка по потребкредиту без залога
        "кредит наличными": "https://www.sberbank.ru/ru/person/credits/money/consumer_unsecured",
        # 08.09.2026 · браузер · ставка и условия автокредита
        "автокредит":       "https://www.sberbank.ru/ru/person/credits/money/avtokredit",
        # 08.09.2026 · http · BNPL «Плати частями»: 2 месяца, до 50 000 ₽, без переплаты
        # (отдельный домен; на sberbank.ru страницы BNPL отдают soft-404 в ~5,8 КБ)
        "рассрочка":        "https://platichastyami.ru/",
    }),
    "tbank": dict(name="Т-Банк", regnum=2673, host="www.tbank.ru", pages={
        # 08.09.2026 · http · витрина кредиток: кэшбэк, грейс, лимиты
        "кредитная карта":  "https://www.tbank.ru/cards/credit-cards/",
        # 08.09.2026 · http · лимит до 5 млн ₽, срок до 5 лет
        "кредит наличными": "https://www.tbank.ru/loans/cash-loan/",
        # 08.09.2026 · http · ПСК 9,899%–33,531%, ставка 10%–32,9% годовых
        "автокредит":       "https://www.tbank.ru/loans/auto-loan/",
        # 08.09.2026 · http · BNPL «Долями»: 6 недель бесплатно, Долями Плюс 3–10 мес
        "рассрочка":        "https://dolyame.ru/",
    }),
    "alfa": dict(name="Альфа-Банк", regnum=1326, host="alfabank.ru", pages={
        # 08.09.2026 · браузер · грейс, кэшбэк, условия по линейке кредиток
        "кредитная карта":  "https://alfabank.ru/get-money/credit-cards/",
        # 08.09.2026 · браузер · ПСК 18,990%–55,899%, ставка 18,99%–55,89% годовых
        "кредит наличными": "https://alfabank.ru/get-money/credit/",
        # 08.09.2026 · браузер · ставка и ПСК по автокредиту
        "автокредит":       "https://alfabank.ru/get-money/autocredit/",
        # 08.09.2026 · http · BNPL «Подели»: 4 платежа без переплат, первый 25%
        "рассрочка":        "https://podeli.ru/",
    }),
    "gpb": dict(name="Газпромбанк", regnum=354, host="www.gazprombank.ru", pages={
        # 08.09.2026 · http · льготный период до 120 дней, лимит до 1 млн ₽
        "кредитная карта":  "https://www.gazprombank.ru/personal/credit-cards/",
        # 08.09.2026 · http · ставки, до 120 дней без %, условия по кредитам
        "кредит наличными": "https://www.gazprombank.ru/personal/credits/",
        # 08.09.2026 · http · карточка автокредита со ставкой и снижением до 15% годовых
        # (раздел /personal/avtokredit/ — витрина, цифры продукта на карточке ниже)
        "автокредит":       "https://www.gazprombank.ru/personal/avtokredit/6018195/",
    }),
    "vtb": dict(name="ВТБ", regnum=1000, host="www.vtb.ru", pages={
        # 08.09.2026 · http · кешбэк до 15%, минимальный платёж 3% от задолженности
        "кредитная карта":  "https://www.vtb.ru/personal/karty/kreditnye/",
        # 08.09.2026 · http · «Комфортный платёж»: дисконт 15 месяцев, условия сохранения ставки
        "кредит наличными": "https://www.vtb.ru/personal/kredit/komfortniy-platezh/",
        # 08.09.2026 · http · скидка до 25% от стоимости авто, условия автокредита
        "автокредит":       "https://www.vtb.ru/personal/avtokredity/",
    }),
    "sovcom": dict(name="Совкомбанк", regnum=963, host="sovcombank.ru", pages={
        # BROKEN 08.09.2026 · сайт за Qrator: обычный GET → HTTP 401 с JS-челленджем,
        # headless-хром → страница «Доступ временно ограничен».  Адреса ниже взяты из
        # их собственного sitemap.xml (он отдаётся), но содержимое не вытягивается.
        # Обходить защиту не стали — это прямо вне рамок проекта.  Держим в реестре,
        # чтобы radar.py фиксировал недоступность, а не «отсутствие изменений».
        "карта рассрочки":  "https://sovcombank.ru/cards/rassrochki/halva",
        "кредитная карта":  "https://sovcombank.ru/cards/credit-cards",
        "кредит наличными": "https://sovcombank.ru/credits/cash",
    }, blocked=True),
    "mts": dict(name="МТС-Банк", regnum=2268, host="www.mtsbank.ru", pages={
        # 08.09.2026 · http · «До 111 дней без %», обслуживание 0 ₽
        # (рендер браузером на mtsbank.ru стабильно даёт пустой DOM — берём http)
        "кредитная карта":  "https://www.mtsbank.ru/chastnim-licam/karti/credit-zero/",
        # 08.09.2026 · http · сумма 20 000–15 000 000 ₽, срок 1–15 лет
        "кредит наличными": "https://www.mtsbank.ru/chastnim-licam/krediti/credit-all/",
        # 08.09.2026 · http · ПСК 23,219%–31,425%, ставка 11,9%–31,9% годовых
        "автокредит":       "https://www.mtsbank.ru/chastnim-licam/krediti/kredit-pod-zalog-avto/",
    }),
    # "raif" (Райффайзенбанк, 3292) снят с наблюдения 09.09.2026: новые кредитки не выдаёт
    # с 2022, наём свёрнут — шум, а не сигнал. Конфиг страниц в sources.py.bak.
    "otp": dict(name="ОТП Банк", regnum=2766, host="www.otpbank.ru", pages={
        # 08.09.2026 · браузер · кэшбэк до 30%, грейс, условия кредиток
        "кредитная карта":  "https://www.otpbank.ru/retail/cards/credit/",
        # 08.09.2026 · браузер · ПСК 18,899%/20,899%, ставка 18,9% годовых
        "кредит наличными": "https://www.otpbank.ru/retail/credits/cash/",
        # 08.09.2026 · браузер · POS-кредитование в магазинах-партнёрах
        "кредит в магазине":"https://www.otpbank.ru/retail/kredit_v_magazine/",
    }),
    "ren": dict(name="Ренессанс Кредит", regnum=3354, host="rencredit.ru", pages={
        # 08.09.2026 · браузер · кэшбэк 1,5%, обслуживание, грейс
        "кредитная карта":  "https://rencredit.ru/cards/credit/",
        # 08.09.2026 · браузер · линейка кредитов наличными со ставками
        "кредит наличными": "https://rencredit.ru/loans/",
    }),
    "uralsib": dict(name="Банк Уралсиб", regnum=2275, host="uralsib.ru", pages={
        # 08.09.2026 · браузер · грейс, кэшбэк, условия кредитных карт
        "кредитная карта":  "https://uralsib.ru/kreditnye-karty",
        # 08.09.2026 · http · срок 13–84 месяца, ПСК, влияние страховки на ставку
        "кредит наличными": "https://uralsib.ru/kredity/kredit-na-lyubye-tseli",
        # 08.09.2026 · http · срок 12–120 месяцев, услуга «Своя ставка»
        "автокредит":       "https://uralsib.ru/avtokredity/noviy-avtomobil",
    }),
    "yandex": dict(name="Яндекс Банк", regnum=3027, host="pay.yandex.ru", pages={
        # 08.09.2026 · http · «Яндекс Сплит»: тарифы и условия рассрочки
        # (bank.yandex.ru закрыт: robots.txt + SmartCaptcha, см. BROKEN в отчёте)
        "рассрочка":        "https://pay.yandex.ru/split",
    }),
    "wb": dict(name="Вайлдберриз Банк", regnum=841, host="wb-bank.ru", pages={
        # 08.09.2026 · браузер · кредитный лимит WB Кэш до 100 000 ₽, вклад до 15,1%
        # ВНИМАНИЕ: правильный домен именно wb-bank.ru.  Старый wbbank.ru из реестра
        # рвёт TLS-хендшейк (sslv3 alert handshake failure) — там ничего нет.
        "витрина":          "https://wb-bank.ru/",
    }),
    "ozon": dict(name="Озон Банк", regnum=3542, host="finance.ozon.ru", pages={
        # 08.09.2026 · браузер · полный каталог продуктов банка
        # ВНИМАНИЕ: finance.ozon.ru гоняет обычный GET по редиректу ?attempt=1 → 403.
        # Работает только через рендер (Chrome держит куки) — core.get_rendered справляется.
        "витрина":          "https://finance.ozon.ru/products",
        # 08.09.2026 · браузер · грейс до 140 дней на Ozon и до 80 дней вне, кэшбэк до 25%
        "кредитная карта":  "https://finance.ozon.ru/promo/cards/credit/bez-procentov",
        # 08.09.2026 · браузер · рассрочка на 6/12/24 месяца, зелёные цены до −30%
        "рассрочка":        "https://finance.ozon.ru/promo/partpayment/landing",
        # 08.09.2026 · браузер · «Деньги в рассрочку» на карту, лимит
        "деньги в рассрочку":"https://finance.ozon.ru/promo/partpayment/dengi-na-ozon-kartu",
    }, own=True),   # свой банк: следим для сверки, в постах — как точка отсчёта
}

# УБРАН ИЗ РЕЕСТРА 08.09.2026: Почта Банк (regnum 650).
#   www.pochtabank.ru отдаёт страницу ВТБ: «ВТБ и Почта Банк объединились, с 1 мая
#   все продукты и счета клиентов Почта Банка перенесены в ВТБ».  Отдельного
#   розничного предложения больше нет — следить не за чем, дублирует vtb.
# УБРАН ИЗ РЕЕСТРА 08.09.2026: Хоум Кредит.
#   www.homecredit.ru редиректит на sovcombank.ru/pages/uvazhaemie-klienti-ooo-hkf-banka-
#   dobro-pozhalovat-v-sovkombank — банк присоединён к Совкомбанку.

# ---- агрегаторы: тональность, рейтинги, «народный рейтинг» -----------------
# Шаблоны проверены 08.09.2026.  banki.ru отдаётся обычным GET (~19–20 КБ текста
# с оценками и отзывами), но при частых заходах начинает возвращать 677-байтную
# заглушку — вызывающей стороне держать паузу, core.PAUSE=3 с достаточно.
# sravni.ru отдаёт SPA-каркас: цифры рейтинга появляются только после рендера,
# поэтому по нему ходить строго через core.get_rendered.
AGGREGATORS = {
    "banki.ru":   "https://www.banki.ru/services/responses/bank/{slug}/",
    "sravni.ru":  "https://www.sravni.ru/bank/{slug}/otzyvy/",
}
# slug на каждом агрегаторе; None — банка на площадке нет.
# Все slug проверены 08.09.2026 поштучно:
#   banki.ru — по HTTP-коду (несуществующий slug честно отдаёт 404);
#   sravni.ru — по <meta name="description">: у верного slug там «Отзывы о <банке>…»
#   с числом отзывов, у неверного meta пустой и страница подменяется общей витриной.
AGG_SLUGS = {
    "sber":    dict(banki="sberbank",    sravni="sberbank-rossii"),   # sravni: НЕ "sberbank"
    "tbank":   dict(banki="tcs",         sravni="t-bank"),            # sravni: НЕ "tinkoff"
    "alfa":    dict(banki="alfabank",    sravni="alfa-bank"),
    "gpb":     dict(banki="gazprombank", sravni="gazprombank"),
    "vtb":     dict(banki="vtb",         sravni="vtb"),
    "sovcom":  dict(banki="sovcombank",  sravni="sovkombank"),
    "mts":     dict(banki="mts-bank",    sravni="mts-bank"),          # banki: НЕ "mtsbank"
    "otp":     dict(banki="otpbank",     sravni="otp-bank"),
    "ren":     dict(banki="rencredit",   sravni="renessans-kredit"),
    "uralsib": dict(banki="uralsib",     sravni="uralsib"),
    "yandex":  dict(banki="yandexbank",  sravni="yandex-bank"),       # banki: НЕ "yandex-bank"
    "wb":      dict(banki="wbbank",      sravni=None),                # на sravni карточки нет
    "ozon":    dict(banki="ozonbank",    sravni="ozon-bank"),         # banki: НЕ "ozon-bank"
}

# ---- регуляторика и иски ---------------------------------------------------
# Все ссылки проверены 08.09.2026 и реально открываются с содержимым.
# Старый набор был нерабочим: cbr.ru/press/ — это 404-страница ЦБ,
# fas.gov.ru/documents — HTTP 404, fas.gov.ru/news — HTTP 500.
REGULATORY = {
    # 08.09.2026 · браузер · лента новостей ЦБ с датами — быстрее всего ловит решения
    "ЦБ — новости":               "https://www.cbr.ru/news/",
    # 08.09.2026 · http · пресс-релизы (в т.ч. меры к банкам, отзывы лицензий)
    "ЦБ — пресс-релизы":          "https://www.cbr.ru/press/pr/",
    # 08.09.2026 · http · среднерыночные значения ПСК — прямой ограничитель ценообразования
    "ЦБ — ПСК":                   "https://www.cbr.ru/statistics/bank_sector/psk/",
    # 08.09.2026 · http · статистика банковского сектора (портфели, просрочка)
    "ЦБ — статистика сектора":    "https://www.cbr.ru/statistics/bank_sector/",
    # 08.09.2026 · http · информация по кредитным организациям (формы 101/102/135)
    "ЦБ — справочник КО":         "https://www.cbr.ru/banking_sector/credit/coinfo/",
    # 08.09.2026 · http · список компаний с признаками нелегальной деятельности
    "ЦБ — предупредительный список": "https://www.cbr.ru/inside/warning-list/",
    # 08.09.2026 · http · нормативные акты и приказы ФАС с датами публикации
    "ФАС — акты и приказы":       "https://fas.gov.ru/documents/type_of_documents/acts",
    # BROKEN 08.09.2026: fas.gov.ru/news → HTTP 500, fas.gov.ru/documents → HTTP 404,
    # br.fas.gov.ru (реестр решений) — SPA, после рендера цифр и решений нет.
    # BROKEN 08.09.2026: rospotrebnadzor.ru → HTTP 401 (JS-челлендж), рендер даёт 653 байта.
    # BROKEN 08.09.2026: kad.arbitr.ru и sudact.ru → отдают только капчу/каркас.
}

# ---- что считаем значимым изменением ---------------------------------------
# Строки диффа, где встречается любое из этих слов, идут в анализ первыми.
SIGNAL_WORDS = [
    "ставк", "процент", "%", "годовых", "пск", "лимит", "грейс", "беспроцентн",
    "льготн", "кэшбэк", "кешбэк", "бонус", "комисси", "обслуживани", "бесплатн",
    "рассрочк", "част", "срок", "до 5 000 000", "млн", "тыс", "платеж",
    "акци", "скидк", "промо", "новый", "запуск", "снижа", "повыша",
]

PRODUCTS = ["кредитная карта", "карта рассрочки", "рассрочка", "деньги в рассрочку",
            "кредит наличными", "кредит в магазине", "автокредит", "витрина"]
