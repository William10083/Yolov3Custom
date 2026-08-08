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


def log_snapshot(market_id, resp):
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
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [int(time.time() * 1000), market_id, resp.status_code, api_ts, best_bid, best_ask, mid, resp.text[:2000]]
        )


OUTCOMES_FILE = os.path.join(os.path.dirname(__file__), "data", "round_outcomes.csv")


def ensure_outcomes_file():
    os.makedirs(os.path.dirname(OUTCOMES_FILE), exist_ok=True)
    if not os.path.exists(OUTCOMES_FILE):
        with open(OUTCOMES_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["market_id", "round_start_ms", "round_end_ms", "start_price", "end_price", "outcome"])


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


def log_round_outcome(market_id, round_start_ms):
    ensure_outcomes_file()
    round_end_ms = round_start_ms + ROUND_SECONDS * 1000
    start_price, end_price, outcome = resolve_round_outcome(round_start_ms)
    with open(OUTCOMES_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([market_id, round_start_ms, round_end_ms, start_price, end_price, outcome])
    if outcome:
        print(f"[outcome] market_id={market_id} -> {outcome} (${start_price:,.2f} -> ${end_price:,.2f})")
    else:
        print(f"[outcome] market_id={market_id} -> no se pudo resolver (klines no disponibles todavia)")


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
    next_refresh_at = round_end_at - REFRESH_MARGIN_SECONDS
    last_snapshot_ts = None
    stale_repeats = 0

    while True:
        try:
            if time.time() >= next_refresh_at:
                print("\n[rollover] fin de ronda esperado -- resolviendo resultado de la ronda anterior...")
                log_round_outcome(market_id, round_start_ms)

                print("[rollover] buscando la ronda actual de nuevo...")
                new_market_id, new_round, new_topic = find_btc_5m_market()
                if new_market_id is not None:
                    market_id, current_round, topic = new_market_id, new_round, new_topic
                    vendor, condition_id, token_id = _extract_poll_context(current_round, topic)
                round_end_at = next_round_boundary()
                round_start_ms = (round_end_at - ROUND_SECONDS) * 1000
                next_refresh_at = round_end_at - REFRESH_MARGIN_SECONDS
                last_snapshot_ts, stale_repeats = None, 0

            resp = get_order_book(market_id, vendor, token_id, condition_id)
            log_snapshot(market_id, resp)
            if resp.status_code == 200:
                print(f"[snapshot] {resp.text[:300]}")
                try:
                    snapshot_ts = resp.json().get("timestamp")
                except ValueError:
                    snapshot_ts = None
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
