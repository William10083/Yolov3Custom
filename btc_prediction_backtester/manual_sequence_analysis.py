"""Analyze the 36-round Up/Down sequence recorded manually from the app.

IMPORTANT CAVEAT: 36 observations is a tiny sample. Any pattern found here
is NOT statistically reliable on its own -- it's included for completeness
and to compare against the much larger real-price backtest in backtest.py.
Treat this as "does the small sample at least agree with the big backtest",
not as a source of truth by itself.
"""

# The sequence you recorded manually, in order.
MANUAL_SEQUENCE = [
    "Down", "Down", "Down", "Up", "Down", "Up", "Down", "Down", "Down", "Down",
    "Up", "Down", "Down", "Down", "Down", "Down", "Down", "Up", "Up", "Down",
    "Down", "Down", "Up", "Up", "Down", "Down", "Down", "Down", "Down", "Up",
    "Down", "Up", "Down", "Down", "Up", "Down",
]


def wilson_ci(successes, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5)) / denom
    return (p, max(0.0, center - margin), min(1.0, center + margin))


def analyze(sequence=MANUAL_SEQUENCE):
    n = len(sequence)
    ups = sequence.count("Up")
    downs = sequence.count("Down")
    print(f"Total de rondas registradas: {n}")
    print(f"Up: {ups} ({ups/n*100:.1f}%)   Down: {downs} ({downs/n*100:.1f}%)")
    print()

    # Transition analysis: given the current value, what came next?
    transitions = {"Up": {"Up": 0, "Down": 0}, "Down": {"Up": 0, "Down": 0}}
    for i in range(n - 1):
        cur, nxt = sequence[i], sequence[i + 1]
        transitions[cur][nxt] += 1

    print("Probabilidad de transicion (dado el resultado actual, que sigue):")
    for cur in ("Up", "Down"):
        total = transitions[cur]["Up"] + transitions[cur]["Down"]
        if total == 0:
            continue
        p_up = transitions[cur]["Up"] / total
        p_down = transitions[cur]["Down"] / total
        print(f"  Despues de {cur:5s} (n={total:2d}): -> Up {p_up*100:5.1f}%   -> Down {p_down*100:5.1f}%")
    print()

    # Streak-reversion check: after a streak of k same-direction rounds,
    # how often did the next round reverse?
    print("Reversion tras rachas (tu lista tiene mucho sesgo a Down, ojo con el tamano de muestra):")
    for k in (2, 3, 4):
        reversals = 0
        continues = 0
        for i in range(k, n):
            streak = sequence[i - k:i]
            if len(set(streak)) == 1:
                direction = streak[0]
                if sequence[i] != direction:
                    reversals += 1
                else:
                    continues += 1
        total = reversals + continues
        if total == 0:
            print(f"  Racha de {k}: no hubo suficientes casos en esta muestra")
            continue
        p, lo, hi = wilson_ci(reversals, total)
        print(
            f"  Racha de {k} iguales (n={total:2d} casos): reversion {reversals}/{total} "
            f"= {p*100:.1f}%  (IC95% [{lo*100:.1f}%, {hi*100:.1f}%])"
        )
    print()
    print(
        "Con n=36 ninguno de estos intervalos de confianza es suficientemente "
        "estrecho para apostar sobre ellos. Sirve solo como sanity-check contra "
        "el backtest de precios reales (backtest.py), que usa miles de rondas."
    )


if __name__ == "__main__":
    analyze()
