"""¿Conviene ajustar el modelo con los ULTIMOS DIAS en vez de con 4 anios?

El planteo: los 4 anios sirven para entender como se comporta el mercado de
bitcoin en general, pero para decidir HOY hay que mirar como se comporto en
estos dias. Un mercado que viene lateral y uno que viene en tendencia no se
predicen con los mismos pesos, y promediar 4 anios los mezcla todos.

walk_forward.py ya comparo 4 anios contra una ventana de 12 meses y no hubo
diferencia -- pero 12 meses no es "los ultimos dias". Esto prueba ventanas
cortas de verdad: 3, 7, 14, 30 y 90 dias.

Como se mide: para cada dia del periodo evaluado se ajusta el modelo SOLO con
los N dias anteriores y se predicen las rondas de ese dia. Nunca ve el futuro,
y cada dia usa el modelo que realmente se habria tenido esa manana.

UN DETALLE QUE DECIDE LA COMPARACION: el l2=1000 se eligio para 252,000
rondas de entrenamiento. Aplicado a una ventana de 3 dias (860 rondas)
aplastaria todos los pesos a cero y la ventana corta perderia por una razon
que no tiene nada que ver con la pregunta. La penalizacion se escala con el
tamano de la ventana para que el encogimiento relativo sea el mismo, que es
la unica comparacion justa.

    python3 short_window_test.py --data data/btcusdt_1m_long.csv
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone

import predict_model as pm

L2_REF = 1000.0
N_REF = 252428  # rondas con las que se eligio ese l2
MARGIN = 0.05
ITERS = 5
DIA_MS = 24 * 3600 * 1000


def dia_de(ms):
    return ms // DIA_MS


def evaluar(w, b, data, margin=MARGIN):
    hits = tot = 0
    todos_h = 0
    for d in data:
        p = pm.predict(w, b, d["v"])
        if (p >= 0.5) == (d["y"] == 1):
            todos_h += 1
        if abs(p - 0.5) < margin:
            continue
        tot += 1
        if (p >= 0.5) == (d["y"] == 1):
            hits += 1
    return hits, tot, todos_h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--dias", type=int, default=120, help="dias a evaluar")
    ap.add_argument("--ventanas", default="3,7,14,30,90")
    args = ap.parse_args()

    print("Cargando...")
    rows = pm.build_dataset(pm.load_csv(args.data))
    rows.sort(key=lambda r: r["t"])
    keys = sorted(rows[0]["x"].keys())

    por_dia = defaultdict(list)
    for r in rows:
        por_dia[dia_de(r["t"])].append(r)
    dias = sorted(por_dia)
    evaluar_dias = dias[-args.dias:]
    ventanas = [int(x) for x in args.ventanas.split(",")]

    d0 = datetime.fromtimestamp(evaluar_dias[0] * DIA_MS / 1000, tz=timezone.utc)
    d1 = datetime.fromtimestamp(evaluar_dias[-1] * DIA_MS / 1000, tz=timezone.utc)
    print(f"Rondas: {len(rows):,}")
    print(f"Evaluando {len(evaluar_dias)} dias: {d0:%d-%b-%Y} a {d1:%d-%b-%Y}")
    print(f"Ventanas a probar: {ventanas} dias, mas '4 anios' como referencia\n")

    # Referencia: el modelo congelado que se envia, entrenado con el 60% mas viejo.
    cut = int(len(rows) * 0.6)
    trR, statsR = pm.standardize(rows[:cut], keys)
    wR, bR = pm.train_logistic_newton(trR, keys, l2=L2_REF, iters=ITERS)

    tot = {v: [0, 0, 0, 0] for v in ventanas}   # hits_conf, n_conf, hits_todas, n_todas
    tot["ref"] = [0, 0, 0, 0]

    for i, d in enumerate(evaluar_dias):
        te_raw = por_dia[d]
        if len(te_raw) < 100:
            continue

        teR, _ = pm.standardize(te_raw, keys, statsR)
        h, n, ht = evaluar(wR, bR, teR)
        tot["ref"][0] += h
        tot["ref"][1] += n
        tot["ref"][2] += ht
        tot["ref"][3] += len(te_raw)

        for v in ventanas:
            prev = [r for r in rows if d - v <= dia_de(r["t"]) < d]
            if len(prev) < 400:
                continue
            # Encogimiento relativo constante: sin esto la ventana corta pierde
            # por estar sobre-regularizada, no por ser corta.
            l2 = L2_REF * len(prev) / N_REF
            tr, stats = pm.standardize(prev, keys)
            w, b = pm.train_logistic_newton(tr, keys, l2=l2, iters=ITERS)
            te, _ = pm.standardize(te_raw, keys, stats)
            h, n, ht = evaluar(w, b, te)
            tot[v][0] += h
            tot[v][1] += n
            tot[v][2] += ht
            tot[v][3] += len(te_raw)

        if (i + 1) % 20 == 0:
            print(f"  ... {i+1}/{len(evaluar_dias)} dias", flush=True)

    print("\n" + "=" * 78)
    print("RESULTADO")
    print("=" * 78)
    print(f"  {'ventana':<16}{'confianza >= 0.55':>28}{'opinando siempre':>26}")
    print("  " + "-" * 74)

    filas = []
    for clave in ["ref"] + ventanas:
        hc, nc, ht, nt = tot[clave]
        if not nc or not nt:
            continue
        pc, lc, hic = pm.wilson(hc, nc)
        pt, lt, hit = pm.wilson(ht, nt)
        nombre = "4 anios (actual)" if clave == "ref" else f"ultimos {clave} dias"
        print(f"  {nombre:<16}"
              f"{pc*100:>8.2f}% [{lc*100:.1f}, {hic*100:.1f}] n={nc:<6}"
              f"{pt*100:>10.2f}% [{lt*100:.1f}, {hit*100:.1f}]")
        filas.append((nombre, pc, lc, nc, clave))

    print("\n" + "=" * 78)
    print("LECTURA")
    print("=" * 78)
    ref = next((f for f in filas if f[4] == "ref"), None)
    cortas = [f for f in filas if f[4] != "ref"]
    if ref and cortas:
        mejor = max(cortas, key=lambda f: f[1])
        dif = (mejor[1] - ref[1]) * 100
        # ¿La diferencia es real o entra en el ruido de las dos muestras?
        se = ((ref[1] * (1 - ref[1]) / ref[3]) + (mejor[1] * (1 - mejor[1]) / mejor[3])) ** 0.5
        z = (mejor[1] - ref[1]) / se if se else 0
        print(f"  Mejor ventana corta: {mejor[0]} con {mejor[1]*100:.2f}%")
        print(f"  Contra 4 anios ({ref[1]*100:.2f}%): {dif:+.2f} pp, z = {z:+.2f}")
        print()
        if z > 1.96:
            print("  La ventana corta gana de forma significativa. Ajustar el modelo")
            print("  con los dias recientes es mejor que promediar 4 anios, y hay")
            print("  que cambiar como se entrena.")
        elif z < -1.96:
            print("  La ventana corta pierde de forma significativa. Con pocos dias")
            print("  no alcanza la muestra para estimar 15 pesos: el modelo termina")
            print("  ajustando ruido reciente en vez de estructura.")
        else:
            print("  No hay diferencia distinguible. Los dias recientes no aportan")
            print("  sobre los 4 anios NI viceversa -- para estas features, la")
            print("  relacion entre ellas y el resultado no cambia mes a mes.")
        print()
        print("  Ojo con como leer esto: lo que se prueba aca es de donde salen los")
        print("  PESOS. Las features siempre miden el mercado del momento -- los")
        print("  ultimos 5, 15, 60 y 240 minutos --, en las dos configuraciones.")


if __name__ == "__main__":
    main()
