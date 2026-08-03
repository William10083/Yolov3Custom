"""Live odds/outcome logger for Binance's Prediction Markets WebSocket API.

RUN THIS ON YOUR OWN DEVICE, NOT IN A REMOTE SANDBOX.
Binance's authenticated API (api.binance.com, including this websocket)
is geo-blocked from some cloud/sandbox IP ranges. It must run from your
own network, with your own credentials, which never leave your device.

Setup (on your own machine):
    pip install websocket-client
    export BINANCE_API_KEY="..."
    export BINANCE_API_SECRET="..."
    python3 local_odds_logger.py

What this does:
  - Opens a signed websocket connection per Binance's documented
    integration guide (HMAC-SHA256 signed query params, X-MBX-APIKEY
    header, 30s PING heartbeat, reconnect with backoff).
  - Subscribes to the prediction-market topic you configure below.
  - Logs every message to a local CSV (timestamp + raw JSON), so nothing
    is lost even if we don't know the exact field layout yet.
  - Does NOT place any orders. Read-only logging only.

IMPORTANT -- about the topic name: a shared "btc-updown-5m-<timestamp>"
web3.binance.com URL confirmed each 5-minute round is its own market,
identified by a unix-second timestamp that lands exactly on a 5-minute
boundary (e.g. ...-1785738000 = 2026-08-03 06:20:00 UTC). So the topic
isn't a single fixed string -- it changes every round. This script
computes it automatically each round (see `round_market_id()` below) and
reconnects with the new topic as each round rolls over.

This is still an EDUCATED GUESS at the exact topic format
(f"btc-updown-5m-{boundary_ts}"), not a confirmed value -- I could not
capture the real websocket subscribe frame (the page is behind an AWS WAF
bot challenge I can't pass from here). First run will tell you fast
whether it's right:
  - If you start getting TOPIC messages with price/odds data -> correct.
  - If you only get PING/connection-ack traffic and nothing else -> wrong
    format. Try MARKET_ID_USES_END_TIME = False (round START instead of
    END), or capture the real value yourself (see README) and hardcode it
    via the TOPIC_OVERRIDE env var.
"""
import csv
import hashlib
import hmac
import json
import os
import random
import string
import threading
import time
import urllib.parse

try:
    import websocket  # pip install websocket-client
except ImportError:
    raise SystemExit(
        "Falta la libreria websocket-client. Instala con: pip install websocket-client"
    )

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")
if not API_KEY or not API_SECRET:
    raise SystemExit(
        "Faltan credenciales. Corre:\n"
        "  export BINANCE_API_KEY=...\n"
        "  export BINANCE_API_SECRET=...\n"
        "antes de este script. Nunca las escribas directamente en el codigo."
    )

WS_BASE = "wss://api.binance.com/sapi/wss"
ROUND_SECONDS = 5 * 60

# Educated guess: does the market id use the round's END boundary (like the
# 1785738000 = 06:20:00 example) or its START? Flip this if the first guess
# doesn't return real data.
MARKET_ID_USES_END_TIME = True

# Escape hatch: if you captured the real topic yourself (see README), set
#   export TOPIC_OVERRIDE="the_real_topic_string"
# and this script will use that fixed value instead of guessing per round.
TOPIC_OVERRIDE = os.environ.get("TOPIC_OVERRIDE")

LOG_FILE = os.path.join(os.path.dirname(__file__), "data", "live_odds_log.csv")


def round_market_id(now_ms=None):
    """Compute the market id ('btc-updown-5m-<ts>') for the round currently
    in progress, and how many seconds until that round ends."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    now_s = now_ms // 1000
    round_start = (now_s // ROUND_SECONDS) * ROUND_SECONDS
    round_end = round_start + ROUND_SECONDS
    boundary_ts = round_end if MARKET_ID_USES_END_TIME else round_start
    market_id = f"btc-updown-5m-{boundary_ts}"
    seconds_left_in_round = round_end - now_s
    return market_id, seconds_left_in_round


def sign_request(params: dict) -> str:
    """HMAC-SHA256 sign, params sorted alphabetically and concatenated
    as a query string, per Binance's documented integration guide."""
    sorted_items = sorted(params.items())
    query_string = urllib.parse.urlencode(sorted_items)
    signature = hmac.new(
        API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return signature


def build_ws_url(topic: str) -> str:
    params = {
        "random": "".join(random.choices(string.ascii_letters + string.digits, k=16)),
        "topic": topic,
        "timestamp": str(int(time.time() * 1000)),
        "recvWindow": "5000",
    }
    params["signature"] = sign_request(params)
    query_string = urllib.parse.urlencode(params)
    return f"{WS_BASE}?{query_string}"


_current_topic = None  # set right before each connection, used only for logging


def ensure_log_file():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["received_at_ms", "topic", "raw_message"])


def log_message(raw_message: str):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([int(time.time() * 1000), _current_topic, raw_message])


def on_message(ws, message):
    print(f"[msg] {message[:200]}")
    log_message(message)
    try:
        envelope = json.loads(message)
        if envelope.get("type") == "TOPIC" and isinstance(envelope.get("data"), str):
            # data field is double-encoded JSON per the integration guide
            inner = json.loads(envelope["data"])
            print(f"  -> parsed: {inner}")
    except (json.JSONDecodeError, AttributeError):
        pass


def on_error(ws, error):
    print(f"[error] {error}")


def on_close(ws, close_status_code, close_msg):
    print(f"[closed] code={close_status_code} msg={close_msg}")


def on_open(ws):
    print("[open] conectado, iniciando heartbeat")


def run_forever_with_backoff():
    ensure_log_file()
    backoff = 2
    while True:
        if TOPIC_OVERRIDE:
            topic = TOPIC_OVERRIDE
            seconds_left = ROUND_SECONDS  # fixed topic: no need to rotate
        else:
            topic, seconds_left = round_market_id()
        print(f"[topic] usando '{topic}' (quedan ~{seconds_left}s de esta ronda)")

        global _current_topic
        _current_topic = topic

        try:
            url = build_ws_url(topic)
            ws = websocket.WebSocketApp(
                url,
                header=[f"X-MBX-APIKEY: {API_KEY}"],
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )

            # Close this connection right as the round ends so the outer
            # loop can recompute the next round's topic and resubscribe.
            rotate_timer = None
            if not TOPIC_OVERRIDE:
                rotate_timer = threading.Timer(max(seconds_left, 1) + 1, ws.close)
                rotate_timer.daemon = True
                rotate_timer.start()

            ws.run_forever(ping_interval=30, ping_payload="")
            if rotate_timer:
                rotate_timer.cancel()
            backoff = 2  # reset after a clean run
        except KeyboardInterrupt:
            print("Detenido por el usuario.")
            return
        except Exception as exc:
            print(f"[fatal] {exc}, reintentando en {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    if not TOPIC_OVERRIDE:
        print(
            "AVISO: usando un topic ADIVINADO ('btc-updown-5m-<timestamp>'), "
            "no confirmado -- ver el docstring de este archivo. Si despues de "
            "un par de minutos solo ves PING/ack y ningun dato de precio/odds, "
            "el formato esta mal: proba MARKET_ID_USES_END_TIME=False o "
            "consigue el topic real y usa la variable TOPIC_OVERRIDE."
        )
    run_forever_with_backoff()
