import base64
import hmac
import json
import logging
import signal
from pathlib import Path
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


CONFIG_PATH = Path(__file__).with_name("config.json")
config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
HOST = os.environ.get("HOST", config.get("host", "127.0.0.1"))
PORT = int(os.environ.get("PORT", config.get("port", 8000)))
SITE_USERNAME = os.environ.get("SITE_USERNAME", config.get("username", "owner"))
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", config.get("password", ""))
REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", str(config.get("require_auth", False))).lower() in ("1", "true", "yes")
logger = logging.getLogger("futures")
stop_event = threading.Event()

CACHE_SECONDS = 15
REQUEST_TIMEOUT = 12
MOSCOW = timezone(timedelta(hours=3))

# Исходные серии из бота. После экспирации выбирается
# ближайшая действующая серия того же инструмента.
CONTRACTS = (
    ("SILV-9.26", "Серебро"),
    ("BR-10.26", "Нефть Brent"),
    ("NG-9.26", "Природный газ"),
    ("COCOA-11.26", "Какао"),
    ("CNY-9.26", "Юань / рубль"),
    ("MIX-9.26", "Индекс МосБиржи"),
    ("GOLD-9.26", "Золото"),
)

# Здесь только общедоступные данные Мосбиржи.
# Ввод пользователей в кеше не хранится.
market_lock = threading.Lock()
refresh_lock = threading.Lock()
market_cache = {
    "instruments": [],
    "updated": None,
    "warning": "Загрузка данных Мосбиржи…",
}
next_attempt = 0.0


def asset(ticker):
    return ticker.split("-", 1)[0]


def today():
    return datetime.now(MOSCOW).date().isoformat()


def rows(block):
    if not isinstance(block, dict):
        raise ValueError("Отсутствует блок данных Мосбиржи")

    columns = block.get("columns")
    data = block.get("data")

    if not isinstance(columns, list) or not isinstance(data, list):
        raise ValueError("Неполный ответ Мосбиржи")

    return [dict(zip(columns, row)) for row in data]


def positive_text(value):
    """Положительное конечное число либо None."""
    try:
        number = Decimal(str(value))
        if number.is_finite() and number > 0:
            return str(number)
    except (InvalidOperation, ValueError):
        pass

    return None


def nonnegative_text(value):
    """Отсутствующие значения не превращаем в нули."""
    try:
        number = Decimal(str(value))
        if number.is_finite() and number >= 0:
            return str(number)
    except (InvalidOperation, ValueError):
        pass

    return None


def fetch_market():
    params = {
        "iss.meta": "off",
        "iss.only": "securities,marketdata",
        "securities.columns": (
            "SECID,SHORTNAME,MINSTEP,STEPPRICE,"
            "INITIALMARGIN,LASTTRADEDATE"
        ),
        "marketdata.columns": (
            "SECID,LAST,VOLTODAY,OPENPOSITION,"
            "UPDATETIME,TRADEDATE"
        ),
    }

    url = (
        "https://iss.moex.com/iss/engines/futures/"
        "markets/forts/securities.json?"
        + urlencode(params)
    )

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "FuturesCalculator/1.0",
        },
    )

    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Слишком большой ответ Мосбиржи")
        data = json.loads(raw)

    securities = rows(data.get("securities"))
    quotes = {
        row["SECID"]: row
        for row in rows(data.get("marketdata"))
        if row.get("SECID")
    }

    day = today()
    timestamp = datetime.now(timezone.utc).isoformat()
    instruments = []

    for original, name in CONTRACTS:
        base = asset(original)
        candidates = []

        for security in securities:
            ticker = str(security.get("SHORTNAME") or "")
            expiry = str(security.get("LASTTRADEDATE") or "")

            if asset(ticker) != base:
                continue
            if not expiry or expiry < day:
                continue

            step = positive_text(security.get("MINSTEP"))
            cost = positive_text(security.get("STEPPRICE"))
            margin = positive_text(security.get("INITIALMARGIN"))

            if not all((step, cost, margin)):
                continue

            candidates.append((security, step, cost, margin))

        candidates.sort(
            key=lambda item: (
                str(item[0]["LASTTRADEDATE"]),
                str(item[0]["SHORTNAME"]),
            )
        )

        # Сохраняем исходную серию, пока она действует.
        selected = next(
            (
                item
                for item in candidates
                if item[0]["SHORTNAME"] == original
            ),
            candidates[0] if candidates else None,
        )

        if selected is None:
            continue

        security, step, cost, margin = selected
        quote = quotes.get(security["SECID"], {})

        instruments.append({
            "asset": base,
            "ticker": security["SHORTNAME"],
            "name": name,
            "step": step,
            "cost": cost,
            "margin": margin,
            "last": positive_text(quote.get("LAST")),
            "oi": nonnegative_text(quote.get("OPENPOSITION")),
            "volume": nonnegative_text(quote.get("VOLTODAY")),
            "expiry": security["LASTTRADEDATE"],
            "quote_date": quote.get("TRADEDATE"),
            "quote_time": quote.get("UPDATETIME"),
            "fetched_at": timestamp,
        })

    if not instruments:
        raise ValueError("Мосбиржа не вернула параметры контрактов")

    return instruments


def refresh_market():
    """
    Фоновое обновление, не чаще одного раза в 15 секунд.
    Пользовательские данные сюда не передаются.
    """
    global market_cache, next_attempt

    with refresh_lock:
        if time.monotonic() >= next_attempt:
            try:
                fresh = fetch_market()

                # При частичном ответе сохраняем предыдущие
                # параметры отсутствующих инструментов.
                merged = {
                    item["asset"]: dict(item, cached=True)
                    for item in market_cache["instruments"]
                }

                for item in fresh:
                    merged[item["asset"]] = dict(item, cached=False)

                instruments = [
                    merged[asset(original)]
                    for original, _ in CONTRACTS
                    if asset(original) in merged
                ]

                fresh_assets = {item["asset"] for item in fresh}
                missing = [
                    name
                    for original, name in CONTRACTS
                    if asset(original) not in fresh_assets
                ]

                warning = ""
                if missing:
                    warning = (
                        "Нет свежих параметров: "
                        + ", ".join(missing)
                        + ". Сохранённые значения помечены отдельно."
                    )

                market_cache = {
                    "instruments": instruments,
                    "updated": datetime.now(timezone.utc).isoformat(),
                    "warning": warning,
                }

            except Exception as error:
                logger.warning("MOEX refresh failed: %s", type(error).__name__)
                # Не выводим запросы посетителей или их данные в логи.
                market_cache = {
                    **market_cache,
                    "instruments": [
                        dict(item, cached=True)
                        for item in market_cache["instruments"]
                    ],
                    "warning": (
                        "Не удалось связаться с Мосбиржей. "
                        "Если доступны предыдущие данные, показаны они. "
                        "Повторная попытка выполняется автоматически."
                    ),
                }

            finally:
                next_attempt = time.monotonic() + CACHE_SECONDS

        return {
            **market_cache,
            "catalog": [
                {"asset": asset(ticker), "name": name}
                for ticker, name in CONTRACTS
            ],
            "refresh_seconds": CACHE_SECONDS,
            "source_delay_minutes": 15,
        }


def market_worker():
    while not stop_event.is_set():
        refresh_market()
        stop_event.wait(CACHE_SECONDS)


def get_market():
    # Запросы посетителя никогда не ждут сетевого ответа биржи.
    with market_lock:
        snapshot = market_cache
        return {
            **snapshot,
            "instruments": [dict(item) for item in snapshot["instruments"]],
            "catalog": [{"asset": asset(ticker), "name": name} for ticker, name in CONTRACTS],
            "refresh_seconds": CACHE_SECONDS,
            "source_delay_minutes": 15,
        }


def number(value, label):
    if not isinstance(value, (str, int, float)):
        raise ValueError(f"{label}: введите число")

    text = str(value).strip().replace(",", ".")

    if len(text) > 40:
        raise ValueError(f"{label}: слишком длинное число")

    try:
        result = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{label}: введите корректное число")

    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label}: число должно быть больше нуля")

    if result > Decimal("1000000000000"):
        raise ValueError(f"{label}: число слишком большое")

    if result < Decimal("0.00000001"):
        raise ValueError(f"{label}: число слишком маленькое")

    return result


def rounded(value, digits=2):
    unit = Decimal("1").scaleb(-digits)
    return format(value.quantize(unit, rounding=ROUND_HALF_UP), "f")


def calculate(payload):
    if not isinstance(payload, dict):
        raise ValueError("Некорректные параметры расчёта")

    market = get_market()
    ticker = payload.get("ticker")

    instrument = next(
        (
            item
            for item in market["instruments"]
            if item["ticker"] == ticker
        ),
        None,
    )

    if instrument is None:
        raise ValueError(
            "Данные серии изменились или недоступны. "
            "Обновите котировки и выберите контракт."
        )

    if instrument["expiry"] < today():
        raise ValueError(
            "Серия завершила торги. Дождитесь обновления "
            "параметров действующего контракта."
        )

    with localcontext() as context:
        context.prec = 80

        count = number(payload.get("count"), "Количество")

        if count != count.to_integral_value() or count > 1000000000:
            raise ValueError(
                "Количество: целое число от 1 до 1 000 000 000"
            )

        step = Decimal(instrument["step"])
        cost = Decimal(instrument["cost"])
        margin = Decimal(instrument["margin"]) * count

        # Формула размера позиции из бота:
        # последняя цена × стоимость шага / шаг цены × количество.
        position = None
        if instrument["last"] is not None:
            position = rounded(
                Decimal(instrument["last"]) * cost / step * count
            )

        oi_value = None
        if instrument["oi"] is not None:
            oi_value = rounded(
                Decimal(instrument["oi"])
                * Decimal(instrument["margin"])
            )

        result = {
            "ticker": ticker,
            "count": str(int(count)),
            "position": position,
            "margin": rounded(margin),
            "oi_value": oi_value,
            "cached": instrument.get("cached", False),
            "fetched_at": instrument["fetched_at"],
        }

        if payload.get("mode") != "full":
            return result

        direction = payload.get("direction")
        if direction not in ("LONG", "SHORT"):
            raise ValueError("Выберите LONG или SHORT")

        entry = number(payload.get("entry"), "Цена входа")
        tp = number(payload.get("tp"), "Take profit")
        sl = number(payload.get("sl"), "Stop loss")

        if direction == "LONG":
            gain = tp - entry
            loss = entry - sl
        else:
            gain = entry - tp
            loss = sl - entry

        if gain <= 0 or loss <= 0:
            raise ValueError(
                "Для LONG: TP выше входа, SL ниже входа."
                if direction == "LONG"
                else "Для SHORT: TP ниже входа, SL выше входа."
            )

        steps_tp = gain / step
        steps_sl = loss / step
        profit = steps_tp * cost * count
        loss_rub = steps_sl * cost * count

        # Отношение и риск считаются до округления сумм.
        result.update({
            "profit": rounded(profit),
            "loss": rounded(loss_rub),
            "rr": rounded(profit / loss_rub),
            "risk": rounded(loss_rub / margin * 100),
            "steps_tp": rounded(steps_tp, 1),
            "steps_sl": rounded(steps_sl, 1),
        })

        return result


HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#111418">
<title>Фьючерсы — калькулятор позиции</title>
<style>
:root{
  color-scheme:dark;--bg:#111418;--panel:#1b2027;
  --line:#343d48;--text:#edf0f4;--muted:#a4afbd;
  --accent:#f5ad6b;--green:#9adbb7;--red:#f5a0aa
}
*{box-sizing:border-box}
html{min-width:320px}
body{margin:0;background:var(--bg);color:var(--text);
  font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
button,input,select{font:inherit}
button{cursor:pointer;touch-action:manipulation;min-height:44px}
input,select,.pair>div,.results,.metric{min-width:0}
button:disabled{opacity:.55;cursor:default}
button,input,select{
  border:1px solid var(--line);border-radius:8px;
  color:var(--text);background:#14191f
}
button{padding:10px 15px}
button:hover{border-color:var(--accent)}
button:focus-visible,input:focus-visible,select:focus-visible{
  outline:2px solid var(--accent);outline-offset:3px
}
header{border-bottom:1px solid var(--line)}
.header-inner{max-width:1320px;margin:auto;padding:21px 24px;
  display:flex;align-items:center;justify-content:space-between;gap:16px}
.logo{font-size:25px;font-weight:750;letter-spacing:-1px}
.logo span{color:var(--accent)}
main{max-width:1320px;margin:30px auto;padding:0 24px}
h1{font-size:27px;font-weight:600;margin:0 0 7px}
.intro{color:var(--muted);margin:0 0 24px;font-size:14px}
.grid{display:grid;grid-template-columns:260px minmax(300px,1fr)
  minmax(300px,1fr);gap:20px;align-items:start}
.panel{background:var(--panel);border:1px solid var(--line);
  border-radius:13px;padding:22px;min-width:0}
h2{font-size:17px;margin:0 0 18px}
.search{width:100%;padding:11px 12px;margin-bottom:14px}
.contracts{display:flex;flex-direction:column;gap:9px}
.contract{text-align:left;width:100%;padding:12px}
.contract.active{border-color:var(--accent);background:#342b24}
.contract strong,.contract small{display:block}
.contract strong{font-size:14px}
.contract small{font-size:12px;color:var(--muted);margin-top:4px}
.note{font-size:12px;color:var(--muted);line-height:1.7}
.tabs{display:flex;gap:8px;margin-bottom:22px}
.tabs button{flex:1;font-size:14px}
.tabs .active{background:var(--accent);border-color:var(--accent);
  color:#191b20}
label{display:block;margin:18px 0 8px;font-size:14px;color:#c7d0dc}
input,select{width:100%;min-height:46px;padding:10px 12px}
input{font-variant-numeric:tabular-nums}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.action{width:100%;margin-top:24px;background:var(--accent);
  color:#191b20;font-weight:650;border-color:var(--accent)}
.text-button{padding:0;border:0;background:none;color:var(--accent);
  font-size:12px;float:right}
dl{margin:22px 0 0}
dl>div{display:flex;justify-content:space-between;gap:16px;
  padding:10px 0;border-bottom:1px solid #303842;font-size:14px}
dt{color:var(--muted);min-width:0;overflow-wrap:anywhere}
dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;
  min-width:0;overflow-wrap:anywhere}
.big-label{font-size:16px;color:#d4dce7}
.big{font-size:clamp(30px,3.6vw,48px);color:var(--accent);
  font-weight:650;letter-spacing:-1.5px;line-height:1.25;
  margin:14px 0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.formula{font-size:12px;color:var(--muted);margin-bottom:20px}
.profit{color:var(--green)}
.loss{color:var(--red)}
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:12px;
  margin-top:23px}
.metric{border-top:1px solid var(--line);padding-top:13px}
.metric span{display:block;color:var(--muted);font-size:12px}
.metric strong{display:block;margin-top:8px;font-size:23px}
.notice{padding:13px 16px;border:1px solid #776045;border-radius:8px;
  background:#302921;color:#efcda5;margin-bottom:20px;font-size:14px}
.error{border-color:#80464e;background:#38252b;color:#ffc1c9}
.quote-state{font-size:12px;color:var(--muted);margin:17px 0 0}
.section-gap{margin-top:20px}
footer{font-size:12px;color:var(--muted);padding:26px 0;
  margin-top:28px;border-top:1px solid var(--line)}
[hidden]{display:none!important}
@media(max-width:1050px){
  .grid{grid-template-columns:230px 1fr}
  .results{grid-column:2}
  .watchlist{grid-row:span 2}
}
@media(max-width:760px){
  main{padding:0 max(16px,env(safe-area-inset-left))
    0 max(16px,env(safe-area-inset-right));margin-top:22px}
  .header-inner{padding:14px max(16px,env(safe-area-inset-right))
    14px max(16px,env(safe-area-inset-left));gap:12px}
  .logo{font-size:23px}
  #refresh{font-size:14px;padding:10px 12px}
  .grid{display:flex;flex-direction:column;gap:16px}
  .panel,.results{width:100%}
  .panel{padding:18px;border-radius:12px}
  .watchlist h2{margin-bottom:12px}
  .search{margin-bottom:12px}
  .contracts{display:flex;flex-direction:row;overflow-x:auto;
    gap:10px;padding:3px 3px 10px;scroll-snap-type:x proximity;
    scrollbar-width:thin;scrollbar-color:var(--line) transparent}
  .contract{flex:0 0 148px;scroll-snap-align:start;min-width:0}
  .contract strong,.contract small{overflow-wrap:anywhere}
  .watchlist .note{margin:8px 0 0}
  .tabs{margin-bottom:16px}
  .tabs button{min-width:0;padding:10px 8px;line-height:1.4}
  input,select{font-size:16px;min-height:48px}
  .action{min-height:50px}
  #reset{width:100%;margin-top:12px}
  label[for="entry"]{display:flex;align-items:center;
    justify-content:space-between;flex-wrap:wrap;gap:4px 12px}
  .text-button{float:none;min-height:44px;padding:8px 0}
  .big{font-size:clamp(28px,8vw,40px);letter-spacing:-1px}
  h1{font-size:24px;line-height:1.3}
  .intro{margin-top:10px;margin-bottom:18px}
  .quote-state,.notice,.formula{overflow-wrap:anywhere}
  .section-gap{margin-top:16px}
  .metric strong{overflow-wrap:anywhere}
  footer{padding-bottom:max(24px,env(safe-area-inset-bottom))}
}
@media(max-width:380px){
  .panel{padding:14px}
  .pair{grid-template-columns:minmax(0,1fr);gap:0}
  dl>div{flex-direction:column;gap:4px}
  dd{text-align:left}
  .metrics{gap:8px}
  .metric strong{font-size:20px}
}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div class="logo">фьючерсы<span>.</span></div>
    <button id="refresh" type="button">Обновить</button>
  </div>
</header>

<main>
  <h1>Калькулятор позиции</h1>
  <p class="intro">
    Автообновление каждые 15 секунд.
    Бесплатные котировки Мосбиржи могут отставать на 15 минут.
  </p>

  <div id="market-warning" class="notice" role="status" hidden></div>

  <div class="grid">
    <aside class="panel watchlist">
      <h2>Контракты</h2>
      <input id="search" class="search" placeholder="Тикер или название"
             aria-label="Поиск контракта">
      <div id="contracts" class="contracts"></div>
      <p class="note">
        После экспирации выбирается ближайшая действующая серия.
        На экране всегда указан её фактический тикер.
      </p>
    </aside>

    <section class="panel">
      <h2 id="selected-title">Загрузка контрактов…</h2>

      <div class="tabs" aria-label="Режим расчёта">
        <button id="quick-tab" class="active" type="button"
                aria-pressed="true">Быстрый</button>
        <button id="full-tab" type="button"
                aria-pressed="false">Сделка с TP / SL</button>
      </div>

      <form id="form">
        <label for="count">Количество контрактов</label>
        <input id="count" inputmode="numeric" value="1" autocomplete="off">

        <div id="full-fields" hidden>
          <label for="direction">Направление</label>
          <select id="direction">
            <option value="LONG">LONG — рост</option>
            <option value="SHORT">SHORT — снижение</option>
          </select>

          <label for="entry">
            Цена входа
            <button id="use-last" type="button" class="text-button">
              Взять последнюю
            </button>
          </label>
          <input id="entry" inputmode="decimal" placeholder="0,00"
                 autocomplete="off">

          <div class="pair">
            <div>
              <label for="tp">Take profit</label>
              <input id="tp" inputmode="decimal" placeholder="Цена цели"
                     autocomplete="off">
            </div>
            <div>
              <label for="sl">Stop loss</label>
              <input id="sl" inputmode="decimal" placeholder="Цена стопа"
                     autocomplete="off">
            </div>
          </div>

          <p id="direction-hint" class="note">
            Для LONG: TP выше входа, SL ниже входа.
          </p>
        </div>

        <button id="calculate" class="action" type="submit">
          Рассчитать
        </button>
        <button id="reset" class="section-gap" type="button">Сбросить</button>
      </form>

      <div id="calc-error" class="notice error section-gap"
           role="alert" hidden></div>

      <dl>
        <div><dt>Последняя цена</dt><dd id="last">—</dd></div>
        <div><dt>Шаг цены</dt><dd id="step">—</dd></div>
        <div><dt>Стоимость шага</dt><dd id="cost">—</dd></div>
        <div><dt>ГО на контракт</dt><dd id="unit-margin">—</dd></div>
        <div><dt>Последний день торгов</dt><dd id="expiry">—</dd></div>
      </dl>
      <p id="quote-state" class="quote-state"></p>
    </section>

    <aside class="results">
      <section class="panel" aria-live="polite">
        <div class="big-label">Размер позиции</div>
        <div id="position" class="big">—</div>
        <div class="formula">
          Последняя цена × стоимость шага ÷ шаг цены × количество
        </div>

        <dl>
          <div><dt>Гарантийное обеспечение</dt><dd id="margin">—</dd></div>
        </dl>

        <div id="full-results" hidden>
          <dl>
            <div><dt>Прибыль по TP</dt>
              <dd id="profit" class="profit">—</dd></div>
            <div><dt>Убыток по SL</dt>
              <dd id="loss" class="loss">—</dd></div>
            <div><dt>Шагов до TP / SL</dt><dd id="steps">—</dd></div>
          </dl>
          <div class="metrics">
            <div class="metric">
              <span>Прибыль / риск</span><strong id="rr">—</strong>
            </div>
            <div class="metric">
              <span>Риск от ГО</span><strong id="risk">—</strong>
            </div>
          </div>
        </div>
        <p class="note">Расчёт без учёта комиссий и проскальзывания.</p>
      </section>

      <section class="panel section-gap">
        <h2>Активность рынка</h2>
        <dl>
          <div><dt>Открытый интерес</dt><dd id="oi">—</dd></div>
          <div><dt>Оценка ОИ × ГО</dt><dd id="oi-value">—</dd></div>
          <div><dt>Объём за день</dt><dd id="volume">—</dd></div>
        </dl>
        <p class="note">
          Это показатели всего контракта на рынке.
          Они не относятся к вашей отдельной позиции.
        </p>
      </section>
    </aside>
  </div>

  <footer>
    Без регистрации и истории расчётов.
    Ввод используется только для текущего расчёта и не сохраняется.
  </footer>
</main>

<script>
"use strict";

const $ = id => document.getElementById(id);
let market = {instruments: [], catalog: []};
let selectedAsset = "BR";
let mode = "quick";
let marketBusy = false;
let marketController = null;
let calcController = null;
let calcVersion = 0;
let debounce = null;
let fullCalculated = false;

function fmt(value, digits = 2) {
  if (value === null || value === undefined) return "—";

  // Decimal-строки форматируются без преобразования в Number:
  // это сохраняет точность больших денежных сумм.
  const text = String(value);
  if (/^-?\d+(?:\.\d+)?$/.test(text)) {
    let [whole, fraction = ""] = text.split(".");
    whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0");
    fraction = fraction.slice(0, digits).replace(/0+$/, "");
    return whole + (fraction ? "," + fraction : "");
  }
  return text;
}

function rub(value) {
  return value === null || value === undefined ? "—" : fmt(value) + " ₽";
}

function instrument() {
  return market.instruments.find(i => i.asset === selectedAsset);
}

function message(id, text) {
  $(id).textContent = text || "";
  $(id).hidden = !text;
}

function clearFull() {
  for (const id of ["profit", "loss", "steps", "rr", "risk"]) {
    $(id).textContent = "—";
  }
}

function invalidate() {
  calcVersion++;
  if (calcController) calcController.abort();
  fullCalculated = false;
  clearFull();
  message("calc-error", "");
}

function setMode(next) {
  invalidate();
  mode = next;
  $("full-fields").hidden = mode !== "full";
  $("full-results").hidden = mode !== "full";

  for (const name of ["quick", "full"]) {
    $(name + "-tab").classList.toggle("active", name === mode);
    $(name + "-tab").setAttribute("aria-pressed", String(name === mode));
  }

  calculate(false);
}

function renderContracts() {
  const container = $("contracts");
  container.replaceChildren();
  const search = $("search").value.trim().toLowerCase();

  for (const entry of market.catalog) {
    const item = market.instruments.find(i => i.asset === entry.asset);
    const ticker = item ? item.ticker : entry.asset;

    if (!(ticker + " " + entry.name).toLowerCase().includes(search)) continue;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "contract" +
      (selectedAsset === entry.asset ? " active" : "");
    button.setAttribute("aria-pressed", String(selectedAsset === entry.asset));

    const title = document.createElement("strong");
    title.textContent = ticker;

    const description = document.createElement("small");
    description.textContent = entry.name;

    const price = document.createElement("small");
    price.textContent = item
      ? (item.last === null ? "Нет последней цены" : fmt(item.last, 8))
      : "Данные временно недоступны";

    button.append(title, description, price);
    button.onclick = () => {
      invalidate();
      selectedAsset = entry.asset;
      for (const id of ["entry", "tp", "sl"]) $(id).value = "";
      renderContracts();
      renderInstrument();
      calculate(false);
    };
    container.append(button);
  }
}

function renderInstrument() {
  const item = instrument();
  $("selected-title").textContent = item
    ? item.ticker + " · " + item.name
    : "Параметры временно недоступны";

  $("last").textContent = item ? fmt(item.last, 8) : "—";
  $("step").textContent = item ? fmt(item.step, 8) : "—";
  $("cost").textContent = item ? rub(item.cost) : "—";
  $("unit-margin").textContent = item ? rub(item.margin) : "—";
  $("expiry").textContent = item
    ? item.expiry.split("-").reverse().join(".") : "—";

  $("oi").textContent = item && item.oi !== null
    ? fmt(item.oi, 0) + " шт." : "—";
  $("volume").textContent = item && item.volume !== null
    ? fmt(item.volume, 0) + " шт." : "—";

  $("calculate").disabled = !item;
  $("use-last").disabled = !item || item.last === null;

  if (!item) {
    $("quote-state").textContent = "";
    return;
  }

  const fetched = new Date(item.fetched_at).toLocaleString("ru-RU");
  const quote = [item.quote_date, item.quote_time].filter(Boolean).join(" ");

  $("quote-state").textContent =
    (item.cached ? "Сохранённые рыночные данные. " : "") +
    "Получены: " + fetched +
    (quote ? ". Время источника: " + quote + "." : "");
}

async function request(url, options = {}) {
  const response = await fetch(url, {...options, cache: "no-store"});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Ошибка сервера");
  return result;
}

async function updateMarket() {
  if (marketBusy) return;

  marketBusy = true;
  $("refresh").disabled = true;
  $("refresh").textContent = "Обновление…";
  marketController = new AbortController();
  const timeout = setTimeout(() => marketController.abort(), 20000);

  try {
    const oldTicker = instrument()?.ticker;
    const data = await request("/api/market", {
      signal: marketController.signal
    });

    market = data;
    const nextTicker = instrument()?.ticker;

    // Цены входа старой серии не переносятся в новую.
    if (oldTicker && nextTicker && oldTicker !== nextTicker) {
      invalidate();
      for (const id of ["entry", "tp", "sl"]) $(id).value = "";
    }

    message("market-warning", data.warning);
    renderContracts();
    renderInstrument();
    await calculate(mode === "full" && fullCalculated);
  } catch (error) {
    message(
      "market-warning",
      "Не удалось обновить данные. Показаны последние полученные значения. " +
      "Повторная попытка выполняется автоматически."
    );
  } finally {
    clearTimeout(timeout);
    marketBusy = false;
    $("refresh").disabled = false;
    $("refresh").textContent = "Обновить";
  }
}

async function calculate(full = false) {
  const item = instrument();

  if (!item) {
    for (const id of ["position", "margin", "oi-value"]) {
      $(id).textContent = "—";
    }
    clearFull();
    return;
  }

  if (calcController) calcController.abort();
  calcController = new AbortController();
  const controller = calcController;
  const version = ++calcVersion;
  const timeout = setTimeout(() => controller.abort(), 20000);

  const payload = {
    ticker: item.ticker,
    count: $("count").value,
    mode: full ? "full" : "quick",
    direction: $("direction").value,
    entry: $("entry").value,
    tp: $("tp").value,
    sl: $("sl").value
  };

  try {
    const result = await request("/api/calculate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
      signal: controller.signal
    });

    if (version !== calcVersion) return;

    $("position").textContent = rub(result.position);
    $("margin").textContent = rub(result.margin);
    $("oi-value").textContent = rub(result.oi_value);
    message("calc-error", "");

    if (full) {
      $("profit").textContent = "+" + rub(result.profit);
      $("loss").textContent = "−" + rub(result.loss);
      $("steps").textContent =
        fmt(result.steps_tp, 1) + " / " + fmt(result.steps_sl, 1);
      $("rr").textContent = fmt(result.rr) + " : 1";
      $("risk").textContent = fmt(result.risk) + "%";
      fullCalculated = true;
    }
  } catch (error) {
    if (version !== calcVersion) return;

    $("position").textContent = "—";
    $("margin").textContent = "—";
    $("oi-value").textContent = "—";
    clearFull();
    fullCalculated = false;

    message(
      "calc-error",
      error.name === "AbortError"
        ? "Сервер не ответил вовремя. Попробуйте ещё раз."
        : error.message
    );
  } finally {
    clearTimeout(timeout);
  }
}

$("refresh").onclick = updateMarket;
$("search").oninput = renderContracts;
$("quick-tab").onclick = () => setMode("quick");
$("full-tab").onclick = () => setMode("full");

$("form").onsubmit = event => {
  event.preventDefault();
  clearTimeout(debounce);
  calculate(mode === "full");
};

$("count").oninput = () => {
  invalidate();
  $("position").textContent = "—";
  $("margin").textContent = "—";
  clearTimeout(debounce);
  debounce = setTimeout(() => calculate(false), 350);
};

for (const id of ["entry", "tp", "sl"]) {
  $(id).oninput = invalidate;
}

$("direction").onchange = () => {
  invalidate();
  $("direction-hint").textContent = $("direction").value === "LONG"
    ? "Для LONG: TP выше входа, SL ниже входа."
    : "Для SHORT: TP ниже входа, SL выше входа.";
};

$("use-last").onclick = () => {
  const item = instrument();
  if (item && item.last !== null) {
    $("entry").value = item.last;
    invalidate();
  }
};

$("reset").onclick = () => {
  invalidate();
  clearTimeout(debounce);
  $("count").value = "1";
  $("direction").value = "LONG";
  $("direction").onchange();
  for (const id of ["entry", "tp", "sl"]) $(id).value = "";
  calculate(false);
};

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) updateMarket();
});

setInterval(() => {
  if (!document.hidden) updateMarket();
}, 15000);

updateMarket();
</script>
</body>
</html>
"""


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    def respond(status, value, content_type="application/json; charset=utf-8", extra=()):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        headers = [
            ("Content-Type", content_type), ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
        ]
        start_response(status, headers + list(extra))
        return [b"" if method == "HEAD" else body]

    # Health checks reveal no market data and remain available during a MOEX outage.
    if path == "/health" and method in ("GET", "HEAD"):
        return respond("200 OK", {"status": "ok"})

    if SITE_PASSWORD:
        try:
            scheme, encoded = environ.get("HTTP_AUTHORIZATION", "").split(" ", 1)
            if scheme.lower() != "basic":
                raise ValueError("scheme")
            credentials = base64.b64decode(encoded, validate=True)
            expected = (SITE_USERNAME + ":" + SITE_PASSWORD).encode("utf-8")
            authenticated = hmac.compare_digest(credentials, expected)
        except (ValueError, TypeError):
            authenticated = False
        if not authenticated:
            return respond("401 Unauthorized", {"error": "Требуется вход"}, extra=[
                ("WWW-Authenticate", 'Basic realm="Personal futures", charset="UTF-8"')])

    if method in ("GET", "HEAD"):
        if path == "/":
            return respond("200 OK", HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/market":
            return respond("200 OK", get_market())
        if path == "/favicon.ico":
            start_response("204 No Content", [("Cache-Control", "no-store")])
            return []
        return respond("404 Not Found", {"error": "Страница не найдена"})

    if method == "POST" and path == "/api/calculate":
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
            return respond("415 Unsupported Media Type", {"error": "Ожидается JSON"})
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
            if not 0 < length <= 4096:
                return respond("413 Content Too Large", {"error": "Некорректный размер запроса"})
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                raise ValueError("Неполный запрос")
            payload = json.loads(raw)
            result = calculate(payload)
        except (ValueError, InvalidOperation) as error:
            return respond("400 Bad Request", {"error": str(error) if not isinstance(error, json.JSONDecodeError) else "Некорректный JSON"})
        except Exception as error:
            logger.error("Calculation failed: %s", type(error).__name__)
            return respond("503 Service Unavailable", {"error": "Расчёт временно недоступен"})
        return respond("200 OK", result)

    return respond("405 Method Not Allowed", {"error": "Метод не поддерживается"}, extra=[("Allow", "GET, HEAD" if path != "/api/calculate" else "POST")])


def main():
    if (REQUIRE_AUTH or HOST not in ("127.0.0.1", "::1", "localhost")) and not SITE_PASSWORD:
        raise SystemExit("Настройте пароль: python configure.py. Публичный запуск без пароля запрещён.")
    from waitress import create_server
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = create_server(
        application, host=HOST, port=PORT, threads=4, connection_limit=32,
        channel_timeout=30, cleanup_interval=5, max_request_body_size=4096,
        max_request_header_size=16384, expose_tracebacks=False,
        ident="FuturesCalculator", log_socket_errors=False,
    )
    worker = threading.Thread(target=market_worker, name="moex-refresh", daemon=True)
    worker.start()

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    logger.info("Site running at http://%s:%s; authentication=%s", HOST, PORT, bool(SITE_PASSWORD))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.close()
        worker.join(timeout=1)


if __name__ == "__main__":
    main()
