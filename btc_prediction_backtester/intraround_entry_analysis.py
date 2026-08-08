"""Is there a better moment INSIDE a round to enter than at its start?

VERDICT (see run_verdict() at the bottom): no. 28 deviations from fair value
replicate across both halves of the data, which looks compelling until you
check why. They are an artifact of pricing fat-tailed returns with a Gaussian
model, not a market inefficiency:

  - Measured kurtosis of the remaining move is 20.8 (1 min left) to 26.8
    (4 min left). A normal distribution has 3.0.
  - A sharper-peaked, fatter-tailed reality means a Gaussian underestimates
    how often price simply sits still (so moderate levels look like
    "continuation") and underestimates big jumps (so extremes look like
    "reversion").
  - That is exactly the measured shape: at minute 1, fair 0.40 -> 36.0% and
    fair 0.60 -> 64.2% (continuation), while fair 0.10 -> 17.1% and fair
    0.90 -> 81.2% (reversion). Antisymmetric errors around 0.5.

Market makers price with proper fat-tailed models. Betting these "deviations"
would be betting that they share my distributional mistake, which they do not.
The one place the Gaussian is unbiased is right at 0.50 (error -0.1pp over
16,740 rounds) -- which is where the live alerter already restricts itself.


The live alerter only looks at the first 45 seconds, because that is where
its signals were measured. That leaves an obvious question unanswered: if
you watched the whole curve instead, would a later entry be better?

This measures it directly. At each 1-minute checkpoint inside every
historical round it computes:
  - fair value for "Up" from the move so far, time remaining and historical
    volatility (the same model naive_fair_prob uses live)
  - what actually happened
and then compares the two per bucket.

If actual outcomes systematically beat fair value in some bucket, that is an
exploitable deviation -- assuming the market prices near fair value, which is
the part we cannot verify without historical odds and which the live logger is
now collecting.

In-sample / out-of-sample split throughout: a deviation that shows up in the
first half and vanishes in the second is noise, and that distinction is the
whole point of running this rather than eyeballing a chart.
"""
import math
from collections import defaultdict

import data_fetch

ROUND_MS = 5 * 60 * 1000

# Historical stdev of BTC moves by remaining minutes (option_edge_analysis.py).
SIGMA_BY_REMAINING_MIN = {1: 0.0650, 2: 0.0920, 3: 0.1125, 4: 0.1295, 5: 0.1445}


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def wilson_ci(successes, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5)) / denom
    return (p, max(0.0, centre - margin), min(1.0, centre + margin))


def build_checkpoints(candles):
    """One record per (round, minute-inside-round) with fair value and outcome."""
    by_open = {c["open_time_ms"]: c["open"] for c in candles}
    starts = sorted(t for t in by_open if t % ROUND_MS == 0)

    records = []
    for start in starts:
        open_price = by_open.get(start)
        end_price = by_open.get(start + ROUND_MS)
        if open_price is None or end_price is None:
            continue
        outcome_up = end_price > open_price

        for minute in (1, 2, 3, 4):
            now_price = by_open.get(start + minute * 60_000)
            if now_price is None:
                continue
            remaining = 5 - minute
            sigma_abs = open_price * SIGMA_BY_REMAINING_MIN[remaining] / 100
            if sigma_abs <= 0:
                continue
            fair_up = normal_cdf((now_price - open_price) / sigma_abs)
            records.append(
                {
                    "start": start,
                    "minute": minute,
                    "fair_up": fair_up,
                    "outcome_up": outcome_up,
                }
            )
    return records


def report(records, label, fee=0.02):
    """Per checkpoint-minute and fair-value bucket: actual vs predicted."""
    buckets = defaultdict(lambda: [0, 0])
    for r in records:
        key = (r["minute"], round(r["fair_up"] * 10) / 10)
        buckets[key][1] += 1
        if r["outcome_up"]:
            buckets[key][0] += 1

    print(f"\n=== {label} ===")
    print(f"{'min':>4} {'justo':>6} {'n':>7} {'Up real':>9} {'error':>8} {'EV si el mercado cotiza al justo':>34}")
    print("-" * 74)
    for (minute, bucket), (ups, n) in sorted(buckets.items()):
        if n < 300:
            continue
        real = ups / n
        error = real - bucket
        # Betting the side the move favours, paying the naive fair price for it.
        side_price = bucket if bucket >= 0.5 else 1 - bucket
        side_real = real if bucket >= 0.5 else 1 - real
        ev = (side_real * (1 - fee) / side_price - 1) if side_price > 0 else float("nan")
        print(f"{minute:>4} {bucket:>6.2f} {n:>7} {real*100:>8.1f}% {error*100:>+7.1f}pp {ev*100:>+33.1f}%")


def significance(records, label):
    """Which (minute, bucket) deviations survive a 95% CI -- i.e. are unlikely
    to be noise -- reported separately for in- and out-of-sample halves."""
    buckets = defaultdict(lambda: [0, 0])
    for r in records:
        key = (r["minute"], round(r["fair_up"] * 10) / 10)
        buckets[key][1] += 1
        if r["outcome_up"]:
            buckets[key][0] += 1

    hits = []
    for (minute, bucket), (ups, n) in sorted(buckets.items()):
        if n < 300:
            continue
        _, lo, hi = wilson_ci(ups, n)
        if lo > bucket or hi < bucket:  # CI excludes the model's prediction
            direction = "CONTINUA" if (ups / n - bucket) * (bucket - 0.5) > 0 else "REVIERTE"
            hits.append((minute, bucket, n, ups / n, lo, hi, direction))

    print(f"\n--- {label}: desviaciones significativas (IC95% excluye el valor justo) ---")
    if not hits:
        print("  ninguna")
    for minute, bucket, n, real, lo, hi, direction in hits:
        print(
            f"  min {minute}, justo {bucket:.2f}: real {real*100:.1f}% "
            f"IC95% [{lo*100:.1f}%, {hi*100:.1f}%] n={n} -> {direction}"
        )
    return {(m, b) for m, b, *_ in hits}


def run_verdict(candles):
    """Test the boring explanation before believing in an edge.

    A Gaussian fair-value model applied to fat-tailed returns produces
    apparent deviations with a specific fingerprint: continuation at moderate
    levels, reversion at extremes, antisymmetric around 0.5. If the measured
    kurtosis is far above 3, that is the explanation and there is nothing to
    trade -- market makers use models that already account for it.
    """
    by_open = {c["open_time_ms"]: c["open"] for c in candles}
    starts = sorted(t for t in by_open if t % ROUND_MS == 0)

    print("\n=== Chequeo: normal vs realidad ===")
    print("Si la curtosis es >> 3, las desviaciones de arriba son un artefacto")
    print("del modelo normal, no una ineficiencia explotable.\n")

    import statistics

    for minute in (1, 2, 3, 4):
        rets = []
        for start in starts:
            now = by_open.get(start + minute * 60_000)
            end = by_open.get(start + ROUND_MS)
            if now is None or end is None or now == 0:
                continue
            rets.append((end - now) / now)
        if len(rets) < 100:
            continue
        mean = statistics.fmean(rets)
        sd = statistics.pstdev(rets)
        kurt = statistics.fmean([((x - mean) / sd) ** 4 for x in rets])
        print(f"  minuto {minute} ({5-minute} restantes): curtosis {kurt:5.1f}   (normal = 3.0)")

    print(
        "\nCurtosis muy por encima de 3 = pico agudo y colas gordas. Con esa forma,\n"
        "un modelo normal subestima cuanto se queda quieto el precio (parece\n"
        "CONTINUACION en niveles medios) y subestima los saltos grandes (parece\n"
        "REVERSION en los extremos) -- exactamente el patron medido arriba.\n"
        "\nConclusion: no hay mejor momento de entrada adentro de la ronda. El unico\n"
        "punto donde el modelo normal es insesgado es 0.50 (error -0.1pp sobre\n"
        "16,740 rondas), que es justo donde el alertador ya se restringe."
    )


if __name__ == "__main__":
    candles = data_fetch.load_cached()
    records = build_checkpoints(candles)
    print(f"Checkpoints construidos: {len(records)} (de {len(records)//4} rondas aprox)")

    half = len(records) // 2
    in_sample, out_sample = records[:half], records[half:]

    report(in_sample, "IN-SAMPLE (primera mitad)")
    report(out_sample, "OUT-OF-SAMPLE (segunda mitad)")

    hits_in = significance(in_sample, "IN-SAMPLE")
    hits_out = significance(out_sample, "OUT-OF-SAMPLE")

    both = hits_in & hits_out
    print("\n=== Desviaciones que replican ===")
    if both:
        print(f"Aparecen en AMBAS mitades: {len(both)}")
        for minute, bucket in sorted(both):
            print(f"  minuto {minute}, valor justo {bucket:.2f}")
        print("\nReplicar descarta el azar, pero NO descarta que el modelo este mal.")
        print("Eso es lo que chequea la seccion siguiente.")
    else:
        print("Ninguna desviacion se repite en ambas mitades -- nada explotable aca.")

    run_verdict(candles)
