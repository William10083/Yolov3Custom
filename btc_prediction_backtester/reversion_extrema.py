"""Cuando la ronda ya se movio mucho, ¿cuantas veces revierte igual?

El planteo (del usuario): si BTC ya subio $100 dentro de la ronda, el precio
de "Down" se desploma a centavos. Comprando a 0.01 los $10 compran ~980
acciones, asi que basta acertar 1 de cada 98 para empatar. Y a veces revierte.

Es un planteo distinto a todo lo anterior. El modelo de este repo predice al
INICIO de la ronda, cuando la cosa esta pareja. Esto es entrar a mitad de
ronda, cuando ya esta desbalanceada y las cuotas son extremas.

Este script mide la primera mitad de la pregunta -- la unica que se puede
contestar con velas: dado que en el minuto m la ronda lleva un movimiento X,
¿que fraccion de esas rondas cierra del lado contrario?

Eso da la probabilidad REAL de la reversion. La segunda mitad -- a cuanto la
cobra el mercado -- necesita el libro de ordenes, pero con la probabilidad
real ya se sabe cual es el precio maximo que valdria la pena pagar.

    python3 reversion_extrema.py --data data/btcusdt_1m_long.csv

MUY IMPORTANTE PARA LEER EL RESULTADO: en mercados de apuestas existe el
"sesgo favorito-longshot" -- la gente paga de mas por los boletos de loteria,
asi que los longshots suelen estar CAROS, no baratos. Que la reversion pague
100x no dice nada por si solo; hay que compararlo contra su probabilidad real.
"""
import argparse
import statistics
from collections import defaultdict

import predict_model as pm

ROUND_MS = 5 * 60 * 1000
FEE = 0.02


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    args = ap.parse_args()

    print("Cargando...")
    candles = pm.load_csv(args.data)
    by = {c["open_time_ms"]: c for c in candles}
    starts = sorted(t for t in by if t % ROUND_MS == 0)
    print(f"Rondas: {len(starts):,}\n")

    # Bucket por minuto transcurrido y por tamano del movimiento hasta ahi,
    # medido en desviaciones tipicas del minuto para que $100 en un mercado
    # violento y $100 en uno quieto no caigan en el mismo cajon.
    datos = defaultdict(lambda: [0, 0])          # (minuto, cajon) -> [revirtio, total]
    dolares = defaultdict(lambda: [0, 0])        # (minuto, cajon$) -> [revirtio, total]

    for s in starts:
        op = by.get(s)
        fin = by.get(s + ROUND_MS)
        if op is None or fin is None:
            continue
        p0 = op["open"]
        pf = fin["open"]
        if pf == p0:
            continue  # empate exacto: resuelve 50-50, no cuenta para ninguno
        cierre_arriba = pf > p0

        # Volatilidad por minuto de la hora previa, para escalar el movimiento.
        prev = [by.get(s - k * 60_000) for k in range(1, 61)]
        prev = [c["open"] for c in prev if c]
        if len(prev) < 30:
            continue
        rets = [(prev[i] - prev[i - 1]) / prev[i - 1] for i in range(1, len(prev)) if prev[i - 1]]
        sd = statistics.pstdev(rets) * p0 if len(rets) > 1 else 0
        if sd <= 0:
            continue

        for m in (1, 2, 3, 4):
            pm_ = by.get(s + m * 60_000)
            if pm_ is None:
                continue
            mov = pm_["open"] - p0
            if mov == 0:
                continue
            va_arriba = mov > 0
            revirtio = cierre_arriba != va_arriba

            # En sigmas del minuto, escaladas por los minutos transcurridos.
            z = abs(mov) / (sd * (m ** 0.5))
            cajon = (
                "0-1σ" if z < 1 else "1-2σ" if z < 2 else
                "2-3σ" if z < 3 else "3-4σ" if z < 4 else "4σ+"
            )
            datos[(m, cajon)][1] += 1
            if revirtio:
                datos[(m, cajon)][0] += 1

            d = abs(mov)
            # Cajones finos abajo: "<$10" mezclaba $0.50 con $9.99, que no
            # son el mismo evento ni de lejos, y justo ahi cae el caso real
            # observado en vivo (-$9 con Up cotizando 0.075).
            cajon_d = (
                "<$3" if d < 3 else "$3-6" if d < 6 else "$6-10" if d < 10 else
                "$10-25" if d < 25 else "$25-50" if d < 50 else
                "$50-100" if d < 100 else "$100+"
            )
            dolares[(m, cajon_d)][1] += 1
            if revirtio:
                dolares[(m, cajon_d)][0] += 1

    def tabla(d, orden, titulo, unidad):
        print("=" * 78)
        print(titulo)
        print("=" * 78)
        print(f"  {'movimiento':<10}" + "".join(f"{'min '+str(m):>16}" for m in (1, 2, 3, 4)))
        print("  " + "-" * 74)
        for cajon in orden:
            fila = f"  {cajon:<10}"
            for m in (1, 2, 3, 4):
                h, n = d.get((m, cajon), [0, 0])
                if n < 100:
                    fila += f"{'--':>16}"
                    continue
                p, lo, hi = pm.wilson(h, n)
                fila += f"{p*100:>9.1f}% n={n//1000}k" if n >= 1000 else f"{p*100:>10.1f}% n={n}"
            print(fila)
        print()
        print(f"  Cada celda: % de rondas que cerraron del lado CONTRARIO al")
        print(f"  movimiento que llevaban en ese minuto.\n")

    tabla(datos, ["0-1σ", "1-2σ", "2-3σ", "3-4σ", "4σ+"],
          "PROBABILIDAD DE REVERSION (movimiento en desviaciones tipicas)", "sigma")
    tabla(dolares, ["<$3", "$3-6", "$6-10", "$10-25", "$25-50", "$50-100", "$100+"],
          "LO MISMO EN DOLARES (como se ve en la pantalla)", "usd")

    print("=" * 78)
    print("QUE PRECIO VALDRIA LA PENA PAGAR POR LA REVERSION")
    print("=" * 78)
    print("  Si la reversion ocurre el P% de las veces, pagar mas que P x 0.98")
    print("  pierde plata. Al reves: el multiplicador maximo que se justifica.\n")
    print(f"  {'movimiento':<10}{'minuto':<8}{'revierte':>11}{'precio max':>13}{'multiplo max':>15}")
    print("  " + "-" * 60)
    for cajon in ["$6-10", "$10-25", "$25-50", "$50-100", "$100+"]:
        for m in (2, 3, 4):
            h, n = dolares.get((m, cajon), [0, 0])
            if n < 300:
                continue
            p, lo, hi = pm.wilson(h, n)
            maxp = lo * (1 - FEE)   # limite inferior: lo prudente
            mult = 1 / maxp if maxp > 0 else 0
            print(f"  {cajon:<10}{m:<8}{p*100:>10.1f}%{maxp:>13.3f}{mult:>14.1f}x")
    print()
    print("  'precio max' usa el LIMITE INFERIOR del IC95%, que es lo prudente:")
    print("  con cuotas extremas un error de estimacion chico cambia todo.")

    print("\n" + "=" * 78)
    print("POR QUE LA TABLA EN DOLARES ENGAÑA")
    print("=" * 78)
    print("  Un caso real medido en vivo: BTC $64,790 con volatilidad de")
    print("  0.004%/min, 4 minutos corridos, movimiento de -$9. El mercado")
    print("  cotizaba la reversion ('Up') a 0.075.")
    print()
    print("    tabla en DOLARES  ($6-10, min 4):  revierte 20.3%  -> parece regalo")
    print("    tabla en SIGMAS   (1-2σ,  min 4):  revierte  1.3%  <- la correcta")
    print("    el mercado cobraba:                          7.5%")
    print()
    print("  En ese mercado sigma de 1 minuto son $2.59, asi que a los 4")
    print("  minutos $9 es 1.7 sigmas: un movimiento GRANDE, no chico. La")
    print("  tabla en dolares lo mete en el mismo cajon que $9 en un mercado")
    print("  violento, donde $9 no es nada.")
    print()
    print("  Resultado: el mercado cobraba 5.8 VECES la probabilidad real.")
    print("  Comprar esa reversion tiene EV de -83% por apuesta.")
    print()
    print("  Es exactamente el sesgo favorito-longshot: el boleto de loteria")
    print("  se paga de mas. Los 100x existen, pero salen menos veces de las")
    print("  que su precio sugiere. USAR SIEMPRE LA TABLA EN SIGMAS.")

    print("\n" + "=" * 78)
    print("LO QUE ESTO NO CONTESTA")
    print("=" * 78)
    print("  A cuanto lo cobra el mercado. Eso necesita el libro de ordenes, y")
    print("  es exactamente el dato que run_all.py esta juntando.")
    print()
    print("  Un dato propio ya observado: con BTC a -$9 de la apertura, 'Up'")
    print("  cotizaba 0.075. Comparalo contra la fila de <$10 mas arriba.")
    print()
    print("  Y el aviso que importa: en mercados de apuestas los longshots")
    print("  suelen estar CAROS, no baratos -- la gente paga de mas por el")
    print("  boleto de loteria. Que pague 100x no lo hace buen negocio; lo")
    print("  hace buen negocio solo si 100x supera su probabilidad real.")


if __name__ == "__main__":
    main()
