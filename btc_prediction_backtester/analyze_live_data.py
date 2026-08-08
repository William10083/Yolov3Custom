"""Analyze the live data collected by local_odds_logger.py.

Run this on the device that has the CSVs:

    python3 analyze_live_data.py

Prints a compact summary meant to be pasted back into the conversation --
no need to move thousands of rows around.

The four questions it answers, in order of how much they matter:

  1. Is the market's own price well calibrated? If the market's quoted
     probability matches how often things actually happen, there is no edge
     to find, full stop. This is the single most important test and it needs
     live odds, which is exactly what the logger has been collecting.
  2. Does the price we paid predict whether we lost? Losing more on the
     cheap entries is the fingerprint of adverse selection -- getting filled
     precisely when the other side knows better.
  3. Is there a dislocation? Does the token price lag BTC's move enough to
     trade against, which is the one within-round entry idea that a live
     trading writeup reported as promising.
  4. How bad is the book quality? Quantifies how often quotes are unusable.
"""
import csv
import json
import math
import os
import statistics
from collections import defaultdict

DATA = os.path.join(os.path.dirname(__file__), "data")
ODDS = os.path.join(DATA, "live_odds_log.csv")
OUTCOMES = os.path.join(DATA, "round_outcomes.csv")

SIGMA_5MIN_PCT = 0.1445


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def wilson(successes, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = successes / n
    d = 1 + z**2 / n
    c = (p + z**2 / (2 * n)) / d
    m = (z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5)) / d
    return (p, max(0.0, c - m), min(1.0, c + m))


def load(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def fnum(row, key):
    v = (row.get(key) or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def section(title):
    print(f"\n{'=' * 58}\n{title}\n{'=' * 58}")


def q1_market_calibration(odds, outcomes):
    """Bucket every snapshot by the market's implied probability of Up, then
    check how often Up actually happened. A market that is right about its
    own odds leaves nothing on the table."""
    section("1. ¿El mercado esta bien calibrado?")

    outcome_by_market = {}
    for r in outcomes:
        mid = (r.get("market_id") or "").strip()
        out = (r.get("outcome") or "").strip()
        if mid and out in ("Up", "Down"):
            outcome_by_market[mid] = out

    buckets = defaultdict(lambda: [0, 0])
    used = 0
    for r in odds:
        mid_price = fnum(r, "mid_price")
        market_id = (r.get("market_id") or "").strip()
        if mid_price is None or market_id not in outcome_by_market:
            continue
        # mid_price is the price of the "Up" token = market's P(Up)
        b = round(mid_price * 10) / 10
        buckets[b][1] += 1
        if outcome_by_market[market_id] == "Up":
            buckets[b][0] += 1
        used += 1

    if not used:
        print("  Sin datos cruzables todavia (falta market_id en comun).")
        return

    print(f"  Snapshots utilizables: {used}\n")
    print(f"  {'precio':>7} {'n':>6} {'Up real':>9} {'error':>8}   veredicto")
    total_abs_err = weight = 0
    for b in sorted(buckets):
        ups, n = buckets[b]
        if n < 30:
            continue
        real = ups / n
        err = real - b
        total_abs_err += abs(err) * n
        weight += n
        verdict = "bien" if abs(err) < 0.08 else "DESVIADO"
        print(f"  {b:>7.2f} {n:>6} {real*100:>8.1f}% {err*100:>+7.1f}pp   {verdict}")

    if weight:
        print(f"\n  Error medio ponderado: {total_abs_err/weight*100:.1f}pp")
        print("  Si ronda 0-5pp, el mercado esta bien cotizado y no hay ventaja que sacar.")


def q2_adverse_selection(outcomes):
    """Win rate by the price we paid. Losing more on cheap fills is the
    signature of being picked off."""
    section("2. ¿El precio que pagamos predice que perdimos?")

    graded = [
        r for r in outcomes
        if (r.get("signal_correct") or "").strip() in ("True", "False")
        and fnum(r, "entry_price") is not None
    ]
    if len(graded) < 10:
        print(f"  Solo {len(graded)} apuestas con precio registrado. Hacen falta ~30+.")
        print("  (entry_price se empezo a guardar recien; segui recolectando.)")
        return

    buckets = defaultdict(lambda: [0, 0])
    for r in graded:
        p = fnum(r, "entry_price")
        b = round(p * 20) / 20  # 0.05 wide
        buckets[b][1] += 1
        if r["signal_correct"] == "True":
            buckets[b][0] += 1

    print(f"  Apuestas evaluadas: {len(graded)}\n")
    print(f"  {'precio pagado':>14} {'n':>5} {'acierto':>9} {'IC95%':>18}")
    for b in sorted(buckets):
        w, n = buckets[b]
        if n < 5:
            continue
        p, lo, hi = wilson(w, n)
        print(f"  {b:>14.2f} {n:>5} {p*100:>8.1f}% [{lo*100:>5.1f}%,{hi*100:>5.1f}%]")
    print("\n  Si el acierto CAE cuando el precio es mas barato, es seleccion adversa:")
    print("  nos llenan justo cuando el otro lado tiene razon.")


def q3_dislocation(odds):
    """Does the token price lag BTC? Compare the market's implied probability
    against fair value from the live gap. A persistent lag would be the one
    within-round entry worth having."""
    section("3. ¿Hay dislocacion (el token va atrasado respecto a BTC)?")

    diffs = []
    by_round = defaultdict(list)
    for r in odds:
        mid_price = fnum(r, "mid_price")
        btc = fnum(r, "btc_price")
        start = fnum(r, "round_start_price")
        if None in (mid_price, btc, start) or start <= 0:
            continue
        sigma_abs = start * SIGMA_5MIN_PCT / 100
        if sigma_abs <= 0:
            continue
        fair_up = normal_cdf((btc - start) / sigma_abs)
        diffs.append(mid_price - fair_up)
        by_round[(r.get("market_id") or "").strip()].append(mid_price - fair_up)

    if len(diffs) < 100:
        print(f"  Solo {len(diffs)} snapshots utilizables.")
        return

    mean = statistics.fmean(diffs)
    sd = statistics.pstdev(diffs)
    big = sum(1 for d in diffs if abs(d) > 0.10)
    print(f"  Snapshots: {len(diffs)}")
    print(f"  Diferencia media (mercado - valor justo): {mean*100:+.1f}pp")
    print(f"  Desvio: {sd*100:.1f}pp")
    print(f"  Snapshots con diferencia > 10pp: {big} ({big/len(diffs)*100:.1f}%)")
    print()
    if sd > 0.15:
        print("  Dispersion enorme -> el precio del libro es ruido, no una senal de")
        print("  dislocacion. No se puede operar contra eso.")
    elif abs(mean) < 0.02 and sd < 0.08:
        print("  El mercado sigue de cerca al valor justo. No hay lag explotable.")
    else:
        print("  Hay una diferencia sistematica; vale la pena mirarla mas de cerca.")


def q4_book_quality(odds):
    section("4. Calidad del libro de ordenes")

    spreads = []
    empty = 0
    total = 0
    for r in odds:
        bid = fnum(r, "best_bid")
        ask = fnum(r, "best_ask")
        total += 1
        if bid is None or ask is None:
            empty += 1
            continue
        spreads.append(ask - bid)

    if not spreads:
        print("  Sin spreads calculables.")
        return

    spreads.sort()
    def pct(p):
        return spreads[min(len(spreads) - 1, int(len(spreads) * p))]

    wide = sum(1 for s in spreads if s > 0.04)
    print(f"  Snapshots totales: {total}")
    print(f"  Sin una de las dos puntas: {empty} ({empty/total*100:.1f}%)")
    print(f"  Spread mediano: {pct(0.5):.3f}")
    print(f"  Spread p90:     {pct(0.9):.3f}")
    print(f"  Spread > 0.04 (no confiable): {wide} ({wide/len(spreads)*100:.1f}%)")


def overall(outcomes):
    section("Resumen")
    graded = [r for r in outcomes if (r.get("signal_correct") or "").strip() in ("True", "False")]
    if graded:
        w = sum(1 for r in graded if r["signal_correct"] == "True")
        p, lo, hi = wilson(w, len(graded))
        z = (w - len(graded) * 0.5) / math.sqrt(len(graded) * 0.25)
        print(f"  Apuestas evaluadas: {len(graded)}")
        print(f"  Acierto: {w}/{len(graded)} = {p*100:.1f}%  IC95% [{lo*100:.1f}%, {hi*100:.1f}%]")
        print(f"  z vs moneda al aire: {z:+.2f}")
        if hi < 0.5:
            print("  -> Significativamente PEOR que el azar.")
        elif lo > 0.5:
            print("  -> Significativamente MEJOR que el azar.")
        else:
            print("  -> Indistinguible del azar con esta muestra.")

    per = defaultdict(lambda: [0, 0])
    for r in graded:
        name = (r.get("signal_name") or "(sin registrar)").strip()
        per[name][1] += 1
        if r["signal_correct"] == "True":
            per[name][0] += 1
    if per:
        print("\n  Por senal:")
        for name, (w, n) in sorted(per.items(), key=lambda kv: -kv[1][1]):
            if n < 3:
                continue
            print(f"    {name}: {w}/{n} ({w/n*100:.0f}%)")

    resolved = [r for r in outcomes if (r.get("outcome") or "").strip() in ("Up", "Down")]
    ups = sum(1 for r in resolved if r["outcome"] == "Up")
    if resolved:
        print(f"\n  Rondas resueltas registradas: {len(resolved)} ({ups} Up / {len(resolved)-ups} Down)")


if __name__ == "__main__":
    odds = load(ODDS)
    outcomes = load(OUTCOMES)
    print(f"live_odds_log.csv: {len(odds)} filas")
    print(f"round_outcomes.csv: {len(outcomes)} filas")

    q1_market_calibration(odds, outcomes)
    q2_adverse_selection(outcomes)
    q3_dislocation(odds)
    q4_book_quality(odds)
    overall(outcomes)

    print("\n\nPegame todo esto y lo analizo.")
