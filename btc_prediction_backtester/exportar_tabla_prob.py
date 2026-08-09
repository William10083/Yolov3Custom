"""Tabla compacta de P(cierra Up) para cualquier instante de la ronda.

La necesita `latencia_mercado.py`, que corre en el telefono y no puede cargar
los 4 anios de velas. Aca se destila todo eso en unos cientos de numeros.

Se indexa por (segundos transcurridos en la ronda, z) donde

    z = movimiento hasta ahora / (sigma_1min x raiz de los MINUTOS QUE FALTAN)

Esa normalizacion es la que importa: lo que decide si un movimiento se
revierte no es cuan raro fue, sino cuanto tiempo queda para deshacerlo.

La probabilidad sale de la distribucion EMPIRICA de los retornos, no de una
campana. Los retornos de BTC a este horizonte tienen curtosis de 35 a 69
contra 3.0 de una gaussiana, y usar la campana da errores de varios puntos
justo en las celdas que mas se usan.

    python3 exportar_tabla_prob.py --data data/btcusdt_1m_long.csv
"""
import argparse
import bisect
import json
import math
import os
import statistics
from collections import defaultdict

import predict_model as pm

ROUND_MS = 5 * 60 * 1000
OUT = os.path.join(os.path.dirname(__file__), "tabla_prob.json")
# Bordes de z; mas finos cerca de 0, que es donde esta casi toda la masa.
BORDES = [-3, -2, -1.5, -1, -0.7, -0.45, -0.25, -0.1,
          0.1, 0.25, 0.45, 0.7, 1, 1.5, 2, 3]


def cajon(z):
    return bisect.bisect_left(BORDES, z)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    args = ap.parse_args()

    print("Cargando...")
    by = {c["open_time_ms"]: c for c in pm.load_csv(args.data)}
    starts = sorted(t for t in by if t % ROUND_MS == 0)
    print(f"Rondas: {len(starts):,}")

    tabla = defaultdict(lambda: [0, 0])
    sigmas = []
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
        if len(rets) < 2:
            continue
        sd1 = statistics.pstdev(rets) * p0
        if sd1 <= 0:
            continue
        sigmas.append(sd1 / p0)
        y = 1 if pf > p0 else 0
        for m in (1, 2, 3, 4):
            c = by.get(s + m * 60_000)
            if c is None:
                continue
            z = (c["open"] - p0) / (sd1 * math.sqrt(5 - m))
            k = f"{m}|{cajon(z)}"
            tabla[k][1] += 1
            tabla[k][0] += y

    salida = {}
    for k, (ups, n) in tabla.items():
        if n >= 200:
            salida[k] = [round(ups / n, 4), n]

    payload = {
        "bordes_z": BORDES,
        "tabla": salida,
        "sigma_relativa_mediana": round(statistics.median(sigmas), 8),
        "nota": ("P(cierra Up) por (minuto de la ronda, cajon de z). "
                 "z = movimiento / (sigma_1min * raiz(minutos restantes)). "
                 "Construida sobre la distribucion empirica, no gaussiana."),
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"\nGuardado en {OUT}  ({len(salida)} celdas con >=200 casos)")

    print("\nP(cierra Up) segun el movimiento y el minuto:")
    print(f"  {'z':<14}" + "".join(f"{'min '+str(m):>10}" for m in (1, 2, 3, 4)))
    print("  " + "-" * 54)
    for i in range(len(BORDES) + 1):
        et = (f"< {BORDES[0]}" if i == 0 else f"> {BORDES[-1]}" if i == len(BORDES)
              else f"{BORDES[i-1]:+.2f}..{BORDES[i]:+.2f}")
        fila = f"  {et:<14}"
        for m in (1, 2, 3, 4):
            v = salida.get(f"{m}|{i}")
            fila += f"{v[0]*100:>9.1f}%" if v else f"{'--':>10}"
        print(fila)


if __name__ == "__main__":
    main()
