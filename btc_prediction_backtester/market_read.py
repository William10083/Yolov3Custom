"""A trader's read of the market as it stands right now.

Everything here is measured against the RECENT market, not against constants
frozen from a 180-day backtest. "High volatility" means high compared to the
last few hours, not compared to last spring. The same move means different
things in a quiet market and a violent one, and a fixed threshold cannot tell
them apart.

This describes the current state. It does not forecast, and it does not
recommend a side -- the measurements in this repo show the market's own price
already carries whatever a signal like this could add. What it is for is
knowing what kind of market you are looking at before deciding anything.

    python3 market_read.py            # una lectura
    python3 market_read.py --watch    # se actualiza cada 30s
"""
import argparse
import math
import statistics
import sys
import time
from datetime import datetime, timezone

import requests

KLINES = "https://data-api.binance.vision/api/v3/klines"
SYMBOL = "BTCUSDT"


def fetch(minutes=240):
    r = requests.get(
        KLINES, params={"symbol": SYMBOL, "interval": "1m", "limit": minutes}, timeout=15
    )
    r.raise_for_status()
    rows = r.json()
    return [
        {
            "t": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "vol": float(k[5]),
            "buy": float(k[9]),
        }
        for k in rows
    ]


def pct_rank(value, sample):
    """Where `value` sits inside `sample`, 0..1. This is what makes a reading
    relative to the current market instead of to a fixed number."""
    if not sample:
        return None
    below = sum(1 for s in sample if s < value)
    return below / len(sample)


def realized_vol(c, window):
    """Stdev of 1-minute returns over the last `window` minutes, in %."""
    seg = c[-window:]
    if len(seg) < 3:
        return None
    rets = [
        (seg[i]["close"] - seg[i - 1]["close"]) / seg[i - 1]["close"]
        for i in range(1, len(seg))
        if seg[i - 1]["close"]
    ]
    return statistics.pstdev(rets) * 100 if len(rets) > 1 else None


def rolling_vols(c, window, count=60):
    """The same measure taken repeatedly over the recent past, so the current
    reading can be ranked against its own recent history."""
    out = []
    for end in range(len(c) - count, len(c)):
        if end - window < 0:
            continue
        seg = c[end - window : end]
        rets = [
            (seg[i]["close"] - seg[i - 1]["close"]) / seg[i - 1]["close"]
            for i in range(1, len(seg))
            if seg[i - 1]["close"]
        ]
        if len(rets) > 1:
            out.append(statistics.pstdev(rets) * 100)
    return out


def efficiency(c, window):
    """|net move| / total distance travelled. Near 1 = clean trend, near 0 =
    chop that ended up nowhere. Same net move, very different market."""
    seg = c[-window:]
    if len(seg) < 3:
        return None
    net = abs(seg[-1]["close"] - seg[0]["close"])
    travel = sum(abs(seg[i]["close"] - seg[i - 1]["close"]) for i in range(1, len(seg)))
    return net / travel if travel else None


def flow(c, window):
    seg = c[-window:]
    v = sum(x["vol"] for x in seg)
    b = sum(x["buy"] for x in seg)
    return b / v if v > 0 else None


def bar(value, lo=0.0, hi=1.0, width=20):
    if value is None:
        return "?" * width
    f = max(0.0, min(1.0, (value - lo) / (hi - lo) if hi > lo else 0))
    n = int(round(f * width))
    return "█" * n + "·" * (width - n)


def seconds_into_round(now=None):
    now = now if now is not None else time.time()
    return int(now) % 300


def read(c):
    now_price = c[-1]["close"]
    lines = []

    lines.append(f"₿  ${now_price:,.2f}   ·   {datetime.now(timezone.utc):%H:%M:%S} UTC")

    secs = seconds_into_round()
    lines.append(f"⏱  Ronda actual: {secs//60}:{secs%60:02d} transcurridos, faltan {(300-secs)//60}:{(300-secs)%60:02d}")

    # --- Volatility, ranked against its own recent history ---
    lines.append("")
    lines.append("VOLATILIDAD")
    v15 = realized_vol(c, 15)
    hist = rolling_vols(c, 15, count=90)
    rank = pct_rank(v15, hist) if (v15 is not None and hist) else None
    if v15 is not None:
        etiqueta = "?"
        if rank is not None:
            etiqueta = (
                "muy baja" if rank < 0.2 else
                "baja" if rank < 0.4 else
                "normal" if rank < 0.6 else
                "alta" if rank < 0.8 else "MUY ALTA"
            )
        lines.append(f"  ultimos 15min: {v15:.4f}%/min   {etiqueta}")
        if rank is not None:
            lines.append(f"  vs ultimas 1.5h: {bar(rank)} percentil {rank*100:.0f}")
        # What that volatility implies for a 5-minute round, right now.
        sigma5 = v15 * math.sqrt(5) / 100 * now_price
        lines.append(f"  → un movimiento tipico de ronda hoy: ±${sigma5:,.0f}")

    # --- Regime: trend or range ---
    lines.append("")
    lines.append("REGIMEN")
    eff15 = efficiency(c, 15)
    eff60 = efficiency(c, 60)
    if eff15 is not None:
        etiqueta = (
            "TENDENCIA limpia" if eff15 > 0.5 else
            "tendencia leve" if eff15 > 0.3 else
            "RANGO / lateral" if eff15 > 0.15 else "chop puro"
        )
        lines.append(f"  15min: {bar(eff15)} {eff15:.2f}  {etiqueta}")
    if eff60 is not None:
        lines.append(f"  60min: {bar(eff60)} {eff60:.2f}")

    m15 = (c[-1]["close"] - c[-16]["close"]) / c[-16]["close"] * 100 if len(c) > 16 else None
    m60 = (c[-1]["close"] - c[-61]["close"]) / c[-61]["close"] * 100 if len(c) > 61 else None
    if m15 is not None:
        lines.append(f"  movimiento 15min: {m15:+.3f}%   60min: {m60:+.3f}%" if m60 is not None
                     else f"  movimiento 15min: {m15:+.3f}%")

    # --- Order flow ---
    lines.append("")
    lines.append("FLUJO DE ORDENES")
    for w in (5, 15, 60):
        f = flow(c, w)
        if f is not None:
            etiqueta = (
                "compra fuerte" if f > 0.60 else
                "compra" if f > 0.54 else
                "equilibrado" if f > 0.46 else
                "venta" if f > 0.40 else "venta fuerte"
            )
            lines.append(f"  {w:>2}min: {bar(f, 0.3, 0.7)} {f*100:.0f}% compra   {etiqueta}")

    # --- Position within the recent range ---
    lines.append("")
    lines.append("POSICION EN EL RANGO")
    for w, nombre in ((60, "1h"), (240, "4h")):
        seg = c[-w:]
        if len(seg) < 10:
            continue
        hi = max(x["high"] for x in seg)
        lo = min(x["low"] for x in seg)
        if hi > lo:
            pos = (now_price - lo) / (hi - lo)
            etiqueta = (
                "techo del rango" if pos > 0.85 else
                "parte alta" if pos > 0.6 else
                "medio" if pos > 0.4 else
                "parte baja" if pos > 0.15 else "piso del rango"
            )
            lines.append(f"  {nombre}: {bar(pos)} {pos*100:.0f}%   {etiqueta}")
            lines.append(f"      ${lo:,.0f} ←→ ${hi:,.0f}  (rango ${hi-lo:,.0f})")

    # --- What this implies for the round in progress ---
    lines.append("")
    lines.append("LECTURA")
    if v15 is not None and rank is not None:
        sigma5 = v15 * math.sqrt(5) / 100 * now_price
        if rank > 0.75:
            lines.append("  Volatilidad alta para el momento: las rondas se definen por")
            lines.append(f"  movimientos grandes (±${sigma5:,.0f}), no por centavos.")
        elif rank < 0.25:
            lines.append("  Mercado quieto: muchas rondas se van a definir por muy poco,")
            lines.append("  practicamente un volado.")
    if eff15 is not None:
        if eff15 > 0.5:
            lines.append("  Direccional: el precio va a algun lado, no rebota.")
        elif eff15 < 0.2:
            lines.append("  Sin direccion: sube y baja sin avanzar. Cualquier lectura")
            lines.append("  direccional acá es ruido.")

    lines.append("")
    lines.append("  (Esto describe el estado actual. No predice la ronda: las mediciones")
    lines.append("   de este repo muestran que el precio del mercado ya incorpora esto.)")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="actualizar cada 30s")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    while True:
        try:
            c = fetch()
            out = read(c)
            if args.watch:
                print("\033[2J\033[H", end="")
            print(out)
        except Exception as exc:
            print(f"[error] {exc}", file=sys.stderr)
        if not args.watch:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
