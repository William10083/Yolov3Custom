"""Paper P&L: what would $10 have become, on the data actually collected.

No betting, no orders -- replays the rounds already logged and applies
different rule sets to each, so the question "can this be improved?" gets
answered with arithmetic instead of with money.

Payout mechanics (binary market): staking S at price p buys
S*(1-fee)/p shares, each worth $1 if the side wins and $0 if it loses.

    win:   +S*(1-fee)/p - S
    lose:  -S

Run on the device holding the CSVs:

    python3 paper_pnl.py

WHY MULTIPLE VARIANTS, AND THE CATCH: testing many rule sets on one small
sample will always throw up a winner by luck alone. With ~60 bets, a variant
needs to beat 50% by a wide margin before it means anything, and the report
prints how wide that margin has to be. A variant that only looks good after
searching is not a strategy, it is the search showing through.
"""
import csv
import json
import math
import os
import re
from collections import defaultdict

DATA = os.path.join(os.path.dirname(__file__), "data")
ODDS = os.path.join(DATA, "live_odds_log.csv")
OUTCOMES = os.path.join(DATA, "round_outcomes.csv")

FEE = 0.02
STAKE = 10.0
START_BANKROLL = 10.0

OUTCOMES_SCHEMA = [
    "market_id", "round_start_ms", "round_end_ms", "start_price", "end_price",
    "outcome", "market_prob_up", "predicted_side", "predicted_reasoning",
    "signal_correct", "signal_name", "entry_price",
]


def load_positional(path, schema):
    """Read by position against the known schema -- older files kept their
    original short header while the writer grew columns, so the header on
    disk cannot be trusted."""
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


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / d
    return (p, max(0.0, c - m), min(1.0, c + m))


def parse_bets(outcomes):
    """One record per graded bet: side, price paid, signal, win/lose."""
    bets = []
    for r in outcomes:
        correct = (r.get("signal_correct") or "").strip()
        if correct not in ("True", "False"):
            continue

        price = None
        raw_price = (r.get("entry_price") or "").strip()
        if raw_price:
            try:
                price = float(raw_price)
            except ValueError:
                price = None

        name = (r.get("signal_name") or "").strip()
        reason = (r.get("predicted_reasoning") or "").strip()
        if price is None and reason:
            m = re.search(r"\ba (0\.\d+)", reason)
            if m:
                price = float(m.group(1))
        if not name and reason:
            name = reason.split(" (")[0].split(";")[0].strip()

        if price is None or not 0 < price < 1:
            continue

        bets.append(
            {
                "won": correct == "True",
                "price": price,
                "signal": name or "(sin nombre)",
                "side": (r.get("predicted_side") or "").strip(),
                "start_ms": (r.get("round_start_ms") or "").strip(),
            }
        )
    bets.sort(key=lambda b: b["start_ms"])
    return bets


def simulate(bets, stake=STAKE, fee=FEE):
    """Cumulative P&L at a fixed stake, plus the $10-bankroll path where a
    loss can end the run outright."""
    pnl = 0.0
    bankroll = START_BANKROLL
    broke_at = None
    curve = []
    for i, b in enumerate(bets, 1):
        payout = stake * (1 - fee) / b["price"]
        pnl += (payout - stake) if b["won"] else -stake

        if bankroll > 0:
            bet = min(stake, bankroll)
            bankroll -= bet
            if b["won"]:
                bankroll += bet * (1 - fee) / b["price"]
            if bankroll <= 0.009 and broke_at is None:
                broke_at = i
        curve.append(pnl)
    return pnl, bankroll, broke_at, curve


def report_variant(name, bets, note=""):
    if not bets:
        print(f"  {name:<44} sin apuestas")
        return None
    wins = sum(1 for b in bets if b["won"])
    p, lo, hi = wilson(wins, len(bets))
    pnl, bankroll, broke_at, _ = simulate(bets)
    roi = pnl / (STAKE * len(bets)) * 100

    # Win rate needed to break even at the average price actually paid.
    avg_price = sum(b["price"] for b in bets) / len(bets)
    breakeven = avg_price / (1 - FEE)

    quiebra = f"quiebra en #{broke_at}" if broke_at else f"${bankroll:.2f}"
    print(
        f"  {name:<44} {wins:>3}/{len(bets):<3} {p*100:>5.1f}%  "
        f"P&L ${pnl:>+8.2f}  ROI {roi:>+6.1f}%  banca {quiebra}"
    )
    if note:
        print(f"      {note}")
    print(
        f"      precio medio {avg_price:.3f} -> hace falta acertar >{breakeven*100:.1f}%  "
        f"| IC95% real [{lo*100:.1f}%, {hi*100:.1f}%]"
    )
    return {"name": name, "n": len(bets), "wins": wins, "pnl": pnl, "lo": lo, "breakeven": breakeven}


def main():
    outcomes = load_positional(OUTCOMES, OUTCOMES_SCHEMA)
    bets = parse_bets(outcomes)

    print(f"Rondas en el archivo: {len(outcomes)}")
    print(f"Apuestas con precio recuperable: {len(bets)}")
    if len(bets) < 10:
        print("\nMuy pocas para simular. Segui recolectando.")
        return

    print(f"\nApuesta fija de ${STAKE:.0f}, fee {FEE*100:.0f}%, banca inicial ${START_BANKROLL:.0f}\n")
    print(f"  {'variante':<44} {'aciertos':<10} {'':<6} {'':<18} {'':<14}")
    print("  " + "-" * 104)

    results = []
    results.append(report_variant("TODAS las alertas (lo que paso)", bets))

    # Variant: avoid the cheap fills, which is where adverse selection showed up.
    for umbral in (0.46, 0.48, 0.50):
        sub = [b for b in bets if b["price"] >= umbral]
        results.append(report_variant(f"solo si el precio >= {umbral:.2f}", sub))

    # Variant: per signal, since the aggregate hides which one is failing.
    por_senal = defaultdict(list)
    for b in bets:
        por_senal[b["signal"]].append(b)
    for name, sub in sorted(por_senal.items(), key=lambda kv: -len(kv[1])):
        if len(sub) >= 5:
            results.append(report_variant(f"solo {name[:34]}", sub))

    # Variant: the mirror image. If a rule loses consistently, inverting it is
    # the first thing to check -- and usually the fee eats it anyway.
    inverted = [{**b, "won": not b["won"], "price": round(1 - b["price"], 4)} for b in bets]
    results.append(
        report_variant("INVERTIDA (apostar al lado contrario)", inverted,
                       note="apostando el lado opuesto, al precio complementario")
    )

    print("\n" + "=" * 106)
    print("Lectura")
    print("=" * 106)

    real = [r for r in results if r and not r["name"].startswith("INVERT")]
    ganadoras = [r for r in real if r["lo"] > r["breakeven"]]
    n_variantes = len(real)

    if ganadoras:
        print(f"\nVariantes cuyo IC95% inferior supera su breakeven ({len(ganadoras)}):")
        for r in ganadoras:
            print(f"  - {r['name']}  ({r['wins']}/{r['n']})")
        print(
            f"\nOJO: se probaron {n_variantes} variantes sobre la MISMA muestra chica.\n"
            f"Con {n_variantes} pruebas al 5%, se espera ~{n_variantes*0.05:.1f} falso(s) positivo(s)\n"
            "por puro azar. Una variante que solo aparece despues de buscar entre muchas\n"
            "no es una estrategia: es la busqueda asomandose. Para creerle habria que\n"
            "fijarla ahora y medirla en rondas NUEVAS."
        )
    else:
        print(
            f"\nNinguna de las {n_variantes} variantes supera su breakeven de forma\n"
            "estadisticamente solida (limite inferior del IC95% por encima del win rate\n"
            "que exige el precio pagado).\n\n"
            "El detalle que mas importa: el breakeven NO es 50%. Al precio medio pagado,\n"
            "con fee de 2%, hay que acertar bastante mas que la mitad solo para empatar.\n"
            "Filtrar rondas sube el acierto pero tambien sube el precio medio -- las\n"
            "apuestas 'seguras' cuestan mas caras, y el umbral sube con ellas."
        )

    total_pnl, bankroll, broke_at, _ = simulate(bets)
    print(f"\nCon las alertas tal cual salieron: ${START_BANKROLL:.0f} -> ", end="")
    print(f"quiebra en la apuesta #{broke_at}." if broke_at else f"${bankroll:.2f}.")
    print(f"A apuesta fija de ${STAKE:.0f} sin limite de banca, el P&L acumulado seria ${total_pnl:+.2f}.")


if __name__ == "__main__":
    main()
