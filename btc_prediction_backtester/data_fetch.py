"""Download and cache historical BTCUSDT 1-minute candles.

Uses Binance's public market-data mirror (data-api.binance.vision), which
serves read-only historical klines with no API key and no order placement
capability. No trading, no account access -- just historical prices for
research/backtesting.
"""
import csv
import os
import time
import requests

BASE_URL = "https://data-api.binance.vision/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
CACHE_DIR = os.path.join(os.path.dirname(__file__), "data")
CACHE_FILE = os.path.join(CACHE_DIR, "btcusdt_1m.csv")


def fetch_klines(start_ms, end_ms, limit=1000):
    """Yield raw kline rows between start_ms and end_ms (inclusive-ish)."""
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "startTime": cursor,
            "limit": limit,
        }
        resp = requests.get(BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for row in rows:
            open_time = row[0]
            if open_time > end_ms:
                return
            yield row
        cursor = rows[-1][0] + 60_000  # advance one minute past last candle
        if len(rows) < limit:
            break
        time.sleep(0.15)  # be polite to the public endpoint


def download(days=45):
    os.makedirs(CACHE_DIR, exist_ok=True)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000

    print(f"Descargando ~{days} dias de velas de 1m para {SYMBOL}...")
    count = 0
    with open(CACHE_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["open_time_ms", "open", "high", "low", "close", "volume"])
        for row in fetch_klines(start_ms, end_ms):
            writer.writerow([row[0], row[1], row[2], row[3], row[4], row[5]])
            count += 1
            if count % 5000 == 0:
                print(f"  {count} velas descargadas...")
    print(f"Listo: {count} velas guardadas en {CACHE_FILE}")
    return CACHE_FILE


def load_cached():
    """Load cached candles as a list of dicts sorted by open_time_ms."""
    if not os.path.exists(CACHE_FILE):
        raise FileNotFoundError(
            f"No hay datos cacheados en {CACHE_FILE}. Corre download() primero."
        )
    rows = []
    with open(CACHE_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(
                {
                    "open_time_ms": int(r["open_time_ms"]),
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["volume"]),
                }
            )
    rows.sort(key=lambda r: r["open_time_ms"])
    return rows


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=45)
    args = parser.parse_args()
    download(days=args.days)
