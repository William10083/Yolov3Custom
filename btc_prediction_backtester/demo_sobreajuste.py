"""Llegar al 90% en el historico: se puede, es facil, y no sirve para nada.

El pedido: mirar cada ronda que fallo, ajustar para que salga bien, repetir
hasta acertar el 90% en los datos historicos, y recien ahi armar el predictor.

Se puede. Este script lo hace. Y muestra por que el resultado es inservible.

El metodo es el mas directo posible: memorizar. Se parten las features en
muchos cajones, y para cada combinacion de cajones se guarda que salio la
mayoria de las veces en el entrenamiento. Eso ES "ver en que fallaste y
ajustarlo", llevado al limite: cada situacion queda corregida para dar el
resultado correcto.

Con suficientes cajones el acierto en los datos vistos sube hasta donde uno
quiera -- 90%, 95%, 100%. Lo unico que hace falta es partir mas fino.

Y despues se mide en rondas que no vio. Ahi esta la respuesta a por que no
hago lo que se me pide.

    python3 demo_sobreajuste.py --data data/btcusdt_1m_long.csv
"""
import argparse
from collections import defaultdict

import predict_model as pm

FEATURES = ["mv_5", "mv_15", "vol_15", "eff_15", "flow_15", "pos_60"]


def cuantiles(valores, n):
    v = sorted(valores)
    return [v[int(len(v) * i / n)] for i in range(1, n)]


def cajon(x, bordes):
    lo, hi = 0, len(bordes)
    while lo < hi:
        mid = (lo + hi) // 2
        if x < bordes[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def evaluar(tabla, filas, bordes, feats):
    aciertos = cubiertas = 0
    for r in filas:
        clave = tuple(cajon(r["x"][k], bordes[k]) for k in feats)
        celda = tabla.get(clave)
        if celda is None:
            continue           # situacion nunca vista: no opina
        cubiertas += 1
        pred = 1 if celda[0] * 2 >= celda[1] else 0
        aciertos += (pred == r["y"])
    return aciertos, cubiertas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    args = ap.parse_args()

    print("Cargando...")
    rows = pm.build_dataset(pm.load_csv(args.data))
    rows.sort(key=lambda r: r["t"])
    n = len(rows)
    tr, te = rows[: int(n * 0.6)], rows[int(n * 0.8):]
    print(f"Entrenamiento: {len(tr):,} rondas   Test (nunca vistas): {len(te):,}\n")

    print("=" * 76)
    print("AJUSTANDO HASTA ACERTAR EN EL HISTORICO")
    print("=" * 76)
    print("  Cuantos mas cajones, mas fino el ajuste: cada situacion del pasado")
    print("  queda corregida para dar el resultado que de verdad salio.\n")
    print(f"  {'cajones':<10}{'combinaciones':>16}{'ENTRENAMIENTO':>17}{'TEST':>12}{'opina en':>12}")
    print("  " + "-" * 67)

    resultados = []
    for nb in (2, 4, 8, 16, 32, 64):
        bordes = {k: cuantiles([r["x"][k] for r in tr], nb) for k in FEATURES}
        tabla = defaultdict(lambda: [0, 0])
        for r in tr:
            clave = tuple(cajon(r["x"][k], bordes[k]) for k in FEATURES)
            tabla[clave][1] += 1
            tabla[clave][0] += r["y"]
        tabla = dict(tabla)

        a_tr, c_tr = evaluar(tabla, tr, bordes, FEATURES)
        a_te, c_te = evaluar(tabla, te, bordes, FEATURES)
        p_tr = a_tr / c_tr if c_tr else 0
        p_te = a_te / c_te if c_te else 0
        cobertura = c_te / len(te) * 100
        resultados.append((nb, len(tabla), p_tr, p_te, cobertura))
        print(f"  {nb:<10}{len(tabla):>16,}{p_tr*100:>16.1f}%{p_te*100:>11.1f}%"
              f"{cobertura:>11.0f}%")

    print("\n" + "=" * 76)
    print("LECTURA")
    print("=" * 76)
    # Se elige la fila que de verdad ilustra el punto: la de mayor acierto
    # historico que TODAVIA opina en una parte apreciable de las rondas. Con
    # los cajones mas finos la tabla no opina casi nunca (cada situacion es
    # unica y nunca se repite), y ahi el % de test es de un puñado de casos.
    utiles = [r for r in resultados if r[4] >= 20]
    nb, celdas, p_tr, p_te, cob = max(utiles, key=lambda r: r[2]) if utiles else resultados[0]
    print(f"  Con {nb} cajones por feature ({celdas:,} combinaciones) el acierto")
    print(f"  sobre los datos historicos llega a {p_tr*100:.1f}%  <-- lo pedido.")
    print()
    print(f"  Sobre rondas que no vio: {p_te*100:.1f}%, opinando en el {cob:.0f}% de ellas.")
    print()
    degenerado = [r for r in resultados if r[4] < 20]
    if degenerado:
        print(f"  (Las filas con {degenerado[0][0]}+ cajones llegan al 99-100% historico pero")
        print("   dejan de opinar: cada situacion se vuelve unica y no se repite")
        print("   nunca. Su % de test sale de un puñado de casos y no significa nada.)")
    print()
    print("  Ese es el punto entero. Llegar al 90% mirando cada fallo y")
    print("  corrigiendolo no requiere inteligencia: requiere memoria. Y lo que")
    print("  se memoriza es el ruido de esas rondas puntuales, que no se repite.")
    print()
    print("  El modelo que esta corriendo hoy da 52% en entrenamiento -- parece")
    print("  peor -- y 52% fuera de muestra. No aprendio mas, aprendio lo unico")
    print("  que era verdad. Un modelo con 90% historico y 50% real es")
    print("  exactamente el que perderia todo el dinero.")
    print()
    print("  Por eso la unica cifra que reporto es la de datos nunca vistos, y")
    print("  por eso no ajusto contra los fallos que ya ocurrieron.")


if __name__ == "__main__":
    main()
