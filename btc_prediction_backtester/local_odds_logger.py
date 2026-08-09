"""Live odds logger for Binance's Prediction Markets REST API.

RUN THIS ON YOUR OWN DEVICE, NOT IN A REMOTE SANDBOX.
Binance's authenticated API (api.binance.com) is geo-blocked from some
cloud/sandbox IP ranges. It must run from your own network, with your own
credentials, which never leave your device.

Setup (on your own machine):
    pip install requests
    export BINANCE_API_KEY="..."
    export BINANCE_API_SECRET="..."
    python3 local_odds_logger.py

WHY REST INSTEAD OF THE WEBSOCKET THIS TIME:
The real docs (developers.binance.com/en/docs/products/w3w-prediction/...)
turned out to describe TWO different websocket channels:
  - wallet-events: push notifications about YOUR OWN orders (buy/sell
    success, fills, claims...) -- NOT market odds. This is what an earlier
    version of this script was accidentally pointed at (REGISTER
    succeeded, but nothing else ever arrived -- because no order events
    were happening).
  - orderbook: the real live-odds channel, topic format
    `web3_prediction_orderbook_{marketId}` where marketId is Binance's
    internal NUMERIC id -- not the "btc-updown-5m-<timestamp>" slug from
    the share URL, which was a wrong guess.
REST is simpler to get right than re-debugging that websocket's auth, and
polling every few seconds is more than enough for our purposes (the
backtest itself only ever used 1-minute granularity). This script:
  1. Finds the current live "BTC Up or Down 5m" market via the market-data
     REST endpoints (search/list) -- prints the RAW response at every step
     so if my guessed field names are wrong, you can see the real ones and
     tell me.
  2. Polls the order-book endpoint for that market's outcome tokens on an
     interval, logging price (implied probability) + timestamp to CSV.
  3. Places NO orders. Read-only.

IMPORTANT CAVEATS (I could not verify these live -- api.binance.com is
geo-blocked from where I run):
  - Exact query parameter names for search/list/detail/order-book are
    educated guesses from the endpoint descriptions, not a captured
    working example. The script tries a couple of variants and prints
    raw responses so you can tell me what actually works.
  - Whether these market-data endpoints need HMAC signing at all is
    unconfirmed (they're not tagged USER_DATA in the docs, which on
    Binance often -- not always -- means a lighter/no auth tier). The
    script signs everything by default since that's always accepted when
    a normal API key is used; flip SIGN_MARKET_DATA_CALLS=False to try
    unsigned if you get a signature-related error on these specific calls.
"""
import csv
import hashlib
import hmac
import json
import math
import os
import statistics
import time
import urllib.parse

try:
    import requests
except ImportError:
    raise SystemExit("Falta la libreria requests. Instala con: pip install requests")

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")
if not API_KEY or not API_SECRET:
    raise SystemExit(
        "Faltan credenciales. Corre:\n"
        "  export BINANCE_API_KEY=...\n"
        "  export BINANCE_API_SECRET=...\n"
        "antes de este script. Nunca las escribas directamente en el codigo."
    )

# Optional -- alerts still print to console and try termux-notification
# even without these. Set both to also get a Telegram message:
#   export TELEGRAM_BOT_TOKEN="..."
#   export TELEGRAM_CHAT_ID="..."
# (ver el README para como conseguir ambos con @BotFather)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Los avisos de este archivo estan APAGADOS por defecto, y no por prudencia:
# sus señales (volume_imbalance_contrarian y compañia) se midieron en vivo y
# dieron 16/50 = 32% de acierto, muy por debajo del azar. win_loss_diagnosis.py
# mostro que no hay ninguna caracteristica que separe sus aciertos de sus
# fallos: la señal esta vacia, no invertida. Seguir mandandolas es ruido con
# formato de recomendacion.
#
# Hoy este archivo sirve para UNA cosa: registrar a cuanto cotiza el mercado
# cada ronda, que es el dato que falta para cerrar el proyecto. Eso lo sigue
# haciendo igual, en los CSV.
#
# Quien avisa por Telegram ahora es predict_next.py, con el modelo medido en
# 54.25% sobre 23,571 llamadas.
#
# SIGNAL_ALERTS=1 las reactiva, si alguna vez hay razon para hacerlo.
SEND_SIGNAL_ALERTS = os.environ.get("SIGNAL_ALERTS", "0") == "1"

# El JSON crudo del libro cada 5 s son ~17,000 lineas por dia. Se guarda igual
# en el CSV, que es donde sirve; en pantalla estorba. VERBOSE=1 lo devuelve.
VERBOSE_SNAPSHOTS = os.environ.get("VERBOSE", "0") == "1"

REST_BASE = "https://api.binance.com"
SIGN_MARKET_DATA_CALLS = True  # flip to False if these specific calls reject the signature
POLL_INTERVAL_SECONDS = 5

# One silent "nothing here" message per round (~288/day). Set
# SEND_STATUS_EVERY_ROUND=0 to keep only the real opportunity alerts.
SEND_STATUS_EVERY_ROUND = os.environ.get("SEND_STATUS_EVERY_ROUND", "1") != "0"

# Observation mode, ON by default. The first 50 graded live predictions came in
# at 32% (16/50) -- 2.5 standard deviations BELOW a coin flip, p=0.0055. That is
# not "no edge found", it is evidence the signals are inverted under live
# conditions, and the backtest gives no reason to expect it (51.2% even in
# strong uptrends). Until live accuracy recovers above 50% on a real sample,
# alerts are labelled as recorded-not-actionable.
# Set PAPER_MODE=0 to present them as actionable again.
PAPER_MODE = os.environ.get("PAPER_MODE", "1") != "0"

LOG_FILE = os.path.join(os.path.dirname(__file__), "data", "live_odds_log.csv")
DEBUG_LOG_FILE = os.path.join(os.path.dirname(__file__), "data", "raw_api_responses.jsonl")


def _sign(params: dict) -> str:
    query_string = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(
        API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def api_get(path: str, params: dict = None, signed: bool = None):
    """GET against the Binance REST API. Logs the raw response for debugging
    regardless of success/failure -- we're reverse-engineering the exact
    param/response shape live, so keep everything."""
    params = dict(params or {})
    if signed is None:
        signed = SIGN_MARKET_DATA_CALLS
    if signed:
        params["timestamp"] = str(int(time.time() * 1000))
        params["recvWindow"] = "5000"
        query_string = urllib.parse.urlencode(sorted(params.items()))
        signature = _sign(params)
        url = f"{REST_BASE}{path}?{query_string}&signature={signature}"
    else:
        query_string = urllib.parse.urlencode(params)
        url = f"{REST_BASE}{path}" + (f"?{query_string}" if query_string else "")

    headers = {"X-MBX-APIKEY": API_KEY}
    resp = requests.get(url, headers=headers, timeout=10)

    os.makedirs(os.path.dirname(DEBUG_LOG_FILE), exist_ok=True)
    with open(DEBUG_LOG_FILE, "a") as f:
        f.write(
            json.dumps(
                {
                    "at_ms": int(time.time() * 1000),
                    "path": path,
                    "params": {k: v for k, v in params.items() if k not in ("timestamp", "recvWindow")},
                    "status": resp.status_code,
                    "body": resp.text[:20000],
                }
            )
            + "\n"
        )

    print(f"[api] GET {path} -> {resp.status_code}")
    if resp.status_code != 200:
        print(f"       {resp.text[:500]}")
    return resp


def _first_present(d: dict, candidates):
    for c in candidates:
        if isinstance(d, dict) and c in d:
            return d[c]
    return None


def find_btc_5m_market():
    """Locate the live 'BTC Up or Down 5m' round and return its numeric
    marketId (plus the raw topic dict, for inspection).

    Confirmed real schema (market/search returns -1022 regardless of
    param name tried -- skip it, market/list works fine unsigned-or-not):

        {"marketTopics": [{
            "marketTopicId": 4455880,        # the recurring "5m game" itself
            "slug": "btc-updown-5m-<ts>",
            "title": "BTC Up or Down 5m",
            "feeRateBps": 200,                # 200 bps = 2% -- the REAL fee
            "slippageBps": 1000,               # 1000 bps = 10% max slippage
                                                # tolerance (this is almost
                                                # certainly the "Automatico |
                                                # 10%" text seen in the app UI
                                                # -- NOT a fee, as first guessed)
            "variantData": {"priceFeedProvider": "CHAINLINK", ...},
            "markets": [{
                "marketId": 6815131,           # <-- THIS is what order-book wants
                "externalId": "1113650",
                "conditionId": "0x...",
                "status": "REGISTERED",
                ...                             # likely has an outcomes/tokens list
            }]
        }]}
    """
    print("\n=== Paso 1: buscando el mercado 'BTC Up or Down 5m' ===")
    resp = api_get("/sapi/v1/w3w/wallet/prediction/market/list", {"limit": 50})
    if resp.status_code != 200:
        print("market/list no respondio 200. Revisa el error de arriba.")
        return None, None, None

    try:
        data = resp.json()
    except ValueError:
        print("Respuesta no es JSON valido:", resp.text[:500])
        return None, None, None

    topics = data.get("marketTopics") if isinstance(data, dict) else None
    if not isinstance(topics, list):
        print("No encontre 'marketTopics' en la respuesta. JSON crudo:")
        print(json.dumps(data, indent=2)[:3000])
        return None, None, None

    topic = None
    for t in topics:
        title = str(_first_present(t, ("title", "name", "question")) or "")
        if "btc" in title.lower() and "up or down" in title.lower():
            topic = t
            break

    if not topic:
        print(f"No encontre un topic 'BTC Up or Down' entre {len(topics)} topics devueltos.")
        titles = [t.get("title") for t in topics if isinstance(t, dict)]
        print("Titulos disponibles:", titles)
        return None, None, None

    fee_bps = topic.get("feeRateBps")
    slippage_bps = topic.get("slippageBps")
    print(f"\nTopic encontrado: {topic.get('title')} (marketTopicId={topic.get('marketTopicId')})")
    print(f"  feeRateBps={fee_bps} (-> {fee_bps/100 if fee_bps is not None else '?'}% fee real)")
    print(f"  slippageBps={slippage_bps} (-> {slippage_bps/100 if slippage_bps is not None else '?'}% slippage tolerance)")

    markets = topic.get("markets")
    if not isinstance(markets, list) or not markets:
        print("El topic no trae una lista 'markets' con la ronda actual. JSON crudo del topic:")
        print(json.dumps(topic, indent=2)[:3000])
        return None, None, None

    current_round = markets[0]
    market_id = current_round.get("marketId")
    print(f"\nRonda actual: {current_round.get('title')}")
    print(f"marketId extraido: {market_id}  (status={current_round.get('status')})")

    # market/list already includes live price/chance/tokenId per outcome --
    # no need for a separate market/detail call at all.
    outcomes = current_round.get("outcomes")
    if isinstance(outcomes, list):
        print("Outcomes (ya trae precio/probabilidad en vivo):")
        for o in outcomes:
            print(f"  {o.get('name')}: price={o.get('price')} chance={o.get('chance')} tokenId={o.get('tokenId')}")

    return market_id, current_round, topic


def get_order_book(market_id, vendor, token_id=None, condition_id=None):
    # order-book's required params, discovered one at a time from live
    # -3026 "required parameter X not present" errors: marketId alone
    # wasn't enough, it also wants `vendor`. Passing everything we have
    # (tokenId, conditionId) defensively in case another one turns out to
    # be required too -- extra/unused params shouldn't hurt.
    params = {"marketId": market_id, "vendor": vendor}
    if token_id:
        params["tokenId"] = token_id
    if condition_id:
        params["conditionId"] = condition_id
    return api_get("/sapi/v1/w3w/wallet/prediction/order-book", params)


LOG_HEADER = [
    "polled_at_ms",
    "market_id",
    "http_status",
    "api_timestamp",
    "best_bid",
    "best_ask",
    "mid_price",
    "btc_price",
    "round_start_price",
    "price_gap_usd",
    "raw_body",
]


def ensure_log_file():
    """Create the odds log, or migrate one written before columns were added.

    Same failure that hit round_outcomes.csv: without this, DictReader keys
    every row off the original short header and silently drops the newer
    columns into the restkey, so analysis over the collected history reads
    back empty. Fixed there but missed here, which cost a full analysis pass
    over 6,980 already-collected snapshots.
    """
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(LOG_HEADER)
        return

    with open(LOG_FILE, newline="") as f:
        first = f.readline()
    if not first.strip():
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(LOG_HEADER)
        return

    existing = next(csv.reader([first]), [])
    if existing == LOG_HEADER:
        return

    with open(LOG_FILE, newline="") as f:
        rows = list(csv.reader(f))

    width = len(LOG_HEADER)
    body = rows[1:]
    # Older rows are shorter and end with raw_body; pad the middle so the JSON
    # stays in the last column rather than sliding into a numeric field.
    migrated = [LOG_HEADER]
    for r in body:
        if len(r) == width:
            migrated.append(r)
        elif len(r) < width:
            migrated.append(r[:-1] + [""] * (width - len(r)) + [r[-1]])
    with open(LOG_FILE, "w", newline="") as f:
        csv.writer(f).writerows(migrated)
    print(f"[migracion] live_odds_log.csv actualizado a {width} columnas ({len(migrated)-1} filas)")


def _best_bid_ask(order_book_json):
    """bids are sorted highest-first, asks lowest-first per the docs."""
    bids = order_book_json.get("bids") or []
    asks = order_book_json.get("asks") or []
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    return best_bid, best_ask


def _book_depth(order_book_json):
    """Size resting at the very top of each side.

    A fresh round's book is often nearly empty, and then the 'best' price is
    whatever lone order happens to sit there -- not a market price. Live data
    showed Down quoted anywhere from 0.23 to 0.71 across rounds where BTC had
    not moved at all, a 48-point spread for an identical state. Prices from a
    book that thin cannot support an EV calculation.
    """
    bids = order_book_json.get("bids") or []
    asks = order_book_json.get("asks") or []
    bid_size = float(bids[0]["size"]) if bids else 0.0
    ask_size = float(asks[0]["size"]) if asks else 0.0
    return bid_size, ask_size


def get_live_btc_price():
    """Current BTCUSDT price from the same public, unauthenticated mirror
    used elsewhere in this project -- this is what lets us log the same
    "Precio actual -$X.XX" gap the app shows, independent of Binance's
    private API (and of whatever exact oracle price the app itself uses)."""
    try:
        resp = requests.get(
            "https://data-api.binance.vision/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=10,
        )
        return float(resp.json()["price"])
    except Exception as exc:
        print(f"[price-error] {exc}")
        return None


def get_round_start_price(round_start_ms, retries=3, delay_seconds=2):
    """Open price of the 1m candle at the round's start boundary -- same
    field resolve_round_outcome() uses for the end boundary, just fetched
    right away instead of waiting for the round to finish."""
    for attempt in range(retries):
        try:
            resp = requests.get(
                "https://data-api.binance.vision/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "startTime": round_start_ms, "limit": 1},
                timeout=10,
            )
            candles = resp.json()
            if isinstance(candles, list) and candles and candles[0][0] == round_start_ms:
                return float(candles[0][1])
        except Exception as exc:
            print(f"[start-price-error] intento {attempt + 1}/{retries}: {exc}")
        time.sleep(delay_seconds)
    return None


def log_snapshot(market_id, resp, round_start_price):
    api_ts = best_bid = best_ask = mid = None
    if resp.status_code == 200:
        try:
            body = resp.json()
            api_ts = body.get("timestamp")
            best_bid, best_ask = _best_bid_ask(body)
            if best_bid is not None and best_ask is not None:
                mid = round((best_bid + best_ask) / 2, 4)
        except (ValueError, KeyError, IndexError):
            pass

    btc_price = get_live_btc_price()
    price_gap_usd = round(btc_price - round_start_price, 2) if (btc_price is not None and round_start_price is not None) else None

    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                int(time.time() * 1000),
                market_id,
                resp.status_code,
                api_ts,
                best_bid,
                best_ask,
                mid,
                btc_price,
                round_start_price,
                price_gap_usd,
                resp.text[:2000],
            ]
        )
    return btc_price, price_gap_usd


OUTCOMES_FILE = os.path.join(os.path.dirname(__file__), "data", "round_outcomes.csv")


OUTCOMES_HEADER = [
    "market_id",
    "round_start_ms",
    "round_end_ms",
    "start_price",
    "end_price",
    "outcome",
    "market_prob_up",
    "predicted_side",
    "predicted_reasoning",
    "signal_correct",
    "signal_name",
    "entry_price",
]


def ensure_outcomes_file():
    """Create the outcomes CSV, or migrate one written before the prediction
    columns existed.

    Without the migration, DictReader keys every row off the stale 6-column
    header, so the 4 newer values (including signal_correct) land in the
    restkey and are invisible -- which is why accuracy kept reporting "no
    graded predictions yet" even right after grading one.
    """
    os.makedirs(os.path.dirname(OUTCOMES_FILE), exist_ok=True)
    if not os.path.exists(OUTCOMES_FILE):
        with open(OUTCOMES_FILE, "w", newline="") as f:
            csv.writer(f).writerow(OUTCOMES_HEADER)
        return

    with open(OUTCOMES_FILE, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        with open(OUTCOMES_FILE, "w", newline="") as f:
            csv.writer(f).writerow(OUTCOMES_HEADER)
        return
    if rows[0] == OUTCOMES_HEADER:
        return

    width = len(OUTCOMES_HEADER)
    migrated = [OUTCOMES_HEADER] + [r + [""] * (width - len(r)) for r in rows[1:] if len(r) <= width]
    with open(OUTCOMES_FILE, "w", newline="") as f:
        csv.writer(f).writerows(migrated)
    print(f"[migracion] round_outcomes.csv actualizado a {width} columnas ({len(migrated)-1} filas conservadas)")


def compute_running_accuracy(last_n=50):
    """Honest accuracy tracking -- NOT a self-learning model. With the
    small sample this script can realistically gather, anything fancier
    would just be fitting noise. This only counts predictions that had a
    definite Up/Down call (market_side ties are skipped)."""
    if not os.path.exists(OUTCOMES_FILE):
        return "Sin historial todavia."
    with open(OUTCOMES_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    graded = [r for r in rows if r.get("signal_correct") in ("True", "False")]
    graded = graded[-last_n:]
    if not graded:
        return "Sin predicciones evaluadas todavia."
    correct = sum(1 for r in graded if r["signal_correct"] == "True")
    total = len(graded)
    return f"Precision acumulada: {correct}/{total} ({correct/total*100:.1f}%) en las ultimas {total} predicciones."


def accuracy_by_signal(min_samples=5):
    """Which signal is actually failing.

    The aggregate number says the system is losing; it cannot say which part.
    This is the only kind of learning worth doing on a sample this size --
    attributing outcomes to the signal that produced them. Adjusting weights
    on a few dozen rounds would just be fitting noise.
    """
    if not os.path.exists(OUTCOMES_FILE):
        return ""
    with open(OUTCOMES_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    per_signal = {}
    for r in rows:
        if r.get("signal_correct") not in ("True", "False"):
            continue
        name = r.get("signal_name") or "(sin registrar)"
        hits, total = per_signal.get(name, (0, 0))
        per_signal[name] = (hits + (r["signal_correct"] == "True"), total + 1)

    lines = []
    for name, (hits, total) in sorted(per_signal.items(), key=lambda kv: -kv[1][1]):
        if total < min_samples:
            continue
        lines.append(f"  {name}: {hits}/{total} ({hits/total*100:.0f}%)")
    return "\n".join(lines)


def resolve_round_outcome(round_start_ms, retries=10, delay_seconds=5):
    """Determine Up/Down for a finished round using the same public,
    unauthenticated Binance klines endpoint as data_fetch.py/backtest.py --
    no need to guess the private API's own "resolved market" schema.
    Retries because the round's last 1m candle may not exist yet right at
    the boundary (data-api.binance.vision closes candles with a small lag)."""
    round_end_ms = round_start_ms + ROUND_SECONDS * 1000
    for attempt in range(retries):
        try:
            resp = requests.get(
                "https://data-api.binance.vision/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "startTime": round_start_ms, "limit": 6},
                timeout=10,
            )
            candles = resp.json()
            by_open_time = {c[0]: c for c in candles} if isinstance(candles, list) else {}
            start_candle = by_open_time.get(round_start_ms)
            end_candle = by_open_time.get(round_end_ms)
            if start_candle and end_candle:
                start_price = float(start_candle[1])  # open price
                end_price = float(end_candle[1])  # open price
                outcome = "Up" if end_price > start_price else ("Down" if end_price < start_price else "Tie")
                return start_price, end_price, outcome
        except Exception as exc:
            print(f"[resolve-error] intento {attempt + 1}/{retries}: {exc}")
        time.sleep(delay_seconds)
    return None, None, None


def log_round_outcome(
    market_id,
    round_start_ms,
    market_prob_up=None,
    predicted_side=None,
    predicted_reasoning=None,
    signal_name=None,
    entry_price=None,
):
    ensure_outcomes_file()
    round_end_ms = round_start_ms + ROUND_SECONDS * 1000
    start_price, end_price, outcome = resolve_round_outcome(round_start_ms)

    signal_correct = None
    if predicted_side and outcome in ("Up", "Down"):
        signal_correct = predicted_side == outcome

    with open(OUTCOMES_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                market_id,
                round_start_ms,
                round_end_ms,
                start_price,
                end_price,
                outcome,
                market_prob_up,
                predicted_side,
                predicted_reasoning,
                signal_correct,
                signal_name,
                entry_price,
            ]
        )

    if outcome:
        print(f"[outcome] market_id={market_id} -> {outcome} (${start_price:,.2f} -> ${end_price:,.2f})")
    else:
        print(f"[outcome] market_id={market_id} -> no se pudo resolver (klines no disponibles todavia)")

    if predicted_side and outcome in ("Up", "Down"):
        icon = "✅" if signal_correct else "❌"
        result_text = "ACERTÓ" if signal_correct else "FALLÓ"
        move = end_price - start_price
        arrow = "📈" if move >= 0 else "📉"
        send_notification(
            f"Ronda {market_id}: {result_text}",
            f"{icon} <b>{result_text}</b> · ronda <code>{market_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Predijimos: <b>{predicted_side}</b>\n"
            f"Resultado:  <b>{outcome}</b>\n\n"
            f"{arrow} <b>Bitcoin</b>\n"
            f"${start_price:,.2f} → ${end_price:,.2f}\n"
            f"Cerró en <b>{fmt_gap(move)}</b>\n\n"
            f"📋 {compute_running_accuracy()}"
            + (f"\n\n<b>Por señal:</b>\n<code>{por_senal}</code>" if (por_senal := accuracy_by_signal()) else ""),
        )
    return outcome


def load_recent_outcomes(limit=10):
    """Preload the last few resolved outcomes from disk so the streak
    signal has context immediately on startup, not just from outcomes
    resolved during this particular run."""
    if not os.path.exists(OUTCOMES_FILE):
        return []
    with open(OUTCOMES_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    outcomes = [r["outcome"] for r in rows if r.get("outcome") in ("Up", "Down")]
    return outcomes[-limit:]


def send_telegram(html_body, silent=False):
    """HTML parse_mode rather than Markdown: these messages are full of
    parentheses, dashes, underscores and $ signs, which Markdown silently
    mangles or rejects outright.

    `silent` maps to Telegram's disable_notification -- the routine
    once-per-round status lands without sound or vibration, so only the
    actual opportunities buzz the phone."""
    if not SEND_SIGNAL_ALERTS:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": html_body,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "disable_notification": "true" if silent else "false",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[telegram-error] {resp.status_code} {resp.text[:200]}")
    except Exception as exc:
        print(f"[telegram-error] {exc}")


def _strip_html(text):
    import re

    return re.sub(r"<[^>]+>", "", text).replace("&amp;", "&")


def send_notification(title, html_body, silent=False):
    """Alerts via three independent channels -- console (always), Telegram
    (if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set) and termux-notification
    (if available). Never raises -- a missing/broken channel must not
    interrupt the polling loop; each is wrapped so the others still fire."""
    plain = _strip_html(html_body)
    if not SEND_SIGNAL_ALERTS:
        # Imprimir el bloque entero de una señal desacreditada llena la terminal
        # y tapa lo unico que importa ahora, que es la recoleccion de precios.
        # Queda una linea para saber que el logger sigue vivo.
        primera = next((x for x in plain.splitlines() if x.strip()), "")
        print(f"[señal-vieja/silenciada] {primera[:70]}")
        return
    print(f"\n{'=' * 60}\n{plain}\n{'=' * 60}\n")
    send_telegram(html_body, silent=silent)
    if silent:
        return  # routine status: Telegram + console are enough, don't buzz twice
    try:
        import subprocess

        subprocess.run(
            ["termux-notification", "--title", title, "--content", plain[:400]],
            timeout=5,
            check=False,
        )
    except Exception:
        pass  # no termux-api installed, or not on Termux -- console/telegram alerts still fire


def fmt_gap(gap):
    """The app shows the round's move as a signed dollar figure (e.g.
    "-$1.54"); mirror that exactly so the alert and the screen agree."""
    if gap is None:
        return "s/d"
    return f"+${gap:,.2f}" if gap >= 0 else f"-${abs(gap):,.2f}"


def get_pre_round_candles(round_start_ms, minutes=30, retries=3, delay_seconds=2):
    """1-minute candles from `minutes` before the round's open through the
    open itself. Same public klines endpoint as everything else here.

    This is what feeds the price-move and order-flow signals: information
    the app never shows you, unlike the Up/Down percentage.
    """
    for attempt in range(retries):
        try:
            resp = requests.get(
                "https://data-api.binance.vision/api/v3/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "startTime": round_start_ms - minutes * 60_000,
                    "limit": minutes + 1,
                },
                timeout=10,
            )
            candles = resp.json()
            if isinstance(candles, list) and candles:
                return {c[0]: c for c in candles}
        except Exception as exc:
            print(f"[pre-round-error] intento {attempt + 1}/{retries}: {exc}")
        time.sleep(delay_seconds)
    return {}


def _pct_move_before(by_time, round_start_ms, minutes):
    """% price move over the `minutes` leading into the round, using OPEN
    prices at both ends -- the same convention backtest.py used, so the
    win rates measured there actually apply to what we compute here."""
    a = by_time.get(round_start_ms - minutes * 60_000)
    b = by_time.get(round_start_ms)
    if not a or not b:
        return None
    start, end = float(a[1]), float(b[1])
    return None if start == 0 else (end - start) / start * 100


def _taker_buy_ratio_before(by_time, round_start_ms, minutes):
    """Share of volume that was aggressive BUYing in the `minutes` before
    the round. Kline index 5 = volume, 9 = taker buy base volume."""
    vol = buy = 0.0
    for m in range(minutes, 0, -1):
        c = by_time.get(round_start_ms - m * 60_000)
        if c:
            vol += float(c[5])
            buy += float(c[9])
    return None if vol <= 0 else buy / vol


# The candle-shape reads below mirror MarketContext in strategies.py exactly
# -- same windows, same OPEN-price convention, same formulas. They have to,
# or the win rates measured in the backtest would not describe what runs here.
# Kline indices: 1 = open, 2 = high, 3 = low.


def _window_before(by_time, round_start_ms, minutes):
    return [
        by_time[round_start_ms - m * 60_000]
        for m in range(minutes, 0, -1)
        if (round_start_ms - m * 60_000) in by_time
    ]


def _open_at(by_time, ms):
    c = by_time.get(ms)
    return float(c[1]) if c else None


def _close_position_before(by_time, round_start_ms, minutes=5):
    win = _window_before(by_time, round_start_ms, minutes)
    end = _open_at(by_time, round_start_ms)
    if not win or end is None:
        return None
    high = max(float(c[2]) for c in win)
    low = min(float(c[3]) for c in win)
    return None if high <= low else (end - low) / (high - low)


def _path_efficiency_before(by_time, round_start_ms, minutes=5):
    win = _window_before(by_time, round_start_ms, minutes)
    end = _open_at(by_time, round_start_ms)
    if len(win) < 2 or end is None:
        return None
    opens = [float(c[1]) for c in win] + [end]
    net = abs(opens[-1] - opens[0])
    travel = sum(abs(opens[i] - opens[i - 1]) for i in range(1, len(opens)))
    return None if travel == 0 else net / travel


def _consecutive_direction_before(by_time, round_start_ms, max_look=6):
    opens = []
    for m in range(max_look, -1, -1):
        p = _open_at(by_time, round_start_ms - m * 60_000)
        if p is None:
            return None
        opens.append(p)
    moves = [opens[i] - opens[i - 1] for i in range(1, len(opens))]
    if not moves or moves[-1] == 0:
        return None
    direction = 1 if moves[-1] > 0 else -1
    count = 0
    for mv in reversed(moves):
        if (mv > 0 and direction == 1) or (mv < 0 and direction == -1):
            count += 1
        else:
            break
    return direction, count


def _distance_from_mean_before(by_time, round_start_ms, minutes=20):
    win = _window_before(by_time, round_start_ms, minutes)
    end = _open_at(by_time, round_start_ms)
    if len(win) < 5 or end is None:
        return None
    opens = [float(c[1]) for c in win]
    sd = statistics.pstdev(opens)
    return None if sd == 0 else (end - statistics.fmean(opens)) / sd


def _range_pct_before(by_time, round_start_ms, minutes=5):
    win = _window_before(by_time, round_start_ms, minutes)
    if not win:
        return None
    high = max(float(c[2]) for c in win)
    low = min(float(c[3]) for c in win)
    return None if low <= 0 else (high - low) / low * 100


# Every entry is a strategy that cleared breakeven in backtest.py's 180-day
# run at the REAL 2% fee, with `q` set to the LOWER bound of its 95% CI --
# never the midpoint, which would overstate the edge systematically.
#
# Deliberately excluded: streak_reversion_4 and _5. They looked strong in an
# earlier run, but in the corrected open-price backtest at 2% fee their CI
# lower bound no longer clears breakeven, so betting them is not justified.
_SIGNAL_SPECS = [
    # (name, q, kind, params) -- q = CI95% lower bound, out-of-sample, fee 2%
    ("micro_mean_reversion_3m", 0.522, "revert", {"minutes": 3, "min_move_pct": 0.05}),
    ("volume_imbalance_contrarian_15m", 0.519, "flow", {"minutes": 15, "threshold": 0.65}),
    ("precio_estirado_2sd", 0.519, "stretched", {"minutes": 20, "min_z": 2.0}),
    ("volume_imbalance_contrarian_3m", 0.518, "flow", {"minutes": 3, "threshold": 0.65}),
    ("volume_imbalance_contrarian_5m", 0.518, "flow", {"minutes": 5, "threshold": 0.65}),
    ("micro_mean_reversion_1m", 0.517, "revert", {"minutes": 1, "min_move_pct": 0.05}),
    ("movimiento_limpio", 0.516, "efficient", {"minutes": 5, "min_efficiency": 0.6, "min_move_pct": 0.05}),
    ("precio_estirado_1sd", 0.516, "stretched", {"minutes": 20, "min_z": 1.0}),
    ("cierre_en_extremo", 0.513, "close_pos", {"minutes": 5, "threshold": 0.8}),
    ("rango_ancho", 0.513, "wide_range", {"minutes": 5, "min_range_pct": 0.15}),
    ("micro_mean_reversion_5m", 0.513, "revert", {"minutes": 5, "min_move_pct": 0.05}),
    ("racha_velas_1m", 0.511, "candle_run", {"min_run": 3}),
    ("streak_reversion_3", 0.511, "streak", {"k": 3}),
    ("contrarian_last_1", 0.509, "streak", {"k": 1}),
]

# Tested but NOT wired up, because the data said no:
#   close_position_continuation  47.9% -- the "closing strong carries" read is
#                                simply wrong here; its mirror wins instead
#   choppy_move_reversion        51.3%, CI [48.5, 54.0] -- includes 50%
#   last_minute_reversal_follow  50.3% -- no signal at all
#   consecutive_candle_reversion_5  CI [49.0, 54.3] -- too few cases to trust
#
# Caveat that applies to the whole table: 77 strategies were tested, so at 95%
# confidence a handful of passes are expected from chance alone. These variants
# are also heavily correlated (all measure the same reversion effect the
# variance-ratio test found), so this is nowhere near 77 independent tests.
# The ones kept here have margin above breakeven rather than barely clearing it,
# but the live accuracy counter -- not the backtest -- is what settles it.


def collect_signals(recent_outcomes, by_time, round_start_ms):
    """Evaluate every backtested strategy against the current round.

    Returns them sorted strongest-first. Note these are NOT independent
    confirmations of each other -- they all measure the same short-horizon
    mean-reversion effect the variance-ratio test picked up, so agreement
    between them must not be compounded into a higher probability.
    """
    fired = []
    for name, q, kind, params in _SIGNAL_SPECS:
        side = detail = None

        if kind == "streak":
            k = params["k"]
            if len(recent_outcomes) >= k:
                window = recent_outcomes[-k:]
                if len(set(window)) == 1:
                    side = "Down" if window[0] == "Up" else "Up"
                    detail = f"{k} ronda(s) seguidas {window[0]}"

        elif kind == "revert":
            pct = _pct_move_before(by_time, round_start_ms, params["minutes"])
            if pct is not None and abs(pct) >= params["min_move_pct"]:
                side = "Down" if pct > 0 else "Up"
                detail = f"BTC movio {pct:+.3f}% en {params['minutes']}min previos"

        elif kind == "flow":
            ratio = _taker_buy_ratio_before(by_time, round_start_ms, params["minutes"])
            if ratio is not None:
                if ratio >= params["threshold"]:
                    side, detail = "Down", f"{ratio*100:.0f}% del volumen fue compra agresiva ({params['minutes']}min)"
                elif ratio <= 1 - params["threshold"]:
                    side, detail = "Up", f"{ratio*100:.0f}% del volumen fue compra agresiva ({params['minutes']}min)"

        elif kind == "stretched":
            z = _distance_from_mean_before(by_time, round_start_ms, params["minutes"])
            if z is not None and abs(z) >= params["min_z"]:
                side = "Down" if z > 0 else "Up"
                detail = f"precio a {z:+.1f} desviaciones de su media de {params['minutes']}min"

        elif kind == "efficient":
            eff = _path_efficiency_before(by_time, round_start_ms, params["minutes"])
            pct = _pct_move_before(by_time, round_start_ms, params["minutes"])
            if (
                eff is not None
                and pct is not None
                and eff >= params["min_efficiency"]
                and abs(pct) >= params["min_move_pct"]
            ):
                side = "Down" if pct > 0 else "Up"
                detail = f"movimiento limpio de {pct:+.3f}% (eficiencia {eff:.0%}) en {params['minutes']}min"

        elif kind == "close_pos":
            pos = _close_position_before(by_time, round_start_ms, params["minutes"])
            if pos is not None:
                if pos >= params["threshold"]:
                    side, detail = "Down", f"cerro en el {pos:.0%} superior de su rango"
                elif pos <= 1 - params["threshold"]:
                    side, detail = "Up", f"cerro en el {1-pos:.0%} inferior de su rango"

        elif kind == "wide_range":
            rng = _range_pct_before(by_time, round_start_ms, params["minutes"])
            pct = _pct_move_before(by_time, round_start_ms, params["minutes"])
            if rng is not None and pct is not None and rng >= params["min_range_pct"] and pct != 0:
                side = "Down" if pct > 0 else "Up"
                detail = f"rango ancho {rng:.2f}% con movimiento {pct:+.3f}%"

        elif kind == "candle_run":
            res = _consecutive_direction_before(by_time, round_start_ms)
            if res is not None:
                direction, count = res
                if count >= params["min_run"]:
                    side = "Down" if direction == 1 else "Up"
                    detail = f"{count} velas de 1min seguidas {'subiendo' if direction == 1 else 'bajando'}"

        if side:
            fired.append({"name": name, "q": q, "side": side, "detail": detail})

    fired.sort(key=lambda s: s["q"], reverse=True)
    return fired


def pick_signal(fired):
    """Use the single strongest fired signal, never a blend.

    Combining correlated signals into one number would mean inventing a
    probability no backtest ever measured. Taking the best single one keeps
    `q` exactly equal to a figure that was actually validated; the others
    are reported as context so agreement or disagreement is visible.
    """
    if not fired:
        return None
    best = fired[0]
    agree = [s for s in fired[1:] if s["side"] == best["side"]]
    disagree = [s for s in fired[1:] if s["side"] != best["side"]]
    return {
        "suggested_side": best["side"],
        "q": best["q"],
        "name": best["name"],
        "detail": best["detail"],
        "agree": agree,
        "disagree": disagree,
        "n_fired": len(fired),
    }


# Relative stdev of 5-minute BTC moves, from option_edge_analysis.py's
# calibration against 180 days of historical data (sigma(5min)=0.1445%).
# Approximate and NOT live-recalculated -- used only for a rough closing
# price range, never as a point prediction.
ROUND_SIGMA_5MIN_PCT = 0.1445


def _market_prob_up(current_round):
    outcomes = current_round.get("outcomes")
    if not isinstance(outcomes, list):
        return None
    for o in outcomes:
        if str(o.get("name", "")).lower() == "up":
            raw = o.get("chance", o.get("price"))
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


MIN_EV_TO_ALERT = 0.02  # below this the "edge" is inside our own error bars

# If the market's price and our own fair-value estimate disagree by more than
# this, something is wrong with OUR inputs -- almost certainly a stale or
# mismatched round-open reference, since the market's own reference comes from
# Chainlink and ours from Binance klines. A large gap is a data problem
# masquerading as free money, so skip rather than bet into it.
MAX_FAIR_VALUE_DISAGREEMENT = 0.10

# Only bet while the round is still close to a coin flip. Two reasons, both
# measured against the 180-day dataset rather than assumed:
#
#   1. The signals were backtested on rounds judged from their start, where the
#      outcome is near even. Away from that, the edge is unmeasured.
#   2. Checking the fair-value model's calibration at moderate deviations shows
#      moves tend to CONTINUE, not revert: at fair 0.40 the real Up rate is
#      36.0%, at 0.60 it is 64.2% -- both further out than predicted. Every
#      signal here is contrarian, so firing at those levels means fighting that
#      drift. At 0.50 the model's error is -0.1pp, effectively unbiased.
#
# 0.08 keeps us in roughly 0.42-0.58, the band where the model is honest.
MAX_FAIR_DEVIATION_TO_BET = 0.08

# A wide spread or a near-empty top of book means the quote is one stray
# resting order, not a market price. Both guards exist because live data
# showed Down quoted between 0.23 and 0.71 on rounds where BTC had not moved,
# which is impossible for a real two-sided market and made every EV figure
# computed off those quotes meaningless.
MAX_SPREAD_TO_TRUST = 0.04
MIN_TOP_OF_BOOK_SIZE = 20.0

# Never buy a side we ourselves expect to lose. A price low enough can make
# the EV arithmetic positive even when q < 0.5, but that is exactly where the
# thin-book and fat-tail problems bite hardest, so it is not a bet worth
# surfacing.
MIN_Q_TO_BET = 0.50


def _normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# Historical stdev of BTC moves by remaining minutes, from
# option_edge_analysis.py's calibration over the 180-day dataset.
_SIGMA_BY_REMAINING_MIN = {1: 0.0650, 2: 0.0920, 3: 0.1125, 4: 0.1295, 5: 0.1445}


def naive_fair_prob(side, round_start_price, btc_price, seconds_into_round):
    """What this side is worth given only the move so far and historical
    volatility -- a zero-drift random walk estimate, the same model
    option_edge_analysis.py showed is reasonably well calibrated.

    Used as a cross-check on the market's price, not as a signal.
    """
    if not round_start_price or btc_price is None:
        return None
    remaining_min = max(1, min(5, math.ceil((ROUND_SECONDS - seconds_into_round) / 60)))
    sigma_abs = round_start_price * _SIGMA_BY_REMAINING_MIN[remaining_min] / 100
    if sigma_abs <= 0:
        return None
    z = (btc_price - round_start_price) / sigma_abs
    p_up = _normal_cdf(z)
    return p_up if side == "Up" else 1 - p_up

# The backtest measured betting reversion AT THE ROUND'S START. Once BTC has
# moved inside the round, that unconditional win rate no longer applies: a side
# trading cheap late in a round is usually cheap because it is genuinely losing,
# and buying it would be the worst bet available, not the best one. So the edge
# is only evaluated in the opening seconds, while the premise still holds.
MAX_SECONDS_INTO_ROUND_TO_BET = 45


def compute_bet_edge(
    signal,
    best_bid,
    best_ask,
    fee_rate,
    seconds_into_round=0,
    round_start_price=None,
    btc_price=None,
    bid_size=None,
    ask_size=None,
):
    """The actual analysis: is the market's price CHEAP relative to our
    estimated probability?

    Direction alone is worthless here -- betting "Up" because the market
    says 72% Up means paying 0.72 for something worth ~0.72. Edge exists
    only when we can buy a side for less than our own probability estimate.

    Buying a share at price p pays $1 if right, so for estimated win
    probability q and fee f:
        EV per $1 staked = q * (1 - f) / p - 1
    which is positive only when p < q * (1 - f).

    In a binary market, buying Down is economically selling Up, so Down's
    ask is approximately 1 - (best bid on Up). We only poll the Up book,
    which is why Down's price is derived rather than read directly.
    """
    if signal is None or best_bid is None or best_ask is None:
        return None
    if seconds_into_round > MAX_SECONDS_INTO_ROUND_TO_BET:
        return None

    side = signal["suggested_side"]
    price = best_ask if side == "Up" else round(1 - best_bid, 4)
    if not 0 < price < 1:
        return None

    # Build our OWN probability from the factors, rather than borrowing the
    # market's. Inside a round the market's price is close to a mechanical
    # function of how far BTC has moved and how much time is left -- not
    # independent insight -- so deferring to it just launders our own inputs
    # back to us. We can compute that same fair value directly, and it keeps
    # us off the market's round-open reference, which is what disagreed by
    # 15pp in the bad alert.
    #
    #   our_prob = fair value from the live move  +  the signal's measured edge
    #
    # The market's price then serves its actual purpose: the cost of the bet.
    fair = naive_fair_prob(side, round_start_price, btc_price, seconds_into_round)
    if fair is None:
        return None

    edge_over_fair = signal["q"] - 0.5
    q = min(0.99, max(0.01, fair + edge_over_fair))

    # A wide gap between our fair value and the market's price, this early in a
    # round, means we and the market are pricing off different opening prices
    # (ours from Binance klines, theirs resolved via Chainlink). That is a stale
    # reference on our side, not an edge.
    disagreement = abs(price - fair)
    reference_looks_wrong = disagreement > MAX_FAIR_VALUE_DISAGREEMENT

    # The backtest measured these signals on rounds seen from the start, with
    # the outcome still a near coin flip. Once the live move has already pushed
    # fair value far from even, we are in a situation the backtest never
    # measured, so the edge cannot be assumed to carry.
    move_too_far = abs(fair - 0.5) > MAX_FAIR_DEVIATION_TO_BET

    # Quote quality. A round that has just opened frequently has almost no
    # resting liquidity, and then "best bid" is a single stray order rather
    # than a price. Any EV computed from it is arithmetic on noise.
    spread = round(best_ask - best_bid, 4)
    thin_book = (
        spread > MAX_SPREAD_TO_TRUST
        or (bid_size is not None and bid_size < MIN_TOP_OF_BOOK_SIZE)
        or (ask_size is not None and ask_size < MIN_TOP_OF_BOOK_SIZE)
    )

    losing_side = q < MIN_Q_TO_BET

    ev = q * (1 - fee_rate) / price - 1
    return {
        "side": side,
        "price": price,
        "q": q,
        "edge_over_fair": edge_over_fair,
        "ev": ev,
        "naive_fair": fair,
        "disagreement": disagreement,
        "spread": spread,
        "reference_looks_wrong": reference_looks_wrong,
        "move_too_far": move_too_far,
        "thin_book": thin_book,
        "losing_side": losing_side,
        "max_price_worth_paying": round(q * (1 - fee_rate), 4),
        "worth_it": (
            ev >= MIN_EV_TO_ALERT
            and not reference_looks_wrong
            and not move_too_far
            and not thin_book
            and not losing_side
        ),
    }


def format_signal_consensus(signal):
    """Show which other strategies fired and on which side.

    Presented as context only: these signals are correlated (all pick up the
    same mean-reversion effect), so agreement is NOT extra evidence and is
    never folded into `q`. Disagreement is worth seeing though.
    """
    parts = []
    if signal.get("agree"):
        parts.append(f"✓ {len(signal['agree'])} más coinciden")
    if signal.get("disagree"):
        nombres = ", ".join(s["name"] for s in signal["disagree"][:2])
        parts.append(f"✗ {len(signal['disagree'])} en contra ({nombres})")
    if not parts:
        return ""
    return "\n<i>" + " · ".join(parts) + "</i>"


def build_status_message(market_id, signal, best_edge, recent_outcomes, btc_price, price_gap_usd, fee_rate):
    """The once-per-round 'nothing to do here' summary. Sent silently, so it
    confirms the bot is alive and shows WHY a round was skipped without
    buzzing the phone for a non-event."""
    header = f"⚪ <b>Sin oportunidad</b> · ronda <code>{market_id}</code>\n━━━━━━━━━━━━━━━━━━\n"

    if signal is None:
        ultimas = " ".join(recent_outcomes[-5:]) if recent_outcomes else "sin historial"
        body = (
            f"📊 <b>Señal</b>\nNinguna de las {len(_SIGNAL_SPECS)} estrategias se activó\n"
            f"Últimas rondas: <code>{ultimas}</code>\n\n"
            f"<i>Se evalúan rachas, movimiento de precio previo y flujo de órdenes. "
            f"Ninguna cumplió su condición de disparo.</i>\n\n"
        )
    elif best_edge is None:
        body = (
            f"📊 <b>Señal</b>\n{signal['name']} → sugiere {signal['suggested_side']}\n"
            f"<i>{signal['detail']}</i>\n\n"
            f"⏱ <i>No se pudo evaluar el precio dentro de los primeros "
            f"{MAX_SECONDS_INTO_ROUND_TO_BET}s (sin libro de órdenes a tiempo).</i>\n\n"
        )
    elif best_edge.get("thin_book"):
        body = (
            f"📊 <b>Señal</b>\n{signal['name']} → sugiere {best_edge['side']}\n"
            f"<i>{signal['detail']}</i>\n\n"
            f"🚩 <b>Descartada: libro sin liquidez</b>\n"
            f"Spread: {best_edge['spread']:.2f}\n\n"
            f"<i>Con el libro casi vacío al abrir la ronda, el \"mejor precio\" es "
            f"una orden suelta, no un precio de mercado. Calcular EV con eso es "
            f"hacer cuentas sobre ruido.</i>\n\n"
        )
    elif best_edge.get("losing_side"):
        body = (
            f"📊 <b>Señal</b>\n{signal['name']} → sugiere {best_edge['side']}\n"
            f"<i>{signal['detail']}</i>\n\n"
            f"🚩 <b>Descartada: esperamos perder esta apuesta</b>\n"
            f"Nuestra estimacion: {best_edge['q']*100:.1f}% <i>(por debajo de 50%)</i>\n\n"
            f"<i>El precio bajo hace que el EV dé positivo, pero estaríamos "
            f"comprando un lado que nosotros mismos creemos que pierde más veces "
            f"de las que gana. No se apuesta ahí.</i>\n\n"
        )
    elif best_edge.get("move_too_far"):
        body = (
            f"📊 <b>Señal</b>\n{signal['name']} → sugiere {best_edge['side']}\n"
            f"<i>{signal['detail']}</i>\n\n"
            f"⏭ <b>Descartada: la ronda ya se definió demasiado</b>\n"
            f"Valor justo de {best_edge['side']}: {best_edge['naive_fair']:.2f}\n\n"
            f"<i>Las señales se midieron al inicio de ronda, con el resultado aún "
            f"parejo. Con el precio ya corrido, además, los movimientos tienden a "
            f"seguir en vez de revertir — apostar contrarian acá sería pelear "
            f"contra eso.</i>\n\n"
        )
    elif best_edge.get("reference_looks_wrong"):
        body = (
            f"📊 <b>Señal</b>\n{signal['name']} → sugiere {best_edge['side']}\n"
            f"<i>{signal['detail']}</i>\n\n"
            f"🚩 <b>Descartada: referencia sospechosa</b>\n"
            f"Mercado pide: <b>{best_edge['price']}</b>\n"
            f"Valor justo segun volatilidad: {best_edge['naive_fair']:.2f}\n"
            f"Discrepancia: <b>{best_edge['disagreement']*100:.0f} puntos</b>\n\n"
            f"<i>Una brecha así con BTC casi quieto significa que el mercado y "
            f"nosotros no estamos mirando el mismo precio de apertura. Eso es un "
            f"problema de datos, no una oportunidad.</i>\n\n"
        )
    else:
        body = (
            f"📊 <b>Señal</b>\n{signal['name']} → sugiere {best_edge['side']}\n"
            f"<i>{signal['detail']}</i>\n"
            f"Prob. estimada: {best_edge['q']*100:.1f}%"
            f"{format_signal_consensus(signal)}\n\n"
            f"💰 <b>Precio: sin margen</b>\n"
            f"Mercado pide: <b>{best_edge['price']}</b>\n"
            f"Nuestra estimacion: {best_edge['q']:.3f}\n"
            f"Ventaja (EV): <b>{best_edge['ev']*100:+.1f}%</b> <i>(fee {fee_rate*100:.1f}% incluida)</i>\n\n"
            f"<i>La ventaja de la señal no alcanza a cubrir la fee.</i>\n\n"
        )

    btc_line = ""
    if btc_price is not None:
        btc_line = f"₿ ${btc_price:,.2f} · {fmt_gap(price_gap_usd)} desde apertura\n"

    return header + body + btc_line + f"📋 {compute_running_accuracy()}"


def closing_range_bounds(round_start_price):
    """68%/90% bands around the round's opening price, from the historical
    5-minute volatility measured in option_edge_analysis.py. A spread of
    plausible closes, never a point forecast."""
    if not round_start_price:
        return 0, 0, 0, 0
    one_sigma = round_start_price * ROUND_SIGMA_5MIN_PCT / 100
    return (
        round_start_price - one_sigma,
        round_start_price + one_sigma,
        round_start_price - 1.645 * one_sigma,
        round_start_price + 1.645 * one_sigma,
    )


def _extract_poll_context(current_round, topic):
    vendor = topic.get("vendor")
    condition_id = current_round.get("conditionId")
    token_id = None
    outcomes = current_round.get("outcomes")
    if isinstance(outcomes, list):
        for o in outcomes:
            if str(o.get("name", "")).lower() == "up":
                token_id = o.get("tokenId")
                break
    return vendor, condition_id, token_id


ROUND_SECONDS = 5 * 60
REFRESH_MARGIN_SECONDS = 5  # refresh a bit before the actual round boundary
STALE_REPEATS_BEFORE_FORCE_REFRESH = 3  # same timestamp this many times in a row -> book is frozen


def next_round_boundary(now=None):
    """Timestamp (s) of the next 5-minute wall-clock boundary -- rounds are
    aligned to :00/:05/:10/... UTC (confirmed earlier: a shared market slug's
    timestamp landed exactly on one), so this is accurate regardless of how
    much of the current round was already elapsed when we joined it."""
    now = now if now is not None else time.time()
    return (int(now) // ROUND_SECONDS + 1) * ROUND_SECONDS


def poll_loop(market_id, current_round, topic):
    ensure_log_file()
    ensure_outcomes_file()
    print(f"\n=== Paso 2: polleando order-book cada {POLL_INTERVAL_SECONDS}s (Ctrl+C para parar) ===")

    vendor, condition_id, token_id = _extract_poll_context(current_round, topic)
    round_end_at = next_round_boundary()
    round_start_ms = (round_end_at - ROUND_SECONDS) * 1000
    round_start_price = get_round_start_price(round_start_ms)
    next_refresh_at = round_end_at - REFRESH_MARGIN_SECONDS
    last_snapshot_ts = None
    stale_repeats = 0

    fee_rate = (topic.get("feeRateBps") or 200) / 10000
    recent_outcomes = load_recent_outcomes()
    fired = collect_signals(recent_outcomes, get_pre_round_candles(round_start_ms), round_start_ms)
    signal = pick_signal(fired)
    if fired:
        print(f"[señales] {len(fired)} activas: " + ", ".join(f"{s['name']}→{s['side']}" for s in fired))
    market_prob_up = _market_prob_up(current_round)
    alerted_this_round = False
    status_sent_this_round = False
    best_edge_this_round = None
    bet_side = bet_reasoning = bet_signal_name = bet_entry_price = None

    while True:
        try:
            if time.time() >= next_refresh_at:
                print("\n[rollover] fin de ronda esperado -- resolviendo resultado de la ronda anterior...")
                outcome = log_round_outcome(
                    market_id, round_start_ms, market_prob_up, bet_side, bet_reasoning,
                    bet_signal_name, bet_entry_price,
                )
                if outcome in ("Up", "Down"):
                    recent_outcomes.append(outcome)
                    recent_outcomes = recent_outcomes[-10:]

                print("[rollover] buscando la ronda actual de nuevo...")
                new_market_id, new_round, new_topic = find_btc_5m_market()
                if new_market_id is not None:
                    market_id, current_round, topic = new_market_id, new_round, new_topic
                    vendor, condition_id, token_id = _extract_poll_context(current_round, topic)
                    fee_rate = (topic.get("feeRateBps") or 200) / 10000
                round_end_at = next_round_boundary()
                round_start_ms = (round_end_at - ROUND_SECONDS) * 1000
                round_start_price = get_round_start_price(round_start_ms)
                next_refresh_at = round_end_at - REFRESH_MARGIN_SECONDS
                last_snapshot_ts, stale_repeats = None, 0

                fired = collect_signals(recent_outcomes, get_pre_round_candles(round_start_ms), round_start_ms)
                signal = pick_signal(fired)
                market_prob_up = _market_prob_up(current_round)
                alerted_this_round = False
                status_sent_this_round = False
                best_edge_this_round = None
                bet_side = bet_reasoning = bet_signal_name = bet_entry_price = None
                if fired:
                    print(f"[señales] {len(fired)} activas: " + ", ".join(f"{s['name']}→{s['side']}" for s in fired))
                else:
                    print(f"[sin señal] ultimas rondas: {' '.join(recent_outcomes[-4:])} -- ninguna estrategia disparo")

            resp = get_order_book(market_id, vendor, token_id, condition_id)
            btc_price, price_gap_usd = log_snapshot(market_id, resp, round_start_price)
            if resp.status_code == 200:
                if VERBOSE_SNAPSHOTS:
                    print(f"[snapshot] {resp.text[:300]}")
                try:
                    body = resp.json()
                    snapshot_ts = body.get("timestamp")
                    best_bid, best_ask = _best_bid_ask(body)
                    bid_size, ask_size = _book_depth(body)
                except ValueError:
                    snapshot_ts, best_bid, best_ask = None, None, None
                    bid_size = ask_size = None

                seconds_into_round = time.time() - (round_start_ms / 1000)
                edge = compute_bet_edge(
                    signal, best_bid, best_ask, fee_rate, seconds_into_round,
                    round_start_price, btc_price, bid_size, ask_size,
                )
                if edge:
                    fair_txt = f"{edge['naive_fair']:.2f}" if edge["naive_fair"] is not None else "s/d"
                    flag = "  [REFERENCIA SOSPECHOSA]" if edge["reference_looks_wrong"] else ""
                    print(
                        f"[edge] {edge['side']} a {edge['price']} | valor justo {fair_txt} | "
                        f"EV {edge['ev']*100:+.1f}%{flag}"
                    )
                    if best_edge_this_round is None or edge["ev"] > best_edge_this_round["ev"]:
                        best_edge_this_round = edge
                if edge and edge["worth_it"] and not alerted_this_round:
                    alerted_this_round = True
                    bet_side = edge["side"]
                    bet_signal_name = signal["name"]
                    bet_entry_price = edge["price"]
                    bet_reasoning = (
                        f"{signal['name']} ({signal['detail']}); "
                        f"{edge['side']} a {edge['price']} (tope {edge['max_price_worth_paying']}); EV {edge['ev']*100:+.1f}%"
                    )
                    lo68, hi68, lo90, hi90 = closing_range_bounds(round_start_price)
                    encabezado = (
                        f"📝 <b>REGISTRADO (no apostar)</b>: {edge['side'].upper()} a <b>{edge['price']}</b>\n"
                        f"<i>Modo observación — la precisión en vivo está por debajo del azar</i>\n"
                        if PAPER_MODE
                        else f"🎯 <b>COMPRAR {edge['side'].upper()}</b> a <b>{edge['price']}</b>\n"
                    )
                    send_notification(
                        f"{'Registrado' if PAPER_MODE else 'VALOR'}: {edge['side']} a {edge['price']}",
                        encabezado + f"━━━━━━━━━━━━━━━━━━\n"
                        f"📊 <b>Señal: {signal['name']}</b>\n"
                        f"<i>{signal['detail']}</i>\n"
                        f"Prob. estimada: {edge['q']*100:.1f}% <i>(límite inf. IC95%)</i>"
                        f"{format_signal_consensus(signal)}\n\n"
                        f"💰 <b>Precio</b>\n"
                        f"Mercado pide: <b>{edge['price']}</b>\n"
                        f"Valor justo (volatilidad): {edge['naive_fair']:.2f}\n"
                        f"Nuestra estimacion: {edge['q']:.3f} "
                        f"<i>(mercado {edge['price']} + ventaja {edge['edge_over_fair']*100:+.1f}pp)</i>\n"
                        f"Ventaja (EV): <b>{edge['ev']*100:+.1f}%</b> <i>(fee {fee_rate*100:.1f}% ya descontada)</i>\n\n"
                        f"₿ <b>Bitcoin ahora</b>\n"
                        f"${btc_price:,.2f} · <b>{fmt_gap(price_gap_usd)}</b> desde apertura\n"
                        f"<i>({int(seconds_into_round)}s dentro de la ronda)</i>\n\n"
                        f"📈 <b>Cierre probable</b>\n"
                        f"68%: ${lo68:,.0f} – ${hi68:,.0f}\n"
                        f"90%: ${lo90:,.0f} – ${hi90:,.0f}\n\n"
                        f"⚠️ <i>EV no incluye price impact</i>\n"
                        f"📋 {compute_running_accuracy()}",
                        silent=PAPER_MODE,  # not actionable, so don't buzz for it
                    )

                # Betting window closed with nothing worth taking: say so once,
                # silently, so a quiet round is visibly "checked and skipped"
                # rather than indistinguishable from a dead script.
                if (
                    SEND_STATUS_EVERY_ROUND
                    and not alerted_this_round
                    and not status_sent_this_round
                    and seconds_into_round > MAX_SECONDS_INTO_ROUND_TO_BET
                ):
                    status_sent_this_round = True
                    send_notification(
                        f"Ronda {market_id}: sin oportunidad",
                        build_status_message(
                            market_id, signal, best_edge_this_round, recent_outcomes,
                            btc_price, price_gap_usd, fee_rate,
                        ),
                        silent=True,
                    )

                if snapshot_ts is not None and snapshot_ts == last_snapshot_ts:
                    stale_repeats += 1
                    if stale_repeats >= STALE_REPEATS_BEFORE_FORCE_REFRESH:
                        print(
                            f"[stale] mismo timestamp {snapshot_ts} repetido "
                            f"{stale_repeats} veces -- la ronda ya resolvio, forzando rollover"
                        )
                        next_refresh_at = 0  # force refresh on next loop iteration
                else:
                    stale_repeats = 0
                last_snapshot_ts = snapshot_ts
            else:
                print(f"[snapshot-error] {resp.status_code} {resp.text[:300]}")
        except KeyboardInterrupt:
            print("Detenido por el usuario.")
            return
        except Exception as exc:
            print(f"[error] {exc}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    market_id, current_round, topic = find_btc_5m_market()
    if market_id is None:
        print(
            "\nNo se pudo continuar automaticamente. Revisa data/raw_api_responses.jsonl, "
            "compartime lo que encontraste, y ajusto la extraccion de campos."
        )
    else:
        poll_loop(market_id, current_round, topic)
