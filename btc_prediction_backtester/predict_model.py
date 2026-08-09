"""Combine every market-read factor into one model and measure it honestly.

Everything tried so far tested factors ONE AT A TIME. A trader reads them
together -- low volatility plus clean trend plus one-sided flow means
something none of those three means alone. This trains a logistic regression
over all of them at once and reports what it actually achieves on rounds it
has never seen.

Design decisions that matter for the answer being trustworthy:

  - Every feature uses candles strictly BEFORE the round opens. No lookahead.
  - Split is chronological, not random: train on the oldest 60%, tune on the
    next 20%, and report on the most recent 20% touched exactly once. Random
    splits leak, because adjacent rounds overlap in their feature windows.
  - The reported number is test-set accuracy with a confidence interval, so
    "better than a coin flip" is a claim that can be checked rather than
    asserted.

    python3 predict_model.py
"""
import math
import random
import statistics

# data_fetch is imported inside main(), not here: predict_next.py and
# market_vs_model.py import this module for features_at() alone, and on a
# phone the historical-download module and its 25 MB cache are neither
# present nor needed.

ROUND_MS = 5 * 60 * 1000


# ---------------------------------------------------------------- features


def features_at(by_time, start, price_now=None):
    """Every feature for the round opening at `start`, or None if any is
    unavailable.

    This is the single source of truth for feature computation: the backtest
    and the live predictor both call it, so they cannot drift apart. That
    mattered here before -- an earlier signal computed one thing offline and
    another thing live, and the difference was invisible until measured.

    `price_now` substitutes for the opening price of the round, which is what
    live use needs: at the boundary the candle does not exist yet, but the
    current trade price is the same number to within a tick.
    """

    def price_at(ms):
        if ms == start and price_now is not None:
            return price_now
        c = by_time.get(ms)
        return c["open"] if c else None

    def pct_move(start_ms, minutes):
        a, b = price_at(start_ms - minutes * 60_000), price_at(start_ms)
        return None if (a is None or b is None or a == 0) else (b - a) / a * 100

    def window(start_ms, minutes):
        out = []
        for m in range(minutes, 0, -1):
            c = by_time.get(start_ms - m * 60_000)
            if c:
                out.append(c)
        return out

    def vol(start_ms, minutes):
        seg = window(start_ms, minutes)
        if len(seg) < 3:
            return None
        px = [c["open"] for c in seg]
        rets = [(px[i] - px[i - 1]) / px[i - 1] for i in range(1, len(px)) if px[i - 1]]
        return statistics.pstdev(rets) * 100 if len(rets) > 1 else None

    def efficiency(start_ms, minutes):
        seg = window(start_ms, minutes)
        if len(seg) < 3:
            return None
        px = [c["open"] for c in seg] + [price_at(start_ms)]
        if px[-1] is None:
            return None
        net = abs(px[-1] - px[0])
        travel = sum(abs(px[i] - px[i - 1]) for i in range(1, len(px)))
        return net / travel if travel else None

    def flow(start_ms, minutes):
        seg = window(start_ms, minutes)
        v = sum(c["volume"] for c in seg)
        b = sum(c["taker_buy_base"] for c in seg)
        return b / v if v > 0 else None

    def range_pos(start_ms, minutes):
        seg = window(start_ms, minutes)
        if len(seg) < 10:
            return None
        hi = max(c["high"] for c in seg)
        lo = min(c["low"] for c in seg)
        now = price_at(start_ms)
        return None if (now is None or hi <= lo) else (now - lo) / (hi - lo)

    if price_at(start) is None:
        return None

    # Volatility relative to its own recent history -- the thing fixed
    # thresholds cannot express.
    v15 = vol(start, 15)
    recent_vols = [vol(start - k * 15 * 60_000, 15) for k in range(1, 7)]
    recent_vols = [x for x in recent_vols if x is not None]
    vol_rank = (
        sum(1 for x in recent_vols if x < v15) / len(recent_vols)
        if (v15 is not None and recent_vols)
        else None
    )

    feats = {
        "mv_5": pct_move(start, 5),
        "mv_15": pct_move(start, 15),
        "mv_60": pct_move(start, 60),
        "vol_15": v15,
        "vol_rank": vol_rank,
        "eff_15": efficiency(start, 15),
        "eff_60": efficiency(start, 60),
        "flow_5": flow(start, 5),
        "flow_15": flow(start, 15),
        "flow_60": flow(start, 60),
        "pos_60": range_pos(start, 60),
        "pos_240": range_pos(start, 240),
    }
    if any(v is None for v in feats.values()):
        return None

    # Interactions a trader reads jointly: a move means one thing in a clean
    # trend and another in chop, and flow matters more when volatile.
    feats["mv15_x_eff15"] = feats["mv_15"] * feats["eff_15"]
    feats["flow15_x_volrank"] = (feats["flow_15"] - 0.5) * feats["vol_rank"]
    feats["mv5_x_volrank"] = feats["mv_5"] * feats["vol_rank"]
    return feats


def build_dataset(candles):
    """One row per round: features known before it opened, plus the outcome."""
    by_time = {c["open_time_ms"]: c for c in candles}
    starts = sorted(t for t in by_time if t % ROUND_MS == 0)

    rows = []
    for start in starts:
        end = by_time.get(start + ROUND_MS)
        open_c = by_time.get(start)
        if end is None or open_c is None:
            continue
        feats = features_at(by_time, start)
        if feats is None:
            continue
        rows.append({
            "t": start,
            "x": feats,
            "y": 1 if end["open"] > open_c["open"] else 0,
        })
    return rows


# ------------------------------------------------------- logistic regression


def standardize(rows, keys, stats=None):
    if stats is None:
        stats = {}
        for k in keys:
            vals = [r["x"][k] for r in rows]
            mu = statistics.fmean(vals)
            sd = statistics.pstdev(vals) or 1.0
            stats[k] = (mu, sd)
    out = []
    for r in rows:
        out.append(
            {
                "v": [(r["x"][k] - stats[k][0]) / stats[k][1] for k in keys],
                "y": r["y"],
            }
        )
    return out, stats


def train_logistic(data, keys, lr=0.05, epochs=40, l2=0.01, seed=0):
    """SGD with a decaying step. Tens of thousands of rounds means the fit
    settles well before 40 passes; the decay is what stops it from bouncing
    around the minimum instead of landing on it."""
    rnd = random.Random(seed)
    w = [0.0] * len(keys)
    b = 0.0
    idx = list(range(len(data)))
    for ep in range(epochs):
        step = lr / (1 + ep * 0.5)
        rnd.shuffle(idx)
        for i in idx:
            v, y = data[i]["v"], data[i]["y"]
            z = b + sum(wi * vi for wi, vi in zip(w, v))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            err = p - y
            for j in range(len(w)):
                w[j] -= step * (err * v[j] + l2 * w[j])
            b -= step * err
    return w, b


def predict(w, b, v):
    z = b + sum(wi * vi for wi, vi in zip(w, v))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / d
    return (p, max(0.0, c - m), min(1.0, c + m))


def evaluate(w, b, data, label, threshold=0.5):
    correct = sum(1 for d in data if (predict(w, b, d["v"]) >= threshold) == (d["y"] == 1))
    p, lo, hi = wilson(correct, len(data))
    print(f"  {label:<26} {correct:>5}/{len(data):<6} {p*100:>6.2f}%   IC95% [{lo*100:.2f}%, {hi*100:.2f}%]")
    return p, lo, hi


def evaluate_confident(w, b, data, label, margin):
    """Accuracy when the model only commits on its most confident calls --
    the "solo cuando esta seguro" idea, measured rather than assumed."""
    picked = [(predict(w, b, d["v"]), d["y"]) for d in data]
    picked = [(p, y) for p, y in picked if abs(p - 0.5) >= margin]
    if len(picked) < 30:
        print(f"  {label:<26} solo {len(picked)} rondas superan el umbral")
        return
    correct = sum(1 for p, y in picked if (p >= 0.5) == (y == 1))
    pr, lo, hi = wilson(correct, len(picked))
    cobertura = len(picked) / len(data) * 100
    print(
        f"  {label:<26} {correct:>5}/{len(picked):<6} {pr*100:>6.2f}%   "
        f"IC95% [{lo*100:.2f}%, {hi*100:.2f}%]  (cubre {cobertura:.0f}% de rondas)"
    )


def main():
    import data_fetch

    print("Cargando datos historicos...")
    candles = data_fetch.load_cached()
    rows = build_dataset(candles)
    keys = sorted(rows[0]["x"].keys())
    print(f"Rondas utilizables: {len(rows):,}   features: {len(keys)}")

    rows.sort(key=lambda r: r["t"])
    n = len(rows)
    a, bnd = int(n * 0.6), int(n * 0.8)
    tr_raw, va_raw, te_raw = rows[:a], rows[a:bnd], rows[bnd:]
    print(f"Split cronologico -> train {len(tr_raw):,} | val {len(va_raw):,} | test {len(te_raw):,}")

    tr, stats = standardize(tr_raw, keys)
    va, _ = standardize(va_raw, keys, stats)
    te, _ = standardize(te_raw, keys, stats)

    print("\nEntrenando...")
    best = None
    for l2 in (0.001, 0.01, 0.1, 1.0):
        w, b = train_logistic(tr, keys, l2=l2)
        acc = sum(1 for d in va if (predict(w, b, d["v"]) >= 0.5) == (d["y"] == 1)) / len(va)
        print(f"  l2={l2:<6} val {acc*100:.2f}%")
        if best is None or acc > best[0]:
            best = (acc, l2, w, b)

    _, l2, w, b = best
    print(f"\nElegido l2={l2} por validacion. El test se mira UNA sola vez.\n")

    print("Resultados:")
    evaluate(w, b, tr, "train (visto)")
    evaluate(w, b, va, "validacion")
    p, lo, hi = evaluate(w, b, te, "TEST (nunca visto)")

    print("\nSolo cuando el modelo esta mas seguro:")
    for margin in (0.02, 0.05, 0.10):
        evaluate_confident(w, b, te, f"confianza >= {0.5+margin:.2f}", margin)

    print("\nPesos aprendidos (estandarizados, mayor = mas influyente):")
    for k, wi in sorted(zip(keys, w), key=lambda kv: -abs(kv[1]))[:8]:
        signo = "Up" if wi > 0 else "Down"
        print(f"  {k:<20} {wi:+.4f}  (empuja hacia {signo})")

    print("\n" + "=" * 66)
    print("VEREDICTO")
    print("=" * 66)
    breakeven = 1 / (2 - 0.02)  # even-money bet at the confirmed 2% fee
    print(f"  Acierto out-of-sample: {p*100:.2f}%  IC95% [{lo*100:.2f}%, {hi*100:.2f}%]")
    print(f"  Necesario para no perder con fee 2%: {breakeven*100:.2f}%")
    if lo > breakeven:
        print("\n  El limite inferior supera el breakeven: hay señal real y explotable.")
    elif lo > 0.5:
        print("\n  Supera 50% de forma significativa, pero NO cubre la fee del 2%.")
        print("  Hay algo de señal; no alcanza para ganar dinero.")
    elif hi < 0.5:
        print("\n  Significativamente PEOR que el azar.")
    else:
        print("\n  Indistinguible de tirar una moneda.")
        print("  Combinar los factores no agrega nada sobre probarlos de a uno:")
        print("  la informacion no esta ahi.")


if __name__ == "__main__":
    main()
