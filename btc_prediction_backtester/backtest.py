"""Backtest engine for the BTC 'Up or Down 5m' prediction game.

Builds 5-minute rounds from real 1-minute BTCUSDT candles and evaluates
each strategy from strategies.py walk-forward: every prediction only sees
data strictly before the round starts (no lookahead / no data leakage).

Data is split into an in-sample half (to look at) and an out-of-sample
half (to confirm), so we don't fool ourselves with a strategy that only
looks good by chance on the full dataset.

Also runs a variance-ratio test on 5-minute returns: a principled,
model-free diagnostic for momentum/mean-reversion in the price series
itself, independent of any specific hand-tuned rule (helps sanity-check
whether brute-force rule search is even worth it).
"""
import statistics

from collections import namedtuple

import data_fetch
import strategies as strat_module

Round = namedtuple("Round", ["start_ms", "start_price", "end_price", "outcome"])

ROUND_SECONDS = 5 * 60


def build_rounds(candles):
    """Build non-overlapping 5-minute rounds aligned to the clock (:00,:05,:10,...).

    Per the app's own rules ("use the open price of the candlestick
    corresponding to the market's end time"), start_price/end_price use the
    OPEN of the 1m candle at each boundary, not the close.
    """
    by_time = {c["open_time_ms"]: c["open"] for c in candles}
    if not candles:
        return []

    first_ms = candles[0]["open_time_ms"]
    last_ms = candles[-1]["open_time_ms"]

    round_ms = ROUND_SECONDS * 1000
    start = (first_ms // round_ms) * round_ms
    if start < first_ms:
        start += round_ms

    rounds = []
    t = start
    while t + round_ms <= last_ms:
        if t in by_time and (t + round_ms) in by_time:
            start_price = by_time[t]
            end_price = by_time[t + round_ms]
            outcome = "Up" if end_price > start_price else "Down"
            rounds.append(Round(t, start_price, end_price, outcome))
        t += round_ms
    return rounds


def wilson_ci(successes, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5)) / denom
    return (p, max(0.0, center - margin), min(1.0, center + margin))


def breakeven_winrate(fee_pct):
    """Win rate needed to break even on an even-money bet where the
    platform takes `fee_pct` out of net winnings (a simplification of a
    pari-mutuel rake). E.g. fee_pct=10 -> need > 52.6% to profit long-run."""
    f = fee_pct / 100
    return 1 / (2 - f)


def evaluate_strategy(strategy_fn, rounds, ctx):
    """Walk forward through rounds, predicting each one from only prior data."""
    history = []
    correct = 0
    total_signals = 0

    for r in rounds:
        pred = strategy_fn(history, ctx, r.start_ms)
        if pred is not None:
            total_signals += 1
            if pred == r.outcome:
                correct += 1
        history.append(r.outcome)

    return correct, total_signals


def variance_ratio_test(rounds, k=2):
    """Lo-MacKinlay style variance ratio test on 5-min round returns.

    VR(k) = Var(k-period return) / (k * Var(1-period return))
    VR ~ 1   -> consistent with a random walk (no exploitable structure)
    VR < 1   -> mean reversion (moves tend to partially reverse)
    VR > 1   -> momentum/trending (moves tend to continue)

    This looks at the price series itself, not any specific betting rule,
    so it's a cleaner way to ask "is there structure here at all?" before
    trusting any brute-force strategy search.
    """
    prices = [rounds[0].start_price] + [r.end_price for r in rounds]
    rets = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
    n = len(rets)
    if n < k * 30:
        return None

    var1 = statistics.pvariance(rets)
    k_rets = [sum(rets[i : i + k]) for i in range(0, n - k + 1, k)]
    vark = statistics.pvariance(k_rets)
    if var1 == 0:
        return None
    vr = vark / (k * var1)

    # approximate standard error under the random-walk null (homoskedastic case)
    m = len(k_rets)
    se = ((2 * (2 * k - 1) * (k - 1)) / (3 * k * n)) ** 0.5
    z = (vr - 1) / se if se > 0 else float("nan")
    return {"k": k, "vr": vr, "z": z, "n_returns": n, "n_k_blocks": m}


def run(candles, fee_pct=10.0):
    rounds = build_rounds(candles)
    n_rounds = len(rounds)
    half = n_rounds // 2
    in_sample = rounds[:half]
    out_sample = rounds[half:]

    print(f"Total de rondas de 5 min construidas: {n_rounds}")
    print(f"  In-sample:     {len(in_sample)} rondas")
    print(f"  Out-of-sample: {len(out_sample)} rondas")
    print()
    up_pct = sum(1 for r in rounds if r.outcome == "Up") / n_rounds * 100
    print(f"Baseline real Up/Down en todo el periodo: {up_pct:.1f}% Up / {100-up_pct:.1f}% Down")

    be = breakeven_winrate(fee_pct)
    print(f"Con una comision asumida de {fee_pct:.0f}%, hace falta un win rate > {be*100:.1f}% para ganar en el largo plazo")
    print()

    print("Variance ratio test (estructura del precio en si, sin ninguna regla):")
    for k in (2, 3, 5, 10):
        res = variance_ratio_test(rounds, k=k)
        if res is None:
            continue
        verdict = "random walk (VR~1)"
        if res["z"] > 2:
            verdict = "MOMENTUM significativo (VR>1, z>2)"
        elif res["z"] < -2:
            verdict = "MEAN REVERSION significativo (VR<1, z<-2)"
        print(f"  k={res['k']:2d}: VR={res['vr']:.3f}  z={res['z']:+.2f}  -> {verdict}")
    print()

    ctx = strat_module.MarketContext(candles)
    strategies = strat_module.build_default_strategies()

    header = f"{'estrategia':40s} {'IS n':>6s} {'IS win%':>8s} {'OOS n':>6s} {'OOS win%':>9s} {'OOS IC95%':>18s} {'>breakeven?':>12s}"
    print(header)
    print("-" * len(header))

    results = []
    for strat in strategies:
        name = getattr(strat, "__name__", "unnamed")
        is_correct, is_total = evaluate_strategy(strat, in_sample, ctx)
        oos_correct, oos_total = evaluate_strategy(strat, out_sample, ctx)

        is_wr = is_correct / is_total * 100 if is_total else float("nan")
        oos_wr = oos_correct / oos_total * 100 if oos_total else float("nan")
        _, lo, hi = wilson_ci(oos_correct, oos_total) if oos_total else (0, 0, 0)

        beats_breakeven = oos_total > 0 and lo > be
        results.append((name, is_wr, is_total, oos_wr, oos_total, lo, hi, beats_breakeven))

        ic_str = f"[{lo*100:5.1f}%,{hi*100:5.1f}%]" if oos_total else "n/a"
        flag = "SI (!)" if beats_breakeven else "no"
        print(f"{name:40s} {is_total:6d} {is_wr:7.1f}% {oos_total:6d} {oos_wr:8.1f}% {ic_str:>18s} {flag:>12s}")

    print()
    winners = [r for r in results if r[7]]
    if not winners:
        print(
            "Ninguna estrategia supera el breakeven de forma estadisticamente "
            "significativa (limite inferior del IC95% out-of-sample > breakeven) "
            "en este periodo de datos."
        )
    else:
        print(f"{len(winners)} estrategia(s) superaron breakeven de forma significativa -- revisar con cuidado (posible sobreajuste, cambia con el periodo).")
        for w in winners:
            print(f"  - {w[0]}")

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--fee", type=float, default=10.0, help="Comision asumida en %%")
    args = parser.parse_args()

    candles = data_fetch.load_cached()
    run(candles, fee_pct=args.fee)
