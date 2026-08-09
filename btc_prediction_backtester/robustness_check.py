"""Does the high-confidence bucket survive the checks that killed everything else?

predict_model.py found that the model's most confident calls (>= 0.55) hit
55.2% on 11,936 unseen rounds. That is above the 2%-fee breakeven. It is also
exactly the shape of every false positive found so far in this repo, so it
gets the same interrogation the others got:

  1. TIME STABILITY. Split the test period into chunks. A real edge shows up
     in most of them. A fluke lives in one.
  2. SIDE BALANCE. If the confident calls are nearly all Down and the period
     closed Down more often than not, the "edge" is the period's drift and
     nothing else. This is the control that explained the live results.
  3. THE MARKET PRICE. The model only uses candles from before the round
     opens -- the same public information the market prices at that moment.
     If the market agrees with the model, the price rises with the model's
     confidence and the breakeven rises with it. 55.2% accuracy is worthless
     at a price of 0.55.
  4. REGULARISATION. The solver is deterministic -- Newton-IRLS, no seed and
     no learning rate -- so the only remaining free choice is l2, and the
     answer has to survive moving it. An earlier SGD version DID depend on
     the shuffle seed: one seed's confident bucket hit 53.9% and another's
     46.3%, which was the optimizer showing through, not the market.

Nothing here re-tunes anything: l2 and the 0.55 margin are fixed to what
predict_model.py already chose.

    python3 robustness_check.py
"""
import statistics
from collections import defaultdict
from datetime import datetime, timezone

import predict_model as pm

try:
    import data_fetch
except ImportError:  # solo hace falta sin --data
    data_fetch = None

L2 = 1000.0       # chosen on validation by predict_model.py -- not re-tuned here
MARGIN = 0.05     # the bucket under examination: confidence >= 0.55
FEE = 0.02


def breakeven_at(price):
    """Win rate needed to break even paying `price` per share at 2% fee."""
    return price / (1 - FEE)


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="CSV de velas (por defecto, el cache de 180 dias)")
    ap.add_argument("--epochs", type=int, default=40)
    args = ap.parse_args()

    if args.data:
        candles = pm.load_csv(args.data)
    else:
        candles = data_fetch.load_cached()
    rows = pm.build_dataset(candles)
    rows.sort(key=lambda r: r["t"])
    keys = sorted(rows[0]["x"].keys())

    n = len(rows)
    a, bnd = int(n * 0.6), int(n * 0.8)
    tr_raw, te_raw = rows[:a], rows[bnd:]
    tr, stats = pm.standardize(tr_raw, keys)
    te, _ = pm.standardize(te_raw, keys, stats)

    print(f"Train {len(tr):,} | test {len(te):,}   (l2={L2}, umbral fijo {0.5+MARGIN:.2f})")

    w, b = pm.train_logistic_newton(tr, keys, l2=L2)
    preds = [pm.predict(w, b, d["v"]) for d in te]

    conf = [
        (p, te[i]["y"], te_raw[i]["t"])
        for i, p in enumerate(preds)
        if abs(p - 0.5) >= MARGIN
    ]
    hits = sum(1 for p, y, _ in conf if (p >= 0.5) == (y == 1))
    pr, lo, hi = pm.wilson(hits, len(conf))
    print(f"\nBucket completo: {hits}/{len(conf)} = {pr*100:.2f}%  IC95% [{lo*100:.2f}%, {hi*100:.2f}%]")

    # ---------------------------------------------------------- 1. estabilidad
    print("\n" + "=" * 72)
    print("1. ESTABILIDAD EN EL TIEMPO  (un edge real aparece en casi todos los tramos)")
    print("=" * 72)
    chunks = 8
    size = len(te) // chunks
    tramos_ok = 0
    for c in range(chunks):
        lo_i, hi_i = c * size, (c + 1) * size if c < chunks - 1 else len(te)
        sub = [
            (p, te[i]["y"])
            for i, p in enumerate(preds)
            if lo_i <= i < hi_i and abs(p - 0.5) >= MARGIN
        ]
        if len(sub) < 20:
            print(f"  tramo {c+1}: solo {len(sub)} rondas, sin muestra")
            continue
        k = sum(1 for p, y in sub if (p >= 0.5) == (y == 1))
        p_, l_, h_ = pm.wilson(k, len(sub))
        d0 = datetime.fromtimestamp(te_raw[lo_i]["t"] / 1000, tz=timezone.utc)
        d1 = datetime.fromtimestamp(te_raw[hi_i - 1]["t"] / 1000, tz=timezone.utc)
        marca = "ok" if p_ > 0.5051 else "NO"
        if p_ > 0.5051:
            tramos_ok += 1
        print(
            f"  tramo {c+1} ({d0:%d-%b} a {d1:%d-%b}): {k:>4}/{len(sub):<4} "
            f"{p_*100:>6.2f}%  IC95% [{l_*100:.1f}%, {h_*100:.1f}%]  {marca}"
        )
    print(f"\n  Tramos por encima del breakeven: {tramos_ok}/{chunks}")

    # ------------------------------------------------------- 2. sesgo de lado
    print("\n" + "=" * 72)
    print("2. ¿ES SOLO LA DERIVA DEL PERIODO?  (el control que explico los datos en vivo)")
    print("=" * 72)
    up_calls = sum(1 for p, _, _ in conf if p >= 0.5)
    down_calls = len(conf) - up_calls
    real_up = sum(1 for _, y, _ in conf if y == 1)
    print(f"  El modelo dijo: {up_calls} Up / {down_calls} Down")
    print(f"  Salio:          {real_up} Up / {len(conf)-real_up} Down "
          f"({real_up/len(conf)*100:.1f}% Up)")

    lado_dominante = "Up" if up_calls >= down_calls else "Down"
    base = (real_up / len(conf)) if lado_dominante == "Up" else (1 - real_up / len(conf))
    share = max(up_calls, down_calls) / len(conf)
    print(f"\n  Dijo {lado_dominante} en el {share*100:.0f}% de estas rondas.")
    print(f"  Apostar {lado_dominante} a ciegas en estas mismas rondas: {base*100:.1f}%")
    print(f"  El modelo logra: {pr*100:.1f}%")
    if pr > base + 0.02:
        print("  -> Le gana a apostar el lado dominante a ciegas. El edge no es la deriva.")
    elif pr < base - 0.02:
        print("  -> Rinde POR DEBAJO de apostar ese lado a ciegas.")
    else:
        print("  -> Practicamente igual que apostar ese lado a ciegas.")
        print("     Todo el 'edge' es el sesgo direccional del periodo, no la señal.")

    # Same question, per side: does it work in both directions?
    print("\n  Desglose por lado (un edge real funciona en los dos):")
    for etiqueta, cond in (("Up", lambda p: p >= 0.5), ("Down", lambda p: p < 0.5)):
        sub = [(p, y) for p, y, _ in conf if cond(p)]
        if len(sub) < 20:
            print(f"    {etiqueta}: solo {len(sub)} rondas")
            continue
        k = sum(1 for p, y in sub if (p >= 0.5) == (y == 1))
        p_, l_, h_ = pm.wilson(k, len(sub))
        print(f"    {etiqueta:<5} {k:>4}/{len(sub):<4} {p_*100:>6.2f}%  "
              f"IC95% [{l_*100:.1f}%, {h_*100:.1f}%]")

    # --------------------------------------------------- 3. el precio de mercado
    print("\n" + "=" * 72)
    print("3. EL PRECIO QUE HABRIA QUE PAGAR")
    print("=" * 72)
    conf_media = statistics.fmean(max(p, 1 - p) for p, _, _ in conf)
    print(f"  Confianza media del modelo en el bucket: {conf_media*100:.1f}%")
    print(f"  Acierto real: {pr*100:.1f}%   (limite inferior IC95%: {lo*100:.1f}%)")
    print(f"\n  Precio maximo que se puede pagar y seguir empatando:")
    print(f"    usando el acierto puntual:        {pr*(1-FEE):.3f}")
    print(f"    usando el limite inferior IC95%:  {lo*(1-FEE):.3f}")
    print("\n  Todas las features salen de velas ANTERIORES al inicio de la ronda:")
    print("  el mercado ve exactamente lo mismo cuando cotiza. Si el mercado")
    print("  coincide con el modelo, el precio sube con la confianza y el")
    print("  breakeven sube con el. Para que esto rinda, el mercado tiene que")
    print(f"  seguir cotizando por DEBAJO de {lo*(1-FEE):.3f} en estas rondas.")
    print("  Eso NO se puede saber con datos historicos de velas -- hace falta")
    print("  el precio cotizado. Es la unica pregunta que queda abierta.")

    # ---------------------------------------------------- 4. sensibilidad a l2
    print("\n" + "=" * 72)
    print("4. SENSIBILIDAD A LA REGULARIZACION")
    print("=" * 72)
    print("  El solver es determinista: no hay semilla que cambie el resultado.")
    print("  Lo unico que queda por elegir es l2, asi que se mide cuanto mueve.")
    for l2 in (0.1, 1.0, 10.0, 100.0, 1000.0):
        ws, bs = pm.train_logistic_newton(tr, keys, l2=l2)
        sub = [(pm.predict(ws, bs, d["v"]), d["y"]) for d in te]
        sub = [(p, y) for p, y in sub if abs(p - 0.5) >= MARGIN]
        if len(sub) < 30:
            print(f"  l2={l2:<8} solo {len(sub)} rondas superan el umbral")
            continue
        k = sum(1 for p, y in sub if (p >= 0.5) == (y == 1))
        p_, l_, h_ = pm.wilson(k, len(sub))
        marca = "ok" if l_ > 0.5051 else "NO"
        print(f"  l2={l2:<8} {k:>6}/{len(sub):<6} {p_*100:>6.2f}%  "
              f"IC95% [{l_*100:.2f}%, {h_*100:.2f}%]  {marca}")


if __name__ == "__main__":
    main()
