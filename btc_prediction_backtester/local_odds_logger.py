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

IMPORTANT -- one thing you need to fill in yourself: TOPIC_NAME below.
Binance's public announcement/docs describe the connection protocol but
not the exact topic string for "BTC Up or Down 5m". To find it:
  1. Open the Binance app's Prediction tab on a device where you can
     inspect network traffic (or check Binance's Prediction Markets API
     docs at https://developers.binance.com/docs/w3w_prediction for an
     updated topic list -- this may have been published after my
     knowledge cutoff).
  2. Once you find it, set TOPIC_NAME accordingly (likely something like
     a market/symbol identifier for the BTC 5-minute market).
This script will run and connect either way, but won't receive anything
useful until TOPIC_NAME is correct.
"""
import csv
import hashlib
import hmac
import json
import os
import random
import string
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

# TODO: reemplazar por el topic real de "BTC Up or Down 5m" (ver docstring arriba)
TOPIC_NAME = "REPLACE_ME_WITH_REAL_TOPIC"

LOG_FILE = os.path.join(os.path.dirname(__file__), "data", "live_odds_log.csv")


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


def ensure_log_file():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["received_at_ms", "raw_message"])


def log_message(raw_message: str):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([int(time.time() * 1000), raw_message])


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
        try:
            url = build_ws_url(TOPIC_NAME)
            ws = websocket.WebSocketApp(
                url,
                header=[f"X-MBX-APIKEY: {API_KEY}"],
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            ws.run_forever(ping_interval=30, ping_payload="")
            backoff = 2  # reset after a clean run
        except KeyboardInterrupt:
            print("Detenido por el usuario.")
            return
        except Exception as exc:
            print(f"[fatal] {exc}, reintentando en {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    if TOPIC_NAME.startswith("REPLACE_ME"):
        print(
            "AVISO: TOPIC_NAME todavia no esta configurado. El script va a "
            "conectar pero no vas a recibir datos utiles hasta que lo pongas. "
            "Ver el docstring de este archivo para como encontrarlo."
        )
    run_forever_with_backoff()
