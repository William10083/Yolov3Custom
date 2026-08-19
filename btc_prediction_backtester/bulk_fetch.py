"""Download years of 1-minute klines from Binance's monthly archives.

data_fetch.py pages the REST API 1000 candles at a time, which is fine for
180 days and hopeless for years. The public archive at data.binance.vision
serves the same data as monthly ZIPs, so four years arrives in a couple of
minutes instead of a couple of thousand requests.

Two traps in these files, both silent if unhandled:

  - Newer archives timestamp in MICROseconds, older ones in milliseconds.
    Mixing them shifts part of the history by a factor of 1000 and every
    round boundary lands in the wrong place.
  - Some months ship a header row, some do not.

Both are detected per row rather than assumed per file.

    python3 bulk_fetch.py --months 48
"""
import argparse
import csv
import io
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://data.binance.vision/data/spot/monthly/klines"
SYMBOL = "BTCUSDT"
OUT = os.path.join(os.path.dirname(__file__), "data", f"{SYMBOL.lower()}_1m_long.csv")
HEADER = ["open_time_ms", "open", "high", "low", "close", "volume", "taker_buy_base"]


def month_list(n):
    today = datetime.now(timezone.utc).replace(day=1)
    months = []
    for i in range(1, n + 1):
        d = today - timedelta(days=1)
        for _ in range(i - 1):
            d = d.replace(day=1) - timedelta(days=1)
        months.append((d.year, d.month))
    return sorted(set(months))


def normalize_ms(raw):
    """Archives switched from ms to microseconds partway through."""
    v = int(raw)
    return v // 1000 if v > 10**14 else v


def fetch_month(year, month):
    url = f"{BASE}/{SYMBOL}/1m/{SYMBOL}-1m-{year}-{month:02d}.zip"
    r = requests.get(url, timeout=120)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    rows = []
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                parts = line.strip().split(",")
                if len(parts) < 10:
                    continue
                try:
                    t = normalize_ms(parts[0])
                except ValueError:
                    continue  # header row
                rows.append([
                    t, parts[1], parts[2], parts[3], parts[4], parts[5], parts[9],
                ])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=48)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    seen = set()
    total = 0
    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for year, month in month_list(args.months):
            sys.stdout.write(f"\r  {year}-{month:02d} ... ")
            sys.stdout.flush()
            try:
                rows = fetch_month(year, month)
            except Exception as exc:
                print(f"\n  [{year}-{month:02d}] error: {exc}")
                continue
            if rows is None:
                sys.stdout.write("no disponible")
                continue
            kept = 0
            for r in rows:
                if r[0] in seen:
                    continue
                seen.add(r[0])
                w.writerow(r)
                kept += 1
            total += kept
            sys.stdout.write(f"{kept:,} velas   (total {total:,})")
    print()

    if total:
        lo, hi = min(seen), max(seen)
        d0 = datetime.fromtimestamp(lo / 1000, tz=timezone.utc)
        d1 = datetime.fromtimestamp(hi / 1000, tz=timezone.utc)
        gaps = (hi - lo) // 60_000 + 1 - total
        print(f"\nGuardado en {OUT}")
        print(f"  {total:,} velas de {d0:%Y-%m-%d} a {d1:%Y-%m-%d}")
        print(f"  minutos faltantes: {gaps:,}  ({gaps/((hi-lo)//60_000+1)*100:.2f}%)")
        print(f"  rondas de 5 min aproximadas: {total//5:,}")


if __name__ == "__main__":
    main()
