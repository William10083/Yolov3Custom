"""Cuando el modelo repite el mismo lado en rondas seguidas, ¿acierta igual?

En vivo salieron cinco DOWN consecutivos y fallaron los cinco, con BTC
subiendo en cada uno. Las cinco tenian la misma feature dominante: el precio
en el techo de su rango de 1h y 4h. O sea que no fueron cinco predicciones,
fue una sola -- "esto revierte" -- repetida cinco veces mientras el precio
seguia de largo.

Si eso es sistematico, es un defecto arreglable: bastaria con no repetir el
mismo lado en rondas consecutivas, o cobrar mas confianza para hacerlo. Si no
lo es, esos cinco fallos fueron una racha y no hay nada que cambiar.

ESTO ES UNA HIPOTESIS POSTERIOR A VER EL RESULTADO, que es la forma mas facil
de inventarse un patron. Asi que se mide como corresponde: primero en
validacion, y recien despues una sola mirada al test para confirmar o
descartar. Si aparece solo en uno de los dos, no es real.

    python3 racha_analysis.py --data data/btcusdt_1m_long.csv
"""
import argparse
from collections import defaultdict

import predict_model as pm

L2 = 1000.0
MARGIN = 0.05
ITERS = 5
ROUND_MS = 5 * 60 * 1000


def analizar(preds, raw, etiqueta):
    """preds: lista de (t, p_up, y). Agrupa en rachas de mismo lado en rondas
    consecutivas y compara el acierto segun la posicion dentro de la racha."""
    conf = [(t, p, y) for t, p, y in preds if abs(p - 0.5) >= MARGIN]
    if len(conf) < 200:
        print(f"  {etiqueta}: solo {len(conf)} llamadas, muestra insuficiente")
        return None
    conf.sort()

    pos_stats = defaultdict(lambda: [0, 0])
    largos = defaultdict(int)
    pos = 0
    largo_actual = 0
    anterior = None
    for t, p, y in conf:
        lado = "Up" if p >= 0.5 else "Down"
        if anterior and t - anterior[0] == ROUND_MS and lado == anterior[1]:
            pos += 1
        else:
            if largo_actual:
                largos[min(largo_actual, 5)] += 1
            pos = 1
            largo_actual = 0
        largo_actual += 1
        clave = min(pos, 4)
        pos_stats[clave][1] += 1
        if (p >= 0.5) == (y == 1):
            pos_stats[clave][0] += 1
        anterior = (t, lado)
    if largo_actual:
        largos[min(largo_actual, 5)] += 1

    print(f"\n  {etiqueta}  ({len(conf):,} llamadas de confianza)")
    print(f"    {'posicion en la racha':<24}{'acierto':>22}")
    nombres = {1: "1ra (racha nueva)", 2: "2da seguida", 3: "3ra seguida", 4: "4ta o mas"}
    resultado = {}
    for k in sorted(pos_stats):
        h, n = pos_stats[k]
        if n < 30:
            print(f"    {nombres[k]:<24} solo {n} casos")
            continue
        p_, lo, hi = pm.wilson(h, n)
        print(f"    {nombres[k]:<24} {h:>5}/{n:<6} {p_*100:>6.2f}%  "
              f"IC95% [{lo*100:.1f}%, {hi*100:.1f}%]")
        resultado[k] = (p_, h, n)

    total = sum(largos.values())
    print(f"    largo de racha: " + "  ".join(
        f"{k}{'+' if k == 5 else ''}:{v/total*100:.0f}%" for k, v in sorted(largos.items())))
    return resultado


def comparar(res, etiqueta):
    """¿La primera de una racha rinde distinto que las repeticiones?"""
    if not res or 1 not in res:
        return None
    p1, h1, n1 = res[1]
    rep_h = sum(res[k][1] for k in res if k > 1)
    rep_n = sum(res[k][2] for k in res if k > 1)
    if rep_n < 50:
        print(f"  {etiqueta}: pocas repeticiones ({rep_n}) para comparar")
        return None
    p2 = rep_h / rep_n
    se = ((p1 * (1 - p1) / n1) + (p2 * (1 - p2) / rep_n)) ** 0.5
    z = (p1 - p2) / se if se else 0
    print(f"  {etiqueta}: 1ra {p1*100:.2f}% (n={n1})  vs  repeticiones "
          f"{p2*100:.2f}% (n={rep_n})   diferencia {(p1-p2)*100:+.2f} pp, z={z:+.2f}")
    return z


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    args = ap.parse_args()

    print("Cargando...")
    rows = pm.build_dataset(pm.load_csv(args.data))
    rows.sort(key=lambda r: r["t"])
    keys = sorted(rows[0]["x"].keys())

    n = len(rows)
    a, bnd = int(n * 0.6), int(n * 0.8)
    tr_raw, va_raw, te_raw = rows[:a], rows[a:bnd], rows[bnd:]
    tr, stats = pm.standardize(tr_raw, keys)
    w, b = pm.train_logistic_newton(tr, keys, l2=L2, iters=ITERS)

    def preds_de(raw):
        std, _ = pm.standardize(raw, keys, stats)
        return [(raw[i]["t"], pm.predict(w, b, d["v"]), d["y"]) for i, d in enumerate(std)]

    print("\n" + "=" * 74)
    print("¿RINDE DISTINTO REPETIR EL MISMO LADO EN RONDAS SEGUIDAS?")
    print("=" * 74)

    res_va = analizar(preds_de(va_raw), va_raw, "VALIDACION")
    print()
    z_va = comparar(res_va, "validacion")

    res_te = analizar(preds_de(te_raw), te_raw, "TEST")
    print()
    z_te = comparar(res_te, "test")

    print("\n" + "=" * 74)
    print("LECTURA")
    print("=" * 74)
    if z_va is None or z_te is None:
        print("  Muestra insuficiente para decidir.")
        return
    ambos = (z_va > 1.96 and z_te > 1.96)
    ninguno = (abs(z_va) < 1.96 and abs(z_te) < 1.96)
    if ambos:
        print("  La primera de una racha rinde MEJOR que las repeticiones, y")
        print("  aparece en validacion Y en test. Es un defecto real: repetir el")
        print("  mismo lado en rondas seguidas es apostar de nuevo lo mismo, no")
        print("  una prediccion nueva. Conviene no repetir, o exigir mas")
        print("  confianza para hacerlo.")
    elif ninguno:
        print("  No hay diferencia ni en validacion ni en test: repetir el mismo")
        print("  lado rinde igual que empezar una racha nueva.")
        print()
        print("  O sea que los cinco DOWN seguidos que fallaron en vivo fueron")
        print("  una racha mala, no un defecto del modelo. Molesta igual --")
        print("  concentra el riesgo en una sola lectura del mercado -- pero")
        print("  arreglarlo no subiria el acierto.")
    else:
        print("  Aparece en una particion y no en la otra. Eso es exactamente lo")
        print("  que hace un patron inventado despues de ver el resultado.")
        print("  No hay que cambiar nada con esta evidencia.")

    print("\n  Recordatorio: esta hipotesis nacio DESPUES de ver cinco fallos en")
    print("  vivo. Por eso hacian falta las dos particiones de acuerdo, no una.")


if __name__ == "__main__":
    main()
