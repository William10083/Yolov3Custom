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
import os
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

REST_BASE = "https://api.binance.com"
SIGN_MARKET_DATA_CALLS = True  # flip to False if these specific calls reject the signature
POLL_INTERVAL_SECONDS = 5

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


def ensure_log_file():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
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
            )


def _best_bid_ask(order_book_json):
    """bids are sorted highest-first, asks lowest-first per the docs."""
    bids = order_book_json.get("bids") or []
    asks = order_book_json.get("asks") or []
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    return best_bid, best_ask


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


def log_round_outcome(market_id, round_start_ms, market_prob_up=None, predicted_side=None, predicted_reasoning=None):
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
            f"📋 {compute_running_accuracy()}",
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


def send_telegram(html_body):
    """HTML parse_mode rather than Markdown: these messages are full of
    parentheses, dashes, underscores and $ signs, which Markdown silently
    mangles or rejects outright."""
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


def send_notification(title, html_body):
    """Alerts via three independent channels -- console (always), Telegram
    (if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set) and termux-notification
    (if available). Never raises -- a missing/broken channel must not
    interrupt the polling loop; each is wrapped so the others still fire."""
    plain = _strip_html(html_body)
    print(f"\n{'=' * 60}\n{plain}\n{'=' * 60}\n")
    send_telegram(html_body)
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


# Streak-reversion win rates from backtest.py's 180-day run at the REAL
# confirmed fee (2% = feeRateBps 200, from live market/list data -- see
# README). Only k>=3 is used as an alert trigger: k=2 fires roughly every
# other round (P(same as previous)~50%), too frequent to treat as a
# special "this round is worth a look" signal -- it's not what "solo
# cuando es muy segura" (the original ask) meant.
_STREAK_STATS = {
    3: "53.2% OOS, IC95% [52.0%, 54.5%] (n~6000 en backtest historico)",
    4: "53.2%-53.7% OOS, IC95% mas ancho ~[51%, 55%] (menos muestra)",
}


def check_streak_signal(recent_outcomes):
    """NOT a validated live trading signal -- based on backtest.py's
    historical 180-day backtest at the real 2% fee, which does NOT include
    price impact/slippage (a real, separate cost -- see README). Not yet
    confirmed against the live order-book data this script is collecting.
    Use your own judgment; this is informational, not a recommendation."""
    if len(recent_outcomes) < 3:
        return None
    last3 = recent_outcomes[-3:]
    if len(set(last3)) != 1:
        return None
    streak_direction = last3[0]
    streak_len = 4 if (len(recent_outcomes) >= 4 and recent_outcomes[-4] == streak_direction) else 3
    suggested_side = "Down" if streak_direction == "Up" else "Up"
    return {
        "streak_direction": streak_direction,
        "streak_len": streak_len,
        "suggested_side": suggested_side,
        "historical_stats": _STREAK_STATS[streak_len],
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


# Conservative win-rate estimate per streak length: the LOWER bound of the
# backtest's 95% CI, not the point estimate. Using the point estimate would
# systematically overstate the edge -- if the true rate sits at the bottom of
# the interval, every bet sized off the midpoint is a losing bet.
_STREAK_WIN_PROB_CONSERVATIVE = {3: 0.520, 4: 0.510}

MIN_EV_TO_ALERT = 0.02  # below this the "edge" is inside our own error bars

# The backtest measured betting reversion AT THE ROUND'S START. Once BTC has
# moved inside the round, that unconditional win rate no longer applies: a side
# trading cheap late in a round is usually cheap because it is genuinely losing,
# and buying it would be the worst bet available, not the best one. So the edge
# is only evaluated in the opening seconds, while the premise still holds.
MAX_SECONDS_INTO_ROUND_TO_BET = 45


def compute_bet_edge(streak_signal, best_bid, best_ask, fee_rate, seconds_into_round=0):
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
    if streak_signal is None or best_bid is None or best_ask is None:
        return None
    if seconds_into_round > MAX_SECONDS_INTO_ROUND_TO_BET:
        return None

    side = streak_signal["suggested_side"]
    q = _STREAK_WIN_PROB_CONSERVATIVE[streak_signal["streak_len"]]
    price = best_ask if side == "Up" else round(1 - best_bid, 4)
    if not 0 < price < 1:
        return None

    ev = q * (1 - fee_rate) / price - 1
    return {
        "side": side,
        "price": price,
        "q": q,
        "ev": ev,
        "max_price_worth_paying": round(q * (1 - fee_rate), 4),
        "worth_it": ev >= MIN_EV_TO_ALERT,
    }


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
    signal = check_streak_signal(recent_outcomes)
    market_prob_up = _market_prob_up(current_round)
    alerted_this_round = False
    bet_side = bet_reasoning = None

    while True:
        try:
            if time.time() >= next_refresh_at:
                print("\n[rollover] fin de ronda esperado -- resolviendo resultado de la ronda anterior...")
                outcome = log_round_outcome(market_id, round_start_ms, market_prob_up, bet_side, bet_reasoning)
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

                signal = check_streak_signal(recent_outcomes)
                market_prob_up = _market_prob_up(current_round)
                alerted_this_round = False
                bet_side = bet_reasoning = None
                if signal is None:
                    print(f"[sin señal] ultimas rondas: {' '.join(recent_outcomes[-4:])} -- sin racha, no se apuesta")

            resp = get_order_book(market_id, vendor, token_id, condition_id)
            btc_price, price_gap_usd = log_snapshot(market_id, resp, round_start_price)
            if resp.status_code == 200:
                print(f"[snapshot] {resp.text[:300]}")
                try:
                    body = resp.json()
                    snapshot_ts = body.get("timestamp")
                    best_bid, best_ask = _best_bid_ask(body)
                except ValueError:
                    snapshot_ts, best_bid, best_ask = None, None, None

                seconds_into_round = time.time() - (round_start_ms / 1000)
                edge = compute_bet_edge(signal, best_bid, best_ask, fee_rate, seconds_into_round)
                if edge:
                    print(
                        f"[edge] {edge['side']} a {edge['price']} | tope que vale pagar: "
                        f"{edge['max_price_worth_paying']} | EV {edge['ev']*100:+.1f}%"
                    )
                if edge and edge["worth_it"] and not alerted_this_round:
                    alerted_this_round = True
                    bet_side = edge["side"]
                    bet_reasoning = (
                        f"racha {signal['streak_len']}x {signal['streak_direction']}; "
                        f"{edge['side']} a {edge['price']} (tope {edge['max_price_worth_paying']}); EV {edge['ev']*100:+.1f}%"
                    )
                    lo68, hi68, lo90, hi90 = closing_range_bounds(round_start_price)
                    send_notification(
                        f"VALOR: {edge['side']} a {edge['price']}",
                        f"🎯 <b>COMPRAR {edge['side'].upper()}</b> a <b>{edge['price']}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📊 <b>Señal</b>\n"
                        f"Racha: {signal['streak_len']}x {signal['streak_direction']} → reversión\n"
                        f"Prob. estimada: {edge['q']*100:.1f}% <i>(límite inf. IC95%)</i>\n\n"
                        f"💰 <b>Precio</b>\n"
                        f"Mercado pide: <b>{edge['price']}</b>\n"
                        f"Tope que vale pagar: {edge['max_price_worth_paying']}\n"
                        f"Ventaja (EV): <b>{edge['ev']*100:+.1f}%</b> <i>(fee {fee_rate*100:.1f}% ya descontada)</i>\n\n"
                        f"₿ <b>Bitcoin ahora</b>\n"
                        f"${btc_price:,.2f} · <b>{fmt_gap(price_gap_usd)}</b> desde apertura\n"
                        f"<i>({int(seconds_into_round)}s dentro de la ronda)</i>\n\n"
                        f"📈 <b>Cierre probable</b>\n"
                        f"68%: ${lo68:,.0f} – ${hi68:,.0f}\n"
                        f"90%: ${lo90:,.0f} – ${hi90:,.0f}\n\n"
                        f"⚠️ <i>EV no incluye price impact</i>\n"
                        f"📋 {compute_running_accuracy()}",
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
