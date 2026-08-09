"""Probabilidad de cierre en CUALQUIER momento de la ronda, no solo al inicio.

El reclamo, repetido varias veces y correcto: el modelo de este repo solo
opina en el segundo 0, cuando el mercado cotiza ~0.50 de los dos lados. Pero
la ronda dura 5 minutos y el precio se mueve todo el tiempo; el momento de
apostar puede ser el minuto 3, no el 0.

Esto estima P(cierre del mismo lado que va ahora) para cada minuto de la
ronda, y lo compara contra lo que deberia valer si el precio fuera una
caminata aleatoria pura. Ahi esta la unica ventaja posible: donde la realidad
se separa de la caminata aleatoria, o donde el mercado se separa de la
realidad.

LA NORMALIZACION CORRECTA, que es facil de errar: para saber si un movimiento
se va a revertir NO importa cuan raro fue, importa cuanto tiempo queda para
deshacerlo. Un movimiento de 2 sigmas al minuto 1 tiene 4 minutos para
revertirse; el mismo al minuto 4 tiene uno solo. Por eso se divide por
sigma*raiz(minutos QUE FALTAN), no por los transcurridos.

Con esa normalizacion, una caminata aleatoria sin deriva da exactamente
P(cierra del mismo lado) = Phi(z). Cualquier separacion de esa curva es
estructura real del mercado -- momentum o reversion -- y es lo que se mide.

    python3 intraround_model.py --data data/btcusdt_1m_long.csv

Construye la tabla con el 60% mas viejo y la valida en el 20% mas reciente.
"""
import argparse
import math
import statistics
from collections import defaultdict

import predict_model as pm

ROUND_MS = 5 * 60 * 1000
FEE = 0.02
BORDES = [-4, -3, -2.5, -2, -1.5, -1, -0.6, -0.3, 0, 0.3, 0.6, 1, 1.5, 2, 2.5, 3, 4]


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def cajon(z):
    for i, b in enumerate(BORDES):
        if z < b:
            return i
    return len(BORDES)


def etiqueta_cajon(i):
    if i == 0:
        return f"< {BORDES[0]}"
    if i == len(BORDES):
        return f"> {BORDES[-1]}"
    return f"{BORDES[i-1]:+.1f} a {BORDES[i]:+.1f}"


def estados(candles):
    """(minuto, z_restante, cerro_arriba) para cada ronda y cada minuto."""
    by = {c["open_time_ms"]: c for c in candles}
    starts = sorted(t for t in by if t % ROUND_MS == 0)
    out = []
    for s in starts:
        op, fin = by.get(s), by.get(s + ROUND_MS)
        if op is None or fin is None:
            continue
        p0, pf = op["open"], fin["open"]
        if pf == p0:
            continue
        prev = [by.get(s - k * 60_000) for k in range(1, 61)]
        prev = [c["open"] for c in prev if c]
        if len(prev) < 30:
            continue
        rets = [(prev[i] - prev[i - 1]) / prev[i - 1] for i in range(1, len(prev)) if prev[i - 1]]
        sd1 = statistics.pstdev(rets) * p0 if len(rets) > 1 else 0
        if sd1 <= 0:
            continue
        for m in (1, 2, 3, 4):
            c = by.get(s + m * 60_000)
            if c is None:
                continue
            mov = c["open"] - p0
            restante = 5 - m
            z = mov / (sd1 * math.sqrt(restante))
            out.append((s, m, z, 1 if pf > p0 else 0))
    return out


def construir(datos):
    tabla = defaultdict(lambda: [0, 0])
    for _, m, z, y in datos:
        k = (m, cajon(z))
        tabla[k][1] += 1
        tabla[k][0] += y
    return tabla


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    args = ap.parse_args()

    print("Cargando...")
    candles = pm.load_csv(args.data)
    datos = estados(candles)
    datos.sort()
    print(f"Estados intra-ronda: {len(datos):,}")

    corte = int(len(datos) * 0.6)
    tr, te = datos[:corte], datos[int(len(datos) * 0.8):]
    tabla = construir(tr)
    print(f"Tabla construida con {len(tr):,} estados; validada con {len(te):,}\n")

    print("=" * 78)
    print("REALIDAD vs CAMINATA ALEATORIA")
    print("=" * 78)
    print("  z = movimiento / (sigma x raiz de minutos QUE FALTAN)")
    print("  Si el precio fuera azar puro, P(cierra arriba) = Phi(z) exacto.\n")
    print(f"  {'minuto':<8}{'z':<16}{'real':>9}{'azar puro':>12}{'diferencia':>13}{'n':>10}")
    print("  " + "-" * 68)
    desvios = []
    for m in (1, 2, 3, 4):
        for i in range(len(BORDES) + 1):
            h, n = tabla.get((m, i), [0, 0])
            if n < 500:
                continue
            real = h / n
            centro = (BORDES[i - 1] + BORDES[i]) / 2 if 0 < i < len(BORDES) else (
                BORDES[0] - 0.5 if i == 0 else BORDES[-1] + 0.5)
            esperado = phi(centro)
            dif = real - esperado
            _, lo, hi = pm.wilson(h, n)
            sig = "*" if (lo > esperado or hi < esperado) else " "
            desvios.append((abs(dif), m, i, real, esperado, n, lo, hi))
            print(f"  {m:<8}{etiqueta_cajon(i):<16}{real*100:>8.1f}%{esperado*100:>11.1f}%"
                  f"{dif*100:>+12.1f}{sig}{n:>10,}")
        print()

    print("=" * 78)
    print("DONDE MAS SE SEPARA DEL AZAR  (y si aguanta fuera de muestra)")
    print("=" * 78)
    tabla_te = construir(te)
    desvios.sort(reverse=True)
    print(f"  {'minuto':<8}{'z':<16}{'train':>9}{'azar':>9}{'TEST':>9}{'n test':>10}")
    print("  " + "-" * 62)
    confirmados = 0
    mirados = 0
    for _, m, i, real, esperado, n, lo, hi in desvios[:10]:
        h2, n2 = tabla_te.get((m, i), [0, 0])
        if n2 < 200:
            continue
        mirados += 1
        real2 = h2 / n2
        # Confirma si el test se desvia del azar en la MISMA direccion.
        mismo = (real - esperado) * (real2 - esperado) > 0
        _, lo2, hi2 = pm.wilson(h2, n2)
        fuerte = mismo and (lo2 > esperado or hi2 < esperado)
        if fuerte:
            confirmados += 1
        marca = "CONFIRMA" if fuerte else ("mismo signo" if mismo else "NO")
        print(f"  {m:<8}{etiqueta_cajon(i):<16}{real*100:>8.1f}%{esperado*100:>8.1f}%"
              f"{real2*100:>8.1f}%{n2:>10,}  {marca}")

    print("\n" + "=" * 78)
    print("LECTURA")
    print("=" * 78)
    print(f"  De las {mirados} celdas mas desviadas en train, {confirmados} se confirman")
    print(f"  en test con significancia.")
    print()
    if confirmados >= max(3, mirados * 0.5):
        print("  El precio intra-ronda NO es una caminata aleatoria: hay celdas")
        print("  donde la realidad se separa del azar de forma consistente. Ahi")
        print("  puede haber ventaja, y el paso siguiente es comparar esas celdas")
        print("  contra el precio que cobra el mercado en ese mismo momento.")
    else:
        print("  Dentro de la ronda el precio se comporta como una caminata")
        print("  aleatoria: la probabilidad real coincide con Phi(z) salvo ruido.")
        print()
        print("  Eso significa que entrar en el minuto 3 no da ninguna ventaja")
        print("  sobre entrar en el 0. La informacion es la misma que ya esta en")
        print("  el precio -- cuanto se movio y cuanto falta -- y esa cuenta la")
        print("  sabe hacer cualquiera.")
        print()
        print("  Lo unico que quedaria es el sesgo favorito-longshot medido en")
        print("  reversion_extrema.py: comprar el FAVORITO caro, no el longshot")
        print("  barato. Eso no necesita mejor prediccion, necesita que el")
        print("  mercado siga cobrando de mas por el boleto de loteria.")


if __name__ == "__main__":
    main()
