"""Backtest engine for the BTC 'Up or Down 5m' prediction game.

Builds 5-minute rounds from real 1-minute BTCUSDT candles and evaluates
each strategy from strategies.py walk-forward: every prediction only sees
data strictly before the round starts (no lookahead / no data leakage).

Data is split into an in-sample half (to look at) and an out-of-sample
half (to confirm), so we don't fool ourselves with a strategy that only
looks good by chance on the full dataset.
"""
import math
from collections import namedtuple

import data_fetch
import strategies as strat_module

Round = namedtuple("Round", ["start_ms", "start_price", "end_price", "outcome"])

ROUND_SECONDS = 5 * 60


def build_rounds(candles):
    """Build non-overlapping 5-minute rounds aligned to the clock (:00,:05,:10,...).

    start_price/end_price use the close of the 1m candle at each boundary,
    matching how the app's countdown resolves a round from t to t+5min.
    """
    by_time = {c["open_time_ms"]: c["close"] for c in candles}
    if not candles:
        return []

    first_ms = candles[0]["open_time_ms"]
    last_ms = candles[-1]["open_time_ms"]

    round_ms = ROUND_SECONDS * 1000
    # align first boundary to a multiple of 5 minutes
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


def evaluate_strategy(strategy_fn, rounds):
    """Walk forward through rounds, predicting each one from only prior data."""
    history = []
    closes = []  # closes strictly before current round start
    correct = 0
    total_signals = 0

    for r in rounds:
        pred = strategy_fn(history, closes)
        if pred is not None:
            total_signals += 1
            if pred == r.outcome:
                correct += 1
        history.append(r.outcome)
        closes.append(r.start_price)

    return correct, total_signals


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

    strategies = strat_module.build_default_strategies()

    header = f"{'estrategia':32s} {'IS n':>6s} {'IS win%':>8s} {'OOS n':>6s} {'OOS win%':>9s} {'OOS IC95%':>18s} {'>breakeven?':>12s}"
    print(header)
    print("-" * len(header))

    results = []
    for strat in strategies:
        name = getattr(strat, "__name__", "unnamed")
        is_correct, is_total = evaluate_strategy(strat, in_sample)
        oos_correct, oos_total = evaluate_strategy(strat, out_sample)

        is_wr = is_correct / is_total * 100 if is_total else float("nan")
        oos_wr = oos_correct / oos_total * 100 if oos_total else float("nan")
        _, lo, hi = wilson_ci(oos_correct, oos_total) if oos_total else (0, 0, 0)

        beats_breakeven = oos_total > 0 and lo > be
        results.append((name, is_wr, is_total, oos_wr, oos_total, lo, hi, beats_breakeven))

        ic_str = f"[{lo*100:5.1f}%,{hi*100:5.1f}%]" if oos_total else "n/a"
        flag = "SI (!)" if beats_breakeven else "no"
        print(f"{name:32s} {is_total:6d} {is_wr:7.1f}% {oos_total:6d} {oos_wr:8.1f}% {ic_str:>18s} {flag:>12s}")

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
