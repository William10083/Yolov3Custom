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
                    "body": resp.text[:4000],
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
    """Try to locate the live 'BTC Up or Down 5m' market and return its
    market id (raw dict too, for inspection). Tries market/search first,
    falls back to market/list + manual filtering. Field names are educated
    guesses -- prints raw JSON at every step so you can correct me."""
    print("\n=== Paso 1: buscando el mercado 'BTC Up or Down 5m' ===")

    for keyword_param in ("keyword", "query", "q", "search"):
        resp = api_get(
            "/sapi/v1/w3w/wallet/prediction/market/search",
            {keyword_param: "BTC Up or Down 5m"},
        )
        if resp.status_code == 200:
            break
    else:
        print("market/search no respondio 200 con ningun nombre de parametro probado.")
        resp = None

    candidates = []
    if resp is not None and resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            data = None
        print("Respuesta cruda de market/search:", json.dumps(data, indent=2)[:2000] if data else resp.text[:500])
        items = _first_present(data, ("data", "items", "markets", "list", "result")) if isinstance(data, dict) else data
        if isinstance(items, list):
            candidates.extend(items)

    if not candidates:
        print("\nProbando market/list como alternativa...")
        resp2 = api_get("/sapi/v1/w3w/wallet/prediction/market/list", {"limit": 50})
        if resp2.status_code == 200:
            try:
                data2 = resp2.json()
            except ValueError:
                data2 = None
            print("Respuesta cruda de market/list:", json.dumps(data2, indent=2)[:2000] if data2 else resp2.text[:500])
            items2 = _first_present(data2, ("data", "items", "markets", "list", "result")) if isinstance(data2, dict) else data2
            if isinstance(items2, list):
                candidates.extend(items2)

    match = None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        title = str(_first_present(item, ("title", "name", "topic", "question")) or "")
        if "btc" in title.lower() and "5" in title:
            match = item
            break

    if not match:
        print(
            "\nNo pude identificar automaticamente el mercado en la respuesta. "
            "Mira el JSON crudo de arriba (y en data/raw_api_responses.jsonl) y "
            "decime cual es el campo con el id del mercado 'BTC Up or Down 5m' -- "
            "lo agrego al codigo."
        )
        return None, None

    market_id = _first_present(match, ("id", "marketId", "topicId", "marketID"))
    print(f"\nMercado encontrado: {match}")
    print(f"market_id extraido: {market_id}")
    return market_id, match


def get_order_book(market_id, outcome_token_id=None):
    params = {"marketId": market_id}
    if outcome_token_id:
        params["outcomeTokenId"] = outcome_token_id
    return api_get("/sapi/v1/w3w/wallet/prediction/order-book", params)


def ensure_log_file():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["polled_at_ms", "market_id", "http_status", "raw_body"])


def log_snapshot(market_id, resp):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([int(time.time() * 1000), market_id, resp.status_code, resp.text[:2000]])


def poll_loop(market_id, market_detail):
    ensure_log_file()
    print(f"\n=== Paso 2: polleando order-book cada {POLL_INTERVAL_SECONDS}s (Ctrl+C para parar) ===")
    outcome_token_id = None
    outcomes = _first_present(market_detail, ("outcomes", "tokens")) if market_detail else None
    if isinstance(outcomes, list):
        for o in outcomes:
            name = str(_first_present(o, ("name", "outcomeName", "title")) or "").lower()
            if name in ("up", "yes"):
                outcome_token_id = _first_present(o, ("tokenId", "outcomeTokenId", "id"))
                break

    while True:
        try:
            resp = get_order_book(market_id, outcome_token_id)
            log_snapshot(market_id, resp)
            if resp.status_code == 200:
                print(f"[snapshot] {resp.text[:200]}")
        except KeyboardInterrupt:
            print("Detenido por el usuario.")
            return
        except Exception as exc:
            print(f"[error] {exc}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    market_id, market_detail = find_btc_5m_market()
    if market_id is None:
        print(
            "\nNo se pudo continuar automaticamente. Revisa data/raw_api_responses.jsonl, "
            "compartime lo que encontraste, y ajusto la extraccion de campos."
        )
    else:
        detail_resp = api_get("/sapi/v1/w3w/wallet/prediction/market/detail", {"marketId": market_id})
        detail_json = None
        if detail_resp.status_code == 200:
            try:
                detail_json = detail_resp.json()
                print("Detalle del mercado:", json.dumps(detail_json, indent=2)[:2000])
            except ValueError:
                pass
        poll_loop(market_id, detail_json)
