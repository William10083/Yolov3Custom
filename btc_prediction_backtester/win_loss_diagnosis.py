"""What separates the bets that won from the bets that lost?

Rather than inventing new variants and hoping one sticks, this compares the
predictions that hit against the ones that missed, feature by feature, on the
rounds already logged. If something consistently distinguishes them, that is
a correction worth making. If nothing does, the misses were noise and no
amount of rule-tweaking will help.

Run where the CSVs live:

    python3 win_loss_diagnosis.py

THE TRAP THIS SCRIPT TRIES NOT TO FALL INTO: with ~60 bets split into
winners and losers, comparing enough features will always turn up a
"difference". Every comparison here is reported with its sample size and a
significance test, the number of tests run is stated, and the expected count
of false positives is printed next to the findings. A gap that only looks
real because several were examined is not a correction.
"""
import csv
import math
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone

DATA = os.path.join(os.path.dirname(__file__), "data")
OUTCOMES = os.path.join(DATA, "round_outcomes.csv")

OUTCOMES_SCHEMA = [
    "market_id", "round_start_ms", "round_end_ms", "start_price", "end_price",
    "outcome", "market_prob_up", "predicted_side", "predicted_reasoning",
    "signal_correct", "signal_name", "entry_price",
]


def load_positional(path, schema):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    body = rows[1:] if rows[0] and rows[0][0] == schema[0] else rows
    out = []
    for r in body:
        if not r:
            continue
        d = dict.fromkeys(schema, "")
        for i, v in enumerate(r):
            if i < len(schema):
                d[schema[i]] = v
        out.append(d)
    return out


def fnum(s):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def two_proportion_z(k1, n1, k2, n2):
    """Standard test for 'do these two groups really differ?'"""
    if n1 == 0 or n2 == 0:
        return None, None
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return None, None
    z = (p1 - p2) / se
    pval = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return z, pval


def parse(outcomes):
    """One record per graded bet, with everything recoverable about it."""
    bets = []
    for r in outcomes:
        correct = (r.get("signal_correct") or "").strip()
        if correct not in ("True", "False"):
            continue

        reason = (r.get("predicted_reasoning") or "").strip()
        price = fnum(r.get("entry_price"))
        if price is None and reason:
            m = re.search(r"\ba (0\.\d+)", reason)
            price = float(m.group(1)) if m else None

        name = (r.get("signal_name") or "").strip()
        if not name and reason:
            name = reason.split(" (")[0].split(";")[0].strip()

        # The signal's own magnitude, whichever kind it was.
        magnitude = None
        m = re.search(r"(\d+)% del volumen", reason)
        if m:
            magnitude = abs(int(m.group(1)) - 50) / 50  # 0 = balanced, 1 = one-sided
        else:
            m = re.search(r"movio ([+-]?\d+\.\d+)%", reason)
            if m:
                magnitude = abs(float(m.group(1)))

        ev = None
        m = re.search(r"EV ([+-]?\d+\.\d+)%", reason)
        if m:
            ev = float(m.group(1))

        start = fnum(r.get("start_price"))
        end = fnum(r.get("end_price"))
        move = abs(end - start) if (start is not None and end is not None) else None

        start_ms = fnum(r.get("round_start_ms"))
        hour = None
        if start_ms:
            hour = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).hour

        bets.append({
            "won": correct == "True",
            "price": price,
            "signal": name or "(sin nombre)",
            "side": (r.get("predicted_side") or "").strip(),
            "magnitude": magnitude,
            "ev": ev,
            "move": move,
            "hour": hour,
            "outcome": (r.get("outcome") or "").strip(),
        })
    return bets


def compare_numeric(bets, key, label, tests):
    """Mean of a feature among winners vs losers."""
    wins = [b[key] for b in bets if b["won"] and b.get(key) is not None]
    losses = [b[key] for b in bets if not b["won"] and b.get(key) is not None]
    if len(wins) < 5 or len(losses) < 5:
        print(f"  {label:<34} muestra insuficiente ({len(wins)} vs {len(losses)})")
        return
    mw, ml = statistics.fmean(wins), statistics.fmean(losses)
    sw = statistics.pstdev(wins) if len(wins) > 1 else 0
    sl = statistics.pstdev(losses) if len(losses) > 1 else 0
    se = math.sqrt(sw**2 / len(wins) + sl**2 / len(losses))
    z = (mw - ml) / se if se > 0 else 0
    pval = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    tests.append(pval)
    flag = "  <-- DIFERENCIA" if pval < 0.05 else ""
    print(f"  {label:<34} acierto {mw:>8.3f} | fallo {ml:>8.3f} | p={pval:.3f}{flag}")


def compare_group(bets, key, label, tests):
    """Win rate broken out by a categorical feature."""
    groups = defaultdict(lambda: [0, 0])
    for b in bets:
        v = b.get(key)
        if v in (None, ""):
            continue
        groups[v][1] += 1
        if b["won"]:
            groups[v][0] += 1

    usable = {k: v for k, v in groups.items() if v[1] >= 5}
    if len(usable) < 2:
        print(f"  {label}: menos de 2 grupos con muestra suficiente")
        return

    print(f"\n  {label}:")
    for k, (w, n) in sorted(usable.items(), key=lambda kv: -kv[1][1]):
        print(f"    {str(k)[:38]:<40} {w:>3}/{n:<3} = {w/n*100:>5.1f}%")

    ordered = sorted(usable.items(), key=lambda kv: -kv[1][1])[:2]
    (k1, (w1, n1)), (k2, (w2, n2)) = ordered
    z, pval = two_proportion_z(w1, n1, w2, n2)
    if pval is not None:
        tests.append(pval)
        flag = "  <-- DIFERENCIA" if pval < 0.05 else ""
        print(f"    {str(k1)[:16]} vs {str(k2)[:16]}: p={pval:.3f}{flag}")


def main():
    outcomes = load_positional(OUTCOMES, OUTCOMES_SCHEMA)
    bets = parse(outcomes)
    wins = [b for b in bets if b["won"]]
    losses = [b for b in bets if not b["won"]]

    print(f"Apuestas evaluadas: {len(bets)}  ({len(wins)} aciertos, {len(losses)} fallos)")
    if len(bets) < 20:
        print("Muy pocas para diagnosticar. Segui recolectando.")
        return

    tests = []

    print("\n" + "=" * 76)
    print("Comparacion numerica: aciertos vs fallos")
    print("=" * 76)
    compare_numeric(bets, "price", "precio pagado", tests)
    compare_numeric(bets, "magnitude", "fuerza de la señal", tests)
    compare_numeric(bets, "ev", "EV que declaramos (%)", tests)
    compare_numeric(bets, "move", "cuanto se movio BTC ($)", tests)

    print("\n" + "=" * 76)
    print("Comparacion por grupo")
    print("=" * 76)
    compare_group(bets, "signal", "por señal", tests)
    compare_group(bets, "side", "por lado apostado", tests)
    compare_group(bets, "outcome", "por resultado real de la ronda", tests)

    # The single most important control: were we simply on the wrong side of a
    # trending period? If nearly every bet was Down while Up kept happening,
    # a chunk of the loss is that alone and not the signals being broken.
    print("\n" + "=" * 76)
    print("Control: ¿fue solo estar del lado equivocado de la tendencia?")
    print("=" * 76)
    sides = defaultdict(int)
    for b in bets:
        sides[b["side"]] += 1
    resolved = [b["outcome"] for b in bets if b["outcome"] in ("Up", "Down")]
    ups = sum(1 for o in resolved if o == "Up")
    print(f"  Apostamos: {dict(sides)}")
    if resolved:
        print(f"  Salio: {ups} Up / {len(resolved)-ups} Down ({ups/len(resolved)*100:.1f}% Up)")
        dominant = max(sides, key=sides.get) if sides else None
        if dominant:
            share = sides[dominant] / len(bets)
            base = (ups / len(resolved)) if dominant == "Up" else (1 - ups / len(resolved))
            print(f"  Apostamos {dominant} en el {share*100:.0f}% de los casos.")
            print(f"  Acertar {dominant} al azar en este periodo daria {base*100:.1f}%.")
            real = sum(1 for b in bets if b["won"]) / len(bets)
            print(f"  Nuestro acierto real: {real*100:.1f}%")
            if real < base - 0.05:
                print("  -> Rendimos POR DEBAJO incluso de apostar ese lado a ciegas.")
                print("     La tendencia no lo explica: las señales estan restando.")
            elif real > base + 0.05:
                print("  -> Rendimos por encima de apostar ese lado a ciegas.")
            else:
                print("  -> Basicamente igual que apostar ese lado a ciegas.")
                print("     Las señales no aportaron nada; el resultado es el sesgo del periodo.")

    print("\n" + "=" * 76)
    print("Lectura")
    print("=" * 76)
    significant = [p for p in tests if p < 0.05]
    print(f"  Comparaciones hechas: {len(tests)}")
    print(f"  Con p<0.05: {len(significant)}")
    print(f"  Falsos positivos esperados por azar: ~{len(tests)*0.05:.1f}")
    if len(significant) <= len(tests) * 0.05 + 0.5:
        print("\n  No hay nada que separe de forma confiable los aciertos de los fallos.")
        print("  Eso significa que no hay correccion que hacer: los fallos no comparten")
        print("  una caracteristica corregible, se ven como ruido alrededor de una")
        print("  ventaja que no existe.")
    else:
        print("\n  Hay mas diferencias significativas que las esperables por azar.")
        print("  Vale investigarlas -- pero fijandolas AHORA y midiendolas en rondas")
        print("  nuevas, no ajustando reglas sobre esta misma muestra.")


if __name__ == "__main__":
    main()
