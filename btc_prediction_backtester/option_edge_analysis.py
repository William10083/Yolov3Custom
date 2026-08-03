"""Is the market's live price (the %) a well-calibrated probability, or does
it ignore the mean-reversion effect we found?

The app's rules confirm this is resolved via a Chainlink oracle and priced
like a CLOB binary-option market (buy "Up" shares at the displayed %,
each share pays $1 if correct) -- the same structure as Polymarket's BTC
5-min markets. That means the displayed % is effectively "the market's
current estimate of P(Up)", almost certainly computed by market makers
assuming BTC moves like a simple random walk (no serial correlation) over
the remaining time.

We already found (backtest.py's variance-ratio test) that BTC 5-min
returns have small but real mean reversion. If market makers price purely
off a random-walk / no-momentum model, they will be systematically WRONG
in the specific situation where a sharp recent move created the current
price gap -- because the real world reverts a bit more than a pure random
walk would. This script tests exactly that, using only historical price
data (no need for real screenshots of the odds, since we compute what a
naive fair price WOULD be from data and compare it to actual outcomes).

Methodology:
  1. Estimate sigma(k minutes) -- historical volatility for k-minute
     forward moves, from the whole dataset.
  2. For every historical round, at each 1-minute checkpoint before it
     ends (1,2,3,4 minutes in), compute:
       - gap = price_now - price_at_round_start
       - z = gap / (sigma(remaining_minutes) * price_at_round_start)
       - naive_p_up = Phi(z)   (fair probability under a zero-drift
         random-walk assumption, i.e. what a naive market maker would
         price)
       - recent 1-minute momentum direction going INTO the checkpoint
  3. Bucket by z and by whether recent momentum agrees with the gap
     direction ("still trending") or opposes it ("already reverting").
  4. Compare actual Up-rate per bucket against naive_p_up. If the
     "already reverting" bucket's actual Up-rate is higher than what
     naive_p_up predicts for a Down-gap situation (or lower for an
     Up-gap), that is the exploitable mispricing -- IF market makers
     really do ignore momentum the way we assumed.
"""
import math
import statistics

import data_fetch
from backtest import build_rounds, ROUND_SECONDS


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def estimate_sigma_schedule(candles, max_k=5):
    """Stdev of k-minute forward relative returns, for k=1..max_k, using
    overlapping windows across the whole dataset (data-driven, not a
    theoretical formula)."""
    opens = [c["open"] for c in candles]
    n = len(opens)
    sigmas = {}
    for k in range(1, max_k + 1):
        rets = []
        for i in range(0, n - k, 1):
            if opens[i] == 0:
                continue
            rets.append((opens[i + k] - opens[i]) / opens[i])
        sigmas[k] = statistics.pstdev(rets) if len(rets) > 1 else None
    return sigmas


def analyze(candles, fee_pct=0.0):
    by_time = {c["open_time_ms"]: c["open"] for c in candles}
    rounds = build_rounds(candles)
    sigmas = estimate_sigma_schedule(candles, max_k=5)

    print("Volatilidad historica (desvio de retorno) por minutos restantes:")
    for k, s in sigmas.items():
        print(f"  {k} min: sigma = {s*100:.4f}%")
    print()

    # buckets keyed by (z_bin_label, momentum_category) -> [correct_up_count, total]
    buckets = {}

    round_ms = ROUND_SECONDS * 1000
    for r in rounds:
        p0 = r.start_price
        if p0 == 0:
            continue
        for e in (1, 2, 3, 4):
            t_e = r.start_ms + e * 60_000
            t_prev = r.start_ms + (e - 1) * 60_000
            p_e = by_time.get(t_e)
            p_prev = by_time.get(t_prev)
            if p_e is None or p_prev is None:
                continue
            remaining = 5 - e
            sigma_rem = sigmas.get(remaining)
            if not sigma_rem:
                continue

            gap = p_e - p0
            z = gap / (sigma_rem * p0)
            z_bin = max(-3.0, min(3.0, round(z * 2) / 2))  # bucket width 0.5, clipped

            recent_move = p_e - p_prev
            gap_sign = 1 if gap > 0 else (-1 if gap < 0 else 0)
            mom_sign = 1 if recent_move > 0 else (-1 if recent_move < 0 else 0)
            if gap_sign == 0 or mom_sign == 0:
                continue
            category = "trending" if mom_sign == gap_sign else "reverting"

            key = (z_bin, category)
            if key not in buckets:
                buckets[key] = [0, 0]
            buckets[key][1] += 1
            if r.outcome == "Up":
                buckets[key][0] += 1

    print("z = (precio_actual - precio_inicio) / (sigma * precio_inicio)")
    print("'trending' = el ultimo minuto siguio en la direccion del gap; 'reverting' = el ultimo minuto fue contra el gap")
    print()
    header = f"{'z_bin':>6s} {'categoria':>10s} {'n':>7s} {'Up real %':>10s} {'Up naive %':>11s} {'diff (pp)':>10s}"
    print(header)
    print("-" * len(header))

    rows = []
    for (z_bin, category), (up_count, total) in sorted(buckets.items(), key=lambda x: (x[0][0], x[0][1])):
        if total < 30:
            continue
        # P(future move > -gap) = 1 - Phi(-gap/sigma) = Phi(gap/sigma) = Phi(z)
        naive_p_up = normal_cdf(z_bin) * 100
        actual_p_up = up_count / total * 100
        diff = actual_p_up - naive_p_up
        rows.append((z_bin, category, total, actual_p_up, naive_p_up, diff))
        print(f"{z_bin:6.1f} {category:>10s} {total:7d} {actual_p_up:9.1f}% {naive_p_up:10.1f}% {diff:+9.1f}")

    print()
    print("Comparacion clave: para el mismo z_bin, 'reverting' vs 'trending'.")
    print("Si 'reverting' tiene consistentemente Up% real mayor que 'trending' cuando el gap es negativo")
    print("(o menor cuando el gap es positivo), hay una brecha explotable frente a un modelo naive de random walk.")
    print()

    by_zbin = {}
    for z_bin, category, total, actual, naive, diff in rows:
        by_zbin.setdefault(z_bin, {})[category] = (total, actual, naive)

    header2 = f"{'z_bin':>6s} {'naive Up%':>10s} {'trending n/Up%':>16s} {'reverting n/Up%':>17s} {'brecha (pp)':>12s}"
    print(header2)
    print("-" * len(header2))
    significant_gaps = []
    for z_bin in sorted(by_zbin):
        d = by_zbin[z_bin]
        if "trending" not in d or "reverting" not in d:
            continue
        t_n, t_up, naive = d["trending"]
        r_n, r_up, _ = d["reverting"]
        gap_pp = r_up - t_up
        print(f"{z_bin:6.1f} {naive:9.1f}% {t_n:6d}/{t_up:5.1f}%    {r_n:6d}/{r_up:5.1f}%     {gap_pp:+10.1f}")
        if min(t_n, r_n) >= 100 and abs(gap_pp) >= 3:
            significant_gaps.append((z_bin, gap_pp, t_n, r_n))

    print()
    if significant_gaps:
        print("Brechas de al menos 3 puntos porcentuales entre 'trending' y 'reverting' (candidatas a mirar mas de cerca):")
        for z_bin, gap_pp, t_n, r_n in significant_gaps:
            print(f"  z={z_bin:+.1f}: brecha {gap_pp:+.1f}pp (trending n={t_n}, reverting n={r_n})")
    else:
        print("No hay brechas >=3pp con muestra decente (n>=100 en ambos grupos) entre 'trending' y 'reverting'.")
        print("Es decir: el momentum de ultimo minuto, DADO el nivel de gap ya alcanzado, no anade señal extra")
        print("consistente y de tamano relevante sobre lo que ya predice el gap solo. La reversion que existe")
        print("(vista en backtest.py) ya esta 'dentro' del gap y el z-score la captura razonablemente.")


if __name__ == "__main__":
    candles = data_fetch.load_cached()
    analyze(candles)
