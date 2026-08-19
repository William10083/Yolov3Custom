"""¿Conviene reentrenar el modelo, o el congelado alcanza?

El modelo que se envia esta entrenado con el 60% mas viejo de los datos, que
termina en Dic-2024. Suena viejo estando en 2026, y la pregunta es legitima:
si se reentrena con TODO lo disponible hasta el dia anterior, ¿acierta mas?

Eso no se contesta opinando. Se contesta con walk-forward: para cada mes del
periodo evaluado se entrena un modelo con todo lo ANTERIOR a ese mes y se lo
mide sobre ese mes. Nunca ve el futuro, y cada mes se evalua con el modelo
que realmente se habria tenido ese dia. Es exactamente la politica que se
usaria en produccion, medida como se usaria.

Se comparan tres politicas sobre los MISMOS meses:

  A. congelado    -- entrenado una vez con el 60% mas viejo, nunca se toca
  B. reentrenado  -- cada mes, con toda la historia previa
  C. ventana movil-- cada mes, solo con los ultimos N meses

C existe porque "mas datos" y "datos mas parecidos a hoy" tiran para lados
opuestos. Si el mercado cambia de regimen, la historia vieja estorba; si no
cambia, tirarla es perder muestra. Cual gana es una medicion, no una opinion.

Nada se ajusta aca: l2 y el umbral vienen fijos de predict_model.py.

    python3 walk_forward.py --data data/btcusdt_1m_long.csv
"""
import argparse
import statistics
from collections import defaultdict
from datetime import datetime, timezone

import predict_model as pm

L2 = 1000.0
MARGIN = 0.05
FEE = 0.02
ITERS = 5  # converge en 4; 5 da margen y no cuesta nada


def month_key(ms):
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return d.year * 12 + (d.month - 1)


def month_name(mk):
    return f"{mk // 12}-{mk % 12 + 1:02d}"


def evaluate(w, b, data, margin=MARGIN):
    """(aciertos, total) restringido a las llamadas de confianza."""
    hits = tot = 0
    for d in data:
        p = pm.predict(w, b, d["v"])
        if abs(p - 0.5) < margin:
            continue
        tot += 1
        if (p >= 0.5) == (d["y"] == 1):
            hits += 1
    return hits, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--months", type=int, default=18, help="meses a evaluar (los mas recientes)")
    ap.add_argument("--window", type=int, default=12, help="meses de ventana movil para la politica C")
    args = ap.parse_args()

    print("Cargando...")
    rows = pm.build_dataset(pm.load_csv(args.data))
    rows.sort(key=lambda r: r["t"])
    keys = sorted(rows[0]["x"].keys())
    print(f"Rondas: {len(rows):,}")

    by_month = defaultdict(list)
    for r in rows:
        by_month[month_key(r["t"])].append(r)
    meses = sorted(by_month)
    evaluar = meses[-args.months:]
    print(f"Meses evaluados: {month_name(evaluar[0])} a {month_name(evaluar[-1])}"
          f"  ({len(evaluar)} folds)\n")

    # Politica A: el modelo congelado, entrenado una sola vez con el 60% mas viejo.
    cut = int(len(rows) * 0.6)
    trA_raw = rows[:cut]
    trA, statsA = pm.standardize(trA_raw, keys)
    wA, bA = pm.train_logistic_newton(trA, keys, l2=L2, iters=ITERS)
    corteA = datetime.fromtimestamp(trA_raw[-1]["t"] / 1000, tz=timezone.utc)
    print(f"A. congelado: entrenado con {len(trA_raw):,} rondas hasta {corteA:%Y-%m-%d}\n")

    tot = {"A": [0, 0], "B": [0, 0], "C": [0, 0]}
    print(f"  {'mes':<10}{'rondas':>8}{'A congelado':>16}{'B reentrenado':>17}{'C ventana':>14}")
    print("  " + "-" * 65)

    for mk in evaluar:
        te_raw = by_month[mk]
        prev = [r for r in rows if month_key(r["t"]) < mk]
        if len(prev) < 20000:
            continue

        fila = f"  {month_name(mk):<10}{len(te_raw):>8}"

        # A: standardize con las stats del entrenamiento congelado.
        teA, _ = pm.standardize(te_raw, keys, statsA)
        hA, nA = evaluate(wA, bA, teA)
        tot["A"][0] += hA
        tot["A"][1] += nA
        fila += f"{(hA/nA*100 if nA else 0):>13.2f}% " if nA else f"{'--':>14}"

        # B: reentrenar con todo lo anterior a este mes.
        trB, statsB = pm.standardize(prev, keys)
        wB, bB = pm.train_logistic_newton(trB, keys, l2=L2, iters=ITERS)
        teB, _ = pm.standardize(te_raw, keys, statsB)
        hB, nB = evaluate(wB, bB, teB)
        tot["B"][0] += hB
        tot["B"][1] += nB
        fila += f"{(hB/nB*100 if nB else 0):>15.2f}% " if nB else f"{'--':>16}"

        # C: ventana movil de los ultimos `window` meses.
        recientes = [r for r in prev if month_key(r["t"]) >= mk - args.window]
        if len(recientes) >= 20000:
            trC, statsC = pm.standardize(recientes, keys)
            wC, bC = pm.train_logistic_newton(trC, keys, l2=L2, iters=ITERS)
            teC, _ = pm.standardize(te_raw, keys, statsC)
            hC, nC = evaluate(wC, bC, teC)
            tot["C"][0] += hC
            tot["C"][1] += nC
            fila += f"{(hC/nC*100 if nC else 0):>12.2f}%" if nC else f"{'--':>13}"
        print(fila, flush=True)

    print("\n" + "=" * 72)
    print("ACUMULADO SOBRE TODOS LOS MESES")
    print("=" * 72)
    etiquetas = {
        "A": "congelado (60% mas viejo)",
        "B": f"reentrenado cada mes con todo",
        "C": f"ventana movil de {args.window} meses",
    }
    mejor = None
    for k in ("A", "B", "C"):
        h, n = tot[k]
        if not n:
            continue
        p, lo, hi = pm.wilson(h, n)
        print(f"  {etiquetas[k]:<34} {h:>6}/{n:<6} {p*100:>6.2f}%  "
              f"IC95% [{lo*100:.2f}%, {hi*100:.2f}%]")
        if mejor is None or p > mejor[1]:
            mejor = (k, p, lo, n)

    print("\n" + "=" * 72)
    print("LECTURA")
    print("=" * 72)
    hA, nA = tot["A"]
    hB, nB = tot["B"]
    if nA and nB:
        pA, pB = hA / nA, hB / nB
        dif = (pB - pA) * 100
        # ¿La diferencia entre reentrenar y no reentrenar es real o es ruido?
        se = ((pA * (1 - pA) / nA) + (pB * (1 - pB) / nB)) ** 0.5
        z = (pB - pA) / se if se else 0
        print(f"  Reentrenar vs congelado: {dif:+.2f} pp   (z = {z:+.2f})")
        if abs(z) < 1.96:
            print("  No hay diferencia estadisticamente distinguible. El modelo")
            print("  congelado NO esta perdiendo vigencia: la fecha de corte vieja")
            print("  no le esta costando acierto.")
        elif z > 0:
            print("  Reentrenar gana de forma significativa. Conviene rehacer")
            print("  model.json periodicamente con los datos nuevos.")
        else:
            print("  Reentrenar EMPEORA de forma significativa -- señal de que la")
            print("  historia reciente es mas ruidosa, no mas informativa.")
    print(f"\n  Todas las cifras son sobre llamadas de confianza (>= {0.5+MARGIN:.2f}),")
    print(f"  con l2={L2} y umbral fijos de antemano. Nada se ajusto aca.")


if __name__ == "__main__":
    main()
